#!/usr/bin/env python3
"""The one place a session is started: build the spawner's argv, run it, report it honestly.

Two hosts ship here, and this module is what they have in common: `dsh-spawn.ps1` opens a DSH
session over the DSH web API, `zcode-spawn.ps1` queues the node into ZCode's own cron scheduler.
Which of the two it is arrives as an argument: `spawn/host.py`'s `default_spawner()` answers it for
a host that names no spawner, and `writer.resolve_spawner` is the pipeline's own answer to the same
question.

One caller reaches this file: `pipeline/writer.spawn_session`, by way of `dispatch
<cycle>` and the one-shot `next --after <node_id> --spawn`. `chain-spawn.py` -- the CLI a skill-run
chain uses to hand on the baton -- is a caller of THAT door rather than a second one, so every
session in this tree starts through the same call.

What it wants is only the call: the argv, the child process, the ceiling, and the way a failure is
turned into one readable line. A failed spawn breaks the chain: the spawner leaves the broken-chain
marker under the project's ledger -- `spawn/marker.py` is the one definition of that file, its landing
place included, and this module hands the spawner that path -- and the caller exits non-zero.

Why the ceiling is 300 and not 180
----------------------------------
`dsh-spawn.ps1` retries every API call three times, backing off 2s then 4s, with a 30s per-request
timeout -- and it makes three calls (`workspace/create`, `session/create`, `session/prompt`). The
worst legal case is therefore about 3 x (30 + 2 + 30 + 4 + 30) = 288s. A 180s ceiling kills pwsh in
the middle of a retry that was about to succeed, and the failure marker is only written once the last
attempt has failed -- so the chain would break with nothing on disk to show for it.

Loaded by PATH, never by name, like every other file in this directory: the caller is itself loaded
by path, and a by-name import would hand the second load the first load's module.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

#: The ceiling for one whole spawn (see the module docstring). It is a parameter so a caller with a
#: reason of its own can move it.
DEFAULT_TIMEOUT = 300

#: Which host each spawner opens its session on: the words `cycle policy spawner` already uses, and
#: what a node's session note records as `host` (`tools/shared/session-note.py`).
HOSTS = {'dsh-spawn.ps1': 'dsh', 'zcode-spawn.ps1': 'zcode'}

#: The session id inside a spawner's last line, one pattern per contract shape: `spawned: session
#: <id> (…)` on DSH, `spawned: automation <id> (session <id>, …)` on ZCode -- which prints no
#: session at all when its own verification could not name one, and then there is nothing to note.
_SESSION_IN_LINE = (re.compile(r'^spawned:\s*session\s+(\S+)'),
                    re.compile(r'\(session\s+([^,)\s]+)'))

HERE = Path(__file__).resolve().parent

# Bootstrap: the loader is fetched by path too, so every file has to carry this paragraph itself
# (see `shared/loader.py`).
SHARED = HERE.parent.parent / 'shared'

_LOADER = None


def _loader():
    """`tools/shared/loader.py`: the one loader, for the modules this tree keeps outside `shared/`."""
    global _LOADER
    if _LOADER is None:
        spec = importlib.util.spec_from_file_location('pf_shared_loader', SHARED / 'loader.py')
        _LOADER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_LOADER)
    return _LOADER


def _marker():
    """`spawn/marker.py`: where a broken chain's marker lands, and the argv's `-MarkerPath` source.

    One copy per process (`register` + `reuse`), the same way every other file here is asked for.
    """
    return _loader().load_by_path('conduct_spawn_marker', HERE / 'marker.py',
                                  register=True, reuse=True)


def host_of(spawner) -> str | None:
    """Which host a spawner opens its session on, by the script's own name.

    None for anything that is not one of the two shipped spawners -- a caller's own script, a test's
    stand-in. A note then falls back to the session id's own shape (`tools/shared/session-note.py`),
    which is what tells the two hosts apart in any case: the name is the stronger evidence, not the
    only one.
    """
    return HOSTS.get(Path(spawner).name)


def session_id_of(line: str) -> str | None:
    """The session id in a spawner's last line, or None when that line carries none.

    The last line is the contract both spawners share, and it is the only place the id ever is:
    `session/create`'s answer goes to the spawner's stdout and nowhere else.
    """
    for pattern in _SESSION_IN_LINE:
        found = pattern.search(line or '')
        if found:
            return found.group(1)
    return None


def argv_for(spawner, cycle_id: str, prompt_file: str | None, project, *, marker_path=None,
             python_exe=None) -> list:
    """The one argv the caller hands to a spawner -- the same one for both hosts.

    `-CycleId` / `-PromptFile` / `-WorkspacePath` are the three named parameters both `dsh-spawn.ps1`
    and `zcode-spawn.ps1` declare; anything a single host needs on top of them (ZCode's `-At` /
    `-Mode` / `-Model`, DSH's `-WebUrl` / `-Token`) travels in the environment instead, so this one
    argv stays true for either script.

    `-PromptFile` goes in when there is one and is left out when there is not: the spawner declares
    it Mandatory, so leaving it out is how a caller bug surfaces as the spawner's own loud refusal --
    passing the string "None" instead would open a session with nothing to load.

    `-MarkerPath` is the broken-chain marker's full path, from `spawn/marker.py` (`marker_path`): the
    spawner writes its failures there and clears it on success, and it never assembles that path
    itself -- `.pf` has one definition and the dispatch side must follow it, not spell it again. That
    path is all that is handed down: the record's shape has one home (`marker.py`), and the shell
    writes it in PowerShell as an exception that cannot be shared (a spawner may run where there is no
    python), with a test pinning the two implementations equal.
    `-PythonExe` is the interpreter already running the caller, for a host that needs one (ZCode's
    dispatcher); the marker write does not go through it.
    """
    argv = ['pwsh', '-NoProfile', '-File', str(spawner), '-CycleId', str(cycle_id)]
    if prompt_file:
        argv += ['-PromptFile', str(prompt_file)]
    argv += ['-WorkspacePath', str(project)]
    argv += ['-MarkerPath', str(marker_path or _marker().marker_path(project, cycle_id))]
    if python_exe:
        argv += ['-PythonExe', str(python_exe)]
    return argv


def spawn_session(project, cycle_id: str, prompt_file: str | None = None, *,
                  spawner, timeout: float = DEFAULT_TIMEOUT, dry_run: bool = False) -> str:
    """Start one session and return the spawner's last line (`spawned: session <id> (...)`, or
    `spawned: automation <id> (...)` on ZCode).

    `spawner` is the path of `dsh-spawn.ps1` or `zcode-spawn.ps1` (or a stand-in, for a drill or a
    test): it is required rather than defaulted so that "which spawner" is answered by the caller that
    already knows. It is also the name a failure is reported by -- whichever script this call was
    about is the one the human has to go and look at.

    A failure is one `RuntimeError` carrying the spawner's own words (its stderr, or its stdout, or
    the exit code and the ceiling it ran out of). Nothing here writes or clears the failure marker:
    that is the spawner's business (through `spawn/marker.py`), and the caller's, not this module's.
    """
    argv = argv_for(spawner, cycle_id, prompt_file, project, python_exe=sys.executable or None)
    if dry_run:
        # The drill path: the argv that WOULD have run is the whole point of the line, and nothing
        # is started (so the prompt file stays where a human can retry it by hand).
        return 'dry-run: ' + ' '.join(argv)
    name = Path(spawner).name or str(spawner)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f'{name} did not finish within {timeout:g}s for cycle '
                           f'{cycle_id}') from e
    except OSError as e:
        raise RuntimeError(f'could not run {name}: {e}') from e
    if proc.returncode != 0:
        raise RuntimeError(f'{name} failed ({proc.returncode}): {proc.stderr or proc.stdout}')
    out = (proc.stdout or '').strip()
    return out.splitlines()[-1] if out else ''