#!/usr/bin/env python3
"""A node's session note: which conversation produced it, and which ones ran it.

Session ids are machine-local -- a DSH session lives under `~/.dsh/sessions/` on the machine that
opened it, a ZCode one in that host's own store -- so an id written into the shared record is a dead
value everywhere else. Notes live under `.pf/meta/`, which `.pf/.gitignore` keeps out of both the
project's history and the ledger's:

    .pf/meta/sessions/<node uuid>.json

    {"v": 1,
     "discussion": [{"session": "...", "host": "dsh", "at": "..."}],
     "run":        [{"session": "...", "host": "dsh", "at": "..."}]}

Two named lists rather than one list with a `role` field: a reader that wants "the conversation to
open" cannot pick the wrong one by forgetting a filter. One session can be a discussion for one node
and a run for another, so the role belongs to the entry, not to the session.

`host` is who opened it (`dsh` / `zcode`, the pipeline's own words for a spawner); `source:
"ledger"` marks a value imported from the `dsh_session` field an older build wrote into `ideas.json`.

Writers are `dag-manage.py add` (the discussion the node was routed in) and the pipeline's
`writer.spawn_session` (every run, at spawn time); readers are `pf session show|list` and
`pf dag info`. No single tool owns this file and there is no package to import it from: it lives in
`tools/shared/`, and its callers load it by path through `loader.py`.

Every write goes through the project's ledger lock (`.pf/meta/ledger.lock`, the one lock every
writer of a `.pf/` record holds) and the tree's atomic JSON write, so two writers noting two
sessions on one node cannot lose one another.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

# `tools/shared/` is the shared layer, and it has no package to import from: every file in it is
# loaded by path through `loader.py`. These two functions are that bootstrap, identical in every file
# that needs it -- a path load, never a name import, because nothing puts `tools/` on `sys.path`.
SHARED = Path(__file__).resolve().parent

_LOADER = None


def _loader():
    """`tools/shared/loader.py`: the one loader, for the modules this tree keeps outside `shared/`."""
    global _LOADER
    if _LOADER is None:
        spec = importlib.util.spec_from_file_location('pf_shared_loader', SHARED / 'loader.py')
        _LOADER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_LOADER)
    return _LOADER


def _shared(name, filename):
    """One file out of `tools/shared/`, the same module for every caller in this process.

    Loaded for use, not copied: the ledger lock's reentrancy record is one per process, and a second
    copy of that module would give two of them.
    """
    return _loader().load_by_path(name, SHARED / filename, register=True, reuse=True)

#: The two keys a note is made of, in the order a human reads them.
ROLES = ('discussion', 'run')
#: Bumped only when the shape changes incompatibly. A reader that does not know this version treats
#: the file as absent rather than guessing at it.
VERSION = 1
#: The hosts a session id can belong to, as the pipeline spells them.
DSH = 'dsh'
ZCODE = 'zcode'
#: ZCode's session ids. `spawn/zcode-spawn.ps1` reads its own result line with exactly this prefix
#: (`'^session:\s*sess_'`), so a `sess_…` value is a ZCode session and nothing else.
ZCODE_ID_PREFIX = 'sess_'
#: The `at` a caller leaves out: "this is happening now". A caller that does NOT know when a session
#: was opened (an import from an older record) passes `at=None` instead, and the note keeps `null`:
#: a made-up timestamp is worse than an absent one.
NOW = object()


def _now() -> str:
    """The account's timestamp shape, seconds and a `Z`."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def host_of_session(session: str | None) -> str:
    """Which host opened this id, from the id's shape alone.

    The fallback for a caller that has no host of its own to state (an import from the old field).
    It never overrides a host a writer did state.
    """
    return ZCODE if str(session or '').startswith(ZCODE_ID_PREFIX) else DSH


def note_path(project, node_id) -> Path:
    """Where one node's note lives: `<project>/.pf/meta/sessions/<node id>.json`."""
    return Path(project) / '.pf' / 'meta' / 'sessions' / f'{node_id}.json'


def _empty() -> dict:
    return {'v': VERSION, **{role: [] for role in ROLES}}


def read(project, node_id) -> dict:
    """One node's note, always in the canonical shape: both lists, `[]` when nothing is known.

    A missing file, an unreadable one and one written by a version this reader does not know all
    read the same way -- empty. A note is machine-local plumbing that any reader can rebuild from the
    sessions themselves, so it must never be the reason a tool dies; what it must never do is pretend
    a value it cannot understand is a value it understands.
    """
    path = note_path(project, node_id)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return _empty()
    if not isinstance(raw, dict) or raw.get('v') != VERSION:
        return _empty()
    note = _empty()
    for role in ROLES:
        entries = raw.get(role)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            clean = _clean_entry(entry)
            if clean:
                note[role].append(clean)
    return note


def _clean_entry(entry) -> dict | None:
    """One entry as this reader understands it, or None when it says nothing usable."""
    if not isinstance(entry, dict):
        return None
    session = entry.get('session')
    if not isinstance(session, str) or not session:
        return None
    clean = {'session': session,
             'host': entry.get('host') if isinstance(entry.get('host'), str) else host_of_session(session),
             'at': entry.get('at') if isinstance(entry.get('at'), str) else None}
    if isinstance(entry.get('source'), str):
        clean['source'] = entry['source']
    return clean


def notes(project) -> dict:
    """Every node's note, keyed by node id. The whole directory, for a listing reader."""
    directory = Path(project) / '.pf' / 'meta' / 'sessions'
    out = {}
    try:
        names = sorted(p.name for p in directory.iterdir() if p.suffix == '.json')
    except OSError:
        return out
    for name in names:
        note = read(project, name[:-len('.json')])
        if note['discussion'] or note['run']:
            out[name[:-len('.json')]] = note
    return out


def sessions_of(note: dict, role: str) -> list:
    """The session ids one role of a note holds, in the order they were written."""
    return [entry['session'] for entry in (note or {}).get(role) or []]


def append(project, node_id, role: str, session: str, host: str | None = None,
           at=NOW, source: str | None = None) -> bool:
    """Note one session under one role. True when the file changed, False when it was already there.

    Appending the same (role, session) twice is a no-op: `next --spawn` retried after a partial
    failure must not leave the same run in the note twice. The role is checked loudly here -- a
    caller that invents a third role is a bug, and a silently accepted one would be a note no reader
    ever looks at.

    `at` left out means now (a writer noting what just happened); `at=None` means "not known" and
    stores `null` (an import from a record that no longer carries the moment).

    The read and the write are one act, and this function takes the project's ledger lock for it:
    two writers noting two sessions on one node would otherwise read the same note and the later
    write would erase the earlier entry. The lock is the one every writer of a `.pf/` record holds,
    and the write is the tree's atomic JSON write (temp file in the same directory, fsync, a replace
    that waits out a reader holding the file open): readers of a note are other processes, and a
    half-written JSON is exactly the failure the tolerant reader above hides. A caller that already
    holds the lock -- a dag command writing a node's note inside its own read-modify-write -- may
    call this; the lock is reentrant per process.
    """
    if role not in ROLES:
        raise ValueError(f'session note role must be one of {ROLES}, got {role!r}')
    if not session:
        raise ValueError('a session note entry needs a session id')
    lock = _shared('pf_shared_ledgerlock', 'ledgerlock.py')
    with lock.ledger_lock(Path(project) / '.pf'):
        note = read(project, node_id)
        if session in sessions_of(note, role):
            return False
        entry = {'session': session, 'host': host or host_of_session(session),
                 'at': _now() if at is NOW else at}
        if source:
            entry['source'] = source
        note[role].append(entry)
        body = {'v': VERSION, **{r: note[r] for r in ROLES}}
        _shared('pf_shared_jsonio', 'jsonio.py').atomic_write_json(note_path(project, node_id), body)
    return True


def drop(project, node_id) -> bool:
    """Forget one node's note (the node is gone). True when a file was there to remove."""
    path = note_path(project, node_id)
    try:
        path.unlink()
        return True
    except OSError:
        return False