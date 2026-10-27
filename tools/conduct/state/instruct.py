"""Standing instructions: the account and everything about "what is visible from where".

Owns the account's vocabulary and read/write helpers, the position rules that decide which
entries a reader sees from where the pipeline stands, and the `instruction` subcommands
(add / list / resolve / rm / revive).
"""
import json
import sys

from .store import (_holding_ids, _ledger_lock, _now, _read_dag, _read_instructions, _read_state,
                    _write_instructions, ledger_dir_for)
from .pipeline_loader import _load_pipeline


# ── Standing instructions ────────────────────────────────────────────────────
# One account, one writer (this CLI). Any session may ADD one; the visibility of what it added is
# decided by the entry's own `scope`, and the session that closes a node may RESOLVE one. The two
# moves that take an entry OFF the account -- archiving it, and taking it away text and all -- are
# the owner's alone. Three scopes; the defaults lean the "noisy" way on purpose (see the module notes
# below).

VALID_TARGETS = ('stage', 'boundary')

#: The word a missing `state` is read as: an entry with no `state` field counts as this word. The
#: reading side (`pipeline/instruct.py`) borrows the name through the loader instead of copying it.
LIVE_STATE = 'live'
#: The account has two states, and one of them is written by the owner alone: `live` (in force) and
#: `withdrawn` (archived -- still on the account, no longer read). Nothing else ends an entry: where
#: it can be seen from is derived on every read from its scope and the bound node/cycle (the impl's
#: `instruction_in_scope`), so a scoped entry goes out of sight on its own. A record carrying any
#: other word in `state` is not one of these; it is read as archived, and nothing writes it back
#: (`_retired`). Taking an entry off the account is NOT a third state: the entry is not in the list
#: at all, and the trace it leaves carries no `state` (`cmd_instruction_rm`).
VALID_INSTRUCTION_STATES = (LIVE_STATE, 'withdrawn')

#: Where the account keeps the traces a removal leaves: one `id` and the moment, no text. They are
#: what holds a removal against a pull from a machine that still has the entry (`tools/conduct/
#: ledger-merge.py`) and what keeps the id from being handed out again (`_next_instruction_id`). No
#: view of the account shows them -- archiving is for the reader, this is bookkeeping.
REMOVED_KEY = 'removed'

_SCOPE_RULES = None


def _scope_rules():
    """`conduct_pipeline.py` loaded by path: it owns the reading of the older `lifetime` field.

    Deliberately not a copy: `entry_scope()` there is the single place that decides what an entry
    with `once` / `run` / `forever` -- or with neither field -- is scoped as, and this command has to
    read the account exactly the way the pipeline reads it.
    """
    global _SCOPE_RULES
    if _SCOPE_RULES is None:
        _SCOPE_RULES = _load_pipeline('conduct_pipeline_scopes')
    return _SCOPE_RULES


def valid_scopes() -> tuple:
    """The `scope` vocabulary: `conduct_pipeline`'s one, not a second copy kept here.

    `SCOPES` lives in `pipeline/instruct.py` (the source of the judgement); this tool borrows it
    instead of copying -- the price of a copy is that after a fourth scope is added to the
    judgement, `--scope` refuses it first, at the argparse step.
    """
    return _scope_rules().SCOPES


def _scope_from_flag(raw: str | None) -> str | None:
    """A `--scope` value as a scope, or None when it is not one of the three."""
    if raw is None:
        return None
    return raw if raw in _scope_rules().SCOPES else None


def _retired(entry: dict) -> bool:
    """True unless the entry is in force: `live` is the only state that is read.

    `withdrawn` is the archive, and a record an older tool wrote with any other word in `state` is
    archived the same way -- out of force, still on the account, never rewritten.
    """
    return not _is_live(entry)


def _is_live(entry: dict) -> bool:
    return entry.get('state', LIVE_STATE) == LIVE_STATE


def _removed(data: dict) -> list:
    """The removal traces on this account: one pair of `id` + moment per entry taken off it.

    No key at all reads as none, and an account that never had a removal is written back with the
    key absent -- a file gains it the first time something is actually removed, and not before.
    """
    traces = data.get(REMOVED_KEY)
    return traces if isinstance(traces, list) else []


def _locate(data: dict, ins_id: str) -> tuple:
    """`(the entry, its removal trace)` for one id: at most one of the two is not None.

    Both None means the account never had this id -- which is a different refusal from an id whose
    entry was removed, and the difference is worth saying out loud (`_unknown_id_error`).
    """
    for item in data['instructions']:
        if item.get('id') == ins_id:
            return item, None
    for trace in _removed(data):
        if isinstance(trace, dict) and trace.get('id') == ins_id:
            return None, trace
    return None, None


def _unknown_id_error(ins_id: str, trace: dict | None, cannot: str) -> None:
    """The one refusal for an id that is not on the account, in its two shapes."""
    if trace is None:
        print(f'Error: no instruction {ins_id!r}', file=sys.stderr)
    else:
        print(f'Error: instruction {ins_id!r} was removed at {trace.get("at")}; {cannot}',
              file=sys.stderr)
    sys.exit(1)


def _next_instruction_id(data: dict) -> str:
    """The next id: one past the highest one ever handed out, removal traces included.

    Counting the list (`len(items) + 1`) was enough while nothing ever left it. A removal makes the
    count go BACK, and the id would be minted a second time -- on a ledger two machines pull from
    each other, the id is the only thing that tells "that entry came back" from "another entry with
    the same number". The traces are what the high-water mark survives on.
    """
    highest = 0
    for record in list(data.get('instructions') or []) + _removed(data):
        value = record.get('id') if isinstance(record, dict) else None
        if isinstance(value, str) and value.startswith('i-') and value[2:].isdigit():
            highest = max(highest, int(value[2:]))
    return f'i-{highest + 1:04d}'


# ── where the pipeline stands, and what is visible from there ──

def _cycle_of_node(active: list, node: str) -> str | None:
    """Node to cycle, backwards: which active cycle is holding this node right now?

    A node id is unique in the whole DAG, and a cycle's record says which set of nodes it holds at
    this moment -- so "which line the reader stands on" can be read off disk even with two active
    cycles side by side, with no guessing at a "current cycle". Only exactly one hit counts: when
    two lines hold the same node (the same work started by two lines at once), the answer is more
    than one, and None comes back.
    """
    hits = [c['id'] for c in active if node in _holding_ids(c)]
    return hits[0] if len(hits) == 1 else None


def _position(project: str | None = None, node: str | None = None) -> tuple:
    """`(current cycle id, current node id)` -- what `cycle current` reads, or `(None, None)`.

    Read through the store's own door (`_read_state`) rather than the module-level `LEDGER_DIR`, so
    `--project` decides which workspace answers.

    `node` is the reader saying where it stands (`instruction list --node <id>`), and it stands as
    named: the node in the line's hand is not substituted for it. That is what the flag is for -- the
    hand says where the LINE is, and the reader is saying which node it is the reader of. With one
    active cycle the cycle half comes from state.json; with two or more side by side the named node
    is what pins the line down as well (the active cycle holding it, when exactly one does).

    When nothing says where the reader stands -- no active cycle, two of them and no node named, or a
    named node no active line holds -- both halves come back None, and the reader judges each entry
    by the status of what it is bound to instead (the wide reading, see `instruction_in_scope`).
    """
    state = _read_state(project)
    active = [c for c in state.get('cycles', []) if c.get('status') == 'active']
    if len(active) == 1:
        cycle = active[0]
    elif node:
        cycle_id = _cycle_of_node(active, node)
        if cycle_id is None:
            return (None, None)
        cycle = next(c for c in active if c['id'] == cycle_id)
    else:
        return (None, None)
    if node:
        return (cycle['id'], node)
    holding = _holding_ids(cycle)
    return (cycle['id'], holding[0] if holding else None)


def _fold(entry: dict) -> dict:
    """One entry as the reader-facing view carries it: the sentence, where it is visible, how it is
    delivered. Ids and bookkeeping stay in the account."""
    return {
        'id': entry.get('id'),
        'text': entry.get('text', ''),
        'scope': _scope_rules().entry_scope(entry),
        'target': entry.get('target'),
    }


def _instruct_empty_lines(total: int, scope: str | None, global_total: int) -> list:
    """Nothing matched one of the ACCOUNT views (`--scope` / `--all` / `--live`), or the account is
    empty. Say which of the four situations this is.

    The distinctions matter to a reader: "no instructions" is a green light, while "the account
    exists but nothing matched" is a different thing and must not be described as the first. Getting
    this wrong in the reassuring direction is exactly how a live instruction reads as absent. The
    vocabulary is the account's: an entry is `global` / `cycle` / `node`, and the older word for the
    first of those has no place in an answer.
    """
    if not total:
        return ['This project has no instruction record at all.',
                'That is not an error: work can start as usual.']
    if scope:
        return [f'No instruction with scope={scope} is still in force.',
                f'(The instruction record has {total} entries.)']
    if not global_total:
        return ['This project has no global instruction.',
                f'(The instruction record has {total} entries, and none of them is global.)']
    return ['No instruction record is in force right now.',
            f'(The instruction record has {total} entries, and none of them is in force. Only the '
            f'owner can archive an instruction.)']


def _nothing_visible_lines(total: int) -> list:
    """The default view found nothing, on a project that has an account.

    This is the dangerous silence: a session that reads it must be able to tell "there
    is nothing for me here" from "there is nothing at all", and the second sentence is what keeps it
    from reading as a green light.
    """
    return ['No instruction is visible from this step.',
            f'(The instruction record has {total} entries: they are bound to another cycle or '
            f'another node, or they are no longer in force. When the position cannot be pinned down '
            f'these entries are still read out -- reading as empty means they really cannot reach '
            f'this step.)']


#: How a scope is spelled for the reader. The vocabulary is English in the account and in the flag
#: (`--scope global|cycle|node`); the sentences a session reads spell the scope out in words, and
#: none of them carries the word `cycle`, which is also how the cycle ids in this account start.
SCOPE_WORDS = {'global': 'global (counts anywhere)',
               'cycle': 'for this run (it stops counting once this run ends)',
               'node': 'this node only (it stops counting once this node is done)'}


def _render_constraints(items: list) -> None:
    """The reader-facing rendering: what a session reads off `instruction list`.

    Text, where it is visible from, and how it is delivered. The bookkeeping (ids, and which cycle
    and node the entry was bound to) stays in the account and in `--json`: a reader has no use for
    it, and showing it invites treating a past reading as a reason the instruction might not apply
    now.
    """
    print(f'This project has {len(items)} instruction(s) visible from this step:')
    print()
    for i, it in enumerate(items, 1):
        print(f'{i}. {it["text"]}')
        if it.get('scope'):
            print(f'   visibility: {SCOPE_WORDS.get(it["scope"], it["scope"])}')
        deliver = {'stage': 'every node', 'boundary': 'read only at the close'}.get(it.get('target'))
        if deliver:
            print(f'   reach: {deliver}')
        print()


def _new_entry_binding(scope: str, cycle: str | None, node: str | None,
                       state: dict) -> tuple:
    """`(cycle, node)` for one new entry -- what its scope will be measured against.

    Two sources, in this order: what the caller named (`--cycle` / `--node`), then the pipeline's own
    position (which exists only while exactly one cycle is active). `scope` decides what "neither"
    means. A `cycle` / `node` entry with no binding has nothing to measure against, so it is read as
    visible everywhere and never goes out of sight -- those two scopes are refused here rather than
    written that way. `global` needs no binding; it still records where it was said, for the
    account's own sake.

    The two names are checked against the state before they are written: a mistyped `--cycle` would
    otherwise make an entry nobody can ever read, which is the same silence in the other direction.
    """
    active = [c for c in state['cycles'] if c.get('status') == 'active']
    known = [c['id'] for c in state['cycles'] if c.get('id')]
    if cycle is not None and cycle not in known:
        print(f'Error: --cycle {cycle!r} is not in state.json; the account has {known or "no cycle"}',
              file=sys.stderr)
        sys.exit(1)
    cycle_id = cycle if cycle is not None else (active[0]['id'] if len(active) == 1 else None)

    if node is not None:
        nodes = _read_dag()
        if nodes and node not in nodes:
            print(f'Error: --node {node!r} is not in the DAG', file=sys.stderr)
            sys.exit(1)
        node_id = node
    else:
        node_id = None

    if cycle_id:
        cycle = next((c for c in state['cycles'] if c.get('id') == cycle_id), None) or {}
        if node_id is None:
            holding = _holding_ids(cycle)
            node_id = holding[0] if holding else None

    if scope == 'cycle' and not cycle_id:
        print(f'Error: a cycle-scoped entry needs a line to bind to. '
              f'{len(active)} active cycle(s) {[c["id"] for c in active]}, so name it: `--cycle '
              f'<id>`. Without a binding the entry would be read as visible to everyone, forever, '
              f'so nothing was written.', file=sys.stderr)
        sys.exit(1)
    if scope == 'node' and not node_id:
        print('Error: a node-scoped entry needs a node to bind to, and the state does not name one '
              '(no active cycle, or the cycle it names is holding no node). Name it: `--node <id>`. '
              'Without a binding the entry would be read as visible to everyone, so nothing was '
              'written.', file=sys.stderr)
        sys.exit(1)
    return cycle_id, node_id


def cmd_instruction_add(text: str, scope: str, target: str, said_by: str,
                     note: str | None, stop_after_nodes: int | None,
                     cycle: str | None = None, node: str | None = None) -> None:
    """Record one instruction, VERBATIM.

    The caller supplies the sentence and the scope, and nothing else: the cycle and node it was said
    in are read from state, so a busy session does not have to know where it is.
    Defaults are the safe direction -- `global` + `stage` -- because missing a real constraint is
    silent while an over-broad one is merely noisy, and a rhythm instruction that fires again only
    parks the run once more, where a human can correct it.

    `--scope` is what the entry is scoped to. `--cycle` / `--node` are the caller
    naming that binding itself, which the state cannot do on its own while two cycles are active side
    by side.
    """
    if not text or not text.strip():
        print('Error: --text must not be empty', file=sys.stderr)
        sys.exit(1)
    scope = _scope_from_flag(scope)
    if scope is None:
        print(f'Error: --scope must be one of {list(valid_scopes())}', file=sys.stderr)
        sys.exit(1)
    if target not in VALID_TARGETS:
        print(f'Error: --target must be one of {list(VALID_TARGETS)}', file=sys.stderr)
        sys.exit(1)

    # Read-append-write-the-whole-file must sit inside the lock: with two sessions adding at
    # once, the later writer would overwrite the whole file and the entry written first would
    # vanish without a word (each also getting an id of the same number). The id is taken as "the
    # highest number ever handed out + 1", which is unambiguous only inside the lock.
    with _ledger_lock():
        state = _read_state()
        cycle_id, node_id = _new_entry_binding(scope, cycle, node, state)

        data = _read_instructions()
        items = data['instructions']
        entry = {
            'id': _next_instruction_id(data),
            'text': text,
            'scope': scope,
            'target': target,
            'state': LIVE_STATE,
            'cycle': cycle_id,
            'node': node_id,
            'said_by': said_by,
            'at': _now(),
            'resolved_at': None,
            'note': note,
        }
        if stop_after_nodes is not None:
            entry['stop_after_nodes'] = stop_after_nodes
        items.append(entry)
        _write_instructions(data)
    if scope == 'global':
        # A global entry has no binding: the `@ line / node` tail would only be misread as a
        # scoped entry.
        print(f'INSTRUCTION {entry["id"]}: [{scope}/{target}] {text}')
    else:
        where = (f'{cycle_id} / {node_id[:8]}' if (cycle_id and node_id)
                 else cycle_id or node_id or '(no binding)')
        print(f'INSTRUCTION {entry["id"]}: [{scope}/{target}] {text}   @ {where}')


def cmd_instruction_list(as_json: bool, live_only: bool, project: str | None = None,
                      scope: str | None = None, all_entries: bool = False,
                      node: str | None = None) -> None:
    """The instruction channel.

    Nothing is injected into a dispatch prompt, so this command is how a session learns
    what the user asked for: it runs it among its first commands and reads the answer. The default
    view is therefore computed from WHERE the pipeline stands -- the line this session works on and
    the node in its hand -- and not from the whole account: a view that is narrower than the truth is
    a constraint that does not exist for the session reading it.

    `--node <id>` is how a session says where it stands when the pointer cannot: that node stands as
    named, and with two active cycles side by side it is also what pins the line down -- a `cycle`
    entry goes to the line whose round holds that node and to nobody else.

    `--json` is the machine view of the same answer (a parseable array, `[]` when there is nothing).
    `--all`, `--live` and `--scope` are the account views: a caller asking for the raw rows gets the
    raw rows, with ids, marks and every entry of the account.
    """
    all_items = _read_instructions(project)['instructions']
    rules = _scope_rules()
    global_total = len([i for i in all_items if rules.entry_scope(i) == 'global'])

    if not all_entries and scope is None and not live_only:
        # The default view: the position comes from `_position`, and whether an entry is read is
        # the pipeline's one judgement (`instruction_in_scope`) -- closing out a node stands in the
        # same position and uses the same judgement.
        cur_cycle, cur_node = _position(project, node=node)
        # The two things the scope judgement needs (the graph's node table, the lines in service
        # right now) are read on the judgement's side: this side does not piece together a second
        # copy.
        nodes, active = rules._scope_facts(ledger_dir_for(project).parent)
        visible = [_fold(i) for i in all_items
                   if _is_live(i)
                   and rules.instruction_in_scope(i, nodes, active,
                                                  cycle=cur_cycle, node=cur_node)]
        if as_json:
            print(json.dumps(visible, ensure_ascii=False, indent=2))
            return
        if not visible:
            # The helpful sentences are for a human or a model reading stdout; `--json` stays a
            # parseable array on purpose, so a caller can branch on `[]` without string matching.
            lines = (_instruct_empty_lines(len(all_items), None, global_total) if not all_items
                     else _nothing_visible_lines(len(all_items)))
            for line in lines:
                print(line)
            return
        _render_constraints(visible)
        return

    items = all_items
    if not all_entries:
        # `--all` is the archive view; every other path leaves retired entries out of the way.
        items = [i for i in items if not _retired(i)]
    if scope is not None:
        items = [i for i in items if rules.entry_scope(i) == scope]
    if live_only:
        items = [i for i in items if _is_live(i)]

    if as_json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    if not items:
        for line in _instruct_empty_lines(len(all_items), scope, global_total):
            print(line)
        return
    marks = {LIVE_STATE: '●', 'withdrawn': '✗'}
    for i in items:
        # Any word other than `live` -- including one an older tool wrote -- reads as archived.
        mark = marks.get(i.get('state', LIVE_STATE), '✗')
        scoped = rules.entry_scope(i)
        deliver = {'stage': 'visible while working', 'boundary': 'read only when closing out'}.get(
            i.get('target'), '?')
        print(f'{mark} {i["id"]}  [{scoped}/{deliver}]  {i.get("text","")}')
        if i.get('note'):
            print(f'      note: {i["note"]}')


def cmd_instruction_resolve(ins_id: str, state: str, note: str | None,
                         project: str | None = None) -> None:
    """Archive one instruction: the owner putting an entry out of force without losing its sentence.

    Visibility is not this command's business, and neither is the ending of a `cycle` / `node`
    entry's life: `scope` decides where an entry can be seen from, and that is derived on every read
    from the bound node/cycle and the DAG as it stands. A scoped entry therefore needs no resolve at
    all -- it goes out of sight on its own. The account has two states, and the only move here is
    archiving a live entry; a record an older tool left with another word in `state` is read as
    archived already (`_retired`) and nothing writes those words back.

    The archive is the state and the moment, and nothing else: only the owner archives an entry, so
    there is no second hand for a field to name (`cmd_instruction_revive` clears the same two).
    """
    allowed = ('withdrawn',)
    if state not in allowed:
        print(f'Error: resolve --state must be one of {list(allowed)} (not {state!r}): withdrawing '
              f'is the only move left, and only the owner makes it. A scoped entry goes out of '
              f'sight on its own when its node is completed or its cycle ends.', file=sys.stderr)
        sys.exit(1)
    with _ledger_lock(project):
        data = _read_instructions(project)
        hit = None
        for item in data['instructions']:
            if item.get('id') == ins_id:
                hit = item
                break
        if hit is None:
            print(f'Error: no instruction {ins_id!r}', file=sys.stderr)
            sys.exit(1)

        hit['state'] = state
        hit['resolved_at'] = _now()
        if note:
            hit['note'] = note
        _write_instructions(data, project)
    print(f'INSTRUCTION {ins_id} → {state}')


def cmd_instruction_rm(ins_id: str, project: str | None = None) -> None:
    """Take one instruction off the account, text and all: the owner's other hand.

    `resolve` archives -- the sentence stays on the account and stops being read. This one takes the
    entry away, because a sentence that is wrong, or that should never have been recorded, is not
    something an archive makes right: it would still be read out by `--all` and by anyone looking at
    the record. What is left is the id and the moment (`REMOVED_KEY`), which is what
    `tools/conduct/ledger-merge.py` holds the removal on -- a pull from a machine that still has the
    entry does not bring it back -- and what keeps the id from being minted again.

    The sentence is printed first: the one reading that proves the caller and the tool meant the same
    entry, since after the write there is nothing on the account to compare against.
    """
    with _ledger_lock(project):
        data = _read_instructions(project)
        hit, trace = _locate(data, ins_id)
        if hit is None:
            _unknown_id_error(ins_id, trace, 'its text is already off the account')
        print(f'INSTRUCTION {hit["id"]}: [{_scope_rules().entry_scope(hit)}/{hit.get("target")}] '
              f'{hit.get("text", "")}')
        data['instructions'].remove(hit)
        data.setdefault(REMOVED_KEY, []).append({'id': ins_id, 'at': _now()})
        _write_instructions(data, project)
    print(f'REMOVED {ins_id}')


def cmd_instruction_revive(ins_id: str, project: str | None = None) -> None:
    """Put an archived entry back in force: the owner undoing their own archive.

    Only the state moves: `live`, and the moment the archive wrote is cleared back to the shape an
    added entry has, because an entry in force has no moment for a withdrawal. Those two fields are
    one act of one code path, so the merge driver resolves them as a unit (see the NOTES in
    `tools/conduct/ledger-merge.py`): a revive on one machine is not torn apart by a pull on another.

    Any other field on the record is ordinary data, and reviving does not rewrite it: this command
    writes the state and its one stamp, and nothing else.

    An entry that is already live is refused, and so is an id the account does not have. Neither is
    putting something back, and a quiet success would be a lie about what is in force.
    """
    with _ledger_lock(project):
        data = _read_instructions(project)
        hit, trace = _locate(data, ins_id)
        if hit is None:
            _unknown_id_error(ins_id, trace, 'its text went with it, and putting an entry back is '
                                             'not how a removal is undone')
        if _is_live(hit):
            print(f'Error: instruction {ins_id!r} is already live: there is nothing to put back',
                  file=sys.stderr)
            sys.exit(1)
        hit['state'] = 'live'
        hit['resolved_at'] = None
        _write_instructions(data, project)
    print(f'INSTRUCTION {ins_id} → live')


