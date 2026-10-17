"""`instructions.json` -- the standing instructions, and the traces a removal leaves.

One record per instruction, keyed by `id`, and the collection goes back out as a list; the account
also carries `removed`, the tombstone list (one `id` and the moment, no text). What this engine adds
to the generic walk:

  * entries propagate deletions like the graph: one side takes an entry off (`instruction rm`), the
    other leaves it alone -> it is gone. A removal on one side against an EDIT on the other is the
    graph's delete-vs-edit shape: a conflict, and a human settles it
  * the removal traces are unioned by `id` -- nothing ever takes one back, and they are what keeps
    two machines pulling from each other from minting the same id twice. Two machines that removed
    the same entry independently differ only in the stamp, and the EARLIER moment wins: whoever
    removed it first is when it left the account
  * the resolution act is ONE thing: `state` / `resolved_at` are written by one code path
    (`instruct resolve` archives, `instruct revive` clears the same two fields back), so a field only
    one side changed is that side's, and two sides that both changed it are ordered by `resolved_at`,
    the newer act winning for both. A revive CLEARS the stamp: a revive on one side against a
    withdrawal on the other has nothing left to order the two acts by -- a conflict. `note`
    deliberately does NOT travel with them: a hand-written note is never silently overwritten, so a
    `note` both sides changed is a conflict
  * the same `id` added by both sides with different content is an add/add collision: a conflict
"""

from .generic import (_MISSING, _UNDECIDED, _as_dict, _get, _keys, _merge_entries, _merge_mapping,
                      _plain_field, _stamp, _three_way)

#: One act of archiving (or putting back) writes these together, and the driver resolves them as a
#: unit. An archived entry used to record the hand that did it as well; nothing writes that any more,
#: so it is ordinary data with no rule of its own.
RESOLUTION_GROUP = ('state', 'resolved_at')
#: The key the trace a removal leaves lives under -- the `id` and the moment, no text. The
#: instruction collection propagates deletions like the graph; the traces themselves are unioned,
#: because nothing ever takes one back.
REMOVED = 'removed'


def merge_instructions(base, ours, theirs, rep) -> dict:
    """The account: `instructions` goes entry-wise with deletions propagating, the removal traces
    union by id, every other field by the plain rule."""
    out = {}
    for field in _keys(ours, theirs):
        if field == 'instructions':
            merged = _merge_entries(_get(base, field), _get(ours, field), _get(theirs, field), rep,
                                    _merge_instruction, _index_by_id, propagate_delete=True)
            out[field] = list(merged.values())
        elif field == REMOVED:
            merged = _merge_entries(_get(base, field), _get(ours, field), _get(theirs, field), rep,
                                    _merge_tombstone, _index_by_id)
            out[field] = list(merged.values())
        else:
            out[field] = _plain_field('instructions.json', field, _get(base, field),
                                      _get(ours, field), _get(theirs, field), rep)
    return {k: v for k, v in out.items() if v is not _MISSING}


def _index_by_id(items) -> dict:
    """A collection of records keyed by their `id`: the entries, and their removal traces."""
    if isinstance(items, list):
        return {e.get('id'): e for e in items if isinstance(e, dict) and e.get('id')}
    if isinstance(items, dict):
        return dict(items)
    return {}


def _merge_instruction(label, base, ours, theirs, rep):
    def resolve(key, b, o, t):
        if key in RESOLUTION_GROUP:
            return _resolution(label, key, b, o, t, ours, theirs, rep)
        return _plain_field(label, key, b, o, t, rep)
    return _merge_mapping(_as_dict(base), _as_dict(ours), _as_dict(theirs), rep, label, resolve)


def _merge_tombstone(label, base, ours, theirs, rep):
    """One removal trace: the id, and when it went away.

    A trace is not an entry anyone edits -- it is the record of one act, and nothing in the tools
    ever takes one back -- so the fields are resolved the plain way, with the moment going to the
    EARLIER side: two machines that removed the same entry independently differ only in the stamp,
    and whoever removed it first is when it left the account.
    """
    def resolve(key, b, o, t):
        if key == 'at':
            return _stamp(label, key, b, o, t, rep, pick=min)
        return _plain_field(label, key, b, o, t, rep)
    return _merge_mapping(_as_dict(base), _as_dict(ours), _as_dict(theirs), rep, label, resolve)


def _resolution(label, key, base_value, ours_value, theirs_value, ours, theirs, rep):
    """The resolution act, resolved as a unit: the newer `resolved_at` side wins, with its stamp."""
    got = _three_way(base_value, ours_value, theirs_value)
    if got is not _UNDECIDED:
        return got
    o_stamp, t_stamp = _get(ours, 'resolved_at'), _get(theirs, 'resolved_at')
    if all(isinstance(s, str) for s in (o_stamp, t_stamp)) and o_stamp != t_stamp:
        newer = 'ours' if o_stamp > t_stamp else 'theirs'
        rep.decide(f'{label}: 两侧都改了这条指令的解除结果 → 取 resolved_at 较新的一侧'
                   f'（{newer} {max(o_stamp, t_stamp)}）')
        winner = ours if o_stamp > t_stamp else theirs
        return _get(winner, key)
    # A revive CLEARS the stamp, and a cleared one can only meet a stamped one when both sides moved
    # this group: two owner acts on two machines, which no stamp in either record orders. Worth
    # saying in those words -- "resolved_at 分不出先后" would send the reader looking for two stamps.
    note = ('一侧把这条放回生效（收掉的时刻清空了）、另一侧又改过它 —— 两次机主动作撞了'
            if (o_stamp is None) != (t_stamp is None) else 'resolved_at 分不出先后')
    rep.conflict(label, key, ours_value, theirs_value, note)
    return _MISSING
