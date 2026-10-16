#!/usr/bin/env python3
"""Which host this shell runs under, which spawner that means, and whether a process is still running.

Two hosts, one pipeline. `dsh-spawn.ps1` opens a DSH session over the DSH web API. Under ZCode that
session would never reach the GUI -- the task list there is a projection seeded once inside the agent
host, and a local workspace has no re-read path -- so the ZCode half queues the node into the desktop's
own cron scheduler instead (see `zcode-dispatch.py`). What the pipeline touches is the same either way:
one argv shape (built in `conduct_spawn.py`), the same `spawned:` last line, and the same failure
marker.

This file sits next to the two scripts it names, and it is the one place "which host am I" is asked.
Its callers are the entry module -- once, for the value the seam hands `writer.spawn_session` -- and
`chain-spawn.py`, which wants that answer alone and has no ledger to load.

`pid_alive` is here for the same reason: it is the same question about a process instead of about the
host ("is that thing still there"), asked of the same OS API set, and it carries the same
`os.name` branch. The two share one set of ctypes declarations, because a handle whose prototype is
left implicit comes back as a 32-bit `int` on 64-bit Windows and cannot be handed back.

Loaded by PATH, never by name, like every other file in this directory: the callers are themselves
loaded by path, and a by-name import would need `tools/` on `sys.path`.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: The two shipped spawners, keyed by the word the pipeline's own policies use for them.
SPAWN_SCRIPTS = {'dsh': HERE / 'dsh-spawn.ps1', 'zcode': HERE / 'zcode-spawn.ps1'}


class _PROCESSENTRY32W(ctypes.Structure):
    """One row of a Toolhelp process snapshot (`Process32FirstW` / `Process32NextW`)."""

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


_KERNEL32 = None


def _kernel32():
    """kernel32, with the prototypes this file's asks need declared once.

    A handle whose prototype is left implicit is truncated to 32 bits on 64-bit Windows, and a
    truncated handle handed back to the API is a wrong answer. Both the snapshot walk and the
    liveness check go through here, so the declarations exist once.
    """
    global _KERNEL32
    if _KERNEL32 is None:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel.OpenProcess.restype = ctypes.c_void_p        # a handle, or NULL
        kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        _KERNEL32 = kernel
    return _KERNEL32


def spawn_path(which=None) -> Path:
    """A spawner as a path: the bare words `dsh` / `zcode`, or any path a caller names outright."""
    if which is None:
        return SPAWN_SCRIPTS['dsh']
    text = str(which).strip()
    if text.lower() in SPAWN_SCRIPTS:
        return SPAWN_SCRIPTS[text.lower()]
    return Path(text).expanduser()


def under_zcode() -> bool:
    """Is a `ZCode.exe` process an ancestor of this one?

    DSH answers this for itself: it injects `DSH_SESSION_ID` into every shell it runs, so its own
    case needs no probe. ZCode injects nothing into the Bash tool (`ZCODE_SESSION_ID` is set for
    hooks, slash-command expansion and MCP servers only), so the process tree is the one dependable
    signal left: a tool call inside ZCode hangs off the agent host, and that host is `ZCode.exe`.
    """
    if os.name != 'nt':
        return False
    try:
        kernel32 = _kernel32()
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


def default_spawner() -> Path:
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
    if under_zcode():
        return SPAWN_SCRIPTS['zcode']
    return SPAWN_SCRIPTS['dsh']


def pid_alive(pid: int) -> bool:
    """Is that process running right now? -- the one question a spawn claim's pid can answer.

    A stale claim was so far judged only by its mtime, and a claim whose writer is STILL dispatching
    (the spawner's own ceiling is 300s) is not stale at any age: taking it over boots a second
    session onto the node. So the pid is asked first and the age rule only judges a dead writer. On
    Windows the ask is `OpenProcess` + `GetExitCodeProcess` (exit code STILL_ACTIVE = alive; a
    handle that will not open for anything but access-denied belongs to a process that exists); on
    POSIX it is `kill(pid, 0)`. A recycled pid can still refuse a dispatch by mistake -- that is a
    delete-the-claim-by-hand case, and the refusal says so.
    """
    if os.name == 'nt':
        ERROR_ACCESS_DENIED = 5
        STILL_ACTIVE = 259
        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                     # exists, but it is not ours to signal
    except OSError:
        return False
    return True