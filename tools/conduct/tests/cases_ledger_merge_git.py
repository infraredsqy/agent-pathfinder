"""`ledger-merge.py` against a REAL `git`: `git merge`, `git merge` that conflicts, and `git rebase`.

Split out of `cases_ledger_merge.py` because these cases start a git process per step and that made
ONE file cost more than the repo's per-file budget (7.0 s); here they are ~3 s, and the collection
point star-imports both modules, so the suite's parallel run is unchanged (the two files run side by
side with everything else).

    python conduct/tests/cases_ledger_merge_git.py      # this file alone
    python conduct/tests/test_conduct_pipeline.py           # with the rest of the suite

git's own contract is the subject: exit 0 means merged, non-zero means `<ours>` is left exactly as it
was and the path is marked unmerged. The driver command is spelled the way it goes into
`.git/config` -- `python` plus the tool's absolute path, forward slashes, double quotes, and `%P` --
so this also proves that spelling runs on Windows (git hands the command to its own shell).

Every case builds its own throwaway repository: a rebase and a merge leave the repository in states
that would leak into the next case, and a shared fixture would have to reset them by hand.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cases_ledger_merge import (EDGE_X, EDGE_Y, MERGE_PY, NODE1, NODE2, NODE3, child_env, cycle,
                                ideas, instruction, node, read, step, tool_bytes)

__all__ = ['TestGitEndToEnd']

GIT = shutil.which('git')

#: What the driver is wired to in `.gitattributes` -- the block the report hands over verbatim.
GITATTRIBUTES = (
    '.pf/ideas.json        merge=pf\n'
    '.pf/state.json        merge=pf\n'
    '.pf/instructions.json merge=pf\n'
    '.pf/signals/**        merge=pf\n'
    '.pf/**                -text\n'
)


def git_env():
    """The identity git needs to commit, as env vars: two `git config` starts saved per case."""
    return dict(child_env(), GIT_AUTHOR_NAME='ledger test', GIT_AUTHOR_EMAIL='ledger@example.invalid',
                GIT_COMMITTER_NAME='ledger test', GIT_COMMITTER_EMAIL='ledger@example.invalid')


@unittest.skipUnless(GIT, 'git is not on PATH')
class TestGitEndToEnd(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='ledger-merge-git-')
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.git('init', '-b', 'main')
        self.git('config', 'merge.pf.driver',
                 f'python "{MERGE_PY.as_posix()}" %O %A %B %P')
        (self.repo / '.gitattributes').write_text(GITATTRIBUTES, encoding='utf-8')
        # init 的两枚文件，加 `.pf/.gitignore`：驱动写回时要拿项目那把锁，锁住在 `.pf/meta/`
        # 里，而它正是这份 ignore 挡住的东西 —— 于是下面那些"合并没留下痕迹"的断言还成立。
        ledger = self.repo / '.pf'
        ledger.mkdir(parents=True, exist_ok=True)
        (ledger / '.gitignore').write_text('meta/\nworktrees/\ntemp/\n', encoding='utf-8')
        (ledger / '.gitattributes').write_text('** -text\n', encoding='utf-8')

    def git(self, *args, check=True):
        proc = subprocess.run([GIT, *args], cwd=str(self.repo), capture_output=True, text=True,
                              encoding='utf-8', errors='replace', env=git_env())
        if check and proc.returncode != 0:
            raise AssertionError(f'git {" ".join(args)} failed ({proc.returncode}):\n'
                                 f'{proc.stdout}\n{proc.stderr}')
        return proc

    def write(self, ledger: str, data, sort_keys=False):
        path = self.repo / ledger
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(tool_bytes(data, sort_keys=sort_keys))

    def commit(self, message, first=False):
        """One commit. Later ones are edits of tracked files, so `-a` saves an `add` start."""
        if first:
            self.git('add', '-A')
            self.git('commit', '-m', message, '--no-gpg-sign')
        else:
            self.git('commit', '-a', '-m', message, '--no-gpg-sign')

    def test_the_attributes_point_every_ledger_at_this_driver(self):
        ledgers = ('.pf/ideas.json', '.pf/state.json', '.pf/instructions.json',
                   '.pf/signals/cyc/aaaa1111.json')
        out = self.git('check-attr', 'merge', *ledgers).stdout
        for ledger in ledgers:
            with self.subTest(ledger=ledger):
                self.assertIn(f'{ledger}: merge: pf', out)

    def test_a_real_merge_uses_the_driver_and_lands_clean(self):
        shared = step(NODE1, '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z')
        self.write('.pf/ideas.json', ideas(aaaa=node('one')), sort_keys=True)
        self.write('.pf/state.json', cycle([shared]))
        self.write('.pf/instructions.json', instruction())
        self.commit('ledger base', first=True)

        self.git('checkout', '-b', 'feature')
        self.write('.pf/ideas.json', ideas(aaaa=node('one'), bbbb=node('two')), sort_keys=True)
        self.write('.pf/state.json',
                   cycle([shared, step(NODE2, '2026-10-01T01:00:00Z', '2026-10-01T02:00:00Z')]))
        self.write('.pf/instructions.json', instruction(text='theirs text'))
        self.commit('feature: a node and a step')

        self.git('checkout', 'main')
        self.write('.pf/ideas.json', ideas(aaaa=node('one'), cccc=node('three')), sort_keys=True)
        self.write('.pf/state.json',
                   cycle([shared, step(NODE3, '2026-10-01T03:00:00Z', '2026-10-01T04:00:00Z')]))
        self.write('.pf/instructions.json', instruction(note='ours note'))
        self.commit('main: another node and step')

        proc = self.git('merge', 'feature', '--no-edit', check=False)

        self.assertEqual(0, proc.returncode, f'{proc.stdout}\n{proc.stderr}')
        self.assertNotIn('CONFLICT', proc.stdout + proc.stderr)
        self.assertEqual('', self.git('status', '--porcelain').stdout.strip())

        merged_ideas = read(self.repo / '.pf/ideas.json')
        self.assertEqual({NODE1, NODE2, NODE3}, set(merged_ideas['nodes']))
        merged_state = read(self.repo / '.pf/state.json')
        self.assertEqual([NODE1, NODE2, NODE3],
                         [s['node'] for s in merged_state['cycles'][0]['path']])
        merged_ins = read(self.repo / '.pf/instructions.json')['instructions'][0]
        self.assertEqual('theirs text', merged_ins['text'])
        self.assertEqual('ours note', merged_ins['note'])
        # and the driver's own bytes survived git: CRLF, and no trailing newline
        self.assertFalse((self.repo / '.pf/ideas.json').read_bytes().endswith(b'\n'))

    def test_a_real_conflict_keeps_ours_and_stops(self):
        self.write('.pf/ideas.json', ideas(aaaa=node('one', deps=[EDGE_X])), sort_keys=True)
        self.write('.pf/state.json', cycle([]))
        self.commit('ledger base', first=True)

        self.git('checkout', '-b', 'feature')
        self.write('.pf/ideas.json', ideas(aaaa=node('one', deps=[EDGE_X, EDGE_Y])), sort_keys=True)
        self.commit('feature: adds an edge')

        self.git('checkout', 'main')
        self.write('.pf/ideas.json', ideas(aaaa=node('one', deps=[])), sort_keys=True)
        self.commit('main: drops the edge')
        ours_bytes = (self.repo / '.pf/ideas.json').read_bytes()

        proc = self.git('merge', 'feature', '--no-edit', check=False)

        self.assertNotEqual(0, proc.returncode, 'a real conflict must stop the merge')
        self.assertIn('dependencies', proc.stdout + proc.stderr)
        status = self.git('status', '--porcelain').stdout
        self.assertTrue(status.startswith('UU .pf/ideas.json'), status)
        self.assertEqual(ours_bytes, (self.repo / '.pf/ideas.json').read_bytes(),
                         'git must leave <ours> exactly as it was')

    def test_a_rebase_runs_the_driver_too(self):
        """`git pull --rebase` is how pf-conduct prepares and closes a node, so the driver has to
        fire on a rebase as well. A rebase replays each commit with a three-way merge against the new
        base, which is the same call the merge case makes -- this pins that it really is."""
        self.write('.pf/ideas.json', ideas(aaaa=node('one')), sort_keys=True)
        self.write('.pf/state.json', cycle([]))
        self.commit('ledger base', first=True)

        self.git('checkout', '-b', 'feature')
        self.write('.pf/ideas.json', ideas(aaaa=node('one'), cccc=node('three')), sort_keys=True)
        self.commit('feature: adds a node')

        self.git('checkout', 'main')
        self.write('.pf/ideas.json', ideas(aaaa=node('one', status='completed')), sort_keys=True)
        self.commit('main: marks the first node completed')

        proc = self.git('rebase', 'feature', check=False)

        self.assertEqual(0, proc.returncode, f'{proc.stdout}\n{proc.stderr}')
        self.assertNotIn('CONFLICT', proc.stdout + proc.stderr)
        self.assertEqual('', self.git('status', '--porcelain').stdout.strip())
        self.assertFalse((self.repo / '.git' / 'rebase-merge').exists(), 'the rebase is still open')
        merged = read(self.repo / '.pf/ideas.json')
        self.assertEqual({NODE1, NODE3}, set(merged['nodes']))           # feature's node survived
        self.assertEqual('completed', merged['nodes'][NODE1]['status'])   # main's edit survived

    def test_a_rebase_conflict_tells_the_user_how_to_get_out(self):
        """The escape hatch, pinned as documentation: `--continue` after fixing, `--abort` to back out.
        The driver's own line is in the same output -- git prints a merge driver's stderr."""
        self.write('.pf/ideas.json', ideas(aaaa=node('one', deps=[EDGE_X])), sort_keys=True)
        self.commit('ledger base', first=True)

        self.git('checkout', '-b', 'feature')
        self.write('.pf/ideas.json', ideas(aaaa=node('one', deps=[EDGE_X, EDGE_Y])), sort_keys=True)
        self.commit('feature: adds an edge')

        self.git('checkout', 'main')
        self.write('.pf/ideas.json', ideas(aaaa=node('one', deps=[])), sort_keys=True)
        self.commit('main: drops the edge')

        proc = self.git('rebase', 'feature', check=False)

        self.assertNotEqual(0, proc.returncode)
        for hint in ('git rebase --continue', 'git rebase --abort', 'pf 冲突'):
            self.assertIn(hint, proc.stdout + proc.stderr, proc.stdout + proc.stderr)
        self.assertTrue((self.repo / '.git' / 'rebase-merge').exists()
                        or (self.repo / '.git' / 'rebase-apply').exists(),
                        'git should be mid-rebase, waiting for the user')


if __name__ == '__main__':
    unittest.main(verbosity=2)
