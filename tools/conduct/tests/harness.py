"""Helpers for exercising the conduct pipeline against a throwaway project.

The shape: **one DAG node = one cycle step = one `pf-conduct` session**, recorded in the cycle
record's own `path` inside `state.json`. There is no stage and no round -- no per-round file, no
second pointer, no cell index anywhere. A node's work is done when its signal is on disk
(`.pf/signals/<cycle>/<node_id>.json`), and the worker that ran it closes it with its ONE
`next --after <node_id>` call.

**Two roots.** The account -- the DAG, `state.json`, the signals, the instructions account, a
node's own text -- is the LEDGER at `<project>/.pf/`. Everything
disposable is the SCRATCH root at `<project>/.pf/`: a node's generated `temp/`, and the
pipeline's own dispatch plumbing (`meta/launch/`, the broken-chain marker).

Two requirements pull against each other here, and the design is the compromise between them:

  1. **No mocks.** Every test drives the real modules and the real CLIs; state only ever advances
     through the tools (never through a helper that writes a stage status by hand).
  2. **The suite must finish in seconds.** On Windows every `python x.py` start costs ~20-45 ms,
     and a round needs a dozen of them, so a naive "shell out for everything" harness spends its
     life waiting on process startup (the first draft of this file ran the same 100 cases in 48 s
     of CPU; it is now ~4 s wall-clock on 16 workers).

So the harness runs the *same* code two ways, and a test proves the two agree:

    FAST (default)      conduct_pipeline.py is imported once and called in-process (`plan` / `apply` /
                        `record`). The `state-manage.py` commands those functions would shell out
                        to are executed by calling that module's own `cmd_*` functions in-process,
                        with its path globals pointed at the sandbox; the `dag-manage` calls inside
                        `ranked_ready_nodes` are answered by dag-manage's own `cmd_list` /
                        `cmd_status`. Same module, same functions, same files, no process start.
    CLI (`cli=True`)    the same operations go through `subprocess` -> `state-manage.py next`.

`TestContract.test_the_fast_path_and_the_cli_agree` drives one whole round both ways and compares
the two projects file by file, so the fast path cannot silently drift from the shipped surface.
Tests whose *subject* is the CLI use `cli=True` (or call the binary directly), and `handoff.py` --
the one command a worker runs -- is always a real process.

One thing the sandbox answers without asking the operating system, and it is an INPUT to the tool
rather than a step of it, documented where it is defined:

    `_dag_hook`       dag-manage's `list` and `status` answer in-process (a process start
                      per plan was the single biggest cost in the suite).

    the ONLY ways state moves, in either mode:
      * `conduct_pipeline.plan / apply / record`   (the tool's own planner and writer)
      * the `state-manage.py` commands          (human/launcher levers and the closing call)
      * `handoff.py` / `signal.py`              (what a worker session's last act really is)

What the harness never does: set the cycle's pointer, append a path step, or close a node itself.
A harness that "helped" would hide exactly the bugs this suite exists to catch -- an earlier
version patched state by hand and hid a missing bookkeeping call behind 30 green tests. The one
place a fixture writes state files directly (`rewrite_cycle` / `rewrite_state`) exists only to
forge on-disk state the tools refuse to produce, which is how the guards against it get tested.

Two roles are stubbed, because they are what a session does and no session can be started here:

    WorkerSession    a node's session: do the work, then run ONE command (handoff.py / signal.py)
    Closer           the same worker's closing act: read what `next` asks for, run `next --after`

`Driver` plays both, node after node, and checks the apply contract on every applied action
("applied" must really have moved the project on disk).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Every `harness_*` module lives in this directory and is imported BY NAME (`import harness`), so the
# directory has to be on `sys.path` before any of them is imported -- a test module does the same
# insert before `from harness import ...`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# The harness is split by what it does; the pieces are star-imported back together here so that a
# caller keeps seeing one module (`from harness import Sandbox, Driver, ...` is what every test
# module does). `harness_sandbox.py` is the one that must not import the CLI runner by name: the
# test suite patches that module's attribute -- see the comment in that file.
from harness_env import *         # noqa: F401,F403
from harness_proc import *        # noqa: F401,F403
from harness_fast import *        # noqa: F401,F403
from harness_sandbox import *     # noqa: F401,F403
from harness_roles import *       # noqa: F401,F403

__all__ = [
    'CONDUCT_DIR', 'DAG_MANAGE', 'DELETED_SIGNAL_KEYS', 'PIPELINE_PATH', 'HANDOFF_PY', 'HERE',
    'NODE_DIR_OF', 'SIGNAL_KEYS', 'SIGNAL_OF', 'SIGNAL_PY', 'SKILLS', 'SPEC_OF', 'STATE_MANAGE',
    'TERMINAL_ACTIONS', 'TOOLS_DIR',
    'Closer', 'Driver', 'FastStateManage', 'InProcessDagManage', 'Sandbox',
    'WorkerSession', 'chdir', 'finish_node', 'keep_going', 'load_pipeline', 'next_action',
    'plan_action', 'run_dag', 'run_handoff', 'run_signal', 'run_state_manage', 'stop_after',
    '_dag_hook',
]
