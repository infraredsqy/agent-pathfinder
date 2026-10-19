#!/usr/bin/env python3
"""Write a completion/failure signal for a conduct node.

Usage:
    python signal.py <cycle> <node> <status> -s <summary> [-o <outputs_json>]

One signal per node per cycle, at `.pf/signals/<cycle>/<node>.json`. A terminal signal on
disk is what tells the pipeline this node's work is done and only its close is left; how the node's
own work was split, and how many sessions it took, never reaches this file.

Exit codes:
    0   Signal written successfully
    1   Validation error or write failure
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONDUCT = HERE.parent                      # the conduct tool directory

# 引导：装载器也按路径取，所以这一段只能每个文件自己带（见 `shared/loader.py`）。
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


_STORE = None


def _store():
    """`conduct_state.store`, loaded by path -- the record's owner, and the one writer of a signal.

    The record commands (state / dag / issue) do not write where cwd points when cwd is a linked
    worktree: they map the worktree back to the main tree, because `.pf/` lives there and only
    there. A worker that runs THIS script directly (not through handoff.py, which resolves the root
    itself and passes `base_dir`) would otherwise trust cwd and drop the signal into the worktree's
    stale `.pf/` fork -- data the main tree never reads, lost in effect. Loading store by path reuses
    its one implementation of that sense (`_write_signal`, the record's one writer), which is what a
    caller that passes no `base_dir` lands on.
    """
    global _STORE
    if _STORE is None:
        _STORE = _loader().load_by_path('pf_conduct_state_store',
                                        CONDUCT / 'conduct_state' / 'store.py',
                                        register=True, reuse=True)
    return _STORE


# 'stopped' is distinct from 'failed': a node may end because the user called it off.
VALID_STATUSES = {'completed', 'failed', 'stopped'}


def write_signal(cycle: str, node: str, status: str, summary: str,
                 outputs: dict, base_dir: Path | None = None) -> Path:
    """Write one node's signal. Returns the path it landed on.

    Factored out of main() so handoff.py can write the signal and carry on in the same process,
    instead of shelling out to a second Python start on the last line of every node.

    The record -- its six keys, its path under `.pf/signals/`, its timestamp -- belongs to
    `conduct_state.store`, which writes it: this function checks what a caller handed over and hands
    it on. `base_dir` is that store's signals directory, for a caller that already resolved the
    project root (`handoff.py` does, from a worktree cwd); it is where THIS one write lands and
    nothing more -- the store's own binding is left exactly where it was, and a write that names no
    `base_dir` still uses it (cwd resolved at call time).
    """
    if not isinstance(status, str) or not status:
        print('Error: a signal needs a status', file=sys.stderr)
        sys.exit(1)
    if not isinstance(outputs, dict):
        print('Error: outputs must be a JSON object', file=sys.stderr)
        sys.exit(1)

    store = _store()
    return store._write_signal(cycle, node, status, summary, outputs,
                               signals_dir=None if base_dir is None else Path(base_dir))


def main() -> None:
    # stdout/stderr 都按 UTF-8 走：报错和路径里可能出现中文，控制台代码页（这台机器默认 GBK）
    # 会把字节写歪，按 UTF-8 解码的父进程直接炸在读线程上。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Write the signal for one conduct node')
    parser.add_argument('cycle', help='Cycle identifier (e.g. story-data)')
    parser.add_argument('node', help='DAG node id this session was working on')
    parser.add_argument('status', choices=sorted(VALID_STATUSES),
                        help='completed | failed | stopped')
    parser.add_argument('-s', '--summary', required=True, help='One-line summary of what happened')
    parser.add_argument('-o', '--outputs', default='{}', help='JSON string of key outputs')
    args = parser.parse_args()

    try:
        outputs = json.loads(args.outputs)
    except json.JSONDecodeError as e:
        print(f'Error: outputs is not valid JSON: {e}', file=sys.stderr)
        sys.exit(1)

    if not isinstance(outputs, dict):
        print('Error: outputs must be a JSON object', file=sys.stderr)
        sys.exit(1)

    if args.status == 'completed' and not args.summary.strip():
        print('Warning: summary is empty', file=sys.stderr)
        # Non-fatal, proceed

    final_path = write_signal(args.cycle, args.node, args.status, args.summary, outputs)
    print(f'Signal written: {final_path}')


if __name__ == '__main__':
    main()
