"""Automatic ids and the id assignment of one batch pool entry: a duplicate inside the batch yields,
and a duplicate of an entry already in the pool is de-duplicated the same way.

An automatic id is "module + the first 40 characters of the title's slug", so in one scan several
rows with the same title prefix compute one id -- each of them is a real entry. This case watches
two things: a duplicate yields inside the batch (`-2`, `-3`, and the row is named in the output),
and "the pool already has this id" still takes the skip branch. The two situations have their own
answers and share no branch.

How to run (either works):

    python -m unittest discover -s issue/tests -t .      (cwd in tools/)
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
#: 50 x's: once the slug is cut to 40 characters, every one of these titles shares one prefix.
X = 'x' * 50
SLUG = 'x' * 40


def load_storage():
    """A copy of `storage.py` by path -- `_auto_id` is a pure function that touches no project."""
    spec = importlib.util.spec_from_file_location('issue_auto_id_case', STORAGE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAutoIdFromATitle(unittest.TestCase):
    """A title in, an id out."""

    def setUp(self):
        self.auto_id = load_storage()._auto_id

    def test_a_missing_or_empty_title_becomes_untitled(self):
        self.assertEqual('demo-untitled', self.auto_id({'module': 'demo'}))
        self.assertEqual('demo-untitled', self.auto_id({'module': 'demo', 'title': ''}))

    def test_a_title_with_no_ascii_word_characters_becomes_untitled(self):
        # A title of pure punctuation carries no ASCII word character, so the slug comes out empty.
        self.assertEqual('demo-untitled', self.auto_id({'module': 'demo', 'title': '!!!'}))
        self.assertEqual('unknown-untitled', self.auto_id({'title': '!!!'}))

    def test_a_long_title_stops_at_forty_characters(self):
        """Where duplicates come from: titles whose first 40 characters are equal compute one id."""
        self.assertEqual(f'demo-{SLUG}', self.auto_id({'module': 'demo', 'title': f'{X} alpha'}))
        self.assertEqual(self.auto_id({'module': 'demo', 'title': f'{X} alpha'}),
                         self.auto_id({'module': 'demo', 'title': f'{X} beta'}))


class BatchCase(unittest.TestCase):
    """A temp fake project: the ledger looks init-ed, and batch pool entry runs a real process."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='issue-auto-id-'))
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
        """An id written out explicitly inside the batch holds its name too: a later automatic
        one yields."""
        code, out, err = self.add_batch([{'id': 'demo-untitled', 'module': 'demo'},
                                         {'module': 'demo', 'title': '!!!'}])

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
        """Row 1's explicit id collides with the pool's entry → that row skips; row 2 computes the
        same id automatically and still yields."""
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