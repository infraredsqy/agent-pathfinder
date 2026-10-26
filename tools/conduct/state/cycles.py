"""The `cycle` group: one line of work, and every verb that acts on it.

Owns all eleven verbs of `pf cycle` -- create / start / rename / status / policy / list / current /
show / hold / close / retry -- plus the vocabulary a cycle carries: VALID_TAGS, `_parse_cycle_tags`
and the policy table (`POLICY_*`, and `cycle_policy` as the one reading of it).

A line's policies are its own thing: three of them (`when_shelved` / `parallel` / `spawner`) belong
to the line and live nowhere else, and the rest are the knobs the installed features declare, which
this line may state on its own record. Nothing in this file spells a feature's name -- the thresholds
a line configures arrive from the declarations of whatever is installed.
"""

import json
import sys
from pathlib import Path

from . import knobs
from .store import (CYCLE_VALID_STATUSES, VALID_NODE_RESULTS,
                    _find_cycle, _holding_ids, _holding_of, _ledger_lock, _loader, _now,
                    _read_dag, _read_signal, _read_state, _resolve_project_root, _set_holding,
                    _signal_path, _write_signal, _write_state, dag_manage, ledger_dir_for)
from .pipeline_loader import _load_pipeline


# ── cycle tags: which nodes this line picks from ────────────────────
# `cycle.tags` is this line's **range to pick from**: every pick reads this one. A new cycle must
# always give it; an empty range is read only by records already on disk and by the filter helper's
# own empty-set branch. The vocabulary is the same three words as dag-manage.py's (that one governs
# nodes, this one the cycle's range); one convention sitting in two tools is held together by a
# test, so the two sides do not grow their own words.
VALID_TAGS = ('writing', 'coding', 'art')


def _require_tags(tags: list, verb: str) -> None:
    """A new line must state the range it picks from; an empty range is refused, with `--tags` in
    the hint.

    Which branch a line **belongs to** (whether a feature that reacts to a crossed threshold may
    insert a node for a line like this one) is expressed by this tag too: a cycle with no tags
    would give "who may react to a crossed threshold" a second answer. Cycles already on disk keep
    running; this gate applies to new calls only.
    """
    if tags:
        return
    print(f'Error: {verb} must be given --tags (the range this line picks from). '
          f'For example: {verb} <slug> --tags coding. '
          f'Known words: {"/".join(VALID_TAGS)} (a word outside the vocabulary is fine too; it is '
          f'taken as given). '
          f'Every pick this line makes, and which features may react to it, reads this one range.',
          file=sys.stderr)
    sys.exit(2)


# ── cycle policies: the knobs a line states itself ───────────────────
# `cycle.policies` is this line's own layer of the knob chain. Three of its keys are the line's and
# nobody else's -- they are NOT settings and they are not on `state/knobs.py`'s table, because the
# readers of every one of them truncate the chain to this record: the pick reads `parallel`, the stop
# answer reads `when_shelved`, `resolve_spawner` reads `spawner`. A value under one of these names in
# a project or a global file would be a setting nothing reads, which is exactly the "I set it, why is
# it not in force" this tree refuses to ship -- so `pf config` does not know these three names and
# `pf cycle policy` is the door that writes them.
#
# `when_shelved`: a node closed out without delivering (so it was shelved, waiting for a human to
# rescue it) -- does this line stop and wait, or go on to pick the next node. Default `stop`: the
# default must fall on the conservative side.
# `parallel`: how many nodes this line holds, and how many sessions it opens, at once. Default 1 (one
# slot at a time). It is a **cap**: whether nodes can run side by side is the dependency graph's call.
# `spawner`: which spawner opens this line's sessions (`dsh` / `zcode`). Words only, never a path:
# the cycle record merges across machines with `.pf/`, so a machine-local path landing in the state
# file is a dead value on another machine; where a path is wanted, use the one-off `--spawner <path>`.
LINE_POLICIES = {
    'when_shelved': {
        'kind': 'word',
        'values': ('stop', 'continue'),
        'default': 'stop',
        'summary': 'what this line does when a node comes back not delivered: stop and wait for a '
                   'human, or mark it shelved and carry on',
    },
    'parallel': {
        'kind': 'integer',
        'default': 1,
        'summary': 'how many nodes this line holds and how many sessions it opens at once (a cap: '
                   'whether they can run side by side is the graph\'s call)',
    },
    'spawner': {
        'kind': 'word',
        'values': ('dsh', 'zcode'),
        'default': None,
        'summary': 'which spawner opens this line\'s sessions (words only, never a path: the cycle '
                   'record merges across machines)',
    },
}

#: The table `cycle policy` judges with: the line's own three, then every knob a declaration brings
#: that takes the cycle layer. A feature's thresholds and its switch do walk the whole chain, so this
#: line may state them (one chain, one place to state a value); the core's own settings do not take
#: this layer, so `max_agents` is not a policy and `pf cycle policy` refuses it. Derived from
#: `knobs.specs()` -- no feature's name is spelled in this file.
_SPECS = {**LINE_POLICIES,
          **{key: cell for key, cell in knobs.specs().items()
             if 'cycle' in knobs.layers_of(cell)}}
#: The same table, for a caller that builds `--help`: one source for what a policy's value looks like.
POLICY_SPECS = _SPECS
#: The two knobs every line carries a value for, in the order a reading prints them. Their defaults
#: are the line table's (writing a value for either again here would be a second source for it).
POLICY_DEFAULTS = {key: LINE_POLICIES[key]['default'] for key in ('when_shelved', 'parallel')}
POLICY_VALUES = {key: tuple(LINE_POLICIES[key].get('values') or ())
                 for key in POLICY_DEFAULTS if LINE_POLICIES[key]['kind'] == 'word'}
#: Integer policies: an integer value, with `off` to cancel. `parallel` lives here (floor 1: a line
#: holds at least one slot).
POLICY_COUNTS = tuple(key for key in POLICY_DEFAULTS if LINE_POLICIES[key]['kind'] == 'integer')
#: Numeric policies: no candidate table, the value parses as a number; `off` cancels (absent means
#: "not configured"). The thresholds of the features that declare a metric land here -- they are
#: **this line's own rules**, the same kind of thing as `tags`, so they ride on the cycle record.
POLICY_NUMBERS = tuple(sorted(key for key, spec in _SPECS.items() if spec['kind'] == 'number'))
#: Word policies: the value must be in the candidate table, `off` clears it, and there is no
#: default -- a line that never configured it falls to the next level of the chain, and nothing
#: changes. `spawner` lives here, and so does a feature's word knob that names no default.
POLICY_WORDS = tuple(sorted(key for key, spec in _SPECS.items()
                            if spec['kind'] == 'word' and spec.get('default') is None
                            and key not in POLICY_DEFAULTS))
POLICY_WORD_CHOICES = {key: tuple(_SPECS[key].get('values') or ()) for key in POLICY_WORDS
                       if _SPECS[key].get('values')}
#: The union of legal policy keys: the four tables are the whole source, computed once here --
#: `cycle policy`'s refusal and argparse's `choices` (through `state-manage.py`) both read this.
POLICY_KEYS = tuple(sorted(_SPECS))


def cycle_policy(cycle: dict) -> dict:
    """This line's effective policies right now: the two always-present knobs filled in with their
    defaults, plus every value the line states on its own record, parsed by its kind.

    A value the kind refuses (a hand-edited record) is read as not stated rather than raising: the
    reading side of a state record is tolerant, the writing side is what refuses. `parallel` falls
    back to its default; a knob with no factory value stays absent until some layer states it.
    """
    stated = cycle.get('policies') or {}
    out = dict(POLICY_DEFAULTS)
    for key, value in stated.items():
        spec = _SPECS.get(key)
        if spec is None:
            continue
        parsed = knobs.parse(spec['kind'], value, spec.get('values'))
        if parsed is not None:
            out[key] = parsed
    return out


def _parse_policy_value(key: str, text: str):
    """One policy value from the command line, parsed by its kind; `None` means "drop the entry".

    `off` is a **clearing word** for `number` / `integer` / `word`: it drops this line's entry, which
    is the same move `pf config unset --cycle` makes. On a `switch` it is a **value** -- a switch is
    turned off by writing `off`, and dropping its entry is `config unset`'s move. The two meanings
    never meet, because the kind decides which one applies.

    Numbers and integers take a positive value only: both are "the last acceptable reading" shaped,
    and a line of 0 would judge every reading as over it.
    """
    spec = _SPECS.get(key)
    if spec is None:
        print(f'Error: unknown policy {key!r}; valid keys: {list(POLICY_KEYS)}', file=sys.stderr)
        sys.exit(1)
    kind = spec['kind']
    if kind == 'switch':
        if text not in ('on', 'off'):
            print(f'Error: policy {key} must be on or off, got {text!r}', file=sys.stderr)
            sys.exit(1)
        return text
    if str(text).strip().lower() == 'off':
        return None
    if kind == 'number':
        try:
            parsed = float(text)
        except (TypeError, ValueError):
            print(f'Error: policy {key} takes a positive number or `off`, got {text!r}',
                  file=sys.stderr)
            sys.exit(1)
        if not parsed > 0 or parsed == float('inf'):
            print(f'Error: policy {key} must be positive (a line of 0 fails every reading)',
                  file=sys.stderr)
            sys.exit(1)
        return int(parsed) if parsed.is_integer() else parsed
    if kind == 'integer':
        try:
            parsed = int(str(text).strip())
        except (TypeError, ValueError):
            print(f'Error: policy {key} takes a positive integer or `off`, got {text!r}',
                  file=sys.stderr)
            sys.exit(1)
        if parsed < 1:
            print(f'Error: policy {key} must be at least 1 (a line holds at least one node)',
                  file=sys.stderr)
            sys.exit(1)
        return parsed
    allowed = spec.get('values')
    if allowed is not None and text not in allowed:
        print(f'Error: policy {key} must be one of {list(allowed)} or `off`, got {text!r}',
              file=sys.stderr)
        sys.exit(1)
    return text


def write_cycle_policy(cycle_id: str, key: str, value) -> dict:
    """Write one slot of this line's own layer (`value is None` drops it), and hand back the whole
    effective policy set.

    **The one writer of this part of the cycle record**: `cycle policy` and `pf config set --cycle`
    both come through here, so the record has one writer and one place where its `updated_at` moves.
    """
    with _ledger_lock():
        state = _read_state()
        cycle = _find_cycle(state, cycle_id)
        policies = cycle.setdefault('policies', {})
        if value is None:
            policies.pop(key, None)
        else:
            policies[key] = value
        cycle['updated_at'] = _now()
        _write_state(state)
        return cycle_policy(cycle)


def _parse_cycle_tags(raw: str | list | None) -> list:
    """`--tags coding,writing` → `['coding', 'writing']`: strip blanks, dedupe, keep the order.

    A tag outside the vocabulary is taken with a one-line warning: the vocabulary is a convention
    for people to read, and the engine takes anything -- if a fourth kind of work really shows up,
    writing it in is enough to use it. The synonym layer is gone: nothing is normalized here, and
    filtering always compares literally.

    Takes a string and takes a list: `cycle start` splits the command line first and then creates
    the cycle, while `cycle create` takes the string a human typed. Both shapes come through this
    one door, so `art` cannot be split into `['a','r','t']` on one of the two paths.
    """
    if isinstance(raw, (list, tuple)):
        raw = ','.join(str(x) for x in raw)
    out = []
    for item in (raw or '').split(','):
        tag = item.strip()
        if tag and tag not in out:
            out.append(tag)
    unknown = [t for t in out if t not in VALID_TAGS]
    if unknown:
        print(f'Warning: tag {", ".join(unknown)} is not in the vocabulary {"/".join(VALID_TAGS)}; '
              f'taken as given, but it can only match a node that wrote the same word.',
              file=sys.stderr)
    return out


def cmd_cycle_create(cycle_id: str, title: str | None,
                     force: bool = False, tags=None) -> None:
    """Create a cycle -- and refuse while another one is still active.

    Two active cycles is not a harmless extra: the bare `next` that everything else relies on can no
    longer tell which cycle it is about, so it degrades into `ask_user` ("continue which one?"),
    and an unattended run has nobody to answer. One cycle at a time is the invariant; `--force` is
    the human saying they know that and want the second one anyway.

    `tags` is this cycle's filter set: which nodes it picks from. It is written down here (and
    nowhere else) because every later pick reads it back -- the set is a property of the LINE, not of
    the step that happened to open it. Empty means the whole graph is in range.

    The cycle starts with nothing in hand: `cycle hold` is what claims the first node.
    """
    # The command-line string goes through the one parsing door first: `cycle start` splits it
    # before calling in here, while a human typing `cycle create --tags art` arrives with the string
    # untouched. Both paths must land on the same list, or `art` is read as three letters on one of
    # them.
    tags = _parse_cycle_tags(tags)
    _require_tags(tags, 'pf cycle create')
    with _ledger_lock():
        state = _read_state()
        for c in state['cycles']:
            if c['id'] == cycle_id:
                print(f'Error: cycle "{cycle_id}" already exists', file=sys.stderr)
                sys.exit(1)

        active = [c['id'] for c in state['cycles'] if c.get('status') == 'active']
        if active and not force:
            print(f'Error: cycle(s) {active} still active -- one cycle at a time. Take them out of '
                  f'service (`pf cycle status <id> inactive`), or pass --force to run two anyway.',
                  file=sys.stderr)
            sys.exit(1)
        if active and force:
            print(f'Note: --force given; {active} stay active, so a bare `next` will have to be told '
                  f'which cycle (--cycle) from now on.', file=sys.stderr)

        now = _now()
        if not title:
            title = f'Cycle {now[:10]}'
        entry = {
            'id': cycle_id, 'title': title,
            'status': 'active',
            # Nothing is in hand until `cycle hold` claims a node; `path` is the steps walked.
            'holding': [], 'path': [],
            'created_at': now, 'updated_at': now,
        }
        if tags:
            entry['tags'] = tags
        state['cycles'].append(entry)
        _write_state(state)

    print(f'CREATED: cycle "{cycle_id}"' + (f' (tags={list(tags)})' if tags else ''))


def cmd_cycle_policy(cycle_id: str, key: str, value: str) -> None:
    """Write down one policy for this line, and report the whole effective set back.

    `policies` is a property of **this line** (the same kind of thing as `tags`), so it rides on
    the cycle record rather than on one call: every step this line takes, and every close, reads
    the same one.

    Each kind states what its values are: a number takes a positive number, an integer a positive
    integer, a word a word from its candidate table, a switch `on` or `off`. `off` drops the entry
    for a number / integer / word ("not configured": the next layer of the chain shows through); on
    a switch it is a value -- a switch is turned off by writing `off`, and dropping its entry is
    `pf config unset --cycle`.
    """
    stored = _parse_policy_value(key, value)
    effective = write_cycle_policy(cycle_id, key, stored)
    print(f'POLICY: "{cycle_id}" → ' + ', '.join(f'{k}={v}' for k, v in effective.items()))


def cmd_cycle_start(cycle_id: str, title: str | None,
                    force: bool = False, tags=None, parallel=None) -> None:
    """Create a cycle AND pin it on the node the rule picks. One command.

    The launcher should not have to know how the DAG is ranked, so the pick happens here, using the
    same rule a close uses. If the graph has nothing ready, this refuses: there is no work to run,
    and inventing some is pf-route's job, done by a human, not the pipeline's.

    `--tags` is the filter set this cycle will pick from, and it has to reach the PICK here: the
    pick happens while the cycle is being created, so a set that only landed in the cycle record
    would be too late for the first node.

    `--parallel` is how many slots this line holds at once: it lands in exactly the
    `policies.parallel` slot (`cycle policy` writes the same key), so how many slots this opening
    pick takes, and how many each later close refills, are one and the same number. Not given means
    the default (one slot at a time).

    Refuses while another cycle is still active, unless `--force`. The alternative -- two active
    cycles -- makes the next bare `next` an `ask_user`, which an unattended run cannot answer, and
    the cycle it opens would very likely pick the node the old one was working on.

    The whole body runs under the ledger lock: the pick must not read the DAG while another
    `cycle start` is mid-claim, or both lines pin the same node (the pick's "ready" excludes the
    nodes a cycle holds only if the other line's claim is already on disk).
    """
    pipeline = _load_pipeline()

    tags = _parse_cycle_tags(tags)
    _require_tags(tags, 'pf cycle start')
    # The command-line slot goes through the same parsing door `cycle policy` uses: a positive
    # integer lands on disk, `off` falls back to the default.
    stated = _parse_policy_value('parallel', parallel) if parallel is not None else None
    with _ledger_lock():
        project = _resolve_project_root(Path.cwd())
        # How many slots the opening pins and how many each close refills are one thing:
        # `parallel` says how many nodes this line holds at once. This pick's `limit` and the opening
        # pins both read it -- `cycle_policy` is the one source for it, asked once when the command
        # line gives it, and the default this new line lands with when it does not.
        stated_or_default = {'parallel': stated} if stated is not None else {}
        cap = cycle_policy({'policies': stated_or_default})['parallel']
        ranked = pipeline.ranked_ready_nodes(project, limit=cap, tags=tags)
        if not ranked:
            blocked = pipeline.tag_blocked_ready_nodes(project, tags)
            if blocked:
                # There is work in the graph; it is just not this line's to run. The
                # sentence must show a human at a glance what differs: one more --tags and it is.
                print('Error: --tags ' + ','.join(tags) + ': this filter set has no ready node; '
                      'these ready nodes in the graph are kept out by tag:\n'
                      + '\n'.join(pipeline.tag_blocked_lines(blocked))
                      + '\nWiden --tags (a node with no tag is in every range).', file=sys.stderr)
                sys.exit(2)
            print('Error: the graph has no ready node -- there is no work to run. Have pf-route '
                  'build the graph first, then start the line.', file=sys.stderr)
            sys.exit(2)
        node = ranked[0]
        cmd_cycle_create(cycle_id, title, force=force, tags=tags)
        state = _read_state()
        cycle = _find_cycle(state, cycle_id)
        if stated is not None:
            cycle.setdefault('policies', {})['parallel'] = stated
        holding = [{'id': n['id'], 'since': cycle['created_at']} for n in ranked[:cap]]
        _set_holding(cycle, holding)
        _write_state(state)
    # The list was taken by this line's `parallel`, so "how many more are queued behind" cannot be
    # read this time -- it is not reported, and not guessed.
    print(f'STARTED: cycle "{cycle_id}" → {node.get("title", node["id"])}'
          + (f'  ({len(holding)} node(s) pinned)' if len(holding) > 1 else ''))


# ── the group's queries ──────────────────────────────────────────────────────

def cmd_cycle_list() -> None:
    state = _read_state()
    if not state['cycles']:
        print('No cycles found.')
        return
    print(f'{"ID":<22} {"Title":<34} {"Status":<12} {"Steps":<6} {"Node":<10}')
    print('-' * 92)
    for c in state['cycles']:
        holding = _holding_ids(c)
        node = holding[0][:8] if holding else '-'
        if len(holding) > 1:
            node += f'+{len(holding) - 1}'
        print(f'{c["id"]:<22} {c.get("title","?"):<34} '
              f'{c.get("status","?"):<12} {len(c.get("path") or []):<6} {node:<10}')


def cmd_cycle_current() -> None:
    """The lines that matter right now: every `active` one, **and every one with a node in hand**.

    The second is why this exists: a line out of service still holds the node it never closed out
    -- and that is exactly the state a human must see (the work stopped half way, and this line
    will not move on by itself; a reading that lists only the active ones does not have it).

    A line can hold several nodes at once (the `parallel` policy): this reading lists every one of
    them, each with its own claim moment on its own line.
    """
    state = _read_state()
    lines = [c for c in state['cycles']
             if c.get('status') == 'active' or _holding_ids(c)]
    if not lines:
        print('No active cycles.')
        return
    for c in lines:
        # ● in service, ○ not -- one glance says whether this line moves on by itself.
        mark = '●' if c.get('status') == 'active' else '○'
        policy = ', '.join(f'{k}={v}' for k, v in cycle_policy(c).items())
        print(f'{mark} {c["id"]} — {c.get("title","")}  [{c.get("status", "?")}]')
        print(f'  Policy: {policy}  Steps walked: {len(c.get("path") or [])}')
        holding = _holding_of(c)
        if holding:
            for item in holding:
                print(f'  Node: {item["id"]}  (since {item.get("since") or "?"})')
        else:
            print('  Node: (none in hand)')


def cmd_cycle_show(cycle_id: str, node_id: str) -> None:
    """One node's standing in this cycle: the signal on disk and the cycle's own hand."""
    state = _read_state()
    cycle = _find_cycle(state, cycle_id)
    item = next((h for h in _holding_of(cycle) if h['id'] == node_id), None)
    if item:
        print(f'cycle {cycle_id}: holding {node_id} since {item.get("since") or "?"}')
    else:
        step = next((s for s in reversed(cycle.get('path') or [])
                     if s.get('node') == node_id), None)
        if step:
            print(f'cycle {cycle_id}: walked {node_id} → {step.get("result")} '
                  f'({step.get("started_at")} .. {step.get("ended_at")})')
        else:
            print(f'cycle {cycle_id}: nothing about {node_id} (not in hand, not in the path)')
    sig = _read_signal(cycle_id, node_id)
    if sig:
        print(f'  signal: {sig.get("status")}  {sig.get("summary","")}')
        if sig.get('outputs'):
            print(f'  outputs: {json.dumps(sig["outputs"], ensure_ascii=False)}')
    else:
        print('  signal: (none)')


# ── the group's writes ───────────────────────────────────────────────────────

def cmd_cycle_hold(cycle_id: str, node_id: str) -> None:
    """Pin the cycle on a node. This is how work is claimed: another cycle's pick skips it.

    The claim is exclusive ACROSS cycles, and the account is where that is enforced: two lines can
    reach for the same node in the overlap window between their closes and opens, and without this
    check both pins would land -- two sessions would then boot onto one node, and no close would
    ever be able to settle it (`cycle close` reads ONE holder). The lock makes the scan-then-write
    one atomic step.

    A line can hold several nodes at once (the `parallel` policy), so pinning one more onto a line
    that already has nodes is not an error -- that write is "open one more slot"; re-pinning the
    same id is allowed (restart semantics).
    """
    with _ledger_lock():
        state = _read_state()
        cycle = _find_cycle(state, cycle_id)
        holder = next((c for c in state['cycles']
                       if c['id'] != cycle_id and node_id in _holding_ids(c)), None)
        if holder is not None:
            print(f'Error: node "{node_id}" is already pinned by cycle "{holder["id"]}" '
                  f'(status {holder.get("status")}).'
                  f' -- one node belongs to one cycle at a time, whatever that line\'s status. '
                  f'Close it there first, or pick another node.', file=sys.stderr)
            sys.exit(1)
        now = _now()
        holding = _holding_of(cycle)
        if not any(item['id'] == node_id for item in holding):
            holding.append({'id': node_id, 'since': now})
        _set_holding(cycle, holding)
        cycle['updated_at'] = now
        _write_state(state)
    print(f'CURRENT_NODE: {cycle_id} → {node_id}')


def cmd_cycle_retry(cycle_id: str, node_id: str, summary: str | None,
                    drop: bool = False) -> None:
    """Take a node's ticket out of the way so its run can be retried.

    Retrying a node whose run came back not delivered needs this: without it the old terminal signal
    stays on disk, so the next reader calls it finished again -- an infinite retry that never retries.

    By default the note is rewritten as `pending` (the mark a retry leaves behind, and readers wait
    on it as on a red light). `--drop` leaves no note at all: that is the step a retry really starts
    from -- as long as one `pending` note is there, `next` always answers `wait` and not a byte
    moves (this has stalled a line).
    """
    if drop:
        # Loading happens outside the lock: it is not part of this read-modify-write.
        pipeline = _load_pipeline()
    with _ledger_lock():
        state = _read_state()
        _find_cycle(state, cycle_id)
        sp = _signal_path(cycle_id, node_id)
        remove = _loader().load_shared('jsonio').unlink_with_retry
        if drop:
            if sp.exists():
                remove(sp)
            # The session's own stamp goes too: leaving it means the retry can never be dispatched
            # (dispatch skips this slot; see nextcmd). That stamp lives in the same place as the
            # prompt / claim (`prompts.started_marker`), and all this hands over is "which line,
            # which slot" -- the landing rule is not spelled out a second time here.
            remove(pipeline.started_marker(ledger_dir_for(None).parent, cycle_id, node_id))
            print(f'Signal dropped (no ticket left): {sp}')
            return
        if sp.exists():
            remove(sp)
        p = _write_signal(cycle_id, node_id, 'pending',
                          summary or 'Node restarted (previous signal cleared)', {})
    print(f'Signal cleared and rewritten as pending: {p}')


def cmd_cycle_close(cycle_id: str, node_id: str, result: str) -> None:
    """Record the step and let the node go.

    The node leaves the cycle's hand here, which is what lets the next pick take it (or the close
    command open the next one). The other nodes this line holds are left alone. The DAG status is a
    separate writer's job -- the conductor runs `dag-manage status` itself -- so this only owns the
    cycle's own record.
    """
    if result not in VALID_NODE_RESULTS:
        print(f'Error: --result must be one of {sorted(VALID_NODE_RESULTS)}', file=sys.stderr)
        sys.exit(1)
    with _ledger_lock():
        state = _read_state()
        cycle = _find_cycle(state, cycle_id)
        holding = _holding_of(cycle)
        item = next((h for h in holding if h['id'] == node_id), None)
        if item is None:
            names = ', '.join(h['id'][:8] for h in holding) or 'nothing'
            print(f'Error: cycle "{cycle_id}" is not holding {node_id}'
                  + f' (it holds {names})'
                  + '; nothing was changed.', file=sys.stderr)
            sys.exit(1)

        now = _now()
        step = {
            'node': node_id,
            'started_at': item.get('since'),
            'ended_at': now,
            'result': result,
        }
        cycle.setdefault('path', []).append(step)
        _set_holding(cycle, [h for h in holding if h['id'] != node_id])
        cycle['updated_at'] = now
        _write_state(state)
    print(f'NODE_CLOSED: {cycle_id} → {node_id} {result} '
          f'(path is now {len(cycle["path"])} step(s))')


def _approve_gate_delivery(cycle: dict, project: str | None = None) -> None:
    """Record the human's "continue" at a checkpoint gate, on the node that gate stopped on.

    **Putting the line back in service IS that word.** The tool's own sentence when it stops on a
    `checkpoint` node is "the next node opens only once you say continue", and there is nothing else
    the human has to say: this command is the whole of their answer. Written down on the node, it
    lets a reader outside this tool tell "a human passed this delivery" from "the line happened to
    move one more step" -- and the path alone cannot say which of the two it was.

    Everything read here is read off the disk: the last step the line put down (the last record on
    `path` that ended) and that node's own mode. Anything else -- a line whose last step down was an
    `auto` or `manual` node, a node since taken off the graph, a node that never delivered -- gets
    not one byte written: the graph's own door judges whether there is a delivery to approve at all
    (`approve_node` answers "nothing was written"). The approval belongs to one delivery, so an old
    one lapses by itself when the node is delivered again: **re-approving is the case that must not
    be written at all**, the node's `updated_at` included.

    Best effort from the write on: this rides along a command that has already done what the human
    asked for, so a graph write that will not go warns one line and leaves that command standing.
    """
    ended = [step for step in (cycle.get('path') or [])
             if step.get('ended_at') and step.get('node')]
    if not ended:
        return
    node_id = ended[-1]['node']
    # A node that never wrote a mode is not `checkpoint` (the graph's own default is `auto`).
    node = (_read_dag(project) or {}).get(node_id) or {}
    if node.get('mode') != 'checkpoint':
        return
    try:
        dag = dag_manage()
        if dag.delivery_approved(node):
            return
        dag.approve_node(ledger_dir_for(project).parent, node_id)
    except OSError as e:
        print(f'Warning: the approval of {node_id[:8]} was not written: {e}', file=sys.stderr)


def cmd_cycle_status(cycle_id: str, status: str) -> None:
    """Put a line into service, or take it out.

    Only `active` / `inactive` land in the record: why a line stopped is written in the reason at
    the moment it stopped, and the status only answers "should it move on". This command is also
    the hand a human uses to pull a line back up -- the tool itself never writes `active`.

    **Pulling a line up is the human's own word at a checkpoint gate**, and that word is written down
    where it belongs: `_approve_gate_delivery` runs on the way in. A line that was already in service
    has no moment here to record, and nothing is written for it. It runs BEFORE the line's own write,
    so a graph this command cannot read stops with nothing changed -- the human fixes that and runs
    the command again, and the approval still lands. (The other way round, the line would be in
    service and the re-run would have no moment left to record it in.)
    """
    if status not in CYCLE_VALID_STATUSES:
        print(f'Error: cycle status must be one of {sorted(CYCLE_VALID_STATUSES)}', file=sys.stderr)
        sys.exit(1)

    with _ledger_lock():
        state = _read_state()
        cycle = _find_cycle(state, cycle_id)
        was_in_service = cycle.get('status') == 'active'
        if status == 'active' and not was_in_service:
            _approve_gate_delivery(cycle)
        cycle['status'] = status
        cycle['updated_at'] = _now()
        _write_state(state)
    print(f'CYCLE_STATUS: "{cycle_id}" → {status}')


def cmd_cycle_rename(cycle_id: str, title: str) -> None:
    with _ledger_lock():
        state = _read_state()
        cycle = _find_cycle(state, cycle_id)
        cycle['title'] = title
        cycle['updated_at'] = _now()
        _write_state(state)
    print(f'RENAMED: "{cycle_id}" → "{title}"')

