"""The DAG ready set, the tag filter, and the pick.

What a cycle may pick next: the ranked ready set, who another cycle is
holding, what a filter set hides, and the reason nothing can be picked.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import seam
from .store import _holding_ids, _ledger, load_dag, load_state, normalize_mode


# ── DAG ──────────────────────────────────────────────────────────────────────

def dag_ready_nodes(project: Path, assume_completed=(), dag: dict | None = None) -> list:
    """Ready = a `todo` node whose every dependency is completed (or absent).

    The dependency rule is mirrored from `tools/dag/graph.py`'s `_is_ready` on purpose: two tools
    answering "is this node ready" differently means a node can be runnable in one and stuck in the
    other -- which is how a hand-built or partial graph ends up with work the DAG calls ready and the
    pipeline refuses to start. A dangling dependency id is `dag-manage validate`'s job to catch.

    `shelved` is out of play and never ready, and neither is anything downstream of it: a node that
    came back not delivered parks its whole subtree until a human revives it. `completed` is done.
    Both are the account's own words here -- `load_dag` hands them over exactly as they are written.

    `assume_completed` names nodes to read as completed for this one call: the close question
    is "the node just worked on is done -- is there anything left?", and the close command is what
    writes that status.

    `dag` is the already-read graph when the caller has one: the close reads the graph once and hands
    the same copy to everything it asks on that path.

    **Being blocked by a tag is not an unmet dependency**: a cycle's filter set
    (`node_in_tag_scope`) only drops rows, and the DAG does not move for it.
    """
    nodes = (dag if dag is not None else load_dag(project)).get('nodes', {})
    assumed = {n for n in assume_completed if n}
    out = []
    for uid, n in nodes.items():
        if n.get('status') in ('completed', 'shelved') or uid in assumed:
            continue
        deps = n.get('dependencies', []) or []
        if all((nodes.get(d) or {}).get('status') == 'completed'
               or d in assumed or d not in nodes for d in deps):
            out.append({'id': uid, 'title': n.get('title', ''), 'mode': normalize_mode(n.get('mode')),
                        'tags': [t for t in (n.get('tags') or []) if isinstance(t, str)]})
    return out


# ── Node tags and cycle routing ──────────────────────────────────────────────
# A cycle's `tags` are its **selection range**: they draw the line around which nodes it picks
# people from. There is one rule -- a node with no tag at all, or one whose tag intersects the
# filter set, is in range.
#
# The conservative direction is to **rather let one through**: picking one node too many costs one
# node's time, while blocking by mistake leaves a line's work with nobody to do it forever.
#
# The filter only lands on "pick the next node": a node already running runs to the end, and the
# DAG's dependencies are not changed by a word -- an `art` node can still wait for a `writing` node
# to finish before it is ready, and what should wait still waits. Blocking only affects "who is
# picked": no status moves, nothing is marked shelved, no dependency edge moves.

def node_in_tag_scope(node: dict, cycle_tags=None) -> bool:
    """Is this node inside this cycle's selection range?

    No filter set means the whole graph is in range, byte-for-byte as it was before the plugin
    existed; a node with no tag is visible everywhere.
    """
    if not cycle_tags:
        return True
    tags = [t for t in (node.get('tags') or []) if isinstance(t, str)]
    if not tags:
        return True
    return bool(set(tags) & set(cycle_tags))


def tag_blocked_lines(blocked: list) -> list:
    """The ready nodes the filter set hides, one per line: title, id, and the tags on it.

    This text is read out to a human as it stands, so it is written the way a human reads: title,
    id, tags, one node per line.
    """
    lines = []
    for n in blocked:
        tags = [t for t in (n.get('tags') or []) if isinstance(t, str)]
        lines.append(f'  - {n.get("title") or "(untitled)"} ({n["id"]}) tags={tags}')
    return lines


def cross_line_reason(tags, blocked: list) -> str:
    """The sentence a human gets when another line is ready and this one has nobody to pick.

    This reason is read to the user as it stands, so it does exactly three things: name which nodes
    are ready, say which tags are hiding them, and give a command that can be copied straight to
    take that line. Stopping here is **handing the choice back to the human**: one line's work is
    done, and whether and when another line starts is his to say.
    """
    need = []
    for n in blocked:
        for t in (n.get('tags') or []):
            if t not in need:
                need.append(t)
    head = (f'This cycle has no ready node left in its filter set {list(tags)}, but the graph still '
            f'has {len(blocked)} ready node(s), only hidden outside by their tags:')
    tail = ('One line has finished its work; whether and when another line starts is yours to '
            'decide.\n'
            f'Open another cycle to take it: pf '
            f'cycle start <slug> --tags {",".join(need)}')
    return '\n'.join([head, *tag_blocked_lines(blocked), tail])


def _rank_ready_nodes(project: Path, exclude=None, exclude_many=(), assume_completed=(),
                      dag: dict | None = None) -> list:
    """The ready set in the DAG's own priority order, unfiltered and untruncated.

    **The ready set comes from `dag_ready_nodes`, and the ranking comes from `dag-manage`.**
    Splitting it this way is what keeps the round-end question honest:

    * `dag-manage list --ready` is a browsing view. It does not exclude COMPLETED nodes, and it
      cannot be told "read this one node as finished" -- which is precisely what the close needs,
      because it asks "is anything left?" *before* the close commands have written the round node's
      status. Excluding the node hides it; assuming it completed un-blocks its dependents. Those
      are different questions and only the second one is the right one.
    * The ranking is a single implementation detail this module must NOT duplicate, so the ready
      set is filtered out of the tool's full (already sorted) listing.
    * A tag filter is applied to this list by `ranked_ready_nodes`/`tag_blocked_ready_nodes`, i.e.
      AFTER the DAG has ranked it: a filter set removes rows, it never re-ranks them.

    `exclude` / `exclude_many` drop ids outright (the nodes a line holds, the node being closed).
    """
    ready = dag_ready_nodes(project, assume_completed=assume_completed, dag=dag)
    drop = set(exclude_many or ())
    if exclude:
        drop.add(exclude)
    if drop:
        ready = [n for n in ready if n.get('id') not in drop]
    if not ready:
        return []
    try:
        # This call goes through the seam (`seam.capture`): **a child process has this one road**, and
        # patching the entry's `_capture` / `_run` as documented stops it. It sits after the ready
        # set: first work it out, then ask for the ranking.
        out = seam.capture(seam.dag('list', '-n', '0'), project)
    except RuntimeError:
        # The ranking is a bonus: when dag-manage cannot answer (timeout, non-zero exit, will not
        # start), the ready set is handed over in the DAG record's own order -- "no ranking read"
        # must not stall the pick.
        return ready
    order = {}
    if out:
        try:
            rows = json.loads(out)
            if isinstance(rows, list):
                order = {r.get('id'): i for i, r in enumerate(rows) if r.get('id')}
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    # How child output is decoded is the seam's own convention (always errors='replace'): this
    # machine's console code page is GBK, and PYTHONIOENCODING is not necessarily set. Rather a
    # replacement character in a node title (ids are ASCII, so the pick is unaffected) than a
    # decoding error capsizing the whole pipeline.
    ready.sort(key=lambda n: order.get(n.get('id'), len(order) + 1))
    return ready


def node_holders(project: Path) -> dict:
    """{node_id: cycle_id} -- the set of nodes each cycle holds right now.

    A node belongs to one cycle at a time: once a cycle takes hold of it, the other lines cannot
    pick it at this step. This holding table is the one reading shared by "pick the next node" and
    "who can pick whom" -- both halves of the pick (this line's ready set, the ready set the filter
    set hides) have to be deducted against the same table, or one node is counted as pickable on
    one side and as not-its-turn on the other, two sentences contradicting each other.

    Only what is still held counts: the moment a node closes out, the cycle puts it down (the DAG's
    own status takes it out of the ready set). The cycle's own status is not part of the judgement
    -- a cycle parked half way still occupies the node it did not finish, until somebody closes it
    out.
    """
    state = load_state(project)
    holders = {}
    for c in state.get('cycles', []):
        for node in _holding_ids(c):
            holders.setdefault(node, c.get('id'))
    return holders


def _free_ready(project: Path, ranked: list, mine=None) -> list:
    """The ready set, minus the nodes another cycle holds right now.

    `mine` is the node(s) in the caller's hand: it belongs to the caller, and must not be deducted
    as "held by somebody else" -- it is not held by anybody else.
    """
    holders = node_holders(project)
    keep = set(mine) if isinstance(mine, (list, tuple, set)) else ({mine} if mine else set())
    return [n for n in ranked if n.get('id') in keep or n.get('id') not in holders]


def ranked_ready_nodes(project: Path, limit: int | None = None, exclude=None, exclude_many=(),
                       assume_completed=(), tags=None, dag: dict | None = None) -> list:
    """The ready set a cycle may pick from, in the DAG's own priority order.

    `tags` is the cycle's filter set. It only drops rows off the DAG's ranking; the ranking itself
    stays `dag-manage`'s. `None`/empty means the whole graph is in range, exactly as before tags
    existed. Nodes another cycle is holding are not in the set at all: the DAG calls them ready
    (their dependencies are met) and they are not available.

    `limit` left out hands over the whole ranking: how many slots a line takes at once is the
    caller's business (the `parallel` policy, and the slots `cycle start` pins itself). A reader
    that truncates with a number of its own answers both "is the node I named there" and "is there
    any work left in the graph" wrong.
    """
    ranked = _rank_ready_nodes(project, exclude=exclude, exclude_many=exclude_many,
                               assume_completed=assume_completed, dag=dag)
    ranked = _free_ready(project, ranked, [x for x in (exclude, *exclude_many) if x])
    visible = [n for n in ranked if node_in_tag_scope(n, tags)]
    return visible if limit is None else visible[:limit]


def tag_blocked_ready_nodes(project: Path, tags, exclude=None, exclude_many=(),
                            assume_completed=(), dag: dict | None = None) -> list:
    """The ready nodes this cycle's filter set hides -- the other half of the same ranking.

    They are what turns "no node to pick" into a sentence a human can act on: the work is right
    there, it belongs to another line, and starting that line is his call (see `cross_line_reason`).
    Empty when there is no filter set, and that early exit is also what keeps an unfiltered close
    from paying for a second `dag-manage` call.
    """
    if not tags:
        return []
    ranked = _rank_ready_nodes(project, exclude=exclude, exclude_many=exclude_many,
                               assume_completed=assume_completed, dag=dag)
    return [n for n in ranked if not node_in_tag_scope(n, tags)]


def holding_ready_nodes(project: Path, exclude=None, exclude_many=(), assume_completed=(),
                        dag: dict | None = None) -> list:
    """Ready nodes another cycle is holding right now: work this cycle cannot pick.

    A line holds them, a tag never blocked them -- the two read differently to a human, and
    `blocked_reason` is where that difference is spelled out.
    """
    holders = node_holders(project)
    drop = set(exclude_many or ())
    if exclude:
        drop.add(exclude)
    ranked = _rank_ready_nodes(project, exclude=exclude, exclude_many=exclude_many,
                               assume_completed=assume_completed, dag=dag)
    return [n for n in ranked if n.get('id') not in drop and n.get('id') in holders]


def blocked_reason(project: Path, tags, tagged: list, holding: list) -> str:
    """Why this cycle has nothing to pick, in the terms of whoever is reading it.

    Two reasons look alike from inside the pipeline (no node in hand) and nothing alike to a human:
    the filter set hides a node, or another cycle is running it. Reporting the second as the first
    sends the reader looking at tags that are not in the way.
    """
    lines = []
    if tagged:
        lines.append(cross_line_reason(tags, tagged))
    if holding:
        holders = node_holders(project)
        holding_lines = []
        for n in holding:
            owner = holders.get(n.get('id')) or '?'
            holding_lines.append(f'  - {n.get("title") or "(untitled)"} ({n["id"]}): cycle '
                                 f'{owner} is holding it')
        lines.append('These nodes in the graph are ready, but another cycle already occupies '
                     'them, so this line cannot pick them:\n'
                     + '\n'.join(holding_lines)
                     + '\nTo get them, wait for that line to run them out (another line being '
                       'busy is not a fault of this line).')
    return '\n'.join(lines)


def pick_node(project: Path, override: str | None = None, exclude=None, exclude_many=(),
              assume_completed=(), tags=None, dag: dict | None = None) -> dict | None:
    """The next node: an explicit instruction wins, otherwise the top of the ready ranking.

    `--node <id>` is the owner naming a node by hand, so the filter set has no say over it: a node
    that is ready is picked whether or not it is in range, and the caller says out loud that it
    came from outside the filter (the alternative -- letting a filter silently swallow a node
    somebody typed out in full -- hides the one decision a human just made). What still refuses is
    a name that is not ready at all, which is the meaning this flag already had.
    """
    ranked = ranked_ready_nodes(project, exclude=exclude, exclude_many=exclude_many,
                                assume_completed=assume_completed, tags=tags, dag=dag)
    if override:
        for n in ranked:
            # Exact match only: a prefix (`--node ab` hitting `abc...`) would aim a round at a
            # node nobody named -- and node ids are uuids, so full ids are always at hand.
            if n['id'] == override:
                return n
        if tags:
            # Named and ready, but outside the filter set: look again in the unfiltered ranking
            # rather than reading "no such ready node".
            for n in ranked_ready_nodes(project, exclude=exclude, exclude_many=exclude_many,
                                        assume_completed=assume_completed, dag=dag):
                if n['id'] == override:
                    return n
        # Named but not ready/present: refuse rather than silently substituting another node.
        return None
    return ranked[0] if ranked else None


def node_spec_path(project: Path, node_id: str) -> str:
    """`<project>/.pf/nodes/<id>/spec.md`, written the way a human reads it (forward slashes).

    This is the one place this path is worked out: the generated spec reports it at the end,
    `insertion.write_spec` writes to it, and the inserted node's directory is made next to it.
    `.pf` and the `nodes/` under it are settled by `shared/ledger.py`.
    """
    return (_ledger().nodes_dir_for(project) / node_id / 'spec.md').as_posix()


def mode_for(node_id: str | None, dag: dict) -> str:
    """The node's own mode, and `auto` when the record leaves the key out.

    Nothing overrides it: the node's mode decides on its own whether the line keeps going after it
    (see `planner.stop_criterion`), so there is no line-level mode left to take precedence over.
    The fallback goes through `normalize_mode`; it reads the graph that was handed in, so the
    caller does not read the disk again for this one value.
    """
    node = ((dag.get('nodes') or {}).get(node_id) or {}) if node_id else {}
    return normalize_mode(node.get('mode'))


def node_row(dag: dict, node_id: str) -> dict:
    """That row in the graph, with the id handed in -- the id is the record's key and is not in the
    row.

    This is the one place a node record's shape is taken: the stop criterion reads this very copy,
    and the entries of the dispatch road grow out of it too. When the graph has no
    such row, it hands over an empty row carrying only the id (callers read with `.get()`, and
    anything missing reads as the default).
    """
    row = dict((dag.get('nodes') or {}).get(node_id) or {})
    row['id'] = node_id
    return row
