#!/usr/bin/env python3
"""The project's one cross-process ledger lock: `<project>/.pf/meta/ledger.lock`.

The records this tree keeps are whole-file read-modify-write -- `state.json`, `ideas.json`,
`instructions.json`, a node's session note, and the records an installed plugin keeps of its own --
so two processes walking that sequence at once means the later write silently erases the earlier one.
Every mutating command holds this one lock across its read -> write, which is what makes conduct,
dag-manage, a note's own writer and every plugin tool that keeps a record here exclude each other.

    ledger_lock(ledger_dir, *, timeout=LEDGER_LOCK_TIMEOUT, gate=None)

`ledger_dir` is `<project>/.pf`; the lock file sits in `meta/`, which `.pf/.gitignore` keeps out of
both the project's history and the ledger's own, so losing it only loses the mutual exclusion and
never wedges anybody. `gate` is the caller's own "this ledger may be written at all" check (the
init markers); it runs once, before the outermost acquire.

The lock is reentrant per process and keyed by the lock file: a command that already holds it -- a
dag command writing a node's note inside its own read-modify-write -- may acquire it again, and only
the outermost acquire touches the file. The record of who holds what belongs to the PROCESS, not to
this module object: these files are loaded by path, so a caller that loads this one under a second
name would otherwise get a second record, and its acquire would sit out the whole timeout against the
first copy's file handle. The record therefore lives in `sys.modules` under its own name, which is
one per process by construction.

A caller that cannot get the lock within `timeout` gets a `TimeoutError`: wedging silently is the
one failure this must not have. Whether that ends the command with a message or gives up an optional
write is the caller's decision, so nothing here prints or exits. On a platform with neither `msvcrt`
nor `fcntl` the exclusion degrades to nothing rather than failing every command.
"""

import contextlib
import os
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:   # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:   # POSIX
    msvcrt = None

#: The give-up ceiling, not the norm: a writer holds the lock for milliseconds.
LEDGER_LOCK_TIMEOUT = 10.0

#: Where this process keeps how deep it is in each lock file. A name nothing imports: it is only the
#: carrier for the one dict per process that every copy of this file has to see alike.
_RECORD = 'pf_shared_ledger_lock_depth'


def _depth() -> dict:
    """This process's depth per lock file -- keyed by the file, so a second project's lock is taken."""
    record = sys.modules.get(_RECORD)
    if not isinstance(record, dict):
        record = {}
        sys.modules[_RECORD] = record
    return record


def _lock_path(ledger_dir) -> Path:
    return Path(ledger_dir) / 'meta' / 'ledger.lock'


@contextlib.contextmanager
def ledger_lock(ledger_dir, *, timeout: float = LEDGER_LOCK_TIMEOUT, gate=None):
    """Hold one project's ledger lock for the duration of a read-modify-write. See the docstring."""
    path = _lock_path(ledger_dir)
    key = str(Path(path).resolve())
    depth = _depth()
    if depth.get(key):
        depth[key] += 1
        try:
            yield
        finally:
            depth[key] -= 1
        return
    if gate is not None:
        gate()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, 'a+')
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if os.name == 'nt' and msvcrt is not None:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f'the ledger lock {path} stayed busy for {timeout:g}s; another process is '
                        f'mid-write. Nothing was changed.') from None
                time.sleep(0.05)
        depth[key] = 1
        try:
            yield
        finally:
            depth[key] = 0
            try:
                if os.name == 'nt' and msvcrt is not None:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        handle.close()