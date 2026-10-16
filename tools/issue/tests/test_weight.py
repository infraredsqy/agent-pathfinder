"""`weight`: 池子有多重，一个数，给管线读。

pf-conduct 在节点收尾时拿它跟这条线自己的 `issue_pool_weight_limit` 比。两件事定这个数：
actionable 的条目才算（跟 `status` 一个口径），`decision: external` 的那些留着不算 ——
它们的决定权在人手上，算进来就是一块谁也挪不动的底板，阈值再也回不到线下。

跑法（两种都行）：

    python -m pytest tools/issue/tests/test_weight.py
    python tools/issue/tests/test_weight.py
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


def run(project: Path, *argv: str):
    """一条真命令：argv 进去，退出码与两路输出出来。"""
    proc = subprocess.run([sys.executable, str(TOOL), *argv], cwd=str(project),
                          capture_output=True, text=True, encoding='utf-8', errors='replace')
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


class PoolCase(unittest.TestCase):
    """一个空项目：记录目录先摆成 init 过的样子，条目走 cmd 的入池口。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='issue-weight-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # 两枚标记文件就是「这个记录目录 init 过」的凭据：写命令见不到它们会停手，
        # 请你去跑 state-manage.py init。这个 fixture 只要一个空池子，所以就地摆好。
        ledger = self.root / '.pf'
        ledger.mkdir(parents=True, exist_ok=True)
        (ledger / '.gitignore').write_text('meta/\nworktrees/\ntemp/\n', encoding='utf-8')
        (ledger / '.gitattributes').write_text('** -text\n', encoding='utf-8')

    def add(self, *items):
        batch = self.root / 'batch.json'
        batch.write_text(json.dumps(list(items), ensure_ascii=False), encoding='utf-8')
        code, out, err = run(self.root, 'add', '--from-file', str(batch))
        self.assertEqual(code, 0, err or out)

    def weight(self, *extra: str) -> str:
        code, out, err = run(self.root, 'weight', *extra)
        self.assertEqual(code, 0, err or out)
        return out

    def issue(self, uid, severity='P3', decision='autofix'):
        return {'id': uid, 'title': uid, 'severity': severity, 'decision': decision,
                'module': 'demo'}


class TestWeight(PoolCase):

    def test_an_empty_pool_weighs_nothing(self):
        self.assertEqual(self.weight(), '0')

    def test_severity_is_what_makes_a_pool_heavy(self):
        self.add(self.issue('a', 'P3'), self.issue('b', 'P1'), self.issue('c', 'P0'))
        self.assertEqual(self.weight(), str(1 + 4 + 8))

    def test_an_entry_waiting_on_the_human_stays_out_of_the_number(self):
        self.add(self.issue('mine', 'P2'), self.issue('yours', 'P0', decision='external'))
        self.assertEqual(self.weight(), '2')

    def test_untriaged_entries_count(self):
        self.add(self.issue('fresh', 'P1', decision='unknown'))
        self.assertEqual(self.weight(), '4')

    def test_a_settled_entry_stops_counting(self):
        self.add(self.issue('done', 'P0'))
        code, out, err = run(self.root, 'resolve', 'done')
        self.assertEqual(code, 0, err or out)
        self.assertEqual(self.weight(), '0')

    def test_a_deferred_entry_is_still_owed(self):
        """`acknowledge` 把条目记成「看见了、押后」——池子还欠着它，权重照算。"""
        self.add(self.issue('later', 'P2'))
        code, out, err = run(self.root, 'acknowledge', 'later')
        self.assertEqual(code, 0, err or out)
        self.assertEqual(self.weight(), '2')

    def test_verbose_names_what_it_held_back(self):
        self.add(self.issue('mine', 'P3'), self.issue('yours', 'P0', decision='external'))
        report = json.loads(self.weight('--verbose'))
        self.assertEqual(report['weight'], 1)
        self.assertEqual(report['counted'], 1)
        self.assertEqual(report['not_counted_external'], 1)
        self.assertEqual(report['not_counted_external_weight'], 8)
        self.assertEqual(report['by_severity'], {'P3': 1})


if __name__ == '__main__':
    unittest.main(verbosity=2)
