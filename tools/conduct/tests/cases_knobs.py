"""The knob layer itself: the four kinds, the table the declarations merge into, the chain, and which
readings a worker is asked for. In process -- a knob's boundary and a layer's precedence are what is
measured, so no CLI is needed and no project is advanced.

What each group pins, because each fails silently otherwise:

  * `parse` -- one value of one kind, read side and write side through the same door. A boolean is
    never a number (JSON's spelling of a decision is not 1), an integer is positive and whole, a
    number normalises an integral float and refuses the non-finite, a word is one of its vocabulary
    when it names one, a switch is exactly `on` / `off`. `parse` states what a value **is**; the
    positive-value rule the CLI adds belongs to the writer, so `-1` is a number here.
  * `specs` -- the built-in settings plus every installed plugin's declaration, merged once. A
    built-in name keeps its own cell (a plugin may not redefine what the core's settings mean), a
    declaration whose kind is outside the four is not a knob, and `shadowed()` is where a
    declaration that claimed a built-in name is reported (the built-in cell still wins).
  * `layers` -- which override layers a knob takes is the knob's own cell: a core setting takes the
    two wide ones (project, global), a declared knob takes all four, and a value hand-written into a
    layer its knob does not take is reported when that file is read, never folded in.
  * `resolve` -- the chain, narrowest first (node -> cycle -> project -> global -> factory default),
    and every reading is a pair: the effective value and the layer it came from. A file layer that
    states something unreadable stops the call; a cycle record that does is simply "not stated".
  * `ticket_readings` -- only a `ticket` reading is asked of a worker, and only when its own
    `when_knob` has a value in force on the chain, the line's own policies included: a layer states
    it, or the declaration gives it a factory default (a threshold with neither must not be measured
    for nothing).
  * `PF_PLUGINS_DIR` -- the scan follows that variable and caches nothing, so a plugin installed or
    taken away inside one process shows up (or disappears) on the next call. The `POLICY_*` tables
    are the one import-time snapshot on this road, so nothing here asks them.

Every case builds its own throwaway plugins directory and points `PF_PLUGINS_DIR` at it: the
shipped tree's own plugins are never asserted, because the suite has to stay green with `plugins/`
empty. `PF_HOME_DIR` is a sandbox for the same reason -- the machine's real `~/.pf/config.json` must
never be read.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import CONDUCT_DIR, STATE_MANAGE, _child_env, init_markers, run_argv, \
    shared_loader  # noqa: E402

__all__ = ['TestKnobKinds', 'TestKnobTable', 'TestKnobChain', 'TestKnobTicketReadings',
           'TestKnobFeaturesDir']

STATE_DIR = CONDUCT_DIR / 'state'
#: The settings the core itself reads. Stated here as the expectation, never asked of the tool. The
#: line's own three policies (`parallel` / `when_shelved` / `spawner`) are not settings: they live on
#: the cycle record, and `cases_node` / `cases_parallel_slots` are where they are pinned.
SETTING_NAMES = {'max_agents', 'test_tier', 'worktree_mode'}
#: A node id the graph knows nothing about: the node layer is one run's own file, not a DAG lookup.
NODE = 'deadbeef-0000-1111-2222-333344445555'


def knobs():
    """`state/knobs.py`, through the package it lives in -- loaded by path, like every caller.

    `reuse` hands back the one copy this process already has, so the tables are the ones every other
    in-process caller sees; the module caches no scan of its own, which is what lets a test install a
    plugin inside the process and read it back.
    """
    package = shared_loader().load_by_path('pf_state_knobs_case', STATE_DIR / '__init__.py',
                                           package_dir=STATE_DIR, register=True, reuse=True)
    return package.knobs


def write_plugin(plugins: Path, name: str, declaration: dict, impl: bool = True) -> Path:
    """One plugin folder under `plugins/`: its declaration, and (by default) a trivial impl."""
    folder = plugins / name
    folder.mkdir(parents=True)
    (folder / 'plugin.json').write_text(json.dumps(declaration), encoding='utf-8')
    if impl:
        (folder / 'impl.py').write_text(
            'def offer(ctx):\n    return None\n\n\ndef check(ctx):\n    return None\n',
            encoding='utf-8')
    return folder


def auditor_declaration(**knobs_extra) -> dict:
    """A well-formed plugin: one number knob, one metric, and three readings of the two sources."""
    declaration = {
        'name': 'auditor',
        'timing': 'close',
        'action': 'insert',
        'knobs': {'audit_limit': {'kind': 'number'}},
        'readings': {
            'audit_minutes': {'source': 'ticket', 'when_knob': 'audit_limit',
                              'ask': 'the minutes a full audit took'},
            'always_asked': {'source': 'ticket', 'ask': 'the number this one always wants'},
            'pool_weight': {'source': 'pool'},
        },
        'metrics': {
            'audit_minutes': {'line': 'audit_limit', 'tag': 'audit:minutes',
                              'method': 'pf-audit', 'order': 0},
        },
    }
    declaration['knobs'].update(knobs_extra)
    return declaration


class _Sandbox(unittest.TestCase):
    """A throwaway project, home and plugins directory, and the two variables pointed at them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-knobs-')
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / 'proj'
        self.root.mkdir()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.ledger = init_markers(self.root)
        self.plugins = self.base / 'plugins'
        self.plugins.mkdir()
        env = mock.patch.dict(os.environ, {'PF_HOME_DIR': str(self.home),
                                           'PF_PLUGINS_DIR': str(self.plugins)})
        env.start()
        self.addCleanup(env.stop)

    # ── helpers ──────────────────────────────────────────────────────────────
    def write_project(self, values: dict):
        (self.ledger / 'config.json').write_text(json.dumps(values), encoding='utf-8')

    def write_node(self, values: dict):
        folder = self.ledger / 'nodes' / NODE
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'config.json').write_text(json.dumps(values), encoding='utf-8')

    def state(self, *args):
        return run_argv([STATE_MANAGE, *args], cwd=self.root, check=False)

    def cli(self, *args, plugins: Path | None = None):
        env = _child_env()
        env['PF_PLUGINS_DIR'] = str(self.plugins if plugins is None else plugins)
        return run_argv([STATE_MANAGE, *args], cwd=self.root, env=env, check=False)


class TestKnobKinds(unittest.TestCase):
    """`parse`: one value of one kind, or None -- the one boundary both sides of a knob come through."""

    def test_integer_takes_a_positive_whole_number(self):
        for value, expected in ((3, 3), ('3', 3), (' 3 ', 3), ('three', None), (0, None),
                                (-1, None), (3.5, None), ('3.5', None),
                                (True, None), (False, None), (None, None)):
            with self.subTest(value=value):
                self.assertEqual(expected, knobs().parse('integer', value),
                                 'a boolean is the JSON spelling of a decision, never 1')

    def test_number_takes_a_decimal_and_normalises_an_integral_float(self):
        for value, expected in ((3, 3), ('3', 3), (2.0, 2), ('2.0', 2), (3.5, 3.5),
                                (' 3.5 ', 3.5), (True, None), ('three', None), (None, None),
                                (float('inf'), None), (float('nan'), None)):
            with self.subTest(value=value):
                self.assertEqual(expected, knobs().parse('number', value))

        # The positive-value rule is the writing command line's, not the kind's: `parse` says what a
        # number IS, so a negative one is a number and the refusal happens where it is written.
        self.assertEqual(-1, knobs().parse('number', -1))

    def test_word_is_one_of_its_vocabulary_when_it_names_one(self):
        parse = knobs().parse
        self.assertEqual('full', parse('word', 'full', ('full', 'minimal')))
        self.assertEqual('minimal', parse('word', 'minimal', ('full', 'minimal')))
        self.assertIsNone(parse('word', 'ful', ('full', 'minimal')))
        self.assertIsNone(parse('word', 'FULL', ('full', 'minimal')), 'a vocabulary is literal')
        self.assertEqual('anything', parse('word', 'anything'),
                         'a word knob that names no vocabulary takes any word')
        self.assertIsNone(parse('word', 3))

    def test_switch_is_on_or_off_and_nothing_else(self):
        parse = knobs().parse
        self.assertEqual('on', parse('switch', 'on'))
        self.assertEqual('off', parse('switch', 'off'))
        for value in ('ON', 'true', True, 1, '', None, 'maybe'):
            with self.subTest(value=value):
                self.assertIsNone(parse('switch', value))

    def test_a_kind_outside_the_four_is_not_a_knob(self):
        self.assertIsNone(knobs().parse('colour', 'red'))


class TestKnobTable(_Sandbox):
    """`specs`: the built-in settings, with the declarations merged in and the built-in cell kept."""

    def test_the_builtin_settings_are_the_core_own_three(self):
        """With no plugin folder installed the table is exactly the three settings the core reads
        itself: the declarations add knobs, they do not have to be there for the core to work.

        The line's own three policies are not in it: they are the cycle record's, not settings.
        """
        table = knobs().specs()
        self.assertEqual(SETTING_NAMES, set(table))
        for name in ('parallel', 'when_shelved', 'spawner'):
            self.assertNotIn(name, table, f'{name} is a line policy, not a setting')

    def test_a_setting_takes_the_two_wide_layers_and_a_declared_knob_takes_all_four(self):
        """Which layers a knob takes is its own cell: the settings are how this project and this
        machine behave, while a declared knob is read through the whole chain by the plugin host."""
        self.assertEqual(('project', 'global'), knobs().layers_of(knobs().specs()['max_agents']))
        write_plugin(self.plugins, 'auditor', auditor_declaration())
        self.assertEqual(knobs().LAYERS, knobs().layers_of(knobs().specs()['audit_limit']))

    def test_a_declaration_can_narrow_its_own_knob_to_one_layer(self):
        """A `layers` cell in the declaration is copied into the merged cell, so a knob a plugin
        wants on the project layer alone is not writable (or readable) anywhere else; and the layer
        names the registry checks against are the chain's own -- the registry restates them because
        it is loaded by path from two packages, so the two spellings are pinned together here."""
        write_plugin(self.plugins, 'auditor',
                      auditor_declaration(audit_limit={'kind': 'number', 'layers': ['project']}))
        self.assertEqual(('project',), knobs().layers_of(knobs().specs()['audit_limit']))
        self.assertEqual(knobs().LAYERS, knobs()._registry().LAYERS)

    def test_a_declaration_adds_its_knobs(self):
        write_plugin(self.plugins, 'auditor', auditor_declaration())
        specs = knobs().specs()
        self.assertIn('audit_limit', specs)
        self.assertEqual('number', specs['audit_limit']['kind'])
        self.assertEqual('auditor', specs['audit_limit']['plugin'],
                         'the merged cell says who declared it')

    def test_a_feature_cannot_redefine_a_builtin_name(self):
        """The built-in cell is the core's own statement of what that setting means: a declaration
        that claims the name is refused here (the merge is where the precedence lives) and reported
        by `shadowed()`, which is the one door that sees both tables -- the doctor prints it."""
        write_plugin(self.plugins, 'auditor',
                      auditor_declaration(max_agents={'kind': 'word', 'values': ['one', 'many']}))
        spec = knobs().specs()['max_agents']
        self.assertEqual('integer', spec['kind'])
        self.assertNotIn('values', spec)
        self.assertEqual(3, knobs().default_of('max_agents'))

        shadows = knobs().shadowed()
        self.assertEqual(1, len(shadows), shadows)
        self.assertIn("'max_agents'", shadows[0])
        self.assertIn("'auditor'", shadows[0], 'the report names the declaration that tried')

    def test_a_declaration_with_a_kind_outside_the_four_is_not_a_knob(self):
        write_plugin(self.plugins, 'auditor', auditor_declaration(odd={'kind': 'colour'}))
        self.assertNotIn('odd', knobs().specs())


class TestKnobChain(_Sandbox):
    """`resolve`: narrowest first, every reading carrying where it came from.

    The key is a **declared** knob (`audit_limit`, a `number`), because that is the kind of knob that
    walks all four layers; a built-in setting takes the two wide ones only, and the case after this
    class pins what happens to a value written into a layer its knob does not take.
    """

    def setUp(self):
        super().setUp()
        write_plugin(self.plugins, 'auditor', auditor_declaration())

    def _record(self):
        return {'id': 'c1', 'policies': {'audit_limit': 2}}

    def test_the_chain_resolves_narrowest_first(self):
        knobs_mod = knobs()
        self.assertEqual((None, 'builtin'), knobs_mod.resolve(str(self.root))['audit_limit'],
                         'a declared threshold has no factory value')

        (self.home / '.pf').mkdir()
        (self.home / '.pf' / 'config.json').write_text('{"audit_limit": 5}', encoding='utf-8')
        self.assertEqual((5, 'global'), knobs_mod.resolve(str(self.root))['audit_limit'])

        self.write_project({'audit_limit': 4})
        self.assertEqual((4, 'project'), knobs_mod.resolve(str(self.root))['audit_limit'])

        self.assertEqual((2, 'cycle'),
                         knobs_mod.resolve(str(self.root), cycle=self._record())['audit_limit'])

        self.write_node({'audit_limit': 7})
        self.assertEqual((7, 'node'),
                         knobs_mod.resolve(str(self.root), cycle=self._record(),
                                           node=NODE)['audit_limit'])
        # The two narrow layers join only when they are handed in.
        self.assertEqual((4, 'project'), knobs_mod.resolve(str(self.root))['audit_limit'])

    def test_a_value_in_a_layer_its_knob_does_not_take_is_reported_and_skipped(self):
        """A core setting takes the two wide layers: one hand-written into a node's own file (or a
        line's record) is named when that layer is read and never quietly folded in."""
        self.write_project({'max_agents': 4})
        self.write_node({'max_agents': 9})

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            effective = knobs().resolve(str(self.root), node=NODE)

        self.assertEqual((4, 'project'), effective['max_agents'],
                         'a value from a layer the knob does not take was read as in force')
        self.assertIn('max_agents', stderr.getvalue())
        self.assertIn('node', stderr.getvalue())
        self.assertIn('project, global', stderr.getvalue(),
                      'the report has to name the layers the knob does take')

        # The cycle record is a state record, read tolerantly: the same value there is simply not
        # stated (no report is owed by a record, which is what "tolerant" means on this side).
        effective = knobs().resolve(str(self.root),
                                    cycle={'id': 'c1', 'policies': {'max_agents': 2}})
        self.assertEqual((4, 'project'), effective['max_agents'])

    def test_every_knob_is_in_the_reading_even_with_no_value(self):
        effective = knobs().resolve(str(self.root))
        self.assertEqual(SETTING_NAMES, set(effective) & SETTING_NAMES)
        self.assertEqual((None, 'builtin'), effective['audit_limit'],
                         'a knob with no factory value rests on None, not on a guess')

    def test_the_label_names_the_layer(self):
        source_label = knobs().source_label
        self.assertEqual('factory default', source_label('builtin'))
        self.assertEqual('project .pf/config.json', source_label('project'))
        self.assertEqual('global ~/.pf/config.json', source_label('global'))
        self.assertEqual('cycle c1', source_label('cycle', cycle=self._record()))
        self.assertEqual('cycle c1', source_label('cycle', cycle='c1'),
                         'a label takes the record or the id and says the same thing')
        self.assertEqual(f'node {NODE[:8]}', source_label('node', node=NODE))

    def test_a_file_layer_that_cannot_be_read_stops_the_call(self):
        """A mis-written setting quietly falling back to the factory value is the failure this rule
        exists for: it would read as "never configured" while it is in fact configured wrongly."""
        self.write_project({'max_agents': 'three'})
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                knobs().resolve(str(self.root))
        self.assertEqual(1, caught.exception.code)
        self.assertIn('max_agents', stderr.getvalue())
        self.assertIn('config.json', stderr.getvalue())

    def test_a_cycle_value_nobody_can_parse_is_not_stated(self):
        """The cycle layer is a state record, and this tool reads those tolerantly: a hand-edited
        policy value drops out of the resolution instead of stopping it."""
        self.write_project({'max_agents': 4})
        effective = knobs().resolve(str(self.root), cycle={'id': 'c1',
                                                           'policies': {'max_agents': 'three'}})
        self.assertEqual((4, 'project'), effective['max_agents'])


class TestKnobTicketReadings(_Sandbox):
    """What a worker is asked for: the ticket readings whose threshold is really in play."""

    def setUp(self):
        super().setUp()
        write_plugin(self.plugins, 'auditor', auditor_declaration())

    def test_a_reading_whose_threshold_nobody_states_is_not_asked(self):
        self.assertEqual(['always_asked'],
                         knobs().ticket_readings(str(self.root)),
                         'a pool-computed number is never asked of a worker, and a ticket reading '
                         'behind an unconfigured threshold is measured for nothing')

    def test_a_stated_threshold_puts_its_reading_in_the_form(self):
        self.write_project({'audit_limit': 10})
        self.assertEqual(['audit_minutes', 'always_asked'],
                         knobs().ticket_readings(str(self.root)),
                         'declaration order, and the narrowing only drops what nobody configured')

    def test_the_lines_own_policies_count_as_stated(self):
        self.write_project({})
        record = {'id': 'c1', 'policies': {'audit_limit': 4}}
        self.assertEqual(['audit_minutes', 'always_asked'],
                         knobs().ticket_readings(str(self.root), record),
                         "a threshold this line set is in force for this line's readings")


class TestKnobFeaturesDir(_Sandbox):
    """`PF_PLUGINS_DIR`: the scan follows the variable, caches nothing, and is empty for no folders."""

    def test_installing_and_removing_a_feature_changes_the_next_call(self):
        self.assertNotIn('audit_limit', knobs().specs())

        folder = write_plugin(self.plugins, 'auditor', auditor_declaration())
        self.assertIn('audit_limit', knobs().specs(), 'the scan is uncached: this same process sees it')

        shutil.rmtree(folder)
        self.assertNotIn('audit_limit', knobs().specs(),
                         'and the next call sees it gone -- the folder is the whole identity')

    def test_a_missing_plugins_directory_is_zero_features_not_an_error(self):
        absent = self.base / 'absent'
        with mock.patch.dict(os.environ, {'PF_PLUGINS_DIR': str(absent)}):
            self.assertEqual(SETTING_NAMES, set(knobs().specs()) & SETTING_NAMES)
        self.assertFalse(absent.exists(), 'reading the scan must not create the directory')

    def test_the_cli_reads_the_plugins_directory_the_variable_names(self):
        write_plugin(self.plugins, 'auditor', auditor_declaration())

        listed = self.cli('config', 'list')
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertIn('audit_limit', listed.stdout)

        self.assertEqual(0, self.cli('config', 'set', 'audit_limit', '5').returncode)
        self.assertEqual('5\n', self.cli('config', 'get', 'audit_limit').stdout)

        empty = self.base / 'empty'
        empty.mkdir()
        gone = self.cli('config', 'list', plugins=empty)
        self.assertEqual(0, gone.returncode, gone.stderr)
        self.assertNotIn('audit_limit', gone.stdout,
                         'with no plugin folder the knob does not exist at all')
        refused = self.cli('config', 'set', 'audit_limit', '5', plugins=empty)
        self.assertNotEqual(0, refused.returncode, 'and a name nobody declares is refused')
        self.assertIn('audit_limit', refused.stderr)


if __name__ == '__main__':
    unittest.main()
