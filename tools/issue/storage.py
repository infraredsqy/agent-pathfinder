"""Data access layer: paths, constants, low-level IO for the issue manager.

The pool lives with everything else this project's pipeline records, under `.pf/issues/`, so it
survives a wiped `temp/` and travels wherever the project's work travels. A project either commits
its `.pf/` or keeps that directory to itself; either way the pool is read and written the same.

The read, the write and the lock are the shared ones (`tools/shared/`), so this file's records are
read and written exactly like the conduct side's. `LEDGER_NAME` / `INIT_MARKERS` /
`_require_initialized` are copies of conduct's and dag-manage's.
"""

import contextlib
import importlib.util
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

#: `.pf/` 的名字，与 conduct、dag-manage 那两份是同一个词。
LEDGER_NAME = '.pf'
#: init 写下的两枚文件：没有它们就是没 init 过。
INIT_MARKERS = ('.gitignore', '.gitattributes')

# `tools/shared/` is the shared layer, and it has no package to import from: every file in it is
# loaded by path through `loader.py`. These two functions are that bootstrap, identical in every file
# that needs it -- a path load, never a name import, because nothing puts `tools/` on `sys.path`.
SHARED = Path(__file__).resolve().parent.parent / 'shared'

_LOADER = None


def _loader():
    """`tools/shared/loader.py`: the one loader, for the modules this tree keeps outside `shared/`."""
    global _LOADER
    if _LOADER is None:
        spec = importlib.util.spec_from_file_location('pf_shared_loader', SHARED / 'loader.py')
        _LOADER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_LOADER)
    return _LOADER


def _shared(name, filename):
    """One file out of `tools/shared/`, the same module for every caller in this process.

    Loaded for use, not copied: the ledger lock's reentrancy record is one per process, and a second
    copy of that module would give two of them.
    """
    return _loader().load_by_path(name, SHARED / filename, register=True, reuse=True)

# ═══════════════════════════════════════════════════════════════════════════════
#  Paths & Constants
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_project_root(cwd: Path) -> Path:
    """cwd 即根，唯一的例外：cwd 是一条 linked worktree 的检出根时，回主树。

    与 `conduct_state.store._resolve_project_root`、dag-manage 侧同名函数是同一份约定摆在
    三处（三个工具没有共同的包），判定一致：worktree 根的 `.git` 是文件，指向
    `<主树>/.git/worktrees/<名字>`，`worktrees` 上三级即主树根；`.git/modules/...` 是
    submodule，不算。主树没有 `.pf/` 时不挪，让老错误照老样子响。
    """
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


#: cwd 在导入那一刻定死（worktree 里则回主树），和 conduct 那边的规矩相同：从项目根跑。
PROJECT_ROOT = _resolve_project_root(Path.cwd())
DATA_DIR = PROJECT_ROOT / LEDGER_NAME / 'issues'
ISSUES_JSON = DATA_DIR / 'issues.json'

VALID_STATUSES = {'open', 'acknowledged', 'resolving', 'resolved', 'rejected'}
VALID_SEVERITIES = {'P0', 'P1', 'P2', 'P3'}
VALID_DECISIONS = {'autofix', 'external', 'unknown'}
#: `scan` 是扫描入池的那一支；`conduct` 是干活的会话撞见一条与本节点无关的债，顺手记进来。
VALID_SOURCES = {'scan', 'manual', 'conduct'}
#: 严重度 → 权重：池子「有多重」就是它加出来的。一条 P0 抵八条 P3。`weight` 命令读它。
SEVERITY_WEIGHTS = {'P0': 8, 'P1': 4, 'P2': 2, 'P3': 1}
ISO_FMT = '%Y-%m-%dT%H:%M:%S.%fZ'


# ═══════════════════════════════════════════════════════════════════════════════
#  Low-level helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _ensure_plan_dir(plan_path: str) -> None:
    Path(plan_path).parent.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).strftime(ISO_FMT)


def _auto_id(item: dict) -> str:
    """Generate a kebab-case ID from module and title."""
    mod = (item.get('module', 'unknown') or 'unknown').replace('/', '-')
    title = (item.get('title', 'untitled') or 'untitled').lower()
    slug = re.sub(r'[^a-z0-9]+', '-', title).strip('-')[:40]
    return f'{mod}-{slug}' if slug else f'{mod}-untitled'


def _read() -> dict:
    """The pool, or an empty one. Bytes that are not UTF-8 and text that is not JSON are refused.

    The read, and the words each failure gets, are `tools/shared/jsonio.py`'s -- the same reader the
    conduct side and dag-manage use, so one pool is read the same way by all three. This layer is the
    CLI: a record that cannot be read ends the command with that message.
    """
    jsonio = _shared('pf_shared_jsonio', 'jsonio.py')
    try:
        data = jsonio.read_json_strict(ISSUES_JSON, 'issues.json')
    except jsonio.JsonReadError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)
    return {'version': 1, 'issues': {}} if data is None else data


def _write(data: dict) -> None:
    """Write the pool whole: temp file, fsync, a replace that tolerates a live reader.

    The write is `tools/shared/jsonio.py`'s, the one every other record in this tree goes through;
    `sort_keys` is this file's own habit, so a rewritten pool reads the same way twice.
    """
    _ensure_dirs()
    _shared('pf_shared_jsonio', 'jsonio.py').atomic_write_json(ISSUES_JSON, data, sort_keys=True)


# ── the cross-process ledger lock ─────────────────────────────────────────────
# issues.json is whole-file read-modify-write; two processes writing at once means the later write
# erases the earlier entry. Every mutating command holds the project's one lock across its read →
# write -- the same `.pf/meta/ledger.lock` file conduct, dag-manage and a session note's own writer
# use, so all of their writes exclude each other.


def _require_initialized() -> None:
    """没有 init 过的 `.pf/` 什么都不许写：锁的 `mkdir` 是唯一会把它造出来的东西。

    读 `DATA_DIR.parent`（也就是 `.pf/`）而不是 `PROJECT_ROOT`：路径可以被调用方重绑到另一个
    项目上，根却冻在导入那一刻。
    """
    ledger = DATA_DIR.parent
    if ledger.is_dir() and all((ledger / m).is_file() for m in INIT_MARKERS):
        return
    print(f'Error: {ledger} 没 init 过（缺 {" 或 ".join(INIT_MARKERS)}）。先跑：\n'
          f'  pf init', file=sys.stderr)
    sys.exit(1)


@contextlib.contextmanager
def _ledger_lock(timeout: float | None = None):
    """Hold the project's ledger lock for the duration of one read-modify-write.

    The lock itself is `tools/shared/ledgerlock.py`. A caller that cannot get it within `timeout` is
    told loudly and exits 1 -- wedging silently is the one failure this must not have.
    """
    lock = _shared('pf_shared_ledgerlock', 'ledgerlock.py')
    kwargs = {} if timeout is None else {'timeout': timeout}
    try:
        with lock.ledger_lock(DATA_DIR.parent, gate=_require_initialized, **kwargs):
            yield
    except TimeoutError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


def _get(uid: str, data: dict) -> dict:
    node = data['issues'].get(uid)
    if node is not None:
        return node
    print(f'Error: issue "{uid}" not found', file=sys.stderr)
    sys.exit(1)
