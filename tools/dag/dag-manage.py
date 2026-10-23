#!/usr/bin/env python3
"""Manage a dependency graph (DAG) of project ideas/features.

The verbs, their flags and their values are this file's parser: `dag-manage.py --help` and
`dag-manage.py help` print them.

Tags:
    A node may carry tags, carry none, or carry several. A tag says which kind of work a thing is
    (`writing` copy / `coding` code / `art` image), and the conductor uses it to draw the range a
    cycle picks from. A tagged node is visible only to a filter set that names the same word; **a
    node with no tag stays inside every range** -- blocking too little only runs one extra node,
    while blocking too much leaves work nobody ever does.

        add --tags art            apply tags (several, comma-separated: --tags art,writing)
        update <uuid> --tags ...  change the tags; --clear-tags takes them all off (= out of every
                                  filter)
        list --tags coding,writing  read it through one filter set: which nodes it would pick

    A tag outside the vocabulary warns on one line and is taken as given -- the vocabulary is laid
    out in front of a human to read, and the engine takes the list as it comes.

Weight:
    A node's weight governs "who does this leg", orthogonal to mode (which governs whether the loop
    runs and whether the close stops): a `light` node is implemented by the conductor itself, which
    only dispatches the review out; `standard` takes the standard path (dispatch Execute).

        add --weight light                        too light to be worth dispatching an executor
        update <uuid> --weight light|standard     change it; writing standard back drops the key

    **Not writing this key means standard**: old graphs and old machines need no migration, and only
    a node that really was given light lands the key.

Approval:
    A node may carry the moment a human approved what it delivered, and that moment belongs to ONE
    delivery: it counts only at or after the node's own completion moment (see `delivery_approved`).
    Delivering the node again pushes the completion moment past it, so the old approval lapses by
    itself -- nothing here has to be deleted, and no caller has to remember to clean it up.

        approve <uuid> [--at <moment>]   record the approval; the moment defaults to now, and
                                         repeating the call moves it to the new one
"""

import argparse
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import uuid as uuid_mod
from datetime import datetime
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════════
#  Paths & Constants
# ═══════════════════════════════════════════════════════════════════════════════

# Bootstrap: the loader is fetched by path too, so every file has to carry this paragraph itself
# (see `shared/loader.py`).
HERE = Path(__file__).resolve().parent
SHARED = HERE.parent / 'shared'

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
    """`tools/shared/ledger.py`: where `.pf/` lands, the worktree-to-main-tree rule, the init gate
    -- one source."""
    return _loader().load_shared('ledger')

# One directory per project: `.pf/` holds the graph, each node's own text (`spec.md`, reports), and
# the cycle book the conduct side keeps. A project either commits it or keeps it to itself -- the
# tool does not care which. The pipeline's plumbing (launch prompts, worktree, a node's temp files)
# sits under `.pf/meta/`, `.pf/worktrees/` and `.pf/nodes/<id>/temp/`, all three ignored by
# `.pf/.gitignore`.
#
# A `.pf/` the tool created by itself, never init'ed, is the one state that must stay unreachable:
# the lock's `mkdir` would otherwise leave a directory that is neither tracked nor ignored for the
# next `git add -A` to sweep up. `_require_initialized` closes that door, and the judgement behind it
# (the two markers, and the sentence a refused write is told) is `tools/shared/ledger.py`'s.


def _resolve_project_root(cwd: Path) -> Path:
    """cwd is the root; the one exception: when cwd is a linked worktree's checkout root, go back to
    the main tree (the rule lives in the ledger)."""
    return _ledger().resolve_project_root(cwd)


#: Which project this module points at right now: `configure()` fixes it once, and the CLI resolves
#: it from cwd once in `main()`. Importing this file binds no project -- the caller says which one.
LEDGER_DIR = None
DATA_DIR = None
NODES_DIR = None
IDEAS_JSON = None


def configure(project_root) -> None:
    """Pin the ledger to one project: `<root>/.pf/`, with its `nodes/` and `ideas.json` under it.

    This is the door paths enter this module through: the CLI calls it once from cwd inside `main()`,
    and in-process callers name the project themselves.
    """
    global LEDGER_DIR, DATA_DIR, NODES_DIR, IDEAS_JSON
    LEDGER_DIR = _ledger().ledger_dir_for(project_root)
    DATA_DIR = LEDGER_DIR
    NODES_DIR = _ledger().nodes_dir_of(DATA_DIR)
    IDEAS_JSON = DATA_DIR / 'ideas.json'


def _project_root() -> Path:
    """One level above the ledger: the `<root>` of `<root>/.pf`; a note's landing place is computed
    from it."""
    return LEDGER_DIR.parent


def _require_configured() -> None:
    """Say it plainly when nothing was named: which project's records to read is the caller's word."""
    if IDEAS_JSON is None:
        print('Error: this module points at no project yet; call configure(project root) first.',
              file=sys.stderr)
        sys.exit(1)


#: Where the Node spec lands: `pipeline/nodes.py` computes it in one place, and this tool takes it
#: from there.
CONDUCT_IMPL = HERE.parent / 'conduct' / 'pipeline'


def _spec_path(uid: str) -> str:
    """`<root>/.pf/nodes/<uid>/spec.md`, whose landing place is given by `nodes.node_spec_path`.

    This path is computed in one place only: what `add` reports, what `open` prints, and where the
    pipeline writes the spec are all this same value. That package is loaded by path under a name of
    its own (`pf_dag_pipeline`), so another copy loaded in the same process does not disturb this
    one.
    """
    pkg_dir = CONDUCT_IMPL
    pkg = _loader().load_by_path('pf_dag_pipeline', pkg_dir / '__init__.py',
                                 package_dir=pkg_dir, register=True, reuse=True)
    return pkg.nodes.node_spec_path(_project_root(), uid)


def _session_note():
    """`tools/shared/session-note.py` -- one module for every caller in this process.

    The conduct side writes every run's id to the same file; this side writes the discussion a node
    was born in, and reads both back for `info`.
    """
    return _loader().load_shared('session-note')


# ── the session note: best effort, both ways ──────────────────────────────────
# The note is plumbing: failing to write it or to remove it warns on one line, and the change
# already landed on the graph is not rolled back for it. The node is the real thing, and the note
# can be rebuilt by hand from the session at hand.

def _note_session(node_id: str, session: str) -> None:
    """Note the session this node was born in -- best effort, and never in the way.

    It is the discussion the node was routed in, which is what makes "is this the user's own words,
    or did the spec drift" answerable later. Failure to write it must not undo an `add`: the node is
    the point, and the note is plumbing that can be rebuilt by hand.
    """
    try:
        _session_note().append(_project_root(), node_id, 'discussion', session)
    except (OSError, ValueError) as e:
        print(f'Warning: the session note for {node_id} was not written: {e}', file=sys.stderr)


def _forget_session(node_id: str) -> None:
    """Forget a node's note after `rm` took the node off the graph -- same best effort as writing one.

    The node is already gone by the time this runs, so the command has done what the caller asked;
    a note this module cannot remove is a stray file, and failing over it would turn a completed
    removal into a failed command.
    """
    try:
        _session_note().drop(_project_root(), node_id)
    except (OSError, ValueError) as e:
        print(f'Warning: the session note for {node_id} was not removed: {e}', file=sys.stderr)

#: A node's status has only three words, and these three are what goes out: `todo` in play and not
#: delivered, `shelved` out of play waiting for a human, `completed` delivered. A word outside the
#: vocabulary is read exactly as it stands; this never guesses a word for it.
#: "Being run by a cycle" is not a status (such a node is still `todo`) -- it lives in state.json's
#: holding relation.
VALID_STATUSES = {'todo', 'shelved', 'completed'}
# A blocked node is never recommended and never "ready": `shelved` is out of play until a human
# puts it back in (the round ran and the verdict was no, or it was shelved on purpose).
# It comes back with `status <uuid> todo`.
BLOCKED_STATUSES = {'shelved'}
#: Three tiers of node execution mode, from "the human is in it a lot" to "the human is barely in
#: it":
#:   manual      -- no execute→review→fix loop; it is a discussion slot: the owner proposes, the AI
#:                   proposes, it is rejected, propose again.
#:   checkpoint  -- the loop runs as usual, but the close stops and waits for the owner to say
#:                   "continue" before the next node opens.
#:   auto        -- the loop runs as usual, and the close opens the next node itself (no human gate;
#:                   only a stuck run or a conflicting requirement is reported to the owner).
VALID_MODES = {'manual', 'checkpoint', 'auto'}
DEFAULT_MODE = 'auto'
#: Node weight, two tiers, orthogonal to mode: mode governs whether it stops, this governs who does
#: it. **Not writing the key means standard**, so only light ever lands the key.
VALID_WEIGHTS = ('light', 'standard')
DEFAULT_WEIGHT = 'standard'
# ── tags ─────────────────────────────────────────────────────────────────────
# Three words, fixed. It is a convention laid out for a human to read; the engine only uses it to
# build that warning: a tag outside the vocabulary warns on one line and is taken as given, and when
# a fourth kind of work really shows up, writing it in is enough. Filtering always compares literally
# and never consults the vocabulary.
VALID_TAGS = ('writing', 'coding', 'art')


# ═══════════════════════════════════════════════════════════════════════════════
#  Low-level helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_dirs() -> None:
    """Create data directories if they do not exist."""
    NODES_DIR.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    """`tools/shared/clock.py`: the moment a record is written with, in the tree's one shape."""
    return _loader().load_shared('clock').now()


def _read() -> dict:
    """Read ideas.json; auto-create on first use.

    The read itself -- absent file, bytes that are not UTF-8, text that is not JSON, and the words
    each of those gets -- is `tools/shared/jsonio.py`'s. This layer is the CLI, so a record that
    cannot be read ends the command with that message, plus the one hint this tool has for it.
    """
    jsonio = _loader().load_shared('jsonio')
    _require_configured()
    try:
        data = jsonio.read_json_strict(IDEAS_JSON, 'ideas.json') or {'nodes': {}}
    except jsonio.JsonReadError as e:
        print(f'Error: {e}', file=sys.stderr)
        if isinstance(e.__cause__, json.JSONDecodeError):
            print('Hint: run "validate" for details.', file=sys.stderr)
        sys.exit(1)
    data.setdefault('nodes', {})
    return data


def ideas_bytes(data: dict) -> bytes:
    """The bytes `ideas.json` lands as: this tool gives the format, and elsewhere takes it from here.

    What writes them is `tools/shared/jsonio.py` (indent 2, non-ASCII passed through as it is, no
    trailing newline); `sort_keys=True` is this file's own habit, so a rewritten graph reads the same
    way every time. The merge driver compares these bytes with the copy on disk, so there is one
    criterion only: if both sides wrote the format out themselves, one byte of difference makes every
    merge look like a change.
    """
    return _loader().load_shared('jsonio').json_bytes(data, sort_keys=True)


def write_ideas_bytes(path, payload: bytes) -> None:
    """Land these bytes at this path: `ideas_bytes`'s criterion and this write come from one tree.

    The merge driver asks the same writer through both doors (this tree's `tools/shared/jsonio.py`),
    so it never judges with one tree's format and then lands bytes in another tree's spelling. Who
    holds the lock and what happens after the write is the caller's business; all that happens here
    is this one write.
    """
    _loader().load_shared('jsonio').atomic_write_bytes(path, payload)


def _write(data: dict) -> None:
    """Atomically write ideas.json: temp file, fsync, a replace that tolerates a live reader.

    The write is `tools/shared/jsonio.py`'s, the same one every other record in this tree goes
    through; `sort_keys` is this file's own habit, so a rewritten graph reads the same way twice.
    """
    _require_configured()
    _ensure_dirs()
    _loader().load_shared('jsonio').atomic_write_json(IDEAS_JSON, data, sort_keys=True)


# ── the cross-process ledger lock ─────────────────────────────────────────────
# ideas.json is whole-file read-modify-write; two processes walking that at once means the later
# write erases the earlier one. Every MUTATING command body holds the project's one lock across
# its read → write -- the same `.pf/meta/ledger.lock` file conduct, the issue tool and a session
# note's own writer use, so writes exclude each other across all of them. Reentrant per process
# (this body writes a node's note inside its own read-modify-write).


def _require_initialized() -> None:
    """A `.pf/` that was never init-ed may have nothing written to it: the lock's `mkdir` is the one
    thing that would create it.

    It reads `LEDGER_DIR`, the ledger `configure()` named; the criterion and that sentence live in
    `tools/shared/ledger.py`.
    """
    if _ledger().is_initialized(LEDGER_DIR):
        return
    print(f'Error: {_ledger().init_message(LEDGER_DIR)}', file=sys.stderr)
    sys.exit(1)


@contextlib.contextmanager
def _ledger_lock(timeout: float | None = None):
    """Hold the project's ledger lock for the duration of one read-modify-write.

    The lock itself is `tools/shared/ledgerlock.py`. A caller that cannot get it within `timeout` is
    told loudly and exits 1 -- wedging silently is the one failure this must not have.
    """
    lock = _loader().load_shared('ledgerlock')
    kwargs = {} if timeout is None else {'timeout': timeout}
    try:
        with lock.ledger_lock(LEDGER_DIR, gate=_require_initialized, **kwargs):
            yield
    except TimeoutError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


def _resolve_uuid(uid: str, data: dict) -> str:
    """`uid` (a full id, or a prefix of one) → the full id in the record; a miss stops right here.

    A prefix counts only when it matches uniquely: an ambiguous prefix is refused at once, and the
    caller is asked to write the id out in full -- letting the tool guess one slot for you; a wrong
    guess corrupts another slot's record. Every command uses this return value as the key into
    `data['nodes']`, so one prefix reaches the same verdict on every verb.
    """
    if uid in data['nodes']:
        return uid
    # Prefix matching for convenience (e.g. "7a6aab2c" → full UUID)
    matches = [k for k in data['nodes'] if k.startswith(uid)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f'Error: "{uid}" matches multiple nodes ({", ".join(matches)})',
              file=sys.stderr)
        sys.exit(1)
    print(f'Error: node {uid} not found', file=sys.stderr)
    sys.exit(1)


def _parse_tags(raw: str) -> list:
    """`--tags art,writing` → `['art', 'writing']`: strip whitespace, drop duplicates, keep the order.

    Taken literally: a tag written here can only match a filter set that wrote the same word.
    A tag outside the vocabulary warns on one line and is taken as given -- when a fourth kind of
    work really shows up, writing it in is enough.
    """
    out = []
    for item in (raw or '').split(','):
        tag = item.strip()
        if tag and tag not in out:
            out.append(tag)
    unknown = [t for t in out if t not in VALID_TAGS]
    if unknown:
        print(f'Warning: tag {", ".join(unknown)} is not in the vocabulary {"/".join(VALID_TAGS)}; '
              f'taken as given, but it can only match a node that wrote the same word.',
              file=sys.stderr)
    return out


def _node_weight(node: dict) -> str:
    """One node's weight; a missing key, or a word this does not recognize, both read as standard.

    The conservative direction is **to put one more person on the work**: when the reading is not
    certain, follow the standard path rather than letting a node fall to the conductor itself over
    one mistyped word in the record.
    """
    return 'light' if node.get('weight') == 'light' else DEFAULT_WEIGHT


def _node_tags(node: dict) -> list:
    """The tags on one node; never tagged means an empty list (= bound by no filter set)."""
    tags = node.get('tags') or []
    return [t for t in tags if isinstance(t, str) and t.strip()]


def _in_tag_scope(node: dict, wanted) -> bool:
    """Is this node inside one filter set? At least one tag in common is a hit; a node with no tag at
    all is visible everywhere.

    The conservative direction is **to block too little rather than too much**: one extra node costs
    one round, while blocking the wrong node leaves a line's work nobody ever does.
    """
    if not wanted:
        return True                     # no filter set = the whole graph is in range (today's rule)
    tags = _node_tags(node)
    if not tags:
        return True
    return bool(set(tags) & set(wanted))


# ═══════════════════════════════════════════════════════════════════════════════
#  Graph
# ═══════════════════════════════════════════════════════════════════════════════

def _graph():
    """`tools/dag/graph.py`: this tool's graph algorithms, loaded by path like every neighbour.

    Pure functions, no IO, so that file can be imported whole and tested on its own
    (`tests/test_graph.py`).
    """
    return _loader().load_by_path('pf_dag_graph', HERE / 'graph.py', register=True, reuse=True)


def _nc_nodes(data: dict) -> dict:
    """Return sub-dict of non-completed nodes."""
    return {k: v for k, v in data['nodes'].items()
            if v.get('status') != 'completed'}


# ── dependency edits ──────────────────────────────────────────────────────────

def _try_add_dep(data: dict, uid: str, dep: str) -> None:
    """Add dep to uid's dependencies; check cycles. Exits on failure."""
    graph = _graph()
    if dep == uid:
        print('Error: self-dependency is not allowed', file=sys.stderr)
        sys.exit(1)
    deps = data['nodes'][uid]['dependencies']
    if dep in deps:
        return              # already present -- no-op
    deps.append(dep)
    adj = graph._adjacency(data['nodes'])
    found, path = graph._can_reach(adj, dep, uid)
    if found:
        cycle_str = ' → '.join([uid] + path)
        print(f'Error: cycle detected: {cycle_str}', file=sys.stderr)
        deps.remove(dep)
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
#  Approval: "a human passed this delivery"
# ═══════════════════════════════════════════════════════════════════════════════
# The `checkpoint` mode is what this exists for: there the tool stops with its own sentence, "the
# next node opens only once you say continue", and that "continue" IS an approval of the delivery the
# line stopped on. Written down, it lets a reader outside this tool tell "a human let this go" from
# "the line happened to move one more step" -- the path alone cannot say which of the two it was.
#
# The moment belongs to ONE delivery, and the two moments on the node decide it: an approval counts
# only at or after that delivery's completion moment. Delivering again writes the completion moment
# afresh, so the old approval lapses by itself -- nothing is deleted, and no reader has to know that
# a cleanup step exists.


def _moment(value) -> datetime | None:
    """One recorded moment read back, or `None` when it is in neither of the tree's two shapes.

    Both shapes are UTC with a literal `Z`, differing only in whether the seconds carry a fraction
    (`tools/shared/clock.py`), so ordering two of them as strings is right for a pair of one shape and
    wrong across the two: `'...44Z'` sorts after `'...44.160813Z'` while meaning earlier. Reading them
    as moments first is what makes one verdict out of either pair; a value in neither shape has no
    order to give, and the caller falls back to comparing the strings it was handed.
    """
    if not isinstance(value, str):
        return None
    for shape in (_loader().load_shared('clock').ISO_FMT,
                  _loader().load_shared('clock').ISO_FMT_SECONDS):
        try:
            return datetime.strptime(value, shape)
        except ValueError:
            pass
    return None


def delivery_approved(node: dict) -> bool:
    """Did a human approve THIS delivery? The approval's moment at or after the delivery's says yes.

    A node with no completion moment has no delivery for an approval to be about -- it never
    delivered, or it was taken back out of `completed` (which drops that moment) -- so it answers no
    whatever it carries.
    """
    approved, completed = node.get('approved_at'), node.get('completed_at')
    if not approved or not completed:
        return False
    approved_at, completed_at = _moment(approved), _moment(completed)
    if approved_at is not None and completed_at is not None:
        return approved_at >= completed_at
    return str(approved) >= str(completed)


def approve_delivery(node: dict, at: str) -> None:
    """Write the approval onto one node: the ONE place `approved_at` lands.

    `updated_at` moves with it -- the record changed, which is the habit every other edit here has.
    """
    node['approved_at'] = at
    node['updated_at'] = at


def _delivery_refusal(uid: str, node: dict) -> str | None:
    """Why this node has nothing to approve, in one sentence a human can act on; `None` when it has.

    An approval is about a delivery: a node still in play (or one shelved after a verdict of no) has
    not delivered, and saying yes to it would record a word about something that never happened.
    """
    if node.get('status') == 'completed' or node.get('completed_at'):
        return None
    title = node.get('title') or ''
    return (f'node {uid}{f" ({title})" if title else ""} has not been delivered yet '
            f'(status={node.get("status") or "todo"}, and no completion moment): there is nothing '
            f'here to approve. What an approval is about is a delivery, and that is what closing out '
            f'the round which delivered it writes down.')


# ═══════════════════════════════════════════════════════════════════════════════
#  Commands
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_add(args):
    with _ledger_lock():
        data = _read()
        if not args.title:
            print('Error: title cannot be empty', file=sys.stderr)
            sys.exit(1)

        # Accept description via positional args (nargs='*') or --desc flag
        desc = args.desc
        if args.description:
            desc = ' '.join(args.description)

        uid = str(uuid_mod.uuid4())
        now = _now()
        mode = args.mode if args.mode is not None else DEFAULT_MODE
        data['nodes'][uid] = {
            'title': args.title,
            'description': desc or '',
            'status': 'todo',
            'mode': mode,
            'dependencies': [],
            'created_at': now,
            'updated_at': now,
        }
        # The tags key lands only when tags were really given: an untagged node has today's exact
        # shape on the graph, not one byte more.
        tags = _parse_tags(args.tags) if args.tags else []
        if tags:
            data['nodes'][uid]['tags'] = tags
        # weight likewise lands only when it is not the default: a standard node has today's exact
        # shape on the graph, not one byte more.
        if args.weight == 'light':
            data['nodes'][uid]['weight'] = 'light'
        # The session a node was born in does not land in the record: `ideas.json` is shared, and a
        # session only means anything on this machine -- elsewhere it is a dead value that can never
        # be opened, and it does not even tell "that was another machine" from "this run was
        # deleted". So it lands in `.pf/meta/sessions/<uuid>.json` (`meta/` is kept out of the
        # version repository by `.pf/.gitignore`), see `tools/shared/session-note.py`. The tool still
        # reads the environment variable; the caller does not copy it over -- the copy always misses
        # one.
        # Written after `_write(data)`: a failed dependency check calls `_rollback_add`, and a slot
        # that never landed on the graph should leave no note behind.
        session = os.environ.get('DSH_SESSION_ID')

        node_dir = NODES_DIR / uid
        node_dir.mkdir(parents=True, exist_ok=True)
        spec_path = _spec_path(uid)

        # Process --depends-on
        if args.depends_on:
            dep_uuids = [d.strip() for d in args.depends_on.split(',') if d.strip()]
            for dep in dep_uuids:
                if dep not in data['nodes']:
                    print(f'Error: dependency {dep} not found', file=sys.stderr)
                    _rollback_add(data, uid)
                    sys.exit(1)
                _try_add_dep(data, uid, dep)

        _write(data)
        # The note is written here: a failed dependency check calls `_rollback_add`, and a slot that
        # never landed on the graph should leave no note behind.
        if session:
            _note_session(uid, session)
    print(f'Node created: {uid}')
    print(f'Spec path (nonexistent): {spec_path}')


def _rollback_add(data: dict, uid: str) -> None:
    """Remove a freshly created node from ideas.json and filesystem."""
    data['nodes'].pop(uid, None)
    _write(data)
    node_dir = NODES_DIR / uid
    if node_dir.exists():
        shutil.rmtree(node_dir)


def cmd_rm(args):
    with _ledger_lock():
        data = _read()
        uid = _resolve_uuid(args.uuid, data)

        # Check for dependents
        dependents = []
        for nid, node in data['nodes'].items():
            if uid in node.get('dependencies', []):
                dependents.append((nid, node.get('title', '')))

        if dependents and not args.force:
            print('Error: the following nodes depend on this node:',
                  file=sys.stderr)
            for did, dtitle in dependents:
                print(f'  {did}  {dtitle}', file=sys.stderr)
            print('Use --force to remove anyway.', file=sys.stderr)
            sys.exit(1)

        # Remove this UUID from every other node's dependencies
        for node in data['nodes'].values():
            deps = node.get('dependencies', [])
            if uid in deps:
                deps.remove(uid)

        del data['nodes'][uid]
        _write(data)

        node_dir = NODES_DIR / uid
        if node_dir.exists():
            shutil.rmtree(node_dir)
        # The note goes with the node: once the node is off the graph, that discussion is no longer
        # "this slot's" discussion.
        _forget_session(uid)

        print(f'Removed {uid}')


def cmd_update(args):
    with _ledger_lock():
        data = _read()
        node = data['nodes'][_resolve_uuid(args.uuid, data)]
        if args.tags is not None and args.clear_tags:
            print('Error: give --tags and --clear-tags one at a time (the first swaps a set in, '
                  'the second takes them all off)', file=sys.stderr)
            sys.exit(1)
        changed = False
        if args.title is not None:
            node['title'] = args.title
            changed = True
        if args.desc is not None:
            node['description'] = args.desc
            changed = True
        if args.mode is not None:
            node['mode'] = args.mode
            changed = True
        if args.weight is not None:
            # Writing standard back drops the key: only light ever lands it in the record (see the
            # DEFAULT_WEIGHT block).
            if args.weight == 'light':
                node['weight'] = 'light'
            else:
                node.pop('weight', None)
            changed = True
        if args.tags is not None:
            tags = _parse_tags(args.tags)
            if tags:
                node['tags'] = tags
                changed = True
            else:
                print('Warning: --tags is empty; to take every tag off use --clear-tags',
                      file=sys.stderr)
        if args.clear_tags:
            # Taken clean = this node is back to "visible everywhere": a node with no tag stays in
            # the picking range of every filter set.
            node.pop('tags', None)
            changed = True
        if changed:
            node['updated_at'] = _now()
            _write(data)
        else:
            print('No changes: provide --title, --desc, --mode, --weight, --tags and/or --clear-tags',
                  file=sys.stderr)


def cmd_depend(args):
    with _ledger_lock():
        data = _read()
        uid = _resolve_uuid(args.uuid, data)
        dep = _resolve_uuid(args.on, data)
        _try_add_dep(data, uid, dep)
        data['nodes'][uid]['updated_at'] = _now()
        _write(data)


def _dependency_to_remove(dep: str, deps: list, data: dict) -> str | None:
    """The full id of the edge to take off; `None` = this edge was never there, nothing to take off.

    The verdict follows the same rule as the node side (a unique match is used, an ambiguous one
    lists the candidates and stops, a miss says it is a miss), only it looks in this edge's list
    first: a node someone already deleted from the graph leaves its edge written in the record, and
    that is exactly what the merge driver's repair hint calls a human in to take off. If none of the
    three roads works it is an error -- silently taking no edge off is the worst this command ever
    looked: the caller thinks the edge is gone while the record still carries it.
    """
    if dep in deps:
        return dep
    named = [d for d in deps if str(d).startswith(dep)]
    if len(named) > 1:
        print(f'Error: "{dep}" matches multiple dependencies ({", ".join(named)})',
              file=sys.stderr)
        sys.exit(1)
    if len(named) == 1:
        return named[0]
    # This id is not in the list: there is nothing to take off only when it is recognized as a slot
    # in the graph (and that slot simply has no such edge); an unrecognized one -- a typo, or neither
    # on the graph nor in this edge's list -- is stopped loudly by `_resolve_uuid`.
    _resolve_uuid(dep, data)
    return None


def cmd_undepend(args):
    with _ledger_lock():
        data = _read()
        uid = _resolve_uuid(args.uuid, data)
        dep_uuid = args.from_
        if args.dep:
            dep_uuid = args.dep
        if not dep_uuid:
            print('Error: specify dependency to remove via positional arg or --from', file=sys.stderr)
            sys.exit(1)
        deps = data['nodes'][uid].get('dependencies', [])
        target = _dependency_to_remove(dep_uuid, deps, data)
        if target is not None:
            deps.remove(target)
            data['nodes'][uid]['updated_at'] = _now()
            _write(data)


def cmd_status(args):
    with _ledger_lock():
        data = _read()
        node = data['nodes'][_resolve_uuid(args.uuid, data)]
        status = args.status
        if status not in VALID_STATUSES:
            print(
                f'Error: invalid status "{args.status}". '
                f'Valid: {", ".join(sorted(VALID_STATUSES))}',
                file=sys.stderr,
            )
            sys.exit(1)
        node['status'] = status
        node['updated_at'] = _now()
        # The completion moment is the node's own fact and does not hide inside some cycle's path:
        # it is what orders "finished most recently". updated_at cannot serve here -- a title,
        # description or mode change bumps it.
        # It is taken off when the node leaves completed: a node that is not completed should not
        # carry a completion moment.
        if status == 'completed':
            node['completed_at'] = node['updated_at']
        else:
            node.pop('completed_at', None)
        _write(data)


def cmd_approve(args):
    """Record that a human approved one node's delivery: the moment lands at or after its completion.

    Repeating the call is allowed, and it moves the moment to the new one -- a human saying yes again
    is saying yes about the delivery on the disk right now. An approval nobody repeats lapses on its
    own when the node is delivered again, so nothing here has to be undone.
    """
    with _ledger_lock():
        data = _read()
        uid = _resolve_uuid(args.uuid, data)
        node = data['nodes'][uid]
        refusal = _delivery_refusal(uid, node)
        if refusal:
            print(f'Error: {refusal}', file=sys.stderr)
            sys.exit(1)
        approve_delivery(node, args.at or _now())
        _write(data)
    print(f'APPROVED: {uid} at {node["approved_at"]}')


def approve_node(project_root, uid: str, at: str | None = None) -> str | None:
    """`cmd_approve`'s read-verdict-write, for another tool's command rather than this CLI.

    The conduct side comes through here at a checkpoint gate: the human putting a line back in service
    is that gate's "continue", and this is where that word becomes a record. The body is the verb's,
    so the two cannot disagree about what an approval is or where it lands; the only difference is
    that this door configures the module for the project it was handed (the module points at one
    project at a time), and that it answers instead of printing.

    `None` means there was nothing to approve -- the node is not on this graph, or it has not
    delivered. That is not an error to raise at a caller whose own command has already done what was
    asked: the ledger lock is held here for the read and the write, and the moment written comes back.
    """
    configure(project_root)
    with _ledger_lock():
        data = _read()
        node = data['nodes'].get(uid)
        if node is None or _delivery_refusal(uid, node):
            return None
        approve_delivery(node, at or _now())
        _write(data)
        return node['approved_at']


def cmd_revive(args):
    with _ledger_lock():
        """Put a shelved node back to todo so a later round can pick it up again.

        Without this, a node left `shelved` is stuck forever: nothing in the pipeline picks a node
        that is not `todo`, and the conductor is forbidden from editing ideas.json by hand.

        A node is `shelved` either because a round ran and the verdict was no, or because it was
        shelved on purpose (machine busy, waiting on the owner). `revive` is the "put it back in
        play" command for both -- a plain release with nothing to weigh is `status <uuid> todo`.
        """
        data = _read()
        node = data['nodes'][_resolve_uuid(args.uuid, data)]
        status = node.get('status')
        if status != 'shelved':
            print(f'Error: node is not shelved (status={status!r}); '
                  f'use "status <uuid> todo" to change it', file=sys.stderr)
            sys.exit(1)
        node['status'] = 'todo'
        node['updated_at'] = _now()
        _write(data)
        print(f'REVIVED: {node.get("title", "")} -> todo')


def cmd_list(args):
    graph = _graph()
    data = _read()

    # Filter by status(es) if --status given: a literal match against the word on disk.
    if args.status:
        status_set = set(args.status)
        nodes = {k: v for k, v in data['nodes'].items()
                 if v.get('status') in status_set}
    else:
        nodes = data['nodes']  # all

    rev = graph._reverse_adj(graph._adjacency(nodes))

    # Compute urgency/importance on non-completed subgraph (philosophy rule)
    nc = _nc_nodes(data)
    adj = graph._adjacency(nc)
    urgency_scores = graph._urgency_scores(adj)
    imp_scores = graph._importance_scores(adj)

    result = []
    for uid, node in nodes.items():
        deps = list(node.get('dependencies', []))
        status = node.get('status', 'todo')
        is_blocked = status in BLOCKED_STATUSES

        entry = {
            'id': uid,
            'title': node.get('title', ''),
            'status': status,
            'mode': node.get('mode', DEFAULT_MODE),
            # `ready` is exactly _node_ready's rule: it is todo itself and every dependency is done.
            # This field and the `--ready` filter have to say the same thing, or the filter is
            # bypassed in one word.
            'ready': graph._node_ready(node, data['nodes']),
        }
        # Tag echo: whoever reads this list has to see at a glance which kind of work the node is
        # and why it enters or stays out of a filter set.
        # A node never tagged does not land this key -- when the whole graph has no tag, the output
        # is byte-for-byte today's.
        tags = _node_tags(node)
        if tags:
            entry['tags'] = tags
        # weight is echoed like tags: a graph with no light node prints byte-for-byte today's output.
        if _node_weight(node) == 'light':
            entry['weight'] = 'light'

        # Only runnable nodes get urgency/importance scores
        if status != 'completed' and not is_blocked:
            entry['urgency_level'] = urgency_scores.get(uid, 0)
            entry['importance_score'] = imp_scores.get(uid, 0)

        if args.verbose:
            entry['deps'] = deps
            entry['dependents'] = rev.get(uid, [])

        result.append(entry)

    if args.ready:
        # Only todo nodes are in ready (`e['ready']` has already said so): shelved is waiting for a
        # human, completed has been delivered, and one whose dependencies are unmet is still waiting
        # -- a root node with no dependencies is not automatically ready either.
        result = [e for e in result if e['ready']]

    if args.tags:
        # "Which nodes would this filter set pick": a hit = at least one tag in common, or no tag at
        # all (a node with no tag stays in every range). This step only drops rows --
        # urgency/importance are still computed over the whole non-completed subgraph, and the
        # ordering below is untouched.
        wanted = _parse_tags(args.tags)
        if wanted:
            result = [e for e in result if _in_tag_scope(e, wanted)]

    # Eisenhower matrix: importance first, then urgency.
    # Completed and blocked nodes have no scores → sort to bottom (0, 0). Blocked must
    # sink: pf-route's global scan is `list --limit N`, so a blocked node left with real
    # scores would be re-recommended every round and blow up again.
    result.sort(key=lambda e: (-e.get('importance_score', 0),
                                -e.get('urgency_level', 0)))

    if args.limit > 0:
        result = result[:args.limit]

    print(json.dumps(result, ensure_ascii=False))


def cmd_info(args):
    graph = _graph()
    data = _read()
    all_nodes = data['nodes']
    uid = _resolve_uuid(args.uuid, data)
    node = all_nodes[uid]

    deps_list = []
    for dep_uid in node.get('dependencies', []):
        dep_node = all_nodes.get(dep_uid, {})
        entry = {'id': dep_uid, 'title': dep_node.get('title', '(unknown)')}
        if args.verbose:
            entry['status'] = dep_node.get('status', '(unknown)')
        deps_list.append(entry)

    dep_by_list = []
    for nid, n in all_nodes.items():
        if uid in n.get('dependencies', []):
            entry = {'id': nid, 'title': n.get('title', '')}
            if args.verbose:
                entry['status'] = n.get('status', '')
            dep_by_list.append(entry)

    nc = _nc_nodes(data)
    adj = graph._adjacency(nc)
    urgency_scores = graph._urgency_scores(adj)
    imp_scores = graph._importance_scores(adj)

    blocked = node.get('status', 'todo') in BLOCKED_STATUSES
    result = {
        'id': uid,
        'title': node.get('title', ''),
        'status': node.get('status', 'todo'),
        'mode': node.get('mode', DEFAULT_MODE),
        'description': node.get('description', ''),
        'depends_on': deps_list,
        'depended_by': dep_by_list,
        # It has to say the same thing as list (see _node_ready): it is todo itself and every
        # dependency is done.
        'ready': graph._node_ready(node, all_nodes),
        # A shelved node is not ranked (it is not in the runnable set), so both scores read zero.
        'urgency_level': 0 if blocked else urgency_scores.get(uid, 0),
        'importance_score': 0 if blocked else imp_scores.get(uid, 0),
    }

    if _node_weight(node) == 'light':
        result['weight'] = 'light'

    if args.verbose:
        result['created_at'] = node.get('created_at', '')
        result['updated_at'] = node.get('updated_at', '')

    # Session notes (the discussion run / the execution run) are echoed only when they exist. `list`
    # gives the skeleton, `info` is this slot's detail -- callers may not read ideas.json raw, so a
    # key that does not appear here was never written.
    note = _session_note().read(_project_root(), uid)
    if note['discussion'] or note['run']:
        result['sessions'] = {role: note[role] for role in ('discussion', 'run')}
    if node.get('completed_at'):
        result['completed_at'] = node['completed_at']
    # Echoed like the completion moment, and for the same reason: callers may not read ideas.json raw,
    # so a key that does not appear here was never written. Whether an approval still counts is
    # `delivery_approved`'s verdict, not this echo's.
    if node.get('approved_at'):
        result['approved_at'] = node['approved_at']

    print(json.dumps(result, ensure_ascii=False))


def cmd_context(args):
    """Show full upstream/downstream transitive context of a node."""
    graph = _graph()
    data = _read()
    all_nodes = data['nodes']
    uid = _resolve_uuid(args.uuid, data)
    node = all_nodes[uid]

    adj = graph._adjacency(all_nodes)
    rev = graph._reverse_adj(adj)

    # Upstream: BFS on dependency edges
    upstream = []
    visited_up = {uid}
    queue = [(dep, 1) for dep in node.get('dependencies', [])]
    while queue:
        dep_id, depth = queue.pop(0)
        if dep_id in visited_up:
            continue
        visited_up.add(dep_id)
        dep_node = all_nodes.get(dep_id, {})
        upstream.append({
            'id': dep_id,
            'title': dep_node.get('title', '(unknown)'),
            'status': dep_node.get('status', '(unknown)'),
            'depth': depth,
        })
        for gdep in dep_node.get('dependencies', []):
            if gdep not in visited_up:
                queue.append((gdep, depth + 1))

    # Downstream: BFS on reverse edges
    downstream = []
    visited_down = {uid}
    queue = [(did, 1) for did in rev.get(uid, [])]
    while queue:
        did, depth = queue.pop(0)
        if did in visited_down:
            continue
        visited_down.add(did)
        dep_node = all_nodes.get(did, {})
        downstream.append({
            'id': did,
            'title': dep_node.get('title', '(unknown)'),
            'status': dep_node.get('status', '(unknown)'),
            'depth': depth,
        })
        for gdep in rev.get(did, []):
            if gdep not in visited_down:
                queue.append((gdep, depth + 1))

    result = {
        'id': uid,
        'title': node.get('title', ''),
        'status': node.get('status', ''),
        'upstream': upstream,
        'downstream': downstream,
    }

    print(json.dumps(result, ensure_ascii=False))


def cmd_search(args):
    """Search nodes by title/description using regex.

    Title matches sort before description-only matches.
    On title match: description is printed in full.
    On description-only match: single-occurrence → snippet, 2+ → full.
    """
    import re
    graph = _graph()
    data = _read()
    nodes = data['nodes'] if not args.active_only else _nc_nodes(data)
    query = args.query

    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error as e:
        print(f'Error: invalid regex: {e}', file=sys.stderr)
        sys.exit(1)
    title_match = lambda t: bool(pattern.search(t))
    desc_match = lambda d: bool(pattern.search(d))
    desc_count = lambda d: len(list(pattern.finditer(d))) if d else 0
    first_pos = lambda d: pattern.search(d).start()

    def _snippet(desc: str) -> str:
        """Return ±20 char window around the first match."""
        pos = first_pos(desc)
        PAD = 20
        start = max(0, pos - PAD)
        end = min(len(desc), pos + PAD)
        out = desc[start:end]
        if start > 0:
            out = '...' + out
        if end < len(desc):
            out = out + '...'
        return out

    title_hits = []
    desc_hits = []

    for uid, node in nodes.items():
        title = node.get('title', '')
        desc = node.get('description', '')
        t_hit = title_match(title)
        d_hit = desc_match(desc)

        if not t_hit and not d_hit:
            continue

        status = node.get('status', 'todo')
        entry = {
            'id': uid,
            'title': title,
            'status': status,
            # The same thing `list`/`info` say (see _node_ready).
            'ready': graph._node_ready(node, data['nodes']),
        }

        if desc:
            if t_hit:
                entry['description'] = desc
            else:
                entry['description'] = (
                    desc if desc_count(desc) >= 2 else _snippet(desc)
                )

        (title_hits if t_hit else desc_hits).append(entry)

    result = title_hits + desc_hits
    limit = args.limit if args.limit > 0 else len(result)
    print(json.dumps(result[:limit], ensure_ascii=False))


def cmd_open(args):
    data = _read()
    uid = _resolve_uuid(args.uuid, data)
    print(_spec_path(uid))


def cmd_validate(args):
    graph = _graph()
    data = _read()
    nodes = data['nodes']
    total = len(nodes)

    status_counts = {}
    for n in nodes.values():
        s = n.get('status', 'unknown')
        status_counts[s] = status_counts.get(s, 0) + 1

    # -- Check 1 : no cycles --
    adj = graph._adjacency(nodes)
    has_cycle, cycle_path = graph._detect_cycle(adj)
    if has_cycle:
        print('FAIL  check=no_cycles  '
              f'reason="cycle detected: {" → ".join(cycle_path)}"  '
              f'nodes={total}')
    else:
        print(f'PASS  check=no_cycles  nodes={total}')

    # -- Check 2 : no dangling references --
    dangling = []
    for uid, node in nodes.items():
        for dep in node.get('dependencies', []):
            if dep not in nodes:
                dangling.append((uid, dep))
    if dangling:
        for src, dep in dangling:
            print(f'FAIL  check=no_dangling  node={src}  references={dep}')
    else:
        print('PASS  check=no_dangling')

    print(f'Total nodes: {total}')
    for s in sorted(status_counts.keys()):
        print(f'  {s}: {status_counts[s]}')

    if has_cycle or dangling:
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI entry point
# ═══════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='dag-manage.py',
        description='Manage a dependency graph (DAG) of project ideas/features.',
    )
    subs = parser.add_subparsers(dest='command', metavar='<command>')
    subs.required = True

    # add
    p = subs.add_parser('add', help='Add a new idea node')
    p.add_argument('title', help='Node title')
    p.add_argument('description', nargs='*', default=[],
                   help='Description text (positional, space-separated)')
    p.add_argument('--desc', default=None,
                   help='Description text (flag, alternative to positional)')
    p.add_argument('--depends-on', default=None,
                   help='Comma-separated dependency UUIDs')
    p.add_argument('--mode', choices=VALID_MODES, default=None,
                   help='Node execution mode: manual/checkpoint/auto (default: auto). '
                        'manual = a discussion node, no execute-review-fix loop; '
                        'checkpoint = the loop runs, then stops for your go-ahead; '
                        'auto = the loop runs and the line carries on by itself')
    p.add_argument('--weight', choices=VALID_WEIGHTS, default=None,
                   help='Node weight: light/standard (default: standard; the key is written only '
                        'for light). light = the conductor implements it itself and dispatches '
                        'review alone; standard = it dispatches Execute as usual')
    p.add_argument('--tags', default=None,
                   help='Comma-separated tags: '
                        'writing / coding / art. Untagged = never filtered')
    p.set_defaults(func=cmd_add)

    # rm
    p = subs.add_parser('rm', help='Remove an idea node')
    p.add_argument('uuid', help='Node UUID')
    p.add_argument('--force', action='store_true',
                   help='Force removal even if depended on')
    p.set_defaults(func=cmd_rm)

    # update
    p = subs.add_parser('update', help='Update title and/or description')
    p.add_argument('uuid', help='Node UUID')
    p.add_argument('--title', default=None, help='New title')
    p.add_argument('--desc', default=None, help='New description')
    p.add_argument('--mode', choices=VALID_MODES, default=None,
                   help='New execution mode: manual/checkpoint/auto')
    p.add_argument('--weight', choices=VALID_WEIGHTS, default=None,
                   help='New weight: light/standard (standard drops the key)')
    p.add_argument('--tags', default=None,
                   help='New tag set, comma-separated (replaces what the node carries)')
    p.add_argument('--clear-tags', action='store_true',
                   help='Drop every tag: the node becomes visible to all filter sets again')
    p.set_defaults(func=cmd_update)

    # depend
    p = subs.add_parser('depend', help='Add a dependency edge')
    p.add_argument('uuid', help='Node UUID that depends on --on')
    p.add_argument('--on', required=True, help='Dependency UUID')
    p.set_defaults(func=cmd_depend)

    # undepend
    p = subs.add_parser('undepend', help='Remove a dependency edge')
    p.add_argument('uuid', help='Node UUID')
    p.add_argument('dep', nargs='?', default=None,
                   help='Dependency UUID to remove (positional)')
    p.add_argument('--from', dest='from_', default=None,
                   help='Dependency UUID to remove (flag, alternative to positional)')
    p.set_defaults(func=cmd_undepend)

    # status
    p = subs.add_parser('status', help='Set node status')
    p.add_argument('uuid', help='Node UUID')
    p.add_argument('status', metavar='{todo,shelved,completed}',
                   choices=VALID_STATUSES,
                   help='New status: todo | shelved | completed. todo = in play, not delivered '
                        '(a node a cycle is working on is still todo); shelved = out of play, '
                        'waiting for a human (never ready, never recommended; release with '
                        'status <uuid> todo); completed = delivered')
    p.set_defaults(func=cmd_status)

    # approve
    p = subs.add_parser('approve',
                        help="Record that a human approved one node's delivery")
    p.add_argument('uuid', help='Node UUID')
    p.add_argument('--at', default=None,
                   help='The moment of the approval (default: now). Written as given, in the record '
                        'timestamp shape: 2026-10-16T15:13:44.160813Z')
    p.set_defaults(func=cmd_approve)

    # revive
    p = subs.add_parser('revive',
                        help='Put a shelved node back to todo so it can run again')
    p.add_argument('uuid', help='Node UUID')
    p.set_defaults(func=cmd_revive)

    # list
    p = subs.add_parser('list', help='List nodes sorted by urgency × importance')
    p.add_argument('--ready', action='store_true',
                   help='Only show ready nodes (todo, with every dependency completed)')
    p.add_argument('--status', nargs='+', metavar='{todo,shelved,completed}',
                   choices=VALID_STATUSES,
                   help='Filter by status(es): --status todo shelved completed')
    p.add_argument('--verbose', '-v', action='store_true',
                   help='Show dependencies and dependents')
    p.add_argument('--limit', '-n', type=int, default=0,
                   help='Max items to output (0 = unlimited)')
    p.add_argument('--tags', default=None,
                   help='Show only what a filter set like this would pick from: comma-separated '
                        'tags. An untagged node always stays in view')
    p.set_defaults(func=cmd_list)

    # info
    p = subs.add_parser('info', help='Show node details')
    p.add_argument('uuid', help='Node UUID')
    p.add_argument('--verbose', action='store_true',
                   help='Extended fields')
    p.set_defaults(func=cmd_info)

    # context
    p = subs.add_parser('context', help='Show full upstream/downstream context of a node')
    p.add_argument('uuid', help='Node UUID')
    p.set_defaults(func=cmd_context)

    # search
    p = subs.add_parser('search', help='Search nodes by title/description using regex')
    p.add_argument('query',
                   help="Regex pattern (case-insensitive). Always quote: search 'IYL|loading'")
    p.add_argument('--limit', '-n', type=int, default=5,
                   help='Max results (0 = unlimited)')
    p.add_argument('--active-only', action='store_true',
                   help='Exclude completed nodes from results')
    p.set_defaults(func=cmd_search)

    # open
    p = subs.add_parser('open', help='Print absolute spec.md path')
    p.add_argument('uuid', help='Node UUID')
    p.set_defaults(func=cmd_open)

    # validate
    p = subs.add_parser('validate', help='Validate graph integrity')
    p.set_defaults(func=cmd_validate)

    # help
    subs.add_parser('help', help='Show the full usage reference')

    return parser


def main():
    # Node titles go out as JSON, and this script's stdout encoding follows the console code page
    # (GBK by default on this machine). The caller (conduct_pipeline.py) decodes as UTF-8, and a GBK
    # byte stream makes its reader thread throw UnicodeDecodeError and stdout become None -- the
    # pipeline's first round boundary dies right there. Every entry point does the same reconfigure
    # itself (stderr too: the error text carries non-ASCII as well).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    # The project is recognized here once: `configure()` pins the ledger, and this run of the
    # command reads and writes by it.
    configure(_resolve_project_root(Path.cwd()))
    parser = _build_parser()
    args = parser.parse_args()
    if args.command == 'help':
        # `help` and `--help` print the same body: the verbs, flags and values are the ones in this
        # parser.
        parser.print_help()
        return
    args.func(args)


if __name__ == '__main__':
    main()
