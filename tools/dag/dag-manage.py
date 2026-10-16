#!/usr/bin/env python3
"""Manage a dependency graph (DAG) of project ideas/features.

Usage:
    dag-manage.py <command> [args]

Commands:
    add            Add a new idea node
    rm             Remove an idea node
    update         Update title and/or description
    depend         Add a dependency edge
    undepend       Remove a dependency edge
    status         Set node status (todo/shelved/completed)
    revive         Put a shelved node back to todo so it can run again
    list           List non-completed nodes
    info           Show node details
    context        Show transitive upstream/downstream context
    search         Search nodes by title/description
    open           Print absolute path to spec.md
    validate       Validate graph integrity

Tags:
    节点可以带 tag，也可以不带，也可以带多个。tag 说的是一件事属于哪一类活
    （`writing` 文案 / `coding` 代码 / `art` 图像），conductor 用它把一条 cycle 的选材
    范围圈出来。带 tag 的节点只对得上号的过滤集可见；**没有 tag 的节点留在所有范围里**
    —— 漏挡只多跑一个节点，误挡会让活永远没人做。

        add --tags art            打 tag（多个用逗号：--tags art,writing）
        update <uuid> --tags ...  改 tag；--clear-tags 摘掉全部（= 不受过滤）
        list --tags coding,writing  按一个过滤集看：它会挑到哪些节点

    词表外的 tag 警告一行，照收——词表摆在人面前给人看，engine 照单全收。

Weight:
    节点的一个 weight 管的是「这一棒谁干」，与 mode（管循环跑不跑、收尾停不停）正交：
    `light` 的节点由 conductor 自己实现、只把复审派出去；`standard` 走标准流程（派 Execute）。

        add --weight light                        轻到不值得派执行者
        update <uuid> --weight light|standard     改；写回 standard 等于把这个键摘掉

    **不写这个键就是 standard**：老图、老机器都不必迁移，只有真打过 light 的节点才落这个键。
"""

import argparse
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════════
#  Paths & Constants
# ═══════════════════════════════════════════════════════════════════════════════

# `tools/shared/` is the shared layer, and it has no package to import from: every file in it is
# loaded by path through `loader.py`. These two functions are that bootstrap, identical in every file
# that needs it -- a path load, never a name import, because nothing puts `tools/` on `sys.path`.
SHARED = Path(__file__).resolve().parent.parent / 'shared'

_LOADER = None


def _loader():
    """`tools/shared/loader.py`: the one loader, for the modules this tree keeps outside `shared/`."""
    global _LOADER
    if _LOADER is None:
        spec = importlib.util.spec_from_file_location('pf_shared_loader', SHARED / 'loader.py')
        _LOADER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_LOADER)
    return _LOADER


def _shared(name, filename):
    """One file out of `tools/shared/`, the same module for every caller in this process.

    Loaded for use, not copied: the ledger lock's reentrancy record is one per process, and a second
    copy of that module would give two of them.
    """
    return _loader().load_by_path(name, SHARED / filename, register=True, reuse=True)

# One directory per project: `.pf/` holds the graph, each node's own text (`spec.md`, reports), and
# the cycle book the conduct side keeps. A project either commits it or keeps it to itself -- the
# tool does not care which. The pipeline's plumbing (launch prompts, worktree, a node's temp files)
# sits under `.pf/meta/`, `.pf/worktrees/` and `.pf/nodes/<id>/temp/`, all three ignored by
# `.pf/.gitignore`.
#
# A `.pf/` the tool created by itself, never init'ed, is the one state that must stay unreachable:
# the lock's `mkdir` would otherwise leave a directory that is neither tracked nor ignored for the
# next `git add -A` to sweep up. `_require_initialized` closes that door; conduct and the issue tool
# each carry their own copy.
LEDGER_NAME = '.pf'
INIT_MARKERS = ('.gitignore', '.gitattributes')


def _resolve_project_root(cwd: Path) -> Path:
    """cwd 即根，唯一的例外：cwd 是一条 linked worktree 的检出根时，回主树。

    `conduct_state.store` 与 issue 侧各有一份同样的判定：worktree 根的 `.git` 是文件，指向
    `<主树>/.git/worktrees/<名字>`，`worktrees` 上三级即主树根；`.git/modules/...` 是
    submodule，不算。主树没有 `.pf/` 时不挪，让老错误照老样子响。
    """
    dot_git = cwd / '.git'
    if not dot_git.is_file():
        return cwd
    try:
        first_line = dot_git.read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return cwd
    if not first_line.startswith('gitdir:'):
        return cwd
    target = first_line.split(':', 1)[1].strip()
    gitdir = Path(target)
    if not gitdir.is_absolute():
        gitdir = (cwd / gitdir).resolve()
    if gitdir.parent.name != 'worktrees':
        return cwd
    main = gitdir.parent.parent.parent          # <main>/.git/worktrees/<name> → <main>
    if (main / LEDGER_NAME).is_dir():
        return main
    return cwd


PROJECT_ROOT = _resolve_project_root(Path.cwd())
LEDGER_DIR = PROJECT_ROOT / LEDGER_NAME
DATA_DIR = LEDGER_DIR
NODES_DIR = DATA_DIR / 'nodes'
IDEAS_JSON = DATA_DIR / 'ideas.json'


def _session_note():
    """`tools/shared/session-note.py` -- one module for every caller in this process.

    The conduct side writes every run's id to the same file; this side writes the discussion a node
    was born in, and reads both back for `info`.
    """
    return _shared('pf_shared_sessionnote', 'session-note.py')


def _note_session(node_id: str, session: str) -> None:
    """Note the session this node was born in -- best effort, and never in the way.

    It is the discussion the node was routed in, which is what makes "is this the user's own words,
    or did the spec drift" answerable later. Failure to write it must not undo an `add`: the node is
    the point, and the note is plumbing that can be rebuilt by hand.
    """
    try:
        _session_note().append(PROJECT_ROOT, node_id, 'discussion', session)
    except (OSError, ValueError) as e:
        print(f'Warning: the session note for {node_id} was not written: {e}', file=sys.stderr)

#: 卡片状态只有三个词，写出去的就是这三个：`todo` 在局中未交付、`shelved` 出局等人、
#: `completed` 已交付。读到一个词表外的词就照原样读它，绝不替它猜一个词。
#: 「正在被某条 cycle 跑」不进状态（那样的节点还是 `todo`），它记在 state.json 的持有关系里。
VALID_STATUSES = {'todo', 'shelved', 'completed'}
# A blocked node is never recommended and never "ready": `shelved` is out of play until a human
# puts it back in (the round ran and the verdict was no, or it was shelved on purpose).
# It comes back with `status <uuid> todo`.
BLOCKED_STATUSES = {'shelved'}
#: 节点执行模式三档，从「人参与得多」到「人参与得少」：
#:   manual      —— 没有 execute→review→fix 循环，是一个讨论格：机主提、AI 提、被驳、再提。
#:   checkpoint  —— 循环照跑，收尾停下等机主说一句「继续」，才开下一个节点。
#:   auto        —— 循环照跑，收尾自己开下一个节点（没有人闸；卡死、需求冲突才报机主）。
VALID_MODES = {'manual', 'checkpoint', 'auto'}
DEFAULT_MODE = 'auto'
#: 节点重量，两档，与 mode 正交：mode 管停不停，它管谁干。**键不落就是 standard**，
#: 所以只有 light 落键。
VALID_WEIGHTS = ('light', 'standard')
DEFAULT_WEIGHT = 'standard'
# ── tags ─────────────────────────────────────────────────────────────────────
# 三个词，固定。它是一份给人看的约定，engine 只拿它拼那句警告：词表外的 tag 警告一行就收下，
# 真的冒出第四类工作时写进去就能用。过滤一律按字面比，不查词表。
VALID_TAGS = ('writing', 'coding', 'art')
#: 词表外的 tag 照收、警告一行，只能和写了同一个词的过滤集对上（见 `_parse_tags`）。
ISO_FMT = '%Y-%m-%dT%H:%M:%S.%fZ'


# ═══════════════════════════════════════════════════════════════════════════════
#  Low-level helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_dirs() -> None:
    """Create data directories if they do not exist."""
    NODES_DIR.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).strftime(ISO_FMT)


def _read() -> dict:
    """Read ideas.json; auto-create on first use.

    The read itself -- absent file, bytes that are not UTF-8, text that is not JSON, and the words
    each of those gets -- is `tools/shared/jsonio.py`'s. This layer is the CLI, so a record that
    cannot be read ends the command with that message, plus the one hint this tool has for it.
    """
    jsonio = _shared('pf_shared_jsonio', 'jsonio.py')
    try:
        data = jsonio.read_json_strict(IDEAS_JSON, 'ideas.json') or {'nodes': {}}
    except jsonio.JsonReadError as e:
        print(f'Error: {e}', file=sys.stderr)
        if isinstance(e.__cause__, json.JSONDecodeError):
            print('Hint: run "validate" for details.', file=sys.stderr)
        sys.exit(1)
    data.setdefault('nodes', {})
    return data


def _write(data: dict) -> None:
    """Atomically write ideas.json: temp file, fsync, a replace that tolerates a live reader.

    The write is `tools/shared/jsonio.py`'s, the same one every other record in this tree goes
    through; `sort_keys` is this file's own habit, so a rewritten graph reads the same way twice.
    """
    _ensure_dirs()
    _shared('pf_shared_jsonio', 'jsonio.py').atomic_write_json(IDEAS_JSON, data, sort_keys=True)


# ── the cross-process ledger lock ─────────────────────────────────────────────
# ideas.json is whole-file read-modify-write; two processes walking that at once means the later
# write erases the earlier one. Every MUTATING command body holds the project's one lock across
# its read → write -- the same `.pf/meta/ledger.lock` file conduct, the issue tool and a session
# note's own writer use, so writes exclude each other across all of them. Reentrant per process
# (this body writes a node's note inside its own read-modify-write).


def _require_initialized() -> None:
    """没有 init 过的 `.pf/` 什么都不许写：锁的 `mkdir` 是唯一会把它造出来的东西。

    读的是 `LEDGER_DIR` 而不是 `PROJECT_ROOT`：这个模块的路径在进程内可以被调用方重新绑到
    另一个项目上（测试夹具就是这么做的），根却冻在导入那一刻。
    """
    if LEDGER_DIR.is_dir() and all((LEDGER_DIR / m).is_file() for m in INIT_MARKERS):
        return
    print(f'Error: {LEDGER_DIR} 没 init 过（缺 {" 或 ".join(INIT_MARKERS)}）。先跑：\n'
          f'  pf init', file=sys.stderr)
    sys.exit(1)


@contextlib.contextmanager
def _ledger_lock(timeout: float | None = None):
    """Hold the project's ledger lock for the duration of one read-modify-write.

    The lock itself is `tools/shared/ledgerlock.py`. A caller that cannot get it within `timeout` is
    told loudly and exits 1 -- wedging silently is the one failure this must not have.
    """
    lock = _shared('pf_shared_ledgerlock', 'ledgerlock.py')
    kwargs = {} if timeout is None else {'timeout': timeout}
    try:
        with lock.ledger_lock(LEDGER_DIR, gate=_require_initialized, **kwargs):
            yield
    except TimeoutError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


def _resolve_uuid(uid: str, data: dict) -> str:
    """Resolve to full UUID (pass-through or prefix match); assumes _get already validated."""
    if uid in data['nodes']:
        return uid
    for k in data['nodes']:
        if k.startswith(uid):
            return k
    return uid  # unreachable if _get was called first


def _get(uid: str, data: dict) -> dict:
    """Look up a node by UUID (exact or prefix match); print error and exit if missing."""
    node = data['nodes'].get(uid)
    if node is not None:
        return node
    # Prefix matching for convenience (e.g. "7a6aab2c" → full UUID)
    matches = [k for k in data['nodes'] if k.startswith(uid)]
    if len(matches) == 1:
        return data['nodes'][matches[0]]
    if len(matches) > 1:
        print(f'Error: "{uid}" matches multiple nodes ({", ".join(matches)})',
              file=sys.stderr)
        sys.exit(1)
    print(f'Error: node {uid} not found', file=sys.stderr)
    sys.exit(1)


def _parse_tags(raw: str) -> list:
    """`--tags art,writing` → `['art', 'writing']`：去空白、去重，保序。

    按字面收：写它只能和写了同一个词的过滤集对上。
    词表外的 tag 警告一行就收下 —— 真冒出第四类工作，写进去就能用。
    """
    out = []
    for item in (raw or '').split(','):
        tag = item.strip()
        if tag and tag not in out:
            out.append(tag)
    unknown = [t for t in out if t not in VALID_TAGS]
    if unknown:
        print(f'Warning: tag {", ".join(unknown)} 不在词表 {"/".join(VALID_TAGS)} 里；照收，'
              f'但它只能和写了同一个词的过滤集对上。', file=sys.stderr)
    return out


def _node_weight(node: dict) -> str:
    """一个节点的 weight；没写、或写了个不认识的词，都按 standard。

    保守方向是**多派一个人干活**：读不准就照标准流程走，别让一个节点因为记录里一个错字而由
    conductor 亲自上阵。
    """
    return 'light' if node.get('weight') == 'light' else DEFAULT_WEIGHT


def _node_tags(node: dict) -> list:
    """一个节点身上的 tag；没打过就是空表（= 不受任何过滤集约束）。"""
    tags = node.get('tags') or []
    return [t for t in tags if isinstance(t, str) and t.strip()]


def _in_tag_scope(node: dict, wanted) -> bool:
    """这个节点在一个过滤集里吗？至少一个 tag 相交就算命中；一个 tag 都没有的节点处处可见。

    保守方向是**宁可漏挡**：多跑一个节点只费一轮，误挡会让一条线的活永远没人做。
    """
    if not wanted:
        return True                     # 没有过滤集 = 整张图都是范围（今天的行为）
    tags = _node_tags(node)
    if not tags:
        return True
    return bool(set(tags) & set(wanted))


# ═══════════════════════════════════════════════════════════════════════════════
#  Graph algorithms
# ═══════════════════════════════════════════════════════════════════════════════

def _adjacency(nodes: dict) -> dict:
    """Build {uid: [dep_uid, ...]} from a nodes dict."""
    return {uid: list(n.get('dependencies', [])) for uid, n in nodes.items()}


def _reverse_adj(adj: dict) -> dict:
    """Reverse adjacency: {uid: [dependent_uid, ...]}."""
    rev = {u: [] for u in adj}
    for u, deps in adj.items():
        for v in deps:
            if v in rev:
                rev[v].append(u)
    return rev


def _nc_nodes(data: dict) -> dict:
    """Return sub-dict of non-completed nodes."""
    return {k: v for k, v in data['nodes'].items()
            if v.get('status') != 'completed'}


def _can_reach(adj: dict, start: str, target: str,
               visited: set = None):
    """Return (bool, path) whether target is reachable from start."""
    if visited is None:
        visited = set()
    if start == target:
        return True, [target]
    if start in visited:
        return False, []
    visited.add(start)
    for v in adj.get(start, []):
        if v not in adj:          # dangling reference -- skip
            continue
        ok, path = _can_reach(adj, v, target, visited)
        if ok:
            return True, [start] + path
    return False, []


def _topological_sort(adj: dict) -> list:
    """Kahn topological sort; return [uid, ...] with dependencies before dependents."""
    rev = _reverse_adj(adj)
    in_deg = {}
    for u in adj:
        count = 0
        for v in adj[u]:
            if v in adj:
                count += 1
        in_deg[u] = count

    order = []
    queue = [u for u in in_deg if in_deg[u] == 0]
    while queue:
        u = queue.pop(0)
        order.append(u)
        for v in rev.get(u, []):
            if v in in_deg:
                in_deg[v] -= 1
                if in_deg[v] == 0:
                    queue.append(v)

    # Any remaining nodes (cycles) appended at the end
    for u in adj:
        if u not in order:
            order.append(u)
    return order


def _urgency_scores(adj: dict) -> dict:
    """Return {uid: urgency} where higher = more urgent = fewer blockers.

    Normalized so the least urgent node in the graph gets 0.
    """
    topo = _topological_sort(adj)
    dep_set = {}
    for u in topo:
        s = {u}
        for v in adj[u]:
            if v in dep_set:
                s |= dep_set[v]
        dep_set[u] = s
    raw = {u: -(len(s) - 1) for u, s in dep_set.items()}
    if not raw:
        return raw
    offset = -min(raw.values())
    return {u: v + offset for u, v in raw.items()}


def _importance_scores(adj: dict) -> dict:
    """Reverse-set DP: count transitive dependents (excl. self).

    Higher = more nodes depend on this = more important.
    """
    topo = _topological_sort(adj)
    rev = _reverse_adj(adj)
    dep_set = {}
    for u in reversed(topo):
        s = {u}
        for v in rev.get(u, []):
            if v in dep_set:
                s |= dep_set[v]
        dep_set[u] = s
    return {u: len(s) - 1 for u, s in dep_set.items()}


def _is_ready(deps: list, all_nodes: dict) -> bool:
    """A node is ready when every dependency is completed or absent."""
    for d in deps:
        n = all_nodes.get(d)
        if n is not None and n.get('status') != 'completed':
            return False
    return True


def _node_ready(node: dict, all_nodes: dict) -> bool:
    """`ready` 的唯一定义：节点自己是 `todo`，且每个依赖都已 `completed`（或不存在）。

    `completed` 的节点不该被推荐（活已经交了），`shelved` 的节点在等人。这比 _is_ready 更窄：
    依赖满足了但节点自己不是 `todo`，照旧不 ready。

    另一半是**管线的事，这个工具不碰**：一条 cycle 正拿在手上的节点，管线那边自己不挑
    （那是 state.json 里的记录）。所以这里不读 state.json，也不知道 cycle 是什么 ——
    `ready` 只说这张图。调用方传进来的 node 就是盘上那个词。
    """
    return node.get('status') == 'todo' and _is_ready(node.get('dependencies', []), all_nodes)


def _detect_cycle(adj: dict):
    """Three-colour DFS cycle detection. Returns (has_cycle, path_list)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    colour = {u: WHITE for u in adj}
    parent = {}
    out_path = []

    def dfs(u):
        colour[u] = GRAY
        for v in adj.get(u, []):
            if v not in colour:
                continue
            if colour[v] == GRAY:                     # back edge
                path = [v]
                cur = u
                while cur != v:
                    path.append(cur)
                    cur = parent.get(cur)
                    if cur is None:
                        break
                path.append(v)
                path.reverse()
                out_path.extend(path)
                return True
            if colour[v] == WHITE:
                parent[v] = u
                if dfs(v):
                    return True
        colour[u] = BLACK
        return False

    for u in adj:
        if colour[u] == WHITE:
            if dfs(u):
                return True, out_path
    return False, []


def _try_add_dep(data: dict, uid: str, dep: str) -> None:
    """Add dep to uid's dependencies; check cycles. Exits on failure."""
    if dep == uid:
        print('Error: self-dependency is not allowed', file=sys.stderr)
        sys.exit(1)
    deps = data['nodes'][uid]['dependencies']
    if dep in deps:
        return              # already present -- no-op
    deps.append(dep)
    adj = _adjacency(data['nodes'])
    found, path = _can_reach(adj, dep, uid)
    if found:
        cycle_str = ' → '.join([uid] + path)
        print(f'Error: cycle detected: {cycle_str}', file=sys.stderr)
        deps.remove(dep)
        sys.exit(1)


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
        # tag 只在真打过时才落这个键：没打过 tag 的节点在图上就是今天这份形状，一个字节都不多。
        tags = _parse_tags(args.tags) if args.tags else []
        if tags:
            data['nodes'][uid]['tags'] = tags
        # weight 同理，只在非默认时落键：standard 的节点在图上就是今天这份形状，一个字节都不多。
        if args.weight == 'light':
            data['nodes'][uid]['weight'] = 'light'
        # 出生 session 不落在记录里：`ideas.json` 是共享的，而 session 只在本机有意义 —— 另一个机器上
        # 它是一个必然打不开的死值，还分不出「那是别台机器」和「这场被删了」。所以它落在
        # `.pf/meta/sessions/<uuid>.json`（`meta/` 由 `.pf/.gitignore` 挡在版本库外），见
        # `tools/shared/session-note.py`。仍然由工具读环境变量，不由调用方抄一遍 —— 抄的那份总会漏。
        # 写在 `_write(data)` 之后：依赖校验失败会 `_rollback_add`，那一格没落图就不该留便条。
        session = os.environ.get('DSH_SESSION_ID')

        node_dir = NODES_DIR / uid
        node_dir.mkdir(parents=True, exist_ok=True)
        spec_path = str(node_dir / 'spec.md')

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
        # 便条写在这里：依赖校验失败会 `_rollback_add`，那一格没落图就不该留便条。
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
        _get(args.uuid, data)

        # Check for dependents
        dependents = []
        for nid, node in data['nodes'].items():
            if args.uuid in node.get('dependencies', []):
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
            if args.uuid in deps:
                deps.remove(args.uuid)

        del data['nodes'][args.uuid]
        _write(data)

        node_dir = NODES_DIR / args.uuid
        if node_dir.exists():
            shutil.rmtree(node_dir)
        # 便条跟着节点走：节点从图上没了，它的那场对话也就不再是"这一格的"对话。
        _session_note().drop(PROJECT_ROOT, args.uuid)

        print(f'Removed {args.uuid}')


def cmd_update(args):
    with _ledger_lock():
        data = _read()
        node = _get(args.uuid, data)
        if args.tags is not None and args.clear_tags:
            print('Error: --tags 和 --clear-tags 一次只给一个（前者换一组，后者摘干净）',
                  file=sys.stderr)
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
            # 写回 standard 就是把这个键摘掉：记录里只有 light 落键（见 DEFAULT_WEIGHT 那一段）。
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
                print('Warning: --tags 是空的；要摘掉全部 tag 请用 --clear-tags', file=sys.stderr)
        if args.clear_tags:
            # 摘干净 = 这个节点回到「处处可见」：没有 tag 的节点在任何过滤集里都留在选材范围。
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
        _get(uid, data)
        _get(dep, data)
        _try_add_dep(data, uid, dep)
        data['nodes'][uid]['updated_at'] = _now()
        _write(data)


def cmd_undepend(args):
    with _ledger_lock():
        data = _read()
        _get(args.uuid, data)
        dep_uuid = args.from_
        if args.dep:
            dep_uuid = args.dep
        if not dep_uuid:
            print('Error: specify dependency to remove via positional arg or --from', file=sys.stderr)
            sys.exit(1)
        deps = data['nodes'][args.uuid].get('dependencies', [])
        if dep_uuid in deps:
            deps.remove(dep_uuid)
            data['nodes'][args.uuid]['updated_at'] = _now()
            _write(data)
        # No-op if dep-uuid not in dependencies array (or doesn't exist)


def cmd_status(args):
    with _ledger_lock():
        data = _read()
        node = _get(args.uuid, data)
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
        # 完结时刻是节点自己的事实，不藏在某条 cycle 的 path 里：按它排得出「最近做完的」。
        # updated_at 当不得这个用——标题、描述、模式的改动都会顶它。
        # 离开 completed 时摘掉：一个不在 completed 的节点不该带着完结时刻。
        if status == 'completed':
            node['completed_at'] = node['updated_at']
        else:
            node.pop('completed_at', None)
        _write(data)


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
        node = _get(args.uuid, data)
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
    data = _read()

    # Filter by status(es) if --status given: a literal match against the word on disk.
    if args.status:
        status_set = set(args.status)
        nodes = {k: v for k, v in data['nodes'].items()
                 if v.get('status') in status_set}
    else:
        nodes = data['nodes']  # all

    rev = _reverse_adj(_adjacency(nodes))

    # Compute urgency/importance on non-completed subgraph (philosophy rule)
    nc = _nc_nodes(data)
    adj = _adjacency(nc)
    urgency_scores = _urgency_scores(adj)
    imp_scores = _importance_scores(adj)

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
            # `ready` 就是 _node_ready 那一条：自己是 todo、依赖都 done。这个字段和 `--ready`
            # 过滤器说的必须是同一件事，否则过滤器一句话就被绕过。
            'ready': _node_ready(node, data['nodes']),
        }
        # tag 回显：读这份清单的人要一眼看出这个节点归哪一类活、为什么它进/不进某个过滤集。
        # 没打过 tag 的节点不落这个键 —— 整张图都没 tag 时，输出与今天逐字节相同。
        tags = _node_tags(node)
        if tags:
            entry['tags'] = tags
        # weight 回显同 tags：没有 light 节点的图，输出与今天逐字节相同。
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
        # 只有 todo 节点在 ready 里（`e['ready']` 已经说了这句）：shelved 在等人、completed 已交，
        # 依赖没满足的还在等 —— 无依赖的根节点也不是自动 ready。
        result = [e for e in result if e['ready']]

    if args.tags:
        # 「这个过滤集会挑到哪些节点」：命中 = 至少一个 tag 相交，或者一个 tag 都没有
        # （没有 tag 的节点留在所有范围里）。这一步只删行 —— urgency/importance 仍在整张
        # 非完成子图上算，下面的排序一个字没动。
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
    data = _read()
    node = _get(args.uuid, data)
    all_nodes = data['nodes']
    uid = _resolve_uuid(args.uuid, data)

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
    adj = _adjacency(nc)
    urgency_scores = _urgency_scores(adj)
    imp_scores = _importance_scores(adj)

    blocked = node.get('status', 'todo') in BLOCKED_STATUSES
    result = {
        'id': uid,
        'title': node.get('title', ''),
        'status': node.get('status', 'todo'),
        'mode': node.get('mode', DEFAULT_MODE),
        'description': node.get('description', ''),
        'depends_on': deps_list,
        'depended_by': dep_by_list,
        # 和 list 说的必须是同一件事（见 _node_ready）：自己是 todo、依赖都 done。
        'ready': _node_ready(node, all_nodes),
        # shelved 的节点不排优先级（它不在可跑的集合里），所以两项分数照旧归零。
        'urgency_level': 0 if blocked else urgency_scores.get(uid, 0),
        'importance_score': 0 if blocked else imp_scores.get(uid, 0),
    }

    if _node_weight(node) == 'light':
        result['weight'] = 'light'

    if args.verbose:
        result['created_at'] = node.get('created_at', '')
        result['updated_at'] = node.get('updated_at', '')

    # 会话便条（讨论场 / 执行场）有才回显。`list` 给的是骨架，`info` 是这一格的详情——
    # 调用方不许读 ideas.json 原文，所以这两个键不出现在这里，就等于没记。
    note = _session_note().read(PROJECT_ROOT, uid)
    if note['discussion'] or note['run']:
        result['sessions'] = {role: note[role] for role in ('discussion', 'run')}
    if node.get('completed_at'):
        result['completed_at'] = node['completed_at']

    print(json.dumps(result, ensure_ascii=False))


def cmd_context(args):
    """Show full upstream/downstream transitive context of a node."""
    data = _read()
    node = _get(args.uuid, data)
    all_nodes = data['nodes']
    uid = _resolve_uuid(args.uuid, data)

    adj = _adjacency(all_nodes)
    rev = _reverse_adj(adj)

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
            # 和 `list`/`info` 说的同一件事（见 _node_ready）。
            'ready': _node_ready(node, data['nodes']),
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
    _get(args.uuid, data)
    uid = _resolve_uuid(args.uuid, data)
    spec_path = NODES_DIR / uid / 'spec.md'
    print(str(spec_path))


def cmd_validate(args):
    data = _read()
    nodes = data['nodes']
    total = len(nodes)

    status_counts = {}
    for n in nodes.values():
        s = n.get('status', 'unknown')
        status_counts[s] = status_counts.get(s, 0) + 1

    # -- Check 1 : no cycles --
    adj = _adjacency(nodes)
    has_cycle, cycle_path = _detect_cycle(adj)
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
    p.add_argument('query', help="Regex pattern (case-insensitive). Always quote: search 'IYL|加载'")
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

    return parser


def main():
    # 中文节点标题走 JSON 输出，而这份脚本的 stdout 编码跟的是控制台代码页（这台机器默认 GBK）。
    # 调用方（conduct_pipeline.py）按 UTF-8 解码，GBK 字节流会让它的读线程直接抛
    # UnicodeDecodeError、stdout 变成 None —— 管线的第一个轮边界就崩在那里。
    # 每个入口自己做同样的 reconfigure（stderr 同理：报错也是中文）。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = _build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
