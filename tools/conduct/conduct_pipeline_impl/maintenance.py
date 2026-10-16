"""The maintenance channel: a crossed threshold inserts a node into the graph.

Maintenance is a PROJECT-level debt -- the test suite's wall clock, the issue pool's backlog. It
belongs to no line and has no edges, so the graph's own pick pushes it to the bottom forever. This
module is the one place that gives it an entrance.

**The judgement happens at the close.** `_close_own_node` asks this module, once, at the `--after`
settle: read each metric's number, compare it with the lines this line carries in its own policies,
and, if a line is crossed, hand back the action that puts a maintenance node between the node just
closed and the node the close picked. Where a number comes from is the metric's own `source`: the
test suite's wall clock travels in the worker's ticket (`reading.test_seconds`), while the issue
pool's weighted debt is read from the tool that owns the pool (`pf issue weight`).

Every number here is OPTIONAL. A project that configures none of them gets `None` from
`crossed()` and insertion never runs: the close's return value is byte-for-byte what it was before
this channel existed. That is the regression this feature is built around.

**The graph edit is commands, and never a write from here.** `insertion()` builds them in the DAG
tool's own vocabulary (`undepend` / `add` / `depend` / `cycle hold`) and `writer.apply` runs
them: `plan()` stays a pure reader, and running the commands in one place is what keeps it that way. The
new node's uuid does not exist until `dag-manage add` has run, so the commands are written against
the `NEW_NODE` placeholder and the uuid is filled in afterwards -- `writer._capture_new_node` runs
that one command through the seam's `capture`, the same read-the-child's-stdout move
`nodes._rank_ready_nodes` already makes for the ranking.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import seam
from .nodes import node_spec_path
from .store import POLICY_NUMBERS, cycle_policy, load_dag

#: The action this module's insertion is handed over as. Registered in `conduct_pipeline.ACTIONS` and
#: handled by `writer.apply` / `writer.record` exactly like `dispatch` -- the difference is only
#: WHICH node is pinned (the one just inserted, not the one the close picked first).
MAINTENANCE_ACTION = 'maintenance_due'


def _reading_key(metric: dict) -> str:
    return metric['reading']


def _line_key(metric: dict) -> str:
    return metric['line']


#: The two metrics, and where each of their numbers lives.
#:
#: `reading` is the key the number appears under inside the ticket's `outputs` (`signal.reading_of`),
#: `line` is the numeric policy key the line itself carries (`cycle policy`, the same slot `when_shelved`
#: lives in). Both are optional, and neither is ever parsed out of anybody's prose.
#:
#: `source` names who produces the number: `ticket` for the wall clock only the session that ran the
#: verification can measure, `pool` for the pool's weighted debt, which the issue tool computes from
#: its own ledger (`pf issue weight`). A ticket never carries a `pool` number, and one that
#: does is ignored.
#:
#: `where` is the line the generated spec shows a reader: it says how this number was obtained, so a
#: maintenance node knows whether to go and measure or to go and look.
#:
#: `subject` / `criterion` / `remedy` are the mechanical completion rule the generated spec carries:
#: what is being pulled back under the line, what the next close must read, and what to run to see it.
#: `method` is the one step the tool can name without guessing at the project: which skill card owns
#: this kind of debt, so a node handed a number does not have to invent a method for it. The runbook
#: itself lives in that card; the spec only points at it (and the card declares which level runs it).
#: The list order is the severity order used to pick WHICH metric a node is inserted for when both
#: cross (the test suite's line first -- it is the one that blocks every other reading).
METRICS = (
    {
        'reading': 'test_seconds',
        'source': 'ticket',
        'unit': 's',
        'line': 'test_seconds_limit',
        'subject': '项目测试套件的全量墙钟（秒）',
        'where': '条子 `outputs.reading.test_seconds`（跑过全量验证的那个会话实测）',
        'criterion': '下一次收尾条子里 `reading.test_seconds` 必须**低于**',
        'remedy': '把项目文档认定的那套全量验证命令**实测**跑一遍（不许估算），把实测秒数报进条子',
        'method': 'test-audit',
    },
    {
        'reading': 'issue_pool_weight',
        'source': 'pool',
        'unit': '',
        'line': 'issue_pool_weight_limit',
        'subject': 'issue 池的加权和',
        'where': '`pf issue weight`（issue 工具自己算，不进条子）',
        'criterion': '下一次收尾时工具算出的池子加权和必须**低于**',
        'remedy': '跑 `pf issue weight -v` 看这个数是怎么加出来的，'
                  '确认它回到线下',
        'method': 'pf-issue-repair',
    },
)

#: The ticket-sourced readings, in METRICS order: the ones a worker is asked for. `asked_readings`
#: narrows this to the lines a given cycle actually carries.
READING_KEYS = tuple(metric['reading'] for metric in METRICS if metric['source'] == 'ticket')

#: `dag-manage` prints its own uuid; nothing on this side can know it before the child has run.
NEW_NODE = '%DUID%'
#: The slot marker `insertion` leaves where `add` belongs in the command list.
ADD_MARKER = '%ADD%'
_UUID_PRINTED = re.compile(r'Node created:\s*([0-9a-fA-F-]{8,})')


# ── the reading: what the worker reported ────────────────────────────────────

def _number(value) -> float | None:
    """One reading, or `None`.

    A number is a number and nothing else: a bool is not one (it is the JSON spelling of a decision,
    not a measurement), and a string is not parsed -- guessing a number out of a worker's prose is
    how a threshold ends up firing on the wrong evidence.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def reading_of(sig: dict | None) -> dict:
    """The ticket-sourced readings on one ticket, as numbers. An absent/garbled reading is simply not
    there.

    只认 `READING_KEYS`（`source: ticket` 的那几个）。老条子上还躺着 `issue_pool_weight` 的，
    这里当作没看见 —— 那个数由 issue 工具从池子里算，条子上的那份没人复核得了。
    """
    outputs = (sig or {}).get('outputs') or {}
    reading = outputs.get('reading') if isinstance(outputs, dict) else None
    if not isinstance(reading, dict):
        return {}
    out = {}
    for key in READING_KEYS:
        value = _number(reading.get(key))
        if value is not None:
            out[key] = value
    return out


# ── the lines: what this line carries ───────────────────────────────────────

def thresholds_for(cycle: dict | None) -> dict:
    """The lines in force for this line: its own policies.

    They are the line's own rules, in the same slot `when_shelved` lives in (`state-manage.py
    cycle policy`), which is why nobody has to write a sentence to configure one -- and why a line
    that has nothing to do with code simply does not carry them.

    「配了数值型策略」= `conduct_state.cycles.POLICY_NUMBERS` 那张表里、这条线的记录上真写着的键
    （`parallel` 那样的整数键不在其中）；一个都没配时这份读数是空的，`crossed` 的空门就靠它 ——
    非空即「这条线至少配了一个度量」。表本身只有一处，取的就是写侧那一份。
    """
    policy = cycle_policy(cycle or {})
    return {key: policy[key] for key in POLICY_NUMBERS if key in policy}


def asked_readings(cycle: dict | None) -> list:
    """这条线要哪几个读数，按 `METRICS` 的顺序。

    名单由两件事收窄：只点 `source: ticket` 的那几个（只有跑过验证的那个会话测得到），
    并且只点这条线真的配了线的那几个 —— 没人读的格子问了就是白测。`handoff.py --form`
    照这份名单印条子骨架。
    """
    thresholds = thresholds_for(cycle)
    return [metric['reading'] for metric in METRICS
            if metric['source'] == 'ticket' and metric['line'] in thresholds]


def pool_weight(project) -> float | None:
    """池子有多重：问 issue 工具要那个数（`pf issue weight`）。

    读不到就交回 `None`，这一趟不判这个度量 —— 缺一个数读作「这一趟不知道」；读成 0 会让
    一条真实的债看起来已经还了。
    """
    try:
        out = seam.capture(seam.issue('weight'), project)
    except (OSError, RuntimeError):
        return None
    lines = [line.strip() for line in (out or '').splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return float(lines[-1])
    except ValueError:
        return None


# ── the judgement ────────────────────────────────────────────────────────────

def _wired(metric: dict, reading: dict, thresholds: dict):
    """(value, line) for one metric, or `None` when either half is missing.

    The comparison is `>=`: the line is the last acceptable number, so reading exactly the line
    means the debt is at its limit and the channel opens. One line per measurement: the action on
    either side of it is the same one, so the line only has to say where the debt stands.
    """
    value = reading.get(_reading_key(metric))
    if value is None:
        return None
    line = thresholds.get(_line_key(metric))
    if line is not None and value >= line:
        return value, line
    return None


def crossed(project, cycle: dict | None, sig: dict | None, dag: dict | None = None):
    """The metric to insert a maintenance node for, or `None` (the common case).

    `None` covers every way this channel stays shut: no number for the metric, no line on this
    cycle, a number under its line, a cycle that is not about code -- the two metrics are
    code-path debt, so a `writing` / `art` line reads the same numbers and does nothing about them --
    and a metric that ALREADY has a maintenance node open on the graph:
    one metric, one live maintenance node at a time (`maintenance_nodes_of_metric`).

    池子那一格只有在「这条线真的配了那条线」时才去问 issue 工具，收尾这条路上不白开进程。
    """
    if not _is_coding(cycle):
        return None
    thresholds = thresholds_for(cycle)
    if not thresholds:
        return None
    values = reading_of(sig)
    if any(metric['source'] == 'pool' and metric['line'] in thresholds for metric in METRICS):
        weight = pool_weight(project)
        if weight is not None:
            values['issue_pool_weight'] = weight
    if not values:
        return None
    for metric in METRICS:
        got = _wired(metric, values, thresholds)
        if got is not None:
            if maintenance_nodes_of_metric(project, metric['reading'], dag):
                continue
            value, line = got
            hit = {**metric, 'value': value, 'at': line}
            hit['title'] = maintenance_title(hit)
            # A CALLABLE, not the text: the node's uuid decides where its spec lands, and the uuid
            # does not exist until `dag-manage add` has run (`writer._capture_new_node`).
            hit['spec_of'] = lambda node_id, _hit=hit: maintenance_spec(
                _hit, node_spec_path(project, node_id))
            return hit
    return None


def maintenance_tag(reading_key: str) -> str:
    """这个度量的维护节点身上的 tag：`add` 写上它，去重读它 —— 度量认得出，不靠标题的字。"""
    return f'maint:{reading_key}'


def maintenance_nodes_of_metric(project, reading_key: str, dag: dict | None = None) -> list:
    """图上这个度量还没了结的维护节点（`todo` 状态），按 id 排。

    「没了结」= 节点还在局中（`todo`）：`completed` 说明这一趟债已经结了、`shelved` 说明它出局等人
    ——两种都不该拦住下一次插入，否则人一直 revive 不动，这条通道就永远哑了。
    判定发生在收尾命令落盘之前，所以**正在被收掉的那个维护节点在这里仍是 `todo`、也照样拦住自己
    的重复插入**——一个度量同时只有一个维护节点在跑，这一条不受收尾进度影响。

    `dag` 是收尾入口读下的那一份图：这一趟判定读的是它，收尾不再为这一个问题重读一次盘。
    """
    tag = maintenance_tag(reading_key)
    nodes = (dag if dag is not None else load_dag(project)).get('nodes') or {}
    out = []
    for uid, node in (nodes or {}).items():
        if not isinstance(node, dict):
            continue
        if node.get('status') != 'todo':
            continue
        if tag in [t for t in (node.get('tags') or []) if isinstance(t, str)]:
            out.append(uid)
    return sorted(out)


def _is_coding(cycle: dict | None) -> bool:
    """只有 tag 含 `coding` 的 cycle 会因这两个度量插入——两个度量都是代码这一支的债。"""
    tags = [t for t in ((cycle or {}).get('tags') or []) if isinstance(t, str)]
    return 'coding' in tags


# ── the node's own text ──────────────────────────────────────────────────────

def maintenance_title(hit: dict) -> str:
    """The node's title: which measurement and which line, in one line a human reads in the DAG."""
    shown = f'{hit["value"]:g}' if hit['value'] != int(hit['value']) else f'{int(hit["value"])}'
    line = f'{hit["at"]:g}' if hit['at'] != int(hit['at']) else f'{int(hit["at"])}'
    return f'维护：{hit["subject"]} = {shown}{hit["unit"]}，达到线 {line}{hit["unit"]}'


def _closing_reading(hit: dict) -> str:
    """这条债下一次靠什么判回来：两种来源写法不同，因为它们被读的方式不同。

    池子那个数由 issue 工具自己算（`pf issue weight`），条子上写它没人读；
    墙钟只有跑过验证的会话测得出来，`handoff.py --form <cycle>` 会把那一格点出来。
    """
    if hit['source'] == 'pool':
        return ('- 这个度量由 issue 工具自己算（`pf issue weight`），'
                '条子里不用填它：\n'
                f'  下一次收尾工具算出的数低于 {hit["at"]:g}{hit["unit"]} 就算回到线下。\n')
    return ('- 在条子的 `-o` 里带上 `"reading": {"' + hit['reading'] + '": <实测数>}`'
            '（表单会把这一格点出来），好让下一次收尾判得出它回没回到线下。\n')


def maintenance_spec(hit: dict, spec_path: str) -> str:
    """The generated spec: why this node exists, what its scope is, which skill owns it, and how it is
    judged done.

    Written by the tool, not by the session that will run it -- the whole point of the channel is
    that the judgement was mechanical, so the node cannot be handed the judgement as well. What it
    does carry is the one thing a worker cannot derive: which measurement was read, what it read,
    and where the line is.

    No function name, file path or line number from the project appears here: which files move is
    the node's own business, and a template that guessed would be a template that lied.
    """
    return (
        '# 维护节点（工具生成）\n'
        '本节点的 spec 由 conduct 的维护通道生成，逐字照抄；它写的是**为什么被插进来**、'
        '**这一次的范围**、**怎么干**，以及**一开始就定死的完成判定**。\n'
        '\n'
        '## 为什么被插进来\n'
        '\n'
        f'- 度量：{hit["subject"]}\n'
        f'- 这个数从哪来：{hit["where"]}\n'
        f'- 收尾那一次读到：**{hit["value"]:g}{hit["unit"]}**\n'
        f'- 写在**这条线的策略**里的线：**{hit["at"]:g}{hit["unit"]}**（`policies.{hit["line"]}`）\n'
        '- 这条通道是项目级的：维护债不归任何一条线、在图上也没有边，排序永远把它压到底，'
        '所以它由收尾那一拍按这条线自己配的线插进来。\n'
        '\n'
        '## 范围\n'
        '\n'
        '- **这一次只做这件事**：把这一个度量拉回线下。不顺手加功能、不顺手重构、'
        '不做和这个度量无关的清理。\n'
        '- 别的度量越线了也好、图里别的活更急也好，都不是本节点的范围 —— 谁越线谁自己进来一趟。\n'
        '- 改动大小以判定为准：判定过不去就继续，判定过去了就停手。\n'
        '\n'
        '## 怎么干\n'
        '\n'
        f'- `方法：{hit["method"]}`（跑法见 `~/.agents/skills/{hit["method"]}/SKILL.md`；'
        '那份卡自己声明谁跑它，spec 这里只指路）\n'
        '\n'
        '## 完成判定（机械可判）\n'
        '\n'
        f'- {hit["criterion"]} {hit["at"]:g}{hit["unit"]}。\n'
        f'- 怎么看到这个数：{hit["remedy"]}。\n'
        '- 判定过不去就**不要**报 `completed`；报 `failed` 并把读到的数写在 `-s` 里，'
        '让这条债留在明面上等人。\n'
        '\n'
        '## 收尾\n'
        '\n'
        '照 pf-conduct 的「收尾」那节走：先要一张现印的条子表单，再写条子 → 收尾 → 提交推送 → 派发。\n'
        + _closing_reading(hit) +
        f'\n（本 spec 落在：{spec_path}）\n'
    )


def write_spec(project, node_id: str, spec: str) -> Path:
    """Put the generated spec in the new node's ledger directory.

    `dag-manage add` carries the text (so the graph row's description says what the node is for) but
    creates only the empty directory: what a node's session reads is `spec.md`, and by the time the
    session boots the graph edit is long over. Writing it here -- from the same `add` step, with the
    uuid the child just minted -- is what keeps `plan()` a pure reader: the planner builds the text,
    `apply` is the only thing that touches a file.

    `node_id` comes straight from the child process's own output, so it is checked before it reaches
    a path: a uuid from anywhere is still a uuid.
    """
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', node_id or ''):
        raise RuntimeError(f'refusing to write a spec under a node id that is not a uuid: '
                           f'{node_id!r}')
    path = Path(node_spec_path(project, node_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(spec, encoding='utf-8')
    return path


# ── the graph edit, as commands ──────────────────────────────────────────────

def insertion(project, closed: str, target: str, cycle_id: str, hit: dict,
              spec_of, close_commands: list | None = None) -> dict:
    """The whole graph edit as one package: the command list, and the `add` at the head of it.

    The list, in order, and it is the order the pipeline and the graph tool both demand rather than
    the order the picture reads:

      1. `cycle close` / `dag-manage status` -- close the node and mark it in the DAG, BEFORE the new
         node is created: the dedup judgement reads the graph and an unfinished node must never be
         what stands between a threshold and its own maintenance node.
      2. `add` -- the maintenance node, with its title and its generated spec. Its stdout is the only
         place its uuid appears, so `writer.apply` runs it through `seam.capture` -- which is why the
         command is also handed back separately, as `add_command`: the caller runs it after the close
         commands, reads the uuid, and drops it from the list it runs next.
      3. `undepend <target> <closed>` -- drop the old direct edge. It frequently does not exist
         (being ready only asks that every dependency be completed or absent), and dropping an edge
         that is not there is a no-op. It has to come before the new edges: while it is still in
         place, `closed -> maintenance` plus `maintenance -> target` closes a cycle through the old
         edge, and `dag-manage` refuses a graph with a cycle in it.
      4. `depend` -- the two new edges: the maintenance node is the closed node's direct dependent,
         and the target waits for it. (`add --depends-on` is deliberately not used: those edges go in
         after the old one is gone, for the same reason.)
      5. `cycle hold` -- the cycle is pinned on the maintenance node. This is one write into
         the cycle's hand, and it has to name the maintenance node on the first try; the line's
         the other nodes it holds are untouched.
    """
    title = maintenance_title(hit)
    spec = spec_of(NEW_NODE)
    add = seam.dag('add', title, '--tags', maintenance_tag(hit['reading']), '--desc',
                   # The spec travels as a positional argument rather than as a shell string:
                   # `seam.dag` returns an argv LIST, so subprocess quotes it. Encoding it by hand
                   # would put a second quoting convention between the tool and the child, and the
                   # child is Python.
                   json.dumps(spec, ensure_ascii=False, separators=(',', ':')))
    return {
        'title': title, 'spec': spec, 'add_command': add,
        'commands': list(close_commands or []) + [
            [ADD_MARKER],                # where `add` belongs; `writer.apply` runs the real argv here
            seam.dag('undepend', target, closed),
            seam.dag('depend', NEW_NODE, '--on', closed),
            seam.dag('depend', target, '--on', NEW_NODE),
            seam.cmd('cycle', 'hold', cycle_id, NEW_NODE),
        ],
    }


def is_add_slot(argv) -> bool:
    """Is this the marker slot where `add` belongs? (`writer.apply` runs the real argv in its place.)"""
    return len(argv) == 1 and str(argv[0]) == ADD_MARKER


def _new_node_id(stdout: str) -> str:
    """The uuid `dag-manage add` printed, or a loud failure (the close is half-applied by then)."""
    found = _UUID_PRINTED.search(stdout or '')
    if not found:
        raise RuntimeError(f'dag-manage add did not report a node id; its output was: {stdout!r}')
    return found.group(1)


def substitute(argv, node_id: str) -> list:
    """The placeholder in an argv, replaced by the uuid the child actually minted."""
    return [str(part).replace(NEW_NODE, node_id) for part in argv]


def read_new_node_id(project, argv) -> str:
    """Run `add` and read the uuid it printed on its stdout.

    Captured rather than run through the seam's `run`: `run` discards stdout, and the uuid only
    exists in that stdout. This is the same move `nodes._rank_ready_nodes` makes to read
    `dag-manage list` -- the tool prints the fact, so the fact is read off the tool.
    """
    return _new_node_id(seam.capture(argv, project))
