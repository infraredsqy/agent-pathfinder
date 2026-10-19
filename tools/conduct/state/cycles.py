"""The `cycle` group: one line of work, and every verb that acts on it.

Owns all eleven verbs of `pf cycle` -- create / start / rename / status / policy / list / current /
show / hold / close / retry -- plus the vocabulary a cycle carries: VALID_TAGS, `_parse_cycle_tags`
and the policy table (`POLICY_*`, and `cycle_policy` as the one reading of it).
"""

import json
import sys
from pathlib import Path

from .store import (CYCLE_VALID_STATUSES, PARALLEL_DEFAULT, VALID_NODE_RESULTS,
                    _find_cycle, _holding_ids, _holding_of, _ledger_lock, _loader, _now,
                    _read_signal, _read_state, _resolve_project_root, _set_holding, _signal_path,
                    _write_signal, _write_state, ledger_dir_for)
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

    Which branch a line **belongs to** (whether it may have a maintenance node inserted for a
    maintenance metric) is expressed by this tag too: a cycle with no tags would give "who may
    react to a maintenance metric" a second answer. Cycles already on disk keep running; this gate
    applies to new calls only.
    """
    if tags:
        return
    print(f'Error: {verb} must be given --tags (the range this line picks from). '
          f'For example: {verb} <slug> --tags coding. '
          f'Known words: {"/".join(VALID_TAGS)} (a word outside the vocabulary is fine too; it is '
          f'taken as given). '
          f'Every pick this line makes, and where its maintenance channel applies, read this one '
          f'range.',
          file=sys.stderr)
    sys.exit(2)


# ── cycle policies: rules a line carries itself ───────────────────────
# `cycle.policies` is optional: a cycle that never wrote the key lands in exactly today's shape,
# and the defaults fill it in on the way back out. The first is `when_shelved` -- a node closed out
# without delivering (so it was shelved, waiting for a human to rescue it): does this line stop and
# wait, or go on to pick the next node. Default `stop`: the default must fall on the conservative
# side.
# The second is `parallel` -- how many nodes this line holds, and how many sessions it opens, at
# once. Default 1 (one slot at a time). It is a **cap**: whether nodes can run side by side is the
# dependency graph's call.
# The same set of words as `pipeline/store.py` (that one owns the reading, this one the writing).
POLICY_DEFAULTS = {'when_shelved': 'stop', 'parallel': PARALLEL_DEFAULT}
POLICY_VALUES = {'when_shelved': ('stop', 'continue')}
#: Integer policies: an integer value, with `off` to cancel. `parallel` lives here (floor 1: a line
#: holds at least one slot).
POLICY_COUNTS = ('parallel',)
#: Numeric policies: no candidate table, the value parses as a number; `off` cancels (absent means
#: "not configured").
#: The two maintenance lines live here: they are **this line's own rules**, the same kind of thing
#: as `tags`, so they ride on the cycle record -- a line that never touches code (`art` /
#: `writing`) does not carry either key at all.
POLICY_NUMBERS = ('test_seconds_limit', 'issue_pool_weight_limit')
#: Word policies: the value must be in the candidate table, `off` clears it, and there is no
#: default -- a line that never configured it falls to the next level of the call chain, and
#: nothing changes. `spawner` lives here: which spawner opens this line's sessions
#: (`dsh` / `zcode`). Words only, never a path: the cycle record merges across machines with
#: `.pf/`, so a machine-local path landing in the state file is a dead value on another machine;
#: where a path is wanted, use the one-off `--spawner <path>`.
POLICY_WORDS = ('spawner',)
POLICY_WORD_CHOICES = {'spawner': ('dsh', 'zcode')}
#: The union of legal policy keys: the four tables are the whole source, computed once here --
#: `cycle policy`'s refusal and argparse's `choices` (through `state-manage.py`) both read this.
POLICY_KEYS = tuple(sorted(set(POLICY_VALUES) | set(POLICY_NUMBERS) | set(POLICY_COUNTS)
                           | set(POLICY_WORDS)))


def cycle_policy(cycle: dict) -> dict:
    """This line's effective policies right now: keys missing from disk (or holding a word nobody
    recognizes) are filled in with their defaults.

    Numeric policies (`POLICY_NUMBERS`) and word policies (`POLICY_WORDS`) have no default: not
    configured means they do not appear in this reading.
    Integer policies (`POLICY_COUNTS`) do have a default (`parallel` defaults to 1); a non-integer
    on disk counts as not configured.
    """
    stated = cycle.get('policies') or {}
    out = {}
    for key, default in POLICY_DEFAULTS.items():
        value = stated.get(key)
        if key in POLICY_COUNTS:
            out[key] = value if _policy_count(value) is not None else default
        else:
            out[key] = value if value in POLICY_VALUES.get(key, ()) else default
    for key in POLICY_NUMBERS:
        number = _policy_number(stated.get(key))
        if number is not None:
            out[key] = number
    for key in POLICY_WORDS:
        value = stated.get(key)
        if value in POLICY_WORD_CHOICES.get(key, ()):
            out[key] = value
    return out


def _policy_count(value):
    """An integer policy off disk, or `None`. Only a value that is already a positive integer
    counts."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _policy_number(value):
    """A numeric policy off disk, or `None`. Only a value that is already a number counts: a boolean
    and a string are both read as not configured."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _parse_policy_number(key: str, text: str):
    """A numeric policy from the command line: a positive number lands on disk, `off` cancels.

    0 and negative numbers are refused outright -- the line is "the last acceptable number", so 0
    would judge every reading as over the line.
    """
    if str(text).strip().lower() == 'off':
        return None
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        print(f'Error: policy {key} takes a positive number or `off`, got {text!r}',
              file=sys.stderr)
        sys.exit(1)
    if parsed <= 0:
        print(f'Error: policy {key} must be positive (a line of 0 fails every reading)',
              file=sys.stderr)
        sys.exit(1)
    return int(parsed) if parsed.is_integer() else parsed


def _parse_policy_count(key: str, text: str):
    """An integer policy from the command line: a positive integer lands on disk, `off` cancels
    (default 1)."""
    if str(text).strip().lower() == 'off':
        return None
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


def _parse_policy_word(key: str, text: str):
    """A word policy from the command line: a word from the candidate table lands on disk, `off`
    clears it (clearing means "not configured", falling back to the next level of the call
    chain)."""
    if str(text).strip().lower() == 'off':
        return None
    if text not in POLICY_WORD_CHOICES.get(key, ()):
        print(f'Error: policy {key} must be one of {list(POLICY_WORD_CHOICES[key])} '
              f'or `off`, got {text!r}', file=sys.stderr)
        sys.exit(1)
    return text


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

    A numeric policy (the two maintenance lines) takes a positive number and `off` drops it; an
    integer one (`parallel`) takes a positive integer; a word one (`spawner`) takes a word from the
    candidate table and `off` drops it.
    """
    if key in POLICY_NUMBERS:
        stored = _parse_policy_number(key, value)
    elif key in POLICY_COUNTS:
        stored = _parse_policy_count(key, value)
    elif key in POLICY_WORDS:
        stored = _parse_policy_word(key, value)
    elif key in POLICY_VALUES:
        allowed = POLICY_VALUES[key]
        if value not in allowed:
            print(f'Error: policy {key} must be one of {list(allowed)}', file=sys.stderr)
            sys.exit(1)
        stored = value
    else:
        print(f'Error: unknown policy {key!r}; valid keys: {list(POLICY_KEYS)}',
              file=sys.stderr)
        sys.exit(1)

    with _ledger_lock():
        state = _read_state()
        cycle = _find_cycle(state, cycle_id)
        policies = cycle.setdefault('policies', {})
        if stored is None:
            policies.pop(key, None)
        else:
            policies[key] = stored
        cycle['updated_at'] = _now()
        _write_state(state)
        effective = cycle_policy(cycle)
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
    stated = _parse_policy_count('parallel', parallel) if parallel is not None else None
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


def cmd_cycle_status(cycle_id: str, status: str) -> None:
    """Put a line into service, or take it out.

    Only `active` / `inactive` land in the record: why a line stopped is written in the reason at
    the moment it stopped, and the status only answers "should it move on". This command is also
    the hand a human uses to pull a line back up -- the tool itself never writes `active`.
    """
    if status not in CYCLE_VALID_STATUSES:
        print(f'Error: cycle status must be one of {sorted(CYCLE_VALID_STATUSES)}', file=sys.stderr)
        sys.exit(1)

    with _ledger_lock():
        state = _read_state()
        cycle = _find_cycle(state, cycle_id)
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

