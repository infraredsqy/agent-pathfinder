"""`mark`: the one record a live scan confirms -- it only adds check_count and only reports
candidates, and changes not one status.

The one source for the protocol "mark only records, it does not close out; closing out goes through
resolve" is here: the candidate set is "the open entries this scan did not confirm" (advisory), and
unknown is an id that was reported but is not in the pool; neither touches any entry's status.

How to run (either works):

    python -m unittest discover -s issue/tests -t .      (cwd in tools/)
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
    """A temp fake project: the ledger looks init-ed, and the command runs a real process."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='issue-mark-'))
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
        """Ids reported twice in one call are one set: one scan confirms once."""
        self.add('a')

        self.mark('a', 'a')

        self.assertEqual(2, self.pool()['a']['check_count'])

    def test_an_empty_argument_is_dropped(self):
        """An empty string left by shell quoting is no id, and does not go into unknown either."""
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
        """mark moves no status: an `open` entry stays `open`, waiting for the resolve path to
        close it."""
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
        """Candidates answer "should this be closed out?": the deferred one has been looked at
        already."""
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