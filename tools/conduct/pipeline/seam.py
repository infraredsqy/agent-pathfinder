"""The entry module's live namespace, for the names a caller patches there.

`conduct_pipeline.py` is the module a caller loads, and these are the names it owns that are read at
CALL time because a caller may replace them: `tests/harness.py` swaps `_run` for its in-process
command runner and points `DAG_MANAGE` at the dag-manage it hooks, and `tests/test_conduct_pipeline.py`
sets `CMD_TIMEOUT` and replaces `_run` to prove that a hung child and a half-applied close are both
reported. A logic module that did `from ... import CMD_TIMEOUT` would freeze a copy at import and
make every one of those patches invisible, so the logic modules come through here instead.

`bind()` is the entry's first act after it loads this package, and it hands over `globals()` -- the
live dict, not a copy. Nothing here caches a value.
"""

from __future__ import annotations

_ns = None


def bind(namespace) -> None:
    """Remember the entry module's live namespace (`conduct_pipeline.globals()`)."""
    global _ns
    _ns = namespace


def _entry():
    if _ns is None:
        raise RuntimeError('the conduct_pipeline entry module never called seam.bind()')
    return _ns


def cmd(*args) -> list:
    """The entry's `_cmd`: an argv that runs `state-manage.py`."""
    return _entry()['_cmd'](*args)


def dag(*args) -> list:
    """The entry's `_dag`: an argv that runs `dag-manage.py`."""
    return _entry()['_dag'](*args)


def issue(*args) -> list:
    """The entry's `_issue`: an argv that runs `issue_manager.py`."""
    return _entry()['_issue'](*args)


def as_text(argv) -> str:
    """The entry's `_as_text`: an argv as the one readable line its errors quote."""
    return _entry()['_as_text'](argv)


def run(argv, cwd) -> None:
    """The entry's `_run`: run one command, bounded by the timeout, and fail loudly."""
    return _entry()['_run'](argv, cwd)


def capture(argv, cwd) -> str:
    """The entry's `_capture`: run one command and hand back its stdout.

    `run` throws stdout away, which is right for every command whose effect IS the point. Two callers
    need the text: `insertion.read_new_node_id` (the uuid `dag-manage add` mints is only ever printed
    on that command's stdout, and every insertion source goes through that one function) and
    `nodes._rank_ready_nodes` (the DAG's own ranking). Same bound, same loud failure on a non-zero
    exit.

    **A child process has exactly two roads, `run` and `capture`**: every module in the package goes
    through here, and none calls `subprocess` on its own -- patching these two names in the entry
    stops every child process. Child output is always decoded with `errors='replace'`, and that
    convention is written down in the entry's `_capture` (one place).
    """
    return _entry()['_capture'](argv, cwd)


def timeout() -> int:
    """The entry's `CMD_TIMEOUT`, as it stands right now."""
    return _entry()['CMD_TIMEOUT']


def spawner_script():
    """The entry's `SPAWNER_SCRIPT`: the path of the session spawner this host opens sessions with.

    One name for both hosts: the value is `spawn/host.py`'s answer (`dsh-spawn.ps1` under DSH,
    `zcode-spawn.ps1` under ZCode, or whatever `DSH_CONDUCT_SPAWNER` names).
    """
    return _entry()['SPAWNER_SCRIPT']
