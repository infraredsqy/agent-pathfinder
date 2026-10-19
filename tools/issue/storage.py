"""Data access layer: paths, constants, low-level IO for the issue manager.

The pool lives with everything else this project's pipeline records, under `.pf/issues/`, so it
survives a wiped `temp/` and travels wherever the project's work travels. A project either commits
its `.pf/` or keeps that directory to itself; either way the pool is read and written the same.

The read, the write and the lock are the shared ones (`tools/shared/`), so this file's records are
read and written exactly like the conduct side's. `.pf/` itself, the worktree judgement and the init
gate come from `tools/shared/ledger.py`, the one place they are spelled.
"""

import contextlib
import importlib.util
import re
import sys
from pathlib import Path

# Bootstrap: the loader is fetched by path too, so every file has to carry this paragraph itself
# (see `shared/loader.py`).
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


def _ledger():
    """`tools/shared/ledger.py`: where `.pf/` lands, the worktree-to-main-tree rule, the init gate
    -- one source."""
    return _loader().load_shared('ledger')

# ═══════════════════════════════════════════════════════════════════════════════
#  Paths & Constants
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_project_root(cwd: Path) -> Path:
    """cwd is the root; the one exception: when cwd is a linked worktree's checkout root, go back to
    the main tree (the rule lives in the ledger)."""
    return _ledger().resolve_project_root(cwd)


#: Which project this module points at right now: `configure()` fixes it once, and the CLI resolves
#: it from cwd once in `main()`. Importing this file binds no project -- the caller says which one.
DATA_DIR = None
ISSUES_JSON = None


def configure(project_root) -> None:
    """Pin the ledger to one project: `<root>/.pf/issues/`, with its `issues.json` under it.

    This is the door paths enter this module through: the CLI calls it once from cwd inside `main()`,
    and in-process callers name the project themselves.
    """
    global DATA_DIR, ISSUES_JSON
    DATA_DIR = _ledger().ledger_dir_for(project_root) / 'issues'
    ISSUES_JSON = DATA_DIR / 'issues.json'


def _require_configured() -> None:
    """Say it plainly when nothing was named: which project's pool to read is the caller's word."""
    if ISSUES_JSON is None:
        print('Error: this module points at no project yet; call configure(project root) first.',
              file=sys.stderr)
        sys.exit(1)

VALID_STATUSES = {'open', 'acknowledged', 'resolving', 'resolved', 'rejected'}
VALID_SEVERITIES = {'P0', 'P1', 'P2', 'P3'}
VALID_DECISIONS = {'autofix', 'external', 'unknown'}
#: `scan` is the scanning entry into the pool; `conduct` is a working session that ran into a debt
#: unrelated to its own node and filed it in passing.
VALID_SOURCES = {'scan', 'manual', 'conduct'}
#: Severity -> weight: the pool's "how heavy" is nothing but this sum. One P0 is worth eight P3s.
#: The `weight` command reads it.
SEVERITY_WEIGHTS = {'P0': 8, 'P1': 4, 'P2': 2, 'P3': 1}


# ═══════════════════════════════════════════════════════════════════════════════
#  Low-level helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _ensure_plan_dir(plan_path: str) -> None:
    Path(plan_path).parent.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    """`tools/shared/clock.py`: the moment a record is written with, in the tree's one shape."""
    return _loader().load_shared('clock').now()


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
    jsonio = _loader().load_shared('jsonio')
    _require_configured()
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
    _require_configured()
    _ensure_dirs()
    _loader().load_shared('jsonio').atomic_write_json(ISSUES_JSON, data, sort_keys=True)


# ── the cross-process ledger lock ─────────────────────────────────────────────
# issues.json is whole-file read-modify-write; two processes writing at once means the later write
# erases the earlier entry. Every mutating command holds the project's one lock across its read →
# write -- the same `.pf/meta/ledger.lock` file conduct, dag-manage and a session note's own writer
# use, so all of their writes exclude each other.


def _require_initialized() -> None:
    """A `.pf/` nobody init'ed may hold no write at all: the lock's `mkdir` is the one thing that
    would conjure it.

    It reads `DATA_DIR.parent` (that is, `.pf/`), and `configure()` named `DATA_DIR`; the judgement
    and the one sentence a refused write is told live in `tools/shared/ledger.py`.
    """
    ledger = DATA_DIR.parent
    if _ledger().is_initialized(ledger):
        return
    print(f'Error: {_ledger().init_message(ledger)}', file=sys.stderr)
    sys.exit(1)


@contextlib.contextmanager
def _ledger_lock(timeout: float | None = None):
    """Hold the project's ledger lock for the duration of one read-modify-write.

    The lock itself is `tools/shared/ledgerlock.py`. A caller that cannot get it within `timeout` is
    told loudly and exits 1 -- wedging silently is the one failure this must not have.
    """
    lock = _loader().load_shared('ledgerlock')
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
