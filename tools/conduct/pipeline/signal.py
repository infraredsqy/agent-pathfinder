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
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _store():
    """`conduct_state.store`, loaded by path -- the same file every tool in this directory borrows.

    The record commands (state / dag / issue) do not write where cwd points when cwd is a linked
    worktree: they map the worktree back to the main tree, because `.pf/` lives there and only
    there. A worker that runs THIS script directly (not through handoff.py, which resolves the root
    itself and passes `base_dir`) used to trust cwd and drop the signal into the worktree's stale
    `.pf/` fork -- data the main tree never reads, lost in effect. Loading store by path reuses its
    one implementation of that sense: its `PROJECT_ROOT` is resolved from cwd at import, which is
    exactly the binding this script wants.
    """
    spec = importlib.util.spec_from_file_location('conduct_signal_store',
                                                  HERE.parent / 'conduct_state' / 'store.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_STORE = _store()
#: `<project>/.pf/signals/`: a signal belongs with the rest of what this project's pipeline
#: records, so it goes next to `state.json`. Resolved through store's worktree sense, so a worktree
#: cwd lands the signal in the main tree.
SIGNALS_DIR = _STORE.PROJECT_ROOT / _STORE.LEDGER_NAME / 'signals'
# 'stopped' is distinct from 'failed': a node may end because the user called it off.
VALID_STATUSES = {'completed', 'failed', 'stopped'}


def _replace_with_retry(tmp: str, path: Path, attempts: int = 4) -> None:
    """`os.replace`, tolerating a reader that holds the target open this instant.

    Copied from `conduct_state.store` (a convention this small sits beside every writer in the
    tree): on Windows a plain `open()` for reading does not carry FILE_SHARE_DELETE, so a concurrent
    reader mid-`json.load` makes the replace fail with PermissionError. The window is tiny and the
    retry is bounded well under a second.
    """
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def write_signal(cycle: str, node: str, status: str, summary: str,
                 outputs: dict, base_dir: Path | None = None) -> Path:
    """Write one node's signal. Returns the path it landed on.

    Factored out of main() so handoff.py can write the signal and carry on in the same process,
    instead of shelling out to a second Python start on the last line of every node.
    """
    signal = {
        'cycle': cycle,
        'node': node,
        'status': status,
        'summary': summary,
        'outputs': outputs,
        'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }

    signal_dir = Path(base_dir or SIGNALS_DIR) / cycle
    signal_dir.mkdir(parents=True, exist_ok=True)

    final_path = signal_dir / f'{node}.json'
    fd, tmp = tempfile.mkstemp(dir=str(signal_dir), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(signal, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, final_path)
    except Exception as e:
        print(f'Error: write failed: {e}', file=sys.stderr)
        if os.path.exists(tmp):
            os.unlink(tmp)
        sys.exit(1)

    return final_path


def main() -> None:
    # stdout/stderr 都按 UTF-8 走：报错和路径里可能出现中文，控制台代码页（这台机器默认 GBK）
    # 会把字节写歪，按 UTF-8 解码的父进程直接炸在读线程上。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Write the signal for one conduct node')
    parser.add_argument('cycle', help='Cycle identifier (e.g. release-prep)')
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
