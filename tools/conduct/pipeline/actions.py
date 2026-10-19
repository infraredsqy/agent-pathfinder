"""The action builders: state -> one action package.

`_ask`, `_dispatch_many`, the two `wait` answers, the close command list, and the stop package -- plus
`render_action`, the other end of the same contract: an action package as the caller reads it.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import maintenance, seam
from .nodes import mode_for, node_row, node_spec_path
from .prompts import build_prompt_text, launch_dir, launch_prompt_path
from .store import DEFAULT_MODE, load_dag


def _ask(question: str, **extra) -> dict:
    return {'action': 'ask_user', 'cycle': None, 'question': question, 'commands': [], **extra}


def _node_entry(project: Path, cycle: dict, row: dict, mode: str) -> dict:
    """One node's entry inside a dispatch-shaped action.

    The id, its title, its mode, where its prompt file lands and the text to write there -- one
    builder, so the multi-node dispatch and the maintenance insert cannot spell their entries
    differently. `row` is a graph row with its id in it (`nodes.node_row`); the maintenance node has
    no row yet, so its caller hands over the id and the title it was generated with.
    """
    node_id = row['id']
    return {'node_id': node_id, 'node_title': row.get('title', ''), 'mode': mode,
            'prompt': str(launch_prompt_path(project, cycle['id'], node_id)),
            'prompt_text': build_prompt_text(node_id)}


def _dispatch_many(project: Path, cycle: dict, node_ids: list) -> dict:
    """One dispatch per node in a line's hand, gathered into the same action.

    On the dispatch side a multi-node line is several "one slot"s side by side: each node has its own
    prompt file, its own session and its own closing commands, and `dispatch <cycle>` opens them all
    together. A single-node line still gets the same shape, with exactly one entry in `nodes`.
    """
    dag = load_dag(project)
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


def _maintenance_due(project: Path, cycle: dict, closed: str, hit: dict, target: dict,
                     close_commands: list, extra_note: str = '') -> dict:
    """The action that inserts a maintenance node between the node just closed and the one it picked.

    Its `commands` are the real graph edit (`maintenance.insertion`): drop the old direct edge if it
    is there, create the maintenance node, wire the two edges, and pin the cycle on it. Nothing is
    written here -- `apply` is the only writer, and `plan` must stay byte-for-byte read-only.

    It is shaped like `_dispatch_many` on purpose ("the same walk as dispatch"): the same `prompt` /
    `prompt_text` / `mode` fields, so `dispatch <cycle>` and `next --spawn` need no second case. The
    difference is WHICH node is pinned -- the one just inserted, not the one the close had picked --
    and that the picked node is named in `deferred_node`, because it is still the next thing the
    line will do, just not yet.
    """
    node_id = maintenance.NEW_NODE
    entry = _node_entry(project, cycle, {'id': node_id, 'title': hit['title']}, DEFAULT_MODE)
    shown = (f'{hit["value"]:g}{hit["unit"]}')
    line = (f'{hit["at"]:g}{hit["unit"]}')
    note = (f'{hit["subject"]} read {shown}, over the line {line} in this line\'s policies '
            f'(`policies.{hit["line"]}`): the maintenance node is inserted between the just-closed '
            f'{closed[:8]} and the {target.get("title") or target["id"]} it had picked, and pinned '
            f'on the new node. Dispatch as with `dispatch`: after committing and pushing, run '
            f'`pf dispatch {cycle["id"]}`.')
    edit = maintenance.insertion(project, closed, target['id'], cycle['id'], hit,
                                 spec_of=hit['spec_of'], close_commands=close_commands)
    return {
        'action': maintenance.MAINTENANCE_ACTION, 'cycle': cycle['id'],
        **entry,
        'metric': hit['reading'], 'reading': shown, 'line': line,
        'deferred_node': target['id'], 'deferred_title': target.get('title', ''),
        # `extra_note` is the line that would have ridden on the dispatch this action replaces (a
        # hand-named node outside the filter set, for one). It has to keep riding along: the caller
        # never sees the action it displaced.
        'note': f'{note}; {extra_note}' if extra_note else note,
        'steps': [f'write prompt text to {entry["prompt"]}',
                  f'insert a maintenance node with the generated spec '
                  f'({node_spec_path(project, node_id)}), between {closed} and {target["id"]}',
                  'DSH: spawn one session with prompt.txt (`dispatch <cycle>` does this)'],
        # `add` is not in `commands`: it is handed over on its own, because its stdout carries the
        # uuid the rest of the list is written in terms of (`writer._capture_new_node`), and `spec_of`
        # is the generated spec, texted for the node that uuid turns out to name.
        'add_command': edit['add_command'],
        'spec_of': hit['spec_of'],
        'commands': edit['commands'],
    }


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

    `as_json` is only the shape: JSON is for readers other than a human (`ensure_ascii=False`, so
    non-ASCII text is passed through as it is), otherwise the human-readable version.
    """
    if preview and action.pop('commands', None):
        action['settling'] = True
    return (json.dumps(action, ensure_ascii=False, indent=2) if as_json
            else _action_lines(action))


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
    if action.get('action') in ('dispatch', maintenance.MAINTENANCE_ACTION):
        lines.append(f"mode:   {action.get('mode')}")
    # The maintenance channel's own fields: which measurement crossed, what it read, where the line
    # was, and the node the close had picked before this one was put in front of it. A reader who is
    # not using --json is exactly the reader who needs to see why the graph grew a node.
    if action.get('metric'):
        lines.append(f"metric: {action['metric']} = {action.get('reading')} "
                     f"(line {action.get('line')})")
    if action.get('deferred_node'):
        lines.append(f"after:  {action.get('deferred_title', '')}  ({action['deferred_node']})")
    if 'applied' in action:
        lines.append(f"applied: {action['applied']}")
    for item in action.get('attention', []):
        who = f"{item.get('node_title')} ({item.get('node_id')})" if item.get('node_id') else '?'
        lines.append(f"attention: {item.get('kind')}  {who}")
        if item.get('hint'):
            lines.append(f"           {item['hint']}")
    return '\n'.join(lines)
