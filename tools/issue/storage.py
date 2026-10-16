"""Data access layer: paths, constants, low-level IO for the issue manager.

The pool lives with everything else this project's pipeline records, under `.pf/issues/`, so it
survives a wiped `temp/` and travels wherever the project's work travels. A project either commits
its `.pf/` or keeps that directory to itself; either way the pool is read and written the same.

`LEDGER_NAME` / `INIT_MARKERS` / `_require_initialized` are copies of conduct's and dag-manage's.
"""

import contextlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

#: `.pf/` 的名字，与 conduct、dag-manage 那两份是同一个词。
LEDGER_NAME = '.pf'
#: init 写下的两枚文件：没有它们就是没 init 过。
INIT_MARKERS = ('.gitignore', '.gitattributes')

try:
    import fcntl
except ImportError:   # Windows; the lock's POSIX branch degrades to no-op
    fcntl = None
import msvcrt

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
#: `optimize` 是池子里的 source 取值：几百条历史条目都写着它。扫描那一支改叫 pf-issue-scan 之后
#: 这个字面量照旧——改它会把 `--source optimize` 的筛选和历史条目一起废掉。
#: `conduct` 是第三种来路：干活的会话撞见一条与本节点无关的债，顺手记进来（不经过扫描）。
VALID_SOURCES = {'optimize', 'manual', 'conduct'}
#: 严重度 → 权重：池子「有多重」就是它加出来的。一条 P0 抵八条 P3。
#: 这套换算原先只活在散文里（会话拿着 `status` 的计数在脑子里乘），于是没人复核得了它；
#: 现在它是这一处的一行代码，`weight` 命令读它。
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
    if not ISSUES_JSON.exists():
        return {'version': 1, 'issues': {}}
    try:
        with ISSUES_JSON.open('r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f'Error: issues.json parse failure: {e}', file=sys.stderr)
        sys.exit(1)


def _write(data: dict) -> None:
    _ensure_dirs()
    # 临时文件跟目标同一个目录：一次原子替换，跨目录那种半路崩掉会留下半个文件。
    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, str(ISSUES_JSON))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── the cross-process ledger lock ─────────────────────────────────────────────
# issues.json is whole-file read-modify-write; two processes writing at once means the later
# write erases the earlier entry. Every mutating command holds the project's one lock across its
# read → write -- the same `.pf/meta/ledger.lock` file conduct and dag-manage use, so the three
# tools' writes exclude each other. `meta/` is ignored by `.pf/.gitignore`, so the lock never
# shows up in anybody's history.
_LEDGER_LOCK_TIMEOUT = 10.0
_LEDGER_LOCK_DEPTH = 0


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
def _ledger_lock(timeout: float = _LEDGER_LOCK_TIMEOUT):
    global _LEDGER_LOCK_DEPTH
    if _LEDGER_LOCK_DEPTH:
        _LEDGER_LOCK_DEPTH += 1
        try:
            yield
        finally:
            _LEDGER_LOCK_DEPTH -= 1
        return
    _require_initialized()
    path = DATA_DIR.parent / 'meta' / 'ledger.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, 'a+')
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if os.name == 'nt' and msvcrt is not None:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    print(f'Error: the ledger lock {path} stayed busy for {timeout:g}s; '
                          f'another process is mid-write. Nothing was changed.', file=sys.stderr)
                    sys.exit(1)
                time.sleep(0.05)
        _LEDGER_LOCK_DEPTH = 1
        try:
            yield
        finally:
            _LEDGER_LOCK_DEPTH = 0
            try:
                if os.name == 'nt' and msvcrt is not None:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        handle.close()


def _replace_with_retry(tmp: str, path: str, attempts: int = 4) -> None:
    """`os.replace`, tolerating a reader that holds the target open this instant (Windows)."""
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def _get(uid: str, data: dict) -> dict:
    node = data['issues'].get(uid)
    if node is not None:
        return node
    print(f'Error: issue "{uid}" not found', file=sys.stderr)
    sys.exit(1)
