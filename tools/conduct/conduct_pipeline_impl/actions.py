"""The action builders: state -> one action package.

`_ask`, `_dispatch_many`, the two `wait` answers, the close command list, and the stop package -- plus
`render_action`, the other end of the same contract: an action package as the caller reads it.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import maintenance, seam
from .nodes import mode_for, node_row, node_spec_path
from .prompts import build_prompt_text, launch_dir, launch_prompt_path
from .store import DEFAULT_MODE, load_dag


def _ask(question: str, **extra) -> dict:
    return {'action': 'ask_user', 'cycle': None, 'question': question, 'commands': [], **extra}


def _node_entry(project: Path, cycle: dict, row: dict, mode: str) -> dict:
    """One node's entry inside a dispatch-shaped action.

    The id, its title, its mode, where its prompt file lands and the text to write there -- one
    builder, so the multi-node dispatch and the maintenance insert cannot spell their entries
    differently. `row` is a graph row with its id in it (`nodes.node_row`); the maintenance node has
    no row yet, so its caller hands over the id and the title it was generated with.
    """
    node_id = row['id']
    return {'node_id': node_id, 'node_title': row.get('title', ''), 'mode': mode,
            'prompt': str(launch_prompt_path(project, cycle['id'], node_id)),
            'prompt_text': build_prompt_text(node_id)}


def _dispatch_many(project: Path, cycle: dict, node_ids: list) -> dict:
    """一条线手里的每个节点各一份 dispatch，装进同一个动作。

    一条多节点的线在派发上就是若干个「一格」并排：每个节点有自己的 prompt 文件、自己的会话、
    自己的收尾命令，`dispatch <cycle>` 把它们一起开出去。单节点的线拿到的还是同一份形状，
    只是 `nodes` 里恰好有一项。
    """
    dag = load_dag(project)
    nodes = [_node_entry(project, cycle, node_row(dag, node_id), mode_for(node_id, dag))
             for node_id in node_ids]
    head = nodes[0]
    return {
        'action': 'dispatch', 'cycle': cycle['id'],
        'node_id': head['node_id'], 'node_title': head['node_title'],
        'nodes': nodes,
        # A multi-node dispatch does not know which node's prompt is "the" one: blanking the
        # singular pair keeps a caller that reads `prompt_text` alone from writing the head's file
        # twice and missing the rest -- `nodes` is the list to walk.
        'mode': head['mode'],
        'prompt': head['prompt'] if len(nodes) == 1 else '',
        'prompt_text': head['prompt_text'] if len(nodes) == 1 else '',
        'steps': [f'write each node\'s prompt text to its own prompt.txt under '
                  f'{launch_dir(project, cycle["id"])}',
                  'DSH: spawn one session per node (`dispatch <cycle>` does all of them; '
                  '`next --spawn` is the one-shot spelling of the same call)'],
        'commands': [],
    }


def _maintenance_due(project: Path, cycle: dict, closed: str, hit: dict, target: dict,
                     close_commands: list, extra_note: str = '') -> dict:
    """The action that inserts a maintenance node between the node just closed and the one it picked.

    Its `commands` are the real graph edit (`maintenance.insertion`): drop the old direct edge if it
    is there, create the maintenance node, wire the two edges, and pin the cycle on it. Nothing is
    written here -- `apply` is the only writer, and `plan` must stay byte-for-byte read-only.

    It is shaped like `_dispatch_many` on purpose ("走法和 dispatch 一样"): the same `prompt` /
    `prompt_text` / `mode` fields, so `dispatch <cycle>` and `next --spawn` need no second case. The
    difference is WHICH node is pinned -- the one just inserted, not the one the close had picked --
    and that the picked node is named in `deferred_node`, because it is still the next thing the
    line will do, just not yet.
    """
    node_id = maintenance.NEW_NODE
    entry = _node_entry(project, cycle, {'id': node_id, 'title': hit['title']}, DEFAULT_MODE)
    shown = (f'{hit["value"]:g}{hit["unit"]}')
    line = (f'{hit["at"]:g}{hit["unit"]}')
    note = (f'{hit["subject"]} 读到 {shown}，达到这条线策略里的线 {line}'
            f'（`policies.{hit["line"]}`）：'
            f'已把维护节点插在刚收掉的 {closed[:8]} 与它原本挑中的 '
            f'{target.get("title") or target["id"]} 之间，并钉在新节点上。'
            f'派发与 `dispatch` 一样，提交并推送之后跑 '
            f'`pf dispatch {cycle["id"]}`。')
    edit = maintenance.insertion(project, closed, target['id'], cycle['id'], hit,
                                 spec_of=hit['spec_of'], close_commands=close_commands)
    return {
        'action': maintenance.MAINTENANCE_ACTION, 'cycle': cycle['id'],
        **entry,
        'metric': hit['reading'], 'reading': shown, 'line': line,
        'deferred_node': target['id'], 'deferred_title': target.get('title', ''),
        # `extra_note` is the line that would have ridden on the dispatch this action replaces (a
        # hand-named node outside the filter set, for one). It has to keep riding along: the caller
        # never sees the action it displaced.
        'note': f'{note}；{extra_note}' if extra_note else note,
        'steps': [f'write prompt text to {entry["prompt"]}',
                  f'insert a maintenance node with the generated spec '
                  f'({node_spec_path(project, node_id)}), between {closed} and {target["id"]}',
                  'DSH: spawn one session with prompt.txt (`dispatch <cycle>` does this)'],
        # `add` is not in `commands`: it is handed over on its own, because its stdout carries the
        # uuid the rest of the list is written in terms of (`writer._capture_new_node`), and `spec_of`
        # is the generated spec, texted for the node that uuid turns out to name.
        'add_command': edit['add_command'],
        'spec_of': hit['spec_of'],
        'commands': edit['commands'],
    }


def _ticket_wait(cycle: dict, node_id: str, holding=None) -> dict:
    """The node's work is finished and its close has not run yet -- the one live answer.

    This is the instant between the worker writing its signal and running its own closing call. The
    node is a *stable* state: nothing is consumed, nothing is written, and asking again gives the
    same answer. It must never be a dead end -- the caller that lands here is told, in the `note`
    and again in `close_with`, the exact command that closes the node.

    一条线手里还有别的节点时（`holding`），那句话带上它们：各自的条子到了照常收；整条线要走完，
    还得靠它们各自的收尾。

    `--spawn` is deliberately NOT part of `close_with`: closing a node and starting the next session
    are two steps, because the worker still has to commit and push what it made before the next
    session boots on the same tree.
    """
    close_with = f'pf next --after {node_id} --json'
    others = [x for x in (holding or []) if x != node_id]
    tail = (f'（手里还有 {", ".join(x[:8] for x in others)}，它们的条子到了照常收）'
            if others else '')
    return {
        'action': 'wait', 'cycle': cycle['id'], 'node_id': node_id, 'commands': [],
        'close_with': close_with,
        'note': f'{node_id[:8]} 的条子已经在盘上，但这个节点还没被收尾 —— 别轮询，也别自己动状态。'
                f'收尾就一条命令：{close_with}（工具读这个节点的条子、收掉它，然后告诉你下一格是'
                f'`dispatch` 还是 `stop_pipeline`）。{tail}'
                f'派发要排在这一趟提交并推送之后：'
                f'`pf dispatch {cycle["id"]}`。',
    }


def _sessions_wait(cycle: dict, node_ids: list, holding=None) -> dict:
    """手里那几格的会话都开出去了：没有可派的，等它们的条子。

    A dispatch that names a node whose session already runs would boot a second session onto it --
    nothing a caller can act on. 下一步在它们自己的收尾（`next --after <节点id>`）那里，所以这里
    答 `wait`。
    """
    others = [x for x in (holding or []) if x not in node_ids]
    tail = (f'（手里还有 {", ".join(x[:8] for x in others)}，它们照跑）' if others else '')
    names = ', '.join(x[:8] for x in node_ids)
    return {'action': 'wait', 'cycle': cycle['id'], 'node_id': node_ids[0], 'commands': [],
            'note': f'{names} 的会话已经开出去了（`started` 记号在盘上）—— 没有可派的东西，'
                    f'等它们的条子，绝不轮询。它们各自的收尾：'
                    f'pf next --after <节点id> --json。{tail}'}


def _retry_wait(cycle: dict, node_id: str, holding=None) -> dict:
    """A `pending` signal is never a green light -- wait, and name the unwind.

    The note carries the literal commands of the documented retry: a retry is a tool command, not
    steps an agent has to remember. 三条，`--drop` 那条不能省：重写出来的 `pending` 也是红灯，
    光清不拿掉，`next` 就永远答 `wait`（这条卡住过一条线）。
    """
    others = [x for x in (holding or []) if x != node_id]
    tail = (f'（手里还有 {", ".join(x[:8] for x in others)}，它们照跑）' if others else '')
    return {'action': 'wait', 'cycle': cycle['id'], 'node_id': node_id, 'commands': [],
            'note': f'{node_id[:8]} 的条子还是 pending（还没结论，或者有人清了条子准备重试）；'
                    f'不要轮询。解套三条：'
                    f'pf cycle retry {cycle["id"]} {node_id} --drop --summary "重试"，'
                    f'再 pf cycle hold {cycle["id"]} {node_id}，'
                    f'再 pf next --spawn --json。'
                    f'{tail}'}


def _close_commands(project: Path, cycle: dict, node_id: str, outcome: str,
                    decision: str, next_nodes: list | None, park: bool = True) -> list:
    """The mechanical half of a node's close: record the step, mark the node, pick or park.

    `outcome` is the word the path step keeps (`completed` / `failed` / `stopped` -- the ticket's own
    answer). The DAG is marked in the vocabulary IT owns: a node that delivered is `completed`, one
    that did not is `shelved` (out of play, waiting for a human), and a node being worked on is
    `todo`.

    **停手就是转 inactive，一条写死的命令。** 「这条线是不是还该往前走」不押在图上：图会长出新
    节点，而一条停下来的线该不该接着跑，是人说了算的事。工具永远不写 `active`。

    一条没交付的节点被搁置，但它后面要不要接着挑人，走的还是同一条岔路（`when_shelved` 策略已经在
    那一步决定过了）：`shelved` 只说明这个节点出局等人，与线走不走是两件事。

    `park=False` 只做登记，一个字的状态都不写 —— 一条不在服务中的线上，线的状态是人给的，工具
    没有资格动它（收尾那道门用它）。

    `next_nodes` 收的是这次收尾补上来的**整份名单**，里头有几个就 pin 几条：腾出的空位可以不止
    一个（`parallel` 减手里还握着的），少 pin 一条，那一格就静静地空着 —— 动作里的 `nodes` 报了
    它，盘上的手里却没有它，而派发是按手里那几个出的。
    """
    cmds = [seam.cmd('cycle', 'close', cycle['id'], node_id, '--result', outcome),
            seam.dag('status', node_id, 'completed' if outcome == 'completed' else 'shelved')]
    if decision == 'continue' and next_nodes:
        cmds.extend(seam.cmd('cycle', 'hold', cycle['id'], n['id']) for n in next_nodes)
        return cmds
    if park:
        cmds.append(seam.cmd('cycle', 'status', cycle['id'], 'inactive'))
    return cmds


def _stop_action(project: Path, cycle: dict, reason: str, cmds: list,
                 node_id: str | None = None, shelved_nodes=(), stop_line: bool = True,
                 dag: dict | None = None) -> dict:
    return {'action': 'stop_pipeline', 'cycle': cycle['id'], 'node_id': node_id,
            'reason': reason,
            'note': ('cycle parked; nothing more to do without a human' if stop_line else
                     'the line stays in service: the nodes still in hand carry it on when their '
                     'own closes run'),
            # The run is ending, so this is the last place the human-facing loose ends can be
            # handed over. `attention: []` when there is nothing to look at.
            'attention': attention_items(project, cycle, shelved_nodes, dag=dag),
            'commands': cmds}


def attention_items(project: Path, cycle: dict, shelved_nodes=(), dag: dict | None = None) -> list:
    """The things a human has to look at, carried by the stop package.

    Two sources, collected in this order:

      1. the nodes this very action is about to shelve (`shelved_nodes`). They are still `todo` on
         disk while this package is built -- the `dag-manage status ... shelved` command has not run
         yet -- so they have to be named here, or the one package that explains the stop would be the
         one place the shelved node is not mentioned.
      2. the DAG's already-`shelved` nodes (the work is out of play, and `dag-manage revive <id>` is
         the command that puts a node back in: `shelved` → `todo`).

    A node that just CLOSED as completed is deliberately not in here: this list is read as "go look
    at these", and naming a success would send the reader to revive work that is already done.

    **`shelved` 是节点状态，`failed` 是条子上的判决**：一个会话报告 `failed`，说的是「我没交出东西」，
    收尾据此把这个节点搁置（标 `shelved`，出局等人）。所以这里说的是节点状态；而路径上的那一格、
    以及 `signal.py` 的 `failed`，说的是判决。
    """
    out = []
    nodes = (dag if dag is not None else load_dag(project)).get('nodes') or {}
    shelved = list(dict.fromkeys(list(shelved_nodes) + [uid for uid, n in nodes.items()
                                                       if n.get('status') == 'shelved']))
    for uid in shelved:
        if not uid:
            continue
        node = nodes.get(uid) or {}
        out.append({
            'kind': 'node_shelved', 'cycle': cycle['id'], 'node_id': uid,
            'node_title': node.get('title', ''),
            'hint': f'dag-manage revive {uid} 放回 todo（shelved → todo），或人工处理后重开 cycle',
        })
    return out


# ── an action as the caller reads it ─────────────────────────────────────────

def render_action(action: dict, as_json: bool = False, preview: bool = False) -> str:
    """One action as the caller reads it -- the ONE place both `next` front ends print from.

    `preview` 是 `--plan` 那一趟：**预览不可执行**。一份还带着 `commands` 的计划停在半路（机械的那
    几步还没跑），这种情况只报「还在落定」，绝不把命令交给调用方 —— 这是那道不变量在输出这一侧的
    落点，两个前端（`conduct_pipeline.py` 与 `state-manage.py next`）共用它一份。

    `as_json` 只是形状：JSON 给人以外的读者（`ensure_ascii=False`，中文原样），否则是人读的那一版。
    """
    if preview and action.pop('commands', None):
        action['settling'] = True
    return (json.dumps(action, ensure_ascii=False, indent=2) if as_json
            else _action_lines(action))


def _action_lines(action: dict) -> str:
    """The human-facing rendering: what the action is, and everything a reader has to act on."""
    lines = [f"action: {action['action']}"]
    if action.get('cycle'):
        lines.append(f"cycle:  {action['cycle']}")
    # `hint` is where the way back lives (the command that puts a line back in service, or the
    # one that pins the node a cycle with nothing in hand has to choose between): a reader who is
    # not using --json is exactly the reader who needs it, so it is rendered, not just returned.
    for key in ('note', 'reason', 'question', 'hint', 'blocked', 'close_with'):
        if action.get(key):
            lines.append(f"{key + ':':<9} {action[key]}")
    if action.get('node_id'):
        lines.append(f"node:   {action.get('node_title', '')}  ({action['node_id']})")
    for item in action.get('nodes') or []:
        if isinstance(item, dict) and item.get('node_id') != action.get('node_id'):
            lines.append(f"node:   {item.get('node_title', '')}  ({item['node_id']})")
    for step in action.get('steps', []):
        lines.append(f"  - {step}")
    if action.get('prompt'):
        lines.append(f"prompt: {action['prompt']}")
    if action.get('action') in ('dispatch', maintenance.MAINTENANCE_ACTION):
        lines.append(f"mode:   {action.get('mode')}")
    # The maintenance channel's own fields: which measurement crossed, what it read, where the line
    # was, and the node the close had picked before this one was put in front of it. A reader who is
    # not using --json is exactly the reader who needs to see why the graph grew a node.
    if action.get('metric'):
        lines.append(f"metric: {action['metric']} = {action.get('reading')} "
                     f"(line {action.get('line')})")
    if action.get('deferred_node'):
        lines.append(f"after:  {action.get('deferred_title', '')}  ({action['deferred_node']})")
    if 'applied' in action:
        lines.append(f"applied: {action['applied']}")
    for item in action.get('attention', []):
        who = f"{item.get('node_title')} ({item.get('node_id')})" if item.get('node_id') else '?'
        lines.append(f"attention: {item.get('kind')}  {who}")
        if item.get('hint'):
            lines.append(f"           {item['hint']}")
    return '\n'.join(lines)
