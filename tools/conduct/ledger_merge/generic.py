"""The three-way primitives every engine is built from, and how a decision is said out loud.

Nothing here knows one record's fields: the rules that do are the four engines next door. What this
module owns is the half of the merge that is the same whichever ledger is being merged:

  * the trichotomy -- `ours == base` takes theirs, `theirs == base` takes ours, a field both sides
    changed to two different values is `_UNDECIDED` and the engine decides or flags it
  * the field-by-field walk of one record, and the entry-wise walk of one collection, including the
    deletion rule the graph needs (`dag-manage rm` is a command a human really uses)
  * the append-only log rule the cycle `path` and an instruction's `applied` share
  * the record vocabulary (`KIND_*`), the table that says which ledger a path or a shape is, and the
    two ways a decision is recorded: a line `--dry-run` prints, or a conflict a human settles
"""

from __future__ import annotations

import json
import sys

#: Which ledger record a file under `.pf/` is.
KIND_IDEAS = 'ideas'
KIND_STATE = 'state'
KIND_INSTRUCTIONS = 'instructions'
KIND_SIGNAL = 'signal'

#: What the driver exits with: merged, a human has to decide, or a usage/read error.
EXIT_OK = 0
EXIT_CONFLICT = 1
EXIT_USAGE = 2

#: The collection key each file is built around -- the shape that names it when the name cannot.
_COLLECTION_KINDS = {'nodes': KIND_IDEAS, 'cycles': KIND_STATE, 'instructions': KIND_INSTRUCTIONS}

_MISSING = object()          # the key is not in the record at all (an explicit null IS a value)
#: An entry one side DELETED, as a value in a conflict line (`_fmt` knows how to print it).
_DELETED = object()
#: `_three_way`'s "no decision" answer. It is NOT `None`: JSON null is a perfectly ordinary value
#: here (`cycle`, `node` and `resolved_at` are null most of the time), and using
#: `None` for both would turn every pair of equal nulls into a conflict.
_UNDECIDED = object()


# ═══════════════════════════════════════════════════════════════════════════════
#  Which ledger is this, and how a decision is reported
# ═══════════════════════════════════════════════════════════════════════════════

def _kind_of(name) -> str | None:
    """Which ledger record a path is, by FILE NAME.

    `name` is git's `%P` (the path in the repo) when the driver is configured with it, or the real
    path when somebody runs the driver by hand.
    """
    if not name:
        return None
    parts = str(name).replace('\\', '/').split('/')
    base = parts[-1]
    if base == 'ideas.json':
        return KIND_IDEAS
    if base == 'state.json':
        return KIND_STATE
    if base == 'instructions.json':
        return KIND_INSTRUCTIONS
    if base.endswith('.json') and 'signals' in parts[:-1]:
        return KIND_SIGNAL
    return None


def _kind_from_shape(*sides) -> str | None:
    """The record's own shape, for when there is no file name to go on.

    Under git there often is not: `%O`/`%A`/`%B` are `.merge_file_XXXXXXXX`. The three collections
    are unmistakable (their key IS the kind), and a ticket is the one record that carries a `status`
    with no collection under it. Anything else is refused rather than guessed at.
    """
    for data in sides:
        if isinstance(data, dict):
            for key, kind in _COLLECTION_KINDS.items():
                if key in data:
                    return kind
    for data in sides:
        if isinstance(data, dict) and data.get('status') and (
                'timestamp' in data or 'outputs' in data):
            return KIND_SIGNAL
    return None


def _display(path) -> str:
    return str(path).replace('\\', '/') if path else '?'


class Report:
    """The decision log (`--dry-run` prints it) and the conflict lines (stderr, always)."""

    def __init__(self, kind: str, path):
        self.kind = kind
        self.path = _display(path)
        self.decisions: list[str] = []
        self.conflicts: list[str] = []

    def decide(self, what: str) -> None:
        self.decisions.append(f'  {what}')

    def conflict(self, entry: str, field: str, ours, theirs, note: str = '') -> None:
        tail = f'（{note}）' if note else ''
        self.conflicts.append(f'pf 冲突 [{self.path}] {entry} 字段 {field}：'
                              f'ours={_fmt(ours)} theirs={_fmt(theirs)}{tail}')

    def flush_conflicts(self) -> None:
        for line in self.conflicts:
            print(line, file=sys.stderr)


def _fmt(value) -> str:
    if value is _MISSING:
        return '<没有这个字段>'
    if value is _DELETED:
        return '<这一侧删掉了>'
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 120 else text[:117] + '...'


def _label(kind: str, key) -> str:
    if kind == KIND_IDEAS:
        return f'节点 {str(key)[:8]}'
    if kind == KIND_STATE:
        return f'cycle {key!r}'
    if kind == KIND_INSTRUCTIONS:
        return f'指令 {key}'
    return '条子'


# ═══════════════════════════════════════════════════════════════════════════════
#  The generic three-way primitives
# ═══════════════════════════════════════════════════════════════════════════════

def _get(obj, key):
    return obj[key] if isinstance(obj, dict) and key in obj else _MISSING


def _as_dict(obj) -> dict:
    return obj if isinstance(obj, dict) else {}


def _keys(*objs) -> list:
    """The merged key order: ours' order, then the keys only the other side has."""
    out = []
    for obj in objs:
        if isinstance(obj, dict):
            for key in obj:
                if key not in out:
                    out.append(key)
    return out


def _same(a, b) -> bool:
    return a is b or a == b


def _three_way(base_value, ours_value, theirs_value):
    """The trivial half of every field rule: `_UNDECIDED` when both sides really did change it."""
    if _same(ours_value, theirs_value):
        return ours_value
    if _same(ours_value, base_value):
        return theirs_value
    if _same(theirs_value, base_value):
        return ours_value
    return _UNDECIDED


def _both_changed(base_value, ours_value, theirs_value) -> bool:
    return _three_way(base_value, ours_value, theirs_value) is _UNDECIDED


def _merge_mapping(base, ours, theirs, rep, entry, resolve) -> dict:
    """One record, field by field.

    `resolve(key, base_value, ours_value, theirs_value)` decides an ordinary field. A field whose
    winning value is `_MISSING` is absent by design: either the key was deleted on one side, or a
    rule refused to decide it.
    """
    out = {}
    for key in _keys(ours, theirs):
        value = resolve(key, _get(base, key), _get(ours, key), _get(theirs, key))
        if value is not _MISSING:
            out[key] = value
    return out


def _merge_entries(base, ours, theirs, rep, resolve_entry, index_of,
                   propagate_delete=False) -> dict:
    """Entry-wise three-way over one collection: the union of the keys, in ours' order.

    A key only one side has is taken from that side WHOLE -- there is nothing to field-merge against.

    `propagate_delete` decides what that missing side MEANS. With it off (cycles, instructions) a
    missing entry is "this side never had it": the entry comes back, because neither tool has a
    command that deletes one, so a resurrection costs nothing. With it on (the graph: `dag-manage rm`
    is a real command a human uses) the base decides:

        base has it, one side deleted it, the other side left it alone -> delete it (result drops it)
        base has it, one side deleted it, the OTHER SIDE CHANGED it  -> conflict (delete vs edit)
        base never had it                                            -> the side that has it added it
        both sides deleted it                                        -> delete it
    """
    base_index, ours_index, theirs_index = (index_of(obj) for obj in (base, ours, theirs))
    merged = {}
    for key in list(ours_index) + [k for k in theirs_index if k not in ours_index]:
        label = _label(rep.kind, key)
        if key in ours_index and key in theirs_index:
            merged[key] = resolve_entry(label, base_index.get(key),
                                        ours_index[key], theirs_index[key], rep)
        elif key in ours_index:
            _keep_or_delete(merged, key, label, base_index, ours_index[key], _DELETED,
                            theirs_index, rep, propagate_delete, ours_side=True)
        else:
            _keep_or_delete(merged, key, label, base_index, theirs_index[key], _DELETED,
                            ours_index, rep, propagate_delete, ours_side=False)
    if propagate_delete:
        for key in base_index:
            if key not in ours_index and key not in theirs_index:
                rep.decide(f'{_label(rep.kind, key)}: 两侧都删了 → 结果里不要它')
    return merged


def _keep_or_delete(merged, key, label, base_index, survivor, deleted_token, other_index,
                    rep, propagate_delete, ours_side: bool):
    """One entry only ONE side has: an addition, or a deletion the other side has to answer for."""
    side = 'ours' if ours_side else 'theirs'
    other = 'theirs' if ours_side else 'ours'
    from_base = base_index.get(key)
    if not propagate_delete or from_base is None:
        rep.decide(f'{label}: 只有 {side} 有 → 取 {side}')
        merged[key] = survivor
        return
    if _same(survivor, from_base):
        rep.decide(f'{label}: {other} 删了、{side} 没动 → 删除（结果里不要它）')
        return
    rep.conflict(label, '（整条记录）', survivor, deleted_token,
                 f'{other} 删了、{side} 改过：删与改撞了')
    merged[key] = survivor                    # the value does not matter: the file is not written


def _plain_field(label, key, base_value, ours_value, theirs_value, rep):
    """A field the table gives no rule for: one side changed it -> that side; both -> a human."""
    got = _three_way(base_value, ours_value, theirs_value)
    if got is not _UNDECIDED:
        return got
    note = ('两侧都新增了这一条，但内容不同' if base_value is _MISSING
            else '两侧都改了这个字段，规则表里没有裁它的依据')
    rep.conflict(label, key, ours_value, theirs_value, note)
    return _MISSING


def _stamp(label, key, base_value, ours_value, theirs_value, rep, pick):
    """A metadata stamp (`updated_at` / `created_at`): the other side's value cannot be lost."""
    got = _three_way(base_value, ours_value, theirs_value)
    if got is not _UNDECIDED:
        return got
    if not all(isinstance(v, str) for v in (ours_value, theirs_value)):
        rep.conflict(label, key, ours_value, theirs_value, '时间戳不是字符串，取不了')
        return _MISSING
    return pick(ours_value, theirs_value)


def _extend_or_union(label, key, base_value, ours_value, theirs_value, *,
                     identity, same_act, order_by, rep, same_note=None):
    """An append-only log on one field: a prefix extends, two tails union, a mark disagrees.

    A cycle's `path` and an instruction's `applied` are one rule: one side being a prefix of the other
    takes the longer one; otherwise, past the common prefix, the two tails are unioned in the order
    `order_by` gives. What differs between the two is named by the caller -- `identity(mark)` is what
    makes two elements THE SAME thing (a node / a mark's `at`+`cycle`+`round`), `same_act(prev, mark)`
    says whether two elements sharing that identity are the same act (the cycle path compares only
    `result`: two sides recording the same outcome for one node, with two stamps, are not a quarrel),
    and `same_note` is how that one disagreement is said out loud.
    """
    got = _three_way(base_value, ours_value, theirs_value)
    if got is not _UNDECIDED:
        return got
    if not isinstance(ours_value, list) or not isinstance(theirs_value, list):
        rep.conflict(label, key, ours_value, theirs_value, '不是列表，裁不了')
        return _MISSING
    ours, theirs = list(ours_value), list(theirs_value)
    i = 0
    while i < len(ours) and i < len(theirs) and ours[i] == theirs[i]:
        i += 1
    if i == len(ours) or i == len(theirs):
        longer = ours if len(ours) >= len(theirs) else theirs
        rep.decide(f'{label}: {key} 一侧是另一侧的前缀 → 取长的那侧（{len(longer)} 项）')
        return longer
    seen: dict = {}
    for mark in ours[i:] + theirs[i:]:
        if not isinstance(mark, dict):
            rep.conflict(label, key, ours_value, theirs_value, f'{key} 里有一项不是对象，裁不了')
            return _MISSING
        tag = identity(mark)
        if tag in seen and not same_act(seen[tag], mark):
            prev = seen[tag]
            rep.conflict(label, key, prev, mark,
                         same_note(tag, prev, mark) if same_note else '同一项两侧内容不同')
            return _MISSING
        seen.setdefault(tag, mark)
    tail = sorted(seen.values(), key=order_by)
    rep.decide(f'{label}: {key} 共同前缀之后两侧各自追加 → 按戳排序取并集（{len(tail)} 项）')
    return ours[:i] + tail


def _merge_collection(label, key, base, ours, theirs, rep, *, resolve_entry, index_of,
                      propagate_delete=False, as_list=False, after=None) -> dict:
    """One record file, whole: the collection key goes entry-wise, every other field by the plain rule.

    `label` is what a conflict line calls this file, `key` is the collection's own key (`nodes` /
    `cycles` / `instructions`), `index_of` reads that one field as `{identity: entry}` -- the two
    spellings a record may be written in (a mapping, or a list of entries carrying an id) are that
    reader's business -- `as_list` says the merged collection goes back out as a list,
    `propagate_delete` is `_merge_entries`', and `after` is whatever this file still has to check once
    the result is whole (the graph's dangling dependencies).
    """
    out = {}
    for field in _keys(ours, theirs):
        if field == key:
            entries = _merge_entries(_get(base, field), _get(ours, field), _get(theirs, field),
                                     rep, resolve_entry, index_of,
                                     propagate_delete=propagate_delete)
            out[field] = list(entries.values()) if as_list else entries
        else:
            out[field] = _plain_field(label, field, _get(base, field), _get(ours, field),
                                      _get(theirs, field), rep)
    merged = {k: v for k, v in out.items() if v is not _MISSING}
    if after is not None:
        after(merged, ours, theirs, rep)
    return merged