"""`config`: one knob on one chain, and the layer the value in force came from.

A setting is a **knob** with a kind -- `number`, `integer`, `word`, `switch` -- and it lives on one
chain, narrowest first:

    node -> this line -> this project -> this machine -> factory default

What is worth a case here, because each fails silently otherwise:

  * no file anywhere reads as the factory defaults, and every value says that is where it came from
  * the whole chain resolves narrowest first, and `unset` lets the next wider layer show through
  * `set` / `unset` write exactly one layer: `--global`, `--cycle <id>` and `--node <id>` each land in
    their own store and leave the others untouched -- the cycle layer rides on the cycle record, the
    node layer is `<project>/.pf/nodes/<id>/config.json`
  * which layers a knob takes is its own business: a core setting takes the project and global layers
    only, while a declared knob takes all four unless its own `layers` cell names a narrower set -- a
    write anywhere else is refused, naming the layers it does take
  * the line's own policies (`parallel` / `when_shelved` / `spawner`) are not settings at all: this
    command refuses them and `list` does not claim them (they live on the line record, written by
    `pf cycle policy`)
  * `list` prints `key = value (layer)`; `get` prints the value alone (that one line is what a skill
    reads), and `-` when no layer states it and there is no factory value
  * a kind's boundary is refused loudly -- on the command line and off disk -- so a broken setting
    never reads as "never configured"; the refusal names the key and what the kind takes
  * `off` is a value for a switch and a refused word for the other kinds (`unset` is how an entry is
    dropped)
  * `worktree_mode` has no constant default: it follows whether the project is a git checkout
  * a key this tool does not own survives a write untouched
  * `init` leaves a blank object at a discoverable path and never clobbers what was written into it
  * the global layer needs no project at all, and `pf` really forwards the verb

The number and switch kinds have no built-in member: they arrive as a **plugin's declaration**, so
this file installs a throwaway one under `PF_PLUGINS_DIR` rather than leaning on whatever plugins
this checkout happens to ship. The suite has to stay green with `plugins/` empty, so nothing here
names a shipped plugin or one of its knobs.

Every case points `PF_HOME_DIR` at its own sandbox: without that, a case would read (and one would
write) the machine's real `~/.pf/config.json`. `_child_env()` inherits both variables, so the child
process the CLI runs in sees the same sandbox.
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

#: A node id the graph knows nothing about: the node layer is one run's own file, not a DAG lookup.
NODE = 'deadbeef-0000-1111-2222-333344445555'


def _install_feature(plugins: Path) -> None:
    """Put a throwaway plugin under `plugins/`, declaring the two kinds the built-ins lack.

    A plugin is a folder: a declaration (data) and an implementation (code). Nothing here runs the
    implementation -- what is wanted is only that its knobs exist, one `number` and one `switch`, so
    the four kinds this file pins are all reachable through the same merged table.
    """
    folder = plugins / 'sandbox'
    folder.mkdir(parents=True)
    (folder / 'impl.py').write_text('def offer(ctx):\n    return None\n', encoding='utf-8')
    (folder / 'plugin.json').write_text(json.dumps({
        'name': 'sandbox',
        'timing': 'close',
        'action': 'insert',
        'knobs': {
            'sandbox_on': {'kind': 'switch', 'default': 'on'},
            'sandbox_number': {'kind': 'number'},
        },
    }), encoding='utf-8')


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
        # The two non-built-in kinds arrive from a plugin's declaration; a sandbox one keeps this
        # file independent of the plugins this checkout ships.
        self.plugins = self.base / 'plugins'
        _install_feature(self.plugins)
        env = mock.patch.dict(os.environ, {'PF_HOME_DIR': str(self.home),
                                           'PF_PLUGINS_DIR': str(self.plugins)})
        env.start()
        self.addCleanup(env.stop)

    # ── helpers ──────────────────────────────────────────────────────────────
    def state(self, *args, cwd=None):
        return run_argv([STATE_MANAGE, *args], cwd=cwd or self.root, check=False)

    def pf(self, *args, cwd=None):
        return run_argv([PF, *args], cwd=cwd or self.root, check=False)

    def effective(self, *context, cwd=None):
        """`list`, read back as {key: (value, layer)} -- the tool's own answer, parsed.

        `context` is the read context a `list` may carry (`--cycle <id>` / `--node <id>`): the narrow
        layers only join the resolution when they are named.
        """
        proc = self.state('config', 'list', *context, cwd=cwd)
        self.assertEqual(0, proc.returncode, proc.stderr)
        out = {}
        for line in proc.stdout.splitlines():
            m = LINE.match(line.strip())
            self.assertIsNotNone(m, f'`config list` printed a line this case cannot read: {line!r}')
            out[m.group('key')] = (m.group('value'), m.group('layer'))
        return out

    def set_(self, key, value, *extra, cwd=None):
        return self.state('config', 'set', key, value, *extra, cwd=cwd)

    def unset(self, key, *extra, cwd=None):
        return self.state('config', 'unset', key, *extra, cwd=cwd)

    def write_project(self, body: str):
        (self.ledger / 'config.json').write_text(body, encoding='utf-8')

    def read_project(self):
        return self.read_file(self.ledger / 'config.json')

    def read_file(self, path: Path):
        self.assertTrue(path.is_file(), f'{path} is not there')
        return json.loads(path.read_text(encoding='utf-8'))

    def read_state(self):
        return self.read_file(self.ledger / 'state.json')

    # ── the chain ────────────────────────────────────────────────────────────
    def test_no_file_anywhere_reads_as_the_factory_defaults(self):
        values = self.effective()
        self.assertEqual('3', values['max_agents'][0])
        self.assertEqual('full', values['test_tier'][0])
        # The sandbox is not a git checkout, so the conditional default lands on `off`.
        self.assertEqual('off', values['worktree_mode'][0])
        # The declared pair: the switch's own default is a value, the number's is "not configured".
        self.assertEqual('on', values['sandbox_on'][0])
        self.assertEqual('-', values['sandbox_number'][0])
        self.assertEqual({'factory default'}, {layer for _, layer in values.values()},
                         'with no file anywhere, every value must be reported as the factory one')

    def test_the_line_policies_are_not_settings(self):
        """`parallel` / `when_shelved` / `spawner` live on the line record: this command refuses them
        (as it did before the chain grew) and `list` does not claim a layer for them."""
        values = self.effective()
        for key in ('parallel', 'when_shelved', 'spawner'):
            with self.subTest(key=key):
                refused = self.set_(key, '2')
                self.assertNotEqual(0, refused.returncode, f'{key} was accepted as a setting')
                self.assertIn(key, refused.stderr)
                self.assertNotIn(key, values, '`list` claims a layer for a value nobody reads')
        self.assertFalse((self.ledger / 'config.json').exists(),
                         'a refused write leaves no file behind')

    def test_a_setting_takes_the_two_wide_layers_only(self):
        """`--node` and `--cycle` are refused for a core setting, naming the layers it does take: a
        value there would be one nothing could ever read."""
        self.assertEqual(0, self.state('cycle', 'create', 'c1', '--tags', 'coding').returncode)
        for extra in (('--cycle', 'c1'), ('--node', NODE)):
            with self.subTest(layer=extra[0]):
                refused = self.set_('max_agents', '4', *extra)
                self.assertNotEqual(0, refused.returncode, f'max_agents accepted {extra[0]}')
                self.assertIn('max_agents', refused.stderr)
                self.assertIn('project, global', refused.stderr,
                              'the refusal names the layers the knob takes')
        self.assertFalse((self.ledger / 'config.json').exists(), 'a refused write wrote a file')
        self.assertEqual({}, self.read_state()['cycles'][0].get('policies') or {},
                         'a refused cycle-layer write still landed on the record')

    def test_a_declared_knob_takes_only_the_layers_its_declaration_names(self):
        """A declaration may narrow its own knob (`layers: ["project"]`): the write side obeys that
        cell, so `--node` and `--cycle` are refused naming what it takes, and `--project` lands."""
        folder = self.plugins / 'narrow'
        folder.mkdir()
        (folder / 'impl.py').write_text('def offer(ctx):\n    return None\n', encoding='utf-8')
        (folder / 'plugin.json').write_text(json.dumps({
            'name': 'narrow', 'timing': 'close', 'action': 'insert',
            'knobs': {'narrow_limit': {'kind': 'number', 'layers': ['project']}},
        }), encoding='utf-8')

        for extra in (('--cycle', 'c1'), ('--node', NODE)):
            with self.subTest(layer=extra[0]):
                refused = self.set_('narrow_limit', '5', *extra)
                self.assertNotEqual(0, refused.returncode, f'narrow_limit accepted {extra[0]}')
                self.assertIn('narrow_limit', refused.stderr)
                self.assertIn('does not take the', refused.stderr)
                self.assertIn('it takes: project', refused.stderr,
                              'the refusal names the one layer the declaration kept')
        self.assertFalse((self.ledger / 'config.json').exists(), 'a refused write wrote a file')

        accepted = self.set_('narrow_limit', '5')
        self.assertEqual(0, accepted.returncode, accepted.stderr)
        self.assertEqual(('5', 'project .pf/config.json'), self.effective()['narrow_limit'])

    def test_the_whole_chain_resolves_narrowest_first(self):
        """Four layers, one key: each write covers the wider ones, and each `unset` steps back.

        The key is the **declared** number knob: a knob a plugin declares takes every layer, which
        is what the chain is for. (A core setting takes the two wide ones, and the case above pins
        that.)
        """
        self.assertEqual(0, self.set_('sandbox_number', '5', '--global').returncode)

        self.assertEqual(0, self.set_('sandbox_number', '4').returncode)
        self.assertEqual(('4', 'project .pf/config.json'), self.effective()['sandbox_number'])

        self.assertEqual(0, self.state('cycle', 'create', 'c1', '--tags', 'coding').returncode)
        self.assertEqual(0, self.set_('sandbox_number', '2', '--cycle', 'c1').returncode)
        self.assertEqual(('2', 'cycle c1'), self.effective('--cycle', 'c1')['sandbox_number'])

        self.assertEqual(0, self.set_('sandbox_number', '7', '--node', NODE).returncode)
        self.assertEqual(('7', f'node {NODE[:8]}'),
                         self.effective('--node', NODE)['sandbox_number'])

        self.assertEqual(0, self.unset('sandbox_number', '--node', NODE).returncode)
        self.assertEqual(('2', 'cycle c1'), self.effective('--cycle', 'c1')['sandbox_number'])
        self.assertEqual(0, self.unset('sandbox_number', '--cycle', 'c1').returncode)
        self.assertEqual(('4', 'project .pf/config.json'), self.effective()['sandbox_number'])
        self.assertEqual(0, self.unset('sandbox_number').returncode)
        self.assertEqual(('5', 'global ~/.pf/config.json'), self.effective()['sandbox_number'])
        self.assertEqual(0, self.unset('sandbox_number', '--global').returncode)
        self.assertEqual(('-', 'factory default'), self.effective()['sandbox_number'],
                         'a knob with no factory value steps back to "not configured"')

    def test_a_write_lands_on_exactly_one_layer(self):
        """`set` names one layer and writes it; everything else stays as it was."""
        self.assertEqual(0, self.set_('max_agents', '4').returncode)
        written = self.set_('max_agents', '5', '--global')
        self.assertEqual(0, written.returncode, written.stderr)
        self.assertEqual({'max_agents': 5}, self.read_file(self.home / '.pf' / 'config.json'))
        self.assertEqual({'max_agents': 4}, self.read_project(),
                         'writing the global layer must not disturb the project one')
        self.assertIn('effective max_agents = 4 (from project .pf/config.json)', written.stdout,
                      'a write that is not in force must say so, and say what is in force')

        self.assertEqual(0, self.set_('sandbox_number', '7', '--node', NODE).returncode)
        self.assertEqual({'sandbox_number': 7}, self.read_file(self.ledger / 'nodes' / NODE
                                                               / 'config.json'))
        self.assertEqual({'max_agents': 4}, self.read_project(),
                         'writing the node layer must not disturb the project one')

        self.assertEqual(0, self.state('cycle', 'create', 'c1', '--tags', 'coding').returncode)
        self.assertEqual(0, self.set_('sandbox_number', '2', '--cycle', 'c1').returncode)
        self.assertEqual({'sandbox_number': 2}, self.read_state()['cycles'][0]['policies'],
                         'the cycle layer rides on the record, whose one writer takes the value')
        self.assertEqual({'max_agents': 4}, self.read_project(),
                         'writing the cycle layer must not open the project layer')

    def test_the_node_layer_is_one_nodes_own_file(self):
        """`<project>/.pf/nodes/<id>/config.json`: a file, not a record lookup -- an id the graph does
        not know still has a layer, and it is read only when that node is named."""
        self.assertFalse((self.ledger / 'nodes' / NODE).exists())
        proc = self.set_('sandbox_number', '7', '--node', NODE)
        self.assertEqual(0, proc.returncode, proc.stderr)

        path = self.ledger / 'nodes' / NODE / 'config.json'
        self.assertTrue(path.is_file(), 'the node layer is not where a reader looks for it')
        self.assertEqual({'sandbox_number': 7}, self.read_file(path))
        self.assertEqual('-\n', self.state('config', 'get', 'sandbox_number').stdout,
                         'the node layer must not join a reading that names no node')
        self.assertEqual('7\n',
                         self.state('config', 'get', 'sandbox_number', '--node', NODE).stdout)

    def test_the_cycle_layer_rides_on_the_cycle_record(self):
        self.assertEqual(0, self.state('cycle', 'create', 'c1', '--tags', 'coding').returncode)
        self.assertEqual(0, self.set_('sandbox_number', '2', '--cycle', 'c1').returncode)

        self.assertEqual({'sandbox_number': 2},
                         self.read_state()['cycles'][0].get('policies'))
        self.assertFalse((self.ledger / 'config.json').exists(),
                         'a cycle-layer write must not create the project layer')
        self.assertEqual('2\n',
                         self.state('config', 'get', 'sandbox_number', '--cycle', 'c1').stdout)

        self.assertEqual(0, self.unset('sandbox_number', '--cycle', 'c1').returncode)
        self.assertEqual({}, self.read_state()['cycles'][0].get('policies') or {},
                         'unset drops the entry, it does not write the default down')

    def test_an_unknown_cycle_id_is_refused(self):
        """A read with that line in context and a write into it both need the line to exist."""
        for verb in (('list',), ('set', 'sandbox_number', '2')):
            with self.subTest(verb=verb):
                proc = self.state('config', *verb, '--cycle', 'nope')
                self.assertNotEqual(0, proc.returncode, f'{" ".join(verb)} accepted an unknown id')
                self.assertIn('nope', proc.stderr)

    def test_the_four_kinds_are_settable_and_readable(self):
        """One knob per kind, one write each, and one reading that gives every value with its layer."""
        cases = (('max_agents', '4', 'integer'), ('test_tier', 'minimal', 'word'),
                 ('sandbox_number', '12.5', 'number'), ('sandbox_on', 'off', 'switch'))
        for key, value, kind in cases:
            with self.subTest(kind=kind, key=key):
                proc = self.set_(key, value)
                self.assertEqual(0, proc.returncode, proc.stderr)
        values = self.effective()
        for key, value, kind in cases:
            with self.subTest(read_back=kind):
                self.assertEqual((value, 'project .pf/config.json'), values[key])
        self.assertEqual({'max_agents': 4, 'test_tier': 'minimal',
                          'sandbox_number': 12.5, 'sandbox_on': 'off'}, self.read_project(),
                         'each kind is written as its own JSON shape, not as the string typed')

    def test_get_prints_the_value_and_nothing_else(self):
        self.assertEqual(0, self.set_('test_tier', 'minimal').returncode)
        proc = self.state('config', 'get', 'test_tier')
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual('minimal\n', proc.stdout)
        self.assertEqual('-\n', self.state('config', 'get', 'sandbox_number').stdout,
                         'a knob with no value and no factory value prints the dash')

    def test_worktree_mode_follows_the_git_checkout(self):
        self.assertEqual('off', self.effective()['worktree_mode'][0])
        (self.root / '.git').mkdir()
        self.assertEqual('fork', self.effective()['worktree_mode'][0],
                         'the same project, one `.git` later, changes its own default')
        (self.root / '.git').rmdir()
        self.assertEqual('off', self.effective()['worktree_mode'][0])

    # ── what the CLI prints / where it writes ────────────────────────────────
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
        self.assertEqual(0, self.set_('test_tier', 'minimal').returncode)
        self.assertEqual({'test_tier': 'minimal'}, self.read_project())

    def test_pf_forwards_the_verb(self):
        self.assertEqual('full\n', self.pf('config', 'get', 'test_tier').stdout)

    # ── refusals ─────────────────────────────────────────────────────────────
    def test_a_value_the_kind_refuses_is_refused_before_anything_is_written(self):
        refusals = (('max_agents', '0'), ('max_agents', 'three'), ('max_agents', 'True'),
                    ('test_tier', 'ful'), ('test_tier', 'off'), ('sandbox_on', 'maybe'),
                    ('sandbox_number', '-2'))
        for key, value in refusals:
            with self.subTest(key=key, value=value):
                proc = self.set_(key, value)
                self.assertNotEqual(0, proc.returncode, f'{key}={value} must be refused')
                self.assertIn(key, proc.stderr)
                self.assertFalse((self.ledger / 'config.json').exists(),
                                 'a refused write leaves no file behind')

    def test_an_unknown_key_is_refused(self):
        proc = self.set_('nope', '1')
        self.assertNotEqual(0, proc.returncode)
        self.assertIn('nope', proc.stderr)
        self.assertIn('choose from', proc.stderr, 'the refusal lists the keys that do exist')
        self.assertFalse((self.ledger / 'config.json').exists(),
                         'a refused write leaves no file behind')

    def test_a_refusal_names_the_words_the_key_takes(self):
        """A refusal has to spell out the values it takes: naming that vocabulary in the error is how
        a human knows what to change it to."""
        word = self.set_('test_tier', 'ful')
        self.assertNotEqual(0, word.returncode)
        self.assertIn("['full', 'minimal']", word.stderr)

        switch = self.set_('sandbox_on', 'maybe')
        self.assertNotEqual(0, switch.returncode)
        self.assertIn('on or off', switch.stderr)

    def test_a_value_off_the_table_on_disk_is_reported_not_swallowed(self):
        """A value written wrongly on disk must not quietly fall back to the default: that makes the
        key look "never configured" when it is in fact configured wrongly."""
        self.write_project('{"max_agents": "three"}')
        proc = self.state('config', 'get', 'max_agents')
        self.assertNotEqual(0, proc.returncode)
        self.assertIn('max_agents', proc.stderr)
        self.assertIn('config.json', proc.stderr, 'the complaint must name the file to go fix')

    def test_a_quoted_number_on_disk_is_the_same_number(self):
        """`integer` only asks "is this a positive whole number": `"3"` on disk and 3 on disk go by
        the same rule.

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

        self.assertEqual(0, self.set_('test_tier', 'minimal').returncode)
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
