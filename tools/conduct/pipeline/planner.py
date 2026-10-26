"""`plan()` and the settle-and-close of one node.

The stop rule, the read-only planner that returns the one next action, and the close a worker's
own `next --after <node_id>` performs.
"""

from __future__ import annotations

from pathlib import Path

from . import feature_host, seam
from .store import (_holding_ids, active_cycles, cycle_policy, find_cycle,
                    load_dag, load_signal, load_state, normalize_mode)
from .instruct import live_instructions
from .nodes import (blocked_reason, holding_ready_nodes, mode_for, node_in_tag_scope, node_row,
                    pick_node, ranked_ready_nodes, tag_blocked_ready_nodes)
from .prompts import started_marker
from .actions import (_ask, _close_commands, _dispatch_many, _insertions_due,
                      _pick_insertions_due, _retry_wait, _sessions_wait, _ticket_wait, _stop_action)


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
        dag = load_dag(project)
        gated = _feature_gate(project, cycle, waiting, dag)
        if gated is not None:
            return gated
        return _dispatch_many(project, cycle, waiting, dag=dag)
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
    `cycle hold`). A feature may put a node in front of the pick (`_feature_pick_insertions`); that
    answer is a graph edit with no pins, so the pick itself is then made on the next step, with the
    inserted node in the same ranking.
    """
    slots = cycle_policy(cycle)['parallel']
    dag = load_dag(project)
    ready = ranked_ready_nodes(project, limit=slots, tags=tags, dag=dag)
    if ready:
        # A gate sits at the dispatch, and pinning is the first half of one: asked before the pins so
        # a refused dispatch leaves nothing pinned behind it.
        gated = _feature_gate(project, cycle, [n['id'] for n in ready], dag)
        if gated is not None:
            return gated
        # The other timing an insertion is allowed at: asked here, after the ready set is known and
        # before anything is pinned, so the nodes a feature wants enter the graph ahead of the pick
        # instead of behind it. What comes back pins nobody -- the next step picks by ranking, and
        # the inserted node is in that ranking.
        offers = _feature_pick_insertions(project, cycle, dag, ready[0])
        if offers:
            # `None` = every offer was skipped (a feature whose text builder raises): the pick is
            # then the ordinary one, exactly as if nothing had asked.
            action = _pick_insertions_due(project, cycle, offers, ready[0])
            if action is not None:
                return action
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

    **One ladder, one entry.** Every question a close asks -- did it deliver, may the line run on,
    who fills the freed slots, and why it stopped -- is answered in this one name, for a line that
    holds one node and for one that holds several alike. How many slots the line holds at once
    (`parallel`) and how many are still in hand (`others`) are parameters, and they decide four
    things:

      * how many nodes this close opens -- one per free slot (see the fill below);
      * where the stop rule sits -- a one-slot line asks it whatever the graph has next (that close
        is its whole round), a line holding several asks it only when the refill comes up empty (a
        slot that can be refilled carries the line on);
      * the shape of the answer -- a one-slot line's open names its one node, a list close's carries
        the `nodes` list its dispatch is built from;
      * which reading the blocked reason uses when nothing was filled -- a one-slot close counts
        this node as delivered, so the dependents it just un-blocked are the work it names, while a
        list close reads the graph as it stands on disk (the asymmetry is deliberate).

    The outcome comes from the signal the worker already wrote ("completed" = it delivered,
    "failed"/"stopped" = it did not), so there is always an answer and nobody to ask. The stop rule
    is computed and enforced here: with no boundary session left, a `--continue` that contradicts
    it would be the hole the rule exists to close.

    The other nodes this line holds stay in hand -- each one is settled by its own worker's call,
    and a settle only ever moves its own node. What this call adds on top is the FILL: the close
    frees one slot per node it puts down (`parallel` minus the slots still in hand), and a slot that
    is free is filled from the ready set right here, so a multi-node line keeps itself running
    without a second driver.

    `line_active=False` -- this line is not in service (a human turned it inactive): closing is still
    the session's right (otherwise that node would stay pinned forever), but it records only. The
    stop rule, the next-node pick and `--node` all stay out of it, `--continue` has no say here, and
    not one word of the line's status is written -- it keeps the status the human gave it.

    This closing pass **reads the graph once** (the `dag` below) and hands it down as an argument:
    the stop rule (the node's own mode, plus the instruction pass's scope judgement), the fill list,
    the blocked reason, the dispatch gate, the features' insertion judgement and the attention list
    all read it -- so stop, fill, cannot-fill, gate-refused and naming-outside-scope each read it
    that one time.

    **A close that fills a slot asks the dispatch gate before it hands the fill over.** The fill is
    the first half of a dispatch (its commands pin the nodes), so a gate that refuses it must refuse
    BEFORE the pins: the answer then settles this node, pins nobody and writes no prompt, and the
    line stays in service for the next step to ask again -- the same shape the pick path has.
    """
    dag = load_dag(project)
    named = node_arg                    # the name a human typed; it may not travel on below
    spoken_name = named                 # the reason names it; clearing below does not affect it
    delivered = sig.get('status') == 'completed'
    policy = cycle_policy(cycle)
    # A node that did not deliver is always shelved; whether to carry on is decided by this line's
    # `when_shelved` policy alone (default: stop).
    policy_carries = policy.get('when_shelved') == 'continue'
    carry_on = delivered or policy_carries
    others = [x for x in _holding_ids(cycle) if x != node_id]
    if not line_active:
        decision, named, carry_on = 'stop', None, False
    decision = decision or ('continue' if carry_on else 'stop')
    tags = cycle.get('tags') or []
    # How many slots this close may open, and whether this line fills them one at a time or several
    # at once: the line's own `parallel` minus what is still in hand. These two numbers are the
    # whole of the difference between a one-slot close and a list close.
    parallel = policy['parallel']
    slots = max(parallel - len(others), 0)
    several = parallel > 1

    # The close commands have not run yet, so the DAG still has this node `todo`. Reading it as
    # completed is what un-blocks its dependents for this one question -- excluding it would only
    # hide the node, and the fill would then miss the very nodes it just un-blocked.
    assumed = [node_id] if delivered else []

    # ── the fill: every free slot gets one node ──────────────────────────────
    filled: list = []
    spoke = ''
    if carry_on and decision == 'continue' and slots:
        if named:
            # A hand-named node is taken whether or not the filter set covers it, and the note says
            # so out loud: a filter silently swallowing a node somebody typed in full would hide the
            # one decision a human just made.
            chosen = pick_node(project, named, exclude=node_id, exclude_many=others,
                               assume_completed=assumed, tags=tags, dag=dag)
            if chosen is not None:
                filled = [chosen]
            if chosen and not node_in_tag_scope(chosen, tags):
                spoke = (f'Note: the named {chosen.get("title") or chosen["id"]}'
                         f' ({chosen["id"]}) is not in this cycle\'s filter set {list(tags)};'
                         f' you named it by hand, so it is taken as named.')
        else:
            filled = ranked_ready_nodes(project, limit=slots, exclude=node_id,
                                        exclude_many=others, assume_completed=assumed,
                                        tags=tags, dag=dag)

    # ── the stop rule: where it sits is the parameter above ──────────────────
    stop_why = ''
    if carry_on and (not several or not filled):
        # A one-slot line's close is its whole round, so the gate is asked whatever the graph has
        # next. A line holding several asks only when nothing was filled: a slot that CAN be
        # refilled carries the line on regardless.
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
        elif not filled and carry_on:
            # Nothing is being filled: either this line still holds work (the baton is on those
            # closes), or the graph has nothing it can pick -- and then say which of the two.
            if others:
                reason = (f'{node_id[:8]} is closed; still holding '
                          f'{", ".join(x[:8] for x in others)}, their notes are collected as they '
                          f'arrive (pf next --after <node id> --json) -- this line stays in '
                          f'service, and on its own close that slot fills and dispatches.')
            else:
                # What a one-slot line's close counts as delivered is this node: the dependents it
                # just unblocked are the work the reader is looking for. A list close reads the
                # graph as it stands on disk.
                blocked_assumed = assumed if not several else ()
                tagged = tag_blocked_ready_nodes(project, tags, exclude=node_id,
                                                 exclude_many=others,
                                                 assume_completed=blocked_assumed, dag=dag)
                holding = holding_ready_nodes(project, exclude=node_id, exclude_many=others,
                                              assume_completed=blocked_assumed, dag=dag)
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

    # ── the dispatch gate, before the pins it would refuse ───────────────────
    # The commands below ARE the first half of a dispatch: they close this node and hold the fill.
    # Asked at the dispatch built out of them it would be too late -- the successor would be pinned
    # with no prompt and no session, while the answer still claimed nothing was. So it is asked
    # here, on the fill, exactly as `_pick_next` asks it before its pins.
    refused = _gate_reasons(project, cycle, [n['id'] for n in filled], dag)
    if refused:
        # The gate is about opening a session, not about the close: this node is settled all the
        # same (the commands below close it and mark the DAG), but nobody is pinned and no prompt is
        # written. The line stays in service, so the next step asks the gate again.
        closed_only = _close_commands(project, cycle, node_id, outcome, 'stop', None, park=False)
        return _stop_action(
            project, cycle,
            _gate_says(refused) +
            f'. {node_id[:8]} is settled all the same -- closing it pins nothing and needs no '
            f'session -- but no next node was pinned and no prompt was written; the line stays in '
            f'service, and the next step will ask the gate again.',
            closed_only, node_id=node_id, shelved_nodes=shelved, dag=dag, stop_line=False)

    # The whole fill list is handed over, not just its head: as many free slots as there are
    # (`parallel` minus what is still held) get filled, and every one has to be pinned -- pinning
    # only the head leaves the rest of the slots quietly empty.
    cmds = _close_commands(project, cycle, node_id, outcome, 'continue', filled)
    # The features' one judgement, at the one moment it can be made: the settle that closes a node is
    # the only place a reading and a freshly picked successor are both in hand. With nothing
    # configured no feature offers anything and this whole pass costs one dict lookup.
    offers = _feature_insertions(project, cycle, node_id, sig, dag, filled[0])
    if offers:
        # The insertion action is a close that also edits the graph, so it carries the same close
        # commands (`_close_commands` with no successor) ahead of its own edits: they run after the
        # closed node is marked. The graph edit itself pins no node -- it mints one, writes its spec
        # and wires its edges -- and no dispatch follows it.
        inserted = _insertions_due(project, cycle, node_id, offers, filled[0],
                                   # park=False: the insertion pins nothing and no dispatch follows
                                   # it, so a `cycle status inactive` here would take the line out of
                                   # service and the next pick would never reach the inserted nodes.
                                   # The line has to stay in service for the usual ranking to pick
                                   # them up. `filled[0]` is the pick they stand in front of; the
                                   # rest of the fill is pinned here like any other close's fill.
                                   close_commands=_close_commands(project, cycle, node_id, outcome,
                                                                  'continue', filled[1:],
                                                                  park=False),
                                   extra_note=spoke)
        if inserted is not None:
            return inserted
        # `None` = every offer was skipped (a feature whose text builder raises): this close goes on
        # as the ordinary one below -- the node is settled, the slot filled, the next session asked
        # for. One broken folder must not freeze the line at its close.
    # The pointer is NOT pinned here: the returned `open_node` is what `apply` runs, and it re-plans
    # into the dispatch through the same `_open` path a cycle with no node in hand uses. The shape
    # follows the line: a one-slot close names its one node, a list close carries `nodes`.
    opened = {'action': 'open_node', 'cycle': cycle['id'], 'node_id': filled[0]['id'],
              'node_title': filled[0].get('title', '')}
    if several:
        opened['nodes'] = [n['id'] for n in filled]
        opening = ', '.join(n.get('title') or n['id'] for n in filled)
    else:
        opening = filled[0].get('title', '')
    opened['note'] = f'{node_id[:8]} closed; opening {opening}' + (f'; {spoke}' if spoke else '')
    opened['follow'] = {'action': 'dispatch', 'cycle': cycle['id'],
                        'node_id': filled[0]['id'], 'node_title': filled[0].get('title', '')}
    opened['commands'] = cmds
    return opened


def _feature_pick_insertions(project: Path, cycle: dict, dag: dict,
                             target: dict | None = None) -> list:
    """The insertions the features want at a PICK, in registry order (empty is the common case).

    The second of the two timings an insert action may occupy (`feature_registry.TIMINGS` has three
    cells for three timings; a feature declares exactly one). A pick has no node being closed and no
    ticket to read, so what a feature is handed here is the line, the graph read for the pick and the
    node the ranking chose -- nothing else. That is also why `node_id` / `sig` are left out: a
    feature that ran at a close and one that runs at a pick are different folders with different
    declarations, and this one has no ticket to be handed.
    """
    ctx = feature_host.Context(project, cycle=cycle, dag=dag, target=target)
    return feature_host.insertions('pick', ctx)


def _feature_insertions(project: Path, cycle: dict, node_id: str, sig: dict | None, dag: dict,
                        target: dict | None = None) -> list:
    """The insertions the features want at this close, in registry order (empty is the common case).

    The whole judgement is `feature_host.insertions`: this only states WHICH state a feature is
    handed -- the line, the node being closed, that node's ticket, the graph read at the close entry
    and the node the close had picked. A line with nothing configured gets an empty list here, and
    that is what keeps the close byte-for-byte what it was before features existed.

    The `dag` handed down is the one read at the close entry: a feature asking "does this metric
    already have a node open" reads that copy, not the disk again.
    """
    ctx = feature_host.Context(project, cycle=cycle, node_id=node_id, sig=sig, dag=dag,
                               target=target)
    return feature_host.insertions('close', ctx)


def _gate_reasons(project: Path, cycle: dict, node_ids, dag: dict | None = None) -> list:
    """The dispatch gate's answers: one sentence per feature that says not now; empty is a go.

    The one place the gate is asked, whichever path is asking -- the pick before its pins, the close
    before the fill it hands over, and the dispatch the writer builds from either. The context
    carries the node that would be dispatched, which is what a gate judging "may this node's session
    open now?" reads.
    """
    ctx = feature_host.Context(project, cycle=cycle, dag=dag,
                               node_id=(list(node_ids) or [None])[0])
    return feature_host.gates('dispatch', ctx)


def _gate_says(reasons) -> str:
    """A refused dispatch's opening sentence: the gate's own words, verbatim, one per feature."""
    return 'a feature gate blocked this dispatch: ' + '; '.join(reasons)


def _feature_gate(project: Path, cycle: dict, node_ids, dag: dict | None = None) -> dict | None:
    """The stop answer when a feature gate refuses this dispatch, or `None` for a green light.

    A gate sits at the dispatch -- the one moment "does anybody mind another session opening now?"
    can be answered -- and **it is asked before anything is pinned or written, on every path**: the
    pick asks it before its pins (`_pick_next`), a close asks it before the pins its fill hands over
    (`_close_own_node`), and the dispatch the writer builds from either comes after. A refused
    dispatch therefore leaves no node held, no prompt file and no session; a refused close-driven
    dispatch still settles the node that was closing, and pins nobody.

    **A refused dispatch does not park the line.** The contract's own example is a gate about time,
    and the pipeline is not a resident process: whatever wakes it only knows how to run one more
    step. Taking the line out of service would make that step answer "line not in service", which
    kills the gate's whole purpose -- so this answer carries no commands and says the line stays in
    service; the next step simply asks the gate again. The words name the gate(s) that refused, so a
    human can act on the reason rather than on the tool.
    """
    reasons = _gate_reasons(project, cycle, node_ids, dag)
    if not reasons:
        return None
    return _stop_action(
        project, cycle,
        _gate_says(reasons) +
        '. No session was opened, no prompt was written and nothing was pinned; the line stays in '
        'service, and the next step will ask the gate again.',
        [], dag=dag, stop_line=False)


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
