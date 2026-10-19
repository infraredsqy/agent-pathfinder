"""`plan()` and the settle-and-close of one node.

The stop rule, the read-only planner that returns the one next action, and the close a worker's
own `next --after <node_id>` performs.
"""

from __future__ import annotations

from pathlib import Path

from . import maintenance, seam
from .store import (_holding_ids, active_cycles, cycle_policy, find_cycle,
                    load_dag, load_signal, load_state, normalize_mode)
from .instruct import live_instructions
from .nodes import (blocked_reason, holding_ready_nodes, mode_for, node_in_tag_scope, node_row,
                    pick_node, ranked_ready_nodes, tag_blocked_ready_nodes)
from .prompts import started_marker
from .actions import (_ask, _close_commands, _dispatch_many, _maintenance_due,
                      _retry_wait, _sessions_wait, _ticket_wait, _stop_action)


def stop_criterion(project: Path, cycle: dict, node: dict | None, dag: dict | None = None) -> tuple:
    """(must_stop, why). Invariant: instructions may only TIGHTEN the stop rule.

    **Whether this line runs on is decided by the node's own mode alone, and the human gate sits in
    a different place in each of the three tiers.** `auto` has no human gate: the close opens the
    next node itself; `manual`'s gate is in the slot's session -- the whole slot is a back-and-forth
    with the user, and `completed` on the note is the user's own "mark it done, carry on", so the
    closing beat raises no second gate; `checkpoint`'s gate is on the closing beat: one round runs
    and stops, and the next node opens only once the human has said continue.

    `_close_own_node` uses this same rule for the session's own closing call: `--continue` cannot
    pry it open, and `--stop` can only tighten it. `dag` is the one graph read at the close entry,
    handed to the instruction pass's scope judgement (the criterion reads the node's status, and the
    graph is not read again).
    """
    mode = normalize_mode((node or {}).get('mode'))
    if mode == 'checkpoint':
        return True, ('node mode=checkpoint: this round is done, and the next node opens only once '
                      'you say continue')
    for ins in live_instructions(project, target='boundary', cycle=cycle.get('id'),
                                 node=(node or {}).get('id'), dag=dag):
        cap = ins.get('stop_after_nodes')
        if isinstance(cap, int) and cap >= 0:
            # "stop after N": this close is the Nth -- by now all N nodes have been delivered.
            done = len(cycle.get('path') or []) + 1
            if done >= cap:
                return True, f'instruction "{ins.get("text", "")[:40]}": stop after {cap} nodes'
    return False, ''


# ── the pure planner ─────────────────────────────────────────────────────────

def plan(project, cycle_id: str | None = None, decision: str | None = None,
         node: str | None = None, note: str = '', self_verified: bool = False,
         after: str | None = None, decision_given: bool = False) -> dict:
    """Read state and return the single next action. Writes nothing.

    `decision` / `node` / `note` are the worker's own closing answers, meaningful only on the call
    that follows a node whose work has finished (`self_verified`, CLI: `next --after <node_id>`);
    `after` names that node -- a line can hold several at once, so "which node is this close about"
    is an argument, not something the cycle's hand can answer. A `--continue` cannot loosen the stop
    rule; `--stop` and `--node` tighten or aim it.

    With `after` absent (an in-process call, an entry point other than the CLI) the one node in hand
    is taken: a line holding only one has no ambiguity to read.

    **Two gates, each minding its own business**:
      * `next --after <node>` is the gate through which a session closes its own node, and it **does
        not look at the line's status**: the node itself says which line it belongs to (on the CLI
        side `resolve_cycle_for_node` settles that), and a stopped line that could not close even
        its own node would leave that node pinned there forever. When the line is not active this
        gate only records -- see `_close_own_node`: it writes not one word of status, picks no node
        and dispatches nothing.
      * No other `next` drives a line that is not active: it picks nothing, dispatches nothing, and
        only reports the one command that puts the line back in service. The close path is the only
        gate into an inactive line.
    """
    project = Path(project).resolve()
    state = load_state(project)

    cycle = find_cycle(state, cycle_id)
    if cycle is None:
        if cycle_id:
            return _ask(f'no cycle named {cycle_id!r}; known cycles: '
                        f'{[c["id"] for c in state.get("cycles", [])]}', requested=cycle_id)
        return _no_line(state)

    if cycle.get('status') != 'active' and not self_verified:
        return _ask(f'cycle {cycle["id"]!r} exists but is not active ({cycle.get("status")})',
                    requested=cycle['id'], candidates=[cycle['id']],
                    hint=f'pf cycle status {cycle["id"]} active')

    tags = cycle.get('tags') or []
    holding = _holding_ids(cycle)

    # No node in hand: the cycle is starting, or every node it carried has been closed. Either way
    # the only question is what the DAG has, and the answer is the same rule for both.
    if not holding:
        return _pick_next(project, cycle, tags)

    if self_verified:
        # The worker's own closing call: settle the node whose signal it already wrote. The other
        # nodes this line holds stay in hand -- each is closed by its own worker's call.
        closing = after or (holding[0] if len(holding) == 1 else None)
        if closing is None or closing not in holding:
            return {'action': 'wait', 'cycle': cycle['id'], 'commands': [],
                    'note': f'{closing[:8] if closing else "(unnamed)"} is not in the hand of '
                            f'{cycle["id"]} (it is holding '
                            f'{", ".join(x[:8] for x in holding)}) -- there is nothing to settle.'}
        sig = load_signal(project, cycle['id'], closing)
        # The worker's own closing call IS the request to close, so a ticket that already says how it
        # went is enough: `completed` and `failed`/`stopped` are both answers, and one of them is not
        # a reason to wait. Only a ticket that is absent or still `pending` leaves nothing to settle.
        if sig is None or sig.get('status') == 'pending':
            return {'action': 'wait', 'cycle': cycle['id'], 'commands': [],
                    'node_id': closing,
                    'note': f'{closing[:8]} has not handed in a note yet '
                            f'(signals/{cycle["id"]}/{closing[:8]}….json is absent, or still '
                            f'pending) -- there is nothing to settle.'}
        return _close_own_node(project, cycle, closing, sig, decision, node, note,
                               line_active=cycle.get('status') == 'active',
                               decision_given=decision_given)

    # Every node in hand has a terminal ticket: the batch is over, and the close is what consumes
    # them -- one per call, the same stable state there has always been. A node whose ticket has
    # not been written yet is dispatched (its session is already the thing that writes it); a
    # `pending` ticket is the retry case (a cleared signal), and there is nothing to start for it.
    sigs = {x: load_signal(project, cycle['id'], x) for x in holding}
    terminal = [x for x in holding if (sigs[x] or {}).get('status') not in (None, 'pending')]
    if terminal:
        return _ticket_wait(cycle, terminal[0], holding)
    fresh = [x for x in holding if sigs[x] is None]
    waiting = [x for x in fresh if not started_marker(project, cycle['id'], x).is_file()]
    if waiting:
        # Freshly pinned nodes are what a dispatch is for. A node whose session already runs is left
        # out of the list: handing it to the spawner again would boot a second session onto it.
        return _dispatch_many(project, cycle, waiting)
    if fresh:
        return _sessions_wait(cycle, fresh, holding)
    return _retry_wait(cycle, holding[0], holding)


def _no_line(state: dict) -> dict:
    """The sentence for when there is no line to drive: none at all, or lines but none in service.

    The two cases must read differently. Saying "no cycle" while a stopped line is plainly sitting on
    disk (perhaps with an unfinished node still pinned) sends the reader off to start a new one, when
    what should happen is putting that one back in service.
    """
    cycles = state.get('cycles', [])
    in_service = [c['id'] for c in active_cycles(state)]
    if len(in_service) > 1:
        return _ask('several active cycles: continue which one?', candidates=in_service)
    if not cycles:
        return {'action': 'no_cycle', 'cycle': None, 'commands': [],
                'note': 'no active cycle. Creating one needs human intent (which nodes, '
                        'unattended or not); this planner will not invent it.',
                'known_cycles': []}
    idle = [c['id'] for c in cycles]
    if len(idle) == 1:
        only = idle[0]
        return _ask(f'no cycle is active; {only!r} is out of service (inactive)',
                    requested=only, candidates=idle, known_cycles=idle,
                    hint=f'pf cycle status {only} active')
    return _ask(f'no cycle is active right now; these lines are all out of service: {idle}',
                candidates=idle, known_cycles=idle,
                hint='pf cycle status <cycle> active')


def _pick_next(project: Path, cycle: dict, tags) -> dict:
    """The cycle has no node in hand: name the next one(s), or stop for a reason a human can act on.

    Several ready rows are not a question: the ranking decides unattended at every close
    (`pick_node` takes the top row), so a line's first pick takes the same rows the fill would.
    The line fills the slots its `parallel` policy allows -- one pin per slot, in ranking order --
    and the pipeline never stops here: a human who wants a different one names it (`--node`,
    `cycle hold`).
    """
    slots = cycle_policy(cycle)['parallel']
    dag = load_dag(project)
    ready = ranked_ready_nodes(project, limit=slots, tags=tags, dag=dag)
    if ready:
        return _open(project, cycle, ready)
    tagged = tag_blocked_ready_nodes(project, tags, dag=dag)
    holding = holding_ready_nodes(project, dag=dag)
    if tagged or holding:
        return _stop_action(project, cycle, blocked_reason(project, tags, tagged, holding),
                            _park_commands(cycle), dag=dag)
    return _stop_action(project, cycle, 'the DAG has no ready node left', _park_commands(cycle),
                        dag=dag)


def _open(project: Path, cycle: dict, nodes: list) -> dict:
    """Point the cycle at one or more nodes. `apply` runs the pins and re-plans into the dispatch."""
    nodes = list(nodes)
    head = nodes[0]
    titles = ', '.join(n.get('title') or n['id'] for n in nodes)
    return {'action': 'open_node', 'cycle': cycle['id'],
            'node_id': head['id'], 'node_title': head.get('title', ''),
            'nodes': [n['id'] for n in nodes],
            'note': f'cycle {cycle["id"]} holds {titles}',
            'commands': [seam.cmd('cycle', 'hold', cycle['id'], n['id']) for n in nodes]}


def _park_commands(cycle: dict) -> list:
    """Stopping is one sentence: turn this line `inactive`.

    There is no paused / completed distinction any more: that fork asked whether the graph still had
    work, and a graph grows and work multiplies, so whether a stopped line should run on is a
    human's call. The tool never writes `active`.
    """
    return [seam.cmd('cycle', 'status', cycle['id'], 'inactive')]


def _close_own_node(project: Path, cycle: dict, node_id: str, sig: dict,
                    decision: str | None, node_arg: str | None, note: str,
                    line_active: bool = True, decision_given: bool = False) -> dict:
    """Turn the worker's own closing call into the mechanical close: record the step, fill the
    slots the close freed, or stop.

    The outcome comes from the signal the worker already wrote ("completed" = it delivered,
    "failed"/"stopped" = it did not), so there is always an answer and nobody to ask. The stop rule
    is computed and enforced here: with no boundary session left, a `--continue` that contradicts
    it would be the hole the rule exists to close.

    The other nodes this line holds stay in hand -- each one is settled by its own worker's call,
    and a settle only ever moves its own node. What this call adds on top is the FILL: the close
    frees one slot (`parallel` minus the slots still in hand), and a slot that is free is filled
    from the ready set
    right here, so a multi-node line keeps itself running without a second driver.

    `line_active=False` -- this line is not in service (a human turned it inactive): closing is still
    the session's right (otherwise that node would stay pinned forever), but it records only. The
    stop rule, the next-node pick and `--node` all stay out of it, `--continue` has no say here, and
    not one word of the line's status is written -- it keeps the status the human gave it.

    This closing pass **reads the graph once** (the `dag` below) and hands it down as an argument:
    the stop rule (the node's own mode, plus the instruction pass's scope judgement), the fill list,
    the blocked reason, the maintenance channel's insertion judgement and the attention list all read
    it -- so stop, fill, cannot-fill and naming-outside-scope each read it that one time.
    """
    dag = load_dag(project)
    named = node_arg                    # the name a human typed; it may not travel on below
    spoken_name = named                 # the reason names it; clearing below does not affect it
    delivered = sig.get('status') == 'completed'
    # A node that did not deliver is always shelved; whether to carry on is decided by this line's
    # `when_shelved` policy alone (default: stop).
    policy_carries = cycle_policy(cycle).get('when_shelved') == 'continue'
    carry_on = delivered or policy_carries
    others = [x for x in _holding_ids(cycle) if x != node_id]
    if not line_active:
        decision, node_arg, carry_on = 'stop', None, False
    decision = decision or ('continue' if carry_on else 'stop')
    tags = cycle.get('tags') or []
    slots = max(cycle_policy(cycle)['parallel'] - len(others), 0)
    if not others and slots <= 1:
        # A single-slot line (or one already full): there is no slot to fill, so take the single-node
        # path -- it owns the human-readable sentence about whether the graph still has work. The
        # multi-node path takes over only when there is a real move to make (fill one slot, or report
        # who else is in hand).
        return _close_last_node(project, cycle, node_id, sig, decision, named, note,
                                policy_carries, line_active, decision_given, dag)

    filled: list = []
    spoke = ''
    if carry_on and decision == 'continue' and slots:
        # The close commands have not run yet, so the DAG still has this node `todo`. Reading it as
        # completed is what un-blocks its dependents for this one question -- excluding it would
        # only hide the node, and the fill would then miss the very nodes it just un-blocked.
        assumed = [node_id] if delivered else []
        if named:
            chosen = pick_node(project, named, exclude=node_id, exclude_many=others,
                               assume_completed=assumed, tags=tags, dag=dag)
            if chosen and not node_in_tag_scope(chosen, tags):
                spoke = (f'Note: the named {chosen.get("title") or chosen["id"]}'
                         f' ({chosen["id"]}) is not in this cycle\'s filter set {list(tags)};'
                         f' you named it by hand, so it is taken as named.')
        else:
            chosen = None
            ranked = ranked_ready_nodes(project, limit=slots, exclude=node_id, exclude_many=others,
                                        assume_completed=assumed, tags=tags, dag=dag)
            filled = ranked
        if chosen is not None:
            filled = [chosen]

    stop_why = ''
    if carry_on and not filled:
        # Nothing is being filled this round: either the close says stop, or the ready set has
        # nothing. Only then does the stop rule decide -- a line with nodes still in hand and a slot
        # to fill carries on regardless.
        must_stop, why = stop_criterion(project, cycle, node_row(dag, node_id), dag=dag)
        if must_stop:
            decision, stop_why = 'stop', why

    outcome = 'completed' if delivered else (sig.get('status') or 'failed')
    # The node this close shelves has to be named by this package itself: the DAG command has not
    # run yet, so on disk it is still todo.
    # (`outcome` is the note's verdict, `shelved` is a node status -- each has its own recipient, so
    # do not mix them.)
    shelved = [] if delivered else [node_id]

    if not line_active:
        # A line that is not in service: records only. Close this node, mark the DAG, then stop --
        # not even the line's status is written.
        reason = (f'cycle {cycle["id"]!r} is not active ({cycle.get("status")}): a close on this '
                  f'line only records -- it closes {node_id[:8]} and marks the DAG, picks no next '
                  f'node and dispatches nothing; the line keeps the status it was given.')
        if spoken_name:
            reason += f' The named node {spoken_name} was not opened.'
        reason += f' To carry on: pf cycle status {cycle["id"]} active'
        cmds = _close_commands(project, cycle, node_id, outcome, 'stop', None, park=False)
        return _stop_action(project, cycle, reason, cmds, node_id=node_id, shelved_nodes=shelved,
                            dag=dag)

    if decision == 'stop' or not filled:
        # With other nodes still in flight this line is not stopped: those nodes are still held, so
        # the line has not stopped, and the baton is on their own closes -- parking it would make
        # their closes record-only, and the work they free would have nobody to take it up.
        park_line = decision == 'stop' or not others
        cmds = _close_commands(project, cycle, node_id, outcome, 'stop', None, park=park_line)
        open_notes = (f' (still holding {", ".join(x[:8] for x in others)}, their notes are '
                      f'collected as they arrive)'
                      if decision == 'stop' and others else '')
        if not delivered and not policy_carries:
            slipped = note or sig.get('summary') or '(no reason written)'
            reason = f'this node did not deliver ({outcome}): {slipped}'
        elif stop_why:
            reason = f'{stop_why} ({note})' if note else stop_why
            if mode_for(node_id, dag) == 'checkpoint':
                reason += (f'; to carry on: pf cycle status {cycle["id"]} active, '
                           f'then pf next --cycle {cycle["id"]} --spawn --json')
        elif named:
            reason = (f'the node {node_arg} named by the instruction is not a ready node; '
                      f'stopping here')
            outside = _named_node_outside_scope(dag, node_arg, tags)
            if outside:
                reason += (f'; it is also not in this cycle\'s filter set {list(tags)}'
                           f' (its tags={outside})')
        elif decision_given:
            # A hand-given --stop: this reason has to say a human stopped it, and must not read as
            # "the graph has no work left".
            reason = ('the human chose to stop at the close (--stop): the line goes back to you'
                      + (f' ({note})' if note else ''))
        elif not filled and carry_on:
            # The fill came up empty: say which of the two reasons it was.
            if others:
                reason = (f'{node_id[:8]} is closed; still holding '
                          f'{", ".join(x[:8] for x in others)}, their notes are collected as they '
                          f'arrive (pf next --after <node id> --json) -- this line stays in '
                          f'service, and on its own close that slot fills and dispatches.')
            else:
                tagged = tag_blocked_ready_nodes(project, tags, exclude=node_id,
                                                 exclude_many=others, dag=dag)
                holding = holding_ready_nodes(project, exclude=node_id, exclude_many=others,
                                              dag=dag)
                if tagged or holding:
                    reason = blocked_reason(project, tags, tagged, holding)
                else:
                    reason = 'the graph has no ready node left'
        else:
            reason = f'stopping at the close: {note or "(no reason written)"}'
        if open_notes:
            reason += open_notes
        # `shelved` is empty for a node that closed as completed: naming it here would hand the
        # reader a revive hint for work that succeeded.
        return _stop_action(project, cycle, reason, cmds, node_id=node_id, shelved_nodes=shelved,
                            stop_line=park_line, dag=dag)

    # The whole fill list is handed over, not just its head: as many free slots as there are
    # (`parallel` minus what is still held) get filled, and every one has to be pinned -- pinning
    # only the head leaves the rest of the slots quietly empty.
    cmds = _close_commands(project, cycle, node_id, outcome, 'continue', filled)
    # The maintenance channel's one judgement, at the one moment it can be made: the settle that
    # closes a node is the only place a reading and a freshly picked successor are both in hand.
    # With nothing configured it answers `None` and this whole feature costs one dict lookup.
    due = _maintenance_hit(project, cycle, sig, dag)
    if due is not None:
        # The maintenance action is a close that also edits the graph, so it carries the same close
        # commands (`_close_commands` with no successor) ahead of its own: the graph edit runs after
        # the closed node is marked, and the pin of the maintenance node comes last in the list.
        return _maintenance_due(project, cycle, node_id, due, filled[0],
                                # park=False: the line is NOT parked by this close -- the
                                # maintenance node is pinned in its place and dispatched right
                                # after, so a `cycle status inactive` here would strand it.
                                # `filled[0]` is the node parked behind the maintenance node; the
                                # rest of the fill is pinned here like any other close's fill.
                                close_commands=_close_commands(project, cycle, node_id, outcome,
                                                               'continue', filled[1:], park=False),
                                extra_note=spoke)
    return {'action': 'open_node', 'cycle': cycle['id'], 'node_id': filled[0]['id'],
            'node_title': filled[0].get('title', ''),
            'nodes': [n['id'] for n in filled],
            'note': f'{node_id[:8]} closed; opening '
                    + ', '.join(n.get('title') or n['id'] for n in filled)
                    + (f'; {spoke}' if spoke else ''),
            'follow': {'action': 'dispatch', 'cycle': cycle['id'],
                       'node_id': filled[0]['id'], 'node_title': filled[0].get('title', '')},
            'commands': cmds}


def _close_last_node(project: Path, cycle: dict, node_id: str, sig: dict,
                     decision: str | None, named: str | None, note: str,
                     policy_carries: bool, line_active: bool,
                     decision_given: bool = False, dag: dict | None = None) -> dict:
    """Close the last node in a line's hand -- the fork away from the multi-node path: here the
    question is whether the graph still has work.

    The judgements match the multi-node path (the stop rule, the blocked reason,
    `blocked_reason`/the stop on the last sentence), and an empty `others` comes here: the two cases
    read separately and neither masks the other. `dag` is the one graph read at the close entry,
    handed all the way down here.
    """
    dag = dag if dag is not None else load_dag(project)
    delivered = sig.get('status') == 'completed'
    tags = cycle.get('tags') or []
    carry_on = delivered or policy_carries
    spoken_name = named                 # the name a human typed: the reason names it; clearing
                                        # below does not affect it
    if not line_active:
        decision, named, carry_on = 'stop', None, False
    decision = decision or ('continue' if carry_on else 'stop')
    # The close commands have not run yet, so the DAG still has this node `todo`. Reading it as
    # completed is what un-blocks its dependents for this one question -- excluding it would only
    # hide the node, and the close would then answer "nothing left" for the very graph whose next
    # node it was supposed to pick. A node that did NOT deliver is shelved instead: it un-blocks
    # nothing, so that read must leave it out of the ready set rather than assume it completed.
    assumed = [node_id] if delivered else []
    remaining = (ranked_ready_nodes(project, exclude=node_id, assume_completed=assumed, tags=tags,
                                    dag=dag)
                 if carry_on else [])
    tagged = (tag_blocked_ready_nodes(project, tags, exclude=node_id, assume_completed=assumed,
                                      dag=dag)
              if carry_on and not remaining else [])
    holding = (holding_ready_nodes(project, exclude=node_id, assume_completed=assumed, dag=dag)
               if carry_on and not remaining else [])
    blocked = tagged + holding

    stop_why = ''
    if carry_on:
        must_stop, why = stop_criterion(project, cycle, node_row(dag, node_id), dag=dag)
        if must_stop:
            decision, stop_why = 'stop', why
    next_node = None
    spoke = ''
    if carry_on and decision == 'continue':
        next_node = pick_node(project, named, exclude=node_id, assume_completed=assumed, tags=tags,
                              dag=dag)
        if next_node is None:
            decision = 'stop'
        elif named and not node_in_tag_scope(next_node, tags):
            spoke = (f'Note: the named {next_node.get("title") or next_node["id"]}'
                     f' ({next_node["id"]}) is not in this cycle\'s filter set {list(tags)};'
                     f' you named it by hand, so it is taken as named.')

    # What the ticket said, kept verbatim on the path; every outcome that is not `completed` is a
    # non-delivery for the DAG -- the node is shelved (its dependents stay parked) so a human decides
    # whether to revive it. `stopped` is not a lesser failure: the work is equally not there.
    outcome = 'completed' if delivered else (sig.get('status') or 'failed')
    # The node this close shelves has to be named by this package itself: the DAG command has not
    # run yet, so on disk it is still todo.
    # (`outcome` is the note's verdict, `shelved` is a node status -- each has its own recipient, so
    # do not mix them.)
    shelved = [] if delivered else [node_id]

    if not line_active:
        # A line that is not in service: records only. Close this node, mark the DAG, then stop --
        # not even the line's status is written.
        reason = (f'cycle {cycle["id"]!r} is not active ({cycle.get("status")}): a close on this '
                  f'line only records -- it closes {node_id[:8]} and marks the DAG, picks no next '
                  f'node and dispatches nothing; the line keeps the status it was given.')
        if spoken_name:
            reason += f' The named node {spoken_name} was not opened.'
        reason += f' To carry on: pf cycle status {cycle["id"]} active'
        cmds = _close_commands(project, cycle, node_id, outcome, 'stop', None, park=False)
        return _stop_action(project, cycle, reason, cmds, node_id=node_id, shelved_nodes=shelved,
                            dag=dag)

    cmds = _close_commands(project, cycle, node_id, outcome, decision,
                           [next_node] if next_node else None)
    if decision == 'stop' or next_node is None:
        if not delivered and not policy_carries:
            slipped = note or sig.get('summary') or '(no reason written)'
            reason = f'this node did not deliver ({outcome}): {slipped}'
        elif stop_why:
            reason = f'{stop_why} ({note})' if note else stop_why
            # The middle tier stops to wait for a human's "continue" -- stopping turns the line
            # inactive, so hand over both commands (back in service, and carry on); otherwise
            # "continue" would have to guess how to continue.
            if mode_for(node_id, dag) == 'checkpoint':
                reason += (f'; to carry on: pf cycle status {cycle["id"]} active, '
                           f'then pf next --cycle {cycle["id"]} --spawn --json')
        elif named:
            reason = (f'the node {named} named by the instruction is not a ready node; '
                      f'stopping here')
            outside = _named_node_outside_scope(dag, named, tags)
            if outside:
                reason += (f'; it is also not in this cycle\'s filter set {list(tags)}'
                           f' (its tags={outside})')
        elif decision_given:
            # A hand-given --stop: this reason has to say a human stopped it, and must not read as
            # "the graph has no work left".
            reason = ('the human chose to stop at the close (--stop): the line goes back to you'
                      + (f' ({note})' if note else ''))
        elif blocked:
            reason = blocked_reason(project, tags, tagged, holding)
        elif not remaining:
            reason = 'the graph has no ready node left'
        else:
            reason = f'stopping at the close: {note or "(no reason written)"}'
        # `shelved` is empty for a node that closed as completed: naming it here would hand the
        # reader a revive hint for work that succeeded.
        return _stop_action(project, cycle, reason, cmds, node_id=node_id, shelved_nodes=shelved,
                            dag=dag)

    # The pointer is NOT pinned here: the returned `open_node` is what `apply` runs, and it re-plans
    # into the dispatch through the same `_open` path a cycle with no node in hand uses.
    # The maintenance channel's one judgement, at the one moment it can be made: the settle that
    # closes a node is the only place a reading and a freshly picked successor are both in hand.
    # With nothing configured it answers `None` and this whole feature costs one dict lookup.
    due = _maintenance_hit(project, cycle, sig, dag)
    if due is not None:
        # The maintenance action is a close that also edits the graph, so it carries the same close
        # commands (`_close_commands` with no successor) ahead of its own: the graph edit runs after
        # the closed node is marked, and the pin of the maintenance node comes last in the list.
        return _maintenance_due(project, cycle, node_id, due, next_node,
                                # park=False: the line is NOT parked by this close -- the
                                # maintenance node is pinned in its place and dispatched right
                                # after, so a `cycle status inactive` here would strand it.
                                close_commands=_close_commands(project, cycle, node_id, outcome,
                                                               'stop', None, park=False),
                                extra_note=spoke)
    return {'action': 'open_node', 'cycle': cycle['id'], 'node_id': next_node['id'],
            'node_title': next_node.get('title', ''),
            'note': f'{node_id[:8]} closed; opening {next_node.get("title", "")}'
                    + (f'; {spoke}' if spoke else ''),
            'follow': {'action': 'dispatch', 'cycle': cycle['id'],
                       'node_id': next_node['id'], 'node_title': next_node.get('title', '')},
            'commands': cmds}


def _maintenance_hit(project: Path, cycle: dict, sig: dict | None, dag: dict):
    """The metric the maintenance channel is opening for, or `None`.

    The whole judgement is one call into `maintenance.crossed`: the reading off this node's ticket,
    the lines this line carries in its own policies, and the tag gate. A line with no line
    configured gets `None` here -- and that is what keeps the close byte-for-byte what it was
    before this channel existed. The `dag` handed down is the one read at the close entry: the
    dedup judgement reads the graph, and that is what it reads.
    """
    return maintenance.crossed(project, cycle, sig, dag)


def _named_node_outside_scope(dag: dict, node_id: str, tags) -> list | None:
    """Does the node named by `--node <id>` carry a tag the filter set does not recognize? If so,
    report it back."""
    if not tags:
        return None
    node = (dag.get('nodes') or {}).get(node_id)
    if not node:
        return None
    node_tags = [t for t in (node.get('tags') or []) if isinstance(t, str)]
    if not node_tags or node_in_tag_scope(node, tags):
        return None
    return node_tags
