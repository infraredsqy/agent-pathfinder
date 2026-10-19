"""Paths, the real modules loaded by path, and the small helpers every fixture is built on.

Owns HERE/CONDUCT_DIR/TOOLS_DIR, the tool paths, the dag-manage resolution that sets
DSH_CONDUCT_DAG_MANAGE, the pipeline constants, load_pipeline/_fresh_module/shared_loader, chdir/
_decode/_child_env, and the pieces the `cases_*` files share instead of each writing their own:
`init_markers` (what `init` puts in a `.pf/`), `run_argv` and `git` (one child process, one identity),
`patched` (one place a global name is swapped), `spawn_dry_run` (the one place a session start is
turned into a drill), and `shipped_sources` (the one list of the shipped tree).
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = [
    'CONDUCT_DIR', 'DAG_MANAGE', 'GIT', 'INIT_GITATTRIBUTES', 'INIT_GITIGNORE',
    'PIPELINE_PATH', 'HANDOFF_PY', 'HERE', 'SIGNAL_PY', 'SPAWN_DRY_RUN', 'STATE_MANAGE',
    'TERMINAL_ACTIONS', 'TOOLS_DIR',
    '_child_env', '_decode', '_fresh_module', '_run_command', 'chdir', 'git', 'git_env',
    'init_markers', 'load_pipeline', 'patched', 'run_argv', 'shared_loader', 'shipped_sources',
    'spawn_dry_run',
]


HERE = Path(__file__).resolve().parent
CONDUCT_DIR = HERE.parent
TOOLS_DIR = CONDUCT_DIR.parent
PIPELINE_PATH = CONDUCT_DIR / 'conduct_pipeline.py'
STATE_MANAGE = CONDUCT_DIR / 'state-manage.py'
SIGNAL_PY = CONDUCT_DIR / 'signal.py'
HANDOFF_PY = CONDUCT_DIR / 'handoff.py'
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
    if not _installed.exists():
        # Loud HERE rather than deep inside the first sandbox that loads a module from that path: a
        # wrong resolution would otherwise surface as a FileNotFoundError from whichever case ran
        # first, which reads as that case's fault.
        raise AssertionError(
            f'dag-manage.py is at neither {DAG_MANAGE} nor {_installed}, and the suite drives the '
            f'real graph tool: point one of those paths at it')
    DAG_MANAGE = _installed
os.environ['DSH_CONDUCT_DAG_MANAGE'] = str(DAG_MANAGE)

#: `git`, resolved once for the whole suite: a case whose fixture needs git skips itself on `GIT`.
GIT = shutil.which('git')

#: The pipeline, kept as a literal on purpose: a test comparing the tools' own tables has to state
#: the expectation itself, not ask the tool what it thinks the answer is.
TERMINAL_ACTIONS = ('stop_pipeline', 'ask_user', 'no_cycle')


def shipped_sources():
    """Every Python file the tools ship, tests excluded.

    The ONE list behind the guards that ask the whole tree a question -- who builds the spawner's
    argv, who spells the broken-chain marker's name, which module defines a top-level name twice --
    so a new package is added here once and no guard keeps a copy of the tree.
    """
    return (sorted(CONDUCT_DIR.glob('*.py'))
            + [path for cluster in ('pipeline', 'spawn')
               for path in sorted((CONDUCT_DIR / cluster).glob('*.py'))]
            + sorted((CONDUCT_DIR / 'state').glob('*.py'))
            + sorted((CONDUCT_DIR / 'ledger_merge').glob('*.py'))
            + sorted((TOOLS_DIR / 'dag').glob('*.py'))
            + sorted((TOOLS_DIR / 'issue').glob('*.py'))
            + sorted((TOOLS_DIR / 'shared').glob('*.py'))
            + sorted((TOOLS_DIR / 'bin').glob('*.py')))


# ═══════════════════════════════════════════════════════════════════════════════
#  A throwaway project's ledger: the one thing `init` is
# ═══════════════════════════════════════════════════════════════════════════════

#: What `init` writes into `.pf/`, and the only thing that says a ledger was put there on purpose:
#: every mutating command refuses a `.pf/` without these two. Stated once -- a fixture that gets one
#: of them wrong is refused deep inside a tool, where it reads as the tool's fault, not the fixture's.
INIT_GITIGNORE = 'meta/\nworktrees/\ntemp/\n'
INIT_GITATTRIBUTES = '** -text\n'


def init_markers(project) -> Path:
    """Write the two `init` markers into `<project>/.pf/` and return that ledger directory.

    For a throwaway project these two files are the whole of what `init` would do (`init` also
    clones the ledger and writes its usage note; nothing a case reads depends on either).
    """
    ledger = Path(project) / '.pf'
    ledger.mkdir(parents=True, exist_ok=True)
    (ledger / '.gitignore').write_text(INIT_GITIGNORE, encoding='utf-8')
    (ledger / '.gitattributes').write_text(INIT_GITATTRIBUTES, encoding='utf-8')
    return ledger


# ═══════════════════════════════════════════════════════════════════════════════
#  One child process, one identity
# ═══════════════════════════════════════════════════════════════════════════════

def _run_command(command, *, cwd=None, env=None, check=True):
    """Run one command line, decoded as UTF-8, and report a non-zero exit loudly."""
    proc = subprocess.run([str(part) for part in command],
                          cwd=str(cwd) if cwd is not None else None, capture_output=True,
                          text=True, encoding='utf-8', errors='replace',
                          env=_child_env() if env is None else env)
    if check and proc.returncode != 0:
        raise AssertionError(f'command failed: {" ".join(str(p) for p in command)}\n  cwd={cwd}\n'
                             f'  rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}')
    return proc


def run_argv(argv, *, cwd=None, env=None, check=True):
    """One child process: `argv` after the interpreter, run from `cwd`, decoded as UTF-8.

    The suite's ONE python runner. `-X utf8` on top of `_child_env()`'s `PYTHONIOENCODING`: the
    tools' messages are Chinese, and together they make the child's streams UTF-8 whatever codepage
    the console is on. `check` raises with the command line and both streams, which is what every
    caller wants to read when a tool refuses.
    """
    return _run_command([sys.executable, '-X', 'utf8', *argv], cwd=cwd, env=env, check=check)


def git_env():
    """The environment a `git` child needs to commit: `_child_env()` plus this suite's identity.

    Every repository these cases touch is a throwaway one, so the identity is fixed here instead of
    configured into the repository (two `git config` starts saved per case), and it can never fall
    back to the machine's global one.
    """
    return dict(_child_env(), GIT_AUTHOR_NAME='pf test', GIT_AUTHOR_EMAIL='pf-test@example.invalid',
                GIT_COMMITTER_NAME='pf test', GIT_COMMITTER_EMAIL='pf-test@example.invalid')


def git(cwd, *args, check=True, env=None):
    """One `git` process against `cwd`, under this suite's own identity.

    The caller keeps its own `skipUnless(GIT)`: this helper assumes git is there.
    """
    return _run_command([GIT, *args], cwd=cwd, check=check, env=env or git_env())


# ═══════════════════════════════════════════════════════════════════════════════
#  The real modules, loaded by path
# ═══════════════════════════════════════════════════════════════════════════════

_PIPELINE_MODULE = None
_SHARED_LOADER = None


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


def shared_loader():
    """`tools/shared/loader.py`, loaded by path and kept for the process.

    Cached for the same reason the tools cache it: `reuse` hands one module object back only when
    every caller comes through the same loader, and the ledger lock's reentrancy record is one per
    process. A second loader would quietly hand a second module to the same file.
    """
    global _SHARED_LOADER
    if _SHARED_LOADER is None:
        _SHARED_LOADER = _fresh_module('pf_shared_loader', TOOLS_DIR / 'shared' / 'loader.py')
    return _SHARED_LOADER


@contextlib.contextmanager
def chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


@contextlib.contextmanager
def patched(target, name: str, value):
    """Put `value` on `target.name` for the block, and put the old one back afterwards.

    The suite's ONE place a process-global name is swapped: the fast path swaps `subprocess.run` and
    the three streams it captures, a case swaps `pipeline._run`, the parser probe swaps
    `argparse.ArgumentParser.parse_args`. Every one of them is record-replace-restore, and the
    restore is the half that matters -- a leak aims every later case in the same worker at the wrong
    thing. Yields the value that was there.
    """
    old = getattr(target, name)
    setattr(target, name, value)
    try:
        yield old
    finally:
        setattr(target, name, old)


def _decode(path: Path):
    """Read one of the tool's JSON files. JSON is UTF-8."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_bytes().decode('utf-8'))
    except UnicodeDecodeError:
        raise AssertionError(f'{path} is not valid UTF-8') from None


#: The switch that turns "start a session" into "print the argv it would have run". `_child_env()`
#: sets it, so every child a case starts is a drill; the one rail that needs a started session's
#: footprint takes it back out again, and that rail is `harness_sandbox`'s spawn entries.
SPAWN_DRY_RUN = 'DSH_CONDUCT_SPAWN_DRY_RUN'


def _child_env():
    """Every child gets `PYTHONIOENCODING=utf-8`, and the drill switch.

    Without the encoding Python picks the console codepage for stdout/stderr, the tools' Chinese
    messages go out as GBK, and a parent decoding as UTF-8 loses the whole stream -- including the
    exit-code diagnostics a failed test needs.

    The switch is what makes "no case starts a session" a property of the harness rather than of each
    case: a child that reaches the spawn door prints the argv instead of running it. The one caller
    that needs a spawner really to run names its own stand-in script and takes the switch back out.
    """
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env[SPAWN_DRY_RUN] = '1'
    return env


@contextlib.contextmanager
def spawn_dry_run():
    """Set the drill switch for the block, and put the old value back afterwards.

    The in-process rail reads `os.environ` directly (`cmd_dispatch`), so a caller driving that rail
    cannot rely on `_child_env()`.
    """
    old = os.environ.get(SPAWN_DRY_RUN)
    os.environ[SPAWN_DRY_RUN] = '1'
    try:
        yield os.environ
    finally:
        if old is None:
            os.environ.pop(SPAWN_DRY_RUN, None)
        else:
            os.environ[SPAWN_DRY_RUN] = old