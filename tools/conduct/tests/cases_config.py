"""`config`: one value, three places it can come from, and the order they resolve in.

A setting has exactly three possible homes -- the built-in table in the code, `~/.pf/config.json`
(global), and `<project>/.pf/config.json` (project) -- and the last layer that names a key wins.
What is worth a case here, because each fails silently otherwise:

  * no file anywhere reads as the built-in defaults, and the layer each value came from is reported
  * the project layer beats the global one, and `unset` lets the earlier layer show through again
  * `worktree_mode` has no constant default: it follows whether the project is a git checkout
  * a value that is off the table, on disk or on the command line, is refused loudly -- a broken
    setting must not read as "never configured"
  * a key this tool does not own survives a write untouched
  * `init` leaves a blank object at a discoverable path and never clobbers what was written into it
  * the global layer needs no project at all, and `pf` really forwards the verb

Every case points `PF_HOME_DIR` at its own sandbox: without that, a case would read (and one would
write) the machine's real `~/.pf/config.json`. `_child_env()` inherits it, so the child process the
CLI runs in sees the same one.
"""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import STATE_MANAGE, TOOLS_DIR, init_markers, run_argv  # noqa: E402

__all__ = ['TestConfig']

PF = TOOLS_DIR / 'bin' / 'pf.py'

#: `list` prints `key<pad> = value (layer)`; the layer is the human label, not the key.
LINE = re.compile(r'^(?P<key>[a-z_]+)\s+=\s+(?P<value>.+?)\s*\((?P<layer>.+?)\)$')


class TestConfig(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-config-')
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / 'proj'
        self.home = self.base / 'home'
        self.home.mkdir()
        # What `init` writes, and the only thing that says this `.pf/` was put there on purpose:
        # a write into the project layer is refused without them.
        self.ledger = init_markers(self.root)
        # The sandbox's own home, so `--global` can never reach the machine's real one.
        home = mock.patch.dict(os.environ, {'PF_HOME_DIR': str(self.home)})
        home.start()
        self.addCleanup(home.stop)

    # ── helpers ──────────────────────────────────────────────────────────────
    def state(self, *args, cwd=None):
        return run_argv([STATE_MANAGE, *args], cwd=cwd or self.root, check=False)

    def pf(self, *args, cwd=None):
        return run_argv([PF, *args], cwd=cwd or self.root, check=False)

    def effective(self, cwd=None):
        """`list`, read back as {key: (value, layer)} -- the tool's own answer, parsed."""
        proc = self.state('config', 'list', cwd=cwd)
        self.assertEqual(0, proc.returncode, proc.stderr)
        out = {}
        for line in proc.stdout.splitlines():
            m = LINE.match(line.strip())
            self.assertIsNotNone(m, f'`config list` printed a line this case cannot read: {line!r}')
            out[m.group('key')] = (m.group('value'), m.group('layer'))
        return out

    def set_(self, key, value, *extra, cwd=None):
        return self.state('config', 'set', key, value, *extra, cwd=cwd)

    def write_project(self, body: str):
        (self.ledger / 'config.json').write_text(body, encoding='utf-8')

    def read_project(self):
        return json.loads((self.ledger / 'config.json').read_text(encoding='utf-8'))

    # ── the three layers ─────────────────────────────────────────────────────
    def test_no_file_anywhere_reads_as_the_builtin_defaults(self):
        values = self.effective()
        self.assertEqual('3', values['max_agents'][0])
        self.assertEqual('full', values['test_tier'][0])
        # The sandbox is not a git checkout, so the conditional default lands on `off`.
        self.assertEqual('off', values['worktree_mode'][0])
        # A channel nobody has written anything about stays on: that is the regression this key lives
        # around -- a project that never wrote it behaves exactly as it did before the key existed.
        self.assertEqual('on', values['issue_repair'][0])
        self.assertEqual({'factory default'}, {layer for _, layer in values.values()},
                         'with no file anywhere, every value must be reported as the built-in one')

    def test_the_project_layer_wins_over_the_global_one(self):
        self.assertEqual(0, self.set_('max_agents', '5', '--global').returncode)
        self.assertEqual(0, self.set_('max_agents', '2').returncode)

        value, layer = self.effective()['max_agents']
        self.assertEqual('2', value)
        self.assertEqual('project .pf/config.json', layer)
        self.assertEqual({'max_agents': 5},
                         json.loads((self.home / '.pf' / 'config.json').read_text(encoding='utf-8')),
                         'writing the project layer must not disturb the global one')

    def test_unset_lets_the_earlier_layer_show_through(self):
        self.set_('max_agents', '5', '--global')
        self.set_('max_agents', '2')
        proc = self.state('config', 'unset', 'max_agents')
        self.assertEqual(0, proc.returncode, proc.stderr)

        value, layer = self.effective()['max_agents']
        self.assertEqual('5', value)
        self.assertEqual('global ~/.pf/config.json', layer)
        self.assertEqual({}, self.read_project(),
                         'the project layer is left as an empty object, not deleted')

    def test_worktree_mode_follows_the_git_checkout(self):
        self.assertEqual('off', self.effective()['worktree_mode'][0])
        (self.root / '.git').mkdir()
        self.assertEqual('fork', self.effective()['worktree_mode'][0],
                         'the same project, one `.git` later, changes its own default')
        (self.root / '.git').rmdir()
        self.assertEqual('off', self.effective()['worktree_mode'][0])

    # ── what the CLI prints ──────────────────────────────────────────────────
    def test_get_prints_the_value_and_nothing_else(self):
        proc = self.state('config', 'get', 'test_tier')
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual('full\n', proc.stdout)

        self.set_('test_tier', 'minimal')
        self.assertEqual('minimal\n', self.state('config', 'get', 'test_tier').stdout)

    def test_the_global_layer_needs_no_project_at_all(self):
        """The global layer travels with the person: standing somewhere with no `.pf/` should still
        allow a write and a read back."""
        self.assertFalse((self.base / '.pf').exists())
        proc = self.set_('max_agents', '4', '--global', cwd=self.base)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual('4\n', self.state('config', 'get', 'max_agents', cwd=self.base).stdout)
        self.assertFalse((self.base / '.pf').exists(),
                         'writing the global layer must not conjure a project ledger')

    def test_the_written_shape_is_a_flat_object_of_named_keys(self):
        self.set_('test_tier', 'minimal')
        self.assertEqual({'test_tier': 'minimal'}, self.read_project())

    def test_pf_forwards_the_verb(self):
        self.assertEqual('full\n', self.pf('config', 'get', 'test_tier').stdout)

    # ── refusals ─────────────────────────────────────────────────────────────
    def test_a_value_off_the_table_is_refused_before_anything_is_written(self):
        for key, value in (('max_agents', '0'), ('max_agents', 'three'),
                           ('max_agents', 'True'), ('test_tier', 'ful')):
            with self.subTest(key=key, value=value):
                proc = self.set_(key, value)
                self.assertNotEqual(0, proc.returncode, f'{key}={value} must be refused')
                self.assertIn(key, proc.stderr)
                self.assertFalse((self.ledger / 'config.json').exists(),
                                 'a refused write leaves no file behind')

    def test_a_value_off_the_table_on_disk_is_reported_not_swallowed(self):
        """A value written wrongly on disk must not quietly fall back to the default: that makes the
        key look "never configured" when it is in fact configured wrongly."""
        self.write_project('{"max_agents": "three"}')
        proc = self.state('config', 'get', 'max_agents')
        self.assertNotEqual(0, proc.returncode)
        self.assertIn('max_agents', proc.stderr)
        self.assertIn('config.json', proc.stderr, 'the complaint must name the file to go fix')

    def test_a_refusal_names_the_words_this_key_takes(self):
        """A refusal has to spell out the values it takes: naming that vocabulary in the error is how
        a human knows what to change it to."""
        proc = self.set_('issue_repair', 'maybe')

        self.assertNotEqual(0, proc.returncode)
        self.assertIn('issue_repair', proc.stderr)
        self.assertIn("['on', 'off']", proc.stderr)
        self.assertFalse((self.ledger / 'config.json').exists(),
                         'a refused write leaves no file behind')

    def test_a_quoted_number_on_disk_is_the_same_number(self):
        """`count` only asks "is this a positive integer": `"3"` on disk and 3 on disk go by the
        same rule.

        The command line hands over a string and the disk hands over a JSON value, and both inputs
        come through the same door -- a kind's boundary lives in one place, so what is let through
        here is exactly "the shape that writes back the same".
        """
        self.write_project('{"max_agents": "3"}')

        proc = self.state('config', 'get', 'max_agents')

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual('3\n', proc.stdout)

    def test_a_key_this_tool_does_not_own_survives_a_write(self):
        self.write_project('{"something_else": 1}')
        proc = self.state('config', 'list')
        self.assertEqual(0, proc.returncode, 'an unknown key is not fatal')
        self.assertIn('something_else', proc.stderr, 'it is worth one warning')

        self.set_('test_tier', 'minimal')
        self.assertEqual({'something_else': 1, 'test_tier': 'minimal'}, self.read_project(),
                         'the tool must not drop keys it does not own')

    def test_a_ledger_nobody_initialized_refuses_the_project_layer(self):
        raw = self.base / 'raw'
        (raw / '.pf').mkdir(parents=True)         # the shape, none of the markers
        proc = self.state('config', 'set', 'test_tier', 'minimal', cwd=raw)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn('init', proc.stderr, 'the refusal must hand over the command that fixes it')
        self.assertFalse((raw / '.pf' / 'config.json').exists())

    # ── init ─────────────────────────────────────────────────────────────────
    def test_init_leaves_a_blank_object_and_never_clobbers_it(self):
        fresh = self.base / 'fresh'
        fresh.mkdir()
        init = self.state('init', '--project', str(fresh), '--no-own-repo', cwd=fresh)
        self.assertEqual(0, init.returncode, init.stderr)
        blank = fresh / '.pf' / 'config.json'
        self.assertTrue(blank.is_file(), 'init puts the file where a reader will find it')
        self.assertEqual({}, json.loads(blank.read_text(encoding='utf-8')),
                         'blank on purpose: an empty object reads as "no overrides", while a copy '
                         'of the defaults would freeze today\'s values into this project')

        self.assertEqual(0, self.set_('test_tier', 'minimal', cwd=fresh).returncode)
        again = self.state('init', '--project', str(fresh), '--no-own-repo', cwd=fresh)
        self.assertEqual(0, again.returncode, again.stderr)
        self.assertEqual('minimal\n', self.state('config', 'get', 'test_tier', cwd=fresh).stdout,
                         're-running init is idempotent and must not wipe what was written')


if __name__ == '__main__':
    unittest.main()