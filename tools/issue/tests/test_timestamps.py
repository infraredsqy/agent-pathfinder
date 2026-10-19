"""What the moment written into the pool looks like: `discovered` / `updated_at` both carry this
tree's one shape.

The shape's source is `tools/shared/clock.py`; the regex below hard-codes microseconds, so if the
source ever drops back to seconds the record loses its decimals along with it and the assertion here
goes red -- this case guards how fine the timestamps in the record are.

How to run (either works):

    python issue\tests\test_timestamps.py
    python -m unittest discover -s issue/tests -t .      (cwd in tools/)
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
#: UTC, microseconds, a literal `Z`. Hard-coded here, not read from `clock.ISO_FMT`: reading it
#: would let the assertion fall back with it once the source drops to seconds -- exactly the drift
#: this case exists to stop.
SHAPE = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z'


def load_clock():
    spec = importlib.util.spec_from_file_location('pf_shared_clock_case', TOOLS / 'shared' / 'clock.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


clock = load_clock()


class TimestampCase(unittest.TestCase):
    """An init-ed empty project: entries enter the pool through real commands, and records are read
    back from disk."""

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
        self.assertRegex(value, SHAPE, f"{label}'s timestamp shape is wrong: {value!r}")
        self.assertIsNotNone(datetime.strptime(value, clock.ISO_FMT),
                             f'source {clock.ISO_FMT!r} does not recognize {value!r}')


class TestTheRecordTimestamp(TimestampCase):

    def test_a_new_issue_carries_the_tree_timestamp_shape(self):
        self.run_tool('add', '--id', 'one', '--title', 'one slot')

        row = self.row('one')

        for key in ('discovered', 'updated_at'):
            with self.subTest(key=key):
                self.check(row[key], key)

    def test_a_later_write_keeps_the_same_shape(self):
        self.run_tool('add', '--id', 'one', '--title', 'one slot')

        self.run_tool('resolve', 'one', '--reason', 'done')

        self.check(self.row('one')['updated_at'], 'updated_at')


if __name__ == '__main__':
    unittest.main(verbosity=2)