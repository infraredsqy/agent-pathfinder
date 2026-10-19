"""One root -- `<project>/.pf/` -- and the four kinds of thing that live under it.

The command's durable account is the LEDGER, `<project>/.pf/`: the cycle book (`state.json`), the
signals (`signals/`), the standing instructions (`instructions.json`), the DAG (`ideas.json`) and
each node's own text (`nodes/<id>/`). A project either commits it or keeps it local, and which of
those it is never changes what this module computes -- only whether git tracks `.pf/`.

Everything else under `.pf/` is plumbing, and the ledger's own `.gitignore` ignores it:

    meta/                 the dispatch plumbing -- launch prompts, the FORK record, the
                          broken-chain marker, and the cross-process ledger lock
    worktrees/<短id>       one linked worktree per parallel node
    nodes/<id>/temp/      what a node generates: probes, raw output, snapshots

None of those is the account: a launch prompt is a message that has already been delivered, the
marker is a "this chain broke" flag, and `temp/` is throwaway by definition.

Holds the path bindings (`bind_paths` re-points them all at once), the door to the cross-process
ledger lock every mutating command holds across its read-modify-write, and the state/signal
accessors built on the shared primitives -- the JSON reader and atomic writer, and the lock itself,
both in `tools/shared/`.

`require_initialized` is the gate: a `.pf/` this tool created by itself -- never init'ed -- is the
one state that must not be reachable, because the lock's own `mkdir` would otherwise leave an
unignored ledger in the tree for the next `git add -A` to sweep up.

A cycle's record in `state.json` is the whole of it -- identity, current node, and the path of nodes
it has finished.
"""


import contextlib
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent   # the conduct tool directory

# 引导：装载器也按路径取，所以这一段只能每个文件自己带（见 `shared/loader.py`）。
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
    """`tools/shared/ledger.py`: `.pf/` 的落点与 init 闸门 —— 正本一处。"""
    return _loader().load_shared('ledger')

#: 这几个名字的正本在 `tools/shared/ledger.py`，本命名空间里照旧可用（`fork.py` / `initledger.py`
#: 用 `from .store import …` 取它们，包的 `__all__` 也照旧收它们）。取而不是抄：这里不留第二份
#: 值，也就没有第二处要和正本对齐的拼法。
_FROM_LEDGER = {'LEDGER_NAME': 'LEDGER_NAME', 'INIT_MARKERS': 'INIT_MARKERS',
                '_resolve_project_root': 'resolve_project_root'}


def __getattr__(name: str):
    """本模块里那几个名字：从 `shared/ledger.py` 那份正本取（见 `_FROM_LEDGER`）。"""
    if name in _FROM_LEDGER:
        return getattr(_ledger(), _FROM_LEDGER[name])
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')

#: 一个根：`.pf/`。`meta/`、`worktrees/`、`nodes/<id>/temp/` 都是它底下被忽略的子目录，
#: 各自由下面的 `*_dir_for()` 算出来，绝不再从别的根拼一条 `.pf/` 底下的路径出来。
#: cwd 在导入那一刻定死（worktree 里则回主树，见 `shared/ledger.py` 的 `resolve_project_root`）。带
#: `--project` 的命令绕开它，见 `ledger_dir_for()`。
PROJECT_ROOT = _ledger().resolve_project_root(Path.cwd())
LEDGER_DIR = _ledger().ledger_dir_for(PROJECT_ROOT)


def _explicit_project(project):
    """调用方点名的那一个项目根：`~` 展开、取绝对 —— `--project` 是人手敲的那串字。

    `None` 不走这里：`ledger_dir_for(None)` 要的是本拷贝导入时绑定的那个根（见下面）。
    """
    return Path(project).expanduser().resolve()


def ledger_dir_for(project: str | None = None) -> Path:
    """Where the ledger (`.pf/`) lives.

    `project is None` keeps every pre-existing caller on the import-time cwd resolution. An explicit
    `--project` matters because `LEDGER_DIR` freezes `Path.cwd()` at import: a caller whose cwd is
    not the project root (a subagent that cd'd away, a launcher that started somewhere else) would
    otherwise read a *different* project's `.pf/` -- and an empty one reads as "no instructions",
    silently, with exit 0. A constraint that looks absent is worse than a loud error.
    """
    if project is None:
        return LEDGER_DIR
    return _ledger().ledger_dir_for(_explicit_project(project))


def meta_dir_for(project: str | None = None) -> Path:
    """Where the dispatch plumbing lives (`<project>/.pf/meta/`): launch prompts, the FORK record,
    the broken-chain marker, the ledger lock. The same rule as `ledger_dir_for`, one level down."""
    return _ledger().meta_dir_of(ledger_dir_for(project))


def nodes_dir_for(project: str | None = None) -> Path:
    """Where each node's own directory lives (`<project>/.pf/nodes/`): its spec, its report group
    and the `temp/` it generates. The same rule as `ledger_dir_for`, one level down."""
    return _ledger().nodes_dir_of(ledger_dir_for(project))


def require_initialized(project: str | None = None) -> None:
    """这个 `.pf/` 必须已经 init 过，否则什么都不许写。

    别的命令都不创建 `.pf/`，唯一会顺手创建它的是锁的 `mkdir` —— 那正是要挡的：一个没 init 的
    `.pf/` 既没被 track 也没被 ignore，下一次 `git add -A` 就把整个目录扫进团队库。判据与那句话
    在 `tools/shared/ledger.py`（纯文件系统，不问 git）；`project` 点过名时那句示例命令行带上它。
    """
    ledger = ledger_dir_for(project)
    if _ledger().is_initialized(ledger):
        return
    print(f'Error: {_ledger().init_message(ledger, project=project)}', file=sys.stderr)
    sys.exit(1)


# ── the cross-process ledger lock ─────────────────────────────────────────────
# state.json / ideas.json / instructions.json / issues.json are whole-file read-modify-write:
# two processes walking that sequence at once means the later write silently erases the earlier
# one. With parallel=2 (two cycles settling at the same time) that window stops being theoretical,
# so every MUTATING command body holds this lock across its read → write. One path per project
# root -- `.pf/meta/ledger.lock` -- shared by conduct, dag-manage, the issue tool and a session
# note's own writer, so their writes exclude each other too.
#
# This is also the gate: every mutating command passes through here, and the lock's `mkdir` would
# otherwise be the one thing that conjures a `.pf/` nobody init'ed.


@contextlib.contextmanager
def _ledger_lock(project: str | None = None, timeout: float | None = None):
    """Hold the project's ledger lock for the duration of one read-modify-write.

    The lock itself is `tools/shared/ledgerlock.py` -- one implementation for every writer of a
    record here. A caller that cannot get it within `timeout` is told loudly and exits 1: wedging
    silently is the one failure this must not have.
    """
    lock = _loader().load_shared('ledgerlock')
    kwargs = {} if timeout is None else {'timeout': timeout}
    try:
        with lock.ledger_lock(ledger_dir_for(project), gate=lambda: require_initialized(project),
                              **kwargs):
            yield
    except TimeoutError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


#: How a path step may end. A node's own work is not tracked here, only the outcome it reported.
VALID_NODE_RESULTS = {'completed', 'failed', 'stopped'}
#: 一条 cycle 只有两个状态：`active` 这条线该往前推、`inactive` 不要推。停下来的原因不留在状态里
#: —— 停手那一刻的 reason 才是那句话，而一条线该不该重新开工是人说了算的。
CYCLE_VALID_STATUSES = {'active', 'inactive'}
#: 一条线同时能握几个节点 —— `parallel` 策略的缺省值：不开并行就是 1。
PARALLEL_DEFAULT = 1
#: state.json / 条子 / 常备指令 / FORK 记录用的那个时刻形状：秒级。正本是 `tools/shared/clock.py`
#: 的 `ISO_FMT_SECONDS`；`ISO_FMT` 是本文件与读它的用例一直叫的那个名字，指的还是这一个形状。
ISO_FMT = _loader().load_shared('clock').ISO_FMT_SECONDS


def _now() -> str:
    """这个记录的时刻：`tools/shared/clock.py` 的秒级形状（微秒那一种是图与池子的）。"""
    return _loader().load_shared('clock').now_seconds()


def _atomic_write(path: Path, data: dict) -> None:
    """One record written whole, or the command ends.

    The write itself (temp file in the target's directory, fsync, a replace that tolerates a live
    reader) is `tools/shared/jsonio.py`; this layer is the CLI, so a failure is a message and exit 1.
    """
    try:
        _loader().load_shared('jsonio').atomic_write_json(path, data)
    except Exception as e:
        print(f'Error: write failed: {e}', file=sys.stderr)
        sys.exit(1)


def record_bytes(data: dict) -> bytes:
    """这四份记录落盘时的那串字节：格式由写方给出，合并驱动照它比对。

    `state.json` / `instructions.json` / 条子都经 `_atomic_write` 落盘，格式在
    `tools/shared/jsonio.py`（缩进 2、中文原样、`sort_keys` 留缺省）。合并驱动拿这串字节与盘上
    那份比对：两串一模一样，那枚文件就原样留着，一个字节都不碰。判据只有写方给得出 —— 驱动若自己
    `json.dumps` 一遍，这边哪天改了格式，往后每一次合并都会看起来像有改动。
    """
    return _loader().load_shared('jsonio').json_bytes(data)


def write_record_bytes(path, payload: bytes) -> None:
    """把这串字节落到这个路径：判据与落盘出自同一棵树。

    `record_bytes` 给出「这个项目该写成什么样」，这里把它写下去，两件都用本树的
    `tools/shared/jsonio.py`。合并驱动经这两道门问同一个写方：草稿 / 演练副本用
    `DSH_CONDUCT_STATE_DIR` 指向真工具时，不会拿一棵树的判据去对另一棵树的落盘。谁持锁、写完之后
    怎么收场是调用方的事，这里只有这次落盘。
    """
    _loader().load_shared('jsonio').atomic_write_bytes(path, payload)


def _read_json(path: Path, label: str) -> dict | None:
    """One record read strict, or the command ends with the reader's own words.

    The reader and the wording of each failure are `tools/shared/jsonio.py`: a file that is not
    there reads as None, bytes that are not UTF-8 and text that is not JSON both raise
    `JsonReadError`. **CLI 入口负责退，包内只抛**：这里就是那道门 —— 打印读者自己的那句话并
    exit 1；进程内直接调 `read_json_strict` 的调用方拿到的是异常，由它自己决定怎么办。
    """
    jsonio = _loader().load_shared('jsonio')
    try:
        return jsonio.read_json_strict(path, label)
    except jsonio.JsonReadError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


# ── 四类记录：一道门 ──────────────────────────────────────────────────────────
# `.pf/` 底下四样东西是这个工具的全部记录 —— cycle 簿（state.json）、条子（signals/）、常备
# 指令清单（instructions.json）、图（ideas.json）。这一节是它们**唯一**的落点算法：每一道门都收
# 一个 `project`，读别人项目的记录用不着自己拼路径，也就不会拼出第二种出错口径。同一份记录有
# 两处读法时，一处读坏了大喊一声，另一处静默当空集 —— 而那句空集落在 `clean-temp --yes` 上，
# 就是照删不误。

def _record_path(project: str | None, *parts: str) -> Path:
    """`<project>/.pf/<parts...>`：记录的落点只在这里与 ledger 根相接。

    `project` 为 None 时用本拷贝导入时绑定（`bind_paths` 挪过）的那个根。
    """
    return ledger_dir_for(project).joinpath(*parts)


# ── State.json ───────────────────────────────────────────────────────────────

def _read_state(project: str | None = None) -> dict:
    """这个项目的 cycle 簿，或一本空簿（`state.json` 不在是正常态）。"""
    data = _read_json(_record_path(project, 'state.json'), 'state.json')
    if data is None:
        return {'cycles': []}
    if 'cycles' not in data:
        data['cycles'] = []
    return data


def _read_dag(project: str | None = None) -> dict:
    """这个项目的图：`ideas.json` 的节点表，或空表。

    读的是盘上那个词，一个字段都不补（`pipeline/store.py` 的 `load_dag` 是给判据
    用的那份，它会把节点 mode 填上）。
    """
    data = _read_json(_record_path(project, 'ideas.json'), 'ideas.json') or {}
    return data.get('nodes') or {}


def _instructions_path(project: str | None = None) -> Path:
    """常备指令清单的落点：清单是 ledger 的一部分，所以是 `<project>/.pf/instructions.json`。"""
    return _record_path(project, 'instructions.json')


def _read_instructions(project: str | None = None) -> dict:
    """这份清单，盘上什么样读出来就什么样。

    读不解析也不落笔：条目的 scope 由 `entry_scope`（`conduct_pipeline`，那一条判定的唯一处）
    在真正要用到的地方读。
    """
    data = _read_json(_instructions_path(project), 'instructions.json')
    if not data:
        return {'instructions': []}
    data.setdefault('instructions', [])
    return data


def _write_instructions(data: dict, project: str | None = None) -> None:
    _atomic_write(_instructions_path(project), data)


def _holding_of(cycle: dict) -> list:
    """这条线此刻握着的那一组节点，规范形：`[{'id':..., 'since':...}, ...]`（可为空）。

    这是一条线的**认领标记**：一个节点只能出现在一条线的这份列表里。
    """
    holding = cycle.get('holding')
    if isinstance(holding, list):
        out = []
        for item in holding:
            if isinstance(item, dict) and item.get('id'):
                out.append({'id': item['id'], 'since': item.get('since')})
            elif isinstance(item, str) and item:
                out.append({'id': item, 'since': None})
        return out
    return []


def _holding_ids(cycle: dict) -> list:
    """握着的那组节点的 id，按认领先后排。"""
    return [item['id'] for item in _holding_of(cycle)]


def _set_holding(cycle: dict, items: list) -> None:
    """把规范形写回这条线：写 `holding`。"""
    cycle['holding'] = [{'id': item['id'], 'since': item.get('since')} for item in items]


def _write_state(data: dict) -> None:
    _atomic_write(_record_path(None, 'state.json'), data)


def _find_cycle(state: dict, cycle_id: str) -> dict:
    for c in state['cycles']:
        if c['id'] == cycle_id:
            return c
    print(f'Error: cycle "{cycle_id}" not found in state.json', file=sys.stderr)
    sys.exit(1)


# ── Signals ──────────────────────────────────────────────────────────────────

def _signal_path(cycle_id: str, node_id: str, project: str | None = None) -> Path:
    """条子的落点：`<project>/.pf/signals/<cycle>/<node>.json`（`project` 缺省用本拷贝绑定的根）。"""
    return _signal_path_in(_record_path(project, 'signals'), cycle_id, node_id)


def _signal_path_in(signals_dir, cycle_id: str, node_id: str) -> Path:
    """同一个落点，那一格的根由调用方直接给出 —— 显式路径的写方走这条（见 `_write_signal`）。

    落点的形状只有这一处：`<signals_dir>/<cycle>/<node>.json`。
    """
    return Path(signals_dir) / cycle_id / f'{node_id}.json'


def _read_signal(cycle_id: str, node_id: str, project: str | None = None):
    return _read_json(_signal_path(cycle_id, node_id, project),
                      f'signals/{cycle_id}/{node_id}.json')


def _write_signal(cycle_id: str, node_id: str, status: str, summary: str, outputs: dict,
                  signals_dir=None) -> Path:
    """One node's signal: the record's one writer, and the six keys a reader gets.

    The path is `_signal_path`'s and the moment is `_now`'s, so a signal written by a worker, by
    `handoff.py` or by `cycle retry` is the same record at the same place.

    `signals_dir` 是**这一笔写入**的落点（`<project>/.pf/signals`），用完即弃：给了它这一笔就落
    在那儿，本拷贝的绑定一个字都不动 —— 挪绑定是 `bind_paths` 那一件事，而一个进程里共用这份
    store 的调用方不止一个，写条子顺手改掉全进程的落点会把它写到上一个项目里去。不给就用本拷贝
    此刻的绑定（cwd 在本拷贝导入时定的那个根）。
    """
    sig_path = (_signal_path_in(signals_dir, cycle_id, node_id) if signals_dir is not None
                else _signal_path(cycle_id, node_id))
    sig = {
        'cycle': cycle_id, 'node': node_id,
        'status': status, 'summary': summary,
        'outputs': outputs, 'timestamp': _now(),
    }
    try:
        _loader().load_shared('jsonio').atomic_write_json(sig_path, sig)
    except Exception as e:
        print(f'Error: write signal failed: {e}', file=sys.stderr)
        sys.exit(1)
    return sig_path


# ── who is holding which node ────────────────────────────────────────────────

def _cycles_holding_node(cycles: list, node: str) -> list:
    """哪些 cycle 现在钉着这个节点？—— 一个节点一次只归一个 cycle。

    cycle 的记录里写着它此刻握着的那一组节点，所以「这个节点现在被谁拿着」是盘上读得出来的事实，
    不必猜一个「当前 cycle」。`--after` 的收尾人只知道自己跑的是哪个节点：节点是唯一的，拿着它的
    那条 cycle 也是唯一的，这就是够把 cycle 定下来 —— 收尾的这条命令不需要（也不该）先知道
    cycle 的名字。

    cycle 自己的状态不参与判定：一条不在服务中的线（inactive）照样握着它没跑完的那个节点，正是
    它得先能被收尾，人才能接着往下走。

    两条 cycle 真钉着同一个节点时（同一条活被两条线同时开跑）返回两条 —— 由调用方报歧义。
    """
    return [c for c in cycles if node in _holding_ids(c)]


def _closed_steps_of_node(cycles: list, node: str) -> str:
    """这个节点在别的 cycle 上已经跑完过的那一步，写成人读的一句。

    收尾命令重复跑一次就落在这里：活交过了，节点也放下了，剩下的只有告诉调用方没有可结算的东西。
    """
    for c in cycles:
        for step in reversed(c.get('path') or []):
            if step.get('node') == node and step.get('result') in ('completed', 'failed'):
                return f"{c.get('id')!r} 的路径上（{step['result']}）"
    return ''


def bind_paths(ledger_dir) -> None:
    """Point this copy of the tool at one project's `.pf/` (in-process callers only).

    The CLI's paths come from `Path.cwd()` at import, because it is started with cwd set to the
    project. A caller that loads this module in process instead has to say which project it means:
    everything else reads these bindings at call time, so this one call moves the whole module.

    `ledger_dir` is `<project>/.pf`; every record's path and every `*_dir_for()` come off that one
    root at call time, so a caller that names the ledger has named all of it.
    """
    global LEDGER_DIR
    LEDGER_DIR = Path(ledger_dir)
