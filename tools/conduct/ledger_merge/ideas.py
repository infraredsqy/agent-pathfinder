"""`ideas.json` -- the DAG: node statuses, the text fields, and the dependencies between nodes.

One record per node, keyed by the uuid. What this engine adds to the generic walk:

  * a `status` both sides moved goes UP a rung (`todo < shelved < completed`); a word outside the
    three is a conflict, never a guess
  * `dependencies` union, but only while NEITHER side removed an edge: an edge one side cut and the
    other side added is a fight about the same edge, and nobody can settle it but a human
  * `title` / `description` / `mode` / `tags` are "the later edit wins", read off `updated_at` (the
    one stamp every writer moves), and a pair of stamps that cannot be ordered is a conflict
  * a node `dag-manage rm` deleted on one side and left alone on the other stays deleted (this is the
    one record whose deletions propagate); deleted on one side and EDITED on the other is a conflict
  * after the merge, an edge that survives into the result while the node it points at does not is
    flagged (`_flag_broken_dependency`) -- the pipeline reads a dangling edge as SATISFIED
"""

from .generic import (_MISSING, _UNDECIDED, _as_dict, _fmt, _get, _merge_collection,
                      _merge_mapping, _plain_field, _stamp, _three_way)

#: The rungs, low to high: what the graph's three words say about how far along a node is.
NODE_STATUS_ORDER = ('todo', 'shelved', 'completed')


def merge_ideas(base, ours, theirs, rep) -> dict:
    """The graph's file: the node table goes entry-wise (deletions propagate), the rest is plain."""
    return _merge_collection('ideas.json', 'nodes', base, ours, theirs, rep,
                             resolve_entry=_merge_node, index_of=_index_nodes,
                             propagate_delete=True, after=_flag_broken_dependency)


def _flag_broken_dependency(merged, ours, theirs, rep) -> None:
    """Flag an edge the MERGE broke: it survives in the result, the node it points at does not.

    `dag-manage rm` keeps ONE machine's graph consistent -- it refuses to delete a node others depend
    on unless `--force`, and with `--force` it strips the edge from every dependent. So a dangling
    reference is what a cross-machine merge can still produce: one side deletes X (cleaning up its own
    edges), the other side's new edge points at X, and no single record was touched by both.

    Worth stopping for, because nothing else in the pipeline would notice in time: `dag-manage validate`
    reports it (`FAIL check=no_dangling`), but a dangling dependency reads as SATISFIED to the ready
    rule, so the dependent node is dispatchable in the meantime -- the pipeline would run a node
    whose prerequisite was deleted, quietly. Only what THIS merge breaks is flagged: an edge that was
    already dangling on one side belongs to that side, and is not a reason to stop a pull.
    """
    merged_nodes = merged.get('nodes') or {}
    ids = set(merged_nodes)
    for uid, row in merged_nodes.items():
        if not isinstance(row, dict):
            continue
        for dep in row.get('dependencies') or []:
            if dep in ids or _was_broken(ours, uid, dep) or _was_broken(theirs, uid, dep):
                continue
            label = f'Node {str(uid)[:8]} (a dependency was deleted by the merge)'
            rep.conflict(label, 'dependencies', row.get('dependencies'), dep,
                         f'the merge deleted {str(dep)[:8]}, while this slot\'s edge still points at it: '
                         f'`dag-manage undepend {str(uid)[:8]} --on {str(dep)[:8]}` takes that edge off, '
                         f'or put the deleted node back. validate reports FAIL check=no_dangling, and '
                         f'until then the edge reads as satisfied (the node shows ready)')


def _was_broken(side, uid, dep) -> bool:
    """Did this side's OWN file already carry a broken `uid -> dep` (the edge, but no such node)?

    The side that still HAS the node is not broken: that is the side whose edge the merge turned
    dangling by deleting the node on the other side.
    """
    nodes = _index_nodes(_get(side, 'nodes'))
    row = nodes.get(uid)
    return (isinstance(row, dict) and dep in (row.get('dependencies') or [])
            and dep not in nodes)


def _index_nodes(nodes) -> dict:
    if isinstance(nodes, dict):
        return dict(nodes)
    if isinstance(nodes, list):        # a hand-written file: index by the id field
        return {n.get('id') or n.get('uuid'): n for n in nodes if isinstance(n, dict)}
    return {}


def _merge_node(label, base, ours, theirs, rep):
    def resolve(key, b, o, t):
        if key == 'status':
            return _node_status(label, b, o, t, rep)
        if key == 'dependencies':
            return _dependencies(label, b, o, t, rep)
        if key in ('title', 'description', 'mode', 'tags'):
            return _text_field(label, key, b, o, t, ours, theirs, rep)
        if key == 'updated_at':
            return _stamp(label, key, b, o, t, rep, pick=max)
        if key == 'created_at':
            return _stamp(label, key, b, o, t, rep, pick=min)
        return _plain_field(label, key, b, o, t, rep)
    return _merge_mapping(_as_dict(base), _as_dict(ours), _as_dict(theirs), rep, label, resolve)


def _node_status(label, base_value, ours_value, theirs_value, rep):
    got = _three_way(base_value, ours_value, theirs_value)
    if got is not _UNDECIDED:
        return got
    o, t = ours_value, theirs_value
    if o in NODE_STATUS_ORDER and t in NODE_STATUS_ORDER:
        higher = max(o, t, key=NODE_STATUS_ORDER.index)
        rep.decide(f'{label}: status takes the higher rung {higher!r}'
                   f' (ours={ours_value!r} theirs={theirs_value!r})')
        return ours_value if higher == o else theirs_value
    rep.conflict(label, 'status', ours_value, theirs_value,
                 'status is not in the vocabulary; the rung order cannot trim it')
    return _MISSING


def _dependencies(label, base_value, ours_value, theirs_value, rep):
    got = _three_way(base_value, ours_value, theirs_value)
    if got is not _UNDECIDED:
        return got
    if not isinstance(ours_value, list) or not isinstance(theirs_value, list):
        rep.conflict(label, 'dependencies', ours_value, theirs_value, 'not a list, cannot trim')
        return _MISSING
    if not isinstance(base_value, list):
        rep.conflict(label, 'dependencies', ours_value, theirs_value,
                     'no base (both sides added), so an added edge cannot be told from a removed one')
        return _MISSING
    added_o = [d for d in ours_value if d not in base_value]
    removed_o = [d for d in base_value if d not in ours_value]
    added_t = [d for d in theirs_value if d not in base_value]
    removed_t = [d for d in base_value if d not in theirs_value]
    if not removed_o and not removed_t:
        merged = list(ours_value) + [d for d in theirs_value if d not in ours_value]
        rep.decide(f'{label}: dependencies both sides only added edges -> take the union {_fmt(merged)}')
        return merged
    rep.conflict(label, 'dependencies', ours_value, theirs_value,
                 f'ours added {_fmt(added_o)} removed {_fmt(removed_o)}; '
                 f'theirs added {_fmt(added_t)} removed {_fmt(removed_t)}')
    return _MISSING


def _text_field(label, key, base_value, ours_value, theirs_value, ours, theirs, rep):
    """A text field both sides edited: the side with the newer `updated_at` is the later edit."""
    got = _three_way(base_value, ours_value, theirs_value)
    if got is not _UNDECIDED:
        return got
    o_stamp, t_stamp = _get(ours, 'updated_at'), _get(theirs, 'updated_at')
    if all(isinstance(s, str) for s in (o_stamp, t_stamp)) and o_stamp != t_stamp:
        newer = 'ours' if o_stamp > t_stamp else 'theirs'
        rep.decide(f'{label}: {key} both sides changed it -> take the side with the newer updated_at '
                   f'({newer} {max(o_stamp, t_stamp)})')
        return ours_value if o_stamp > t_stamp else theirs_value
    rep.conflict(label, key, ours_value, theirs_value, 'updated_at cannot order the two')
    return _MISSING