"""`mark`：扫描活着确认的那一笔记录 —— 只加 check_count、只报候选，一个状态都不改。

「mark 只记、不结案，结案另走 resolve」这条协议的正本在这里：候选集是「这次扫描没确认到的
open 条目」（advisory），unknown 是报了但池子里没有的 id；两条都不动任何条目的状态。

跑法（两种都行）：

    python -m unittest discover -s issue/tests -t .      （cwd 在 tools/）
    python issue/tests/test_mark.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / 'issue_manager.py'


class PoolCase(unittest.TestCase):
    """一个 temp 假项目：记录目录摆成 init 过的样子，命令起真进程。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='issue-mark-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # 两枚标记文件就是「这个记录目录 init 过」的凭据：写命令见不到它们会停手。
        ledger = self.root / '.pf'
        ledger.mkdir(parents=True, exist_ok=True)
        (ledger / '.gitignore').write_text('meta/\nworktrees/\ntemp/\n', encoding='utf-8')
        (ledger / '.gitattributes').write_text('** -text\n', encoding='utf-8')

    def cli(self, *argv: str):
        proc = subprocess.run([sys.executable, '-X', 'utf8', str(TOOL), *argv], cwd=str(self.root),
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    def add(self, uid: str, **fields):
        argv = ['add', '--id', uid, '--title', fields.pop('title', uid)]
        for key, value in fields.items():
            flag = '--' + key.replace('_', '-')
            argv += [flag, *value] if isinstance(value, list) else [flag, value]
        code, out, err = self.cli(*argv)
        self.assertEqual(code, 0, err or out)

    def mark(self, *ids: str) -> dict:
        code, out, err = self.cli('mark', *ids)
        self.assertEqual(code, 0, err or out)
        return json.loads(out)

    def pool(self) -> dict:
        code, out, err = self.cli('list')
        self.assertEqual(code, 0, err or out)
        return {i['id']: i for i in json.loads(out)}


class TestMarkBookkeeping(PoolCase):

    def test_a_confirmed_id_gets_one_more_check_count(self):
        self.add('a')

        self.mark('a')

        self.assertEqual(2, self.pool()['a']['check_count'])

    def test_asking_twice_in_one_call_still_counts_once(self):
        """同一趟里重复报的 id 是一个集合：一次扫描确认一次。"""
        self.add('a')

        self.mark('a', 'a')

        self.assertEqual(2, self.pool()['a']['check_count'])

    def test_an_empty_argument_is_dropped(self):
        """shell 引号留下的空串不算 id，也不进 unknown。"""
        self.add('a')

        report = self.mark('a', '')

        self.assertEqual(['a'], report['marked'])
        self.assertEqual([], report['unknown_ids'])

    def test_an_id_the_pool_does_not_have_comes_back_as_unknown(self):
        self.add('a')

        report = self.mark('a', 'never-filed')

        self.assertEqual(['a'], report['marked'])
        self.assertEqual(['never-filed'], report['unknown_ids'])

    def test_a_skipped_scan_leaves_every_status_alone(self):
        """mark 不动状态：open 的照旧 open，等 resolve 那条路来结案。"""
        self.add('a')

        self.mark('a')

        issue = self.pool()['a']
        self.assertEqual('open', issue['status'])
        self.assertIsNone(issue['reason'])


class TestMarkCandidates(PoolCase):

    def test_an_open_entry_the_scan_did_not_confirm_is_a_candidate(self):
        self.add('seen')
        self.add('unseen')

        report = self.mark('seen')

        self.assertEqual(['unseen'], [i['id'] for i in report['candidates']])

    def test_a_confirmed_entry_is_not_its_own_candidate(self):
        self.add('a')

        report = self.mark('a')

        self.assertEqual([], report['candidates'])

    def test_an_acknowledged_entry_is_never_a_candidate(self):
        """候选是给「该结案了吗」看的：押后的那条已经有人看过了。"""
        self.add('later')
        code, out, err = self.cli('acknowledge', 'later')
        self.assertEqual(code, 0, err or out)
        self.add('seen')

        report = self.mark('seen')

        self.assertEqual([], report['candidates'])

    def test_a_settled_entry_is_never_a_candidate(self):
        self.add('done')
        code, out, err = self.cli('resolve', 'done', '--reason', 'fixed')
        self.assertEqual(code, 0, err or out)

        report = self.mark('done')

        self.assertEqual([], report['candidates'])

    def test_a_candidate_carries_the_reading_a_triage_needs(self):
        self.add('unseen', severity='P1', title='unseen title')

        candidate = self.mark('nothing-filed')['candidates'][0]

        self.assertEqual({'id', 'severity', 'title', 'discovered'}, set(candidate))
        self.assertEqual('P1', candidate['severity'])
        self.assertEqual('unseen title', candidate['title'])


if __name__ == '__main__':
    unittest.main(verbosity=2)