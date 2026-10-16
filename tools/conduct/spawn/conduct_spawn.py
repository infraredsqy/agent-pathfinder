#!/usr/bin/env python3
"""The one place a session is started: build `dsh-spawn.ps1`'s argv, run it, report it honestly.

One caller reaches this file: the pipeline (`conduct_pipeline_impl/writer.spawn_session`), by way of
`dispatch <cycle>` and the one-shot `next --after <node_id> --spawn`.

What it wants is only the call: the argv, the child process, the ceiling, and the way a failure is
turned into one readable line. A failed spawn breaks the chain -- `dsh-spawn.ps1` writes
`.pf/meta/CONDUCT-CALLBACK-FAILED-<cycle>.json` and the caller exits non-zero.

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

import re
import subprocess
from pathlib import Path

#: The ceiling for one whole spawn (see the module docstring). It is a parameter so a caller with a
#: reason of its own can move it.
DEFAULT_TIMEOUT = 300

#: The spawner, as its name appears in the errors a failure has to be readable through.
SPAWNER = 'dsh-spawn.ps1'

#: Which host each spawner opens its session on: the words `cycle policy spawner` already uses, and
#: what a node's session note records as `host` (`tools/session-note.py`).
HOSTS = {'dsh-spawn.ps1': 'dsh', 'zcode-spawn.ps1': 'zcode'}

#: The session id inside a spawner's last line, one pattern per contract shape: `spawned: session
#: <id> (…)` on DSH, `spawned: automation <id> (session <id>, …)` on ZCode -- which prints no
#: session at all when its own verification could not name one, and then there is nothing to note.
_SESSION_IN_LINE = (re.compile(r'^spawned:\s*session\s+(\S+)'),
                    re.compile(r'\(session\s+([^,)\s]+)'))


def host_of(spawner) -> str | None:
    """Which host a spawner opens its session on, by the script's own name.

    None for anything that is not one of the two shipped spawners -- a caller's own script, a test's
    stand-in. A note then falls back to the session id's own shape (`tools/session-note.py`), which
    is what tells the two hosts apart in any case: the name is the stronger evidence, not the only
    one.
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


def argv_for(spawner, cycle_id: str, prompt_file: str | None, project) -> list:
    """The one argv the caller hands to `dsh-spawn.ps1`.

    `-PromptFile` goes in when there is one and is left out when there is not: the spawner declares
    it Mandatory, so leaving it out is how a caller bug surfaces as the spawner's own loud refusal --
    passing the string "None" instead would open a session with nothing to load.
    """
    argv = ['pwsh', '-NoProfile', '-File', str(spawner), '-CycleId', str(cycle_id)]
    if prompt_file:
        argv += ['-PromptFile', str(prompt_file)]
    argv += ['-WorkspacePath', str(project)]
    return argv


def spawn_session(project, cycle_id: str, prompt_file: str | None = None, *,
                  spawner, timeout: float = DEFAULT_TIMEOUT, dry_run: bool = False) -> str:
    """Start one session and return the spawner's last line (`spawned: session <id> (...)`).

    `spawner` is the path of `dsh-spawn.ps1` (or a stand-in, for a drill or a test): it is required
    rather than defaulted so that "which spawner" is answered by the caller that already knows.

    A failure is one `RuntimeError` carrying the spawner's own words (its stderr, or its stdout, or
    the exit code and the ceiling it ran out of). Nothing here writes or clears the failure marker:
    that is `dsh-spawn.ps1`'s business, and the caller's, not this module's.
    """
    argv = argv_for(spawner, cycle_id, prompt_file, project)
    if dry_run:
        # The drill path: the argv that WOULD have run is the whole point of the line, and nothing
        # is started (so the prompt file stays where a human can retry it by hand).
        return 'dry-run: ' + ' '.join(argv)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f'{SPAWNER} did not finish within {timeout:g}s for cycle '
                           f'{cycle_id}') from e
    except OSError as e:
        raise RuntimeError(f'could not run {SPAWNER}: {e}') from e
    if proc.returncode != 0:
        raise RuntimeError(f'{SPAWNER} failed ({proc.returncode}): {proc.stderr or proc.stdout}')
    out = (proc.stdout or '').strip()
    return out.splitlines()[-1] if out else ''
