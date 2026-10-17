#!/usr/bin/env python3
"""The one reader and the one atomic writer for this tree's JSON files.

Every record the tools keep is JSON -- state, signals, the graph, the instructions, the issue pool,
a node's session note, the broken-chain marker -- and every one of them is a whole-file write read
by other processes (a second command, a test, a human's editor). So a write lands the same way
everywhere:

    replace_with_retry(tmp, path, attempts=4)   the replace, tolerating a reader mid-read
    unlink_with_retry(path, attempts=4)         the same tolerance for a removal
    json_bytes(data, *, sort_keys=False)        what one record is on disk, as bytes
    atomic_write_bytes(path, payload)           temp file + fsync + that replace
    atomic_write_json(path, data, *, sort_keys=False)   the two above, in one call
    read_json_strict(path, label)               the read, and what it says when it cannot

`json_bytes` is where "a record's bytes" is decided -- indent, the text as it is, sorted keys only
when the caller's writer sorts them, the platform line separator, no trailing newline. The write is
that payload landed in binary, so the bytes on disk are the bytes computed, and a caller that needs to
know whether a write would change a file asks for them instead of writing its own `json.dumps`.

Why the replace is retried rather than failed: on Windows a plain `open()` for reading does not
carry FILE_SHARE_DELETE, so `os.replace` onto a file some reader holds open raises PermissionError.
The window is tiny and the retry is bounded well under a second.

A read has exactly three outcomes, and this is the one place their wording lives -- every caller of
a `.pf/` record reports them the same way:

    the file is not there       -> None. An absent record is the caller's own default, not a failure.
    the bytes are not UTF-8     -> JsonReadError('<label> is not valid UTF-8: <why>')
    the text is not JSON        -> JsonReadError('<label> parse failure: <why>')

`label` is how the caller names the file to a human (`state.json`, `signals/<cycle>/<node>.json`, a
path). A `JsonReadError` is a ValueError, so a caller that already catches those keeps working; a
CLI layer prints the message and ends the command, a library call lets it out for its own caller.
"""

import json
import os
import tempfile
import time
from pathlib import Path

__all__ = ['JsonReadError', 'atomic_write_bytes', 'atomic_write_json', 'json_bytes',
           'read_json_strict', 'replace_with_retry', 'unlink_with_retry']


class JsonReadError(ValueError):
    """A record on disk that exists but cannot be read as this tree's JSON."""


def replace_with_retry(tmp, path, attempts: int = 4) -> None:
    """`os.replace(tmp, path)`, retried while a reader holds the target open this instant."""
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def unlink_with_retry(path, attempts: int = 4) -> None:
    """`path.unlink(missing_ok=True)`, retried under the same rule as the replace above."""
    path = Path(path)
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def json_bytes(data, *, sort_keys: bool = False) -> bytes:
    """One record's bytes on disk -- the exact payload `atomic_write_json` lands.

    The one definition of the format: `indent=2`, the text as it is (Chinese stays readable), keys
    sorted only when the caller's own writer sorts them, the platform line separator (these files are
    CRLF on Windows), and no trailing newline. A caller that has to know whether a write would change
    a file -- the ledger merge driver does -- asks for these bytes rather than spelling a second
    `json.dumps` out, which is one byte away from every merge looking like a change.
    """
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=sort_keys)
    return text.replace('\n', os.linesep).encode('utf-8')


def atomic_write_bytes(path, payload: bytes) -> None:
    """Write one record's bytes so a reader sees all of it or none of it.

    The temp file is made in the target's own directory (a cross-directory replace would leave half
    a file behind if it failed), flushed and fsynced before the replace. The target's directory is
    created when it is missing. A failure removes the temp file and raises: what a write failure
    means -- a message and a non-zero exit, a warning, or letting it out to the caller -- is the
    caller's decision, and this primitive has no opinion about it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_json(path, data, *, sort_keys: bool = False) -> None:
    """One record written whole: `atomic_write_bytes(path, json_bytes(data, sort_keys=sort_keys))`.

    See those two for the payload, the temp file, the fsync, and what a failure leaves behind.
    """
    atomic_write_bytes(path, json_bytes(data, sort_keys=sort_keys))


def read_json_strict(path, label: str):
    """One record, or None when the file is not there. See this module's docstring for the rest.

    JSON is UTF-8 by spec and every record here carries Chinese text, so bytes that are not UTF-8
    are a corrupt record and not an older build's: both that and unparseable text are refused, with
    the wording above.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except UnicodeDecodeError as e:
        raise JsonReadError(f'{label} is not valid UTF-8: {e}') from e
    except json.JSONDecodeError as e:
        raise JsonReadError(f'{label} parse failure: {e}') from e