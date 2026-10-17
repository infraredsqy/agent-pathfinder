"""The merge driver's body: one three-way merge of one `.pf/` record, and the rules it runs on.

`ledger-merge.py` is the entry point -- the argv surface git invokes -- and nothing else; everything
behind it is here. The layers:

    generic.py        the three-way primitives, the decision log, and the record vocabulary
    ideas.py          `.pf/ideas.json` -- the DAG
    state.py          `.pf/state.json` -- the cycles
    instructions.py   `.pf/instructions.json` -- the standing instructions
    signal.py         a node's ticket under `.pf/signals/<cycle>/<node>.json`

The two tools it leans on, each by the same rule -- the sibling directory of the shipped tree, or the
tool's own variable for a review draft or a drill copy that points at the real tool:

    DSH_CONDUCT_DAG_MANAGE   where `dag-manage.py` is (it owns the graph's bytes)
    DSH_CONDUCT_STATE_DIR    where the `conduct_state` package is (it owns the other three records)

Which ledger file is this? (the fourth argument)
------------------------------------------------
Under git, `<ours>` is NOT the worktree file: for a content merge git materializes all three sides as
its own temp files (`.merge_file_XXXXXXXX`) and copies `<ours>` into place afterwards. So the driver
is told which ledger it is looking at by git's `%P`, the path in the repo, and the recommended
`merge.pf.driver` command passes it as the fourth argument. Name first (`%P`, then `<ours>`/`<theirs>`
/`<base>` for a hand-run), shape second (a `nodes` / `cycles` / `instructions` key, else a ticket), and
a file that is neither is refused with exit 2.

Why a driver at all
-------------------
Two machines can each run a pipeline against their own copy of the same records. Every writer rewrites
its WHOLE file from an in-memory dict (`json.dump(..., indent=2, ensure_ascii=False)` -- plus
`sort_keys=True` for `ideas.json`, CRLF line endings, no trailing newline), so a line-level merge of
two ledgers is not a merge: two machines that each touched one field produce two files that differ on
every line. `git merge` would then stop on a conflict that is not one, or worse, `ort` would "cleanly"
produce a nonsense mixture of the two files. So the merge is done on the parsed records, and the
result is written back through the tools' OWN writers, byte for byte the way `state-manage.py` /
`dag-manage.py` would have written it: this module asks each file's owner for the record's bytes
(`conduct_state.store.record_bytes`, `dag-manage.py`'s `ideas_bytes`) and hands them back to that
same owner to land (`write_record_bytes` / `write_ideas_bytes`), so a merge's judgement and its bytes
come from one tree and the two formats can never drift apart.

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
                                                         otherwise the newer file mtime (tie: ours)

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
-----------------------------------------------------------------------------
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
  * A node deleted while another node still depends on it is checked (`ideas._flag_broken_dependency`),
    but only for edges THIS merge breaks: an edge already dangling on one side stays that side's
    problem, and `dag-manage validate` is the place for the graph as a whole.
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
    leaves the bytes alone -- and `run` skips the write entirely when they are unchanged.
  * A ticket (signals) is one report and is never resolved field by field against a rival: recency
    decides it, with a terminal status privileged.

Exit codes, and what git does with them
---------------------------------------
    0   merged and written into <ours> (or nothing needed doing)
    1   a human has to decide, or the project's ledger lock stayed busy: NOTHING is written, so git
        keeps <ours> and records the conflict
    2   usage error: a file that cannot be read as a ledger, or a tool this driver was pointed at by
        name (`DSH_CONDUCT_DAG_MANAGE` / `DSH_CONDUCT_STATE_DIR`) whose path holds nothing
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from . import generic, ideas, instructions, signal, state
from .generic import (EXIT_CONFLICT, EXIT_OK, EXIT_USAGE, KIND_IDEAS, KIND_INSTRUCTIONS,
                      KIND_STATE, Report, _kind_from_shape, _kind_of)

HERE = Path(__file__).resolve().parent.parent   # the conduct tool directory

# `tools/shared/` is the shared layer, and it has no package to import from: every file in it is
# loaded by path through `loader.py`. This function is that bootstrap, the same shape in every file
# that needs it -- a path load, never a name import, because nothing puts `tools/` on `sys.path`.
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


def _load_by_path(name: str, path: Path, **kwargs):
    """Load a module (or package) by path through the tree's one loader.

    `package_dir` names a package's own directory; the loader registers that, which is what makes
    the package's relative imports resolve. `register` leaves a plain module in `sys.modules`.
    """
    return _loader().load_by_path(name, path, **kwargs)


def _shared(name: str, filename: str):
    """One file out of `tools/shared/`, the same module for every caller in this process.

    Loaded for use, not copied: the ledger lock's reentrancy record is one per process, and a second
    copy of that module would give two of them.
    """
    return _loader().load_by_path(name, SHARED / filename, register=True, reuse=True)


#: The two tools this driver leans on: the sibling directories of the shipped tree, or the variable
#: each one's name stands for (see this module's docstring). One rule for both, resolved by
#: `tools/shared/toolpath.py`.
DAG_MANAGE = _shared('pf_shared_toolpath', 'toolpath.py').tool_path(
    'DSH_CONDUCT_DAG_MANAGE', HERE.parent / 'dag' / 'dag-manage.py')
CONDUCT_STATE = _shared('pf_shared_toolpath', 'toolpath.py').tool_path(
    'DSH_CONDUCT_STATE_DIR', HERE / 'conduct_state')

_STORE = None
_DAG = None


def store():
    """`conduct_state.store`: the tool's own reader and atomic writer, loaded by path.

    Under a private name, so a caller that already has `conduct_state` loaded (the test harness, an
    in-process launcher) keeps its own copy.
    """
    global _STORE
    if _STORE is None:
        pkg = _load_by_path('pf_ledger_conduct_state', CONDUCT_STATE / '__init__.py',
                            package_dir=CONDUCT_STATE)
        _STORE = pkg.store
    return _STORE


def dag_manage():
    """`dag-manage.py`: it owns `ideas.json`, and its record's bytes are asked for, never rewritten."""
    global _DAG
    if _DAG is None:
        if not DAG_MANAGE.exists():
            raise FileNotFoundError(f'dag-manage.py not found at {DAG_MANAGE}')
        _DAG = _load_by_path('pf_ledger_dag_manage', DAG_MANAGE, register=True)
    return _DAG


# ═══════════════════════════════════════════════════════════════════════════════
#  The tools this reuses: their reader, their bytes, their lock
# ═══════════════════════════════════════════════════════════════════════════════

def _read(path: Path, label: str):
    """One side of the merge, or None when the file is absent or empty.

    git hands an EMPTY file for an add/add, and a missing one for "this side has nothing"; neither is
    an error. A file that exists but is not JSON IS one: the read and the wording of each failure are
    `tools/shared/jsonio.py`'s, and they are turned into this driver's own line plus exit 2 by `run`.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw.strip():
        return None
    jsonio = _shared('pf_shared_jsonio', 'jsonio.py')
    try:
        return jsonio.read_json_strict(path, label)
    except jsonio.JsonReadError as e:
        print(f'Error: {e}', file=sys.stderr)
        print(f'ledger-merge: {label} 不是合法 JSON，不写任何东西', file=sys.stderr)
        raise


def _write(kind: str, path: Path, payload: bytes) -> bool:
    """Land the merged bytes under the project's ledger lock; False when the lock never came free.

    `.pf/meta/ledger.lock` is what makes this driver exclude the mutating commands (conduct / dag /
    issue): the driver rewrites the WHOLE file, and a whole-file write racing another whole-file write
    silently erases one side's records. The lock itself is `tools/shared/ledgerlock.py` -- one
    implementation for every writer of a record here -- and git's cwd (the worktree the merge runs in)
    maps to the main tree by the store's own rule, which is where the lock (and the ledger) live. A
    lock that stays busy is a write that could not be made exclusively: the driver exits 1, git marks
    the conflict, and a human reruns the merge.

    The bytes go down through `_owner_write`, the same owner `_owner_bytes` computed them in, so the
    judgement and the landing are never two trees' answers.
    """
    lock = _shared('pf_shared_ledgerlock', 'ledgerlock.py')
    try:
        with lock.ledger_lock(store().ledger_dir_for(), gate=store().require_initialized):
            _owner_write(kind, path, payload)
    except TimeoutError as e:
        print(f'Error: {e}', file=sys.stderr)
        return False
    return True


def _owner_bytes(kind: str, data) -> bytes:
    """The bytes the file's OWNER would leave -- for "did this merge change anything at all".

    `ideas.json` is dag-manage's file and `state.json` / `instructions.json` / a ticket are the
    store's, so each is asked for its own record's bytes. Spelling either format out here instead
    would be a second copy of a writer: one byte of drift and every merge would look like a change.
    """
    if kind == KIND_IDEAS:
        return dag_manage().ideas_bytes(data)
    return store().record_bytes(data)


def _owner_write(kind: str, path: Path, payload: bytes) -> None:
    """Those same bytes, landed by the same owner that computed them.

    The landing primitive is `tools/shared/jsonio.py` in the OWNER's tree, not this module's:
    `DSH_CONDUCT_STATE_DIR` / `DSH_CONDUCT_DAG_MANAGE` can point a draft copy at another tree, and a
    file judged by one tree's format and written by another's writer is two answers to one question.
    """
    if kind == KIND_IDEAS:
        dag_manage().write_ideas_bytes(path, payload)
    else:
        store().write_record_bytes(path, payload)


# ═══════════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════════

def _merge(kind, base, ours, theirs, rep, ours_path, theirs_path):
    if kind == KIND_IDEAS:
        return ideas.merge_ideas(base, ours, theirs, rep)
    if kind == KIND_STATE:
        return state.merge_state(base, ours, theirs, rep)
    if kind == KIND_INSTRUCTIONS:
        return instructions.merge_instructions(base, ours, theirs, rep)
    return signal.merge_signal(base, ours, theirs, rep, ours_path, theirs_path)


def _no_such_tool(e: FileNotFoundError) -> int:
    """The tree this driver was pointed at holds no such tool: one line, and a usage error.

    `DSH_CONDUCT_DAG_MANAGE` / `DSH_CONDUCT_STATE_DIR` name where a tool lives, and a path that holds
    nothing gets the same answer as a file that cannot be read as a ledger: exit 2, with a person left
    to fix the command or the setting. This driver runs unattended in the middle of somebody's
    `git pull`, so a traceback here is noise nobody reads -- the line says which path came up empty.
    """
    print(f'ledger-merge: {e}', file=sys.stderr)
    return EXIT_USAGE


def run(args) -> int:
    """One parsed command line: read the three sides, merge them, and say what git should do."""
    ours_path, theirs_path, base_path = Path(args.ours), Path(args.theirs), Path(args.base)
    kind = (_kind_of(args.path) or _kind_of(args.ours) or _kind_of(args.theirs)
            or _kind_of(args.base))
    jsonio = _shared('pf_shared_jsonio', 'jsonio.py')
    try:
        base = _read(base_path, f'base {base_path.name}')
        ours = _read(ours_path, f'ours {ours_path.name}')
        theirs = _read(theirs_path, f'theirs {theirs_path.name}')
    except jsonio.JsonReadError:
        return EXIT_USAGE
    except FileNotFoundError as e:
        return _no_such_tool(e)

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
        payload = _owner_bytes(kind, merged)
    except (TypeError, ValueError) as e:
        print(f'ledger-merge [{rep.path}]: 合出来的结果不是合法 JSON：{e}', file=sys.stderr)
        return EXIT_USAGE
    except FileNotFoundError as e:
        return _no_such_tool(e)
    try:
        current = ours_path.read_bytes()
    except OSError:
        current = None
    if current == payload:
        return EXIT_OK                    # nothing to change: do not touch the file at all
    try:
        wrote = _write(kind, ours_path, payload)
    except FileNotFoundError as e:
        return _no_such_tool(e)
    return EXIT_OK if wrote else EXIT_CONFLICT


#: What `ledger-merge.py` reaches for, and what a caller that loads this package gets: the run
#: itself, the two dispatch decisions (which ledger is this), the store binding an in-process caller
#: aims at its own project, and the layers a reader opens. A name that appears here and nowhere else
#: is a loud ImportError at load time, not a missing attribute later.
__all__ = [
    # the run, and the tools it reuses
    'run', 'store', 'dag_manage',

    # the two dispatch decisions: which ledger a path or a shape is
    '_kind_of', '_kind_from_shape',

    # the layers themselves, for a caller that wants one engine alone
    'generic', 'ideas', 'state', 'instructions', 'signal',
]

_LAYERS = (generic, ideas, state, instructions, signal)

for _name in __all__:
    if _name in globals():
        continue
    for _layer in _LAYERS:
        if hasattr(_layer, _name):
            globals()[_name] = getattr(_layer, _name)
            break
    else:
        raise ImportError(f'ledger_merge.__all__ names {_name!r}, which no layer defines')