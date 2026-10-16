#!/usr/bin/env python3
"""Where a part of this tree that a file uses lives: an environment variable, else the neighbour.

    tool_path(env_var, default)

Every tool here keeps its parts side by side in one checkout -- `conduct/`, `dag/`, `issue/`,
`shared/` -- and each file finds its neighbours from the path it is in. A tree that keeps a part
somewhere else (a review draft, a drill copy) names that part with the part's own environment variable
instead of the caller growing a second search path of its own: a silent fallback into another checkout
would hide the failure that matters -- a shipped tree whose neighbour went missing.

The rule is one function because it is one rule: `<env var>`, when it is set and not empty, else
`default`. Every file that resolves a part of this tree resolves it here, so "not set" cannot come to
mean two things.
"""

import os
from pathlib import Path

__all__ = ['tool_path']


def tool_path(env_var: str, default):
    """The path `env_var` names, or `default` when the environment says nothing."""
    named = os.environ.get(env_var)
    return Path(named) if named else Path(default)