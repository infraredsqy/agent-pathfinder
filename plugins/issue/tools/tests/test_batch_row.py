"""The meaning of one row's values in a batch file: `null` on one of the four gate keys stops the
whole batch, `null` on one of the five content keys takes the default.

`null` in a row and "this key is not written" are not the same thing, and the outcome splits by what
the key governs:

    severity / decision / files / functions    governs whether this row may enter the pool -- `null`
                                               is a write error: the batch exits 1, names that row,
                                               and not one entry lands
    title / module / source / description      content keys -- `null` means the same as not writing
    / suggestion                               the key, so the default is taken
    reason / plan_path                         the default is `null` already; both spellings settle
                                               to one value

The command-line door does not split: a flag `argparse` did not get is "not given", and its value
equals the "key absent" column here.

How to run (either works):

    python -m unittest discover -s plugins/issue/tools/tests -t plugins/issue/tools
    python plugins/issue/tools/tests/test_batch_row.py
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

#: How an entry with nothing written at all lands, the two timestamp keys aside.
DEFAULTS = {'severity': 'P3', 'decision': 'unknown', 'files': [], 'functions': [],
            'title': 'x', 'module': '', 'source': 'scan', 'description': '', 'suggestion': '',
            'reason': None, 'plan_path': None}


class BatchCase(unittest.TestCase):
    """A temp fake project: the ledger looks init-ed, and batch pool entry runs a real process."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='issue-row-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # The two marker files are the proof that "this ledger has been init-ed": a write command
        # stops without them.
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
    """`null` is a write error: the batch stops, no default papers over it, and no entry lands."""

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
    """`null` on a content key means the same as not writing that key."""

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
        """The command-line door's values equal the "key absent" column -- title alone takes its
        own id."""
        code, out, err = self.cli('add', '--id', 'x')
        self.assertEqual(code, 0, err or out)

        code, out, err = self.add_batch([{'id': 'y'}])
        self.assertEqual(code, 0, err or out)

        pool = self.pool()
        self.assertEqual(dict(DEFAULTS, title='x'), self.picked(pool['x']))
        self.assertEqual(dict(DEFAULTS, title='y'), self.picked(pool['y']))


if __name__ == '__main__':
    unittest.main(verbosity=2)