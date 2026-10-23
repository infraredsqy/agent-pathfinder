"""One root -- `<project>/.pf/` -- and the four kinds of thing that live under it.

The command's durable account is the LEDGER, `<project>/.pf/`: the cycle book (`state.json`), the
signals (`signals/`), the standing instructions (`instructions.json`), the DAG (`ideas.json`) and
each node's own text (`nodes/<id>/`). A project either commits it or keeps it local, and which of
those it is never changes what this module computes -- only whether git tracks `.pf/`.

Everything else under `.pf/` is plumbing, and the ledger's own `.gitignore` ignores it:

    meta/                 the dispatch plumbing -- launch prompts, the FORK record, the
                          broken-chain marker, and the cross-process ledger lock
    worktrees/<short-id>    one linked worktree per parallel node
    nodes/<id>/temp/      what a node generates: probes, raw output, snapshots

None of those is the account: a launch prompt is a message that has already been delivered, the
marker is a "this chain broke" flag, and `temp/` is throwaway by definition.

Holds the path bindings (`bind_paths` re-points them all at once), the door to the cross-process
ledger lock every mutating command holds across its read-modify-write, and the state/signal
accessors built on the shared primitives -- the JSON reader and atomic writer, and the lock itself,
both in `tools/shared/`.

`require_initialized` is the gate: a `.pf/` this tool created by itself -- never init'ed -- is the
one state that must not be reachable, because the lock's own `mkdir` would otherwise leave an
unignored ledger in the tree for the next `git add -A` to sweep up.

A cycle's record in `state.json` is the whole of it -- identity, current node, and the path of nodes
it has finished.
"""


import contextlib
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent   # the conduct tool directory

# Bootstrap: the loader is fetched by path too, so every file has to carry this paragraph itself
# (see `shared/loader.py`).
SHARED = HERE.parent / 'shared'

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
    """`tools/shared/ledger.py`: where `.pf/` lands and the init gate -- one source."""
    return _loader().load_shared('ledger')

#: These names have their one source in `tools/shared/ledger.py` and stay reachable in this
#: namespace (`fork.py` / `initledger.py` fetch them with `from .store import ...`, and the
#: package's `__all__` still collects them). Fetched, not copied: there is no second value here,
#: and so no second spelling that has to be kept in step with the source.
_FROM_LEDGER = {'LEDGER_NAME': 'LEDGER_NAME', 'INIT_MARKERS': 'INIT_MARKERS',
                '_resolve_project_root': 'resolve_project_root'}


def __getattr__(name: str):
    """Those few names in this module: fetched from the source in `shared/ledger.py`
    (see `_FROM_LEDGER`)."""
    if name in _FROM_LEDGER:
        return getattr(_ledger(), _FROM_LEDGER[name])
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')

#: One root: `.pf/`. `meta/`, `worktrees/` and `nodes/<id>/temp/` are ignored subdirectories under
#: it, each computed by the `*_dir_for()` below, and never again by assembling a path under some
#: other root's `.pf/`. cwd is frozen at import time (in a worktree it goes back to the main tree,
#: see `resolve_project_root` in `shared/ledger.py`). A command with `--project` goes around it,
#: see `ledger_dir_for()`.
PROJECT_ROOT = _ledger().resolve_project_root(Path.cwd())
LEDGER_DIR = _ledger().ledger_dir_for(PROJECT_ROOT)


def _explicit_project(project):
    """The one project root the caller named: `~` expanded, made absolute -- `--project` is the
    string a human typed.

    `None` does not come this way: `ledger_dir_for(None)` wants the root this copy was bound to at
    import time (see below).
    """
    return Path(project).expanduser().resolve()


def ledger_dir_for(project: str | None = None) -> Path:
    """Where the ledger (`.pf/`) lives.

    `project is None` keeps every pre-existing caller on the import-time cwd resolution. An explicit
    `--project` matters because `LEDGER_DIR` freezes `Path.cwd()` at import: a caller whose cwd is
    not the project root (a subagent that cd'd away, a launcher that started somewhere else) would
    otherwise read a *different* project's `.pf/` -- and an empty one reads as "no instructions",
    silently, with exit 0. A constraint that looks absent is worse than a loud error.
    """
    if project is None:
        return LEDGER_DIR
    return _ledger().ledger_dir_for(_explicit_project(project))


def meta_dir_for(project: str | None = None) -> Path:
    """Where the dispatch plumbing lives (`<project>/.pf/meta/`): launch prompts, the FORK record,
    the broken-chain marker, the ledger lock. The same rule as `ledger_dir_for`, one level down."""
    return _ledger().meta_dir_of(ledger_dir_for(project))


def nodes_dir_for(project: str | None = None) -> Path:
    """Where each node's own directory lives (`<project>/.pf/nodes/`): its spec, its report group
    and the `temp/` it generates. The same rule as `ledger_dir_for`, one level down."""
    return _ledger().nodes_dir_of(ledger_dir_for(project))


def require_initialized(project: str | None = None) -> None:
    """This `.pf/` must already have been init-ed, or nothing may be written.

    No other command creates `.pf/`; the only thing that would create it in passing is the lock's
    `mkdir` -- and that is exactly what this blocks: a `.pf/` that is neither tracked nor ignored
    gets swept into the team repo by the next `git add -A`. The rule and that sentence live in
    `tools/shared/ledger.py` (pure filesystem, never asks git); when `project` was named, the
    sample command line in it carries that name.
    """
    ledger = ledger_dir_for(project)
    if _ledger().is_initialized(ledger):
        return
    print(f'Error: {_ledger().init_message(ledger, project=project)}', file=sys.stderr)
    sys.exit(1)


# ── the cross-process ledger lock ─────────────────────────────────────────────
# state.json / ideas.json / instructions.json / issues.json are whole-file read-modify-write:
# two processes walking that sequence at once means the later write silently erases the earlier
# one. With parallel=2 (two cycles settling at the same time) that window stops being theoretical,
# so every MUTATING command body holds this lock across its read → write. One path per project
# root -- `.pf/meta/ledger.lock` -- shared by conduct, dag-manage, the issue tool and a session
# note's own writer, so their writes exclude each other too.
#
# This is also the gate: every mutating command passes through here, and the lock's `mkdir` would
# otherwise be the one thing that conjures a `.pf/` nobody init'ed.


@contextlib.contextmanager
def _ledger_lock(project: str | None = None, timeout: float | None = None):
    """Hold the project's ledger lock for the duration of one read-modify-write.

    The lock itself is `tools/shared/ledgerlock.py` -- one implementation for every writer of a
    record here. A caller that cannot get it within `timeout` is told loudly and exits 1: wedging
    silently is the one failure this must not have.
    """
    lock = _loader().load_shared('ledgerlock')
    kwargs = {} if timeout is None else {'timeout': timeout}
    try:
        with lock.ledger_lock(ledger_dir_for(project), gate=lambda: require_initialized(project),
                              **kwargs):
            yield
    except TimeoutError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


#: How a path step may end. A node's own work is not tracked here, only the outcome it reported.
VALID_NODE_RESULTS = {'completed', 'failed', 'stopped'}
#: A cycle has only two statuses: `active`, the line should move forward, and `inactive`, it should
#: not. Why it stopped is not kept in the status -- the reason at the moment it stopped is that
#: sentence, and whether a line should start work again is a human's call.
CYCLE_VALID_STATUSES = {'active', 'inactive'}
#: How many nodes one line may hold at once -- the `parallel` policy's default: no parallelism is 1.
PARALLEL_DEFAULT = 1
#: The moment shape state.json / a note / the standing instructions / the FORK record use: seconds.
#: Its one source is `ISO_FMT_SECONDS` in `tools/shared/clock.py`; `ISO_FMT` is the name this file
#: and the cases that read it have always used, and it still means this one shape.
ISO_FMT = _loader().load_shared('clock').ISO_FMT_SECONDS


def _now() -> str:
    """The moment for this record: the seconds shape from `tools/shared/clock.py` (the microsecond
    one is the graph's and the pool's)."""
    return _loader().load_shared('clock').now_seconds()


def _atomic_write(path: Path, data: dict) -> None:
    """One record written whole, or the command ends.

    The write itself (temp file in the target's directory, fsync, a replace that tolerates a live
    reader) is `tools/shared/jsonio.py`; this layer is the CLI, so a failure is a message and exit 1.
    """
    try:
        _loader().load_shared('jsonio').atomic_write_json(path, data)
    except Exception as e:
        print(f'Error: write failed: {e}', file=sys.stderr)
        sys.exit(1)


def record_bytes(data: dict) -> bytes:
    """The bytes these four records land as: the writer gives the format, and the merge driver
    compares against it.

    `state.json` / `instructions.json` and a note all land through `_atomic_write`, with the format
    in `tools/shared/jsonio.py` (indent 2, non-ASCII passed through as it is, `sort_keys` left at
    its default). The merge driver compares this byte string against the one on disk: identical,
    and that file is left exactly as it is, not a byte touched. Only the writer can give the rule
    -- if the driver did its own `json.dumps`, then the day this side changed format every later
    merge would look like a change.
    """
    return _loader().load_shared('jsonio').json_bytes(data)


def write_record_bytes(path, payload: bytes) -> None:
    """Land that byte string at this path: the rule and the landing come from the same tree.

    `record_bytes` gives "what this project should be written as", and this writes it down, both
    using this tree's `tools/shared/jsonio.py`. The merge driver asks the same writer through both
    doors: when a draft or drill copy points `DSH_CONDUCT_STATE_DIR` at the real tool, it will not
    judge by one tree's rule and land by another's. Who holds the lock and how it is wrapped up
    afterwards is the caller's business; this is only the landing.
    """
    _loader().load_shared('jsonio').atomic_write_bytes(path, payload)


def _read_json(path: Path, label: str) -> dict | None:
    """One record read strict, or the command ends with the reader's own words.

    The reader and the wording of each failure are `tools/shared/jsonio.py`: a file that is not
    there reads as None, bytes that are not UTF-8 and text that is not JSON both raise
    `JsonReadError`. **The CLI entry does the exiting; inside the package it only raises**: this is
    that door -- it prints the reader's own sentence and exits 1; a caller that calls
    `read_json_strict` in process gets the exception and decides for itself what to do.
    """
    jsonio = _loader().load_shared('jsonio')
    try:
        return jsonio.read_json_strict(path, label)
    except jsonio.JsonReadError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


# ── The four records: one door ───────────────────────────────────────────────
# The four things under `.pf/` are the whole of this tool's records -- the cycle book
# (state.json), the notes (signals/), the standing instruction list (instructions.json) and the
# graph (ideas.json). This section is their **only** path algorithm: every door takes a `project`,
# so reading another project's records never means assembling a path by hand, and no second
# spelling of a failure can be assembled either. When one record has two readers, one shouting
# when it reads wrong and the other silently treating it as empty -- and that empty reading, on
# `clean-temp --yes`, means deleting it all the same.

def _record_path(project: str | None, *parts: str) -> Path:
    """`<project>/.pf/<parts...>`: the record's landing place meets the ledger root only here.

    With `project` None it uses the root this copy was bound to at import time (moved by
    `bind_paths`).
    """
    return ledger_dir_for(project).joinpath(*parts)


# ── State.json ───────────────────────────────────────────────────────────────

def _read_state(project: str | None = None) -> dict:
    """This project's cycle book, or an empty one (`state.json` being absent is normal)."""
    data = _read_json(_record_path(project, 'state.json'), 'state.json')
    if data is None:
        return {'cycles': []}
    if 'cycles' not in data:
        data['cycles'] = []
    return data


def _read_dag(project: str | None = None) -> dict:
    """This project's graph: `ideas.json`'s node table, or an empty table.

    It reads the word on disk and fills in no field (`pipeline/store.py`'s `load_dag` is the copy
    meant for the rules, and it fills in a node's mode).
    """
    data = _read_json(_record_path(project, 'ideas.json'), 'ideas.json') or {}
    return data.get('nodes') or {}


#: `dag-manage.py` -- the graph's own writer. This tool reads `ideas.json` itself (`_read_dag`) and
#: writes it never, with ONE exception: the approval a human's "continue" leaves on the node a
#: checkpoint gate stopped on. That write goes through the graph's own command, so `ideas.json` still
#: has exactly one writer. Where that file lives is `tools/shared/toolpath.py`'s rule, the same one
#: the pipeline and the merge driver resolve their own copy with.
DAG_MANAGE = _loader().load_shared('toolpath').tool_path(
    'DSH_CONDUCT_DAG_MANAGE', HERE.parent / 'dag' / 'dag-manage.py')

_DAG_MANAGE = None


def dag_manage():
    """`dag-manage.py` loaded by path: the door the checkpoint gate's approval is written through.

    One private copy per process of THIS package -- never registered under a name another loader asks
    for, because the graph's tool points at one project at a time and `approve_node` re-points it on
    every call: two throwaway projects in one process cost a load each time this package is loaded
    fresh, and nothing of the first project's binding survives into the second.
    """
    global _DAG_MANAGE
    if _DAG_MANAGE is None:
        if not DAG_MANAGE.exists():
            raise FileNotFoundError(f'dag-manage.py not found at {DAG_MANAGE}')
        _DAG_MANAGE = _loader().load_by_path('pf_state_dag_manage', DAG_MANAGE)
    return _DAG_MANAGE


def _instructions_path(project: str | None = None) -> Path:
    """Where the standing instruction list lands: the list is part of the ledger, so it is
    `<project>/.pf/instructions.json`."""
    return _record_path(project, 'instructions.json')


def _read_instructions(project: str | None = None) -> dict:
    """This list, read out exactly as it is on disk.

    Reading neither parses nor writes: an entry's scope is read by `entry_scope` (in
    `conduct_pipeline`, the one place that rule lives) where it is really needed.
    """
    data = _read_json(_instructions_path(project), 'instructions.json')
    if not data:
        return {'instructions': []}
    data.setdefault('instructions', [])
    return data


def _write_instructions(data: dict, project: str | None = None) -> None:
    _atomic_write(_instructions_path(project), data)


def _holding_of(cycle: dict) -> list:
    """The set of nodes this line is holding right now, canonical form: `[{'id':..., 'since':...},
    ...]` (may be empty).

    This is a line's **claim mark**: one node may appear in only one line's list.
    """
    holding = cycle.get('holding')
    if isinstance(holding, list):
        out = []
        for item in holding:
            if isinstance(item, dict) and item.get('id'):
                out.append({'id': item['id'], 'since': item.get('since')})
            elif isinstance(item, str) and item:
                out.append({'id': item, 'since': None})
        return out
    return []


def _holding_ids(cycle: dict) -> list:
    """The ids of the held nodes, ordered by claim time."""
    return [item['id'] for item in _holding_of(cycle)]


def _set_holding(cycle: dict, items: list) -> None:
    """Write the canonical form back onto this line: write `holding`."""
    cycle['holding'] = [{'id': item['id'], 'since': item.get('since')} for item in items]


def _write_state(data: dict) -> None:
    _atomic_write(_record_path(None, 'state.json'), data)


def _find_cycle(state: dict, cycle_id: str) -> dict:
    for c in state['cycles']:
        if c['id'] == cycle_id:
            return c
    print(f'Error: cycle "{cycle_id}" not found in state.json', file=sys.stderr)
    sys.exit(1)


# ── Signals ──────────────────────────────────────────────────────────────────

def _signal_path(cycle_id: str, node_id: str, project: str | None = None) -> Path:
    """Where a note lands: `<project>/.pf/signals/<cycle>/<node>.json` (`project` defaults to the
    root this copy is bound to)."""
    return _signal_path_in(_record_path(project, 'signals'), cycle_id, node_id)


def _signal_path_in(signals_dir, cycle_id: str, node_id: str) -> Path:
    """The same landing place, with the root handed in directly by the caller -- the writer of an
    explicit path comes this way (see `_write_signal`).

    The shape of the landing place lives in exactly one place:
    `<signals_dir>/<cycle>/<node>.json`.
    """
    return Path(signals_dir) / cycle_id / f'{node_id}.json'


def _read_signal(cycle_id: str, node_id: str, project: str | None = None):
    return _read_json(_signal_path(cycle_id, node_id, project),
                      f'signals/{cycle_id}/{node_id}.json')


def _write_signal(cycle_id: str, node_id: str, status: str, summary: str, outputs: dict,
                  signals_dir=None) -> Path:
    """One node's signal: the record's one writer, and the six keys a reader gets.

    The path is `_signal_path`'s and the moment is `_now`'s, so a signal written by a worker, by
    `handoff.py` or by `cycle retry` is the same record at the same place.

    `signals_dir` is the landing place of **this one write** (`<project>/.pf/signals`), used and
    dropped: give it, and this write lands there while this copy's binding is left untouched --
    moving the binding is `bind_paths`' one job, and more than one caller in a process shares this
    store, so having a note write quietly change the whole process's destination would land it in
    the previous project. Without it, this copy's current binding is used (the root cwd resolved to
    when this copy was imported).
    """
    sig_path = (_signal_path_in(signals_dir, cycle_id, node_id) if signals_dir is not None
                else _signal_path(cycle_id, node_id))
    sig = {
        'cycle': cycle_id, 'node': node_id,
        'status': status, 'summary': summary,
        'outputs': outputs, 'timestamp': _now(),
    }
    try:
        _loader().load_shared('jsonio').atomic_write_json(sig_path, sig)
    except Exception as e:
        print(f'Error: write signal failed: {e}', file=sys.stderr)
        sys.exit(1)
    return sig_path


# ── who is holding which node ────────────────────────────────────────────────

def _cycles_holding_node(cycles: list, node: str) -> list:
    """Which cycles are pinning this node right now? -- one node belongs to one cycle at a time.

    A cycle's record carries the set of nodes it is holding right now, so "who is holding this node
    now" is a fact readable off the disk; there is no need to guess at a "current cycle". The
    closer that runs `--after` only knows which node it ran: a node is unique and so is the one
    cycle holding it, and that is enough to pin the cycle down -- this closing command does not
    need (and should not need) to know the cycle's name first.

    A cycle's own status takes no part in the rule: a line out of service (inactive) still holds
    the node it never finished, and that is exactly why it has to be closable before a human can
    move on.

    When two cycles really are pinning the same node (one piece of work started by two lines at
    once), both are returned -- the caller reports the ambiguity.
    """
    return [c for c in cycles if node in _holding_ids(c)]


def _closed_steps_of_node(cycles: list, node: str) -> str:
    """The step this node already ran to completion on another cycle, written as one human sentence.

    Running the closing command a second time lands here: the work was delivered and the node was
    put down, so all that is left is to tell the caller there is nothing to settle.
    """
    for c in cycles:
        for step in reversed(c.get('path') or []):
            if step.get('node') == node and step.get('result') in ('completed', 'failed'):
                return f"on {c.get('id')!r}'s path ({step['result']})"
    return ''


def bind_paths(ledger_dir) -> None:
    """Point this copy of the tool at one project's `.pf/` (in-process callers only).

    The CLI's paths come from `Path.cwd()` at import, because it is started with cwd set to the
    project. A caller that loads this module in process instead has to say which project it means:
    everything else reads these bindings at call time, so this one call moves the whole module.

    `ledger_dir` is `<project>/.pf`; every record's path and every `*_dir_for()` come off that one
    root at call time, so a caller that names the ledger has named all of it.
    """
    global LEDGER_DIR
    LEDGER_DIR = Path(ledger_dir)
