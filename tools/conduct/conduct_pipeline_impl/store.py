"""The read layer over one project's `.pf/`.

State, signals, the DAG, the instructions and each node's own text all live in `<project>/.pf/`,
and they are read here. A project either commits that directory or keeps it to itself; nothing in
this module cares which. What is not part of the record is the dispatch plumbing -- `meta/launch/`,
the session notes, the claim/started markers, the broken-chain marker -- which is why `meta_dir()`
is the other door this module offers. One loader per file, each returning what is on disk, or an
empty shape when the file is absent.

A cycle record in `state.json` is the whole of a cycle -- its identity, where it is now, and the
path it has walked:

    {"id": ..., "title": ..., "status": "active",
     "holding": [{"id": "<uuid>", "since": "..."}],
     "path": [{"node": "<uuid>", "started_at": "...", "ended_at": "...", "result": "completed"}],
     "tags": [...], "policies": {"when_shelved": "stop", "parallel": 2}}

`holding` is how many nodes this line carries at once: the claim goes in there and `parallel`
decides how many fit (`_holding_of` / `_holding_ids` are the one reading of it).

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
import json
from pathlib import Path


# ── the two stored vocabularies, and the derived labels ──────────────────────
# 读进来的就是盘上那个词，写出去的也是这一个：词表外的词照字面读，绝不替它猜一个词
# （与 `dag-manage.py` 同一条约定）。
#: 节点状态：`todo` 在局中未交付（正在被某条 cycle 跑的节点也还是 todo）、`shelved` 出局等人、
#: `completed` 已交付。
#: cycle 状态只有两个：`active` 这条线该往前推、`inactive` 不要推。一条线停下来的原因不值得留
#: 在状态里（停手那一刻的 reason 才是）。
#: 一条 cycle 自己的策略，读的时候补默认值。这里与 `conduct_state/cycles.py` 的那份是同一套词：
#: 那边管写（`cycle policy`），这边管读（停手判据、维护线、并行上限要用它）。
POLICY_DEFAULTS = {'when_shelved': 'stop', 'parallel': 1}
POLICY_VALUES = {'when_shelved': ('stop', 'continue')}
#: 整数型策略（有默认值，下限 1）：这条线同时能握几个节点。
POLICY_COUNTS = ('parallel',)
#: 数值型策略（没有默认值，没配就不出现在读数里）：两条维护线住在这里。
POLICY_NUMBERS = ('test_seconds_limit', 'issue_pool_weight_limit')
#: 词表型策略（候选表里的词才收，`off` 清除即"没配"）：这条线的会话由哪份 spawner 开。
POLICY_WORDS = ('spawner',)
POLICY_WORD_CHOICES = {'spawner': ('dsh', 'zcode')}
#: 节点执行模式：`manual` 是一个讨论格（没有 execute→review→fix 循环）、`checkpoint` 循环照跑但
#: 收尾停下等人说一句继续、`auto` 收尾自己开下一个节点。三个词就是全部。
#: 节点没写 mode 时的兜底，与 `dag-manage.py` 的 DEFAULT_MODE 同一个词。
DEFAULT_MODE = 'auto'


def normalize_mode(mode: str | None) -> str:
    """盘上那个词，或（没写时）兜底的 `auto`。"""
    return mode or DEFAULT_MODE


def cycle_policy(cycle: dict) -> dict:
    """这条线现在的有效策略。

    盘上没有这个键（或写了个不认识的词）就按默认值走：缺省值必须是保守的那一边 —— `when_shelved`
    的默认是 `stop`（读不准时停下等人），`parallel` 的默认是 1（读不准时一个节点一个节点来）。

    数值型策略（`POLICY_NUMBERS`）与词表型策略（`POLICY_WORDS`）没有默认值：
    没配就不出现在这份读数里。
    """
    stated = cycle.get('policies') or {}
    out = {}
    for key, default in POLICY_DEFAULTS.items():
        value = stated.get(key)
        if key in POLICY_COUNTS:
            out[key] = value if _is_count(value) else default
        else:
            out[key] = value if value in POLICY_VALUES.get(key, ()) else default
    for key in POLICY_NUMBERS:
        value = stated.get(key)
        if not isinstance(value, bool) and isinstance(value, (int, float)):
            out[key] = value
    for key in POLICY_WORDS:
        value = stated.get(key)
        if value in POLICY_WORD_CHOICES.get(key, ()):
            out[key] = value
    return out


def _is_count(value) -> bool:
    """盘上的一个整数型策略值成不成立：真整数、下限 1。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


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

SESSION_NOTE_PY = Path(__file__).resolve().parents[2] / 'shared' / 'session-note.py'

_session_note_module = None

#: This module's own "the caller left `at` out" marker: it means "let the note decide", which is
#: `tools/shared/session-note.py`'s NOW (the moment of the call). An importer that does not know when
#: a session was opened passes `at=None` instead, and the note keeps `null`.
_NOTE_NOW = object()


def _session_note():
    """`tools/shared/session-note.py`, loaded by path, once per process.

    By path, like every other cross-file reference in this tree: nothing puts `tools/` on `sys.path`,
    so there is no name to import it by.
    """
    global _session_note_module
    if _session_note_module is None:
        spec = importlib.util.spec_from_file_location('session_note_pipeline', SESSION_NOTE_PY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _session_note_module = module
    return _session_note_module


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


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except UnicodeDecodeError as e:
        raise ValueError(f'{path}: not valid UTF-8: {e}') from e
    except json.JSONDecodeError as e:
        raise ValueError(f'{path}: not valid JSON: {e}') from e


def load_state(project: Path) -> dict:
    data = _read_json(ledger_dir(project) / 'state.json')
    if not data:
        return {'cycles': []}
    data.setdefault('cycles', [])
    return data


def load_signal(project: Path, cycle_id: str, node_id: str) -> dict | None:
    return _read_json(ledger_dir(project) / 'signals' / cycle_id / f'{node_id}.json')


def load_dag(project: Path) -> dict:
    """The graph, with every node's mode filled in when the record leaves it out.

    The STATUS word is read exactly as it is on disk: the words are the ones `dag-manage.py` writes,
    because two tools disagreeing about one word is the shape of bug this reader exists to prevent.
    """
    data = _read_json(ledger_dir(project) / 'ideas.json') or {'nodes': {}}
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
    data = _read_json(ledger_dir(project) / 'instructions.json')
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


def _holding_of(cycle: dict) -> list:
    """这条线此刻握着的那一组节点，规范形 `[{'id':..., 'since':...}, ...]`（可为空）。

    这是认领标记的唯一读法。写侧只写 `holding`（见 `conduct_state/store.py` 的 `_set_holding`）。
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
    """握着的那组节点的 id，按认领先后排。"""
    return [item['id'] for item in _holding_of(cycle)]


def cycle_path(cycle: dict) -> list:
    """The nodes this cycle has finished, in the order it walked them."""
    return cycle.get('path') or []
