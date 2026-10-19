#!/usr/bin/env python3
"""Running git in a project -- this tree's one way of asking.

    git(project, *args)  ->  subprocess.CompletedProcess

Three callers ask git the same question with the same shape: `pf doctor` asks whether this project
tracks its ledger and what merge driver it registered, `init` asks whether this is a repository at all
and wires the driver in, and the parallel fork asks whether a branch is taken. The shape they share is
the whole of this module: git runs with the project as cwd, output is captured as text and decoded
UTF-8 with `errors='replace'` (a Windows console code page must not turn a path into a traceback), and
a non-zero exit is NOT raised -- every caller branches on the result itself, because "no such driver"
and "not a repository" are answers, not failures.

What each caller does with the answer is its own business. What belongs here is the one way this tree
talks to git: a decode fix, a timeout, or a change of flag order lands in one place.

This module imports the standard library and nothing else. Its callers load it by path through
`loader.py`, the same way every file outside `tools/shared/` reaches in here.
"""

import subprocess

__all__ = ['git']


def git(project, *args: str) -> subprocess.CompletedProcess:
    """`git <args>` run in `project`, captured and decoded the one way. See this module's docstring."""
    return subprocess.run(['git', *args], cwd=str(project), capture_output=True, text=True,
                          encoding='utf-8', errors='replace')