"""`tools/issue/storage.py` 的路径绑定：导入不绑项目，点名之后才读得到那个池子。

CLI 走 `issue_manager.py main()` 里的 `configure(cwd)`（`test_weight.py` 与 conduct 那套用例
起的都是子进程，覆盖的就是那一条路）；这里盯的是进程内那条路 —— 没有点名时它出声停手，
点过名之后它读的正是点名的那个项目。

跑法（两种都行）：

    python -m unittest discover -s issue/tests -t .      （cwd 在 tools/）
    python issue/tests/test_storage_paths.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

STORAGE_PY = Path(__file__).resolve().parent.parent / 'storage.py'


def load_storage():
    """`storage.py` 的一份新拷贝，按路径加载 —— 每个用例一份，绑定互不串味。"""
    spec = importlib.util.spec_from_file_location('issue_storage_case', STORAGE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pool(issue_id: str) -> dict:
    return {'version': 1, 'issues': {issue_id: {'id': issue_id, 'title': issue_id,
                                                'severity': 'P3', 'status': 'open'}}}


class TestThePoolModuleBindsNothingAtImport(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='issue-paths-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_a_fresh_module_points_at_no_project(self):
        mod = load_storage()

        self.assertIsNone(mod.DATA_DIR)
        self.assertIsNone(mod.ISSUES_JSON)

    def test_a_read_without_a_project_says_so(self):
        mod = load_storage()
        err = io.StringIO()

        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            mod._read()

        self.assertEqual(1, caught.exception.code)
        self.assertIn('configure', err.getvalue())

    def test_configure_reads_the_project_the_caller_named(self):
        """cwd 落在别处：名字点给谁，就读谁。"""
        other = Path(tempfile.mkdtemp(prefix='issue-paths-other-'))
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        landed = other / '.pf' / 'issues' / 'issues.json'
        landed.parent.mkdir(parents=True)
        landed.write_text(json.dumps(pool('theirs')), encoding='utf-8')
        mod = load_storage()

        mod.configure(other)

        self.assertEqual(['theirs'], sorted(mod._read()['issues']))
        self.assertEqual(landed, mod.ISSUES_JSON)

    def test_a_write_lands_under_the_named_project(self):
        mod = load_storage()
        mod.configure(self.root)

        mod._write(pool('mine'))

        landed = self.root / '.pf' / 'issues' / 'issues.json'
        self.assertTrue(landed.is_file())
        self.assertEqual(['mine'], sorted(json.loads(landed.read_text(encoding='utf-8'))['issues']))


if __name__ == '__main__':
    unittest.main(verbosity=2)