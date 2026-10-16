#!/usr/bin/env python3
"""The conduct pipeline's advance logic, as code instead of prose.

What the pipeline is
--------------------
A cycle is one line of work. It picks ONE DAG node, hands it to a session, and when that session
reports back it records the step and picks the next node -- or stops. One node at a time, one cycle
at a time (a second active cycle is legal, but then a bare `next` has to be told which one).

    cycle -> node -> (the session's own work on it) -> next node or stop

How a node splits its own work is the session's business and never the pipeline's: those are
subagents it commands, not cells this tool tracks. Nothing about a node's internals is written to
`.pf/` -- what the pipeline keeps is the cycle's identity, the node it is holding right now, and the
path of nodes it has finished. Its dispatch plumbing (prompt files, the broken-chain marker) sits in
`.pf/meta/`, which `.pf/.gitignore` keeps out of the history.

Two rules this module owns
--------------------------
**`commands` are never handed to an agent.** The tool runs them; the caller reads `action` and does
the one thing only a session can do: spawn a session, or ask a human. (The v1 bug this prevents: a
tool that both performed an action and handed the agent the commands to perform it again stepped the
pointer twice.)

**The DAG's node status is the single source of truth for "did it work".** A node reports by writing
a signal; a terminal signal on disk means its work is finished and only its close is left. The
worker's own closing call -- `state-manage.py next --after <node_id> --json` -- reads the outcome
off that signal, records the path step, marks the node, and answers what comes next. Starting the
next session is a SEPARATE command (`dispatch <cycle>`, or the one-shot `next --spawn`), because the
worker still has to commit and push before the next session boots in the same worktree.

Invariants this module owns, each of which cost a bug to learn
-------------------------------------------------------------
  1. A cycle holds at most one node at a time (`current_node`). Setting it is how work is claimed:
     another cycle's pick skips a node some line is holding.
  2. A terminal signal is the whole of "this node is done"; a `running` signal is never a green
     light. `cycle retry` is what preps a retry.
  3. A node whose work is finished but whose close has not run is a *stable* state: `next` answers
     `wait` with the closing command, as many times as it is asked, and writes nothing.
  4. Closing a node is not the same as finishing the cycle: the close records the step and marks the
     DAG node, and it is the worker's own `next --after` that performs it.
  5. The node a cycle is about is recorded when the cycle is pointed at it, not when it reports.
     That is what makes a mid-run edit of the DAG harmless.
  6. `commands` are run here and stripped from the returned action.
  7. apply() refuses to report success unless something really moved.
  8. Every child process is bounded (CMD_TIMEOUT); a hung child must fail loudly, not hang a night.
  9. Instructions may only TIGHTEN the stop rule, never loosen it: a node whose mode is `checkpoint`
     always stops the run at the end of its node, whatever the instructions say -- and whatever the
     closing call asked for. `auto` and `manual` may carry on: a manual node's whole life is the
     conversation with the user, and its completion is the user's release, so there is no second
     gate at the close. The node's own mode is the one place that question is answered; there is no
     line-level mode above it.
 10. The node's outcome is coarse on purpose: `completed` is the worker saying it delivered and
     `failed`/`stopped` is the worker saying it did not. Deviation is normal; only a *total* failure
     is worth stopping a night for.

Usage:
    python conduct_pipeline.py plan   [--cycle ID] [--project DIR]
    python conduct_pipeline.py apply  [--cycle ID] [--project DIR]
    python conduct_pipeline.py record [--cycle ID] [--project DIR]
    python conduct_pipeline.py contract

Those four are this module's own entry, for drills and tests. The pipeline's one entry point is
`pf next`: `--plan` runs `plan`, `--after <node>` runs `apply`, neither runs `record`, and
`pf next --help` prints the action vocabulary `contract` prints.

Environment:
    DSH_CONDUCT_DAG_MANAGE   where `dag-manage.py` is, when it is not the sibling `dag/`
                             directory of this file (a review draft, a drill copy).
    DSH_CONDUCT_SPAWNER      which spawner opens the next session: `dsh`, `zcode`, or a path.
                             Default: whichever host this shell is running under (see
                             `_default_spawner`). The per-call `--spawner` beats it, and a
                             cycle's own `spawner` policy beats it too -- the full order is
                             `resolve_spawner`'s.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONDUCT = HERE.parent                      # the conduct tool directory
STATE_MANAGE = HERE / 'state-manage.py'
SPAWN_SCRIPTS = {'dsh': CONDUCT / 'spawn' / 'dsh-spawn.ps1', 'zcode': CONDUCT / 'spawn' / 'zcode-spawn.ps1'}
# `dag-manage.py` lives in the sibling `dag/` directory of the shipped tree, and that is the only
# place this module looks for it. A tree that keeps the conduct scripts somewhere else (a review
# draft, a drill copy) points at the real tool with `DSH_CONDUCT_DAG_MANAGE` rather than relying on
# this module having a second search path of its own: a silent fallback into `~/.agents` would also
# hide the failure that matters -- a shipped tree whose sibling `dag/` went missing.
DAG_MANAGE = Path(os.environ.get('DSH_CONDUCT_DAG_MANAGE')
                  or CONDUCT.parent / 'dag' / 'dag-manage.py')
#: `issue_manager.py` owns the issue pool (`.pf/issues/issues.json`) and the one number this side
#: reads out of it: the pool's weighted debt, which the maintenance channel compares with a line's
#: `issue_pool_weight_limit`. Same rule as `DAG_MANAGE` above -- the sibling `issue/` directory of
#: the shipped tree, with `DSH_CONDUCT_ISSUE_MANAGE` for a tree that keeps these scripts elsewhere.
ISSUE_MANAGE = Path(os.environ.get('DSH_CONDUCT_ISSUE_MANAGE')
                    or CONDUCT.parent / 'issue' / 'issue_manager.py')
#: Every child process this module starts is bounded by this many seconds. It lives
#: HERE, in the entry module, with the command builders below and `_run`, because those are the
#: names a caller patches on the loaded module: the test harness replaces `pipeline._run` with its
#: in-process runner and sets `pipeline.CMD_TIMEOUT` to prove a hung child is reported. The logic
#: modules in `conduct_pipeline_impl/` reach them through `seam`, which reads this module's globals at
#: call time; a by-name import would freeze a copy and make every one of those patches invisible.
CMD_TIMEOUT = 120


# ── command builders (the mechanical half) ───────────────────────────────────

def _cmd(*args) -> list:
    return [sys.executable, str(STATE_MANAGE), *[str(a) for a in args]]


def _dag(*args) -> list:
    return [sys.executable, str(DAG_MANAGE), *[str(a) for a in args]]


def _issue(*args) -> list:
    return [sys.executable, str(ISSUE_MANAGE), *[str(a) for a in args]]


def _as_text(cmd: list) -> str:
    return ' '.join(str(c) for c in cmd)


def _run(cmd: list, cwd: Path) -> None:
    # cwd matters: `.pf/` is resolved relative to the process cwd.
    try:
        proc = subprocess.run([str(c) for c in cmd], cwd=str(cwd), capture_output=True,
                              text=True, encoding='utf-8', timeout=CMD_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f'command did not finish within {CMD_TIMEOUT}s: {_as_text(cmd)}') from e
    if proc.returncode != 0:
        raise RuntimeError(f'command failed: {_as_text(cmd)}\n{proc.stderr or proc.stdout}')


def _capture(cmd: list, cwd: Path) -> str:
    """Run one command and hand back its stdout.

    `_run` discards stdout on purpose -- a command's effect is what the pipeline wants from it. The
    maintenance channel's `dag-manage add` is the one command whose EFFECT is a fact that only
    appears in its output (the uuid it minted), so that one call needs the text as well. Same bound,
    same loud failure, and it lives here next to `_run` because it is the same seam: a caller
    patching child processes patches this too.
    """
    try:
        proc = subprocess.run([str(c) for c in cmd], cwd=str(cwd), capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=CMD_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f'command did not finish within {CMD_TIMEOUT}s: {_as_text(cmd)}') from e
    if proc.returncode != 0:
        raise RuntimeError(f'command failed: {_as_text(cmd)}\n{proc.stderr or proc.stdout}')
    return proc.stdout or ''


# ── which spawner opens the next session ─────────────────────────────────────
# Two hosts, one pipeline. `dsh-spawn.ps1` opens a DSH session over the DSH web API. Under ZCode that
# session would never reach the GUI -- the task list there is a projection seeded once inside the
# agent host, and a local workspace has no re-read path -- so the ZCode half queues the node into the
# desktop's own cron scheduler instead (see `zcode-dispatch.py`). What the pipeline touches is the
# same either way: one argv shape (built in `conduct_spawn.py`), the same `spawned:` last line, and
# the same failure marker.

class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', ctypes.c_ulong),
        ('cntUsage', ctypes.c_ulong),
        ('th32ProcessID', ctypes.c_ulong),
        ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)),
        ('th32ModuleID', ctypes.c_ulong),
        ('cntThreads', ctypes.c_ulong),
        ('th32ParentProcessID', ctypes.c_ulong),
        ('pcPriClassBase', ctypes.c_long),
        ('dwFlags', ctypes.c_ulong),
        ('szExeFile', ctypes.c_wchar * 260),
    ]


def spawn_path(which=None) -> Path:
    """A spawner as a path: the bare words `dsh` / `zcode`, or any path a caller names outright."""
    if which is None:
        return SPAWN_SCRIPTS['dsh']
    text = str(which).strip()
    if text.lower() in SPAWN_SCRIPTS:
        return SPAWN_SCRIPTS[text.lower()]
    return Path(text).expanduser()


def _under_zcode() -> bool:
    """Is a `ZCode.exe` process an ancestor of this one?

    DSH answers this for itself: it injects `DSH_SESSION_ID` into every shell it runs, so its own
    case needs no probe. ZCode injects nothing into the Bash tool (`ZCODE_SESSION_ID` is set for
    hooks, slash-command expansion and MCP servers only), so the process tree is the one dependable
    signal left: a tool call inside ZCode hangs off the agent host, and that host is `ZCode.exe`.
    """
    if os.name != 'nt':
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot == -1:
            return False
        parents = {}
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        try:
            more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                parents[entry.th32ProcessID] = (entry.th32ParentProcessID, str(entry.szExeFile))
                more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        # A probe that cannot answer must not decide: fall through to the historical default.
        return False
    pid = os.getpid()
    for _ in range(12):
        parent, name = parents.get(pid, (0, ''))
        if name.lower() == 'zcode.exe':
            return True
        if parent in (0, pid):
            return False
        pid = parent
    return False


def _default_spawner() -> Path:
    """Which spawner this shell's host should use, when the caller names none.

    `DSH_CONDUCT_SPAWNER` answers first: a caller that has already decided should not be
    second-guessed. Then the host answers for itself. Anything else -- a human at a prompt, a drill,
    another harness -- keeps the historical default, so DSH behaves exactly as it did before this
    file learned about a second host.
    """
    explicit = os.environ.get('DSH_CONDUCT_SPAWNER')
    if explicit:
        return spawn_path(explicit)
    if os.environ.get('DSH_SESSION_ID'):
        return SPAWN_SCRIPTS['dsh']
    if _under_zcode():
        return SPAWN_SCRIPTS['zcode']
    return SPAWN_SCRIPTS['dsh']


#: The entry's `SPAWN_PS1`: what `seam.spawn_ps1()` hands `writer.spawn_session`, read at call time
#: because a caller -- and now a host -- may replace it.
SPAWN_PS1 = _default_spawner()


def resolve_spawner(project, cycle_id: str | None, cli_value: str | None = None) -> Path:
    """Which spawner opens this dispatch's session: the most specific answer wins.

    `--spawner`（调用方这一次的点名叫什么就是什么）> 这条线自己的 `spawner` 策略（
    `pf cycle policy <cycle> spawner dsh|zcode`，随线记录走）> `DSH_CONDUCT_SPAWNER`（这台机器）
    > 宿主自认 > `dsh`。线没配策略、调用没带旗子，落回 `SPAWN_PS1` —— 与这条函数存在之前
    完全同一条路，所以没配的线行为一字不变。`SPAWN_PS1` 而不是现算 `_default_spawner()`：
    它是本模块可替换的那道缝（`seam.bind` 的约定），替换它的调用方在线策略缺席时仍被尊重。
    """
    if cli_value:
        return spawn_path(cli_value)
    if cycle_id:
        cycle = find_cycle(load_state(project), cycle_id)
        word = cycle_policy(cycle).get('spawner') if cycle else None
        if word:
            return SPAWN_SCRIPTS[word]
    return SPAWN_PS1


# ═══════════════════════════════════════════════════════════════════════════════
#  The implementation package, loaded by path
# ═══════════════════════════════════════════════════════════════════════════════

def _load_impl():
    """Load `conduct_pipeline_impl/` by path, fresh, and hand it back.

    By PATH, not by name, and with the name dropped from `sys.modules` first: every caller loads
    this file fresh (`tests/harness.py`, `handoff.py`, `state-manage.py`), and a by-name import
    would hand the second load the first load's modules. Inside the package, `from . import store`
    resolves through the registration below.
    """
    pkg = 'conduct_pipeline_impl'
    for stale in [n for n in sys.modules if n == pkg or n.startswith(pkg + '.')]:
        del sys.modules[stale]
    spec = importlib.util.spec_from_file_location(
        pkg, CONDUCT / pkg / '__init__.py', submodule_search_locations=[str(CONDUCT / pkg)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg] = mod
    spec.loader.exec_module(mod)
    return mod


_impl = _load_impl()

# The seam: the logic modules read `_cmd` / `_dag` / `_as_text` / `_run` / `CMD_TIMEOUT` /
# `SPAWN_PS1` out of THIS module's live namespace, so a caller patching them here is honoured
# everywhere. `globals()` is the live dict, not a copy -- that is the whole point.
_impl.seam.bind(globals())

# Everything the package exports, reachable from here: the CLI below, `handoff.py`, `state-manage.py`
# and the test harness all load this module and see one flat surface -- exactly the one it offered at
# module scope before the split.
globals().update({name: getattr(_impl, name) for name in _impl.__all__})


# ── CLI ──────────────────────────────────────────────────────────────────────

# The action vocabulary, printable with `conduct_pipeline.py contract`. Kept as data so the doc and
# the code cannot drift: the same list answers "what can `next` hand me". This dict is the single
# source of the names -- a name added to the code without a line here, or a line here with no
# producer, fails the suite.
ACTIONS = {
    'dispatch':      '一个节点已钉住、prompt 已落盘 —— 把它派出去（`dispatch <cycle>` 就是干这个；'
                     '`next --spawn` 是一次到位的同一次调用）',
    'open_node':     '工具自己在钉下一个节点 —— 调用方读到的已经是那一格 dispatch',
    'maintenance_due': '维护度量越线了，工具刚往图里插了一个维护节点并把它钉住 —— 走法与 '
                       '`dispatch` 一样：prompt 已落盘、可以派发，区别只在于被钉住的是新插入的'
                       '那个节点（提交并推送之后照样 `dispatch <cycle>`）',
    'stop_pipeline': '没活了 / 有人叫停 / 这条线不在服务中 —— 收摊，把结局告诉用户',
    'wait':          '还没到能动手的时候 —— 读 note / close_with，绝不轮询',
    'ask_user':      '状态有歧义 —— 问人',
    'no_cycle':      '还没有 cycle —— 开 cycle 要人的意图',
    'fail_hard':     '状态坏了 / 链走不下去 —— 把 reason 和 options 摆给用户',
}


def _render_contract() -> str:
    lines = ['`next` 返回的 action 全集（判别式是 `action` 字段）：']
    for name, what in ACTIONS.items():
        lines.append(f'  {name:<15} {what}')
    lines.append('')
    lines.append('不变量：返回的 action **不带 `commands`** —— 命令由工具自己跑；'
                 '调用方只读 `action`，做只有会话能做的事。')
    return '\n'.join(lines)


def _render(action: dict) -> str:
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
    if action.get('action') in ('dispatch', 'maintenance_due'):
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


def main(argv=None) -> int:
    # 中文 everywhere：reason、options、contract 全是人话。两个流都按 UTF-8 走，否则控制台代码页
    # （这台机器默认 GBK）把字节写歪，按 UTF-8 解码的父进程直接炸在读线程上（stdout 变 None）。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(
        prog='conduct_pipeline.py',
        description='Decide what the conduct pipeline does next (read-only planner + writer).')
    parser.add_argument('command', choices=['plan', 'apply', 'record', 'contract'])
    parser.add_argument('--cycle', help='Cycle id (default: the single active cycle)')
    parser.add_argument('--project', default='.', help='Project root (default: cwd)')
    parser.add_argument('--continue', dest='cont', action='store_true',
                        help="the closing call's answer: keep going")
    parser.add_argument('--stop', dest='stop', action='store_true',
                        help="the closing call's answer: stop here (parks the cycle)")
    parser.add_argument('--node', help="the closing call's override: which node to run next")
    parser.add_argument('--note', default='', help='why the closing call decided that')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    if args.command == 'contract':
        print(_render_contract())
        return 0

    decision = 'continue' if args.cont else 'stop' if args.stop else None
    fn = {'plan': plan, 'apply': apply, 'record': record}[args.command]
    try:
        if args.command == 'record':
            action = fn(args.project, args.cycle)
        else:
            action = fn(args.project, args.cycle, decision=decision,
                        node=args.node, note=args.note)
    except (RuntimeError, ValueError) as e:
        print(f'Error: {e}', file=sys.stderr)
        return 2
    if args.command == 'plan':
        # A preview must not be runnable. A plan that carried commands was still mid-step
        # (mechanical bookkeeping) -- say so instead.
        if action.pop('commands', None):
            action['settling'] = True
    print(json.dumps(action, ensure_ascii=False, indent=2) if args.json
          else _render(action))
    return 0


if __name__ == '__main__':
    sys.exit(main())
