"""The read layer over one project's `.pf/`.

State, signals, the DAG, the instructions and each node's own text all live in `<project>/.pf/`,
and they are read here. A project either commits that directory or keeps it to itself; nothing in

this module cares which. The settings file is read here as well, and it is the one thing on this
page that is not this tool's own record: `pf config` owns it, and `load_config` merges its two
layers for the judgement that needs one key. What is not part of the record is the dispatch
plumbing -- `meta/launch/`, the session notes, the claim/started markers, the broken-chain marker
-- which is why `meta_dir()` is the other door this module offers. One loader per file, each
returning what is on disk, or an empty shape when the file is absent. The read itself, and the
wording a record that cannot be read gets, is `tools/shared/jsonio.py`'s: it raises
`JsonReadError` and the caller handles it.


A cycle record in `state.json` is the whole of a cycle -- its identity, where it is now, and the
path it has walked:

    {"id": ..., "title": ..., "status": "active",
     "holding": [{"id": "<uuid>", "since": "..."}],
     "path": [{"node": "<uuid>", "started_at": "...", "ended_at": "...", "result": "completed"}],
     "tags": [...], "policies": {"when_shelved": "stop", "parallel": 2}}

`holding` is how many nodes this line carries at once: the claim goes in there and `parallel`
decides how many fit (`_holding_of` / `_holding_ids`, borrowed from `state/store.py`, are the
one reading of it).

How a node's own work is split never was the pipeline's business, and neither is how many sessions a
node took.

A cycle has exactly two statuses, `active` (this line should push forward) and `inactive` (do not
push); the graph has three, `todo` / `shelved` / `completed`. Two labels are DERIVED and never stored:
`ready` (a `todo` node whose dependencies are all `completed` and which no cycle holds) and
`running` (a `todo` node a cycle holds). The graph tool stays single-table, so the join that turns
"a node some cycle holds" into `running` lives here, in the readers that know both records.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

# Bootstrap: the loader is fetched by path too, so each file has to carry this bit itself (see
# `shared/loader.py`).
SHARED = Path(__file__).resolve().parents[2] / 'shared'

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
    """`tools/shared/ledger.py`: where `.pf/` lands and where those two settings files land -- one
    source."""
    return _loader().load_shared('ledger')

#: The settings file's name: its one source is `tools/shared/ledger.py`, and this package only
#: receives that value (the package's `__all__` takes it).
_FROM_LEDGER = {'CONFIG_NAME': 'CONFIG_NAME'}


def __getattr__(name: str):
    """The few names this module takes from their one source (see `_FROM_LEDGER`)."""
    if name in _FROM_LEDGER:
        return getattr(_ledger(), _FROM_LEDGER[name])
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


# ── the writer package's own two files, borrowed ─────────────────────────────
# A cycle's policy table and algorithms live in `state/cycles.py` (that side owns writing,
# `cycle policy`), and holding's canonical reading lives in `state/store.py` (that side owns the
# claim's reads and writes). This side only reads them, so it takes them from the one source rather
# than keeping its own copy: two copies drifting apart is how "a line is configured for `zcode` but
# read as unconfigured" and "two lines grab the same slot" come about.
#
# Both packages are loaded by path, and importing each other would wind a cycle, so what is taken is
# the **file**: through this tree's one loader (`tools/shared/loader.py`), giving the two files a
# package name of this package's own in `sys.modules` -- the `from .store import ...` opening
# `cycles.py` needs a package to resolve against, and `state/__init__.py` need not run (it pulls in
# every layer of the writing side, and here only two files are wanted).
#
# The boundary of this borrowing: **the two packages each load the same file** (registering it under
# their own name), so this process holds two module objects and the tables read out of them are
# **equal by value** (cases pin the `POLICY_*` tables with `==`), while a cross-package `is` does
# not hold -- nobody should compare these two with `is`. To change the table, change the file
# `state/cycles.py` itself and this side follows; copying it again here is the same drift in two
# places.

_STATE_DIR = Path(__file__).resolve().parent.parent / 'state'
_STATE_PKG = 'pf_pipeline_state_source'


def _state_module(name: str):
    """`state/<name>.py`, loaded by path, one copy of each file per process."""
    if _STATE_PKG not in sys.modules:
        package = types.ModuleType(_STATE_PKG)
        package.__path__ = [str(_STATE_DIR)]
        sys.modules[_STATE_PKG] = package
    loader = _loader()
    if name != 'store':                 # `cycles.py`'s first line is `from .store import ...`
        loader.load_by_path(f'{_STATE_PKG}.store', _STATE_DIR / 'store.py',
                            register=True, reuse=True)
    return loader.load_by_path(f'{_STATE_PKG}.{name}', _STATE_DIR / f'{name}.py',
                               register=True, reuse=True)


_STATE_CYCLES = _state_module('cycles')
_STATE_STORE = _state_module('store')

#: The policy tables and algorithms: `state/cycles.py`'s one source, of which this is only another
#: door.
POLICY_DEFAULTS = _STATE_CYCLES.POLICY_DEFAULTS
POLICY_VALUES = _STATE_CYCLES.POLICY_VALUES
POLICY_COUNTS = _STATE_CYCLES.POLICY_COUNTS
POLICY_NUMBERS = _STATE_CYCLES.POLICY_NUMBERS
POLICY_WORDS = _STATE_CYCLES.POLICY_WORDS
POLICY_WORD_CHOICES = _STATE_CYCLES.POLICY_WORD_CHOICES
cycle_policy = _STATE_CYCLES.cycle_policy

#: Which line a node is held by right now: holding's canonical reading, whose one source is
#: `state/store.py`.
_holding_of = _STATE_STORE._holding_of
_holding_ids = _STATE_STORE._holding_ids

# ── the stored vocabularies, and the derived labels ──────────────────────────
# What is read in is the word on disk, and that is what is written out too: a word outside the
# vocabulary is read literally, and the tool never guesses one for it (the same convention as
# `dag-manage.py`).
#: Node status: `todo` is in play and not delivered (a node some cycle is running is still todo),
#: `shelved` is out of play waiting for a human, `completed` is delivered.
#: A cycle has only two statuses: `active` -- this line should push forward -- and `inactive` -- do
#: not push. Why a line stopped is not worth keeping in the status (the reason at the moment it
#: stopped is).
#: Node execution mode: `manual` is a discussion slot (no execute→review→fix loop), `checkpoint`
#: runs the loop but the close stops and waits for a human to say continue, `auto` has the close
#: open the next node itself. Those three words are all of them.
#: The fallback for a node that does not write a mode, the same word as `dag-manage.py`'s
#: DEFAULT_MODE.
DEFAULT_MODE = 'auto'


def normalize_mode(mode: str | None) -> str:
    """The word on disk, or (when none is written) the `auto` fallback -- the one spelling of that
    fallback.

    Reading the disk back (`load_dag`), taking the value (`nodes.mode_for`) and the stop criterion
    (`planner.stop_criterion`) all come through here: adding a mode word is finished by changing
    this one place.
    """
    return mode or DEFAULT_MODE


# ── paths / read layer ───────────────────────────────────────────────────────

def ledger_dir(project: Path) -> Path:
    """`<project>/.pf/`: everything this tool writes for one project lives under it."""
    return _ledger().ledger_dir_for(project)


def meta_dir(project: Path) -> Path:
    """The dispatch plumbing under the ledger, `<project>/.pf/meta/`.

    Launch prompts, claim/started markers, the broken-chain marker and the session notes live here
    -- none of them is part of the record `.pf/` keeps, which is why this is its own door.
    `.pf/.gitignore` keeps `meta/` out of both the project's history and the ledger's own.
    """
    return _ledger().meta_dir_for(project)


# ── the settings: this project's, and the machine's underneath it ─────────────
# A project's and a machine's settings (the cells `pf config` owns). Unlike the record, settings
# **are not this pipeline's**: they are another tool's one source, and the pipeline reads one cell
# of them (the maintenance channel's switch). So this only reads, and hands over only two layers:
# the factory defaults and the vocabulary stay on the writing side (`state/config.py`), and the cell
# being read says for itself which words it knows.

#: The settings file's name, the same for both layers: `<project>/.pf/config.json` and
#: `~/.pf/config.json`. Taken from the one source (see `_FROM_LEDGER`), so this module spells no
#: second literal.


def global_config_path() -> Path:
    """This machine's layer of settings, `~/.pf/config.json` -- the same one `pf config set
    --global` writes.

    Where it lands is given by the one source (`shared/ledger.py`'s `home_config_path`, where the
    `PF_HOME_DIR` sandbox switch is read too): the reading side and the writing side therefore
    resolve the same "home".
    """
    return _ledger().home_config_path()


def project_config_path(project: Path) -> Path:
    return _ledger().config_path_for(project)


def load_config(project: Path) -> dict:
    """This project's settings: the machine's layer, with the project's layer covering it -- the layer
    written later wins.

    Either layer may be absent (read as "the factory default was never changed"), and either may
    carry keys this pipeline does not know (others own them). This hands over only what is written
    down: **each cell's vocabulary and factory default are stated by the judgement that reads it**
    (the maintenance channel's `maintenance.issue_repair_on`) -- a cell of one settings file
    explained somewhere else is how one word grows two readings.
    """
    out = {}
    for path in (global_config_path(), project_config_path(project)):
        layer = _jsonio().read_json_strict(path, _ledger().CONFIG_NAME)
        if isinstance(layer, dict):
            out.update(layer)
    return out


# ── session notes: which conversation produced a node, which ones ran it ──────
# `.pf/meta/sessions/<node id>.json`. Machine-local by design: a session id is only meaningful on
# the machine that opened it, so it stays out of the shared record. The shape lives in
# `tools/shared/session-note.py`.

SESSION_NOTE_PY = SHARED / 'session-note.py'

#: This module's own "the caller left `at` out" marker: it means "let the note decide", which is
#: `tools/shared/session-note.py`'s NOW (the moment of the call). An importer that does not know when
#: a session was opened passes `at=None` instead, and the note keeps `null`.
_NOTE_NOW = object()


def _jsonio():
    """`tools/shared/jsonio.py`: the tree's one reader and atomic writer for these JSON records."""
    return _loader().load_shared('jsonio')


def _session_note():
    """`tools/shared/session-note.py` -- one module for every caller in this process.

    By path, like every other cross-file reference in this tree: nothing puts `tools/` on `sys.path`,
    so there is no name to import it by. The note keeps no state of its own, so callers share one.
    """
    return _loader().load_shared('session-note')


def session_note(project: Path, node_id: str) -> dict:
    """One node's note: the discussion it came from, and the sessions that ran it."""
    return _session_note().read(project, node_id)


def session_notes(project: Path) -> dict:
    """Every note under this ledger, keyed by node id -- the listing reader's door."""
    return _session_note().notes(project)


def note_session(project: Path, node_id: str, role: str, session: str, host: str | None = None,
                 at=_NOTE_NOW, source: str | None = None) -> bool:
    """Note one session under one role. True when the note changed; a repeat is a no-op.

    `at` left out records the moment of the call; `at=None` records "not known" (an import from a
    record that no longer carries the time). Raises on a real write error: each caller decides for
    itself whether a missing note is worth failing for (a spawn is not; an import is).
    """
    module = _session_note()
    extra = {} if at is _NOTE_NOW else {'at': at}
    return module.append(project, node_id, role, session, host=host, source=source, **extra)


def load_state(project: Path) -> dict:
    data = _jsonio().read_json_strict(ledger_dir(project) / 'state.json', 'state.json')
    if not data:
        return {'cycles': []}
    data.setdefault('cycles', [])
    return data


def load_signal(project: Path, cycle_id: str, node_id: str) -> dict | None:
    return _jsonio().read_json_strict(ledger_dir(project) / 'signals' / cycle_id / f'{node_id}.json',
                                      f'signals/{cycle_id}/{node_id}.json')


def load_dag(project: Path) -> dict:
    """The graph, with every node's mode filled in when the record leaves it out.

    The STATUS word is read exactly as it is on disk: the words are the ones `dag-manage.py` writes,
    because two tools disagreeing about one word is the shape of bug this reader exists to prevent.
    """
    data = _jsonio().read_json_strict(ledger_dir(project) / 'ideas.json', 'ideas.json') or {'nodes': {}}
    for node in (data.get('nodes') or {}).values():
        if isinstance(node, dict):
            node['mode'] = normalize_mode(node.get('mode'))
    return data


def load_instructions(project: Path) -> dict:
    """The account, exactly as it is on disk.

    Nothing is resolved or stamped here: a reader that needs an entry's scope asks `entry_scope`,
    the one place the older `lifetime` field is interpreted. The file changes only when someone
    explicitly migrates it -- a read never rewrites another workspace's account.
    """
    data = _jsonio().read_json_strict(ledger_dir(project) / 'instructions.json', 'instructions.json')
    if not data:
        return {'instructions': []}
    data.setdefault('instructions', [])
    return data


def active_cycles(state: dict) -> list:
    return [c for c in state.get('cycles', []) if c.get('status') == 'active']


def find_cycle(state: dict, cycle_id: str | None) -> dict | None:
    cycles = state.get('cycles', [])
    if cycle_id:
        for c in cycles:
            if c['id'] == cycle_id:
                return c
        return None
    act = active_cycles(state)
    return act[0] if len(act) == 1 else None


def cycle_path(cycle: dict) -> list:
    """The nodes this cycle has finished, in the order it walked them."""
    return cycle.get('path') or []
