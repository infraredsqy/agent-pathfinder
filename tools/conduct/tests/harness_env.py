"""Paths, the real modules loaded by path, and the small shared helpers.

Owns HERE/CONDUCT_DIR/TOOLS_DIR, the tool paths, the dag-manage resolution that sets
DSH_CONDUCT_DAG_MANAGE, the pipeline constants, load_pipeline/_fresh_module, chdir/_decode/_child_env.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from pathlib import Path

__all__ = [
    'CONDUCT_DIR', 'DAG_MANAGE', 'DELETED_SIGNAL_KEYS', 'PIPELINE_PATH', 'HANDOFF_PY', 'HERE',
    'NODE_DIR_OF', 'SIGNAL_KEYS', 'SIGNAL_OF', 'SIGNAL_PY', 'SKILLS', 'SPEC_OF', 'STATE_MANAGE',
    'TERMINAL_ACTIONS', 'TOOLS_DIR', '_child_env', '_decode', '_fresh_module', 'chdir', 'load_pipeline',
]


HERE = Path(__file__).resolve().parent
CONDUCT_DIR = HERE.parent
TOOLS_DIR = CONDUCT_DIR.parent
PIPELINE_PATH = CONDUCT_DIR / 'pipeline' / 'conduct_pipeline.py'
STATE_MANAGE = CONDUCT_DIR / 'pipeline' / 'state-manage.py'
SIGNAL_PY = CONDUCT_DIR / 'pipeline' / 'signal.py'
HANDOFF_PY = CONDUCT_DIR / 'pipeline' / 'handoff.py'
# `dag-manage.py` is NOT part of the conduct tool and is never edited by a conduct change, so it
# is resolved: next to us in the shipped tree, else the machine's installed copy.
#
# The tree tells the tool where dag-manage is the supported way -- `DSH_CONDUCT_DAG_MANAGE`, which
# `conduct_pipeline.py` reads at import -- rather than that module carrying a second search path of its
# own. It has to be set HERE, at harness import time: `conduct_pipeline.py` binds the path when it is
# loaded, and both the in-process module and every child process (`state-manage.py next`,
# `handoff.py`) load their own copy. `_child_env()` inherits it, so one assignment covers all of
# them, and nothing in the shipped tree depends on this file.
DAG_MANAGE = TOOLS_DIR / 'dag' / 'dag-manage.py'
if not DAG_MANAGE.exists():
    _installed = Path(os.path.expanduser('~')) / '.agents' / 'tools' / 'dag' / 'dag-manage.py'
    if _installed.exists():
        DAG_MANAGE = _installed
os.environ['DSH_CONDUCT_DAG_MANAGE'] = str(DAG_MANAGE)

# The pipeline, kept as a literal on purpose: a test comparing the tools' own tables has to state
# the expectation itself, not ask the tool what it thinks the answer is.
SKILLS = ['pf-conduct']
#: Where a node's own directory lives: the ledger, `<project>/.pf/nodes/<id>/`. It holds the node's
#: text (`spec.md`, and the reports its session writes, by CONVENTION -- nothing validates them any
#: more). What the node GENERATES is scratch and goes to `.pf/nodes/<id>/temp/`.
NODE_DIR_OF = '.pf/nodes/%s'
SPEC_OF = '.pf/nodes/%s/spec.md'
#: One signal per node: `.pf/signals/<cycle>/<node_id>.json` -- the ledger, next to `state.json`.
#: There is no cell index left to put in the file name.
SIGNAL_OF = '.pf/signals/%s/%s.json'
#: The `outputs` keys a dispatch prompt asks a worker for. The three old artifact keys
#: (`spec_path` / `phase_paths` / `run_path`) are NOT among them: they are deleted, along with the
#: checking that used to read them. `reading` IS read back -- it is the maintenance channel's two
#: measurements, the one `outputs` key the pipeline has an opinion about.
SIGNAL_KEYS = ['deliverable', 'acceptance', 'touched', 'reading']
DELETED_SIGNAL_KEYS = ('spec_path', 'phase_paths', 'run_path')

TERMINAL_ACTIONS = ('stop_pipeline', 'ask_user', 'no_cycle', 'fail_hard')


# ═══════════════════════════════════════════════════════════════════════════════
#  The real modules, loaded by path
# ═══════════════════════════════════════════════════════════════════════════════

_PIPELINE_MODULE = None


def load_pipeline():
    """The real conduct_pipeline.py, loaded by path (a fresh copy per sandbox)."""
    global _PIPELINE_MODULE
    if _PIPELINE_MODULE is None:
        spec = importlib.util.spec_from_file_location('conduct_pipeline', PIPELINE_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PIPELINE_MODULE = mod
    return _PIPELINE_MODULE


def _fresh_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@contextlib.contextmanager
def chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _decode(path: Path):
    """Read one of the tool's JSON files. JSON is UTF-8."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_bytes().decode('utf-8'))
    except UnicodeDecodeError:
        raise AssertionError(f'{path} is not valid UTF-8') from None


def _child_env():
    """Every child gets `PYTHONIOENCODING=utf-8`.

    Without it Python picks the console codepage for stdout/stderr, the tools' Chinese messages go
    out as GBK, and a parent decoding as UTF-8 loses the whole stream -- including the exit-code
    diagnostics a failed test needs.
    """
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    return env
