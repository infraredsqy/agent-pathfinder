#!/usr/bin/env python3
"""This tree's ledger contract: the one name, the one layout, the one gate.

A project's durable account is its LEDGER, `<project>/.pf/`, and this file is the only place that
spelling lives -- the graph, the cycle book, the signals, the standing instructions, each node's own
text and the issue pool all sit under that one directory, and every tool reaches it from here:

    ledger_dir_for(project)     `<project>/.pf/`
    meta_dir_for(project)       `<project>/.pf/meta/`         the dispatch plumbing, ignored by git
    nodes_dir_for(project)      `<project>/.pf/nodes/`        one directory per node
    config_path_for(project)    `<project>/.pf/config.json`   this project's settings layer
    home_config_path()          `~/.pf/config.json`           the machine's settings layer

The three `*_of(ledger_dir)` forms take the ledger directory itself, for a caller that already holds
one: an in-process binding that moves per project, a worktree's root, home treated as a project. The
project forms are those three one level up. Everything takes its input explicitly -- `project` and
`ledger_dir` are required, never None; a caller whose project is optional keeps that optionality (and
the mutable binding behind it) in its own wrapper. The machine layer is the same shape one home up,
and `PF_HOME_DIR` is its own switch (a sandbox, a drill copy): read here, so the reader and the writer
of the settings resolve that layer the same way.

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
import os

__all__ = ['CONFIG_NAME', 'INIT_MARKERS', 'LEDGER_NAME', 'config_path_for', 'config_path_of',
           'home_config_path', 'init_message', 'is_initialized', 'ledger_dir_for', 'meta_dir_for',
           'meta_dir_of', 'nodes_dir_for', 'nodes_dir_of', 'resolve_project_root']

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
    """cwd is the root; the one exception: when cwd is a linked worktree's checkout root, go back to
    the main tree (the rule lives in the ledger).

    In parallel mode a node's code work goes into the main tree's `.pf/worktrees/<short id>`, so the
    worker's cwd lands inside the worktree; and a worktree is a full checkout, so in commit mode it
    holds a copy of `.pf/` as of the moment of the fork -- if a recording command accepted that copy,
    settling would land on a stale snapshot, and the two lines would not see each other's claims. A
    worktree root's `.git` is a **file** (`gitdir: <main tree>/.git/worktrees/<name>`), while in the
    main tree it is a directory: follow the pointer, and three levels up from `worktrees` is the main
    tree root. A submodule's `.git` file points at `.git/modules/...`, which is not a worktree, so it
    stays put in cwd. When the main tree has no ledger (it is not a pipeline project) nothing moves,
    so the error that follows fires right where it is.
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
    """`<project>/.pf/`."""
    return Path(project) / LEDGER_NAME


def meta_dir_of(ledger_dir) -> Path:
    """`<ledger>/meta/`: the dispatch artifacts (the launch prompt, the claim/started markers, the
    broken-chain marker, the session notes and that one lock)."""
    return Path(ledger_dir) / 'meta'


def nodes_dir_of(ledger_dir) -> Path:
    """`<ledger>/nodes/`: one directory per node (its spec, its deliverables, the `temp/` it made)."""
    return Path(ledger_dir) / 'nodes'


def config_path_of(ledger_dir) -> Path:
    """`<ledger>/config.json`: this project's settings layer."""
    return Path(ledger_dir) / CONFIG_NAME


def meta_dir_for(project) -> Path:
    """`<project>/.pf/meta/`."""
    return meta_dir_of(ledger_dir_for(project))


def nodes_dir_for(project) -> Path:
    """`<project>/.pf/nodes/`."""
    return nodes_dir_of(ledger_dir_for(project))


def config_path_for(project) -> Path:
    """`<project>/.pf/config.json`."""
    return config_path_of(ledger_dir_for(project))


def home_config_path() -> Path:
    """This machine's settings layer: `~/.pf/config.json` (the same one `pf config set --global` writes).

    Home is counted as a project, so this layer has the same spelling and the same name as the project
    layer. `PF_HOME_DIR` points "home" somewhere else (a sandbox for tests and drills, the same
    precedent as `ZCODE_DATA_BASE_DIR`): it is this layer's own switch, read here, so the reading side
    and the writing side do not resolve that layer each their own way.
    """
    home = os.environ.get('PF_HOME_DIR') or str(Path.home())
    return config_path_for(Path(home).expanduser())


def is_initialized(ledger_dir) -> bool:
    """Whether this ledger counts as init-ed: it is there and both init markers are there."""
    ledger = Path(ledger_dir)
    return ledger.is_dir() and all((ledger / marker).is_file() for marker in INIT_MARKERS)


def init_message(ledger_dir, *, project=None) -> str:
    """The one sentence a refused write is told when nothing was init-ed: the ledger, which two markers
    are missing, and how to run `init`.

    `project` is the project root the caller named: when it is given it goes into that example command
    line (`pf init --project`), because such a call may not run inside the project; when it is not, the
    line is "run `pf init` where you are".
    """
    tail = f' --project "{project}"' if project else ''
    return (f'{ledger_dir} was never init-ed (missing {" or ".join(INIT_MARKERS)}). Run this first:\n'
            f'  pf init{tail}')