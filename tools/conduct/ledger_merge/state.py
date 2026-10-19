"""`state.json` -- the cycles: one line of work each, and the path of nodes it has finished.

One record per cycle, keyed by `id`, and the collection goes back out as a list (that is the shape
the file has). What this engine adds to the generic walk:

  * `path` is append-only: a prefix extends, two different tails are merged in the order walked, and
    the same node recorded twice with two different outcomes is a conflict (`_path` hands that rule to
    `_extend_or_union`)
  * `status` is `active` / `inactive`: two sides naming two different words really do disagree, and a
    conflict is what a human settles -- never a coin toss
  * no cycle entry is ever deleted: neither tool has a command that removes one, so a missing entry is
    "this side never had it" and the entry comes back (`propagate_delete` off)
"""

from .generic import (_MISSING, _UNDECIDED, _as_dict, _extend_or_union, _merge_collection,
                      _merge_mapping, _plain_field, _stamp, _three_way)


def merge_state(base, ours, theirs, rep) -> dict:
    """The cycle book: the `cycles` list goes entry-wise, every other field by the plain rule."""
    return _merge_collection('state.json', 'cycles', base, ours, theirs, rep,
                             resolve_entry=_merge_cycle, index_of=_index_cycles, as_list=True)


def _index_cycles(cycles) -> dict:
    if isinstance(cycles, list):
        return {c.get('id'): c for c in cycles if isinstance(c, dict) and c.get('id')}
    if isinstance(cycles, dict):
        return dict(cycles)
    return {}


def _merge_cycle(label, base, ours, theirs, rep):
    def resolve(key, b, o, t):
        if key == 'path':
            return _path(label, b, o, t, rep)
        if key == 'status':
            return _cycle_status(label, b, o, t, rep)
        if key == 'updated_at':
            return _stamp(label, key, b, o, t, rep, pick=max)
        if key == 'created_at':
            return _stamp(label, key, b, o, t, rep, pick=min)
        return _plain_field(label, key, b, o, t, rep)

    return _merge_mapping(_as_dict(base), _as_dict(ours), _as_dict(theirs), rep, label, resolve)


def _cycle_status(label, base_value, ours_value, theirs_value, rep):
    """`active` / `inactive`: the account carries exactly these two.

    Two sides that name two different ones really do disagree -- that is a conflict a human settles,
    never a coin toss.
    """
    got = _three_way(base_value, ours_value, theirs_value)
    if got is not _UNDECIDED:
        return got
    rep.conflict(label, 'status', ours_value, theirs_value,
                 'the two sides name two different words for the cycle status')
    return _MISSING


def _path(label, base_value, ours_value, theirs_value, rep):
    """`path` is append-only: a prefix extends, two different tails are merged in the order walked."""
    return _extend_or_union(
        label, 'path', base_value, ours_value, theirs_value, rep=rep,
        identity=lambda step: step.get('node'),
        same_act=lambda prev, step: prev.get('result') == step.get('result'),
        order_by=lambda step: (step.get('ended_at') or step.get('started_at') or ''),
        same_note=lambda tag, prev, step: f'the same node {str(tag)[:8]} has different results '
                                        f'on the two sides')