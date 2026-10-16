"""The parallel-mode fork: one worktree per node, and the record that proves it was closed out.

并行模式（SKILL「并行：worktree」）把一个节点的代码活放进主树的
`.pf/worktrees/<节点短id>` 检出里，分支 `pf/<节点短id>`（git 的 ref 名不许以点开头，分支就取目录名去掉那个点）；
`.pf/` 自己永远落主树。今天这一套全靠人手敲 git 命令，两个门槛（主树干净、名字没被占）都靠备料那一下自律——
而收尾那边只有 SKILL 的一句「合回清理做完再 handoff」，没人核对。这个模块把两头都变成
工具闸：

    fork-worktree <node_id>   备料末尾：检查主树干净、短 id 没被占，才 `git worktree add`，
                              并写下 `.pf/meta/FORK-<短id>.json`（node / branch / head / at）
    next --after <node_id>    收尾开头：盘上有 FORK 记录的节点，必须 worktree 与分支都已删
                              （合回清理完成）才许结算；闸过了把记录摘掉

记录为什么放 `.pf/meta/`：它是这一趟作业的过程物、不是要留下的东西，`.pf/.gitignore` 把
`meta/` 挡在版本库外面，两种档位都一样。缺记录时收尾一个字节都不多动，这是回归底线；有了
记录，合回没做完就响亮拒绝，两条线才不会在主树上互相踩。

短 id 口径：节点全 id 的前 `SHORT_ID_LEN` 个字符（uuid 的第一段）。`cmd_cycle_current` 的节点列、
merge driver 的冲突行都是这个截法，SKILL 的 `<节点短id>` 也是它。
"""

import subprocess
import sys
from pathlib import Path

from .store import LEDGER_NAME, _atomic_write, _now, _resolve_project_root

#: 短 id 的长度：uuid 第一段的 8 个字符。
SHORT_ID_LEN = 8


def node_short_id(node_id: str) -> str:
    """节点全 id 的短形式：并行模式的 worktree 目录与分支名都由它命名。"""
    return node_id[:SHORT_ID_LEN]


def worktree_path(project: Path, node_id: str) -> Path:
    """一个节点的并行 worktree 检出：`<project>/.pf/worktrees/<短id>`。"""
    return Path(project) / LEDGER_NAME / 'worktrees' / node_short_id(node_id)


def worktree_branch(node_id: str) -> str:
    """一个节点的并行出口分支：`pf/<短id>`（SKILL「并行：worktree」的原话）。

    分支名取自记录目录：git 的 ref 名不许以点开头，于是目录 `.pf/` 对应的分支前缀就是 `pf/`。
    """
    return f'pf/{node_short_id(node_id)}'


def fork_record_path(project: Path, node_id: str) -> Path:
    """fork 的一次性记录：`<project>/.pf/meta/FORK-<短id>.json`。"""
    return Path(project) / LEDGER_NAME / 'meta' / f'FORK-{node_short_id(node_id)}.json'


def _git(project: Path, *args) -> subprocess.CompletedProcess:
    """git 的一答。cwd 必须是解析后的主树根：并行模式的 fork 可能是从 worktree 的 cwd 发起的，
    而主树干净与否、分支占没占、worktree 加在哪，问的都是主树。"""
    return subprocess.run(['git', *args], cwd=str(project), capture_output=True, text=True,
                          encoding='utf-8', errors='replace')


def _branch_exists(project: Path, branch: str) -> bool:
    return _git(project, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}').returncode == 0


def cmd_fork_worktree(node_id: str) -> None:
    """备料的最后一拍：给这个节点开 worktree，并写下 fork 记录。

    四道检查各点各名：git 本身没得用、主树不干净（他线可能正在收尾中段）、
    检出目录已被占、出口分支已被占。worktree add 本身是原子的（分支重了 git 自己拒），
    这里前置检查只为把「撞上了什么」说清楚，而不是让调用方去读 git 的 stderr。
    """
    project = _resolve_project_root(Path.cwd())
    short = node_short_id(node_id)
    wt_rel = Path(LEDGER_NAME) / 'worktrees' / short
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
    """收尾兜底：FORK 记录还在，合回就必须已经清完，否则不许结算。

    合回是三步（SKILL「并行：worktree」）：worktree 里并出口分支、主树 squash 合入、然后
    `git worktree remove` + `git branch -D`。第三步没做，说明这次收尾走的是并行路径但没走完
    ——再往下 handoff、派发，下一格就开在一个还挂着旧分支的树上。所以：worktree 目录或
    出口分支任何一个还在，就在写任何东西之前响亮拒绝；两样都清了，才摘掉记录放行。
    盘上没有记录的节点从这里原样通过（连 git 都不问）：非并行收尾一个字节都不多动。
    """
    record = fork_record_path(project, node_id)
    if not record.exists():
        return
    short = node_short_id(node_id)
    wt_rel = Path(LEDGER_NAME) / 'worktrees' / short
    branch = worktree_branch(node_id)
    remaining = []
    if worktree_path(project, node_id).exists():
        remaining.append(f'worktree {worktree_path(project, node_id)} 还在')
    if _branch_exists(project, branch):
        remaining.append(f'分支 {branch} 还在')
    if remaining:
        print(f'Error: 节点 {node_id} 的合回没做完（{"；".join(remaining)}）。先照 SKILL'
              f'「并行：worktree」把合回清完（`git worktree remove {wt_rel.as_posix()}` 与 '
              f'`git branch -D {branch}`），再收尾这个节点；确认这个节点没走过 fork 时，'
              f'删掉 {record} 再来。收尾什么都没改。', file=sys.stderr)
        sys.exit(2)
    record.unlink(missing_ok=True)
