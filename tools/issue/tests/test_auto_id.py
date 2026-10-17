"""自动 id 与一次批量入池的 id 分配：批内撞名让位，与池子里既有的那条撞名同样去重。

自动 id 是「模块 + 标题的 slug 前 40 字符」，所以一次扫描里标题前缀相同的几条会算出同一个 id
—— 它们各自都是真条目。这份用例盯两件事：撞名在批内自己让位（`-2`、`-3`，并在输出里点名），
以及「池子里已经有这个 id」仍旧走 skip 那一支。两种情形各有各的答案，不共用一条分支。

跑法（两种都行）：

    python -m unittest discover -s issue/tests -t .      （cwd 在 tools/）
    python issue/tests/test_auto_id.py
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / 'issue_manager.py'
STORAGE_PY = TOOL.parent / 'storage.py'
#: 50 个 x：slug 截到 40 字符之后，这一串标题前缀一模一样。
X = 'x' * 50
SLUG = 'x' * 40


def load_storage():
    """`storage.py` 的一份拷贝，按路径加载 —— `_auto_id` 是纯函数，不碰任何项目。"""
    spec = importlib.util.spec_from_file_location('issue_auto_id_case', STORAGE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAutoIdFromATitle(unittest.TestCase):
    """标题进，id 出。"""

    def setUp(self):
        self.auto_id = load_storage()._auto_id

    def test_a_missing_or_empty_title_becomes_untitled(self):
        self.assertEqual('demo-untitled', self.auto_id({'module': 'demo'}))
        self.assertEqual('demo-untitled', self.auto_id({'module': 'demo', 'title': ''}))

    def test_a_title_with_no_ascii_word_characters_becomes_untitled(self):
        self.assertEqual('demo-untitled', self.auto_id({'module': 'demo', 'title': '修复标题'}))
        self.assertEqual('unknown-untitled', self.auto_id({'title': '修复标题'}))

    def test_a_long_title_stops_at_forty_characters(self):
        """撞名的来处：前 40 字符一样的标题，算出来的 id 是同一个。"""
        self.assertEqual(f'demo-{SLUG}', self.auto_id({'module': 'demo', 'title': f'{X} alpha'}))
        self.assertEqual(self.auto_id({'module': 'demo', 'title': f'{X} alpha'}),
                         self.auto_id({'module': 'demo', 'title': f'{X} beta'}))


class BatchCase(unittest.TestCase):
    """一个 temp 假项目：记录目录摆成 init 过的样子，批量入池起真进程。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='issue-auto-id-'))
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
        return self.cli('add', '--from-file', str(batch), '--auto-id')

    def ids(self):
        code, out, err = self.cli('list', '--brief')
        self.assertEqual(code, 0, err or out)
        return sorted(i['id'] for i in json.loads(out))


class TestOneBatchOfAutoIds(BatchCase):

    def test_two_titles_sharing_a_prefix_both_land(self):
        code, out, err = self.add_batch([{'module': 'demo', 'title': f'{X} alpha'},
                                         {'module': 'demo', 'title': f'{X} beta'}])

        self.assertEqual(code, 0, err or out)
        self.assertEqual([f'demo-{SLUG}', f'demo-{SLUG}-2'], self.ids())
        self.assertIn('Batch import: 2 created, 0 skipped', out)

    def test_every_disambiguated_id_is_named_with_its_row(self):
        code, out, err = self.add_batch([{'module': 'demo', 'title': f'{X} alpha'},
                                         {'module': 'demo', 'title': f'{X} beta'},
                                         {'module': 'demo', 'title': f'{X} gamma'}])

        self.assertEqual(code, 0, err or out)
        self.assertIn(f'Auto-id disambiguated: row 2 "demo-{SLUG}" -> "demo-{SLUG}-2"', out)
        self.assertIn(f'Auto-id disambiguated: row 3 "demo-{SLUG}" -> "demo-{SLUG}-3"', out)
        self.assertEqual(3, len(self.ids()))

    def test_an_explicit_id_in_the_batch_holds_its_name_too(self):
        """批内显式写下的 id 一样占位：后面自动算出来的那条让位。"""
        code, out, err = self.add_batch([{'id': 'demo-untitled', 'module': 'demo'},
                                         {'module': 'demo', 'title': '修复标题'}])

        self.assertEqual(code, 0, err or out)
        self.assertEqual(['demo-untitled', 'demo-untitled-2'], self.ids())

    def test_an_id_already_in_the_pool_is_skipped_and_never_suffixed(self):
        code, out, err = self.cli('add', '--id', 'demo-old', '--title', 'old', '--module', 'demo')
        self.assertEqual(code, 0, err or out)

        code, out, err = self.add_batch([{'module': 'demo', 'title': 'old'}])

        self.assertEqual(code, 0, err or out)
        self.assertEqual(['demo-old'], self.ids())
        self.assertIn('Batch import: 0 created, 1 skipped', out)
        self.assertIn('already exists, skipping', err)
        self.assertNotIn('disambiguated', out)

    def test_a_skipped_explicit_id_still_holds_its_name_for_the_next_row(self):
        """row 1 的显式 id 撞上池子里那条 → 那一行 skip；row 2 自动算出的同一个 id 仍然让位。"""
        code, out, err = self.cli('add', '--id', 'demo-old', '--title', 'old', '--module', 'demo')
        self.assertEqual(code, 0, err or out)

        code, out, err = self.add_batch([{'id': 'demo-old', 'module': 'demo', 'title': 'again'},
                                         {'module': 'demo', 'title': 'old'}])

        self.assertEqual(code, 0, err or out)
        self.assertIn('Batch import: 1 created, 1 skipped', out)
        self.assertIn('Auto-id disambiguated: row 2 "demo-old" -> "demo-old-2"', out)
        self.assertIn('issue "demo-old" already exists, skipping', err)
        self.assertEqual(['demo-old', 'demo-old-2'], self.ids())

    def test_the_same_file_a_second_time_skips_every_entry(self):
        items = [{'module': 'demo', 'title': f'{X} alpha'},
                 {'module': 'demo', 'title': f'{X} beta'}]
        code, out, err = self.add_batch(items)
        self.assertEqual(code, 0, err or out)

        code, out, err = self.add_batch(items)

        self.assertEqual(code, 0, err or out)
        self.assertIn('Batch import: 0 created, 2 skipped', out)
        self.assertEqual(2, len(self.ids()))


if __name__ == '__main__':
    unittest.main(verbosity=2)