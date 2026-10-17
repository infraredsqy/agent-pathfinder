"""The real subprocesses and the CLI-call helpers: `_run_proc`, `run_state_manage`, `run_dag`,
`run_signal`, `run_handoff`, plus the small CLI helpers `_cycle_of`, `_kw`, `_json_out`.
`run_state_manage` stays a module-level global: the test suite patches it through this module
object, and every call site must see the patch.
"""

from __future__ import annotations

import json
from pathlib import Path

from harness_env import DAG_MANAGE, HANDOFF_PY, SIGNAL_PY, STATE_MANAGE, run_argv

__all__ = [
    "_run_proc", "run_state_manage", "run_dag", "run_signal", "run_handoff",
    "_cycle_of", "_kw", "_json_out",
]


# ═══════════════════════════════════════════════════════════════════════════════
#  Real subprocesses (the CLI surface, and the two worker commands)
# ═══════════════════════════════════════════════════════════════════════════════

def _run_proc(project: Path, argv, check: bool = True, env=None):
    """One real child process against `project`, through the suite's one runner."""
    return run_argv(argv, cwd=project, env=env, check=check)


def run_state_manage(project: Path, *args, check: bool = True, env=None):
    """The real state-manage.py, as a process. Used by the CLI-subject tests."""
    return _run_proc(project, [STATE_MANAGE, *args], check=check, env=env)


def run_dag(project: Path, *args, check: bool = True):
    return _run_proc(project, [DAG_MANAGE, *args], check=check)


def run_signal(project: Path, cycle_id, node_id, status, summary='', outputs=None, check=True):
    """What a worker session's last act is: write the node's signal. The real signal.py.

    `python signal.py <cycle> <node> <status> -s "..." -o '{...}'`.
    """
    return _run_proc(project, [SIGNAL_PY, cycle_id, node_id, status,
                               '-s', summary,
                               '-o', json.dumps(outputs or {}, ensure_ascii=False)], check=check)


def run_handoff(project: Path, cycle_id, node_id, status, summary='', outputs=None,
                dry_run=False, check=True):
    """The worker's one command: write the node's ticket. The close is `next --after <node>`.

    `python handoff.py <cycle> <node> <status> -s "..." -o '{...}'`. `--dry-run` keeps
    dsh-spawn.ps1 out of the tests.
    """
    argv = [HANDOFF_PY, cycle_id, node_id, status,
            '-s', summary, '-o', json.dumps(outputs or {}, ensure_ascii=False)]
    if dry_run:
        argv.append('--dry-run')
    return _run_proc(project, argv, check=check)


# ═══════════════════════════════════════════════════════════════════════════════
#  Small pieces the sandbox leans on
# ═══════════════════════════════════════════════════════════════════════════════

def _cycle_of(extra):
    """The cycle name in an extra-argument list, or None.

    `--cycle X` is the CLI spelling; `_cycle X` is how the harness names the same thing on the
    in-process path (there is no argv to parse there, so plan() gets it as an argument).
    """
    extra = list(extra)
    for token in ('--cycle', '_cycle'):
        if token in extra:
            return extra[extra.index(token) + 1]
    return None


def _kw(extra):
    """`next --after <node> --continue --note x` -> plan()/apply() keyword arguments.

    `_cycle X` is how the in-process path names the cycle the CLI spells `--cycle X`: there is no
    argv to parse here. `_after <node>` is the same for the closing call's node: the CLI's `--after`
    becomes an argument (a cycle can hold several nodes, so the node being closed is an argument
    now), while the `--node` / `--continue` / `--note` closing answers stay keywords.
    """
    out = {'decision': None, 'node': None, 'note': '', 'after': None}
    i = 0
    while i < len(extra):
        token = extra[i]
        if token == '--continue':
            out['decision'] = 'continue'; i += 1
        elif token == '--stop':
            out['decision'] = 'stop'; i += 1
        elif token == '--node':
            out['node'] = extra[i + 1]; i += 2
        elif token == '--note':
            out['note'] = extra[i + 1]; i += 2
        elif token == '_after':
            out['after'] = extra[i + 1]; i += 2
        elif token in ('--cycle', '_cycle'):
            i += 2
        else:
            raise AssertionError(f'unsupported next option in the in-process path: {token!r}')
    return out


def _json_out(proc, argv):
    """Parse a tool's stdout, refusing to guess when the child produced no JSON at all.

    A `None` stdout means the child's output could not be decoded by the parent. Saying so here
    beats a `TypeError: ... not NoneType` three frames further down.
    """
    if proc.stdout is None:
        raise AssertionError(f'no stdout could be read from: {" ".join(map(str, argv))}\n'
                             f'rc={proc.returncode}\nstderr={proc.stderr!r}')
    return json.loads(proc.stdout)
