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
import os
import sys
import types
from pathlib import Path

# 引导：装载器也按路径取，所以这一段只能每个文件自己带（见 `shared/loader.py`）。
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
    """`tools/shared/ledger.py`: `.pf/` 的落点与那两枚设置文件的落点 —— 正本一处。"""
    return _loader().load_shared('ledger')

#: 设置文件的名字：正本在 `tools/shared/ledger.py`，本包只是收下那个值（包的 `__all__` 收它）。
_FROM_LEDGER = {'CONFIG_NAME': 'CONFIG_NAME'}


def __getattr__(name: str):
    """本模块里那几个取自正本的名字（见 `_FROM_LEDGER`）。"""
    if name in _FROM_LEDGER:
        return getattr(_ledger(), _FROM_LEDGER[name])
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


# ── the writer package's own two files, borrowed ─────────────────────────────
# 一条 cycle 的策略表与算法住 `state/cycles.py`（那边管写，`cycle policy`），holding 的
# 规范读法住 `state/store.py`（那边管认领的读写）。这一侧只读它们，于是从正本取用，不
# 各留一份：两份分头漂移就是「一条线配了 `zcode` 却读成没配」「两条线抢同一格」的来路。
#
# 两个包都是按路径加载的，互相 import 会绕出一个环，所以取的是**文件**：走本树唯一的装载器
# （`tools/shared/loader.py`），在 `sys.modules` 里给这两个文件一个本包自己的包名 —— `cycles.py`
# 开头那句 `from .store import ...` 需要一个能解析的包，而 `state/__init__.py` 不必执行
# （它把写侧每一层都拉进来，这里只要两个文件）。
#
# 这条借用的边界：**两个包各自装载同一个文件**（各按自己的名字注册），于是本进程里有两份模块
# 对象，读出来的表**按值相等**（用例按 `==` 比对钉住 `POLICY_*` 那几张表），跨包的 `is` 不成立
# —— 谁也不该拿 `is` 去比这两份。要改这张表，改的是 `state/cycles.py` 那个文件本身，
# 这一侧跟着变；在这里再抄一份，就是两处分头漂。

_STATE_DIR = Path(__file__).resolve().parent.parent / 'state'
_STATE_PKG = 'pf_pipeline_state_source'


def _state_module(name: str):
    """`state/<name>.py`，按路径加载，同一个文件在本进程里只有一份。"""
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

#: 策略表与算法：`state/cycles.py` 的正本，这里只是它的另一个入口。
POLICY_DEFAULTS = _STATE_CYCLES.POLICY_DEFAULTS
POLICY_VALUES = _STATE_CYCLES.POLICY_VALUES
POLICY_COUNTS = _STATE_CYCLES.POLICY_COUNTS
POLICY_NUMBERS = _STATE_CYCLES.POLICY_NUMBERS
POLICY_WORDS = _STATE_CYCLES.POLICY_WORDS
POLICY_WORD_CHOICES = _STATE_CYCLES.POLICY_WORD_CHOICES
cycle_policy = _STATE_CYCLES.cycle_policy

#: 一个节点此刻拿在哪条线手里：认领的规范读法，`state/store.py` 的正本。
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
    return _ledger().ledger_dir_for(project)


def meta_dir(project: Path) -> Path:
    """The dispatch plumbing under the ledger, `<project>/.pf/meta/`.

    Launch prompts, claim/started markers, the broken-chain marker and the session notes live here
    -- none of them is part of the record `.pf/` keeps, which is why this is its own door.
    `.pf/.gitignore` keeps `meta/` out of both the project's history and the ledger's own.
    """
    return _ledger().meta_dir_for(project)


# ── the settings: this project's, and the machine's underneath it ─────────────
# 一个项目与一台机器的设置（`pf config` 管的那几格）。与记录不同，设置**不属于这条管线**：它是别的
# 工具的正本，管线只读其中一格（维护通道那个开关）。所以这里只读、只交出两层：出厂默认与词表留在
# 写那一侧（`state/config.py`），读的那一格自己说它认什么词。

#: 设置文件的名字，两层同名：`<项目>/.pf/config.json` 与 `~/.pf/config.json`。取自正本（见
#: `_FROM_LEDGER`），本模块不拼第二个字面量。


def global_config_path() -> Path:
    """这台机器那一层设置，`~/.pf/config.json` —— `pf config set --global` 写的同一份。

    `PF_HOME_DIR` 把它指到沙箱（与写那一侧同一枚环境变量），否则它是谁的就是谁的。家目录当作
    "一个项目"来算那层落点，拼法也走同一个 `config_path_for`。
    """
    home = os.environ.get('PF_HOME_DIR') or str(Path.home())
    return _ledger().config_path_for(Path(home).expanduser())


def project_config_path(project: Path) -> Path:
    return _ledger().config_path_for(project)


def load_config(project: Path) -> dict:
    """这个项目的设置：机器那一层，上面盖着项目那一层 —— 后写的那层赢。

    两层都可以不在（读作"没改过出厂默认"），也可以带着这个管线不认识的键（别人管它们）。这里
    只交出写下来的东西：**每一格的词表与出厂默认由读它的那个判断自己说**（维护通道的
    `maintenance.issue_repair_on`），一份设置里的一格跑到别处去解释，就是同一个词长出两种读法。
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
