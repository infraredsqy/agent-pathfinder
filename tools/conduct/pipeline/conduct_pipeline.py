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
  1. A cycle holds the nodes it claimed (`holding`). A claim is how work is taken:
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
    python conduct_pipeline.py apply  [--cycle ID] [--project DIR] [--after NODE_ID]
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
                             `spawn/host.py`'s `default_spawner`). The per-call `--spawner` beats
                             it, and a cycle's own `spawner` policy beats it too -- the full order
                             is `resolve_spawner`'s.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONDUCT = HERE.parent                      # the conduct tool directory
STATE_MANAGE = HERE / 'state-manage.py'
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
                              text=True, encoding='utf-8', errors='replace', timeout=CMD_TIMEOUT)
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

    这两个函数是**子进程仅有的两条路**（同一条缝的两半）：包里的模块一律经 `seam.run` /
    `seam.capture` 走到这里，谁都不自己 `subprocess`。子进程输出怎么解码也由这里定：一律
    `errors='replace'` —— 这台机器的控制台代码页是 GBK，而 PYTHONIOENCODING 未必设过，读回来的
    字节宁可带替换符，也不能让解码异常把整条管线掀翻。
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
# `spawn/host.py` owns that answer: the two shipped spawners, the process-tree probe that tells the
# two hosts apart, and the `DSH_CONDUCT_SPAWNER` override all live next to the scripts they name, so
# a change to the probe lands in that file while the drills that patch this module's seam keep
# working. What this module keeps of it is one seam name, `SPAWNER_SCRIPT` (bound below, next to the
# loader that reaches the file).


# ═══════════════════════════════════════════════════════════════════════════════
#  Loading a neighbouring file, and the implementation package
# ═══════════════════════════════════════════════════════════════════════════════

# `tools/shared/` is the shared layer, and it has no package to import from: every file in it is
# loaded by path through `loader.py`. This function is that bootstrap, the same shape in every file
# that needs it -- a path load, never a name import, because nothing puts `tools/` on `sys.path`.
SHARED = CONDUCT.parent / 'shared'

_LOADER = None


def _loader():
    """`tools/shared/loader.py`: the one loader, for the modules this tree keeps outside `shared/`."""
    global _LOADER
    if _LOADER is None:
        spec = importlib.util.spec_from_file_location('pf_shared_loader', SHARED / 'loader.py')
        _LOADER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_LOADER)
    return _LOADER


def _tool_path(env_var: str, default):
    """`tools/shared/toolpath.py`: `<env var>` when it is set, else the neighbour this tree ships.

    The two tools below are the tree's parts this module only calls; one rule, resolved once, is what
    keeps `DSH_CONDUCT_DAG_MANAGE` and `DSH_CONDUCT_ISSUE_MANAGE` meaning the same thing here as they
    do in the merge driver.
    """
    toolpath = _loader().load_by_path('pf_shared_toolpath', SHARED / 'toolpath.py',
                                      register=True, reuse=True)
    return toolpath.tool_path(env_var, default)


# `dag-manage.py` lives in the sibling `dag/` directory of the shipped tree, and that is the only
# place this module looks for it. A tree that keeps the conduct scripts somewhere else (a review
# draft, a drill copy) points at the real tool with `DSH_CONDUCT_DAG_MANAGE` rather than relying on
# this module having a second search path of its own: a silent fallback into `~/.agents` would also
# hide the failure that matters -- a shipped tree whose sibling `dag/` went missing.
DAG_MANAGE = _tool_path('DSH_CONDUCT_DAG_MANAGE', CONDUCT.parent / 'dag' / 'dag-manage.py')
#: `issue_manager.py` owns the issue pool (`.pf/issues/issues.json`) and the one number this side
#: reads out of it: the pool's weighted debt, which the maintenance channel compares with a line's
#: `issue_pool_weight_limit`. Same rule as `DAG_MANAGE` above -- the sibling `issue/` directory of
#: the shipped tree, with `DSH_CONDUCT_ISSUE_MANAGE` for a tree that keeps these scripts elsewhere.
ISSUE_MANAGE = _tool_path('DSH_CONDUCT_ISSUE_MANAGE', CONDUCT.parent / 'issue' / 'issue_manager.py')


def _load_impl():
    """Load `conduct_pipeline_impl/` by path, fresh, and hand it back.

    By PATH, not by name, and with the name dropped from `sys.modules` first: every caller loads
    this file fresh (`tests/harness.py`, `handoff.py`, `state-manage.py`), and a by-name import
    would hand the second load the first load's modules. Inside the package, `from . import store`
    resolves through the registration the load makes.
    """
    pkg = 'conduct_pipeline_impl'
    return _loader().load_by_path(pkg, CONDUCT / pkg / '__init__.py',
                                  package_dir=CONDUCT / pkg, register=True, fresh=True)


def _host():
    """`spawn/host.py`: the two spawners, the host probe and the pid question, loaded by path.

    One copy per process (`register` + `reuse`): this module and the logic package's writer both ask
    for it, and a caller patching the probe patches the copy they both see.
    """
    return _loader().load_by_path('conduct_spawn_host', CONDUCT / 'spawn' / 'host.py',
                                  register=True, reuse=True)


#: The entry's `SPAWNER_SCRIPT`: what `seam.spawner_script()` hands `writer.spawn_session`, read at
#: call time because a caller may replace it. Its value is the host's own answer, asked once here.
SPAWNER_SCRIPT = _host().default_spawner()

_impl = _load_impl()

# The seam: the logic modules read `_cmd` / `_dag` / `_as_text` / `_run` / `CMD_TIMEOUT` /
# `SPAWNER_SCRIPT` out of THIS module's live namespace, so a caller patching them here is honoured
# everywhere. `globals()` is the live dict, not a copy -- that is the whole point.
_impl.seam.bind(globals())

# Everything the package exports, reachable from here: the CLI below, `handoff.py`, `state-manage.py`
# and the test harness all load this module and see one flat surface -- exactly the one it offered at
# module scope before the split.
globals().update({name: getattr(_impl, name) for name in _impl.__all__})


# ── CLI ──────────────────────────────────────────────────────────────────────

# The action vocabulary, printable with `conduct_pipeline.py contract`. Kept as data so the doc and
# the code cannot drift: the same list answers "what can `next` hand me". This dict is the single
# source of the names.
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
}


def _render_contract() -> str:
    lines = ['`next` 返回的 action 全集（判别式是 `action` 字段）：']
    for name, what in ACTIONS.items():
        lines.append(f'  {name:<15} {what}')
    lines.append('')
    lines.append('不变量：返回的 action **不带 `commands`** —— 命令由工具自己跑；'
                 '调用方只读 `action`，做只有会话能做的事。')
    return '\n'.join(lines)


def main(argv=None) -> int:
    """This module's own entry: parse the line, run `plan` / `apply` / `record`, print the answer.

    What is here is the parse, the vocabulary (`contract`) and the one call into the package. The
    output -- JSON or the human rendering, and the rule that a preview carries no commands -- is
    `actions.render_action`'s, the same one `pf next` prints through, so the two spellings of the
    pipeline's front door cannot drift apart in what they show.

    The closing call's answers (`--continue` / `--stop` / `--node` / `--note`) are refused unless
    something is being settled or previewed, exactly as `pf next` refuses them without `--after` or
    `--plan`: with nothing being settled, there is no question for them to answer, and a quiet no-op
    would read as if that answer had been taken. `apply` is this module's spelling of
    `pf next --after <node>` -- the settle that reads the ticket -- so `--after` names the node it is
    about (one line can hold several at once) and the closing answers ride with it; the reason text a
    human reads is the same on both entries, `self_verified` and `decision_given` included. `--after`
    is refused where nothing is settled (`plan` / `record`): the preview spelling of a closing call is
    `plan` with the same answers.
    """
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
    parser.add_argument('--after', dest='after', default=None, metavar='NODE_ID',
                        help='apply: settle THIS node -- how a line holding more than one node says '
                             'which close this is')
    parser.add_argument('--note', default='', help='why the closing call decided that')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    if args.command == 'contract':
        print(_render_contract())
        return 0

    decision = 'continue' if args.cont else 'stop' if args.stop else None
    decision_given = bool(args.cont or args.stop)
    if args.after and args.command != 'apply':
        print('Error: --after names the node `apply` settles, and nothing is settled by '
              f'`{args.command}`. Nothing was changed.', file=sys.stderr)
        return 2
    if (decision or args.node or args.note) and args.command != 'plan' and not args.after:
        print("Error: --continue/--stop/--node/--note are the closing call's answers and need "
              '`plan` (preview that call) or `apply --after <node_id>` (settle that node). '
              'Nothing was changed.', file=sys.stderr)
        return 2
    try:
        if args.command == 'record':
            action = record(args.project, args.cycle)
        elif args.command == 'apply':
            action = apply(args.project, args.cycle, decision=decision, node=args.node,
                           note=args.note, self_verified=True, after=args.after,
                           decision_given=decision_given)
        else:
            action = plan(args.project, args.cycle, decision=decision, node=args.node,
                          note=args.note, decision_given=decision_given)
    except (RuntimeError, ValueError) as e:
        print(f'Error: {e}', file=sys.stderr)
        return 2
    print(render_action(action, args.json, preview=args.command == 'plan'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
