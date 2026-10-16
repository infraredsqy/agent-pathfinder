#!/usr/bin/env python3
"""`pf`: one command in front of the tools.

Every skill used to spell out a full `python .../state-manage.py ...` path, which tied the prose to
one machine's install layout. They now say `pf next`, `pf handoff`, `pf issue top` --
where the tools live is resolved here, once, from this file's own location.

Three verbs belong to `pf` itself:

    pf doctor [--fix]   read the health of this checkout and this project; --fix runs the setup act
    pf version          print where this checkout lives
    pf migrate [...]    bring a project's ledger over from `.nav/` (see migrate-ledger.py)

Everything else forwards to a tool, keeping the tool's own subcommand names so the docs stay
greppable. `pf init` and `pf register` are forwards too: the registration logic lives next to the
ledger it serves, in `conduct_state/initledger.py`.
"""

import os
import subprocess
import sys
from pathlib import Path

#: The tools this command fronts. `HERE` is `<repo>/tools/bin`, so the tool tree is one level up --
#: the checkout may sit anywhere: nothing below is absolute.
HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
CONDUCT = TOOLS / 'conduct'
PIPELINE = CONDUCT / 'pipeline'
DAG = TOOLS / 'dag' / 'dag-manage.py'
ISSUE = TOOLS / 'issue' / 'issue_manager.py'
STATE_MANAGE = PIPELINE / 'state-manage.py'
HANDOFF = PIPELINE / 'handoff.py'
CONDUCT_PIPELINE = PIPELINE / 'conduct_pipeline.py'
LEDGER_MERGE = CONDUCT / 'ledger-merge.py'
MIGRATE = CONDUCT / 'migrate-ledger.py'
#: The one file that belongs to no single tool: the session notes both `dag-manage.py` and the
#: pipeline load by path (`tools/session-note.py`). A checkout missing it still runs -- the notes
#: are plumbing, and every writer treats a failure to note one as best effort -- which is exactly
#: why a health check has to name it.
SESSION_NOTE = TOOLS / 'session-note.py'

LEDGER_NAME = '.pf'
MARKERS = ('.gitignore', '.gitattributes')

#: Everything that lives in `state-manage.py`, forwarded verbatim: each name is either a bare verb
#: or a noun group carrying its own verbs, so `pf cycle list` reaches the tool as `cycle list`.
STATE_ENTRIES = ('init', 'register', 'cycle', 'instruction', 'config', 'session', 'next',
                 'dispatch', 'clean-temp', 'fork-worktree')

#: Groups whose remaining argv goes straight to one tool.
GROUPS = {'dag': DAG, 'issue': ISSUE}

USAGE = """\
pf -- the agent pipeline's own command

  Level 2 is a verb: it acts on the project or on the pipeline itself. Level 3 is a noun -- a record
  with an identity -- plus the verb that acts on it. So `pf init` and `pf next` are verbs, and
  `pf cycle list`, `pf issue top`, `pf dag add` are a group's verbs.

  verbs:    init                        put .pf/ in place (run once per project)
            register                    write the ledger's git merge driver into this project
            doctor [--fix]              report the health of this checkout and this project
            migrate [--dry-run]         bring a project's ledger over from .nav/
            version                     print where this checkout lives
            next [--plan|--after <node>]
                                        show (or do) the next pipeline action: `--plan` is the
                                        read-only preview, `--after <node>` is the worker's own
                                        closing call, and neither drives the pipeline to its next
                                        stable action. `pf next --help` prints the action
                                        vocabulary a caller branches on
            dispatch <cycle>            start the session for the node the cycle holds
            handoff <cycle> <node> <completed|failed|stopped> [-s ...] [-o ...]
            clean-temp [--yes]          list (or remove) every node's temp/ directory
            fork-worktree <node>        fork one node's parallel-mode worktree
            merge-ledger <O> <A> <B> <P>   the three-way merge driver git calls

  groups:   cycle <verb>                one line of work: create / start / rename / status /
                                        policy / list / current / show / hold / close / retry
            dag <verb>                  the graph: add / depend / list / status / open / revive ...
            issue <verb>                the issue pool: add / top / weight / resolve ...
            config <verb>               this machine's and this project's settings:
                                        list / get / set / unset
            instruction <verb>          standing user instructions: list / add / resolve
            session <verb>              a node's own conversations, which live on this machine only:
                                        show / list / import

Run `pf <verb> --help` for a verb's own flags, or `pf <group> --help` for a group's verbs.
"""


def _run(*argv: str) -> int:
    """Hand over to a tool with the same interpreter and the same stdio, and keep its exit code."""
    env = dict(os.environ, PYTHONUTF8='1')
    return subprocess.call([sys.executable, '-X', 'utf8', *argv], env=env)


def _project_root(start: Path) -> Path:
    """The nearest ancestor holding a `.git`; the start directory when there is none."""
    current = start.resolve()
    for _ in range(24):
        if (current / '.git').is_dir():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return start.resolve()


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['git', *args], cwd=str(project), capture_output=True, text=True,
                          encoding='utf-8', errors='replace')


def _is_tracked(root: Path) -> bool:
    return bool(_git(root, 'ls-files', '--', LEDGER_NAME).stdout.strip())


def _registered_driver(root: Path) -> str:
    return _git(root, 'config', '--get', f'merge{LEDGER_NAME}.driver').stdout.strip()


def cmd_doctor(fix: bool = False) -> int:
    """Read the health of the checkout and the project; with --fix, run the setup act once."""
    problems = 0
    print(f'pf 命令：{HERE / "pf.py"}')
    print(f'工具树：{TOOLS}')
    for path in (STATE_MANAGE, HANDOFF, CONDUCT_PIPELINE, DAG, ISSUE, LEDGER_MERGE, SESSION_NOTE):
        if not path.is_file():
            print(f'  ✗ 缺 {path}')
            problems += 1
    if sys.version_info < (3, 10):
        print(f'  ✗ Python {sys.version.split()[0]} 太旧（这套工具要 3.10+）')
        problems += 1

    root = _project_root(Path.cwd())
    ledger = root / LEDGER_NAME
    print(f'项目根：{root}')
    if not ledger.is_dir():
        print(f'  · 这里还没有 {LEDGER_NAME}/（新项目跑 `pf init`；旧记录跑 `pf migrate`）')
        return 1 if problems else 0

    gaps = []
    missing = [m for m in MARKERS if not (ledger / m).is_file()]
    if missing:
        gaps.append(f'{LEDGER_NAME}/ 缺 {" 和 ".join(missing)}')
    if not (ledger / 'AGENTS.md').is_file():
        gaps.append(f'{LEDGER_NAME}/AGENTS.md 不在')

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
        print(USAGE)
        return 0
    verb, rest = argv[0], argv[1:]

    if verb == 'version':
        print(f'pf at {HERE} (tools: {TOOLS})')
        return 0
    if verb == 'doctor':
        return cmd_doctor(fix='--fix' in rest)
    if verb == 'migrate':
        return _run(str(MIGRATE), *rest)
    if verb == 'merge-ledger':
        return _run(str(LEDGER_MERGE), *rest)
    if verb == 'handoff':
        return _run(str(HANDOFF), *rest)
    if verb in GROUPS:
        return _run(str(GROUPS[verb]), *rest)
    if verb in STATE_ENTRIES:
        return _run(str(STATE_MANAGE), verb, *rest)

    print(f'pf: 不认识 `{verb}`。\n', file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))