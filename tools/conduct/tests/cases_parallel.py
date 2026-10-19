"""The mechanical gates behind `parallel=2`: the on-disk mutex, the worktree return to the main
tree, one session per node at a time, and one claim belonging to one line only.

With two cycles running side by side (`parallel: worktree` mode), these things race:

  * Four whole-file read-modify-write records (state / ideas / instructions / issues) walked by two
    processes at once -- without the lock they overwrite each other whole. Here **real processes**
    collide: four `instruct add` start together, and all four must land.
  * A worker's cwd sits in `.pf/worktrees/<short id>`, where a `.pf/` checkout from fork time is --
    if the record commands trusted it, the two lines would each keep their own books. Here a **real
    git worktree** proves it: with cwd in the worktree, the close and the graph marks land in the
    main tree, the checkout in the worktree does not move a byte, and signal.py run directly at
    close time (bypassing handoff.py) also lands in the main tree.
  * When one node's dispatch is triggered twice (a timed rescue colliding with an in-flight
    dispatch), the second must be refused -- the cost of two open sessions cannot be reclaimed.
    Here the five faces of SPAWN.claim prove it: an in-flight claim refuses, a live pid refuses no
    matter how old, a failure releases, and a dead pid's note is judged by staleness -- under 30
    minutes it still refuses, and only once stale is it taken over.
  * Two lines pinning the same node in the overlapping close window would open two sessions and
    never close out -- here the cross-cycle guard inside the lock refuses the second pin loudly,
    while a re-pin of the same id on its own line (restart semantics) passes.

The other half -- the `parallel` policy (how many slots a line holds at once, how a close refills
them) -- is in `cases_parallel_slots.py`; fork-worktree and its close-time fallback gate are in
`cases_fork.py` (real git, real CLI; only a file of its own keeps the per-file runtime down; how to
run it and its registration are in that file's header).

    python conduct/tests/cases_parallel.py         # run this file alone
    python conduct/tests/test_conduct_pipeline.py      # with the whole suite
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import NODE, pipeline
from harness import (DAG_MANAGE, GIT, SIGNAL_PY, STATE_MANAGE, Sandbox, _child_env, git,
                     init_markers, run_argv)

__all__ = ['TestLedgerLock', 'TestWorktreeRedirect', 'TestSpawnClaim', 'TestOneNodeOneCycle']

PYTHON = sys.executable
CONDUCT_DIR = Path(pipeline.__file__).resolve().parent


class TestLedgerLock(unittest.TestCase):
    """Four concurrent `instruction add`: the lock serializes read-modify-write, so all four land;
    without it the last writer overwrites the whole file."""

    def test_concurrent_instruction_adds_all_survive(self):
        tmp = tempfile.TemporaryDirectory(prefix='conduct-lock-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        # init's two marker files: the lock only sits in an init-ed `.pf/`, and all four
        # concurrent writes need it.
        init_markers(root)
        env = _child_env()
        # Four processes start together: their read-modify-write windows overlap, and the lock
        # queues them.
        procs = [subprocess.Popen(
            [PYTHON, '-X', 'utf8', str(STATE_MANAGE), 'instruction', 'add',
             '--text', f'concurrent entry {n}', '--scope', 'global', '--said-by', 'parallel-test'],
            cwd=str(root), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace')
            for n in range(4)]
        outs = [p.communicate() for p in procs]
        for n, (proc, (out, err)) in enumerate(zip(procs, outs)):
            self.assertEqual(proc.returncode, 0,
                             f'add #{n} failed: {err}')
        data = json.loads((root / '.pf' / 'instructions.json').read_text(encoding='utf-8'))
        texts = [i['text'] for i in data['instructions']]
        self.assertEqual(sorted(texts), sorted(f'concurrent entry {n}' for n in range(4)),
                         'a concurrent writer was erased: the lock did not hold')
        ids = [i['id'] for i in data['instructions']]
        self.assertEqual(len(set(ids)), len(ids), 'two entries minted the same id')


@unittest.skipUnless(GIT, 'git is not on PATH')
class TestWorktreeRedirect(unittest.TestCase):
    """With cwd in a linked worktree, the record commands return to the main tree; the worktree's
    checkout does not move."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='conduct-wt-')
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / 'main'
        self.repo.mkdir(parents=True)
        git(self.repo, 'init', '-b', 'main')
        # init's two marker files. This repo is the "committed" flavour: `.pf/` goes into the repo
        # with everything else, and `.pf/.gitignore` keeps meta/ and worktrees/ out.
        pf = init_markers(self.repo)
        (pf / 'state.json').write_text(json.dumps(
            {'cycles': [{'id': 'cyc', 'title': 't', 'status': 'active', 'holding': [],
                         'path': []}]}, indent=2), encoding='utf-8')
        (pf / 'ideas.json').write_text(json.dumps(
            {'nodes': {NODE: {'title': 'node', 'status': 'todo', 'mode': 'auto',
                              'dependencies': []}}}, indent=2), encoding='utf-8')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-m', 'seed', '--no-gpg-sign')
        git(self.repo, 'worktree', 'add', '.pf/worktrees/w1', '-b', 'pf/w1')
        self.wt = self.repo / '.pf' / 'worktrees' / 'w1'

    def _run(self, tool, *args):
        return run_argv([tool, *args], cwd=self.wt, check=False)

    def test_a_ledger_write_from_the_worktree_lands_in_the_main_tree(self):
        proc = self._run(STATE_MANAGE, 'cycle', 'hold', 'cyc', NODE)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        state = json.loads((self.repo / '.pf' / 'state.json').read_text(encoding='utf-8'))
        self.assertEqual([h['id'] for h in state['cycles'][0]['holding']], [NODE],
                         'the close landed in the worktree stale copy, not the main tree')
        stale = json.loads((self.wt / '.pf' / 'state.json').read_text(encoding='utf-8'))
        self.assertEqual(stale['cycles'][0]['holding'], [],
                         'the worktree checkout was written to')

    def test_a_dag_write_from_the_worktree_lands_in_the_main_tree(self):
        proc = self._run(DAG_MANAGE, 'status', NODE, 'completed')
        self.assertEqual(proc.returncode, 0, proc.stderr)

        dag = json.loads((self.repo / '.pf' / 'ideas.json').read_text(encoding='utf-8'))
        self.assertEqual(dag['nodes'][NODE]['status'], 'completed')
        stale = json.loads((self.wt / '.pf' / 'ideas.json').read_text(encoding='utf-8'))
        self.assertEqual(stale['nodes'][NODE]['status'], 'todo')

    def test_the_standalone_signal_script_lands_in_the_main_tree(self):
        """When a worker runs signal.py straight in the worktree (bypassing handoff.py), the note
        must still land in the main tree.

        handoff.py resolves the main tree itself before passing base_dir; nobody resolves it for a
        solo signal.py, so it must carry that knowledge on its own -- this is the proof of it.
        """
        proc = run_argv([SIGNAL_PY, 'cyc', NODE, 'completed', '-s', 'worktree close', '-o', '{}'],
                        cwd=self.wt, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        sig = json.loads((self.repo / '.pf' / 'signals' / 'cyc' / f'{NODE}.json')
                         .read_text(encoding='utf-8'))
        self.assertEqual('completed', sig['status'])
        self.assertFalse((self.wt / '.pf' / 'signals' / 'cyc' / f'{NODE}.json').exists(),
                         'the signal landed in the worktree stale copy, not the main tree')


class TestSpawnClaim(unittest.TestCase):
    """The five faces of SPAWN.claim: an in-flight claim refuses, a live pid refuses no matter how
    old, a failure releases, and a dead pid's note is judged by staleness -- fresh refuses, stale
    takes over."""

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.sb.add_node(NODE, 'UI design', mode='auto')
        self.sb.start_cycle()
        self.prompt = (self.sb.root / '.pf' / 'meta' / 'launch' / 'cyc' / NODE / 'prompt.txt')
        self.prompt.parent.mkdir(parents=True, exist_ok=True)
        self.prompt.write_text('/pf-conduct test\n', encoding='utf-8')
        self.claim = self.prompt.parent / 'SPAWN.claim'

    def stub(self, name='ok-spawner.ps1', body=None):
        """A stand-in spawner, never a host one."""
        return self.sb.fake_spawner(name, body=body)

    def _dispatch(self, spawner=None, dry_run=False):
        """One `dispatch cyc`, through the harness's spawn entry, aimed at a stand-in spawner.

        The claim is only taken (and therefore only checked) where a spawn is really attempted:
        `spawn_session` skips it on the drill rail, so every case here runs on the other one -- and
        the only thing that rail can reach is this stub.
        """
        return self.sb.dispatch(spawner=spawner or self.stub(), dry_run=dry_run, check=False)

    def test_an_in_flight_spawn_refuses_a_second_dispatch(self):
        self.claim.write_text('99999 started just-now', encoding='utf-8')
        proc = self._dispatch()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('SPAWN.claim', proc.stderr, 'the refusal must name the claim it tripped on')

    def test_a_live_pid_refuses_the_dispatch_even_when_the_claim_is_stale(self):
        """The pid on the claim is still alive, so the dispatch may really still be in flight: no
        takeover, however old the claim."""
        self.claim.write_text(f'{os.getpid()} started long-ago', encoding='utf-8')
        old = time.time() - 31 * 60
        os.utime(self.claim, (old, old))
        proc = self._dispatch()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('SPAWN.claim', proc.stderr)
        self.assertTrue(self.claim.exists(), "a live pid's claim must not be taken over")

    def test_a_dead_pid_falls_back_to_the_staleness_rule(self):
        """The process that wrote the claim is dead: takeover falls back to the 30-minute staleness
        rule -- fresh refuses, stale takes over."""
        finished = subprocess.Popen([PYTHON, '-c', 'pass'])
        finished.wait()
        self.claim.write_text(f'{finished.pid} started long-ago', encoding='utf-8')
        old = time.time() - 31 * 60
        os.utime(self.claim, (old, old))
        proc = self._dispatch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.claim.exists(),
                         'the successful spawn cleans the launch dir, claim included')

    def test_a_dead_pid_with_a_fresh_claim_refuses_the_dispatch(self):
        """A dead pid but a fresh note: with the staleness window not yet reached it is not stale,
        and takeover may not run early -- the dispatch is refused and the note stays where it is."""
        finished = subprocess.Popen([PYTHON, '-c', 'pass'])
        finished.wait()
        self.claim.write_text(f'{finished.pid} started just-now', encoding='utf-8')
        proc = self._dispatch()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('SPAWN.claim', proc.stderr, 'the refusal must name the claim it tripped on')
        self.assertTrue(self.claim.exists(), 'a refused dispatch must leave the claim alone')

    def test_a_failed_spawn_releases_the_claim(self):
        proc = self._dispatch(spawner=self.stub('failing-spawner.ps1', body='exit 1\n'))
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.claim.exists(),
                         'a failed spawn must leave the prompt free for the manual retry')


class TestOneNodeOneCycle(unittest.TestCase):
    """cycle hold's cross-cycle guard: a node belongs to one line at a time, and a second line
    pinning it must be refused by name."""

    def setUp(self):
        self.sb = Sandbox(cli=True)
        self.addCleanup(self.sb.close)
        self.sb.add_node(NODE, 'UI design', mode='auto')
        self.sb.create_cycle('cyc')
        self.sb.create_cycle('cyc2', force=True)

    def test_a_node_shelved_by_another_cycle_refuses_the_pin(self):
        self.sb.set_current_node(NODE, cycle_id='cyc')
        proc = self.sb.set_current_node(NODE, cycle_id='cyc2', check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(NODE, proc.stderr, 'the refusal must name the node')
        self.assertIn('cyc', proc.stderr, 'the refusal must name the cycle that holds it')
        holders = [c['id'] for c in self.sb.state()['cycles']
                   if NODE in [h['id'] for h in c.get('holding') or []]]
        self.assertEqual(['cyc'], holders, 'the second pin must not have landed')

    def test_repinning_the_same_id_on_its_own_cycle_stays_legal(self):
        """Re-pinning the same id on its own line is restart semantics; the guard must not block
        it."""
        self.sb.set_current_node(NODE, cycle_id='cyc')
        proc = self.sb.set_current_node(NODE, cycle_id='cyc', check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == '__main__':
    unittest.main()