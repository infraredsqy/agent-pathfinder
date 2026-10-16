#!/usr/bin/env python3
"""Start one session from a prompt file -- the lever a skill-run chain uses to hand on the baton.

    python chain-spawn.py --prompt-file <path> [--cycle-id <id>] [--workspace <dir>] [--dry-run]

The pipeline (`next --spawn` / `dispatch <cycle>`) decides WHAT the next session works on before it
reaches the spawner. This file is the other caller shape: a chain a skill runs on itself --
text-repair rounds, say -- where the prompt file already carries the whole handoff (its first line is
the next invocation line).

It is a CLI over the SAME door: the session is started by the pipeline's `spawn_session`
(`conduct_pipeline_impl/writer.py`), the one place that starts sessions, so what that door carries comes
with this call too -- the session note that makes the conversation visible to `session show/list`, the
marker's write/clear policy (the spawner on the other end writes and clears it), and the spawner's own
answer to "which one" (`spawn/host.py`'s `default_spawner`, honoring `DSH_CONDUCT_SPAWNER`; a line's
`spawner` policy is read by `resolve_spawner` inside that door when the caller names a cycle this
project records).

One guarantee does NOT come with it, and stating it beats assuming it: the spawn claim. A claim is
taken only for a prompt that sits in the launch directory it belongs to
(`meta/launch/<cycle>/<node_id>/prompt.txt`), and a skill-run chain keeps its prompt next to itself
(`.pf/repair/chain/…`), so this path takes no claim -- two concurrent handoffs of the same prompt file
have no mutual exclusion. That gap is open: the door refuses a double dispatch only where the pipeline
placed the prompt itself.

Exit codes
----------
0   spawned (with --dry-run: printed the argv that would have run)
1   nothing started: the spawner refused or died; its words are on stderr, the marker -- if the
    spawner got far enough to write one -- is on disk
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONDUCT = HERE.parent                      # the conduct tool directory

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


def _loaded(path: Path, name: str):
    """A sibling loaded by path -- how every file in this directory calls its neighbours."""
    return _loader().load_by_path(name, path)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(
        prog='chain-spawn.py',
        description='按 prompt 文件开一个新 session：走管线那一道门，链在这里交棒。')
    parser.add_argument('--prompt-file', required=True,
                        help='下一棒的 prompt 全文；第一行是下一棒的调用行')
    parser.add_argument('--cycle-id', default=None,
                        help='降级标记用的名字（缺省取 prompt 文件名的 stem）')
    parser.add_argument('--workspace', default='.',
                        help='新 session 的工作目录（缺省当前目录）')
    parser.add_argument('--dry-run', action='store_true',
                        help='只打印将要用到的 argv，不开会话')
    args = parser.parse_args(argv)

    prompt_file = Path(args.prompt_file).resolve()
    workspace = Path(args.workspace).resolve()
    cycle_id = args.cycle_id or prompt_file.stem
    pipeline = _loaded(CONDUCT / 'pipeline' / 'conduct_pipeline.py', 'conduct_pipeline')
    try:
        # 管线那一道门：开会的口只此一个（`writer.spawn_session`）。spawner 由它的末档回答
        # （入口算好的宿主自认），会话便条与标记的写、清都在那一道门里；claim 认领的只是落在
        # launch 目录里的 prompt，这条链拿不到它（见模块说明）。
        output = pipeline.spawn_session(workspace, cycle_id, str(prompt_file),
                                        dry_run=args.dry_run)
    except (OSError, RuntimeError, ValueError) as error:
        marker = _loaded(HERE / 'marker.py', 'pf_spawn_marker').marker_path(workspace, cycle_id)
        print(f'没开起来：{error}\n'
              f'prompt 还在 {prompt_file}；spawner 若已落降级标记，就在 {marker}。',
              file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == '__main__':
    sys.exit(main())