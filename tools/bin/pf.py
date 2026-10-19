#!/usr/bin/env python3
"""`pf`: one command in front of the tools.

Every skill says `pf next`, `pf handoff`, `pf issue top`: where the tools live is resolved here, once,
from this file's own location, so the prose reads the same on every machine.

Everything else forwards to a tool, keeping the tool's own subcommand names so the docs stay
greppable. Which words those are is the table further down, and `--help` prints that same table. A
forwarded verb's flags are its tool's: `pf <verb> --help` is where they are. `pf init` and
`pf register` are forwards too: the registration logic lives next to the ledger it serves, in
`conduct/state/initledger.py`.
"""

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

#: The tools this command fronts. `HERE` is `<repo>/tools/bin`, so the tool tree is one level up --
#: the checkout may sit anywhere: nothing below is absolute.
HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
CONDUCT = TOOLS / 'conduct'
DAG = TOOLS / 'dag' / 'dag-manage.py'
ISSUE = TOOLS / 'issue' / 'issue_manager.py'
STATE_MANAGE = CONDUCT / 'state-manage.py'
HANDOFF = CONDUCT / 'handoff.py'
CONDUCT_PIPELINE = CONDUCT / 'conduct_pipeline.py'
LEDGER_MERGE = CONDUCT / 'ledger-merge.py'
#: Shared plumbing, and every writer treats a missing session note as best effort -- which is exactly
#: why the doctor has to check it by name.
SESSION_NOTE = TOOLS / 'shared' / 'session-note.py'

# 引导：装载器也按路径取，所以这一段只能每个文件自己带（见 `shared/loader.py`）。
SHARED = TOOLS / 'shared'

_LOADER = None


def _loader():
    """`tools/shared/loader.py`: the one loader, for the modules this tree keeps outside `shared/`."""
    global _LOADER
    if _LOADER is None:
        spec = importlib.util.spec_from_file_location('pf_shared_loader', SHARED / 'loader.py')
        _LOADER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_LOADER)
    return _LOADER


def _ledger():
    """`tools/shared/ledger.py`: `.pf/` 的落点与 init 闸门 —— `pf` 只读它，不借任何工具的包。"""
    return _loader().load_shared('ledger')

#: This tree's own version. Release tags are `v<version>`, and every build handed to someone
#: carries its own number -- one number, one content.
VERSION = '0.1.0-alpha.1'


def _verb_version(rest: list[str]) -> int:
    """`pf version`: this tree's version, and where the checkout lives."""
    print(f'pf {VERSION} at {HERE} (tools: {TOOLS})')
    return 0


def _verb_migrate(rest: list[str]) -> int:
    """`pf migrate`: the door stays shut until this system itself needs migrating."""
    print('migrate：这个口子现在关着 —— 没有要迁移的东西；等这套体系自己要迁移时再开。')
    return 0


def _verb_doctor(rest: list[str]) -> int:
    """`pf doctor [--fix]`."""
    return cmd_doctor(fix='--fix' in rest)


#: The words `pf` itself runs. One row each: the name the router matches, the form `--help` prints,
#: the one line of sense, and the function that answers it. Their flags are printed here because
#: nothing else prints them.
SELF_VERBS = (
    ('doctor', 'doctor [--fix]',
     'read the health of this checkout and this project', _verb_doctor),
    ('version', 'version', 'print the version and where this checkout lives', _verb_version),
    ('migrate', 'migrate',
     'inactive for now -- it reopens when this system itself needs migrating', _verb_migrate),
)

#: The words `pf` hands to a tool. One row each: the name, the file that answers it, whether the
#: name goes along in the argv (`pf next` reaches `state-manage.py` as `next`, `pf dag add` reaches
#: `dag-manage.py` as `add`), and the one line of sense `--help` prints.
#:
#: A forwarded verb's flags live in the tool's own parser, and `pf <verb> --help` prints them: that
#: parser is the authority, so no flags are kept here.
FORWARD_VERBS = (
    ('init', STATE_MANAGE, True, 'put the ledger in place (run once per project)'),
    ('register', STATE_MANAGE, True, "write the ledger's git merge driver into this project"),
    ('next', STATE_MANAGE, True, 'show (or do) the next pipeline action'),
    ('dispatch', STATE_MANAGE, True, 'start the session for the node the cycle holds'),
    ('clean-temp', STATE_MANAGE, True, "list (or remove) every node's temp/ directory"),
    ('fork-worktree', STATE_MANAGE, True, "fork one node's parallel-mode worktree"),
    ('handoff', HANDOFF, False, "a worker's own closing call for the line it just ran"),
    ('merge-ledger', LEDGER_MERGE, False, 'the three-way merge driver git calls'),
)

#: The groups: a word whose remaining argv names one of its own verbs. Which verbs those are is the
#: parser behind the group -- `pf cycle --help`, `pf issue --help` -- never a list kept here.
FORWARD_GROUPS = (
    ('cycle', STATE_MANAGE, True, 'one line of work'),
    ('dag', DAG, False, 'the graph'),
    ('issue', ISSUE, False, 'the issue pool'),
    ('config', STATE_MANAGE, True, "this machine's and this project's settings"),
    ('instruction', STATE_MANAGE, True, 'standing user instructions'),
    ('session', STATE_MANAGE, True, "a node's own conversations, which live on this machine only"),
)

#: The files no verb names: the pipeline entry every verb file loads, and the shared session note.
#: `TOOL_FILES` checks these alongside everything the tables above point at.
UNROUTED_TOOLS = (CONDUCT_PIPELINE, SESSION_NOTE)

#: What `doctor` insists on finding: the tables' targets, once each, then the two above -- so a verb
#: added to a table is checked by itself.
TOOL_FILES = tuple(dict.fromkeys(
    [row[1] for row in (*FORWARD_VERBS, *FORWARD_GROUPS)] + list(UNROUTED_TOOLS)))

#: `main()`'s routing table: the names are the ones the three tables above carry, taken as they are.
_SELF_HANDLERS = {row[0]: row[3] for row in SELF_VERBS}

_USAGE_SHAPE = """\
pf -- the agent pipeline's own command

  Level 2 is a verb: it acts on the project or on the pipeline itself. Level 3 is a noun -- a record
  with an identity -- plus the verb that acts on it. So `pf init` and `pf next` are verbs, and
  `pf cycle list`, `pf issue top`, `pf dag add` are a group's verbs.

"""


def _usage_rows(pairs) -> str:
    """The word column and the sense column, one row per pair; a long sense wraps under itself."""
    left_margin, column, width = 12, 42, 100
    out = []
    for left, right in pairs:
        body = textwrap.wrap(right, width - column) or ['']
        out.append(f'{" " * left_margin}{left:<{column - left_margin}}{body[0]}')
        out += [f'{" " * column}{line}' for line in body[1:]]
    return '\n'.join(out)


def usage() -> str:
    """`pf --help`: the shape of the command, then the words the tables above carry.

    The listing is grown from those tables, so a verb added to one shows up here by itself and the
    router and this text cannot come apart.
    """
    own = _usage_rows([(display, blurb) for _n, display, blurb, _f in SELF_VERBS])
    verbs = _usage_rows([(name, blurb) for name, _t, _k, blurb in FORWARD_VERBS])
    groups = _usage_rows([(f'{name} <verb>', blurb) for name, _t, _k, blurb in FORWARD_GROUPS])
    return (
        _USAGE_SHAPE
        + '\n  pf itself:\n' + own
        + '\n\n  verbs:    (each one hands the rest of the line to a tool)\n' + verbs
        + "\n\n  groups:   (a group's verbs are that tool's: `pf <group> --help`)\n" + groups
        + '\n\n  Run `pf <verb> --help` for the verb\'s own flags and values, or `pf <group> --help`\n'
          "  for a group's verbs.\n"
    )


def _run(*argv: str) -> int:
    """Hand over to a tool with the same interpreter and the same stdio, and keep its exit code."""
    env = dict(os.environ, PYTHONUTF8='1')
    return subprocess.call([sys.executable, '-X', 'utf8', *argv], env=env)


def _project_root(start: Path) -> Path:
    """This project's root, read the way every tool `pf` fronts reads it: `start` itself,
    except inside a linked worktree, where it is the main tree the worktree was forked from."""
    return _ledger().resolve_project_root(start)


def _ledger_name() -> str:
    """`.pf/`, named once, in the ledger contract."""
    return _ledger().LEDGER_NAME


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    """`tools/shared/gitcmd.py`: 这棵树问 git 的唯一种写法（cwd、解码、不抛都在那里）。"""
    return _loader().load_shared('gitcmd').git(project, *args)


def _is_tracked(root: Path) -> bool:
    return bool(_git(root, 'ls-files', '--', _ledger_name()).stdout.strip())


def _registered_driver(root: Path) -> str:
    return _git(root, 'config', '--get', f'merge{_ledger_name()}.driver').stdout.strip()


def cmd_doctor(fix: bool = False) -> int:
    """Read the health of the checkout and the project; with --fix, run the setup act once."""
    problems = 0
    print(f'pf 命令：{HERE / "pf.py"}')
    print(f'工具树：{TOOLS}')
    for path in TOOL_FILES:
        if not path.is_file():
            print(f'  ✗ 缺 {path}')
            problems += 1
    if sys.version_info < (3, 10):
        print(f'  ✗ Python {sys.version.split()[0]} 太旧（这套工具要 3.10+）')
        problems += 1

    root = _project_root(Path.cwd())
    ledger = _ledger().ledger_dir_for(str(root))
    print(f'项目根：{root}')
    if not ledger.is_dir():
        print(f'  · 这里还没有 {_ledger_name()}/（新项目跑 `pf init`）')
        return 1 if problems else 0

    gaps = []
    markers = _ledger().INIT_MARKERS
    missing = [m for m in markers if not (ledger / m).is_file()]
    if missing:
        gaps.append(f'{_ledger_name()}/ 缺 {" 和 ".join(missing)}')
    if not (ledger / 'AGENTS.md').is_file():
        gaps.append(f'{_ledger_name()}/AGENTS.md 不在')

    git_here = (root / '.git').exists()
    driver = _registered_driver(root) if git_here else ''
    if git_here:
        if not driver:
            gaps.append('合并驱动没登记')
        elif not driver.startswith('pf '):
            gaps.append('合并驱动登记的是绝对形式（搬动 checkout 之后要重登记）')

    for gap in gaps:
        print(f'  · {gap}')
    if not gaps:
        print('  · 记录目录、使用约定、合并驱动都在位')
    elif fix:
        print('  → 跑一次 `init` 补齐（它认得已经在的记录，重复跑没有副作用）')
        code = _run(str(STATE_MANAGE), 'init', '--project', str(root),
                    '--commit' if _is_tracked(root) else '--local')
        if code != 0:
            problems += 1
        else:
            for gap in gaps:
                print(f'  ✓ {gap} —— 已补齐')
    else:
        print('  （`pf doctor --fix` 可以补齐）')

    done = not problems and (not gaps or fix)
    print('全部体检项通过。' if done else f'{problems + len(gaps)} 项待办。')
    return 0 if done else 1


def main(argv: list[str]) -> int:
    # doctor 的读数是给人看的中文：按 UTF-8 出去，别吃控制台代码页（这台机器 GBK）那一套。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(usage())
        return 0
    verb, rest = argv[0], argv[1:]

    own = _SELF_HANDLERS.get(verb)
    if own is not None:
        return own(rest)
    for name, target, keep_name, _blurb in (*FORWARD_VERBS, *FORWARD_GROUPS):
        if name == verb:
            return _run(str(target), *((verb, *rest) if keep_name else rest))

    print(f'pf: 不认识 `{verb}`。\n', file=sys.stderr)
    print(usage(), file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))