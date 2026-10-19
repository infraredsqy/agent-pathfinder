"""fork-worktree and the backstop gate at its close-out: the two ends of a parallel fork, each
carrying a real-git proof.

  * fork wants a clean main tree (no forking inside the dirty window in the middle of another
    line's close-out), and a taken short id (either the worktree directory or the `pf/<short id>`
    branch still around) is refused; a successful fork lands node / branch / head / at in
    `.pf/meta/FORK-<short id>.json`.
  * the close-out opens: for a node with a FORK record on disk, an unclean worktree or branch is
    refused loudly and nothing is written; only once both are clean does it pass, and the same
    beat drops the record. A node with no record is not even asked about git -- a non-parallel
    close-out touches not one byte more, and that is the regression floor.

Why a file of its own: every case is one `git init` in setUp plus several real CLI runs (fork and
close are each a whole python plus its git subprocess), and pushing them into cases_parallel.py
would take that file over its 5 s per-file budget. The collection point star-imports this one, so
the suite's parallel run is unchanged (see the header of test_conduct_pipeline.py).

    python conduct/tests/cases_fork.py          # this file alone
    python conduct/tests/test_conduct_pipeline.py   # with the rest of the suite
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import NODE  # noqa: E402
from harness import GIT, STATE_MANAGE, git, init_markers, run_argv  # noqa: E402

__all__ = ['TestForkWorktree']


@unittest.skipUnless(GIT, 'git is not on PATH')
class TestForkWorktree(unittest.TestCase):
    """fork-worktree, and the FORK backstop gate of `next --after`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='conduct-fork-')
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / 'main'
        self.repo.mkdir(parents=True)
        self.short = NODE[:8]
        self.record = self.repo / '.pf' / 'meta' / f'FORK-{self.short}.json'
        self.wt = self.repo / '.pf' / 'worktrees' / self.short
        git(self.repo, 'init', '-b', 'main')
        # init's two marker files: without them, every command that writes refuses.
        pf = init_markers(self.repo)
        (pf / 'signals' / 'cyc').mkdir(parents=True)
        (pf / 'state.json').write_text(json.dumps(
            {'cycles': [{'id': 'cyc', 'title': 't', 'status': 'active', 'tags': ['coding'],
                         'holding': [{'id': NODE, 'since': '2026-10-01T00:00:00Z'}],
                         'path': []}]}, indent=2), encoding='utf-8')
        (pf / 'ideas.json').write_text(json.dumps(
            {'nodes': {NODE: {'title': 'node', 'status': 'shelved', 'mode': 'auto',
                              'dependencies': []}}}, indent=2), encoding='utf-8')
        (pf / 'signals' / 'cyc' / f'{NODE}.json').write_text(json.dumps(
            {'cycle': 'cyc', 'node': NODE, 'status': 'completed', 'summary': 'done',
             'outputs': {}, 'timestamp': '2026-10-01T00:00:00Z'}, indent=2), encoding='utf-8')
        # This repository is the "committed kind": `.pf/` goes into it too, and only
        # `.pf/.gitignore` keeps meta/ out.
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-m', 'seed', '--no-gpg-sign')
        self.head = git(self.repo, 'rev-parse', 'HEAD').stdout.strip()

    def _state_manage(self, *args):
        return run_argv([STATE_MANAGE, *args], cwd=self.repo, check=False)

    def _cycle(self):
        return json.loads((self.repo / '.pf' / 'state.json').read_text(encoding='utf-8'))['cycles'][0]

    def test_a_dirty_main_tree_refuses_the_fork(self):
        """A dirty main tree may be another line closing out: fork must refuse and leave nothing."""
        (self.repo / '.pf' / 'state.json').write_text('dirty', encoding='utf-8')

        proc = self._state_manage('fork-worktree', NODE)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('dirty', proc.stderr, 'the refusal must say why (the dirty window)')
        self.assertFalse(self.wt.exists(), 'a refused fork must not leave a worktree')
        self.assertFalse(self.record.exists(), 'a refused fork must not leave a FORK record')

    def test_a_clean_main_tree_forks_and_the_uncleaned_close_refuses(self):
        proc = self._state_manage('fork-worktree', NODE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.wt.is_dir())
        self.assertIn(f'pf/{self.short}', git(self.repo, 'branch', '--list', f'pf/{self.short}').stdout)
        rec = json.loads(self.record.read_text(encoding='utf-8'))
        self.assertEqual(NODE, rec['node'])
        self.assertEqual('main', rec['branch'], 'the record must carry the branch forked from')
        self.assertEqual(self.head, rec['head'])
        self.assertIn('at', rec)

        proc = self._state_manage('next', '--after', NODE)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('the merge-back is unfinished', proc.stderr,
                      'the refusal must name the unfinished merge-back')
        self.assertIn(f'pf/{self.short}', proc.stderr, 'the refusal must name what is still there')
        self.assertEqual([NODE], [h['id'] for h in self._cycle()['holding']],
                         'the refusal must not have settled anything')
        self.assertTrue(self.record.exists(), 'the gate refused: the record must stay')

    def test_a_taken_short_id_refuses_the_second_fork(self):
        self.assertEqual(0, self._state_manage('fork-worktree', NODE).returncode)
        proc = self._state_manage('fork-worktree', NODE)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('already exists', proc.stderr)

        # Only the worktree is dropped, the branch stays: the second occupying face must name it too.
        git(self.repo, 'worktree', 'remove', f'.pf/worktrees/{self.short}')
        proc = self._state_manage('fork-worktree', NODE)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(f'pf/{self.short}', proc.stderr)

    def test_a_close_after_the_cleanup_passes_and_drops_the_record(self):
        self.assertEqual(0, self._state_manage('fork-worktree', NODE).returncode)
        git(self.repo, 'worktree', 'remove', f'.pf/worktrees/{self.short}')
        git(self.repo, 'branch', '-D', f'pf/{self.short}')

        proc = self._state_manage('next', '--after', NODE)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.record.exists(), 'the gate passed: the FORK record must be dropped')
        cycle = self._cycle()
        self.assertEqual(cycle['holding'], [])
        self.assertEqual([NODE], [s['node'] for s in cycle['path']], 'the node must be settled')

    def test_a_close_without_a_fork_record_is_untouched_by_the_gate(self):
        proc = self._state_manage('next', '--after', NODE)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([NODE], [s['node'] for s in self._cycle()['path']])
        self.assertFalse(self.record.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
