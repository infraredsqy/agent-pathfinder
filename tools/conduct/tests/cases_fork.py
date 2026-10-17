"""fork-worktree 与它的收尾兜底闸：并行 fork 的两头，各带一次真 git 的证明。

  * fork 要主树干净（他线收尾中段的脏窗里不许 fork），短 id 被占（worktree 目录或
    `pf/<短id>` 分支任一还在）要拒绝；fork 成功要在 `.work/meta/FORK-<短id>.json` 落
    node / branch / head / at。
  * 收尾开头：盘上有 FORK 记录的节点，worktree 与分支没清干净就响亮拒绝、什么都不写；
    清干净了才放行，放行的同一拍把记录摘掉。没有记录的节点连 git 都不被问——非并行
    收尾一个字节都不多动，这是回归底线。

为什么单独一个文件：每个 case 都是 setUp 一次 `git init` 加好几次真 CLI（fork、close 各是
一整个 python + 它的 git 子进程），压进 cases_parallel.py 会把那份文件顶过单文件 5 s 的
预算。集合点 star-import 这份，全套件的并行跑法不变（见 test_conduct_pipeline.py 的文件头）。

    python conduct/tests/cases_fork.py          # 这个文件单独跑
    python conduct/tests/test_conduct_pipeline.py   # 跟全套件一起
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
    """fork-worktree 与 `next --after` 的 FORK 兜底闸。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='conduct-fork-')
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / 'main'
        self.repo.mkdir(parents=True)
        self.short = NODE[:8]
        self.record = self.repo / '.pf' / 'meta' / f'FORK-{self.short}.json'
        self.wt = self.repo / '.pf' / 'worktrees' / self.short
        git(self.repo, 'init', '-b', 'main')
        # init 的两枚文件：没有它们，任何会写的命令都会拒。
        pf = init_markers(self.repo)
        (pf / 'signals' / 'cyc').mkdir(parents=True)
        (pf / 'state.json').write_text(json.dumps(
            {'cycles': [{'id': 'cyc', 'title': 't', 'status': 'active', 'tags': ['coding'],
                         'holding': [{'id': NODE, 'since': '2026-10-01T00:00:00Z'}],
                         'path': []}]}, indent=2), encoding='utf-8')
        (pf / 'ideas.json').write_text(json.dumps(
            {'nodes': {NODE: {'title': '节点', 'status': 'shelved', 'mode': 'auto',
                              'dependencies': []}}}, indent=2), encoding='utf-8')
        (pf / 'signals' / 'cyc' / f'{NODE}.json').write_text(json.dumps(
            {'cycle': 'cyc', 'node': NODE, 'status': 'completed', 'summary': 'done',
             'outputs': {}, 'timestamp': '2026-10-01T00:00:00Z'}, indent=2), encoding='utf-8')
        # 这个仓库是"提交档"：`.pf/` 跟着一起进库，只靠 `.pf/.gitignore` 把 meta/ 挡在外面。
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-m', 'seed', '--no-gpg-sign')
        self.head = git(self.repo, 'rev-parse', 'HEAD').stdout.strip()

    def _state_manage(self, *args):
        return run_argv([STATE_MANAGE, *args], cwd=self.repo, check=False)

    def _cycle(self):
        return json.loads((self.repo / '.pf' / 'state.json').read_text(encoding='utf-8'))['cycles'][0]

    def test_a_dirty_main_tree_refuses_the_fork(self):
        """主树不干净可能就是他线正在收尾：fork 必须拒，什么也不留。"""
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
        self.assertIn('合回没做完', proc.stderr, 'the refusal must name the unfinished merge-back')
        self.assertIn(f'pf/{self.short}', proc.stderr, 'the refusal must name what is still there')
        self.assertEqual([NODE], [h['id'] for h in self._cycle()['holding']],
                         'the refusal must not have settled anything')
        self.assertTrue(self.record.exists(), 'the gate refused: the record must stay')

    def test_a_taken_short_id_refuses_the_second_fork(self):
        self.assertEqual(0, self._state_manage('fork-worktree', NODE).returncode)
        proc = self._state_manage('fork-worktree', NODE)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('already exists', proc.stderr)

        # 只摘 worktree、分支还在：第二个占位 face 也要点名。
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
