"""The `next` command: the one command that drives the pipeline.

Owns cmd_next: the `--plan` preview, the `--after` closing call and the dispatch/spawn tail -- and
cmd_dispatch, the standalone half of that tail (`dispatch <cycle>` starts the session whose prompt
the closing call already wrote). The decision logic itself lives in `conduct_pipeline.py`, loaded via
`_load_pipeline()`.

`dispatch` lives next to `next` on purpose: it is the same spawn call, under the same drill switch
(`DSH_CONDUCT_SPAWN_DRY_RUN`), against the same prompt path -- and a second file would be a second
place for that convention to drift.
"""

import json
import os
import sys
from pathlib import Path

from .store import (_closed_steps_of_node, _cycles_holding_node, _holding_ids, _read_state,
                    _resolve_project_root)
from .fork import gate_close_on_fork
from .pipeline_loader import _load_pipeline


# ── the closing call's by-node resolution ────────────────────────────────────

def resolve_cycle_for_node(cycles: list, node: str) -> dict | None:
    """Which cycle is the one that just finished `node`? -- None if it is not decidable.

    The closing call carries only `--after <node_id>`: a worker should not have to learn cycle
    bookkeeping. The node names its own cycle, and this is where that is read.
    """
    holders = _cycles_holding_node(cycles, node)
    return holders[0] if len(holders) == 1 else None


def describe_holders(cycles: list, node: str) -> str:
    """A human sentence for why `--after <node>` could not be resolved."""
    holders = _cycles_holding_node(cycles, node)
    if len(holders) > 1:
        return (f'--after {node} is the node of several cycles '
                f'({", ".join(c["id"] for c in holders)}); name it with --cycle.')
    closed = _closed_steps_of_node(cycles, node)
    if closed:
        return (f'--after {node} is already closed on {closed}; there is nothing left to settle. '
                f'Reopen it, or start a new cycle.')
    heads = ', '.join(f'{c["id"]}:{", ".join(_holding_ids(c)) or "(none)"}' for c in cycles)
    return (f'--after {node} does not name a node any cycle is holding. '
            f'Every cycle holds {heads or "none"}.')


def cmd_next(args) -> None:
    """Print (and, unless told otherwise, carry out) the pipeline's next action.

    `args` is the parsed `next` line (state-manage's namespace): every field this command needs is read
    here, off that one object. A new flag is therefore one line in the parser plus the one line here
    that reads it -- no second place restating a default the parser already holds.

    The decision logic lives in conduct_pipeline.py, loaded by path so that every caller gets a
    module of its own (the test harness loads a fresh copy per sandbox). Two modes:

      plan    read-only: state -> the next action. Writes nothing at all.
      apply   drive the pipeline to its next *stable* action -- consume a finished node's signal,
              do every piece of bookkeeping that follows, and stop at the first thing a caller can
              act on (`dispatch`, `wait`, `stop_pipeline`).

    `--continue|--stop` / `--node` / `--note` are the closing call's answers: they are refused
    unless the call settles a node (`--after`) or previews one (`--plan`).

    `--after <node_id>` is the worker's own closing call: it settles the node that just finished
    against the signal the worker wrote, then either dispatches the next node or stops. It settles on
    the spot, so it runs even under `--plan`: the point of the call is the outcome, and a preview
    that could not report it would be a second command for the worker to learn.
    """
    pipeline = _load_pipeline()
    mode = 'plan' if args.plan else 'apply'
    cycle_id, after, node, note = args.cycle, args.after, args.node, args.note
    decision = 'continue' if args.cont else 'stop' if args.stop else None
    decision_given = bool(args.cont or args.stop)
    spawn, spawner, named_project, as_json = args.spawn, args.spawner, args.project, args.json

    # The closing call's answers answer a settle. With neither `--after` nor `--plan` there is no
    # question for them to answer, and the branch below would drop them on the floor in silence.
    if (decision or node or note) and not (after or mode == 'plan'):
        print("Error: --continue/--stop/--node/--note are the closing call's answers and need "
              '--after <node> (settle that node) or --plan (preview it). Nothing was changed.',
              file=sys.stderr)
        sys.exit(2)

    # cwd 在 linked worktree 里时回主树：并行模式下收尾那几条命令是从 worktree 的 cwd 跑的，
    # 结转、挑人、prompt 全部落主树的记录与 scratch，worktree 只是代码检出。
    project = (Path(named_project).expanduser().resolve() if named_project
               else _resolve_project_root(Path.cwd()))

    # 裸 `next` 是给「至多一条线在跑」的世界的简写。两条线并排时它不再是简写而是歧义——歧义
    # 是调用方式的问题，不是管线的问题，所以在这里响亮拒掉并列出候选，而不是发一个 ask_user
    # 给人猜（ask_user 只留给管线自己真的拿不了主意的事）。
    if cycle_id is None and after is None:
        running = [c['id'] for c in pipeline.load_state(project).get('cycles', [])
                   if c.get('status') == 'active']
        if len(running) > 1:
            print('Error: several cycles are active (' + ', '.join(running)
                  + '); bare `next` cannot pick one. Name the line: '
                  'pf next --cycle <id> …', file=sys.stderr)
            sys.exit(2)
    try:
        if after:
            # 收尾兜底（并行模式）：这个节点还留着 FORK 记录，就要求合回已经清完
            # （worktree 与出口分支都删了）才许结算。没有记录的节点原样通过，连 git 都不问。
            gate_close_on_fork(project, after)
            # Settle the node that just finished. Which cycle it belongs to is read off the node
            # itself -- a worker should not have to learn cycle bookkeeping -- and the name is
            # checked BEFORE anything is written: closing a node over a different cycle's work is
            # worse than not settling, and the caller's only way to know is a line on stderr and no
            # writes. `apply` (not `plan`) because the outcome -- dispatch the next node, or stop --
            # is the whole point of the call.
            state = pipeline.load_state(project)
            cycles = state.get('cycles', [])
            if cycle_id:
                cycle = pipeline.find_cycle(state, cycle_id)
                if cycle is None:
                    print(f'Error: no cycle named {cycle_id!r}; nothing was changed.',
                          file=sys.stderr)
                    sys.exit(2)
            else:
                cycle = resolve_cycle_for_node(cycles, after)
                if cycle is None:
                    print(f'Error: {describe_holders(cycles, after)} Nothing was changed.',
                          file=sys.stderr)
                    sys.exit(2)
                cycle_id = cycle['id']
            if after not in _holding_ids(cycle):
                print(f'Error: --after {after} does not name a node this cycle is holding '
                      f'({"+".join(x[:8] for x in _holding_ids(cycle)) or "none"} of cycle '
                      f'{cycle["id"]}). Nothing was changed. '
                      f'Name the node whose work you are closing.', file=sys.stderr)
                sys.exit(2)
            action = pipeline.apply(project, cycle_id, decision=decision, node=node, note=note,
                                self_verified=True, after=after,
                                decision_given=decision_given)
        elif mode == 'plan':
            action = pipeline.plan(project, cycle_id, decision=decision, node=node, note=note,
                                   decision_given=decision_given)
        else:
            action = pipeline.record(project, cycle_id)
    except (RuntimeError, ValueError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(2)
    # `--spawn` is what makes the worker's closing call one-and-done: the session that settled its
    # own node does not then have to hand-roll the DSH API calls to start the node it picked.
    # `--plan` is read-only and must stay that way -- never spawn on a dry run -- unless the caller
    # asked for `--after`, which is a settle call by definition.
    #
    # `DSH_CONDUCT_SPAWN_DRY_RUN=1` is the drill switch: the spawn call is made and its result
    # recorded exactly as in production, but `spawn_session` returns the stub line instead of
    # starting a session (and the launch dir is kept, because a failed/absent real spawn is the case
    # a human has to retry by hand).
    if spawn and (mode != 'plan' or after) and action.get('action') == 'dispatch' \
            and action.get('nodes'):
        dry = os.environ.get('DSH_CONDUCT_SPAWN_DRY_RUN', '') not in ('', '0', 'false', 'False')
        # One node or several: `nodes` is the dispatch's own list (a single-node line has one item),
        # and every session is started from its own prompt file. A session that fails stops the
        # sweep right there -- the rest is a retry the human drives with `dispatch <cycle>`.
        spawned = []
        skipped = []
        try:
            # 这条线握着的每一格走同一份派发，spawner 也只有一个答案：CLI 点名 > 线策略 >
            # 环境变量 > 宿主自认，在循环外一次问清（`resolve_spawner` 总是给得出一条路径）。
            chosen = pipeline.resolve_spawner(project, action['cycle'], spawner)
            for item in action['nodes']:
                if pipeline.started_marker(project, action['cycle'], item['node_id']).is_file():
                    # 这一格的会话上一拍就开出去了（收尾重规划出来的那份名单里可能还带着它）：
                    # 跳过 —— 同一个节点上第二个会话的代价收不回来。
                    skipped.append(item['node_id'])
                    continue
                spawned.append(pipeline.spawn_session(
                    project, action['cycle'], item['prompt'], dry_run=dry,
                    spawner=chosen))
                if not dry:
                    pipeline.clean_launch_dir(project, action['cycle'], item['node_id'])
        except (RuntimeError, ValueError) as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(3)
        if skipped:
            action['spawn_skipped'] = skipped
        action['spawned'] = spawned if len(spawned) > 1 else (spawned[0] if spawned else '')
    # 输出只有一套：`render_action` 管 JSON 与人读的那一版，也管「预览不可执行」那条规则。
    # `--plan` 与 `--after` 一起给出时这一趟其实在结算（上面走的是 apply 那条路），所以预览只认
    # 真的预览了的那一种。
    print(pipeline.render_action(action, as_json, preview=mode == 'plan' and not after))


def cmd_dispatch(args) -> None:
    """Dispatch and nothing else: start the session for the node this cycle is holding.

    `args` is the parsed `dispatch` line (state-manage's namespace), read here field by field.

    The closing call (`next --after <node> --json`) deliberately stops short of starting anything --
    it settles the node, picks the next one and writes its prompt to
    `.pf/meta/launch/<cycle>/<node_id>/prompt.txt`. The worker that closed its node still has to
    commit and push its own work, and the next session boots in the SAME worktree and pulls on its
    first line, so starting it before the push would hand it a half-finished tree. The two halves are
    therefore two commands:

        pf next --after <node> --json     settle it, write the next prompt
        git commit … && git push                       the worker's own work
        pf dispatch <cycle>               start that prompt's session (this verb)

    It only ever READS the ledger and starts one session: no `status`, no `path`, no `holding`,
    no writes anywhere except the spawner's own `.pf/meta/` bookkeeping (the stale-marker cleanup,
    which lives in `dsh-spawn.ps1` and is untouched here). A real spawn also drops the delivered
    launch directory, exactly as `next --spawn` does; `DSH_CONDUCT_SPAWN_DRY_RUN=1` reports the call
    without opening a session and keeps the prompt file, which is what a human retries by hand.
    """
    pipeline = _load_pipeline()
    cycle_id, as_json, spawner, named_project = args.id, args.json, args.spawner, args.project
    root = (Path(named_project).expanduser().resolve() if named_project
            else _resolve_project_root(Path.cwd()))
    try:
        state = pipeline.load_state(root)
    except (RuntimeError, ValueError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(2)
    cycle = pipeline.find_cycle(state, cycle_id)
    if cycle is None:
        print(f'Error: no cycle named {cycle_id!r} in {pipeline.ledger_dir(root)}; nothing to dispatch.',
              file=sys.stderr)
        sys.exit(2)
    # The account carries exactly the two words, so this one comparison is the whole judgement.
    if cycle.get('status') != 'active':
        print(f'Error: cycle "{cycle_id}" is {cycle.get("status")!r}, not active: a line that is not '
              f'in service does not dispatch. `pf cycle status {cycle_id} active` '
              f'puts it back.', file=sys.stderr)
        sys.exit(2)
    holding = _holding_ids(cycle)
    if not holding:
        print(f'Error: cycle "{cycle_id}" is holding no node, so there is nothing to dispatch. '
              f'`next --cycle {cycle_id} --json` picks the next one and writes its prompt.',
              file=sys.stderr)
        sys.exit(2)
    prompts = []
    already = []
    for node_id in holding:
        # prompt 的落点与 started 记号都由管线那一侧算（`prompts.launch_prompt_path` /
        # `started_marker`）：这一侧只交出「哪条线、哪一格」。
        if pipeline.started_marker(root, cycle_id, node_id).is_file():
            # 这一格的会话已经开过了（started 记号是上一次成功派发留下的）。再交给 spawner 一次
            # 就是在同一个节点上开第二个会话 —— 跳过它，绝不重开。
            already.append(node_id)
            continue
        prompt = pipeline.launch_prompt_path(root, cycle_id, node_id)
        if not prompt.is_file():
            print(f'Error: no prompt file at {prompt}. That file is written by the closing call '
                  f'that settled this node: `pf next --after {node_id} --json`.',
                  file=sys.stderr)
            sys.exit(2)
        prompts.append((node_id, prompt))
    if not prompts:
        named = ', '.join(x[:8] for x in already) or '（无）'
        print(f'Error: every node this cycle holds already had its session started ({named}), so '
              f'there is nothing to dispatch: doing it again would boot a second session onto the '
              f'same node. To reopen one, retry it the documented way: '
              f'`pf cycle retry {cycle_id} <node> --drop`, then '
              f'`pf cycle hold {cycle_id} <node>`, then '
              f'`pf next --cycle {cycle_id} --spawn --json`.', file=sys.stderr)
        sys.exit(2)
    dry = os.environ.get('DSH_CONDUCT_SPAWN_DRY_RUN', '') not in ('', '0', 'false', 'False')
    spawned = []
    try:
        # One node, one session, one prompt file: every node the cycle holds that has a prompt.
        # 与 `next --spawn` 同一个答案：CLI 点名 > 线策略 > 环境变量 > 宿主自认。
        chosen = pipeline.resolve_spawner(root, cycle_id, spawner)
        for node_id, prompt in prompts:
            spawned.append(pipeline.spawn_session(root, cycle_id, str(prompt), dry_run=dry,
                                              spawner=chosen))
            if not dry:
                pipeline.clean_launch_dir(root, cycle_id, node_id)
    except (RuntimeError, ValueError) as e:
        # The same shape as `next --spawn`: the spawner's failure is reported and its own marker
        # (written by dsh-spawn.ps1) is left exactly as it is. What did start stays started; the
        # retry is this same command, and the SPAWN.claim of every started node refuses a re-spawn.
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(3)
    if as_json:
        print(json.dumps({'action': 'dispatch', 'cycle': cycle_id,
                          'node_id': prompts[0][0], 'prompt': str(prompts[0][1]),
                          'nodes': [n for n, _ in prompts],
                          'spawned': spawned if len(spawned) > 1 else (spawned or [''])[0]},
                         ensure_ascii=False, indent=2))
    else:
        print('\n'.join(spawned) if spawned else '')
