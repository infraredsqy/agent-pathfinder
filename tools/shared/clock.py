#!/usr/bin/env python3
"""A moment in a record: the two shapes this tree writes, and "now" in each of them.

    now()          ->  '2026-10-16T15:13:44.160813Z'     microseconds, `ISO_FMT`
    now_seconds()  ->  '2026-10-16T15:13:44Z'            seconds, `ISO_FMT_SECONDS`

UTC and a literal `Z` in both. Which record carries which is a judgement, not a preference, and this
is where it is stated:

    microseconds  the graph's `created_at` / `updated_at` (`ideas.json`) and the issue pool's
                  `discovered` / `updated_at` (`issues.json`). They are the finest reading the tree has
                  of when a node or an issue moved, and a reader built on this shape loses nothing.
    seconds       everything the pipeline and its plumbing stamp: a cycle's own `updated_at` and its
                  holding `since` / path `started_at` / `ended_at` (`state.json`), a signal's
                  `timestamp`, an instruction entry's `at` / `resolved_at`, a fork record's `at`, and a
                  node's session note's `at`. A moment at second resolution is what these carry -- the
                  same key name in two records is not what picks the shape; the record is.

Both formats live here as constants rather than inside the functions: a reader that has to take a
value apart again (`datetime.strptime`) asks for the shape by name, and two spellings of one format is
how a record and its reader come apart.

This module imports the standard library and nothing else. Its callers load it by path through
`loader.py`, the same way every file outside `tools/shared/` reaches in here.
"""

from datetime import datetime, timezone

__all__ = ['ISO_FMT', 'ISO_FMT_SECONDS', 'now', 'now_seconds']

#: UTC, microseconds, a literal `Z` -- the graph's and the pool's timestamps.
ISO_FMT = '%Y-%m-%dT%H:%M:%S.%fZ'
#: UTC, seconds, a literal `Z` -- the pipeline's own records.
ISO_FMT_SECONDS = '%Y-%m-%dT%H:%M:%SZ'


def now() -> str:
    """This moment, in the microsecond record timestamp shape."""
    return datetime.now(timezone.utc).strftime(ISO_FMT)


def now_seconds() -> str:
    """This moment, in the second record timestamp shape."""
    return datetime.now(timezone.utc).strftime(ISO_FMT_SECONDS)