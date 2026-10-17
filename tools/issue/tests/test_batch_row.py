"""批量文件里一行值的语义：四个门控键给 `null` 就整批停手，五个内容键给 `null` 取缺省。

一行里给 `null` 与「没写这个键」不是一回事，按这个键管什么分两种收场：

    severity / decision / files / functions    管这一条能不能进池子 —— `null` 是写坏了：整批
                                              退出 1、点名那一行，池子一字不落
    title / module / source / description      内容键 —— `null` 与没写同义，取缺省
    / suggestion
    reason / plan_path                         缺省本来就是 `null`，两种写法落成同一个值

命令行那条门不分岔：`argparse` 没给的旗标就是「没给」，取值与这里「键缺失」那一列相同。

跑法（两种都行）：

    python -m unittest discover -s issue/tests -t .      （cwd 在 tools/）
    python issue/tests/test_batch_row.py
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

#: 一个「什么都没写」的条目落下来的样子，时间戳两键除外。
DEFAULTS = {'severity': 'P3', 'decision': 'unknown', 'files': [], 'functions': [],
            'title': 'x', 'module': '', 'source': 'scan', 'description': '', 'suggestion': '',
            'reason': None, 'plan_path': None}


class BatchCase(unittest.TestCase):
    """一个 temp 假项目：记录目录摆成 init 过的样子，批量入池起真进程。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='issue-row-'))
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

    def add_batch(self, items):
        batch = self.root / 'batch.json'
        batch.write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')
        return self.cli('add', '--from-file', str(batch))

    def pool(self):
        code, out, err = self.cli('list')
        self.assertEqual(code, 0, err or out)
        return {i['id']: i for i in json.loads(out)}

    def picked(self, record):
        return {key: record[key] for key in DEFAULTS}


class TestAGateKeyGivenNull(BatchCase):
    """`null` 是写坏了：整批停手，不拿缺省糊过去，也不落下任何一条。"""

    def stops_the_batch(self, key):
        code, out, err = self.add_batch([{'id': 'good', 'title': 'good'},
                                         {'id': 'x', key: None}])
        self.assertEqual(1, code)
        self.assertIn(f'row 2 (id=x)', err)
        self.assertNotIn('created', out)
        self.assertEqual({}, self.pool())

    def test_severity_null_stops_the_batch(self):
        self.stops_the_batch('severity')

    def test_decision_null_stops_the_batch(self):
        self.stops_the_batch('decision')

    def test_files_null_stops_the_batch(self):
        self.stops_the_batch('files')

    def test_functions_null_stops_the_batch(self):
        self.stops_the_batch('functions')


class TestAContentKeyGivenNull(BatchCase):
    """内容键给 `null` 与没写这个键同义。"""

    def takes_the_default(self, key):
        code, out, err = self.add_batch([{'id': 'x', key: None}])
        self.assertEqual(code, 0, err or out)
        self.assertEqual(DEFAULTS[key], self.picked(self.pool()['x'])[key])

    def test_title_null_falls_back_to_the_id(self):
        self.takes_the_default('title')

    def test_module_null_falls_back_to_empty(self):
        self.takes_the_default('module')

    def test_source_null_falls_back_to_scan(self):
        self.takes_the_default('source')

    def test_description_null_falls_back_to_empty(self):
        self.takes_the_default('description')

    def test_suggestion_null_falls_back_to_empty(self):
        self.takes_the_default('suggestion')


class TestNoKeyAtAll(BatchCase):

    def test_a_row_with_nothing_but_an_id_takes_every_default(self):
        code, out, err = self.add_batch([{'id': 'x'}])

        self.assertEqual(code, 0, err or out)
        self.assertEqual(DEFAULTS, self.picked(self.pool()['x']))

    def test_the_command_line_door_takes_those_same_defaults(self):
        """命令行那条门的取值与「键缺失」那一列相同 —— 只有 title 取的是各自的 id。"""
        code, out, err = self.cli('add', '--id', 'x')
        self.assertEqual(code, 0, err or out)

        code, out, err = self.add_batch([{'id': 'y'}])
        self.assertEqual(code, 0, err or out)

        pool = self.pool()
        self.assertEqual(dict(DEFAULTS, title='x'), self.picked(pool['x']))
        self.assertEqual(dict(DEFAULTS, title='y'), self.picked(pool['y']))


if __name__ == '__main__':
    unittest.main(verbosity=2)