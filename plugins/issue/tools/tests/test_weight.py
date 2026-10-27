"""`weight`: how heavy the pool is, one number, for the pipeline to read.

pf-conduct compares it with a threshold the pipeline configures for the line when a node closes out.
Two things fix the number: only actionable entries count (the same reading as `status`), and
`decision: external` ones stay out -- the call is the human's, so counting them would be a floor
nobody can move and the threshold could never come back under the line.

How to run (either works):

    python -m unittest discover -s plugins/issue/tools/tests -t plugins/issue/tools
    python plugins/issue/tools/tests/test_weight.py
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
    """One real command: argv in, exit code and both output streams out."""
    proc = subprocess.run([sys.executable, str(TOOL), *argv], cwd=str(project),
                          capture_output=True, text=True, encoding='utf-8', errors='replace')
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


class PoolCase(unittest.TestCase):
    """An empty project: the ledger is laid out init-ed first, and entries go in through the cmd
    pool door."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='issue-weight-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # The two marker files are the proof that "this ledger has been init-ed": a write command
        # stops without them and asks you to run state-manage.py init. This fixture only wants an
        # empty pool, so they are laid out here.
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
        """`acknowledge` records the entry as "seen, deferred" -- the pool still owes it, so it
        weighs."""
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