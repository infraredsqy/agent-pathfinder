"""The read layer over one project's `.pf/`.

State, signals, the DAG, the instructions and each node's own text all live in `<project>/.pf/`,
and they are read here. A project either commits that directory or keeps it to itself; nothing in
this module cares which. What is not part of the record is the dispatch plumbing -- `meta/launch/`,
the session notes, the claim/started markers, the broken-chain marker -- which is why `meta_dir()`
is the other door this module offers. One loader per file, each returning what is on disk, or an
empty shape when the file is absent. The read itself, and the wording a record that cannot be read
gets, is `tools/shared/jsonio.py`'s: it raises `JsonReadError` and the caller handles it.

A cycle record in `state.json` is the whole of a cycle -- its identity, where it is now, and the
path it has walked:

    {"id": ..., "title": ..., "status": "active",
     "holding": [{"id": "<uuid>", "since": "..."}],
     "path": [{"node": "<uuid>", "started_at": "...", "ended_at": "...", "result": "completed"}],
     "tags": [...], "policies": {"when_shelved": "stop", "parallel": 2}}

`holding` is how many nodes this line carries at once: the claim goes in there and `parallel`
decides how many fit (`_holding_of` / `_holding_ids`, borrowed from `conduct_state/store.py`, are the
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

# `tools/shared/` is the shared layer, and it has no package to import from: every file in it is
# loaded by path through `loader.py`. These two functions are that bootstrap, identical in every file
# that needs it -- a path load, never a name import, because nothing puts `tools/` on `sys.path`.
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


def _shared(name, filename):
    """One file out of `tools/shared/`, the same module for every caller in this process.

    Loaded for use, not copied: the ledger lock's reentrancy record is one per process, and a second
    copy of that module would give two of them.
    """
    return _loader().load_by_path(name, SHARED / filename, register=True, reuse=True)


# ── the writer package's own two files, borrowed ─────────────────────────────
# 一条 cycle 的策略表与算法住 `conduct_state/cycles.py`（那边管写，`cycle policy`），holding 的
# 规范读法住 `conduct_state/store.py`（那边管认领的读写）。这一侧只读它们，于是从正本取用，不
# 各留一份：两份分头漂移就是「一条线配了 `zcode` 却读成没配」「两条线抢同一格」的来路。
#
# 两个包都是按路径加载的，互相 import 会绕出一个环，所以取的是**文件**：走本树唯一的装载器
# （`tools/shared/loader.py`），在 `sys.modules` 里给这两个文件一个本包自己的包名 —— `cycles.py`
# 开头那句 `from .store import ...` 需要一个能解析的包，而 `conduct_state/__init__.py` 不必执行
# （它把写侧每一层都拉进来，这里只要两个文件）。
#
# 这条借用的边界：**两个包各自装载同一个文件**（各按自己的名字注册），于是本进程里有两份模块
# 对象，读出来的表**按值相等**（用例按 `==` 比对钉住 `POLICY_*` 那几张表），跨包的 `is` 不成立
# —— 谁也不该拿 `is` 去比这两份。要改这张表，改的是 `conduct_state/cycles.py` 那个文件本身，
# 这一侧跟着变；在这里再抄一份，就是两处分头漂。

_STATE_DIR = Path(__file__).resolve().parent.parent / 'conduct_state'
_STATE_PKG = 'pf_pipeline_state_source'


def _state_module(name: str):
    """`conduct_state/<name>.py`，按路径加载，同一个文件在本进程里只有一份。"""
    if _STATE_PKG not in sys.modules:
        package = types.ModuleType(_STATE_PKG)
        package.__path__ = [str(_STATE_DIR)]
        sys.modules[_STATE_PKG] = package
    loader = _loader()
    if name != 'store':                 # `cycles.py` 的第一句就是 `from .store import ...`
        loader.load_by_path(f'{_STATE_PKG}.store', _STATE_DIR / 'store.py',
                            register=True, reuse=True)
    return loader.load_by_path(f'{_STATE_PKG}.{name}', _STATE_DIR / f'{name}.py',
                               register=True, reuse=True)


_STATE_CYCLES = _state_module('cycles')
_STATE_STORE = _state_module('store')

#: 策略表与算法：`conduct_state/cycles.py` 的正本，这里只是它的另一个入口。
POLICY_DEFAULTS = _STATE_CYCLES.POLICY_DEFAULTS
POLICY_VALUES = _STATE_CYCLES.POLICY_VALUES
POLICY_COUNTS = _STATE_CYCLES.POLICY_COUNTS
POLICY_NUMBERS = _STATE_CYCLES.POLICY_NUMBERS
POLICY_WORDS = _STATE_CYCLES.POLICY_WORDS
POLICY_WORD_CHOICES = _STATE_CYCLES.POLICY_WORD_CHOICES
cycle_policy = _STATE_CYCLES.cycle_policy

#: 一个节点此刻拿在哪条线手里：认领的规范读法，`conduct_state/store.py` 的正本。
_holding_of = _STATE_STORE._holding_of
_holding_ids = _STATE_STORE._holding_ids

# ── the stored vocabularies, and the derived labels ──────────────────────────
# 读进来的就是盘上那个词，写出去的也是这一个：词表外的词照字面读，绝不替它猜一个词
# （与 `dag-manage.py` 同一条约定）。
#: 节点状态：`todo` 在局中未交付（正在被某条 cycle 跑的节点也还是 todo）、`shelved` 出局等人、
#: `completed` 已交付。
#: cycle 状态只有两个：`active` 这条线该往前推、`inactive` 不要推。一条线停下来的原因不值得留
#: 在状态里（停手那一刻的 reason 才是）。
#: 节点执行模式：`manual` 是一个讨论格（没有 execute→review→fix 循环）、`checkpoint` 循环照跑但
#: 收尾停下等人说一句继续、`auto` 收尾自己开下一个节点。三个词就是全部。
#: 节点没写 mode 时的兜底，与 `dag-manage.py` 的 DEFAULT_MODE 同一个词。
DEFAULT_MODE = 'auto'


def normalize_mode(mode: str | None) -> str:
    """盘上那个词，或（没写时）兜底的 `auto` —— 这一条兜底的唯一拼法。

    读盘回填（`load_dag`）、取值（`nodes.mode_for`）、停手判据（`planner.stop_criterion`）都走这里：
    多一个 mode 词时，这一处改完就齐了。
    """
    return mode or DEFAULT_MODE


# ── paths / read layer ───────────────────────────────────────────────────────

def ledger_dir(project: Path) -> Path:
    """`<project>/.pf/`: everything this tool writes for one project lives under it."""
    return Path(project) / '.pf'


def meta_dir(project: Path) -> Path:
    """The dispatch plumbing under the ledger, `<project>/.pf/meta/`.

    Launch prompts, claim/started markers, the broken-chain marker and the session notes live here
    -- none of them is part of the record `.pf/` keeps, which is why this is its own door.
    `.pf/.gitignore` keeps `meta/` out of both the project's history and the ledger's own.
    """
    return ledger_dir(project) / 'meta'


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
    return _shared('pf_shared_jsonio', 'jsonio.py')


def _session_note():
    """`tools/shared/session-note.py` -- one module for every caller in this process.

    By path, like every other cross-file reference in this tree: nothing puts `tools/` on `sys.path`,
    so there is no name to import it by. The note keeps no state of its own, so callers share one.
    """
    return _shared('pf_shared_sessionnote', 'session-note.py')


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
