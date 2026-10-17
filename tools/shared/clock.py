#!/usr/bin/env python3
"""A moment in a record: the shape this tree writes, and "now" in that shape.

    now()  ->  '2026-10-16T15:13:44.160813Z'

UTC, microseconds, a literal `Z`. Microseconds are what the records on disk already carry -- the
graph's `created_at` / `updated_at` and the pool's -- so a reader built on this shape loses nothing,
and a writer that hands back a coarser one narrows what the record can say.

An account that keeps its moment at second resolution states that shape where it is written.

`ISO_FMT` is the same shape as a `strftime` string, for a reader that has to take a value apart
again. That reader is why the shape lives here as a constant rather than inside the function: two
spellings of one format is how a record and its reader come apart.

This module imports the standard library and nothing else. Its callers load it by path through
`loader.py`, the same way every file outside `tools/shared/` reaches in here.
"""

from datetime import datetime, timezone

__all__ = ['ISO_FMT', 'now']

#: UTC, microseconds, a literal `Z`.
ISO_FMT = '%Y-%m-%dT%H:%M:%S.%fZ'


def now() -> str:
    """This moment, in the record timestamp shape."""
    return datetime.now(timezone.utc).strftime(ISO_FMT)