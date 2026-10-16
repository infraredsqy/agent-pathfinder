"""`plan()` and the settle-and-close of one node.

The stop rule, the read-only planner that returns the one next action, and the close a worker's
own `next --after <node_id>` performs.
"""

from __future__ import annotations

from pathlib import Path

from . import maintenance, seam
from .store import (DEFAULT_MODE, _holding_ids, _holding_of, active_cycles, cycle_policy, find_cycle,
                    load_dag, load_signal, load_state, normalize_mode)
from .instruct import live_instructions
from .nodes import (blocked_reason, holding_ready_nodes, node_in_tag_scope, node_spec_path,
                    pick_node, ranked_ready_nodes, tag_blocked_ready_nodes)
from .prompts import started_marker
from .actions import (_ask, _close_commands, _dispatch, _dispatch_many, _maintenance_due,
                      _retry_wait, _sessions_wait, _ticket_wait, _stop_action)

__all__ = ['plan', 'stop_criterion', 'node_mode', 'node_spec']


def node_mode(project: Path, node_id: str | None) -> str:
    node = ((load_dag(project).get('nodes') or {}).get(node_id) or {})
    return normalize_mode(node.get('mode'))


def stop_criterion(project: Path, cycle: dict, node: dict | None) -> tuple:
    """(must_stop, why). Invariant: instructions may only TIGHTEN the stop rule.

    **这条线跑不跑下去，只看节点自己的 mode，人闸的位置三档各不同。** `auto` 没有人闸，收尾自己
    开下一个节点；`manual` 的人闸在格子的会话里——整格就是与用户来回推进，条子上的 completed 就是
    用户那句「标成完成，继续」，收尾这一拍不再设第二道闸；`checkpoint` 的人闸在收尾这一拍：跑完
    一轮停下，等人说了继续才开下一个节点。

    `_close_own_node` 对会话自己的收尾调用也用这一条：`--continue` 撬不动它，`--stop` 只能收得更紧。
    """
    mode = (node or {}).get('mode') or DEFAULT_MODE
    if mode == 'checkpoint':
        return True, '节点 mode=checkpoint：这一轮循环跑完了，等你说了继续才开下一个节点'
    for ins in live_instructions(project, target='boundary', cycle=cycle.get('id')):
        cap = ins.get('stop_after_nodes')
        if isinstance(cap, int) and cap >= 0:
            # "跑够 N 个就停"：这一次收尾就是第 N 个 —— 这时 N 个节点都已经交了。
            done = len(cycle.get('path') or []) + 1
            if done >= cap:
                return True, f'指令「{ins.get("text", "")[:40]}」：跑够 {cap} 个节点就停'
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

    `after` 缺省时（in-process 调用，CLI 之外的入口）取手里唯一那个节点：只握一个的线读不出歧义。

    **两条门，各管各的**：
      * `next --after <node>` 是会话收自己那个节点的门，**不看这条线的状态**：节点自己就说明了它
        属于哪条线（CLI 那边由 `resolve_cycle_for_node` 定下来），而一条停下来的线如果连自己的
        节点都收不掉，那个节点就永远钉在那儿了。线上不是 active 时，这条门只做登记 —— 见
        `_close_own_node`：一个字的状态都不写，挑人、派发一概没有。
      * 别的 `next` 一概不驱动一条不是 active 的线：不挑人、不派发，只把那条能把它放回服务的
        命令报出来。收尾那条路是进 inactive 线的唯一一道门。
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
                    'note': f'{closing[:8] if closing else "(未点名)"} 不在 {cycle["id"]} 手里'
                            f'（它握着 {", ".join(x[:8] for x in holding)}）—— 没有可结算的东西。'}
        sig = load_signal(project, cycle['id'], closing)
        # The worker's own closing call IS the request to close, so a ticket that already says how it
        # went is enough: `completed` and `failed`/`stopped` are both answers, and one of them is not
        # a reason to wait. Only a ticket that is absent or still `pending` leaves nothing to settle.
        if sig is None or sig.get('status') == 'pending':
            return {'action': 'wait', 'cycle': cycle['id'], 'commands': [],
                    'node_id': closing,
                    'note': f'{closing[:8]} 还没有交条子（signals/{cycle["id"]}/{closing[:8]}….json '
                            f'不在，或还是 pending）—— 没有可结算的东西。'}
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
    """没有可以驱动的线时那句话：一条都没有，还是有线但都不在服务中。

    两种情况读起来必须不一样。说「没有 cycle」而盘上明明躺着一条停住的线（可能还钉着一个没收尾
    的节点），读的人会去开一条新的，而真正该做的是把那一条放回服务。
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
    ready = ranked_ready_nodes(project, limit=slots, tags=tags)
    if ready:
        return _open(project, cycle, ready)
    tagged = tag_blocked_ready_nodes(project, tags)
    holding = holding_ready_nodes(project)
    if tagged or holding:
        return _stop_action(project, cycle, blocked_reason(project, tags, tagged, holding),
                            _park_commands(cycle))
    return _stop_action(project, cycle, 'DAG 里已经没有 ready 节点', _park_commands(cycle))


def _open(project: Path, cycle: dict, nodes: list) -> dict:
    """Point the cycle at one or more nodes. `apply` runs the pins and re-plans into the dispatch."""
    nodes = list(nodes)
    head = nodes[0]
    titles = ', '.join(n.get('title') or n['id'] for n in nodes)
    return {'action': 'open_node', 'cycle': cycle['id'],
            'node_id': head['id'], 'node_title': head.get('title', ''),
            'nodes': [n['id'] for n in nodes],
            'note': f'cycle {cycle["id"]} 握在 {titles}',
            'commands': [seam.cmd('cycle', 'hold', cycle['id'], n['id']) for n in nodes]}


def _park_commands(cycle: dict) -> list:
    """停手就是一句话：把这条线转成 `inactive`。

    没有 paused / completed 之分了：那条岔路问的是「图上还有没有活」，而图会长大、活会变多，
    一条停下来的线该不该接着跑是人说了算的事。工具永远不写 `active`。
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

    `line_active=False` —— 这条线不在服务中（人把它转成了 inactive）：收尾仍然是会话的权利（否则
    那个节点会永远钉在那儿），但只做登记。停手判据、挑下一个节点、`--node` 一概不走，`--continue`
    在这里没有发言权，线的状态一个字都不写 —— 它保持人给它的那个状态。
    """
    named = node_arg                    # 人敲出来的那个名字，下面可能就不再往下传了
    spoken_name = named                 # reason 里点名用它，不受下面清空影响
    delivered = sig.get('status') == 'completed'
    # 节点没交付时它一定会被搁置；要不要接着走，只由这条线的 `when_shelved` 策略说了算（默认停）。
    policy_carries = cycle_policy(cycle).get('when_shelved') == 'continue'
    carry_on = delivered or policy_carries
    others = [x for x in _holding_ids(cycle) if x != node_id]
    if not line_active:
        decision, node_arg, carry_on = 'stop', None, False
    decision = decision or ('continue' if carry_on else 'stop')
    tags = cycle.get('tags') or []
    slots = max(cycle_policy(cycle)['parallel'] - len(others), 0)
    if not others and slots <= 1:
        # 单格线（或已经满了）：没有格子可补，走单节点那条路 —— 它负责「图里还有没有活」那句
        # 人读的话。多节点那条路只在真有动作可做时接管（补一格、或报手里还有谁）。
        return _close_last_node(project, cycle, node_id, sig, decision, named, note,
                                policy_carries, line_active, decision_given)

    filled: list = []
    spoke = ''
    if carry_on and decision == 'continue' and slots:
        # The close commands have not run yet, so the DAG still has this node `todo`. Reading it as
        # completed is what un-blocks its dependents for this one question -- excluding it would
        # only hide the node, and the fill would then miss the very nodes it just un-blocked.
        assumed = [node_id] if delivered else []
        if named:
            chosen = pick_node(project, named, exclude=node_id, exclude_many=others,
                               assume_completed=assumed, tags=tags)
            if chosen and not node_in_tag_scope(chosen, tags):
                spoke = (f'注意：点名的 {chosen.get("title") or chosen["id"]}'
                         f'（{chosen["id"]}）不在本 cycle 的过滤集 {list(tags)} 里；'
                         f'它是你亲手点的，就按点名走。')
        else:
            chosen = None
            ranked = ranked_ready_nodes(project, limit=slots, exclude=node_id, exclude_many=others,
                                        assume_completed=assumed, tags=tags)
            filled = ranked
        if chosen is not None:
            filled = [chosen]

    stop_why = ''
    if carry_on and not filled:
        # Nothing is being filled this round: either the close says stop, or the ready set has
        # nothing. Only then does the stop rule decide -- a line with nodes still in hand and a slot
        # to fill carries on regardless.
        must_stop, why = stop_criterion(project, cycle,
                                        {'id': node_id, 'mode': node_mode(project, node_id)})
        if must_stop:
            decision, stop_why = 'stop', why

    outcome = 'completed' if delivered else (sig.get('status') or 'failed')
    # 这次收尾要搁置的那个节点，得由这个包自己点名：DAG 的那条命令还没跑，盘上它还是 todo。
    # （`outcome` 是条子上的判决，`shelved` 是节点状态 —— 两者各有各的收件人，别混。）
    shelved = [] if delivered else [node_id]

    if not line_active:
        # 一条不在服务中的线：只做登记。收掉这个节点、标了 DAG，然后停下 —— 连线的状态都不写。
        reason = (f'cycle {cycle["id"]!r} 不是 active（{cycle.get("status")}）：这条线上的收尾只做'
                  f'登记 —— 收掉 {node_id[:8]}、标了 DAG，没有挑下一个节点，也没有派发；'
                  f'线的状态保持原样。')
        if spoken_name:
            reason += f' 点名的节点 {spoken_name} 没有被打开。'
        reason += f' 要接着走：pf cycle status {cycle["id"]} active'
        cmds = _close_commands(project, cycle, node_id, outcome, 'stop', None, park=False)
        return _stop_action(project, cycle, reason, cmds, node_id=node_id, shelved_nodes=shelved)

    if decision == 'stop' or not filled:
        # 手里还有别的节点在飞时不停这条线：那些节点还握着，这条线就没停，接力棒在它们自己的收尾
        # 上 —— park 掉的话，它们的收尾只做登记，腾出来的活再没人接。
        park_line = decision == 'stop' or not others
        cmds = _close_commands(project, cycle, node_id, outcome, 'stop', None, park=park_line)
        open_notes = (f'（手里还有 {", ".join(x[:8] for x in others)}，各自的条子到了照常收）'
                      if decision == 'stop' and others else '')
        if not delivered and not policy_carries:
            slipped = note or sig.get('summary') or '（未写原因）'
            reason = f'这个节点没有交付（{outcome}）：{slipped}'
        elif stop_why:
            reason = f'{stop_why}（{note}）' if note else stop_why
            if node_mode(project, node_id) == 'checkpoint':
                reason += (f'；要接着走：pf cycle status {cycle["id"]} active，'
                           f'再 pf next --cycle {cycle["id"]} --spawn --json')
        elif named:
            reason = f'指令指定的节点 {node_arg} 不是就绪节点，停在这里'
            outside = _named_node_outside_scope(project, node_arg, tags)
            if outside:
                reason += (f'；另外它不在本 cycle 的过滤集 {list(tags)} 里'
                           f'（它的 tags={outside}）')
        elif decision_given:
            # 人亲手给的 --stop：这条 reason 要说是人停的，不能读成"图里没活了"。
            reason = '收尾时人选择了停（--stop）：线交回给你' + (f'（{note}）' if note else '')
        elif not filled and carry_on:
            # The fill came up empty: say which of the two reasons it was.
            if others:
                reason = (f'{node_id[:8]} 收掉了；手里还有 {", ".join(x[:8] for x in others)}，'
                          f'它们的条子到了照常收（pf next --after <节点id> --json）'
                          f'——这条线留在服务中，那一格收尾时自己补格、派发。')
            else:
                tagged = tag_blocked_ready_nodes(project, tags, exclude=node_id,
                                                 exclude_many=others)
                holding = holding_ready_nodes(project, exclude=node_id, exclude_many=others)
                if tagged or holding:
                    reason = blocked_reason(project, tags, tagged, holding)
                else:
                    reason = '图里没有就绪节点了'
        else:
            reason = f'收尾时决定停下：{note or "（未写原因）"}'
        if open_notes:
            reason += open_notes
        # `shelved` is empty for a node that closed as completed: naming it here would hand the
        # reader a revive hint for work that succeeded.
        return _stop_action(project, cycle, reason, cmds, node_id=node_id, shelved_nodes=shelved,
                            stop_line=park_line)

    # 整份补格名单都交出去，不是只交第一个：空位有几个（`parallel` 减手里还握着的）就补几个，
    # 每一个都得 pin 一条 —— 只 pin 头一个，剩下的格会静静地空着。
    cmds = _close_commands(project, cycle, node_id, outcome, 'continue', filled)
    # The maintenance channel's one judgement, at the one moment it can be made: the settle that
    # closes a node is the only place a reading and a freshly picked successor are both in hand.
    # With nothing configured it answers `None` and this whole feature costs one dict lookup.
    due = _maintenance_hit(project, cycle, sig)
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
                    + (f'；{spoke}' if spoke else ''),
            'follow': {'action': 'dispatch', 'cycle': cycle['id'],
                       'node_id': filled[0]['id'], 'node_title': filled[0].get('title', '')},
            'commands': cmds}


def _close_last_node(project: Path, cycle: dict, node_id: str, sig: dict,
                     decision: str | None, named: str | None, note: str,
                     policy_carries: bool, line_active: bool,
                     decision_given: bool = False) -> dict:
    """收尾一条线手里的最后一个节点 —— 与多节点那条路分岔：这里要问「图里还有没有活」。

    判定与多节点那条路一致（停手判据、blocked 理由、blocked_reason/停在最后一句），
    `others` 为空时走这里，两种情况各读各的，互不遮掩。
    """
    delivered = sig.get('status') == 'completed'
    tags = cycle.get('tags') or []
    carry_on = delivered or policy_carries
    spoken_name = named                   # 人敲出来的那个名字：reason 里点名用它，不受下面清空影响
    if not line_active:
        decision, named, carry_on = 'stop', None, False
    decision = decision or ('continue' if carry_on else 'stop')
    # The close commands have not run yet, so the DAG still has this node `todo`. Reading it as
    # completed is what un-blocks its dependents for this one question -- excluding it would only
    # hide the node, and the close would then answer "nothing left" for the very graph whose next
    # node it was supposed to pick. A node that did NOT deliver is shelved instead: it un-blocks
    # nothing, so that read must leave it out of the ready set rather than assume it completed.
    assumed = [node_id] if delivered else []
    remaining = (ranked_ready_nodes(project, exclude=node_id, assume_completed=assumed, tags=tags)
                 if carry_on else [])
    tagged = (tag_blocked_ready_nodes(project, tags, exclude=node_id, assume_completed=assumed)
              if carry_on and not remaining else [])
    holding = (holding_ready_nodes(project, exclude=node_id, assume_completed=assumed)
               if carry_on and not remaining else [])
    blocked = tagged + holding

    stop_why = ''
    if carry_on:
        must_stop, why = stop_criterion(project, cycle,
                                        {'id': node_id, 'mode': node_mode(project, node_id)})
        if must_stop:
            decision, stop_why = 'stop', why
    next_node = None
    spoke = ''
    if carry_on and decision == 'continue':
        next_node = pick_node(project, named, exclude=node_id, assume_completed=assumed, tags=tags)
        if next_node is None:
            decision = 'stop'
        elif named and not node_in_tag_scope(next_node, tags):
            spoke = (f'注意：点名的 {next_node.get("title") or next_node["id"]}'
                     f'（{next_node["id"]}）不在本 cycle 的过滤集 {list(tags)} 里；'
                     f'它是你亲手点的，就按点名走。')

    # What the ticket said, kept verbatim on the path; every outcome that is not `completed` is a
    # non-delivery for the DAG -- the node is shelved (its dependents stay parked) so a human decides
    # whether to revive it. `stopped` is not a lesser failure: the work is equally not there.
    outcome = 'completed' if delivered else (sig.get('status') or 'failed')
    # 这次收尾要搁置的那个节点，得由这个包自己点名：DAG 的那条命令还没跑，盘上它还是 todo。
    # （`outcome` 是条子上的判决，`shelved` 是节点状态 —— 两者各有各的收件人，别混。）
    shelved = [] if delivered else [node_id]

    if not line_active:
        # 一条不在服务中的线：只做登记。收掉这个节点、标了 DAG，然后停下 —— 连线的状态都不写。
        reason = (f'cycle {cycle["id"]!r} 不是 active（{cycle.get("status")}）：这条线上的收尾只做'
                  f'登记 —— 收掉 {node_id[:8]}、标了 DAG，没有挑下一个节点，也没有派发；'
                  f'线的状态保持原样。')
        if spoken_name:
            reason += f' 点名的节点 {spoken_name} 没有被打开。'
        reason += f' 要接着走：pf cycle status {cycle["id"]} active'
        cmds = _close_commands(project, cycle, node_id, outcome, 'stop', None, park=False)
        return _stop_action(project, cycle, reason, cmds, node_id=node_id, shelved_nodes=shelved)

    cmds = _close_commands(project, cycle, node_id, outcome, decision,
                           [next_node] if next_node else None)
    if decision == 'stop' or next_node is None:
        if not delivered and not policy_carries:
            slipped = note or sig.get('summary') or '（未写原因）'
            reason = f'这个节点没有交付（{outcome}）：{slipped}'
        elif stop_why:
            reason = f'{stop_why}（{note}）' if note else stop_why
            # 中间那一档停下来是等人说「继续」—— 停手照旧把线转成 inactive，所以要把放回服务与
            # 接着走的那两条命令一起给他，否则「继续」得自己猜怎么继续。
            if node_mode(project, node_id) == 'checkpoint':
                reason += (f'；要接着走：pf cycle status {cycle["id"]} active，'
                           f'再 pf next --cycle {cycle["id"]} --spawn --json')
        elif named:
            reason = f'指令指定的节点 {named} 不是就绪节点，停在这里'
            outside = _named_node_outside_scope(project, named, tags)
            if outside:
                reason += (f'；另外它不在本 cycle 的过滤集 {list(tags)} 里'
                           f'（它的 tags={outside}）')
        elif decision_given:
            # 人亲手给的 --stop：这条 reason 要说是人停的，不能读成"图里没活了"。
            reason = '收尾时人选择了停（--stop）：线交回给你' + (f'（{note}）' if note else '')
        elif blocked:
            reason = blocked_reason(project, tags, tagged, holding)
        elif not remaining:
            reason = '图里没有就绪节点了'
        else:
            reason = f'收尾时决定停下：{note or "（未写原因）"}'
        # `shelved` is empty for a node that closed as completed: naming it here would hand the
        # reader a revive hint for work that succeeded.
        return _stop_action(project, cycle, reason, cmds, node_id=node_id, shelved_nodes=shelved)

    # The pointer is NOT pinned here: the returned `open_node` is what `apply` runs, and it re-plans
    # into the dispatch through the same `_open` path a cycle with no node in hand uses.
    # The maintenance channel's one judgement, at the one moment it can be made: the settle that
    # closes a node is the only place a reading and a freshly picked successor are both in hand.
    # With nothing configured it answers `None` and this whole feature costs one dict lookup.
    due = _maintenance_hit(project, cycle, sig)
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
                    + (f'；{spoke}' if spoke else ''),
            'follow': {'action': 'dispatch', 'cycle': cycle['id'],
                       'node_id': next_node['id'], 'node_title': next_node.get('title', '')},
            'commands': cmds}


def _maintenance_hit(project: Path, cycle: dict, sig: dict | None):
    """The metric the maintenance channel is opening for, or `None`.

    The whole judgement is one call into `maintenance.crossed`: the reading off this node's ticket,
    the lines this line carries in its own policies, and the tag gate. A line with no line
    configured gets `None` here -- and that is what keeps the close byte-for-byte what it was
    before this channel existed.
    """
    return maintenance.crossed(project, cycle, sig)


def _named_node_outside_scope(project: Path, node_id: str, tags) -> list | None:
    """`--node <id>` 点名的那个节点，身上是否有过滤集不认的 tag？有就把它报回来。"""
    if not tags:
        return None
    node = (load_dag(project).get('nodes') or {}).get(node_id)
    if not node:
        return None
    node_tags = [t for t in (node.get('tags') or []) if isinstance(t, str)]
    if not node_tags or node_in_tag_scope(node, tags):
        return None
    return node_tags


def node_spec(project: Path, node_id: str) -> str:
    return node_spec_path(project, node_id)
