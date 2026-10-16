"""Repair-and-tidy levers over what is already on disk.

Owns cmd_clean_temp: the sweep of the per-node `temp/` directories, which live in each node's own
place (`<project>/.pf/nodes/<id>/temp/`). temp is the one place process records go, so clearing it
is a whole-directory sweep -- never a per-file judgement. The migration commands that used to live
here are gone with the shapes they translated: a cycle record in `state.json` is the whole of a cycle
now, and there is no older layout this tool knows how to read.

Two rules that are not obvious:

  * a node a cycle is holding RIGHT NOW is skipped. Its temp is where the session running it is
    writing raw output this minute; clearing it would take that evidence away mid-flight.
  * every live worktree is swept too (`.pf/worktrees/<短id>/.pf/nodes/<id>/temp/`). The prompts
    send workers to the MAIN tree for snapshots, but a worker that reads the path wrong creates the
    directory in the worktree instead, and a sweep that only looked at the main tree would leave it
    there to die with the checkout.
"""

import json
import shutil

from .store import _holding_of, ledger_dir_for, nodes_dir_for

#: 正在干活的节点不扫。`--force` 留给"那个节点已经死了、却还握在 cycle 手里"这一种。
RUNNING_NOTE = '正被 cycle 握着，跳过'


def _holding_ids(project: str | None) -> set:
    """盘上所有 cycle 此刻握着的节点 id。读这个项目自己的 state.json，绕开模块绑定的 cwd。"""
    state_path = ledger_dir_for(project) / 'state.json'
    if not state_path.is_file():
        return set()
    try:
        data = json.loads(state_path.read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError):
        return set()
    holding = set()
    for cycle in data.get('cycles') or []:
        for item in _holding_of(cycle):
            holding.add(item['id'])
    return holding


def _nodes_roots(project: str | None) -> list:
    """每一处可能长出 temp 的 nodes 根：主树的，加上每一棵还活着的 worktree 里的。"""
    ledger = ledger_dir_for(project)
    roots = [(nodes_dir_for(project), '')]
    worktrees = ledger / 'worktrees'
    if worktrees.is_dir():
        for wt in sorted(worktrees.iterdir()):
            roots.append((wt / '.pf' / 'nodes', f'[{wt.name}] '))
    return roots


def cmd_clean_temp(project: str | None = None, node: str | None = None,
                   confirm: bool = False, force: bool = False) -> None:
    """Remove the per-node `temp/` directories (probes, raw output, snapshots).

    Listed by default; `--yes` deletes. Nothing outside a node's own `temp/` is ever touched. A node
    a cycle is holding right now is listed with a note rather than removed -- `--force` overrides
    that for the case where the session is gone and the claim was never released.
    """
    holding = _holding_ids(project)
    targets, running = [], []
    for nodes_dir, tag in _nodes_roots(project):
        if not nodes_dir.is_dir():
            continue
        for node_dir in sorted(nodes_dir.iterdir()):
            if not node_dir.is_dir():
                continue
            if node and node_dir.name != node and not node_dir.name.startswith(node):
                continue
            temp_dir = node_dir / 'temp'
            if not temp_dir.is_dir():
                continue
            row = (tag + node_dir.name, temp_dir)
            if node_dir.name in holding and not force:
                running.append(row)
            else:
                targets.append(row)

    if not targets and not running:
        print('没有 temp 目录可清。')
        return

    for name, _temp_dir in running:
        print(f'{name[:8]}  {RUNNING_NOTE}')
    for name, _temp_dir in targets:
        files = [f for f in _temp_dir.rglob('*') if f.is_file()]
        size = sum(f.stat().st_size for f in files)
        print(f'{name[:8]}  {len(files):4d} 个文件  {size / 1024:8.1f} KB')
    print(f'共 {len(targets)} 个 temp 目录可清'
          + (f'，另有 {len(running)} 个正在干活、跳过。' if running else '。'))

    if not confirm:
        print('加 --yes 删除（逐文件判断不做，整个目录一起走）。')
        return

    removed = 0
    for _name, temp_dir in targets:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if not temp_dir.exists():
            removed += 1
    print(f'已删除 {removed} 个 temp 目录。')
