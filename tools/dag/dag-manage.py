#!/usr/bin/env python3
"""Manage a dependency graph (DAG) of project ideas/features.

The verbs, their flags and their values are this file's parser: `dag-manage.py --help` and
`dag-manage.py help` print them.

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
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════════
#  Paths & Constants
# ═══════════════════════════════════════════════════════════════════════════════

# 引导：装载器也按路径取，所以这一段只能每个文件自己带（见 `shared/loader.py`）。
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
    """`tools/shared/ledger.py`: `.pf/` 的落点、worktree 回主树那条判定、init 闸门 —— 正本一处。"""
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
    """cwd 即根，唯一例外：cwd 是一条 linked worktree 的检出根时回主树（判定在 ledger 正本里）。"""
    return _ledger().resolve_project_root(cwd)


#: 这个模块此刻指着哪一个项目：`configure()` 一次定下，CLI 在 `main()` 里按 cwd 解析一次。
#: 导入这份文件时不绑项目 —— 模块指向哪，由调用方说一句。
LEDGER_DIR = None
DATA_DIR = None
NODES_DIR = None
IDEAS_JSON = None


def configure(project_root) -> None:
    """把记录目录钉在一个项目上：`<root>/.pf/`，它下面的 `nodes/` 与 `ideas.json`。

    这是路径进这个模块的那道门：CLI 在 `main()` 里用 cwd 调一次，进程内的调用方自己点名。
    """
    global LEDGER_DIR, DATA_DIR, NODES_DIR, IDEAS_JSON
    LEDGER_DIR = _ledger().ledger_dir_for(project_root)
    DATA_DIR = LEDGER_DIR
    NODES_DIR = _ledger().nodes_dir_of(DATA_DIR)
    IDEAS_JSON = DATA_DIR / 'ideas.json'


def _project_root() -> Path:
    """记录目录的上一层：`<root>/.pf` 的那个 `<root>`，便条的落点按它算。"""
    return LEDGER_DIR.parent


def _require_configured() -> None:
    """还没点过名就说清楚：读哪个项目的记录，得先由调用方说一句。"""
    if IDEAS_JSON is None:
        print('Error: 这个模块还没指向任何项目；先调 configure(项目根)。', file=sys.stderr)
        sys.exit(1)


#: 节点 spec 的落点：`conduct_pipeline_impl/nodes.py` 一处算它，这个工具照着取。
CONDUCT_IMPL = HERE.parent / 'conduct' / 'conduct_pipeline_impl'


def _spec_path(uid: str) -> str:
    """`<root>/.pf/nodes/<uid>/spec.md`，落点由 `nodes.node_spec_path` 给出。

    这条路径的算法只有一处：`add` 报的、`open` 印的、管线往那儿写 spec 的，都是这同一个值。
    那个包按路径取，名字专用（`pf_dag_conduct_impl`），同一个进程里谁另载一份都互不打扰。
    """
    pkg_dir = CONDUCT_IMPL
    pkg = _loader().load_by_path('pf_dag_conduct_impl', pkg_dir / '__init__.py',
                                 package_dir=pkg_dir, register=True, reuse=True)
    return pkg.nodes.node_spec_path(_project_root(), uid)


def _session_note():
    """`tools/shared/session-note.py` -- one module for every caller in this process.

    The conduct side writes every run's id to the same file; this side writes the discussion a node
    was born in, and reads both back for `info`.
    """
    return _loader().load_shared('session-note')


# ── the session note: best effort, both ways ──────────────────────────────────
# 便条是管道物：写不成、删不成，都只警告一行，图上那一笔已经落下的改动不因此回退。节点是正事，
# 便条照着手边的会话重新补得回来。

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

#: 节点状态只有三个词，写出去的就是这三个：`todo` 在局中未交付、`shelved` 出局等人、
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
    """`ideas.json` 落盘时的那串字节：格式由这个工具给出，别处照它取。

    写下来的是 `tools/shared/jsonio.py`（缩进 2、中文原样、没有尾换行），`sort_keys=True` 是本文件
    自己的习惯，让重写过的图每次读起来一样。合并驱动拿这串字节与盘上那份比对，判据因此只有一处：
    两边各写一遍格式的话，差一个字节就是每一次合并都看起来像有改动。
    """
    return _loader().load_shared('jsonio').json_bytes(data, sort_keys=True)


def write_ideas_bytes(path, payload: bytes) -> None:
    """把这串字节落到这个路径：`ideas_bytes` 的判据与这里的落盘出自同一棵树。

    合并驱动经这两道门问同一个写方（本树的 `tools/shared/jsonio.py`），所以它不会拿一棵树的格式
    判完、再用另一棵树的写法落盘。谁持锁、写完之后怎么收场是调用方的事，这里只有这次落盘。
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
    """没有 init 过的 `.pf/` 什么都不许写：锁的 `mkdir` 是唯一会把它造出来的东西。

    读的是 `LEDGER_DIR`，也就是 `configure()` 点过的那个记录目录；判据与那句话在
    `tools/shared/ledger.py`。
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
    """`uid`（全 id，或它的前缀）→ 记录里那个全 id；对不上就停在这里。

    前缀只在唯一对得上时算数：多义的前缀一次拒掉，请调用方把 id 写全 —— 让工具替你猜一格，
    猜错了就是改坏另一格记录。每条命令都拿这个返回值当 `data['nodes']` 的键，同一个前缀于是
    在所有动词上走到同一个裁决。
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
#  Graph
# ═══════════════════════════════════════════════════════════════════════════════

def _graph():
    """`tools/dag/graph.py`: this tool's graph algorithms, loaded by path like every neighbour.

    纯函数、不碰 IO，所以那一份能整份 import 起来单独测（`tests/test_graph.py`）。
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
        # 便条跟着节点走：节点从图上没了，它的那场对话也就不再是"这一格的"对话。
        _forget_session(uid)

        print(f'Removed {uid}')


def cmd_update(args):
    with _ledger_lock():
        data = _read()
        node = data['nodes'][_resolve_uuid(args.uuid, data)]
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
        _try_add_dep(data, uid, dep)
        data['nodes'][uid]['updated_at'] = _now()
        _write(data)


def _dependency_to_remove(dep: str, deps: list, data: dict) -> str | None:
    """要摘的那条边的全 id；`None` = 这条边本来就不在，没什么可摘的。

    裁决与节点那一侧同一个规矩（唯一对得上就用它，多义列出候选并停手，对不上就说对不上），
    只是先在这条边的清单里找：图上已经被人删掉的节点，它留下的那条边还写在记录里，而那正是
    合并驱动的修复提示叫人来摘的东西。三条路都不通的就报错 —— 静默不摘边是这条命令从前最坏
    的样子：调用方以为边没了，记录里还留着。
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
    # 清单里没有这个 id：它认得出来是图上哪一格（那一格上就没有这条边）时才算没东西可摘；
    # 认不出来 —— 打错了，或既不在图上也不在这条边的清单里 —— 由 `_resolve_uuid` 出声停手。
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
            # `ready` 就是 _node_ready 那一条：自己是 todo、依赖都 done。这个字段和 `--ready`
            # 过滤器说的必须是同一件事，否则过滤器一句话就被绕过。
            'ready': graph._node_ready(node, data['nodes']),
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
        # 和 list 说的必须是同一件事（见 _node_ready）：自己是 todo、依赖都 done。
        'ready': graph._node_ready(node, all_nodes),
        # shelved 的节点不排优先级（它不在可跑的集合里），所以两项分数都归零。
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
    note = _session_note().read(_project_root(), uid)
    if note['discussion'] or note['run']:
        result['sessions'] = {role: note[role] for role in ('discussion', 'run')}
    if node.get('completed_at'):
        result['completed_at'] = node['completed_at']

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
            # 和 `list`/`info` 说的同一件事（见 _node_ready）。
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

    # help
    subs.add_parser('help', help='Show the full usage reference')

    return parser


def main():
    # 中文节点标题走 JSON 输出，而这份脚本的 stdout 编码跟的是控制台代码页（这台机器默认 GBK）。
    # 调用方（conduct_pipeline.py）按 UTF-8 解码，GBK 字节流会让它的读线程直接抛
    # UnicodeDecodeError、stdout 变成 None —— 管线的第一个轮边界就崩在那里。
    # 每个入口自己做同样的 reconfigure（stderr 同理：报错也是中文）。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    # 项目在这里认一次：记录目录由 `configure()` 钉住，这一趟命令都按它读写。
    configure(_resolve_project_root(Path.cwd()))
    parser = _build_parser()
    args = parser.parse_args()
    if args.command == 'help':
        # `help` 与 `--help` 印同一份正文：动词、旗标和取值就是这个 parser 里的那些。
        parser.print_help()
        return
    args.func(args)


if __name__ == '__main__':
    main()
