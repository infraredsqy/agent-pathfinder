"""`instructions.json` -- the standing instructions, and how far each one has got.

One record per instruction, keyed by `id`, and the collection goes back out as a list. What this
engine adds to the generic walk:

  * the resolution act is ONE thing: `state` / `resolved_at` / `resolved_by` are written by one code
    path (`instruct resolve`), so the side whose `resolved_at` is newer wins all three together.
    `note` deliberately does NOT travel with them: a hand-written note is never silently
    overwritten, so a `note` both sides changed is a conflict
  * the same `id` added by both sides with different content is an add/add collision: a conflict
"""

from .generic import (_MISSING, _UNDECIDED, _as_dict, _get, _merge_collection, _merge_mapping,
                      _plain_field, _three_way)

#: One `instruct resolve` writes these together; they are one act and move as one.
RESOLUTION_GROUP = ('state', 'resolved_at', 'resolved_by')


def merge_instructions(base, ours, theirs, rep) -> dict:
    """The instruction list: `instructions` goes entry-wise, every other field by the plain rule."""
    return _merge_collection('instructions.json', 'instructions', base, ours, theirs, rep,
                             resolve_entry=_merge_instruction, index_of=_index_instructions,
                             as_list=True)


def _index_instructions(items) -> dict:
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
    rep.conflict(label, key, ours_value, theirs_value, 'resolved_at 分不出先后')
    return _MISSING