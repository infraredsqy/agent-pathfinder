"""写进池子的时刻长什么样：`discovered` / `updated_at` 都走这一棵树的那一个形状。

形状的正本在 `tools/shared/clock.py`；下面那条正则把微秒写死，所以正本一旦退回秒级，
记录会跟着少掉小数位，这里的断言就红 —— 用例守的是「记录里的时间戳有多细」这件事。

跑法（两种都行）：

    python issue\tests\test_timestamps.py
    python -m unittest discover -s issue/tests -t .      （cwd 在 tools/）
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent.parent
TOOL = TOOLS / 'issue' / 'issue_manager.py'
#: UTC、微秒、字面 `Z`。这里写死，不读 `clock.ISO_FMT`：读它的话，正本退回秒级时断言会跟着
#: 一起退回，那正是这条用例要拦住的漂移。
SHAPE = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z'


def load_clock():
    spec = importlib.util.spec_from_file_location('pf_shared_clock_case', TOOLS / 'shared' / 'clock.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


clock = load_clock()


class TimestampCase(unittest.TestCase):
    """一个 init 过的空项目：条目走真命令进池，记录从盘上读回来。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='issue-clock-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        ledger = self.root / '.pf'
        ledger.mkdir(parents=True, exist_ok=True)
        (ledger / '.gitignore').write_text('meta/\n', encoding='utf-8')
        (ledger / '.gitattributes').write_text('** -text\n', encoding='utf-8')

    def run_tool(self, *argv):
        proc = subprocess.run([sys.executable, str(TOOL), *[str(a) for a in argv]],
                              cwd=str(self.root), capture_output=True, text=True,
                              encoding='utf-8', errors='replace')
        self.assertEqual(0, proc.returncode, proc.stderr or proc.stdout)
        return proc.stdout

    def row(self, issue_id: str) -> dict:
        pool = json.loads((self.root / '.pf' / 'issues' / 'issues.json').read_text(encoding='utf-8'))
        return pool['issues'][issue_id]

    def check(self, value: str, label: str):
        self.assertRegex(value, SHAPE, f'{label} 的时间戳形状不对：{value!r}')
        self.assertIsNotNone(datetime.strptime(value, clock.ISO_FMT),
                             f'正本 {clock.ISO_FMT!r} 认不出 {value!r}')


class TestTheRecordTimestamp(TimestampCase):

    def test_a_new_issue_carries_the_tree_timestamp_shape(self):
        self.run_tool('add', '--id', 'one', '--title', '一格')

        row = self.row('one')

        for key in ('discovered', 'updated_at'):
            with self.subTest(key=key):
                self.check(row[key], key)

    def test_a_later_write_keeps_the_same_shape(self):
        self.run_tool('add', '--id', 'one', '--title', '一格')

        self.run_tool('resolve', 'one', '--reason', 'done')

        self.check(self.row('one')['updated_at'], 'updated_at')


if __name__ == '__main__':
    unittest.main(verbosity=2)