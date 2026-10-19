"""A throwaway project, plus the way the pieces under `tools/shared/` are fetched back by path.

Self-contained: `tools/shared/` is the layer other tools drive from outside, so its suite takes no
fixture from another tool's -- one living next door to its consumer would leave this layer unable
to prove itself on its own. There are only three things here: locating this tree, fetching one
module by path, and making a temp project.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

__all__ = ['HERE', 'NODE', 'SHARED_DIR', 'TOOLS_DIR', 'SharedCase', '_child_env', '_fresh_module',
           '_shared', 'shared_loader']

HERE = Path(__file__).resolve().parent
SHARED_DIR = HERE.parent
TOOLS_DIR = SHARED_DIR.parent

#: These cases need a node id and nothing else -- the note's own shape does not read it: spelled
#: once here, so no case has to build its own.
NODE = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa'

_SHARED_LOADER = None


def _fresh_module(name, path):
    """A fresh copy of one file loaded by path, which nobody else holds."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def shared_loader():
    """`tools/shared/loader.py`, loaded by path and kept in this process.

    It stays in the process for the same reason the tools keep it: `reuse` hands back the same
    module object only when every caller goes through the one loader, and the project's
    cross-process lock keeps its reentrant record per process -- a second loader would quietly hand
    a second module to the same file.
    """
    global _SHARED_LOADER
    if _SHARED_LOADER is None:
        _SHARED_LOADER = _fresh_module('pf_shared_loader', SHARED_DIR / 'loader.py')
    return _SHARED_LOADER


def _shared(name, filename):
    """One file under `tools/shared/`, as this process's single copy."""
    return shared_loader().load_by_path(name, SHARED_DIR / filename, register=True, reuse=True)


def _child_env():
    """Every child this suite starts gets UTF-8 streams, and the switch that says "do not really
    open a session"."""
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['DSH_CONDUCT_SPAWN_DRY_RUN'] = '1'
    return env


class _Sandbox:
    """One throwaway project root: `<tmp>/`, holding nothing but what the case itself wrote."""

    def __init__(self, root):
        self.root = root


class SharedCase(unittest.TestCase):
    """One temp directory per case, as many as you ask for.

    The object `make()` hands back carries `root` -- the same handle the other suites hand back --
    so a case moved in from elsewhere needs no change to its own body (`sb = self.make()` /
    `sb.root`).
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-shared-case-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def make(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-shared-project-')
        self.addCleanup(tmp.cleanup)
        return _Sandbox(Path(tmp.name))