"""The action builders: state -> one action package.

`_ask`, `_dispatch_many`, the two `wait` answers, the close command list, and the stop package -- plus
`render_action`, the other end of the same contract: an action package as the caller reads it.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import feature_host, insertion, seam
from .nodes import mode_for, node_row, node_spec_path
from .prompts import build_prompt_text, launch_dir, launch_prompt_path
from .store import load_dag

#: The action's own cells. A feature's `fields` is its own data (`metric`, and whatever it wants a
#: reader to see beside the action), never a door into the answer itself: an offer saying `cycle`
#: would name another line, one saying `action` another kind of action, and `prompt` / `nodes` would
#: pretend an insertion is a dispatch. `_insertions_due` drops each of these with one `WARNING:`
#: naming the key, so a copy-paste into `fields` costs the feature that key and nothing else.
RESERVED_FIELDS = frozenset({
    'action', 'cycle', 'node_id', 'node_title', 'note', 'steps',
    'spec', 'add_command', 'spec_of', 'commands', 'insertions',
    # the dispatch / prompt shape an insertion deliberately does not have
    'nodes', 'mode', 'prompt', 'prompt_text',
})


def _ask(question: str, **extra) -> dict:
    return {'action': 'ask_user', 'cycle': None, 'question': question, 'commands': [], **extra}


def _node_entry(project: Path, cycle: dict, row: dict, mode: str) -> dict:
    """One node's entry inside a dispatch-shaped action.

    The id, its title, its mode, where its prompt file lands and the text to write there -- one
    builder, so the multi-node dispatch and the single-node one cannot spell their entries
    differently. `row` is a graph row with its id in it (`nodes.node_row`).
    """
    node_id = row['id']
    return {'node_id': node_id, 'node_title': row.get('title', ''), 'mode': mode,
            'prompt': str(launch_prompt_path(project, cycle['id'], node_id)),
            'prompt_text': build_prompt_text(node_id)}


def _dispatch_many(project: Path, cycle: dict, node_ids: list, dag: dict | None = None) -> dict:
    """One dispatch per node in a line's hand, gathered into the same action.

    On the dispatch side a multi-node line is several "one slot"s side by side: each node has its own
    prompt file, its own session and its own closing commands, and `dispatch <cycle>` opens them all
    together. A single-node line still gets the same shape, with exactly one entry in `nodes`.

    `dag` is the already-read graph when the caller has one (the close and the pick each read it
    once, and a feature gate is asked with that same copy).
    """
    dag = dag if dag is not None else load_dag(project)
    nodes = [_node_entry(project, cycle, node_row(dag, node_id), mode_for(node_id, dag))
             for node_id in node_ids]
    head = nodes[0]
    return {
        'action': 'dispatch', 'cycle': cycle['id'],
        'node_id': head['node_id'], 'node_title': head['node_title'],
        'nodes': nodes,
        # A multi-node dispatch does not know which node's prompt is "the" one: blanking the
        # singular pair keeps a caller that reads `prompt_text` alone from writing the head's file
        # twice and missing the rest -- `nodes` is the list to walk.
        'mode': head['mode'],
        'prompt': head['prompt'] if len(nodes) == 1 else '',
        'prompt_text': head['prompt_text'] if len(nodes) == 1 else '',
        'steps': [f'write each node\'s prompt text to its own prompt.txt under '
                  f'{launch_dir(project, cycle["id"])}',
                  'DSH: spawn one session per node (`dispatch <cycle>` does all of them; '
                  '`next --spawn` is the one-shot spelling of the same call)'],
        'commands': [],
    }


def _insert_node(cycle: dict, edit: dict, note: str, steps, fields: dict | None = None) -> dict:
    """One insertion source's package, as the caller reads it.

    The graph edit itself is `insertion.insert`'s (`edit`); what a source adds is the words: why this
    node, and where it went. The shape is the same for every source, because it is the same action --
    which is the point of the core mechanism.

    **It is not a dispatch and it is not shaped like one.** There is no `prompt` / `prompt_text` /
    `mode`: no session is opened, no prompt file is written, and the line's hand is not moved
    (`insertion.py`). The node sits in the graph and the next dispatch picks it up by the usual
    ranking, so a caller that reads `dispatch` fields here has nothing to read -- deliberately.

    `node_id` is the `NEW_NODE` placeholder until `writer.apply` has run `add` and rebound it, and
    the same goes for the paths in `steps`: the uuid does not exist before that.

    `fields` goes in FIRST, so the core's own cells below always win. The filter in `_insertions_due`
    is where a reserved key is dropped with a warning; this order is the second half of the same
    rule, and it holds even for a caller that hands unfiltered fields in.
    """
    return {
        **(fields or {}),
        'action': insertion.INSERT_ACTION, 'cycle': cycle['id'],
        'node_id': insertion.NEW_NODE, 'node_title': edit['title'],
        'note': note, 'steps': list(steps or []),
        # `add` and `spec_of` are the writer's half of the mechanism, not the caller's: `apply` pops
        # them before this action reaches anybody, and `insertion.mint` is what runs the one and
        # reads the uuid off the other.
        'spec': edit['spec_path'], 'add_command': edit['add_command'],
        'spec_of': edit['spec_of'], 'commands': edit['commands'],
    }


def _insertion_steps(project: Path, closed: str | None, target: dict) -> list:
    """What this action does, in the order a reader needs it: it is a graph edit, not a session.

    The words are the core's, not a feature's: they name the mechanism's own place (the generated
    spec's landing spot, which is `insertion.NEW_NODE` until `apply` has minted the uuid) and the two
    things an insertion deliberately does not do.

    `closed` is the node this insertion rides on at a CLOSE, or `None` at a PICK -- where no node was
    closed at all, so the words must not claim one was: the new node simply enters the graph in front
    of the node the pick had chosen.
    """
    target_id = (target or {}).get('id') or ''
    where = f'between {closed} and {target_id}' if closed else f'in front of {target_id}'
    return [f'insert a node with the generated spec '
            f'({node_spec_path(project, insertion.NEW_NODE)}), {where}',
            'no session was opened and the inserted node is not pinned: the next dispatch picks it '
            'by the usual ranking (on a line that fills several slots, this same close may pin the '
            'rest of its fill)' if closed else
            'no session was opened and the inserted node is not pinned: the next dispatch picks it '
            'by the usual ranking']


def _pick_insertions_due(project: Path, cycle: dict, offers: list, target: dict) -> dict | None:
    """The action that inserts one node per offer at a PICK, in front of the node the pick chose.

    The pick is the other timing an insertion is allowed at (see `feature_registry.TIMINGS`), and it
    is the same mechanism as the close's: each offer declares a node, `insertion.insert` makes it,
    and the ranking is what decides who runs first. What is different is that there is no node being
    closed: no close commands, no pins, and the place is `after=None` -- the new node has no parent,
    so it is ready the moment it exists.

    The picked node is NOT deferred by an edge here (nothing is wired to it): the pick was made by
    ranking, and this insertion's node is ranked by the same rules on the next step. Naming the pick
    in the words is what tells the reader which node it stood in front of.

    `None` when every offer had to be skipped (see `_insertions_due`): the pick then answers as it
    always did.
    """
    return _insertions_due(project, cycle, None, offers, target, [])


def _text_of(offer: dict):
    """One offer's text, evaluated once here, as the builder the graph edit and the mint call.

    The feature's own `spec_of()` runs inside `_insertions_due`'s `try`: `insertion.insert` needs the
    text before the close lands (`add --desc`), so a builder that raises would stop the close. What
    goes on into the action is this function instead -- it hands back that one text with the minted
    uuid filled in where the placeholder was, so `mint` never asks the feature a second time. A text
    that is not a string is refused here too: a builder answering `None` would fail while writing
    `spec.md`, long after the close has run.
    """
    text = offer['spec_of'](insertion.NEW_NODE)
    if not isinstance(text, str):
        raise TypeError(f"`spec_of` must return the node's text as a string, got "
                        f"{type(text).__name__}")
    return lambda node_id: insertion.fill(text, node_id)


def _own_fields(name: str, fields) -> dict:
    """One offer's `fields`, with the action's own cells dropped (one `WARNING:` per key).

    `fields` is the feature's data and nothing else. The shape check in `feature_host` refuses a
    value that is not a dict; this refuses the keys that belong to the action itself
    (`RESERVED_FIELDS`). A warning rather than a silent drop, because a `cycle` that vanishes is a
    bug its author has to see -- a copy-pasted `note` is the plausible accident.
    """
    kept = {}
    for key, value in dict(fields or {}).items():
        if key in RESERVED_FIELDS:
            feature_host.warn(name, 'field is reserved',
                              f'{key!r} is the action\'s own cell and was dropped')
            continue
        kept[key] = value
    return kept


def _insertions_due(project: Path, cycle: dict, closed: str | None, offers: list, target: dict,
                    close_commands: list, extra_note: str = '') -> dict | None:
    """The action that inserts one node per offer, between the node just closed and the one it picked.

    An insertion source (a feature's `offer`) says what it wants in the graph and the words around
    it; this joins those declarations with the core mechanism (`insertion.insert`) -- the uuid, the
    text and the edges have one implementation, and this is the action they are carried in.

    Its `commands` are the real graph edits, in order: the first insertion that lands carries the
    close commands ahead of it, so the node this close settles is marked before anything is created;
    the rest ride on that first run. Nothing is written here -- `apply` is the only writer, and
    `plan` must stay byte-for-byte read-only.

    **Two sources are siblings, not a chain.** Each offer is placed against the same pair (after the
    closed node, before the node the close had picked), so the inserted nodes are ready together and
    the pick decides who runs first -- an insertion never waits for another insertion.

    **Each offer is contained on its own.** Building one offer's edit runs its source's `spec_of()`
    eagerly (`_text_of`), and a feature whose builder raises must not stop somebody's close: one
    `WARNING:` line, that offer contributes nothing this pass, and the offers after it still land.
    With every offer skipped this returns `None`, and the caller answers the ordinary close -- the
    node is still settled and the next slot still dispatches, as if no feature had spoken.

    **The offer's `fields` is its own data.** The keys the action itself owns (`RESERVED_FIELDS`) are
    dropped from it, each with one warning: the action's identity -- which line, which kind, which
    node, the note, the steps -- is the core's answer and never something a feature can rewrite.

    `closed` is `None` for a PICK-time insertion (`_pick_insertions_due`): no node was closed, so
    there are no close commands to carry and the new nodes enter the graph with no parent.

    The picked node is named in `deferred_node`: it is still the next thing this line will do, just
    not yet -- the new node stands in front of it. What does NOT happen here is a dispatch: no
    session is opened and the inserted nodes are not pinned.
    """
    edits = []
    for offer in offers:
        try:
            edit = insertion.insert(
                project,
                title=offer['title'],
                tags=offer.get('tags') or (),
                spec_of=_text_of(offer),
                after=closed,
                before=(target or {}).get('id'),
                # The close runs once, however many insertions ride on it: the first one that
                # really lands carries it (an offer skipped above has no command list of its own).
                close_commands=close_commands if not edits else None,
            )
        except Exception as e:                   # noqa: BLE001 -- tolerant at runtime, strict in doctor
            feature_host.warn(offer.get('feature') or '?', 'failed', e)
            continue
        edit['fields'] = _own_fields(offer.get('feature') or '?', offer.get('fields'))
        edit['note'] = str(offer.get('note') or '')
        edit['steps'] = _insertion_steps(project, closed, target)
        edits.append(edit)

    if not edits:
        return None
    head = edits[0]
    note = head['note']
    if extra_note:
        note = f'{note}; {extra_note}' if note else extra_note
    # The first insertion is mirrored into the singular fields, so a reader that knows only the old
    # shape (one `spec`, one `node_id`) still reads something true; `insertions` is where the whole
    # list lives and is the field to walk when there is more than one.
    insertions = [{'title': edit['title'], 'tags': edit['tags'],
                   'spec_path': edit['spec_path'], 'add_command': edit['add_command'],
                   'spec_of': edit['spec_of'], 'commands': edit['commands'],
                   'fields': edit['fields'], 'note': edit['note'], 'steps': edit['steps']}
                  for edit in edits]
    return _insert_node(cycle, head, note, head['steps'], {
        **head['fields'],
        'insertions': insertions,
    })


def _ticket_wait(cycle: dict, node_id: str, holding=None) -> dict:
    """The node's work is finished and its close has not run yet -- the one live answer.

    This is the instant between the worker writing its signal and running its own closing call. The
    node is a *stable* state: nothing is consumed, nothing is written, and asking again gives the
    same answer. It must never be a dead end -- the caller that lands here is told, in the `note`
    and again in `close_with`, the exact command that closes the node.

    When the line still holds other nodes (`holding`), that sentence carries them too: their own
    notes are collected as they land, and for the whole line to finish, their own closes have to run.

    `--spawn` is deliberately NOT part of `close_with`: closing a node and starting the next session
    are two steps, because the worker still has to commit and push what it made before the next
    session boots on the same tree.
    """
    close_with = f'pf next --after {node_id} --json'
    others = [x for x in (holding or []) if x != node_id]
    tail = (f' (still holding {", ".join(x[:8] for x in others)}, their notes are collected as they '
            f'arrive)' if others else '')
    return {
        'action': 'wait', 'cycle': cycle['id'], 'node_id': node_id, 'commands': [],
        'close_with': close_with,
        'note': f'The note for {node_id[:8]} is already on disk, but this node has not been closed '
                f'out yet -- do not poll, and do not move the state yourself. '
                f'Closing it out is one command: {close_with} (the tool reads this node\'s note, '
                f'closes it, and then tells you whether the next slot is `dispatch` or '
                f'`stop_pipeline`).{tail}'
                f' Dispatch comes after this run\'s commit and push: '
                f'`pf dispatch {cycle["id"]}`.',
    }


def _sessions_wait(cycle: dict, node_ids: list, holding=None) -> dict:
    """Every slot in hand has its session open: there is nothing to dispatch, so wait for their notes.

    A dispatch that names a node whose session already runs would boot a second session onto it --
    nothing a caller can act on. The next step is their own closes (`next --after <node id>`), so
    this answers `wait`.
    """
    others = [x for x in (holding or []) if x not in node_ids]
    tail = (f' (still holding {", ".join(x[:8] for x in others)}, they keep running)'
            if others else '')
    names = ', '.join(x[:8] for x in node_ids)
    return {'action': 'wait', 'cycle': cycle['id'], 'node_id': node_ids[0], 'commands': [],
            'note': f'{names}: their sessions are already open (the `started` stamp is on disk) -- '
                    f'there is nothing to dispatch, so wait for their notes and never poll. '
                    f'Their own closes: pf next --after <node id> --json.{tail}'}


def _retry_wait(cycle: dict, node_id: str, holding=None) -> dict:
    """A `pending` signal is never a green light -- wait, and name the unwind.

    The note carries the literal commands of the documented retry: a retry is a tool command, not
    steps an agent has to remember. Three commands, and the `--drop` one cannot be skipped: a
    rewritten `pending` is a red light too, and clearing the note without taking it out leaves
    `next` answering `wait` forever (this has stalled a line before).
    """
    others = [x for x in (holding or []) if x != node_id]
    tail = (f' (still holding {", ".join(x[:8] for x in others)}, they keep running)'
            if others else '')
    return {'action': 'wait', 'cycle': cycle['id'], 'node_id': node_id, 'commands': [],
            'note': f'The note for {node_id[:8]} is still pending (no verdict yet, or somebody '
                    f'cleared the note to retry); do not poll. Three commands unwind it: '
                    f'pf cycle retry {cycle["id"]} {node_id} --drop --summary "retry", then '
                    f'pf cycle hold {cycle["id"]} {node_id}, then pf next --spawn --json.'
                    f'{tail}'}


def _close_commands(project: Path, cycle: dict, node_id: str, outcome: str,
                    decision: str, next_nodes: list | None, park: bool = True) -> list:
    """The mechanical half of a node's close: record the step, mark the node, pick or park.

    `outcome` is the word the path step keeps (`completed` / `failed` / `stopped` -- the ticket's own
    answer). The DAG is marked in the vocabulary IT owns: a node that delivered is `completed`, one
    that did not is `shelved` (out of play, waiting for a human), and a node being worked on is
    `todo`.

    **Stopping is turning inactive, one hardcoded command.** Whether this line should still push on
    is not pinned on the graph: the graph grows new nodes, and whether a stopped line should keep
    running is the human's call. The tool never writes `active`.

    A node that was not delivered is shelved, but whether the line picks anyone after it walks the
    same fork (the `when_shelved` policy already decided at that step): `shelved` only says this node
    is out of play waiting for a human -- whether the line keeps going is a separate matter.

    `park=False` registers only and writes not one word of status -- on a line that is not in
    service, the status is the human's to give and the tool has no business touching it (the close
    gate uses it).

    `next_nodes` is the **whole list** this close fills in, and one is pinned per entry: the slots
    freed can be more than one (`parallel` minus what is still in hand), and pin one too few and that
    slot sits quietly empty -- the action's `nodes` reports it while the hand on disk does not hold
    it, and the dispatch goes by what the hand holds.
    """
    cmds = [seam.cmd('cycle', 'close', cycle['id'], node_id, '--result', outcome),
            seam.dag('status', node_id, 'completed' if outcome == 'completed' else 'shelved')]
    if decision == 'continue' and next_nodes:
        cmds.extend(seam.cmd('cycle', 'hold', cycle['id'], n['id']) for n in next_nodes)
        return cmds
    if park:
        cmds.append(seam.cmd('cycle', 'status', cycle['id'], 'inactive'))
    return cmds


def _stop_action(project: Path, cycle: dict, reason: str, cmds: list,
                 node_id: str | None = None, shelved_nodes=(), stop_line: bool = True,
                 dag: dict | None = None) -> dict:
    return {'action': 'stop_pipeline', 'cycle': cycle['id'], 'node_id': node_id,
            'reason': reason,
            'note': ('cycle parked; nothing more to do without a human' if stop_line else
                     'the line stays in service: the nodes still in hand carry it on when their '
                     'own closes run'),
            # The run is ending, so this is the last place the human-facing loose ends can be
            # handed over. `attention: []` when there is nothing to look at.
            'attention': attention_items(project, cycle, shelved_nodes, dag=dag),
            'commands': cmds}


def attention_items(project: Path, cycle: dict, shelved_nodes=(), dag: dict | None = None) -> list:
    """The things a human has to look at, carried by the stop package.

    Two sources, collected in this order:

      1. the nodes this very action is about to shelve (`shelved_nodes`). They are still `todo` on
         disk while this package is built -- the `dag-manage status ... shelved` command has not run
         yet -- so they have to be named here, or the one package that explains the stop would be the
         one place the shelved node is not mentioned.
      2. the DAG's already-`shelved` nodes (the work is out of play, and `dag-manage revive <id>` is
         the command that puts a node back in: `shelved` → `todo`).

    A node that just CLOSED as completed is deliberately not in here: this list is read as "go look
    at these", and naming a success would send the reader to revive work that is already done.

    **`shelved` is a node status; `failed` is a verdict on the note**: a session reporting `failed`
    is saying "I handed over nothing", and the close shelves this node on that basis (marked
    `shelved`, out of play waiting for a human). So what is spoken of here is node status, while the
    entry on the path, and `signal.py`'s `failed`, speak of the verdict.
    """
    out = []
    nodes = (dag if dag is not None else load_dag(project)).get('nodes') or {}
    shelved = list(dict.fromkeys(list(shelved_nodes) + [uid for uid, n in nodes.items()
                                                       if n.get('status') == 'shelved']))
    for uid in shelved:
        if not uid:
            continue
        node = nodes.get(uid) or {}
        out.append({
            'kind': 'node_shelved', 'cycle': cycle['id'], 'node_id': uid,
            'node_title': node.get('title', ''),
            'hint': f'dag-manage revive {uid} puts it back to todo (shelved → todo), or handle it '
                    f'by hand and restart the cycle',
        })
    return out


# ── an action as the caller reads it ─────────────────────────────────────────

def render_action(action: dict, as_json: bool = False, preview: bool = False) -> str:
    """One action as the caller reads it -- the ONE place both `next` front ends print from.

    `preview` is the `--plan` run: **a preview is not executable**. A plan that still carries
    `commands` has stopped half way (the mechanical steps have not run yet), and that case reports
    only "still settling" -- it never hands the commands to the caller. This is that invariant's
    landing point on the output side, shared by both front ends (`conduct_pipeline.py` and
    `state-manage.py next`).

    **At every depth.** An insertion action carries its steps twice -- once mirrored at the top and
    once per insertion (`insertions`, one per source that asked) -- and the nested copy holds the
    same `add`, the same edge edits and the same `spec_of` callable. A preview that scrubbed only the
    top would hand a caller an executable list through the side door (and `--plan --json` would not
    even serialize a callable). What stays is what a reader needs: `title`, `spec_path`, `fields`,
    `steps`, `note`.

    `as_json` is only the shape: JSON is for readers other than a human (`ensure_ascii=False`, so
    non-ASCII text is passed through as it is), otherwise the human-readable version.
    """
    if preview:
        pending = bool(action.pop('commands', None))
        # The writer's half of an insertion at the top level: the same three fields `apply` pops
        # before an executed action reaches anybody. `spec_of` is a callable, so a preview that kept
        # it would not even serialize (`--plan --json`).
        pending = bool(action.pop('add_command', None)) or pending
        action.pop('spec_of', None)
        for item in action.get('insertions') or []:
            if not isinstance(item, dict):
                continue
            # ... and at the nested level, where each further insertion's copy lives. What stays is
            # what a reader needs: `title`, `tags`, `spec_path`, `fields`, `steps`, `note`.
            for key in ('commands', 'add_command'):
                pending = bool(item.pop(key, None)) or pending
            item.pop('spec_of', None)
        if pending:
            action['settling'] = True
    return (json.dumps(action, ensure_ascii=False, indent=2) if as_json
            else _action_lines(action))


def _field_lines(fields: dict) -> list:
    """The fields an insertion source reported, as the one or two lines a reader needs.

    Which measurement, what it read, where the line was, and the node the close had picked before
    this one was put in front of it. A reader who is not using `--json` is exactly the reader who
    needs to see why the graph grew a node.
    """
    lines = []
    if fields.get('metric'):
        lines.append(f"metric: {fields['metric']} = {fields.get('reading')} "
                     f"(line {fields.get('line')})")
    if fields.get('deferred_node'):
        lines.append(f"after:  {fields.get('deferred_title', '')}  ({fields['deferred_node']})")
    return lines


def _action_lines(action: dict) -> str:
    """The human-facing rendering: what the action is, and everything a reader has to act on."""
    lines = [f"action: {action['action']}"]
    if action.get('cycle'):
        lines.append(f"cycle:  {action['cycle']}")
    # `hint` is where the way back lives (the command that puts a line back in service, or the
    # one that pins the node a cycle with nothing in hand has to choose between): a reader who is
    # not using --json is exactly the reader who needs it, so it is rendered, not just returned.
    for key in ('note', 'reason', 'question', 'hint', 'blocked', 'close_with'):
        if action.get(key):
            lines.append(f"{key + ':':<9} {action[key]}")
    if action.get('node_id'):
        lines.append(f"node:   {action.get('node_title', '')}  ({action['node_id']})")
    for item in action.get('nodes') or []:
        if isinstance(item, dict) and item.get('node_id') != action.get('node_id'):
            lines.append(f"node:   {item.get('node_title', '')}  ({item['node_id']})")
    for step in action.get('steps', []):
        lines.append(f"  - {step}")
    if action.get('prompt'):
        lines.append(f"prompt: {action['prompt']}")
    # An insertion's landing spot: the file the node's own session will read. It has no prompt,
    # because nothing was opened -- this line and the one above are exactly that difference.
    # With several insertions the first one is the action's own (this line); the rest are rendered
    # below, each with the fields its own source reported.
    if action.get('spec'):
        lines.append(f"spec:   {action['spec']}")
    if action.get('action') == 'dispatch':
        lines.append(f"mode:   {action.get('mode')}")
    lines.extend(_field_lines(action))
    if 'applied' in action:
        lines.append(f"applied: {action['applied']}")
    for extra in (action.get('insertions') or [])[1:]:
        if not isinstance(extra, dict):
            continue
        lines.append(f"node:   {extra.get('title', '')}  "
                     f"({extra.get('node_id') or extra.get('spec_path', '')})")
        if extra.get('spec_path'):
            lines.append(f"spec:   {extra['spec_path']}")
        for step in extra.get('steps') or []:
            lines.append(f"  - {step}")
        lines.extend(_field_lines(extra.get('fields') or {}))
    for item in action.get('attention', []):
        who = f"{item.get('node_title')} ({item.get('node_id')})" if item.get('node_id') else '?'
        lines.append(f"attention: {item.get('kind')}  {who}")
        if item.get('hint'):
            lines.append(f"           {item['hint']}")
    return '\n'.join(lines)
