"""Path binding in this plugin's `tools/storage.py`: importing binds no project, and the pool is only
readable after a name is given.

The CLI goes through `configure(cwd)` in `issue_manager.py main()` (`test_weight.py` and the conduct
cases all start child processes, and that is the path they cover); what is watched here is the
in-process path -- it speaks up and stops when nothing is named, and once a name is given it reads
exactly the project that was named.

How to run (either works):

    python -m unittest discover -s plugins/issue/tools/tests -t plugins/issue/tools
    python plugins/issue/tools/tests/test_storage_paths.py
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
    """A fresh copy of `storage.py` loaded by path -- one per case, so bindings do not leak."""
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
        """cwd is elsewhere: whoever is named is the one read."""
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