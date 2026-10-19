#!/usr/bin/env python3
"""The one source for the broken-chain marker: where it lands, what it looks like, how it is
written, and who writes it under which conditions.

One failure means this chain will not move on by itself again. **The marker exists = the chain is
broken right now** -- it is the one door for a human to come in and take over, so it has to be
obvious: a write that does not reach the disk and a silent degradation are both shapes of this rule
failing, and that is exactly why this module exists. The call sites come down to two: the python
side (the beat in `handoff.py` where a note cannot be written) uses the three functions below
directly; the layer both spawners share (`spawn/spawn-common.ps1`) writes the same shape itself --
that exception and its reason are written in the `how` item.

    lands at  marker_path(project, cycle) -- `<project>/.pf/meta/CONDUCT-CALLBACK-FAILED-<cycle>.json`
              one file per line: with parallel=2 two lines run side by side, and a success on this
              line clears only this line's marker, never wiping another line's broken-chain sign.
    shape     record(cycle, phase, reason, attempts=, workspace=, extra=) -- one shape, and every
              path that writes it grows the same one; the host's own facts (dsh's api_base, zcode's
              db) travel in the `extra` slot, so drift no longer lands on the fields. `detected_at`
              is this record's own moment, taking the microsecond shape from
              `tools/shared/clock.py` (`clock.now()`) -- the moment's shape has exactly one source.
    how       write(path, payload) -- `tools/shared/jsonio.py`'s atomic write: temp file +
              same-volume rename + a retry for "the reader has the target file open right now".
              Every `.pf/` record in the tree goes through this one primitive.
              **One exception**: the spawner layer (`spawn/spawn-common.ps1`) writes the same shape
              in pure PowerShell. The reason is physical -- the machine a spawner runs on, at that
              moment, may have no python, and PowerShell cannot import this module, while this very
              write is the only door left to a human once the chain has broken. The two
              implementations' shapes are pinned by `conduct/tests/cases_spawn_shell.py`'s
              `TestTheSpawnerShell` (the same key set, the same sentence, the same moment shape).
    who writes  the failed beat itself: the spawner writes at its own failure point (that
              `spawn/spawn-common.ps1` layer writes its own disk, following this shape), and the beat
              where a note cannot be written is written by `handoff.py`.
              **One failure leaves one record**: the first one is the root cause, and later ones do
              not overwrite it.
    who clears  the spawner's success path (`spawn/spawn-common.ps1`): deleting a file has no shape
              to speak of, and a success beat must not fail because it cannot clear (a clear that
              fails may not block a successful dispatch), so clearing does not come through here.
    who deletes  the human. Once they have taken over and checked that nothing is wrong, they delete
              this file themselves.

The name `.pf` and the `meta/` layer are borrowed from `tools/shared/ledger.py` (`meta_dir_for`):
the dispatch side stops assembling a second copy of its own, and when the name changes the marker
moves along with the record.

The command line is the face for a human troubleshooting or drilling: `python marker.py write
--path ...` writes one marker by hand to this shape. The spawner no longer calls it (see the
exception above); the python side uses `marker_path` / `record` / `write` directly.

Loaded by PATH, never by name, like every other file in this directory: the callers are themselves
loaded by path, and a by-name import would hand the second load the first load's modules.
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

#: The one spelling of the marker file name. One file per line, and `{cycle}` is the cycle id the
#: spawner hands over.
MARKER_NAME = 'CONDUCT-CALLBACK-FAILED-{cycle}.json'

#: The one sentence the marker writes about itself: where the chain broke, how to take over, when it
#: may be deleted. Every path that writes it uses this one -- one wording for one thing.
DEFAULT_NOTE = ('The chain broke here: this step could not start the next one, and the pipeline '
                'will not move on by itself. To take over by hand use `pf next --after <node_id>` '
                '(the closing step) or `pf dispatch <cycle>` (the dispatch step); once you have '
                'checked that nothing is wrong, delete this file.')

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
    """`tools/shared/ledger.py`: `.pf/` and the `meta/` under it -- one source."""
    return _loader().load_shared('ledger')


def _clock():
    """`tools/shared/clock.py`: the moment in a record -- its shape and "now" both come from this
    one source."""
    return _loader().load_shared('clock')


def marker_path(project, cycle: str) -> Path:
    """Where this line's degradation marker lands --
    `<project>/.pf/meta/CONDUCT-CALLBACK-FAILED-<cycle>.json`.

    This is the tree's one place that spells this path: `argv_for` hands its result to the spawner
    as `-MarkerPath`, and `chain-spawn.py` takes the "where is the marker" phrase of its error from
    it.
    """
    return _ledger().meta_dir_for(project) / MARKER_NAME.format(cycle=cycle)


def record(cycle: str, phase: str, reason: str, *, attempts: int = 0, workspace='',
           extra=None) -> dict:
    """The record the marker leaves on disk, grown in one place.

    `phase` is which step broke (the spawner's own step name), `reason` is that beat's own words,
    `attempts` is the number of attempts that step counted (0 when it counted none), `workspace` is
    this line's project root, and `extra` is the host's own facts (dsh's api_base, zcode's db): they
    are leads for someone troubleshooting, so they get a slot of their own rather than letting the
    two spawners grow a field set each. `note` is this shape's fixed sentence (`DEFAULT_NOTE`).
    """
    return {
        'cycle_id': str(cycle),
        'phase': str(phase),
        'attempts': int(attempts),
        'reason': str(reason),
        'workspace': str(workspace),
        'extra': dict(extra or {}),
        'detected_at': _clock().now(),
        'note': DEFAULT_NOTE,
    }


def write(path, payload: dict) -> Path:
    """Put one marker on disk: `jsonio`'s atomic write (temp file + same-volume rename + retry).

    A bare `os.replace` on Windows runs into "the reader has the target file open right now" and a
    PermissionError, and this path is exactly the write that must not be lost -- so it goes through
    the same primitive as every other `.pf/` record in the tree. A failed write lets the exception
    out (`PermissionError` is one kind of `OSError`): whether this beat swallows it, and what it
    says, is the caller's judgement.
    """
    path = Path(path)
    _loader().load_shared('jsonio').atomic_write_json(path, payload)
    return path


def _extra_pairs(values) -> dict:
    """Parse the `KEY=VALUE` run (how the spawner hands over the host's facts) into one object."""
    pairs = {}
    for item in values or []:
        key, _, value = str(item).partition('=')
        if key.strip():
            pairs[key.strip()] = value
    return pairs


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(
        prog='marker.py',
        description='Write one broken-chain marker: the shape, the landing place and the retry all '
                    'live here; the spawner only hands over the facts.')
    verbs = parser.add_subparsers(dest='verb', required=True)
    writer = verbs.add_parser(
        'write', help='write one marker (the caller decides about an existing one)')
    writer.add_argument('--path', required=True, help='the full marker path (from marker_path)')
    writer.add_argument('--cycle', required=True, help='cycle id (recorded as cycle_id)')
    writer.add_argument('--phase', required=True, help='which step broke')
    writer.add_argument('--reason', required=True, help='the words of that beat')
    writer.add_argument('--attempts', type=int, default=0,
                        help='the number of attempts that step counted (0 when it counted none)')
    writer.add_argument('--workspace', default=None,
                        help='project root (default: derived from the marker path)')
    writer.add_argument('--extra', action='append', default=[], metavar='KEY=VALUE',
                        help='facts of the host itself; may be given more than once')
    args = parser.parse_args(argv)

    # The marker's landing place fixes the project root (`<project>/.pf/meta/...`); its own copy is
    # used only when -WorkspacePath was given.
    workspace = args.workspace or Path(args.path).parent.parent.parent
    payload = record(args.cycle, args.phase, args.reason, attempts=args.attempts,
                     workspace=workspace, extra=_extra_pairs(args.extra))
    try:
        write(args.path, payload)
    except OSError as e:
        print(f'could not write the degradation marker ({args.path}): {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())