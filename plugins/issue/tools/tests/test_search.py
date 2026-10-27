"""`search`: one regex cast over the pool; hits come back in three tiers, with the matched stretch
of the field cut out to look at.

The tier order is this output's one source: id/title first (the whole description comes with it),
then description/suggestion (a window of ±20 characters, and two hits in one field return the whole
text), then files/functions/module (the field that matched and its value). One entry enters one
tier only.

How to run (either works):

    python -m unittest discover -s plugins/issue/tools/tests -t plugins/issue/tools
    python plugins/issue/tools/tests/test_search.py
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
        self.root = Path(tempfile.mkdtemp(prefix='issue-search-'))
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

    def search(self, query: str, *extra: str) -> list:
        code, out, err = self.cli('search', query, *extra)
        self.assertEqual(code, 0, err or out)
        return json.loads(out)


class TestSearchTiers(PoolCase):

    def test_the_three_tiers_come_back_in_this_order(self):
        """One entry enters one tier only: id/title first, description/suggestion second,
        files/functions/module last."""
        self.add('needle-id', title='plain', description='nothing')
        self.add('title-hit', title='a needle in the title', description='nothing')
        self.add('desc-hit', title='plain', description='nothing needle here')
        self.add('file-hit', title='plain', description='nothing', files=['src/needle.py'])

        result = self.search('needle')

        self.assertEqual(['id', 'title', 'description', 'files'], [i['match'] for i in result])

    def test_a_title_hit_carries_the_whole_description(self):
        self.add('both', title='needle up here', description='needle down here too')

        hit = self.search('needle')[0]

        self.assertEqual('title', hit['match'])
        self.assertEqual('needle down here too', hit['description'])
        self.assertNotIn('snippet', hit)

    def test_a_description_hit_shows_a_window_of_twenty_characters_on_each_side(self):
        """The window is ±20 characters from the hit, and the `...` at either end marks the cut."""
        description = 'x' * 30 + 'needle' + 'y' * 30
        self.add('desc-hit', title='plain', description=description)

        hit = self.search('needle')[0]

        self.assertEqual('description', hit['match'])
        self.assertEqual('...' + description[10:50] + '...', hit['snippet'])

    def test_a_hit_at_the_very_start_has_no_leading_ellipsis(self):
        description = 'needle at the very start'
        self.add('desc-hit', title='plain', description=description)

        hit = self.search('needle')[0]

        self.assertEqual(description[:20] + '...', hit['snippet'])

    def test_two_hits_in_one_field_show_the_whole_text(self):
        self.add('twice', title='plain', description='needle one, needle two')

        hit = self.search('needle')[0]

        self.assertEqual('needle one, needle two', hit['snippet'])

    def test_a_suggestion_hit_names_that_field(self):
        self.add('sugg-hit', title='plain', description='nothing', suggestion='try a needle')

        hit = self.search('needle')[0]

        self.assertEqual('suggestion', hit['match'])
        self.assertEqual('try a needle', hit['snippet'])

    def test_a_module_hit_wins_over_the_files_of_the_same_entry(self):
        self.add('mod-hit', title='plain', module='needle-module', files=['src/needle.py'])

        hit = self.search('needle')[0]

        self.assertEqual('module', hit['match'])
        self.assertEqual('needle-module', hit['value'])

    def test_a_file_hit_reports_the_path_that_matched(self):
        self.add('file-hit', title='plain', files=['src/other.py', 'src/needle.py'])

        hit = self.search('needle')[0]

        self.assertEqual('files', hit['match'])
        self.assertEqual('src/needle.py', hit['value'])

    def test_a_function_hit_names_the_functions_field(self):
        self.add('func-hit', title='plain', functions=['_needle_helper'])

        hit = self.search('needle')[0]

        self.assertEqual('functions', hit['match'])
        self.assertEqual('_needle_helper', hit['value'])

    def test_the_query_ignores_case(self):
        self.add('title-hit', title='A NEEDLE in the title')

        self.assertEqual(['title-hit'], [i['id'] for i in self.search('needle')])


class TestSearchFilters(PoolCase):

    def test_a_search_can_be_narrowed_by_status(self):
        self.add('settled', title='a needle')
        self.add('held', title='a needle')
        code, out, err = self.cli('resolve', 'settled', '--reason', 'fixed')
        self.assertEqual(code, 0, err or out)

        self.assertEqual(['held', 'settled'], sorted(i['id'] for i in self.search('needle')))
        self.assertEqual(['held'], [i['id'] for i in self.search('needle', '--status', 'open')])

    def test_a_search_can_be_narrowed_by_severity(self):
        self.add('heavy', title='a needle', severity='P0')
        self.add('light', title='a needle', severity='P3')

        self.assertEqual(['heavy'], [i['id'] for i in self.search('needle', '--severity', 'P0')])

    def test_a_search_can_be_narrowed_by_source(self):
        self.add('from-scan', title='a needle', source='scan')
        self.add('from-manual', title='a needle', source='manual')

        self.assertEqual(['from-manual'],
                         [i['id'] for i in self.search('needle', '--source', 'manual')])

    def test_an_invalid_regex_stops_with_a_message(self):
        code, out, err = self.cli('search', 'a(')

        self.assertEqual(1, code)
        self.assertIn('invalid regex', err)


if __name__ == '__main__':
    unittest.main(verbosity=2)