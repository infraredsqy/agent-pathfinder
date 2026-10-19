"""Repair-and-tidy levers over what is already on disk.

Owns cmd_clean_temp: the sweep of the per-node `temp/` directories, which live in each node's own
place (`<project>/.pf/nodes/<id>/temp/`). temp is the one place process records go, so clearing it
is a whole-directory sweep -- never a per-file judgement.

Two rules that are not obvious:

  * a node a cycle is holding RIGHT NOW is skipped. Its temp is where the session running it is
    writing raw output this minute; clearing it would take that evidence away mid-flight.
  * every live worktree is swept too (`.pf/worktrees/<short id>/.pf/nodes/<id>/temp/`). The prompts
    send workers to the MAIN tree for snapshots, but a worker that reads the path wrong creates the
    directory in the worktree instead, and a sweep that only looked at the main tree would leave it
    there to die with the checkout.
"""

import shutil

from .store import _holding_of, _read_state, ledger_dir_for, nodes_dir_for

#: A node that is working right now is not swept. `--force` is for the one case of "that node is dead
#: and yet the cycle still holds it".
RUNNING_NOTE = 'held by a cycle right now; skipped'


def _held_node_ids(project: str | None) -> set:
    """Every node id the cycles on disk are holding right now -- read from this project's own state.json.

    "Reading state" and "reading holding" are two different things, and each goes through its own door
    here: `_read_state` is that record's door (a broken read shouts on the spot), `_holding_of` is the
    canonical shape of a claim. Handing back a silent empty set on an unreadable state would lend
    clean-temp the sentence "no node is held" -- and it would delete the temp in use on that sentence.
    """
    state = _read_state(project)
    return {item['id'] for cycle in state.get('cycles') or [] for item in _holding_of(cycle)}


def _nodes_roots(project: str | None) -> list:
    """Every nodes root a temp could grow under: the main tree's, plus each live worktree's.

    A worktree is a full checkout of the main tree, so its own root is "one more project root": the
    same `nodes_dir_for`.
    """
    ledger = ledger_dir_for(project)
    roots = [(nodes_dir_for(project), '')]
    worktrees = ledger / 'worktrees'
    if worktrees.is_dir():
        for wt in sorted(worktrees.iterdir()):
            roots.append((nodes_dir_for(wt), f'[{wt.name}] '))
    return roots


def cmd_clean_temp(project: str | None = None, node: str | None = None,
                   confirm: bool = False, force: bool = False) -> None:
    """Remove the per-node `temp/` directories (probes, raw output, snapshots).

    Listed by default; `--yes` deletes. Nothing outside a node's own `temp/` is ever touched. A node
    a cycle is holding right now is listed with a note rather than removed -- `--force` overrides
    that for the case where the session is gone and the claim was never released.
    """
    holding = _held_node_ids(project)
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
        print('There is no temp directory to clear.')
        return

    for name, _temp_dir in running:
        print(f'{name[:8]}  {RUNNING_NOTE}')
    for name, _temp_dir in targets:
        files = [f for f in _temp_dir.rglob('*') if f.is_file()]
        size = sum(f.stat().st_size for f in files)
        print(f'{name[:8]}  {len(files):4d} files  {size / 1024:8.1f} KB')
    print(f'{len(targets)} temp directories can be cleared'
          + (f'; another {len(running)} are working right now and skipped.' if running else '.'))

    if not confirm:
        print('Add --yes to delete (no per-file judgement: the whole directory goes).')
        return

    removed = 0
    for _name, temp_dir in targets:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if not temp_dir.exists():
            removed += 1
    print(f'Removed {removed} temp directories.')