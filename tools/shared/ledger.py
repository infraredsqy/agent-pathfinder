#!/usr/bin/env python3
"""This tree's ledger contract: the one name, the one layout, the one gate.

A project's durable account is its LEDGER, `<project>/.pf/`, and this file is the only place that
spelling lives -- the graph, the cycle book, the signals, the standing instructions, each node's own
text and the issue pool all sit under that one directory, and every tool reaches it from here:

    ledger_dir_for(project)     `<project>/.pf/`
    meta_dir_for(project)       `<project>/.pf/meta/`         the dispatch plumbing, ignored by git
    nodes_dir_for(project)      `<project>/.pf/nodes/`        one directory per node
    config_path_for(project)    `<project>/.pf/config.json`   this project's settings layer

The three `*_of(ledger_dir)` forms take the ledger directory itself, for a caller that already holds
one: an in-process binding that moves per project, a worktree's root, home treated as a project. The
project forms are those three one level up. Everything takes its input explicitly -- `project` and
`ledger_dir` are required, never None; a caller whose project is optional keeps that optionality (and
the mutable binding behind it) in its own wrapper.

A project either commits its ledger or keeps it to itself, and nothing here cares which. `init` is
what puts one in place, and the two files it leaves behind are the judgement of whether that happened:

    is_initialized(ledger_dir)  both markers are there -- a pure filesystem question, never git
                                (a commit-mode ledger is one `git add` away from being misread)
    init_message(ledger_dir)    the one sentence a refused write is told, tail included

`resolve_project_root(cwd)` is the other judgement here: a cwd that is a linked worktree's checkout
root maps back to the main tree, because that is where the ledger is.

The boundary with the rest of `tools/shared/`: `loader.py`, `jsonio.py` and `ledgerlock.py` are
stdlib-only and know nothing about `.pf` -- the first loads files by path, the second reads and writes
one JSON record, the third hands out a lock over whatever directory it is given. This file is the one
that knows `.pf` as a word, and it still knows nothing about the shape of any record inside it: what a
cycle, a node, a signal or a close looks like belongs to those owners.

It imports the standard library and loads nothing -- not even `loader.py`, which could not be shared
with a file that has no loader yet anyway. A caller reaches it by path, like every other file outside
`tools/shared/` reaches in, through `loader.load_shared('ledger')`. Nothing is bound at import time:
no path is computed from `__file__`, so the name has one home and no copy of it freezes.
"""

from pathlib import Path

__all__ = ['CONFIG_NAME', 'INIT_MARKERS', 'LEDGER_NAME', 'config_path_for', 'config_path_of',
           'init_message', 'is_initialized', 'ledger_dir_for', 'meta_dir_for', 'meta_dir_of',
           'nodes_dir_for', 'nodes_dir_of', 'resolve_project_root']

#: The ledger's own name: `.pf/`, a dot-directory, the tool's own household like `.work/`.
LEDGER_NAME = '.pf'

#: What `init` writes into the ledger. Their presence is the whole judgement of "this `.pf/` was put
#: there on purpose" -- a pure filesystem question, never git (a committed ledger would otherwise read
#: as un-init'ed between `init` and the project's first `git add`).
INIT_MARKERS = ('.gitignore', '.gitattributes')

#: The settings file's name, the same in both override layers: `<project>/.pf/config.json` and
#: `~/.pf/config.json`.
CONFIG_NAME = 'config.json'


def resolve_project_root(cwd) -> Path:
    """cwd 即根，唯一的例外：cwd 是一条 linked worktree 的检出根时，回主树。

    并行模式把节点的代码活放进主树的 `.pf/worktrees/<短id>`，worker 的 cwd 落在 worktree 里；
    而 worktree 是完整检出，提交档下里面有一份 fork 时刻的 `.pf/` —— 记录命令若认它，结转就落在
    陈旧快照上，两条线互相看不见对方的认领。worktree 根的 `.git` 是一个**文件**
    （`gitdir: <主树>/.git/worktrees/<名字>`），主树里是目录：顺着指针走，`worktrees` 的上三级
    就是主树根。submodule 的 `.git` 文件指向 `.git/modules/...`，不是 worktree，留在 cwd 不动。
    主树没有记录目录时（不是管线项目）不挪，让后面的错误当场响。
    """
    cwd = Path(cwd)
    dot_git = cwd / '.git'
    if not dot_git.is_file():
        return cwd
    try:
        first_line = dot_git.read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return cwd
    if not first_line.startswith('gitdir:'):
        return cwd
    target = first_line.split(':', 1)[1].strip()
    gitdir = Path(target)
    if not gitdir.is_absolute():
        gitdir = (cwd / gitdir).resolve()
    if gitdir.parent.name != 'worktrees':
        return cwd
    main = gitdir.parent.parent.parent          # <main>/.git/worktrees/<name> → <main>
    if (main / LEDGER_NAME).is_dir():
        return main
    return cwd


def ledger_dir_for(project) -> Path:
    """`<project>/.pf/`。"""
    return Path(project) / LEDGER_NAME


def meta_dir_of(ledger_dir) -> Path:
    """`<ledger>/meta/`：派发物（launch prompt、claim/started 记号、断链标记、会话便条与那把锁）。"""
    return Path(ledger_dir) / 'meta'


def nodes_dir_of(ledger_dir) -> Path:
    """`<ledger>/nodes/`：每一格自己的目录（spec、交付物、跑出来的 `temp/`）。"""
    return Path(ledger_dir) / 'nodes'


def config_path_of(ledger_dir) -> Path:
    """`<ledger>/config.json`：这个项目的设置覆盖层。"""
    return Path(ledger_dir) / CONFIG_NAME


def meta_dir_for(project) -> Path:
    """`<project>/.pf/meta/`。"""
    return meta_dir_of(ledger_dir_for(project))


def nodes_dir_for(project) -> Path:
    """`<project>/.pf/nodes/`。"""
    return nodes_dir_of(ledger_dir_for(project))


def config_path_for(project) -> Path:
    """`<project>/.pf/config.json`。"""
    return config_path_of(ledger_dir_for(project))


def is_initialized(ledger_dir) -> bool:
    """这个记录目录算不算 init 过：它在，且两枚 init 记号都在。"""
    ledger = Path(ledger_dir)
    return ledger.is_dir() and all((ledger / marker).is_file() for marker in INIT_MARKERS)


def init_message(ledger_dir, *, project=None) -> str:
    """没 init 过时那一句：记录目录、缺的是哪两枚记号、`init` 怎么跑。

    `project` 是调用方点过名的项目根：给了就把它带进那条示例命令行（`pf init --project`），
    因为这种调用可能不在项目里跑；没给就是「就地跑 `pf init`」。
    """
    tail = f' --project "{project}"' if project else ''
    return (f'{ledger_dir} 没 init 过（缺 {" 或 ".join(INIT_MARKERS)}）。先跑：\n'
            f'  pf init{tail}')