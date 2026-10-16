"""The DAG ready set, the tag filter, and the pick.

What a cycle may pick next: the ranked ready set, who another cycle is
holding, what a filter set hides, and the reason nothing can be picked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import seam
from .store import (DEFAULT_MODE, _holding_ids, ledger_dir, load_dag, load_state,
                    normalize_mode)


# ── DAG ──────────────────────────────────────────────────────────────────────

def dag_ready_nodes(project: Path, assume_completed=()) -> list:
    """Ready = a `todo` node whose every dependency is completed (or absent).

    The dependency rule is mirrored from `dag-manage._is_ready` on purpose: two tools answering "is
    this node ready" differently means a node can be runnable in one and stuck in the other -- which
    is how a hand-built or partial graph ends up with work the DAG calls ready and the pipeline
    refuses to start. A dangling dependency id is `dag-manage validate`'s job to catch.

    `shelved` is out of play and never ready, and neither is anything downstream of it: a node that
    came back not delivered parks its whole subtree until a human revives it. `completed` is done.
    Both are the account's own words here -- `load_dag` hands them over exactly as they are written.

    `assume_completed` names nodes to read as completed for this one call: the close question
    is "the node just worked on is done -- is there anything left?", and the close command is what
    writes that status.

    **被 tag 挡住不是依赖没满足**：一条 cycle 的过滤集（`node_in_tag_scope`）只删行，DAG 不为它动。
    """
    nodes = load_dag(project).get('nodes', {})
    assumed = {n for n in assume_completed if n}
    out = []
    for uid, n in nodes.items():
        if n.get('status') in ('completed', 'shelved') or uid in assumed:
            continue
        deps = n.get('dependencies', []) or []
        if all((nodes.get(d) or {}).get('status') == 'completed'
               or d in assumed or d not in nodes for d in deps):
            out.append({'id': uid, 'title': n.get('title', ''), 'mode': n.get('mode') or DEFAULT_MODE,
                        'tags': [t for t in (n.get('tags') or []) if isinstance(t, str)]})
    return out


# ── 节点 tag 与 cycle 分流 ────────────────────────────────────────────────────
# 一条 cycle 的 `tags` 是它的**选材范围**：给「在哪些节点里挑人」画一条线。判据只有一条
# ——节点一个 tag 都没有，或者它的 tag 与过滤集相交，它就在范围里。
#
# 保守方向是**宁可漏挡**：多挑一个节点只多花一个节点的时间，误挡会让一条线的活永远没人做。
#
# 过滤只落在「挑下一个节点」这一步：正在跑的节点照跑完，DAG 的依赖关系一个字不改 ——
# `art` 节点照样能等 `writing` 节点做完才就绪，该等的还等。挡住只影响「挑谁」，不动状态、
# 不标 shelved、不动依赖边。

def node_in_tag_scope(node: dict, cycle_tags=None) -> bool:
    """这个节点在这条 cycle 的选材范围里吗？

    没有过滤集 = 整张图都是范围，与这个功能没出现时逐字节相同；没有 tag 的节点处处可见。
    """
    if not cycle_tags:
        return True
    tags = [t for t in (node.get('tags') or []) if isinstance(t, str)]
    if not tags:
        return True
    return bool(set(tags) & set(cycle_tags))


def tag_blocked_lines(blocked: list) -> list:
    """被过滤集挡在外面的就绪节点，一个一行：标题、id、它身上的 tag。

    这段话会被原样念给人听，所以它按人读的方式写：标题、id、tag，一行一个节点。
    """
    lines = []
    for n in blocked:
        tags = [t for t in (n.get('tags') or []) if isinstance(t, str)]
        lines.append(f'  - {n.get("title") or "(无标题)"}（{n["id"]}）tags={tags}')
    return lines


def cross_line_reason(tags, blocked: list) -> str:
    """别的线就绪、本线无人可挑时那句人话。

    这段 reason 会被原样念给用户听，所以它只做三件事：点名哪些节点就绪、说清被哪些 tag 挡在
    外面、给出一条能直接抄的命令去接那条线。停在这里是**把选择权交回人手上**：一条线的活做完
    了，另一条线要不要开工、什么时候开工，由他说了算。
    """
    need = []
    for n in blocked:
        for t in (n.get('tags') or []):
            if t not in need:
                need.append(t)
    head = (f'本 cycle 的过滤集 {list(tags)} 里没有就绪节点了，但图里还有 '
            f'{len(blocked)} 个节点就绪，只是被 tag 挡在外面：')
    tail = ('一条线的活做完了，另一条线要不要开工、什么时候开工，由你定。\n'
            f'另开一个 cycle 接它：pf '
            f'cycle start <slug> --tags {",".join(need)}')
    return '\n'.join([head, *tag_blocked_lines(blocked), tail])


def _rank_ready_nodes(project: Path, exclude=None, exclude_many=(), assume_completed=()) -> list:
    """The ready set in the DAG's own priority order, unfiltered and untruncated.

    **The ready set comes from `dag_ready_nodes`, and the ranking comes from `dag-manage`.**
    Splitting it this way is what keeps the round-end question honest:

    * `dag-manage list --ready` is a browsing view. It does not exclude COMPLETED nodes, and it
      cannot be told "read this one node as finished" -- which is precisely what the close needs,
      because it asks "is anything left?" *before* the close commands have written the round node's
      status. Excluding the node hides it; assuming it completed un-blocks its dependents. Those
      are different questions and only the second one is the right one.
    * The ranking is a single implementation detail this module must NOT duplicate, so the ready
      set is filtered out of the tool's full (already sorted) listing.
    * A tag filter is applied to this list by `ranked_ready_nodes`/`tag_blocked_ready_nodes`, i.e.
      AFTER the DAG has ranked it: a filter set removes rows, it never re-ranks them.

    `exclude` / `exclude_many` drop ids outright (the nodes a line holds, the node being closed).
    """
    ready = dag_ready_nodes(project, assume_completed=assume_completed)
    drop = set(exclude_many or ())
    if exclude:
        drop.add(exclude)
    if drop:
        ready = [n for n in ready if n.get('id') not in drop]
    if not ready:
        return []
    try:
        proc = subprocess.run(seam.dag('list', '-n', '0'),
                              capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=seam.timeout(), cwd=str(project))
    except (subprocess.TimeoutExpired, OSError):
        return ready
    order = {}
    # 读子进程输出一律 errors='replace'：这台机器的控制台代码页是 GBK，而 PYTHONIOENCODING 未必
    # 设过。宁可节点标题里出现替换符（id 是 ASCII，挑选不受影响），也不能让解码异常把整条管线掀翻。
    if proc.returncode == 0 and proc.stdout:
        try:
            rows = json.loads(proc.stdout)
            if isinstance(rows, list):
                order = {r.get('id'): i for i, r in enumerate(rows) if r.get('id')}
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    ready.sort(key=lambda n: order.get(n.get('id'), len(order) + 1))
    return ready


def node_holders(project: Path) -> dict:
    """{node_id: cycle_id} —— 每条 cycle 现在握着的那组节点。

    一个节点一次只归一条 cycle：cycle 一握住它，别的线在这一步就挑不到它。这份占位表是
    「挑下一个节点」和「谁挑得到谁挑不到」共用的那一份读数 —— 挑人的两步（本线的就绪集、
    被过滤集挡住的那批就绪集）都要按同一份表扣掉，否则同一个节点会一边被算成「可挑」、一边被
    算成「轮不到」，两句话自相矛盾。

    只算还握着的：节点一收尾，cycle 就把它放下（DAG 的 status 自己会让它离开就绪集）。cycle
    自己的 status 不在判定里 —— 一条 parked 在半路的 cycle 照样占着它没跑完的那个节点，直到
    有人把它收尾。
    """
    state = load_state(project)
    holders = {}
    for c in state.get('cycles', []):
        for node in _holding_ids(c):
            holders.setdefault(node, c.get('id'))
    return holders


def _free_ready(project: Path, ranked: list, mine=None) -> list:
    """就绪集里，扣掉别的 cycle 现在握着的那些。

    `mine` 是调用方手上的节点（可以是多个）：它属于调用方，不该因为「被别人拿着」而被扣 ——
    它没有被别人拿着。
    """
    holders = node_holders(project)
    keep = set(mine) if isinstance(mine, (list, tuple, set)) else ({mine} if mine else set())
    return [n for n in ranked if n.get('id') in keep or n.get('id') not in holders]


def ranked_ready_nodes(project: Path, limit: int = 8, exclude=None, exclude_many=(),
                       assume_completed=(), tags=None) -> list:
    """The ready set a cycle may pick from, in the DAG's own priority order.

    `tags` is the cycle's filter set. It only drops rows off the DAG's ranking; the ranking itself
    stays `dag-manage`'s. `None`/empty means the whole graph is in range, exactly as before tags
    existed. Nodes another cycle is holding are not in the set at all: the DAG calls them ready
    (their dependencies are met) and they are not available.
    """
    ranked = _rank_ready_nodes(project, exclude=exclude, exclude_many=exclude_many,
                               assume_completed=assume_completed)
    ranked = _free_ready(project, ranked, [x for x in (exclude, *exclude_many) if x])
    visible = [n for n in ranked if node_in_tag_scope(n, tags)]
    return visible[:limit]


def tag_blocked_ready_nodes(project: Path, tags, exclude=None, exclude_many=(),
                            assume_completed=()) -> list:
    """The ready nodes this cycle's filter set hides -- the other half of the same ranking.

    They are what turns "no node to pick" into a sentence a human can act on: the work is right
    there, it belongs to another line, and starting that line is his call (see `cross_line_reason`).
    Empty when there is no filter set, and that early exit is also what keeps an unfiltered close
    from paying for a second `dag-manage` call.
    """
    if not tags:
        return []
    ranked = _rank_ready_nodes(project, exclude=exclude, exclude_many=exclude_many,
                               assume_completed=assume_completed)
    return [n for n in ranked if not node_in_tag_scope(n, tags)]


def holding_ready_nodes(project: Path, exclude=None, exclude_many=(), assume_completed=()) -> list:
    """Ready nodes another cycle is holding right now: work this cycle cannot pick.

    A line holds them, a tag never blocked them -- the two read differently to a human, and
    `blocked_reason` is where that difference is spelled out.
    """
    holders = node_holders(project)
    drop = set(exclude_many or ())
    if exclude:
        drop.add(exclude)
    ranked = _rank_ready_nodes(project, exclude=exclude, exclude_many=exclude_many,
                               assume_completed=assume_completed)
    return [n for n in ranked if n.get('id') not in drop and n.get('id') in holders]


def blocked_reason(project: Path, tags, tagged: list, holding: list) -> str:
    """Why this cycle has nothing to pick, in the terms of whoever is reading it.

    Two reasons look alike from inside the pipeline (no node in hand) and nothing alike to a human:
    the filter set hides a node, or another cycle is running it. Reporting the second as the first
    sends the reader looking at tags that are not in the way.
    """
    lines = []
    if tagged:
        lines.append(cross_line_reason(tags, tagged))
    if holding:
        holders = node_holders(project)
        holding_lines = []
        for n in holding:
            owner = holders.get(n.get('id')) or '?'
            holding_lines.append(f'  - {n.get("title") or "(无标题)"}（{n["id"]}）：cycle '
                                 f'{owner} 正拿着它')
        lines.append('图里这些节点就绪，但已经被别的 cycle 占着，本线挑不到：\n'
                     + '\n'.join(holding_lines)
                     + '\n要它们，就等那一条线把它们跑完（别的线正忙不属于本线的故障）。')
    return '\n'.join(lines)


def pick_node(project: Path, override: str | None = None, exclude=None, exclude_many=(),
              assume_completed=(), tags=None) -> dict | None:
    """The next node: an explicit instruction wins, otherwise the top of the ready ranking.

    `--node <id>` is the owner naming a node by hand, so the filter set has no say over it: a node
    that is ready is picked whether or not it is in range, and the caller says out loud that it
    came from outside the filter (the alternative -- letting a filter silently swallow a node
    somebody typed out in full -- hides the one decision a human just made). What still refuses is
    a name that is not ready at all, which is the meaning this flag already had.
    """
    ranked = ranked_ready_nodes(project, exclude=exclude, exclude_many=exclude_many,
                                assume_completed=assume_completed, tags=tags)
    if override:
        for n in ranked:
            # Exact match only: a prefix (`--node ab` hitting `abc...`) would aim a round at a
            # node nobody named -- and node ids are uuids, so full ids are always at hand.
            if n['id'] == override:
                return n
        if tags:
            # Named and ready, but outside the filter set: look again in the unfiltered ranking
            # rather than reading "no such ready node".
            for n in ranked_ready_nodes(project, exclude=exclude, exclude_many=exclude_many,
                                        assume_completed=assume_completed):
                if n['id'] == override:
                    return n
        # Named but not ready/present: refuse rather than silently substituting another node.
        return None
    return ranked[0] if ranked else None


def node_spec_path(project: Path, node_id: str) -> str:
    return str(ledger_dir(project) / 'nodes' / node_id / 'spec.md')


def mode_for(node_id: str | None, dag: dict) -> str:
    """The node's own mode, and `auto` when the record leaves the key out.

    Nothing overrides it: the node's mode decides on its own whether the line keeps going after it
    (see `planner.stop_criterion`), so there is no line-level mode left to take precedence over.
    """
    if node_id:
        node = (dag.get('nodes') or {}).get(node_id)
        if node and node.get('mode'):
            return normalize_mode(node['mode'])
    return DEFAULT_MODE
