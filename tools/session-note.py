#!/usr/bin/env python3
"""A node's session note: which conversation produced it, and which ones ran it.

Session ids are machine-local: a DSH session lives under `~/.dsh/sessions/` on the machine that
opened it, a ZCode one inside that host's own store. So an id written on the account is a value that
is guaranteed dead on every other machine -- it cannot be opened there, and it cannot even be told
apart from "that session was deleted here". Ids therefore do not ride on the account; they live
under `.pf/meta/`, which `.pf/.gitignore` keeps out of both the project's history and the ledger's.
`meta/` is plumbing, and a note is plumbing.

    .pf/meta/sessions/<node uuid>.json

    {"v": 1,
     "discussion": [{"session": "...", "host": "dsh", "at": "..."}],
     "run":        [{"session": "...", "host": "dsh", "at": "..."}]}

Two NAMED lists, not one list carrying a `role` field: a reader that wants "the conversation to
open" must not be able to pick the discussion one by forgetting a filter. One session can honestly
be a discussion for one node and a run for another (a conversation that routes a batch can then be
handed a node), and the role is a property of the fact, not of the session.

`host` says who opened it -- the words the pipeline already uses for a spawner (`dsh` / `zcode`,
`conduct_pipeline.py`'s `SPAWN_SCRIPTS`). A ZCode session cannot be opened by a DSH panel, and its
ids have their own shape (`sess_…`, the exact prefix `spawn/zcode-spawn.ps1` matches its own session
line with), which is also how a value imported from the old field is labelled.

`source: "ledger"` marks a value imported from the `dsh_session` field an older build wrote into
`ideas.json`. It may name a session on the machine that wrote the ledger, not on this one.

Writers: `dag-manage.py add` (the discussion the node was routed in) and the pipeline's
`writer.spawn_session` (every run, at spawn time -- the id exists from that moment, and a running
node is exactly the node whose conversation one wants to open). Readers: `pf session show|list`,
`pf dag info`, and anything that jumps into a conversation. The three tools share no package, so
this file is loaded BY PATH by the ones that write it -- the convention every entry point in
`conduct/` already follows.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

#: The two keys a note is made of, in the order a human reads them.
ROLES = ('discussion', 'run')
#: Bumped only when the shape changes incompatibly. A reader that does not know the version treats
#: the file as absent rather than guessing at it -- a note is disposable plumbing, and guessing is
#: the one thing that would make it worse than absent.
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
    It never overrides a host a writer did state: the writer was there, this function was not.
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
    read the same way -- empty. A note is machine-local plumbing that any reader can rebuild from
    the sessions themselves, so it must never be the reason a tool dies; what it must never do is
    pretend a value it cannot understand is a value it understands.
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

    The write is atomic (temp file in the same directory, then `os.replace`): readers of a note are
    other processes, and a half-written JSON is exactly the failure this file's tolerant reader
    would then hide.
    """
    if role not in ROLES:
        raise ValueError(f'session note role must be one of {ROLES}, got {role!r}')
    if not session:
        raise ValueError('a session note entry needs a session id')
    note = read(project, node_id)
    if session in sessions_of(note, role):
        return False
    entry = {'session': session, 'host': host or host_of_session(session),
             'at': _now() if at is NOW else at}
    if source:
        entry['source'] = source
    note[role].append(entry)
    path = note_path(project, node_id)
    body = json.dumps({'v': VERSION, **{r: note[r] for r in ROLES}}, ensure_ascii=False, indent=2) + '\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return True


def drop(project, node_id) -> bool:
    """Forget one node's note (the node is gone). True when a file was there to remove."""
    path = note_path(project, node_id)
    try:
        path.unlink()
        return True
    except OSError:
        return False