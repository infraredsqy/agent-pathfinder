"""Tests for the conduct pipeline -- one DAG node per cycle step, closed by the worker that ran it.

One entry point, the whole collection (no DSH, no real sessions, no network):

    python tools/conduct/tests/test_conduct_pipeline.py      # the pool
    python -m unittest discover -s conduct/tests -t .        # from tools/, the same collection

Both run every case the same way: the pool spreads the subprocess-bound cases over the machine's
cores. `--jobs=N` and `-v` are accepted by the first form; anything else is handed to `unittest`.

No case patches a process-global name any more, so every module runs in the pool: a seam that a case
needs is a name on the module that owns it (`harness_env.patched` is the one place those are swapped
and put back).

**pytest does not take this road**: it runs case by case, and the same batch takes half a minute. Use
it to look at failure details; day to day, `python <this file>` is this suite's own entry point.

Every case builds a throwaway project in a temp dir and drives the real modules and the real CLIs.
The rule that matters: **state only advances through the tools** (`conduct_pipeline.plan/apply/record`,
the `state-manage.py` commands, `handoff.py`). The harness never performs a pipeline step itself --
an earlier version did, and that is exactly what let a missing bookkeeping call hide behind 30 green
tests.

Speed is part of the contract here: the whole suite must finish in a few seconds, so `load_tests`
hands `unittest discover` the same suite this file runs directly. See harness.py for what is driven
in-process versus as a process -- and for the one test that pins the two together.

What this suite is organised around
-----------------------------------
  1. the shape: one DAG node = one cycle step = one `pf-conduct` session, recorded in the cycle's
     own `path` inside `state.json` (no stage, no round, no per-round file, no second pointer)
  2. the close: the worker's own `next --after <node_id>`, the outcome read off the signal it
     already wrote, and the next node picked by the DAG's own rule
  3. instructions: one account, two states, three scopes, two targets, read fresh by the session
  4. the invariants that each cost a bug (the stop rule, tags as a pick filter, `wait` being
     stable, plan being read-only)
  5. the levers that survive: `cycle retry` + `cycle hold`, `--stop`, `clean-temp`
  6. the guards each of those costs: `TestCloseGuards` (the close), `TestSignalGuards` (a forged
     ticket), `TestCleanTemp` (the sweep, and a state it cannot read)
  7. `TestEndToEnd`: the whole walk through the real CLI, with a dry-run spawn and no session
"""

from __future__ import annotations

import concurrent.futures
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The cases themselves live in `cases_*.py`, one group per file, and each one is star-imported here:
# `_collect_tests` walks THIS module's globals for `TestCase` subclasses, and a worker process runs
# `globals()[cls_name]`, so a class that is not in this namespace does not run at all. The names
# `cases_*` are deliberately outside both `unittest`'s `test*.py` pattern and pytest's
# `test_*.py` / `*_test.py` patterns: this file is the suite's one collection point, and a second
# one would run every case twice.
from cases_base import *          # noqa: F401,F403
from cases_node import *          # noqa: F401,F403
from cases_select import *        # noqa: F401,F403
from cases_instruct import *      # noqa: F401,F403
from cases_contract import *      # noqa: F401,F403
from cases_close import *         # noqa: F401,F403
from cases_recovery import *      # noqa: F401,F403
from cases_maintenance import *   # noqa: F401,F403
from cases_insertion import *     # noqa: F401,F403
from cases_maintenance_run import *  # noqa: F401,F403
from cases_cli import *           # noqa: F401,F403
from cases_cli_entries import *   # noqa: F401,F403
from cases_spawn_shell import *   # noqa: F401,F403
from cases_e2e import *           # noqa: F401,F403
from cases_ledger_merge import *  # noqa: F401,F403
from cases_ledger_merge_git import *  # noqa: F401,F403
from cases_dispatch import *      # noqa: F401,F403
from cases_zcode import *         # noqa: F401,F403
from cases_parallel import *      # noqa: F401,F403
from cases_parallel_slots import *  # noqa: F401,F403
from cases_fork import *          # noqa: F401,F403
from cases_init import *          # noqa: F401,F403
from cases_config import *        # noqa: F401,F403
from cases_session import *       # noqa: F401,F403
from cases_shared import *        # noqa: F401,F403
from cases_bin import *           # noqa: F401,F403
from cases_chain_spawn import *   # noqa: F401,F403
from cases_suite import *         # noqa: F401,F403


def _collect_tests():
    """(class name, method name) pairs for the pool.

    Walked straight off this module's own `TestCase` classes, NOT via `loadTestsFromModule`:
    this module defines `load_tests`, and asking the loader for a suite here would call it again
    (which calls this function again) -- the recursion returns a partial list, and the partial
    list is what a worker process would then be asked to run.
    """
    found = set()
    for name, obj in list(globals().items()):
        if isinstance(obj, type) and issubclass(obj, unittest.TestCase) and obj is not \
                unittest.TestCase:
            if name.startswith('_'):
                continue                      # `_Outcome` and friends are not cases
            for method in dir(obj):
                if method.startswith('test') and callable(getattr(obj, method)):
                    found.add((name, method))
    return sorted(found)


def _run_one(name):
    """Worker process: run one case and hand back its failures/errors as text."""
    cls_name, method = name
    case = globals()[cls_name](method)
    result = unittest.TestResult()
    case.run(result)
    report = ''.join(tb for _, tb in list(result.failures) + list(result.errors))
    return name, result.wasSuccessful(), report


def _run_batch(names):
    """Worker process: run a batch of cases, one result each."""
    return [_run_one(name) for name in names]


def _jobs(count):
    """How many worker processes to use.

    `--jobs=N` wins; otherwise one per core, capped at the case count. Measured on this machine's
    16 cores: 16 workers -> ~4.3 s, 12 -> ~5.1 s, 8 -> ~5.8 s. The cases are subprocess-bound, so
    more workers keep paying off; each one only costs a Python start and this module's import.
    """
    if _JOBS_OVERRIDE:
        return _JOBS_OVERRIDE
    return max(1, min(count, os.cpu_count() or 4))


def run_in_parallel(names):
    """Run every case, spread over the machine. Returns (elapsed_seconds, outcomes).

    Batched per worker (`pool.map` over one round-robin slice each) rather than one task per case
    and rather than one task per worker-hand-picked set: measured on this suite, round-robin over
    one slice per worker beat both a size-ordered split (5.1 s vs 4.7 s) and one task per case, and
    it needs no knowledge of which cases are slow.
    """
    jobs = _jobs(len(names))
    batches = [names[i::jobs] for i in range(jobs)]
    started = time.perf_counter()
    outcomes = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=jobs) as pool:
        for batch in pool.map(_run_batch, [b for b in batches if b]):
            outcomes.extend(batch)
    return time.perf_counter() - started, outcomes


_REMOTE_CLASSES = {}
_JOBS_OVERRIDE = 0


def _remote_class(cls_name):
    """A `unittest.TestCase` subclass named after an original case class.

    `unittest`'s display and reporting are built on `TestCase` identity (`__str__` from
    `__module__`/`__qualname__`), so each original class gets a generated twin here. The twin is
    NEVER registered in the module globals: the discovery loader walks `dir(module)`, and a class
    that cannot be constructed without arguments would break it.
    """
    cls = _REMOTE_CLASSES.get(cls_name)
    if cls is None:
        cls = type(cls_name, (unittest.TestCase,),
                   {'__module__': __name__, '__qualname__': cls_name,
                    '_remote_pending': True,
                    # `TestCase.__str__` gives only the method name; the results key needs the
                    # full path, and this is also what the failure report should read.
                    '__str__': lambda self: f'{type(self).__name__}.{self._testMethodName}'})
        cls.run = _remote_run
        _REMOTE_CLASSES[cls_name] = cls
    return cls


def _remote_run(self, result=None):
    """A generated twin runs only through `ParallelSuite.run`, never on its own."""
    raise AssertionError(f'{self} ran outside the parallel suite')


def _remote_case(name):
    """One generated twin, with the method name `unittest` insists on finding.

    `TestCase.__init__` refuses a method name it cannot find on the class, so the twin carries a
    stub for exactly that name; the stub is what raises if the case is ever run outside the suite.
    """
    cls_name, method = name
    cls = _remote_class(cls_name)
    if not hasattr(cls, method):
        setattr(cls, method, _remote_run)
    return cls(method)


class ParallelSuite(unittest.TestSuite):
    """The pool's cases, executed across processes.

    `load_tests` puts one of these at the head of the suite it returns, so `python -m unittest
    discover` runs the pool exactly as this file does directly -- a suite that behaves differently
    depending on which entry point a machine uses is how "it passes on my box" starts. The serial
    tail is not in here (see the module docstring); it trails this suite as a plain one.

    The cases it carries are generated `TestCase` twins, so `unittest`'s own accounting and
    display do the reporting: the suite only decides where each case ran.
    """

    def __init__(self, names):
        self._names = list(names)
        super().__init__([_remote_case(name) for name in self._names])
        # The enclosing suite drops each case object once it "has run" (a memory optimisation that
        # also adjusts `testsRun`). It must not: the enclosing suite never ran anything -- this
        # one did, and it added one result per case already.
        self._cleanup = False

    def countTestCases(self):
        return len(self._names)

    def run(self, result, debug=False):
        if debug:                                  # `unittest -f` / pdb: one case at a time
            return super().run(result, debug)
        elapsed, outcomes = run_in_parallel(self._names)
        cases = {str(case): case for case in self}
        for (cls_name, method), ok, report in sorted(outcomes):
            case = cases[f'{cls_name}.{method}']
            case._remote_pending = False           # the twin must not be run again by unittest
            # `TestResult.addSuccess` does not count the case: `TestCase.run` is what calls
            # `startTest` (which is what increments `testsRun`), and these cases ran in another
            # process. Without this the runner prints "Ran 0 tests" over a suite that ran all of
            # them -- and exits as if nothing had run at all.
            result.startTest(case)
            if ok:
                result.addSuccess(case)
            else:
                result.addFailure(case, _as_exc_info(report, f'{cls_name}.{method} failed'))
            result.stopTest(case)
        print(f'#{len(outcomes)} cases ran in {elapsed:.3f}s '
              f'({_jobs(len(self._names))} workers)')
        return result


def _as_exc_info(report, label):
    """A worker's traceback text as the exc_info triple `unittest` results expect.

    `addFailure` calls `_exc_info_to_string`, which reads frames off the exception's
    `__traceback__`. The worker already formatted the traceback into text, so the text becomes the
    exception's message and a small helper frame is the traceback -- the reader still gets the
    real message under the real case name.
    """
    text = (report or label).strip() or label

    def _as_raised():
        raise RuntimeError(text)

    try:
        _as_raised()
    except RuntimeError:
        return sys.exc_info()


def load_tests(loader, standard_tests, pattern):
    """`unittest discover` entry point: the pool as `ParallelSuite` -- the same collection, in the
    same order, as running this file directly."""
    names = _collect_tests()
    if not names:
        return standard_tests
    return unittest.TestSuite([ParallelSuite(names)])


def _run_parallel(argv):
    """The `python test_conduct_pipeline.py` entry point.

    Same cases, same batching and the same worker function as `load_tests` uses, with `--jobs=N`,
    `-v` and the progress dots on top. It exists so this file is runnable on its own.
    """
    verbosity = 1
    jobs = 0
    rest = []
    for arg in argv:
        if arg in ('-v', '--verbose'):
            verbosity = 2
        elif arg.startswith('--jobs='):
            jobs = int(arg.split('=', 1)[1])
        else:
            rest.append(arg)
    if rest:                                   # anything else: hand it to unittest
        return unittest.main(argv=[sys.argv[0], *rest], verbosity=verbosity, exit=False)

    names = _collect_tests()
    global _JOBS_OVERRIDE
    _JOBS_OVERRIDE = jobs
    elapsed, outcomes = run_in_parallel(names)
    outcomes.sort()
    failed = [(n, r) for n, ok, r in outcomes if not ok]
    if verbosity > 1:
        for (cls_name, method), ok, _ in outcomes:
            print(f'{cls_name}.{method} ... {"ok" if ok else "FAIL"}')
    else:
        print(''.join('.' if ok else 'F' for _, ok, _ in outcomes))
    for (cls_name, method), report in failed:
        print(f'\n{"=" * 70}\nFAIL: {cls_name}.{method}\n{"-" * 70}\n{report}')
    print('-' * 70)
    print(f'Ran {len(outcomes)} tests in {elapsed:.3f}s ({_jobs(len(names))} workers)')
    if failed:
        print(f'\nFAILED (failures={len(failed)})')
        return 1
    print('\nOK')
    return 0


if __name__ == '__main__':
    sys.exit(_run_parallel(sys.argv[1:]))
