"""The parallel-mode fork: one worktree per node, and the record that proves it was closed out.

Parallel mode (the SKILL's "parallel: worktree") puts a node's code work into the main tree's
`.pf/worktrees/<node short id>` checkout, on branch `pf/<node short id>` (a git ref name may not start
with a dot, so the branch drops that dot from the directory name); `.pf/` itself always lands in the
main tree. Today this is all hand-typed git commands, and both gates (a clean main tree, an unused
name) rest on the prep step's own self-discipline -- while the closing side has only the SKILL's one
line, "clean up the merge-back before handoff", and nobody checks it. This module turns both ends into
tool gates:

    fork-worktree <node_id>   at the end of prep: check the main tree is clean and the short id is
                              free, then `git worktree add`, and write
                              `.pf/meta/FORK-<short id>.json` (node / branch / head / at)
    next --after <node_id>    at the start of the close: a node with a FORK record on disk may only
                              be settled once worktree and branch are both deleted (the merge-back is
                              cleaned up); once the gate passes, the record is taken off

Why the record lives in `.pf/meta/`: it is this run's process material, not something to keep, and
`.pf/.gitignore` keeps `meta/` out of the repository in both modes. With no record the close does not
touch one byte more -- that is the regression floor; with a record, an unfinished merge-back is refused
loudly, so two lines do not step on each other in the main tree.

Short id rule: the first `SHORT_ID_LEN` characters of the node's full id (the uuid's first segment).
`cmd_cycle_current`'s node column and the merge driver's conflict line both cut it that way, and the
SKILL's `<node short id>` is the same.
"""

import subprocess
import sys
from pathlib import Path

from .store import _atomic_write, _ledger, _loader, _now, _resolve_project_root

#: The short id's length: the 8 characters of the uuid's first segment.
SHORT_ID_LEN = 8


def node_short_id(node_id: str) -> str:
    """The short form of a node's full id: the parallel mode's worktree directory and branch name are both named from it."""
    return node_id[:SHORT_ID_LEN]


def worktree_path(project: Path, node_id: str) -> Path:
    """One node's parallel worktree checkout: `<project>/.pf/worktrees/<short id>`."""
    return _ledger().ledger_dir_for(project) / 'worktrees' / node_short_id(node_id)


def worktree_branch(node_id: str) -> str:
    """One node's parallel exit branch: `pf/<short id>` (the SKILL's "parallel: worktree" wording).

    The branch name comes from the ledger directory's name: a git ref name may not start with a dot,
    so the branch prefix for the directory `.pf/` is `pf/`.
    """
    return f'pf/{node_short_id(node_id)}'


def fork_record_path(project: Path, node_id: str) -> Path:
    """A fork's one-off record: `<project>/.pf/meta/FORK-<short id>.json`."""
    return _ledger().meta_dir_for(project) / f'FORK-{node_short_id(node_id)}.json'


def _git(project: Path, *args) -> subprocess.CompletedProcess:
    """One answer from git; the way to ask is in `tools/shared/gitcmd.py`. cwd must be the resolved main
    tree root: a parallel-mode fork may be started from a worktree's cwd, while whether the main tree is
    clean, whether the branch is taken and where the worktree is added are all main-tree questions."""
    return _loader().load_shared('gitcmd').git(project, *args)


def _branch_exists(project: Path, branch: str) -> bool:
    return _git(project, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}').returncode == 0


def cmd_fork_worktree(node_id: str) -> None:
    """The last beat of prep: fork a worktree for this node and write the fork record.

    Four checks, each naming its own failure: git itself unusable, a dirty main tree (another line may
    be mid-close), the checkout directory already taken, the exit branch already taken. `worktree add`
    is atomic by itself (a duplicate branch is refused by git), so the checks up front are only there to
    say clearly WHAT was hit, rather than making the caller read git's stderr.
    """
    project = _resolve_project_root(Path.cwd())
    short = node_short_id(node_id)
    # git wants that path relative to the main tree: one landing, computed once by `worktree_path`;
    # here it is only taken in its relative form.
    wt_rel = worktree_path(project, node_id).relative_to(project)
    branch = worktree_branch(node_id)

    probe = _git(project, 'rev-parse', '--show-toplevel')
    if probe.returncode != 0:
        print(f'Error: {project} is not inside a git repository ({probe.stderr.strip()}); '
              f'a parallel fork has nothing to fork from.', file=sys.stderr)
        sys.exit(1)

    # `--untracked-files=no`: what blocks a fork is TRACKED work in flight (another line's
    # merge-back or commit), not untracked plumbing -- the dispatch prompts this very line just
    # wrote sit under `.pf/meta/`, which `.pf/.gitignore` keeps out of the tree's business.
    status = _git(project, 'status', '--porcelain', '--untracked-files=no')
    if status.returncode != 0:
        print(f'Error: could not read the main tree\'s git status: {status.stderr.strip()}',
              file=sys.stderr)
        sys.exit(1)
    if status.stdout.strip():
        print(f'Error: the main tree has uncommitted tracked changes -- another line may be '
              f'mid-merge-back (doc-update and the ledger commit leave a dirty window of '
              f'minutes). Wait a few minutes and re-check `git status` before forking:\n'
              f'{status.stdout}', file=sys.stderr)
        sys.exit(1)

    if worktree_path(project, node_id).exists():
        print(f'Error: {wt_rel.as_posix()} already exists -- the short id {short!r} is taken '
              f'(by node {node_id} itself, or the last fork was never cleaned up). Nothing was '
              f'forked.', file=sys.stderr)
        sys.exit(1)
    if _branch_exists(project, branch):
        print(f'Error: branch {branch} already exists -- the short id {short!r} is taken. '
              f'Nothing was forked.', file=sys.stderr)
        sys.exit(1)

    added = _git(project, 'worktree', 'add', wt_rel.as_posix(), '-b', branch)
    if added.returncode != 0:
        print(f'Error: git worktree add failed: {added.stderr.strip()}', file=sys.stderr)
        sys.exit(1)

    head = _git(project, 'rev-parse', 'HEAD').stdout.strip()
    forked_from = _git(project, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip() or 'HEAD'
    _atomic_write(fork_record_path(project, node_id), {
        'node': node_id,
        'branch': forked_from,
        'head': head,
        'at': _now(),
    })
    print(f'FORKED: {node_id} → {project / wt_rel} (branch {branch}, off {forked_from}@{head})')


def gate_close_on_fork(project: Path, node_id: str) -> None:
    """The close's backstop: while a FORK record is still there the merge-back must be finished, or the
    node may not be settled.

    The merge-back is three steps (the SKILL's "parallel: worktree"): merge the exit branch inside the
    worktree, squash it into the main tree, then `git worktree remove` + `git branch -D`. The third step
    not done means this close took the parallel path but did not finish it -- carry on to handoff and
    dispatch, and the next slot opens on a tree still carrying an old branch. So: if the worktree
    directory or the exit branch is still there, refuse loudly before writing anything; only when both
    are gone is the record taken off and the close let through. A node with no record on disk passes
    through here unchanged (git is not even asked): a non-parallel close touches not one byte more.
    """
    record = fork_record_path(project, node_id)
    if not record.exists():
        return
    wt_rel = worktree_path(project, node_id).relative_to(project)
    branch = worktree_branch(node_id)
    remaining = []
    if worktree_path(project, node_id).exists():
        remaining.append(f'worktree {worktree_path(project, node_id)} is still there')
    if _branch_exists(project, branch):
        remaining.append(f'branch {branch} is still there')
    if remaining:
        print(f'Error: Node {node_id}: the merge-back is unfinished ({"; ".join(remaining)}). Clean the '
              f'merge-back up first, as the SKILL says ("parallel: worktree"): `git worktree remove '
              f'{wt_rel.as_posix()}` and `git branch -D {branch}` -- then close this node; if you know it '
              f'never went through a fork, delete {record} and come back. The close changed nothing.',
              file=sys.stderr)
        sys.exit(2)
    record.unlink(missing_ok=True)