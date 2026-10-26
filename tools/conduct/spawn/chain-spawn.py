#!/usr/bin/env python3
"""Start one session from a prompt file -- the lever a skill-run chain uses to hand on the baton.

    python chain-spawn.py --prompt-file <path> [--cycle-id <id>] [--workspace <dir>] [--dry-run]

The pipeline (`next --spawn` / `dispatch <cycle>`) decides WHAT the next session works on before it
reaches the spawner. This file is the other caller shape: a chain a skill runs on itself --
text-repair rounds, say -- where the prompt file already carries the whole handoff (its first line is
the next invocation line).

It is a CLI over the SAME door: the session is started by the pipeline's `spawn_session`
(`pipeline/writer.py`), the one place that starts sessions, so what that door carries comes
with this call too -- the session note that makes the conversation visible to `session show/list`, the
marker's write/clear policy (the spawner on the other end writes and clears it), and the spawner's own
answer to "which one" (`spawn/host.py`'s `default_spawner`, honoring `DSH_CONDUCT_SPAWNER`; a line's
`spawner` policy is read by `resolve_spawner` inside that door when the caller names a cycle this
project records).

**The dispatch gate is asked here, before anything starts.** A plugin may declare `block` at the
`dispatch` timing -- the question "may a session open now?" -- and this lever opens a real session, so
it is one more mouth the gate has to cover: the gate is asked with the workspace as the project and
the line's own record when `--cycle-id` names one this project knows (the record is empty otherwise:
the chain's name need not be a line, and a gate over a project-level setting is answerable either
way). Any reason the gate hands back refuses the start with one stderr line and a non-zero exit, and
no session -- the same answer `next` and `dispatch` give. `--dry-run` asks too: what it prints is the
session that *would* open, and a gate about to say no is part of that answer.

One guarantee does NOT come with it, and stating it beats assuming it: the spawn claim. A claim is
taken only for a prompt that sits in the launch directory it belongs to
(`meta/launch/<cycle>/<node_id>/prompt.txt`), and a skill-run chain keeps its prompt next to itself
(`.pf/repair/chain/…`), so this path takes no claim -- two concurrent handoffs of the same prompt file
have no mutual exclusion. That gap is open: the door refuses a double dispatch only where the pipeline
placed the prompt itself.

Exit codes
----------
0   spawned (with --dry-run: printed the argv that would have run)
1   nothing started: the dispatch gate said not now, or the spawner refused or died; its words are on
    stderr, the marker -- if the spawner got far enough to write one -- is on disk
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONDUCT = HERE.parent                      # the conduct tool directory

# Bootstrap: the loader is fetched by path too, so every file has to carry this paragraph itself
# (see `shared/loader.py`).
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


def _start(workspace: Path, cycle_id: str, prompt_file: Path, dry_run: bool) -> str:
    """Start the session: the pipeline's own door, and nothing else.

    A name rather than an inline call so that a caller can stand in front of it without patching
    `subprocess` process-wide: what a chain hands on is the workspace, the cycle and the prompt, and
    which script the door ends up running is answered below it, by the module that owns that argv.
    """
    pipeline = _loaded(CONDUCT / 'conduct_pipeline.py', 'conduct_pipeline')
    return pipeline.spawn_session(workspace, cycle_id, str(prompt_file), dry_run=dry_run)


def _dispatch_gate(workspace: Path, cycle_id: str) -> list:
    """The dispatch gate's answer for this start: one reason string per plugin saying "not now".

    The gate lives at the moment a session opens (a `block` plugin at the `dispatch` timing), and
    this file opens sessions, so it is asked here -- with the workspace as the project and the line's
    own record when this project records it. `--cycle-id` names the chain's degradation marker and
    need not be a line at all: with no such record the gate is asked over the project anyway (an
    empty record stated as such), so a gate keyed off a project- or machine-level knob still gets its
    question. An empty list is a green light.
    """
    pipeline = _loaded(CONDUCT / 'conduct_pipeline.py', 'conduct_pipeline')
    cycle = pipeline.find_cycle(pipeline.load_state(workspace), cycle_id) or {}
    return pipeline.gates('dispatch', pipeline.Context(workspace, cycle=cycle))


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(
        prog='chain-spawn.py',
        description='Start a new session from a prompt file: through the pipeline\'s one door; the '
                    'chain hands on the baton here.')
    parser.add_argument('--prompt-file', required=True,
                        help='the full prompt for the next baton; its first line is the next '
                             'invocation line')
    parser.add_argument('--cycle-id', default=None,
                        help='the name the degradation marker uses (default: the prompt file stem)')
    parser.add_argument('--workspace', default='.',
                        help='working directory of the new session (default: the current '
                             'directory)')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the argv that would be used and start no session')
    args = parser.parse_args(argv)

    prompt_file = Path(args.prompt_file).resolve()
    workspace = Path(args.workspace).resolve()
    cycle_id = args.cycle_id or prompt_file.stem
    try:
        # The gate first: "may a session open now?" is answered before anything is started, and a
        # refusal leaves the prompt, the marker and the line exactly as they were.
        held = _dispatch_gate(workspace, cycle_id)
        if held:
            print('not started: the dispatch gate says not now -- ' + '; '.join(held),
                  file=sys.stderr)
            return 1
        # The pipeline's one door: a session starts at exactly one mouth (`writer.spawn_session`).
        # Its last tier answers which spawner (the host counting itself as something the entry
        # worked out), and the session note along with the marker's write and clear all live inside
        # that door; a claim covers only a prompt sitting in the launch directory, and this chain
        # gets none (see the module docstring).
        output = _start(workspace, cycle_id, prompt_file, args.dry_run)
    except (OSError, RuntimeError, ValueError) as error:
        marker = _loaded(HERE / 'marker.py', 'pf_spawn_marker').marker_path(workspace, cycle_id)
        print(f'could not start it: {error}\n'
              f'the prompt is still at {prompt_file}; if the spawner already left a degradation '
              f'marker, it is at {marker}.',
              file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == '__main__':
    sys.exit(main())