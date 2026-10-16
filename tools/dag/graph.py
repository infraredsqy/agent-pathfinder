#!/usr/bin/env python3
"""The graph itself: adjacency, order, reachability, scores, cycles, readiness.

Nine pure functions over one shape -- `{uid: node}` in, values out. No file, no lock, no `argparse`,
no `sys.exit`: a caller hands over the nodes it already has and gets an answer back. `dag-manage.py`
is the only caller that is a CLI, and it loads this file by path the way every neighbour here is
loaded (`tools/shared/loader.py`); the tests beside this file import it directly.

The record's own words -- `status`, `dependencies`, `weight`, `tags` -- are read exactly as they are
written on disk. Nothing here decides what a word means beyond "completed", "todo" and "shelved"
being the three the graph is ranked by.
"""

from __future__ import annotations


def _adjacency(nodes: dict) -> dict:
    """Build {uid: [dep_uid, ...]} from a nodes dict."""
    return {uid: list(n.get('dependencies', [])) for uid, n in nodes.items()}


def _reverse_adj(adj: dict) -> dict:
    """Reverse adjacency: {uid: [dependent_uid, ...]}."""
    rev = {u: [] for u in adj}
    for u, deps in adj.items():
        for v in deps:
            if v in rev:
                rev[v].append(u)
    return rev


def _can_reach(adj: dict, start: str, target: str,
               visited: set = None):
    """Return (bool, path) whether target is reachable from start."""
    if visited is None:
        visited = set()
    if start == target:
        return True, [target]
    if start in visited:
        return False, []
    visited.add(start)
    for v in adj.get(start, []):
        if v not in adj:          # dangling reference -- skip
            continue
        ok, path = _can_reach(adj, v, target, visited)
        if ok:
            return True, [start] + path
    return False, []


def _topological_sort(adj: dict) -> list:
    """Kahn topological sort; return [uid, ...] with dependencies before dependents."""
    rev = _reverse_adj(adj)
    in_deg = {}
    for u in adj:
        count = 0
        for v in adj[u]:
            if v in adj:
                count += 1
        in_deg[u] = count

    order = []
    queue = [u for u in in_deg if in_deg[u] == 0]
    while queue:
        u = queue.pop(0)
        order.append(u)
        for v in rev.get(u, []):
            if v in in_deg:
                in_deg[v] -= 1
                if in_deg[v] == 0:
                    queue.append(v)

    # Any remaining nodes (cycles) appended at the end
    for u in adj:
        if u not in order:
            order.append(u)
    return order


def _urgency_scores(adj: dict) -> dict:
    """Return {uid: urgency} where higher = more urgent = fewer blockers.

    Normalized so the least urgent node in the graph gets 0.
    """
    topo = _topological_sort(adj)
    dep_set = {}
    for u in topo:
        s = {u}
        for v in adj[u]:
            if v in dep_set:
                s |= dep_set[v]
        dep_set[u] = s
    raw = {u: -(len(s) - 1) for u, s in dep_set.items()}
    if not raw:
        return raw
    offset = -min(raw.values())
    return {u: v + offset for u, v in raw.items()}


def _importance_scores(adj: dict) -> dict:
    """Reverse-set DP: count transitive dependents (excl. self).

    Higher = more nodes depend on this = more important.
    """
    topo = _topological_sort(adj)
    rev = _reverse_adj(adj)
    dep_set = {}
    for u in reversed(topo):
        s = {u}
        for v in rev.get(u, []):
            if v in dep_set:
                s |= dep_set[v]
        dep_set[u] = s
    return {u: len(s) - 1 for u, s in dep_set.items()}


def _is_ready(deps: list, all_nodes: dict) -> bool:
    """A node is ready when every dependency is completed or absent."""
    for d in deps:
        n = all_nodes.get(d)
        if n is not None and n.get('status') != 'completed':
            return False
    return True


def _node_ready(node: dict, all_nodes: dict) -> bool:
    """`ready` 的唯一定义：节点自己是 `todo`，且每个依赖都已 `completed`（或不存在）。

    `completed` 的节点不该被推荐（活已经交了），`shelved` 的节点在等人。这比 _is_ready 更窄：
    依赖满足了但节点自己不是 `todo`，照旧不 ready。

    另一半是**管线的事，这个工具不碰**：一条 cycle 正拿在手上的节点，管线那边自己不挑
    （那是 state.json 里的记录）。所以这里不读 state.json，也不知道 cycle 是什么 ——
    `ready` 只说这张图。调用方传进来的 node 就是盘上那个词。
    """
    return node.get('status') == 'todo' and _is_ready(node.get('dependencies', []), all_nodes)


def _detect_cycle(adj: dict):
    """Three-colour DFS cycle detection. Returns (has_cycle, path_list)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    colour = {u: WHITE for u in adj}
    parent = {}
    out_path = []

    def dfs(u):
        colour[u] = GRAY
        for v in adj.get(u, []):
            if v not in colour:
                continue
            if colour[v] == GRAY:                     # back edge
                path = [v]
                cur = u
                while cur != v:
                    path.append(cur)
                    cur = parent.get(cur)
                    if cur is None:
                        break
                path.append(v)
                path.reverse()
                out_path.extend(path)
                return True
            if colour[v] == WHITE:
                parent[v] = u
                if dfs(v):
                    return True
        colour[u] = BLACK
        return False

    for u in adj:
        if colour[u] == WHITE:
            if dfs(u):
                return True, out_path
    return False, []