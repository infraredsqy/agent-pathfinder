#!/usr/bin/env python3
"""The git merge driver for the pf ledger: a three-way merge of the files under `.pf/`.

    python ledger-merge.py <base> <ours> <theirs>            # git's %O %A %B
    python ledger-merge.py <base> <ours> <theirs> <path>     # ... and %P, the ledger's own path
    python ledger-merge.py --dry-run <base> <ours> <theirs> [<path>]

Wired up as `merge.pf.driver`; the result is written into `<ours>` and git carries on.

Which ledger file is this? (the fourth argument)
------------------------------------------------
`%A` is what the result must be written into -- but under git it is NOT the worktree file: for a
content merge git materializes all three sides as its own temp files (`.merge_file_XXXXXXXX`) and
copies `%A` into place afterwards. So the driver is told which ledger it is looking at by `%P`, the
path in the repo, and the recommended `merge.pf.driver` command passes it as the fourth
argument. Name first (`%P`, then `%A`/`%B`/`%O` for a hand-run), shape second (a `nodes` /
`cycles` / `instructions` key, else a ticket), and a file that is neither is refused with exit 2.

Why a driver at all
-------------------
Two machines can each run a pipeline against their own copy of the same records. Every
writer rewrites its WHOLE file from an in-memory dict (`json.dump(..., indent=2, ensure_ascii=False)`
-- plus `sort_keys=True` for `ideas.json`, CRLF line endings, no trailing newline), so a line-level
merge of two ledgers is not a merge: two machines that each touched one field produce two files that
differ on every line. `git merge` would then stop on a conflict that is not one, or worse, `ort`
would "cleanly" produce a nonsense mixture of the two files. So the merge is done on the parsed
records, and the result is written back through the tools' OWN writers, byte for byte the way
`state-manage.py` / `dag-manage.py` would have written it -- `conduct_state.store` for its three
files, and dag-manage's `_write` for the graph (the one writer that sorts keys).

One record per entry, and what identifies it
--------------------------------------------
    .pf/ideas.json          nodes{}          key: the node uuid
    .pf/state.json          cycles[]         key: `id`
    .pf/instructions.json   instructions[]   key: `id`
                            removed[]        key: `id` -- the trace a removal leaves (id + moment)
    .pf/signals/<cyc>/<node>.json            the file IS one record: one node's ticket

Every rule below runs ONLY when both sides changed a field to different values; `ours == base` takes
theirs, `theirs == base` takes ours, and a field only one side touched is not a question at all.

AUTO-MERGE  (no question asked)
-------------------------------
  * a field only one side changed                     -> that side
  * an entry only one side ever HAD                   -> that side (an addition)
  * an entry `dag-manage rm` / `instruction rm`
    deleted, with the other side leaving it alone     -> deleted (both collections propagate
                                                         deletions; see NOTES for the three other
                                                         shapes)
  * instructions `removed` traces                     -> union, keyed by `id` (a trace is never
                                                         dropped)
  * ideas node `status`                               -> the higher rung:
                                                         todo < shelved < completed
  * ideas node `dependencies`                         -> union, when NEITHER side removed an edge
  * ideas node `title`/`description`/`mode`/`tags`    -> the side with the newer `updated_at`
  * `updated_at` / `created_at`                       -> the later / the earlier stamp
  * state cycle `path` (append-only)                  -> the longer list when one is a prefix of the
                                                         other; else the two tails merged, ordered by
                                                         `ended_at` (`started_at` as fallback)
  * state cycle `status`                              -> the two words are `active` / `inactive`;
                                                         two sides naming different ones is a fight
  * instructions resolution (state + its stamp)       -> the side whose `resolved_at` is newer
  * signals                                           -> a terminal status beats a non-terminal one;
                                                         otherwise the newer %A/%B file mtime (tie: ours)

FLAG  (nothing is written, exit 1, a human decides)
---------------------------------------------------
  * ideas node `dependencies` when a side removed an edge and the two lists still differ
  * ideas node `status` written in a word outside the vocabulary
  * ideas node: any other field both sides changed to different values
  * ideas node: `dag-manage rm` on one side, an EDIT of that node on the other
  * ideas node: a dependency the MERGE left dangling -- the edge is in the result and the node it
    points at is not (deleted by this merge, while the other side added or kept the edge). The
    pipeline reads a dangling edge as SATISFIED, so the dependent node would become dispatchable with
    nothing said until somebody runs `dag-manage validate`
  * state cycle: `title` / `tags` / `policies` both changed to different values (`status` is
    reachable only as a real disagreement: two sides naming two different words)
  * instructions: the same `id` added by BOTH sides with different content (an add/add collision)
  * instructions: an entry one side removed with `rm`, an EDIT of that entry on the other -- the
    graph's delete-vs-edit shape, for the same reason (a human said "take it off", a machine said
    something else about it)
  * instructions: any field both sides changed, except the resolution group. The fields an older
    tool wrote (`applied` / `applied_rounds` / `superseded_by`) are ordinary fields with no rule of
    their own, so changing one on both sides lands here too

NOTES  (deliberate consequences, worth knowing before trusting a clean merge)
------------------------------------------------------------------------------
  * A side that is missing (the whole FILE) is "take the side that exists", so a file one side
    deleted comes back. That is about the files git hands the driver, not about entries inside one.
  * Deletions propagate for the two collections a human really deletes from: the GRAPH
    (`.pf/ideas.json`, `dag-manage rm`) and the standing instructions (`instruction rm`). One side
    takes an entry off, the other leaves it alone -> it is gone. The other three shapes are the ones
    listed above (added / edited-that-way -> conflict / removed both ways). Cycles keep the old union
    rule: no tool deletes one, so a resurrection is a no-op rather than a surprise.
  * An instruction removal leaves a trace -- the id and the moment, no text, under the account's own
    key -- and it travels like any other record (the union rule above). It is what keeps a ledger two
    machines pull from each other from minting the same id twice: an id is minted one past the
    highest one ever handed out, and after a removal the entry list alone no longer remembers the id
    it just gave back. No view of the account shows traces.
  * A node deleted while another node still depends on it is checked (`_flag_broken_dependency`), but
    only for edges THIS merge breaks: an edge already dangling on one side stays that side's problem,
    and `dag-manage validate` is the place for the graph as a whole.
  * instructions: `state`/`resolved_at` are written by ONE code path as one act --
    `cmd_instruction_resolve` archives, `cmd_instruction_revive` clears the same two fields back to
    in force -- so the rule reads them as a unit: a field only one side changed is that side's, and
    two sides that both changed it are ordered by `resolved_at`, the newer act winning for both.
    A revive CLEARS that stamp, so a revive on one side against a withdrawal on the other has nothing
    left to order the two acts by: that is a conflict, and a human settles it. Where the other side
    left the two alone -- the ordinary revive -- the side that wrote them wins whole, and the merged
    record cannot come back half-archived.
    `note` deliberately does NOT travel with them: a hand-written note is never silently
    overwritten, so a `note` both sides changed is a conflict.
  * instructions: an older tool's bookkeeping (`applied` / `applied_rounds` / `superseded_by`, and the
    hand an archive used to record in `resolved_by`) is not claimed by anything any more, so the
    driver gives it no rule. A record still carrying those fields merges as it stands and needs no
    repair: a reader treats any `state` other than `live` as archived, and these fields are ordinary
    data nobody reads -- which is also why a merge never rewrites one.
  * Key order in the merged file: ours', with keys only theirs has appended (`ideas.json` is sorted
    by its own writer). An existing record is never reordered, so a merge that decides nothing new
    leaves the bytes alone -- and `main` skips the write entirely when they are unchanged.
  * A ticket (signals) is one report and is never resolved field by field against a rival: recency
    decides it, with a terminal status privileged.

Exit codes, and what git does with them
---------------------------------------
    0   merged and written into <ours> (or nothing needed doing)
    1   a human has to decide: NOTHING is written, so git keeps <ours> and records the conflict
    2   usage error, or a file that cannot be read as a ledger
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
#: dag-manage owns `.pf/ideas.json`, so the driver writes it through dag-manage's own `_write`. The
#: same resolution rule as `conduct_pipeline.py`: the sibling `dag/` of the shipped tree, else the
#: documented `DSH_CONDUCT_DAG_MANAGE` (a review draft or a drill copy points at the real tool).
DAG_MANAGE = Path(os.environ.get('DSH_CONDUCT_DAG_MANAGE')
                  or HERE.parent / 'dag' / 'dag-manage.py')

KIND_IDEAS = 'ideas'
KIND_STATE = 'state'
KIND_INSTRUCTIONS = 'instructions'
KIND_SIGNAL = 'signal'

EXIT_OK = 0
EXIT_CONFLICT = 1
EXIT_USAGE = 2

#: The rungs, low to high: what the graph's three words say about how far along a node is.
NODE_STATUS_ORDER = ('todo', 'shelved', 'completed')
#: One act of archiving (or putting back) writes these together, and the driver resolves them as a
#: unit. An archived entry used to record the hand that did it as well; nothing writes that any more,
#: so it is ordinary data with no rule of its own (see NOTES).
RESOLUTION_GROUP = ('state', 'resolved_at')
#: instructions.json: the key the trace a removal leaves lives under -- the `id` and the moment, no
#: text. The instruction collection propagates deletions like the graph; the traces themselves are
#: unioned, because nothing ever takes one back.
REMOVED = 'removed'
#: 终态：这一格的活有结论了 —— `completed` 交付了，`failed` / `stopped` 没交付。
#: `pending` 是还没结论的记号（重试留下的），永远进不了这张表。
TERMINAL_SIGNAL_STATUSES = ('completed', 'failed', 'stopped')

_MISSING = object()          # the key is not in the record at all (an explicit null IS a value)
#: An entry one side DELETED, as a value in a conflict line (`_fmt` knows how to print it).
_DELETED = object()
#: `_three_way`'s "no decision" answer. It is NOT `None`: JSON null is a perfectly ordinary value
#: here (`cycle`, `node` and `resolved_at` are null most of the time), and using
#: `None` for both would turn every pair of equal nulls into a conflict.
_UNDECIDED = object()


# ═══════════════════════════════════════════════════════════════════════════════
#  The tools this reuses: their reader, their writers, their vocabularies
# ═══════════════════════════════════════════════════════════════════════════════

_STORE = None
_DAG = None


def _load_by_path(name: str, path: Path, package_dir: Path | None = None):
    """Load a module (or package) by path, registered so relative imports resolve."""
    kwargs = {'submodule_search_locations': [str(package_dir)]} if package_dir else {}
    spec = importlib.util.spec_from_file_location(name, path, **kwargs)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod                      # a package needs this before exec_module
    spec.loader.exec_module(mod)
    return mod


def store():
    """`conduct_state.store`: the tool's own reader and atomic writer, loaded by path.

    Under a private name, so a caller that already has `conduct_state` loaded (the test harness, an
    in-process launcher) keeps its own copy.
    """
    global _STORE
    if _STORE is None:
        pkg = _load_by_path('pf_ledger_conduct_state', HERE / 'conduct_state' / '__init__.py',
                            HERE / 'conduct_state')
        _STORE = pkg.store
    return _STORE


def dag_manage():
    """`dag-manage.py`: it owns `ideas.json`, and its `_write` is the only writer that sorts keys."""
    global _DAG
    if _DAG is None:
        if not DAG_MANAGE.exists():
            raise FileNotFoundError(f'dag-manage.py not found at {DAG_MANAGE}')
        _DAG = _load_by_path('pf_ledger_dag_manage', DAG_MANAGE)
    return _DAG


def _read(path: Path, label: str):
    """One side of the merge, or None when the file is absent or empty.

    git hands an EMPTY file for an add/add, and a missing one for "this side has nothing"; neither is
    an error. A file that exists but is not JSON IS one: the tool's own reader reports it and exits
    non-zero, and so do we.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        return store()._read_json(path, label)
    except SystemExit:
        print(f'ledger-merge: {label} 不是合法 JSON，不写任何东西', file=sys.stderr)
        raise


def _write(kind: str, path: Path, data) -> None:
    """Write the merged ledger through the writer that owns that file, under the project's lock.

    This driver is the one ledger writer in the tree that did NOT hold `.pf/meta/ledger.lock`:
    every mutating command (conduct / dag / issue) wraps its read-modify-write in it, so a merge
    landing while a command is mid-write could interleave with it -- the driver rewrites the WHOLE
    file, and a whole-file write racing another whole-file write silently erases one side's
    records. The lock is taken through `conduct_state.store` itself (same path, same msvcrt/fcntl
    protocol, same give-up-loudly behaviour), and git's cwd -- the worktree the merge runs in --
    maps to the main tree by the same rule the tools use, which is where the lock (and the ledger)
    live. A lock that stays busy makes the store exit non-zero, git marks the conflict, and a human
    reruns the merge: the right outcome for a write we could not make exclusively.
    """
    with store()._ledger_lock():
        if kind == KIND_IDEAS:
            dag = dag_manage()
            # dag-manage's `_write` writes to ITS OWN module paths (`_ensure_dirs`/`mkstemp` want the
            # directory, `IDEAS_JSON` the target), so aim them at the file being merged. This is the same
            # three-global patch the test harness uses to drive dag-manage in process.
            dag.DATA_DIR = path.parent
            dag.NODES_DIR = path.parent / 'nodes'
            dag.IDEAS_JSON = path
            dag._write(data)
            return
        # state.json / instructions.json / a signal: exactly what the CLI writes them with.
        store()._atomic_write(path, data)


def _serialize(kind: str, data) -> bytes:
    """What `_write` will leave on disk, as bytes -- for "did this merge change anything at all".

    Mirrors the writers: `indent=2, ensure_ascii=False` (+ `sort_keys=True` for ideas), the platform
    line separator (CRLF on this machine -- the tools write in text mode), no trailing newline. A
    test pins all four kinds against the real writers.
    """
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=(kind == KIND_IDEAS))
    return text.replace('\n', os.linesep).encode('utf-8')


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


#: The collection key each file is built around -- the shape that names it when the name cannot.
_COLLECTION_KINDS = {'nodes': KIND_IDEAS, 'cycles': KIND_STATE, 'instructions': KIND_INSTRUCTIONS}


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


# ═══════════════════════════════════════════════════════════════════════════════
#  The decisions, and how they are reported
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
#  ideas.json -- the DAG
# ═══════════════════════════════════════════════════════════════════════════════

def _merge_ideas(base, ours, theirs, rep) -> dict:
    out = {}
    for key in _keys(ours, theirs):
        if key == 'nodes':
            out[key] = _merge_entries(_get(base, key), _get(ours, key), _get(theirs, key), rep,
                                      _merge_node, _index_nodes, propagate_delete=True)
        else:
            out[key] = _plain_field('ideas.json', key, _get(base, key), _get(ours, key),
                                    _get(theirs, key), rep)
    merged = {k: v for k, v in out.items() if v is not _MISSING}
    _flag_broken_dependency(merged.get('nodes') or {}, ours, theirs, rep)
    return merged


def _flag_broken_dependency(merged_nodes, ours, theirs, rep) -> None:
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
    ids = set(merged_nodes)
    for uid, row in merged_nodes.items():
        if not isinstance(row, dict):
            continue
        for dep in row.get('dependencies') or []:
            if dep in ids or _was_broken(ours, uid, dep) or _was_broken(theirs, uid, dep):
                continue
            label = f'节点 {str(uid)[:8]}（依赖被合并删掉了）'
            rep.conflict(label, 'dependencies', row.get('dependencies'), dep,
                         f'合并删掉了 {str(dep)[:8]}，这一格的边还指着它：'
                         f'`dag-manage undepend {str(uid)[:8]} --on {str(dep)[:8]}` 摘掉这条边，'
                         f'或把被删的节点放回来。validate 会报 FAIL check=no_dangling，而在那之前'
                         f'这条边被当成已满足（节点会显示 ready）')


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
        rep.decide(f'{label}: status 按偏序取高 {higher!r}'
                   f'（ours={ours_value!r} theirs={theirs_value!r}）')
        return ours_value if higher == o else theirs_value
    rep.conflict(label, 'status', ours_value, theirs_value, 'status 不在词表里，偏序裁不了')
    return _MISSING


def _dependencies(label, base_value, ours_value, theirs_value, rep):
    got = _three_way(base_value, ours_value, theirs_value)
    if got is not _UNDECIDED:
        return got
    if not isinstance(ours_value, list) or not isinstance(theirs_value, list):
        rep.conflict(label, 'dependencies', ours_value, theirs_value, '不是列表，裁不了')
        return _MISSING
    if not isinstance(base_value, list):
        rep.conflict(label, 'dependencies', ours_value, theirs_value,
                     '没有 base（两侧都是新增），分不清是加边还是删边')
        return _MISSING
    added_o = [d for d in ours_value if d not in base_value]
    removed_o = [d for d in base_value if d not in ours_value]
    added_t = [d for d in theirs_value if d not in base_value]
    removed_t = [d for d in base_value if d not in theirs_value]
    if not removed_o and not removed_t:
        merged = list(ours_value) + [d for d in theirs_value if d not in ours_value]
        rep.decide(f'{label}: dependencies 两侧都只加边 → 取并集 {_fmt(merged)}')
        return merged
    rep.conflict(label, 'dependencies', ours_value, theirs_value,
                 f'ours 加 {_fmt(added_o)} 删 {_fmt(removed_o)}；'
                 f'theirs 加 {_fmt(added_t)} 删 {_fmt(removed_t)}')
    return _MISSING


def _text_field(label, key, base_value, ours_value, theirs_value, ours, theirs, rep):
    """A text field both sides edited: the side with the newer `updated_at` is the later edit."""
    got = _three_way(base_value, ours_value, theirs_value)
    if got is not _UNDECIDED:
        return got
    o_stamp, t_stamp = _get(ours, 'updated_at'), _get(theirs, 'updated_at')
    if all(isinstance(s, str) for s in (o_stamp, t_stamp)) and o_stamp != t_stamp:
        newer = 'ours' if o_stamp > t_stamp else 'theirs'
        rep.decide(f'{label}: {key} 两侧都改 → 取 updated_at 较新的一侧（{newer} '
                   f'{max(o_stamp, t_stamp)}）')
        return ours_value if o_stamp > t_stamp else theirs_value
    rep.conflict(label, key, ours_value, theirs_value, 'updated_at 分不出先后')
    return _MISSING


# ═══════════════════════════════════════════════════════════════════════════════
#  state.json -- the cycles
# ═══════════════════════════════════════════════════════════════════════════════

def _merge_state(base, ours, theirs, rep) -> dict:
    out = {}
    for key in _keys(ours, theirs):
        if key == 'cycles':
            merged = _merge_entries(_get(base, key), _get(ours, key), _get(theirs, key), rep,
                                    _merge_cycle, _index_cycles)
            out[key] = list(merged.values())
        else:
            out[key] = _plain_field('state.json', key, _get(base, key), _get(ours, key),
                                    _get(theirs, key), rep)
    return {k: v for k, v in out.items() if v is not _MISSING}


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
    rep.conflict(label, 'status', ours_value, theirs_value, '两侧的 cycle 状态是两个不同的词')
    return _MISSING


def _path(label, base_value, ours_value, theirs_value, rep):
    """`path` is append-only: a prefix extends, two different tails are merged in the order walked."""
    got = _three_way(base_value, ours_value, theirs_value)
    if got is not _UNDECIDED:
        return got
    if not isinstance(ours_value, list) or not isinstance(theirs_value, list):
        rep.conflict(label, 'path', ours_value, theirs_value, '不是列表，裁不了')
        return _MISSING
    ours, theirs = list(ours_value), list(theirs_value)
    i = 0
    while i < len(ours) and i < len(theirs) and ours[i] == theirs[i]:
        i += 1
    if i == len(ours) or i == len(theirs):
        longer = ours if len(ours) >= len(theirs) else theirs
        rep.decide(f'{label}: path 一侧是另一侧的前缀 → 取长的那侧（{len(longer)} 步）')
        return longer
    steps: dict = {}
    for step in ours[i:] + theirs[i:]:
        if not isinstance(step, dict):
            rep.conflict(label, 'path', ours_value, theirs_value, 'path 里有一格不是对象，裁不了')
            return _MISSING
        node = step.get('node')
        if node in steps and steps[node].get('result') != step.get('result'):
            rep.conflict(label, 'path', steps[node], step,
                         f'同一个节点 {str(node)[:8]} 两侧的 result 不同')
            return _MISSING
        steps.setdefault(node, step)
    tail = sorted(steps.values(), key=lambda s: (s.get('ended_at') or s.get('started_at') or ''))
    rep.decide(f'{label}: path 在共同前缀之后两侧各走了一段 → 按 ended_at 排序取并集'
               f'（{len(tail)} 步）')
    return ours[:i] + tail


# ═══════════════════════════════════════════════════════════════════════════════
#  instructions.json -- the standing instructions
# ═══════════════════════════════════════════════════════════════════════════════

def _merge_instructions(base, ours, theirs, rep) -> dict:
    out = {}
    for key in _keys(ours, theirs):
        if key == 'instructions':
            merged = _merge_entries(_get(base, key), _get(ours, key), _get(theirs, key), rep,
                                    _merge_instruction, _index_by_id, propagate_delete=True)
            out[key] = list(merged.values())
        elif key == REMOVED:
            merged = _merge_entries(_get(base, key), _get(ours, key), _get(theirs, key), rep,
                                    _merge_tombstone, _index_by_id)
            out[key] = list(merged.values())
        else:
            out[key] = _plain_field('instructions.json', key, _get(base, key), _get(ours, key),
                                    _get(theirs, key), rep)
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


# ═══════════════════════════════════════════════════════════════════════════════
#  signals -- one node's ticket on one cycle
# ═══════════════════════════════════════════════════════════════════════════════

def _merge_signal(base, ours, theirs, rep, ours_path=None, theirs_path=None) -> dict:
    """A ticket is ONE report: recency decides it, with a terminal status privileged."""
    def resolve(key, b, o, t):
        if key == 'status':
            return _signal_status(b, o, t, rep, ours_path, theirs_path)
        if _both_changed(b, o, t):
            return _by_mtime(o, t, ours_path, theirs_path, rep, key)
        return _three_way(b, o, t)
    return _merge_mapping(_as_dict(base), _as_dict(ours), _as_dict(theirs), rep, '条子', resolve)


def _signal_status(base_value, ours_value, theirs_value, rep, ours_path, theirs_path):
    got = _three_way(base_value, ours_value, theirs_value)
    if got is not _UNDECIDED:
        return got
    o_terminal = ours_value in TERMINAL_SIGNAL_STATUSES
    t_terminal = theirs_value in TERMINAL_SIGNAL_STATUSES
    if o_terminal != t_terminal:
        winner = ours_value if o_terminal else theirs_value
        rep.decide(f'条子: status 终态压过非终态 → {winner!r}'
                   f'（ours={ours_value!r} theirs={theirs_value!r}）')
        return winner
    return _by_mtime(ours_value, theirs_value, ours_path, theirs_path, rep, 'status 同档')


def _by_mtime(ours_value, theirs_value, ours_path, theirs_path, rep, what):
    winner, side = _newer_side(ours_value, theirs_value, ours_path, theirs_path)
    rep.decide(f'条子: {what} 两侧都改 → 取 mtime 较新的一侧（{side}；'
               f'{_mtime_note(ours_path, theirs_path)}）')
    return winner


def _newer_side(ours, theirs, ours_path, theirs_path):
    """The side whose %A/%B file is newer; a tie (or a missing file) goes to ours."""
    try:
        ours_mtime = Path(ours_path).stat().st_mtime
        theirs_mtime = Path(theirs_path).stat().st_mtime
    except (OSError, TypeError):
        return ours, 'ours'
    if theirs_mtime > ours_mtime:
        return theirs, 'theirs'
    return ours, 'ours'


def _mtime_note(ours_path, theirs_path) -> str:
    try:
        return (f'%A mtime={Path(ours_path).stat().st_mtime:.3f} / '
                f'%B mtime={Path(theirs_path).stat().st_mtime:.3f}')
    except (OSError, TypeError):
        return '有一侧没有文件'


# ═══════════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════════

def _merge(kind, base, ours, theirs, rep, ours_path, theirs_path):
    if kind == KIND_IDEAS:
        return _merge_ideas(base, ours, theirs, rep)
    if kind == KIND_STATE:
        return _merge_state(base, ours, theirs, rep)
    if kind == KIND_INSTRUCTIONS:
        return _merge_instructions(base, ours, theirs, rep)
    return _merge_signal(base, ours, theirs, rep, ours_path, theirs_path)


def _parse(argv):
    parser = argparse.ArgumentParser(
        prog='ledger-merge.py',
        description='Three-way merge of one pf ledger file (a git merge driver: %O %A %B [%P]).')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print the decisions and write nothing')
    parser.add_argument('base', help="git's %O -- the common ancestor")
    parser.add_argument('ours', help="git's %A -- the merged result is written HERE")
    parser.add_argument('theirs', help="git's %B -- the other branch")
    parser.add_argument('path', nargs='?', default=None,
                        help="git's %%P -- the ledger's path in the repo, which is what names it "
                             "(under git, %%O/%%A/%%B are temp files)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse(argv)
    ours_path, theirs_path, base_path = Path(args.ours), Path(args.theirs), Path(args.base)
    kind = (_kind_of(args.path) or _kind_of(args.ours) or _kind_of(args.theirs)
            or _kind_of(args.base))
    try:
        base = _read(base_path, f'base {base_path.name}')
        ours = _read(ours_path, f'ours {ours_path.name}')
        theirs = _read(theirs_path, f'theirs {theirs_path.name}')
    except SystemExit:
        return EXIT_USAGE
    except FileNotFoundError as e:
        print(f'ledger-merge: {e}', file=sys.stderr)
        return EXIT_USAGE

    if kind is None:
        kind = _kind_from_shape(ours, theirs, base)
    if kind is None:
        print(f'ledger-merge: 既不是这个驱动管的记录文件（按名字判：{args.path or args.ours}），'
              f'内容也不像四种记录之一，不碰它', file=sys.stderr)
        return EXIT_USAGE

    rep = Report(kind, args.path or args.ours)

    if ours is None and theirs is None:
        print(f'ledger-merge [{rep.path}]: 两侧都没有文件，没有可合的东西', file=sys.stderr)
        return EXIT_OK
    if ours is None:
        rep.decide('ours 不存在（这一侧新增）→ 取 theirs')
        ours, theirs = theirs, None
    elif theirs is None:
        rep.decide('theirs 不存在（这一侧新增）→ 取 ours')

    if theirs is None:
        merged = ours                                     # nothing to merge: one side has it all
    else:
        if not all(isinstance(side, dict) for side in (ours, theirs)):
            print(f'ledger-merge [{rep.path}]: 文件不是一个 JSON 对象，形状不对，不写',
                  file=sys.stderr)
            return EXIT_USAGE
        if base is not None and not isinstance(base, dict):
            print(f'ledger-merge [{rep.path}]: base 不是一个 JSON 对象，形状不对，不写',
                  file=sys.stderr)
            return EXIT_USAGE
        merged = _merge(kind, base, ours, theirs, rep, str(ours_path), str(theirs_path))

    if args.dry_run:
        print(f'ledger-merge --dry-run [{rep.path}] kind={kind}')
        if rep.decisions:
            for line in rep.decisions:
                print(line)
        else:
            print('  （没有需要裁决的地方，两侧一致或只有一侧改过）')
        rep.flush_conflicts()
        print(f'  → {"有冲突，未写" if rep.conflicts else "无冲突，本应写入 " + rep.path}')
        return EXIT_CONFLICT if rep.conflicts else EXIT_OK

    if rep.conflicts:
        rep.flush_conflicts()
        print(f'pf: {len(rep.conflicts)} 处要人裁决，{rep.path} 一个字都没动；'
              f'先手动合好它，再 `git add` 继续。', file=sys.stderr)
        return EXIT_CONFLICT

    try:
        payload = _serialize(kind, merged)
    except (TypeError, ValueError) as e:
        print(f'ledger-merge [{rep.path}]: 合出来的结果不是合法 JSON：{e}', file=sys.stderr)
        return EXIT_USAGE
    try:
        current = ours_path.read_bytes()
    except OSError:
        current = None
    if current == payload:
        return EXIT_OK                    # nothing to change: do not touch the file at all
    _write(kind, ours_path, merged)
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
