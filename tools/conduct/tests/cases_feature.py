"""The feature mechanism: a folder under `features/` is the whole of a feature.

A feature is two files in a folder -- a declaration (`feature.json`) and an implementation
(`impl.py`) -- and the folder's name is its identity. The core knows the **kinds** only: four knob
kinds (`number` / `integer` / `word` / `switch`), three timings (`close` / `pick` / `dispatch`) and two
actions (`insert` / `block`). Every name the outside uses comes from a declaration, merged for every
reader by `pipeline/feature_registry.py` and asked at its timing by `pipeline/feature_host.py`.

What this file pins, in the order the criteria are stated:

  * **the registry**: what is installed, which names it merges (knobs, dedupe tags, methods, ticket
    readings), and that the core's own files name none of them;
  * **a fake second feature**: a hand-written folder plugs in through `PF_FEATURES_DIR` with the core
    untouched -- and an ill-formed or duplicated declaration is reported by `validate()` while the
    runtime skips it;
  * **the deleted folder**: with no feature installed at all the pipeline still closes and still
    dispatches, and the close is byte-for-byte the one it always gave (no pool child process, no
    reading asked on the form);
  * **the dispatch gate**: a `block` feature at `dispatch` stops a session from opening -- on every
    road that would pin a new node (`next`'s pick and `next --after`'s close) and on the standalone
    `pf dispatch` verb -- with `stop_pipeline`, no pin, no prompt and the line left in service (a
    refused close still settles the node it was closing);
  * **the pick timing**: an `insert` feature may also act at a pick, where its node enters the graph
    in front of the node the ranking chose; it pins nothing, writes no prompt and opens no session,
    and it is asked again on every step -- so a feature that wants the line to carry on stops
    offering while one of its own nodes is open on the graph;
  * **a door may not end the process**: `sys.exit()` at any of the three doors where a feature's own
    code runs -- a gate, an offer, the text builder an offer hands over -- is contained exactly like
    a raise, so it costs that feature its pass and never the line's step.

Every case here builds its own features directory: nothing in this file depends on `features/` as it
is shipped in this checkout.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE, NODE2, pipeline, normalise  # noqa: E402
from harness import (HANDOFF_PY, STATE_MANAGE, TOOLS_DIR, _run_proc, features_dir,  # noqa: E402
                     patched, requires_feature, shipped_sources, write_feature)

__all__ = ['TestTheRegistry', 'TestTheDeletedFeatureFolder', 'TestTheDispatchGate',
           'TestThePickTiming', 'TestABrokenOfferIsContained', 'TestTheOffersFields',
           'TestADoorMayNotEndTheProcess']

#: The tag an insertion fixture's node carries, and what its offer uses to stay one at a time.
FIXTURE_TAG = 'fixture:feature'

#: An `insert` feature: it asks for one node until one of its own is open on the graph. It carries
#: the line's own filter tags beside its dedupe tag -- a node outside the line's range is invisible to
#: that line's pick, so a feature that wants its node picked says which range it belongs to.
INSERT_IMPL = '''
TAG = %(tag)r


def offer(ctx):
    if ctx.nodes_with_tag(TAG):
        return None
    line = [tag for tag in ((ctx.cycle or {}).get('tags') or []) if isinstance(tag, str)]
    return {
        'title': 'Inserted by the fixture',
        'tags': [TAG, *line],
        'spec_of': lambda node_id: f'# fixture\\n\\nwritten for {node_id}\\n',
        'fields': {'metric': 'fixture'},
        'note': 'the fixture asked for this node',
    }
'''

#: A `block` feature: one sentence saying "not now".
BLOCK_IMPL = '''
def check(ctx):
    return 'the fixture gate says not now'
'''

#: A gate whose own code is broken: the runtime reports it and carries on.
BOOM_IMPL = '''
def check(ctx):
    raise RuntimeError('the fixture gate is broken')
'''

#: An `insert` feature whose text builder raises. A well-shaped offer: the shape check cannot catch
#: this one, so it is the case the `try` around the graph edit is for.
BOOM_OFFER_IMPL = '''
def offer(ctx):
    def spec_of(node_id):
        raise RuntimeError('the template is broken')
    return {'title': 'Boom', 'tags': ['coding'], 'spec_of': spec_of}
'''

#: A door that ends the process instead of answering. `SystemExit` is not an `Exception`, so the
#: containment has to name it: a feature may not stop somebody's project that way either.
EXIT_GATE_IMPL = '''
import sys


def check(ctx):
    sys.exit('the fixture gate wants the process to end')
'''

EXIT_OFFER_IMPL = '''
import sys


def offer(ctx):
    sys.exit('the fixture offer wants the process to end')
'''

#: The same failure one step later: the offer is accepted and the builder it handed over ends the
#: process. That builder runs inside the containment around the graph edit.
EXIT_BUILDER_IMPL = '''
import sys


def offer(ctx):
    def spec_of(node_id):
        sys.exit('the fixture template wants the process to end')
    return {'title': 'Exit', 'tags': ['coding'], 'spec_of': spec_of}
'''

#: The same timing with an offer whose *shape* is wrong: `tags` is one string (the graph tool would
#: read it as one tag per character) and the title is not a string at all.
WRONG_TAGS_IMPL = '''
def offer(ctx):
    return {'title': 'Wrong tags', 'tags': 'coding',
            'spec_of': lambda node_id: '# fixture\\n'}
'''

WRONG_TITLE_IMPL = '''
def offer(ctx):
    return {'title': 5, 'tags': ['coding'], 'spec_of': lambda node_id: '# fixture\\n'}
'''

#: An offer whose builder answers on its FIRST call and raises on any later one: the close is the
#: only caller allowed to run it (`insert` and `mint` both need the text), so a green case means the
#: text was built once and handed on.
ONCE_IMPL = '''
CALLS = []


def offer(ctx):
    def spec_of(node_id):
        CALLS.append(node_id)
        if len(CALLS) > 1:
            raise RuntimeError('the text builder was asked a second time')
        return f'# once\\n\\nwritten for {node_id}\\n'
    return {'title': 'Built once', 'tags': ['coding'], 'spec_of': spec_of}
'''

#: A pick feature that answers EVERY time it is asked -- nothing de-dupes it. The shape a caller
#: must survive without letting one call grow the graph by a burst of nodes.
ALWAYS_OFFER_IMPL = '''
def offer(ctx):
    line = [tag for tag in ((ctx.cycle or {}).get('tags') or []) if isinstance(tag, str)]
    return {
        'title': 'Inserted every time',
        'tags': ['fixture:always', *line],
        'spec_of': lambda node_id: f'# fixture\\n\\nwritten for {node_id}\\n',
        'fields': {'metric': 'fixture'},
        'note': 'the fixture answers every time it is asked',
    }
'''

#: An offer that puts the action's own cells into `fields`: `cycle` names another line, `action`
#: another kind, `note` / `steps` replace what the close itself says. Only `metric` is its own data.
RESERVED_FIELDS_IMPL = '''
def offer(ctx):
    line = [tag for tag in ((ctx.cycle or {}).get('tags') or []) if isinstance(tag, str)]
    return {
        'title': 'Inserted by the fixture',
        'tags': ['fixture:reserved', *line],
        'spec_of': lambda node_id: f'# fixture\\n\\nwritten for {node_id}\\n',
        'fields': {'action': 'dispatch', 'cycle': 'some-other-line',
                   'note': 'a lie about the close', 'steps': ['a lie about what this does'],
                   'metric': 'fixture'},
        'note': 'the fixture asked for this node',
    }
'''

#: A close feature that judges with the threshold its declaration gives a factory default: the
#: reading is asked of the worker from the start and the close compares against the default.
JUDGE_IMPL = '''
TAG = 'audit:open'


def offer(ctx):
    if ctx.nodes_with_tag(TAG):
        return None
    line = [tag for tag in ((ctx.cycle or {}).get('tags') or []) if isinstance(tag, str)]
    score = ctx.reading('audit_score')
    limit = ctx.knob('audit_limit')
    if score is None or limit is None or score < limit:
        return None
    return {
        'title': 'Inserted by the fixture',
        'tags': [TAG, *line],
        'spec_of': lambda node_id: f'# fixture\\n\\nwritten for {node_id}\\n',
        'fields': {'metric': 'audit_score', 'reading': score, 'line': limit},
        'note': 'the audit score crossed the line',
    }
'''


def insert_impl(tag=FIXTURE_TAG):
    return INSERT_IMPL % {'tag': tag}


def insert_decl(name='fixture', timing='close', **extra):
    decl = {'name': name, 'summary': f'{name}, a fixture', 'timing': timing, 'action': 'insert',
            'knobs': {}, 'readings': {}, 'metrics': {}}
    decl.update(extra)
    return decl


def block_decl(name='gate', knob=None, **extra):
    decl = {'name': name, 'summary': f'{name}, a fixture gate', 'timing': 'dispatch',
            'action': 'block', 'knobs': {}, 'readings': {}, 'metrics': {}}
    if knob:
        decl['enable_knob'] = knob
        decl['knobs'] = {knob: {'kind': 'switch', 'default': 'on',
                                'summary': 'whether this gate is asked'}}
    decl.update(extra)
    return decl


class FeatureBase(Base):
    """A case's own features directory: `self.features()` makes one and points the scan at it."""

    def features(self):
        """A throwaway features directory, entered for the case's whole life (and emptied after it)."""
        tmp = tempfile.TemporaryDirectory(prefix='conduct-features-')
        self.addCleanup(tmp.cleanup)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(features_dir(tmp.name))
        return Path(tmp.name)

    def coding_line(self, *, pair=True):
        """A coding line started on its head node; the close can walk at any time."""
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        if pair:
            sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        return sb

    def finished_head(self, *, reading=None):
        """The same line, one step on: the head's ticket is on disk and its close has not run."""
        sb = self.coding_line()
        sb.next()
        outputs = {'deliverable': 'x'}
        if reading is not None:
            outputs['reading'] = reading
        sb.write_signal('cyc', NODE, 'completed', 'done', outputs)
        return sb

    def pick_line(self):
        """A coding line created but not started: nothing is in hand, so the next step is a pick."""
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.create_cycle('cyc', tags=['coding'])
        return sb


class TestTheRegistry(FeatureBase):
    """What is installed, and the one table every reader is handed."""

    @requires_feature('maintenance')
    def test_the_installed_feature_declares_every_name_the_outside_uses(self):
        """One folder declares the shipped feature: its knobs, its readings, its dedupe tags and its
        method names -- and the core reads them all from there."""
        registry = pipeline.registry()
        self.assertIn('maintenance', [feature['name'] for feature in registry.load_all()])
        self.assertEqual([], registry.validate(), 'the shipped declaration does not validate')

        knobs = registry.knob_specs()
        self.assertEqual(knobs['test_seconds_limit']['kind'], 'number')
        self.assertEqual(knobs['issue_pool_weight_limit']['kind'], 'number')
        self.assertEqual(knobs['maintenance']['kind'], 'switch')
        self.assertEqual(knobs['maintenance']['default'], 'on')

        self.assertEqual(registry.dedupe_tags(),
                         ['maint:test_seconds', 'maint:issue_pool_weight'])
        self.assertEqual(registry.methods(), ['pf-test-audit', 'pf-issue-repair'])

        readings = {reading['key']: reading for reading in registry.readings()}
        self.assertEqual(readings['test_seconds']['source'], 'ticket')
        self.assertEqual(readings['test_seconds']['when_knob'], 'test_seconds_limit')
        self.assertTrue(readings['test_seconds']['ask'],
                        'the form has to be able to ask the reading in words')
        self.assertEqual(readings['issue_pool_weight']['source'], 'pool')
        # the merged table is what every door reads the same names from
        self.assertIn('test_seconds_limit', pipeline.knob_specs())

    @requires_feature('maintenance')
    def test_the_core_names_no_installed_feature(self):
        """The shipped core spells none of the feature's names: not a knob, not a dedupe tag, not a
        method, not the feature's own name.

        The names are read off the declaration, never restated here: a rename inside the folder is a
        folder-local edit, and this guard follows it. The scan is the tree's own list of what ships
        (`shipped_sources`, tests and `features/` excluded).
        """
        decl = (json.loads((pipeline.registry().features_dir() / 'maintenance' /
                            'feature.json').read_text(encoding='utf-8')))
        names = {'maintenance'}
        names |= set(decl.get('knobs') or {})
        names |= set(decl.get('readings') or {})
        for metric in (decl.get('metrics') or {}).values():
            if isinstance(metric, dict):
                names |= {metric.get('tag'), metric.get('method')}
        names.discard(None)

        hits = []
        for path in shipped_sources():
            text = path.read_text(encoding='utf-8')
            hits.extend(f'{path.relative_to(TOOLS_DIR)}: {name}'
                        for name in sorted(names) if name in text)
        self.assertEqual([], hits, f'the core names a feature: {hits}')

    @requires_feature('maintenance')
    def test_the_cells_and_kinds_are_the_ones_the_core_knows(self):
        """The vocabulary is closed, and every installed declaration sits in a cell it serves.

        Four knob kinds, three timings, two actions, and the two-axis table with exactly three
        served cells (insert at close or pick, block at dispatch). A declaration naming anything
        else is a `validate()` problem (the case below drives that), and the shipped folder must be
        inside the table or the core would be accepting a feature it never asks.
        """
        registry = pipeline.registry()
        self.assertEqual({'number', 'integer', 'word', 'switch'}, set(registry.KINDS))
        self.assertEqual(('close', 'pick', 'dispatch'), registry.TIMINGS)
        self.assertEqual(('insert', 'block'), registry.ACTIONS)
        self.assertEqual({('close', 'insert'), ('pick', 'insert'), ('dispatch', 'block')},
                         set(registry.CELLS))

        declared = {(feature['decl']['timing'], feature['decl']['action'])
                    for feature in registry.load_all() if isinstance(feature.get('decl'), dict)}
        self.assertTrue(declared <= set(registry.CELLS),
                        f'a declaration claims a cell the core does not serve: '
                        f'{declared - set(registry.CELLS)}')

    def test_an_empty_features_dir_answers_nothing(self):
        """No folder installed: every door answers empty, the knob table is the core's own, and a line
        that carries a threshold-shaped policy asks nothing of a worker."""
        self.features()
        registry = pipeline.registry()

        self.assertEqual([], registry.load_all())
        self.assertEqual([], registry.validate())
        self.assertEqual({}, registry.knob_specs())
        self.assertEqual([], registry.dedupe_tags())
        self.assertEqual([], registry.methods())
        self.assertEqual([], registry.readings())
        self.assertEqual({'max_agents', 'test_tier', 'worktree_mode'}, set(pipeline.knob_specs()),
                         'the core knob table is not the core\'s own')

        sb = self.finished_head()
        self.assertEqual([], pipeline.ticket_readings(sb.root, sb.cycle()),
                         'a reading was asked with no feature installed to declare it')

    def test_a_fake_second_feature_is_just_a_folder(self):
        """A hand-written folder is an installed feature: its knob merges, its reading narrows by the
        knob it names, its tag and method show, and its offer is asked at its timing.

        This is the whole of "a second feature plugs in": one `write_feature` call, and no core file
        learns a name (the case after this one pins that half).
        """
        root = self.features()
        write_feature(root, 'audit', {
            'name': 'audit', 'summary': 'a second feature', 'timing': 'close', 'action': 'insert',
            'knobs': {'audit_limit': {'kind': 'number', 'summary': 'how many open audits are too many'}},
            'readings': {'audit_score': {'source': 'ticket', 'when_knob': 'audit_limit',
                                         'ask': 'how many audit findings are still open'}},
            'metrics': {'audit_score': {'line': 'audit_limit', 'tag': 'audit:open',
                                        'method': 'pf-audit'}},
        }, insert_impl('audit:open'))

        registry = pipeline.registry()
        self.assertEqual(['audit'], [feature['name'] for feature in registry.load_all()])
        self.assertEqual([], registry.validate())
        self.assertEqual(registry.knob_specs()['audit_limit']['kind'], 'number')
        self.assertEqual(registry.knob_specs()['audit_limit']['feature'], 'audit')
        self.assertEqual(registry.dedupe_tags(), ['audit:open'])
        self.assertEqual(registry.methods(), ['pf-audit'])
        self.assertEqual(['audit_score'], [reading['key'] for reading in registry.readings()])
        self.assertIn('audit_limit', pipeline.knob_specs(),
                      'the declaration did not reach the merged knob table')

        sb = self.finished_head()
        self.assertEqual([], pipeline.ticket_readings(sb.root, sb.cycle()),
                         'a reading is asked before its narrowing knob is stated')
        sb.set_policy('cyc', 'audit_limit', 3)
        self.assertEqual(['audit_score'], pipeline.ticket_readings(sb.root, sb.cycle()),
                         'the reading is not asked once its knob is stated')
        offers = pipeline.insertions('close', pipeline.Context(
            sb.root, cycle=sb.cycle(), node_id=NODE, sig=sb.read_signal('cyc', NODE),
            dag=sb.dag(), target={'id': NODE2}))
        self.assertEqual(1, len(offers), offers)

    def test_a_second_feature_needs_no_core_change(self):
        """The name a declaration adds reaches the merged table while no shipped file learns it: the
        core is data-driven, and a second feature is a folder move."""
        root = self.features()
        write_feature(root, 'audit', {
            'name': 'audit', 'summary': 'a second feature', 'timing': 'close', 'action': 'insert',
            'knobs': {'audit_limit': {'kind': 'number'}},
            'readings': {}, 'metrics': {},
        }, insert_impl('audit:open'))

        self.assertIn('audit_limit', pipeline.knob_specs())
        spelled = [path.relative_to(TOOLS_DIR).as_posix() for path in shipped_sources()
                   if 'audit_limit' in path.read_text(encoding='utf-8')]
        self.assertEqual([], spelled, f'the core spells a feature\'s name: {spelled}')

    def test_a_broken_declaration_is_reported_and_skipped(self):
        """Three kinds of broken folder, all of them `validate()`'s to report and none of them a crash
        at runtime: an unknown timing, a declaration that is not JSON, and a cell the core does not
        serve (`block` at a pick)."""
        root = self.features()
        write_feature(root, 'unknown-timing', insert_decl('unknown-timing', timing='lunch'),
                      insert_impl())
        write_feature(root, 'not-json', None, insert_impl(), files={'feature.json': '{"name": '})
        write_feature(root, 'pick-block', {'name': 'pick-block', 'timing': 'pick',
                                           'action': 'block'}, BLOCK_IMPL)

        registry = pipeline.registry()
        problems = '\n'.join(registry.validate())
        self.assertIn("timing 'lunch'", problems)
        self.assertIn('parse failure', problems)
        self.assertIn("'block' at 'pick'", problems)
        # runtime: a declaration nobody can serve is skipped, and nothing raises
        self.assertEqual([], registry.by_timing('close'))
        self.assertEqual([], registry.by_action('insert'))
        self.assertEqual({}, registry.knob_specs())

        sb = self.finished_head()
        self.assertEqual([], pipeline.insertions('close', pipeline.Context(
            sb.root, cycle=sb.cycle(), node_id=NODE, sig=sb.read_signal('cyc', NODE),
            dag=sb.dag())))
        action = sb.close_round(NODE)
        self.assertEqual('dispatch', action['action'], 'a broken folder stopped the close')

    def test_a_declared_layers_cell_narrows_the_knob_and_is_validated(self):
        """A declaration may narrow which layers its knob takes, and the merged cell is where the
        readers take it from; a layer name that is not on the chain is a declaration problem (the
        doctor's list), and the knob then keeps the whole chain while the feature is not acted on."""
        root = self.features()
        write_feature(root, 'narrow', {
            'name': 'narrow', 'summary': 'one knob of its own', 'timing': 'close',
            'action': 'insert',
            'knobs': {'narrow_limit': {'kind': 'number', 'layers': ['project']},
                      'broken_limit': {'kind': 'number', 'layers': ['line']}},
            'readings': {}, 'metrics': {},
        }, insert_impl('narrow:open'))

        registry = pipeline.registry()
        self.assertEqual(('project',), registry.knob_specs()['narrow_limit']['layers'])
        self.assertNotIn('layers', registry.knob_specs()['broken_limit'],
                         'a layers cell off the chain was copied in anyway')
        problems = '\n'.join(registry.validate())
        self.assertIn('broken_limit', problems)
        self.assertIn("'line'", problems)
        # the merged table is what every reader of the chain takes the cell from
        self.assertEqual(('project',), pipeline.knob_specs()['narrow_limit']['layers'])

    def test_a_declared_default_is_in_force_so_the_form_asks_and_the_close_judges(self):
        """A threshold that carries a factory `default` is in force with nobody writing it: the
        ticket form asks the worker for the number and the close judges against the default.

        A declaration with no default is the other half -- the shipped feature declares none, so
        nothing is asked until somebody writes a line (that half is the case above).
        """
        sb = self.finished_head(reading={'audit_score': 12})
        write_feature(self.features(), 'audit', {
            'name': 'audit', 'summary': 'a second feature', 'timing': 'close', 'action': 'insert',
            'knobs': {'audit_limit': {'kind': 'number', 'default': 10}},
            'readings': {'audit_score': {'source': 'ticket', 'when_knob': 'audit_limit',
                                         'ask': 'how many audit findings are still open'}},
            'metrics': {'audit_score': {'line': 'audit_limit', 'tag': 'audit:open',
                                        'method': 'pf-audit'}},
        }, JUDGE_IMPL)
        self.assertEqual(10, pipeline.resolve_knob(sb.root, 'audit_limit', cycle=sb.cycle()),
                         'the declaration\'s default is not the effective value')

        text = _run_proc(sb.root, [HANDOFF_PY, '--form', 'cyc', NODE]).stdout
        self.assertIn('"reading"', text, 'a threshold in force was never asked at the form')
        self.assertIn('audit_score', text)

        action = sb.close_round(NODE)

        self.assertEqual('insert_node', action['action'], action)
        self.assertEqual(3, len(sb.dag()['nodes']), 'the close did not judge against the default')

    def test_a_name_two_features_claim_is_reported(self):
        """Two folders declaring the same knob is a problem the doctor prints, and one cell wins at
        runtime: every door still answers one value, never a coin toss."""
        root = self.features()
        cell = {'kind': 'number', 'summary': 'the first one wins'}
        write_feature(root, 'a-one', {'name': 'a-one', 'timing': 'close', 'action': 'insert',
                                      'knobs': {'audit_limit': cell}, 'readings': {},
                                      'metrics': {}}, insert_impl('a:one'))
        write_feature(root, 'b-two', {'name': 'b-two', 'timing': 'close', 'action': 'insert',
                                      'knobs': {'audit_limit': cell}, 'readings': {},
                                      'metrics': {}}, insert_impl('b:two'))

        registry = pipeline.registry()
        self.assertTrue([problem for problem in registry.validate()
                         if 'claimed by both' in problem], registry.validate())
        self.assertEqual(registry.knob_specs()['audit_limit']['feature'], 'a-one',
                         'the merged cell does not follow folder order')


class TestTheDeletedFeatureFolder(FeatureBase):
    """The completion criterion the other way round: with no feature folder installed, the pipeline
    still closes, still dispatches, and its close is byte-for-byte the one it always gave."""

    def test_a_line_that_carries_a_threshold_closes_and_dispatches(self):
        """A line whose ledger still carries a hand-written threshold: no feature reads it, so the close
        is an ordinary dispatch and the graph does not grow."""
        self.features()
        sb = self.coding_line()
        sb.rewrite_cycle(policies={'test_seconds_limit': 5, 'issue_pool_weight_limit': 1})
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': 999}})

        action = sb.close_round(NODE)

        self.assertEqual('dispatch', action['action'], action)
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(2, len(sb.dag()['nodes']), 'a node was inserted with no feature installed')
        self.assertEqual(sb.path_nodes(), [NODE])
        self.assertEqual([NODE2], sb.holding_ids())
        self.assertTrue((sb.root / '.pf' / 'meta' / 'launch' / 'cyc' / NODE2 / 'prompt.txt')
                        .is_file(), 'the close did not write the next prompt')

    def test_the_close_is_byte_for_byte_the_control(self):
        """Two projects, one of them carrying a threshold in its ledger: with no feature installed the
        two closes answer the same action, and the ledgers agree word for word (the policies slot
        aside, which is the one intended difference)."""
        def walked(data):
            data = normalise(data)
            for cycle in data.get('cycles') or []:
                cycle.pop('policies', None)
            return data

        self.features()
        control = self.coding_line()
        control.next()
        control.write_signal('cyc', NODE, 'completed', 'done', {'deliverable': 'x'})

        carrying = self.coding_line()
        carrying.rewrite_cycle(policies={'test_seconds_limit': 5})
        carrying.next()
        carrying.write_signal('cyc', NODE, 'completed', 'done', {'deliverable': 'x'})

        self.assertEqual(normalise(control.close_round(NODE)),
                         normalise(carrying.close_round(NODE)))
        for rel in ('.pf/ideas.json', '.pf/state.json'):
            self.assertEqual(walked(json.loads(control.tree_contents()[rel])),
                             walked(json.loads(carrying.tree_contents()[rel])),
                             f'{rel} changed because a threshold is in the ledger')

    def test_no_pool_child_process_is_started(self):
        """The pool's number used to be fetched by the close. With nothing installed there is no
        reading to fetch, so not one child process for it is started."""
        self.features()
        sb = self.coding_line()
        sb.rewrite_cycle(policies={'issue_pool_weight_limit': 1})
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done', {'deliverable': 'x'})

        seen = []
        real = pipeline._capture

        def spy(argv, cwd):
            seen.append([str(part) for part in argv])
            return real(argv, cwd)

        with patched(pipeline, '_capture', spy):
            sb.close_round(NODE)

        pool = [call for call in seen if 'issue_manager.py' in ' '.join(call)]
        self.assertEqual([], pool, f'the pool tool was asked: {pool}')

    def test_the_ticket_form_asks_for_no_reading(self):
        """`handoff --form` is built out of the declarations too: with none installed the skeleton
        carries no `reading` slot at all."""
        self.features()
        sb = self.coding_line(pair=False)
        sb.rewrite_cycle(policies={'test_seconds_limit': 5})

        text = _run_proc(sb.root, [HANDOFF_PY, '--form', 'cyc']).stdout

        self.assertIn('-o \'{"deliverable"', text)
        self.assertNotIn('"reading"', text)


class TestTheDispatchGate(FeatureBase):
    """The gate sits at the dispatch: the one moment "may a session open now?" can be answered."""

    def test_a_gate_stops_the_dispatch_and_leaves_the_line_in_service(self):
        """A refused dispatch carries `stop_pipeline` with **no commands**: the line is not parked (a
        gate about time has to be asked again on the next step), nothing is pinned, no prompt is
        written, and the node the line already held is still held."""
        sb = self.coding_line()
        write_feature(self.features(), 'gate', block_decl(), BLOCK_IMPL)

        action = sb.plan()

        self.assertEqual('stop_pipeline', action['action'], action)
        self.assertEqual([], action['commands'], 'a refused dispatch handed over a command')
        self.assertIn('feature gate blocked this dispatch', action['reason'])
        self.assertIn('the fixture gate says not now', action['reason'])
        self.assertEqual('active', sb.cycle()['status'], 'a gate parked the line')
        self.assertEqual([NODE], sb.holding_ids(), 'the line lost the node it was holding')
        self.assertFalse((sb.root / '.pf' / 'meta' / 'launch' / 'cyc' / NODE
                          / 'prompt.txt').exists(), 'a refused dispatch wrote a prompt')
        # the next step asks again, and nothing was consumed by the first answer
        self.assertEqual('stop_pipeline', sb.plan()['action'])

    def test_a_gate_refuses_a_close_before_it_pins_the_successor(self):
        """The close path asks the gate before the pins its fill would write, exactly like the pick.

        The refusal settles the node all the same -- a gate says nothing about a close -- but it
        pins nobody and writes no prompt, and the line stays in service. So the successor is not
        lost: the step after the close dispatches it once the gate is satisfied, and while the gate
        refuses, `pf cycle current` shows an empty hand rather than a held node nobody was told
        about (the bug this pins: the pin used to land before the gate was asked).
        """
        sb = self.finished_head()
        write_feature(self.features(), 'gate', block_decl(knob='gate_on'), BLOCK_IMPL)

        asked = []
        real = pipeline.feature_host.gates

        def spy(timing, ctx):
            asked.append(timing)
            return real(timing, ctx)

        with patched(pipeline.feature_host, 'gates', spy):
            refused = sb.close_round(NODE)

        self.assertEqual('stop_pipeline', refused['action'], refused)
        self.assertEqual(['dispatch'], asked, f'the close asked the gate {asked}')
        self.assertEqual(1, refused['reason'].count('the fixture gate says not now'),
                         f'the refusal is not named exactly once: {refused["reason"]!r}')
        self.assertIn(f'{NODE[:8]} is settled', refused['reason'])
        self.assertIn('no next node was pinned', refused['reason'])
        self.assertEqual([NODE], sb.path_nodes(), 'the refused close did not settle its node')
        self.assertEqual([], sb.holding_ids(), 'a refused close-driven dispatch pinned a node')
        self.assertEqual('active', sb.cycle()['status'], 'a refused close-driven dispatch parked '
                                                         'the line')
        launch = sb.root / '.pf' / 'meta' / 'launch' / 'cyc'
        self.assertFalse((launch / NODE2 / 'prompt.txt').exists(),
                         'a refused close-driven dispatch wrote the successor\'s prompt')

        # the gate is the only thing in the way, so switching it off lets that dispatch happen
        _run_proc(sb.root, [STATE_MANAGE, 'config', 'set', 'gate_on', 'off'])

        dispatched = sb.next()

        self.assertEqual('dispatch', dispatched['action'], dispatched)
        self.assertEqual(NODE2, dispatched['node_id'])
        self.assertEqual([NODE2], sb.holding_ids())
        self.assertTrue((launch / NODE2 / 'prompt.txt').is_file())

    def test_the_gate_is_a_switch_and_off_lets_the_dispatch_through(self):
        """A gate may declare its own switch (`enable_knob`): off on any layer skips it, exactly like
        every other feature."""
        sb = self.coding_line()
        root = self.features()
        write_feature(root, 'gate', block_decl(knob='gate_on'), BLOCK_IMPL)
        self.assertEqual('stop_pipeline', sb.plan()['action'])

        # the switch is a knob on the same chain every other setting uses, so the real CLI writes it
        _run_proc(sb.root, [STATE_MANAGE, 'config', 'set', 'gate_on', 'off'])

        action = sb.plan()
        self.assertEqual('dispatch', action['action'], action)
        self.assertEqual(NODE, action['node_id'])

    def test_pf_dispatch_consults_the_same_gate(self):
        """The standalone `pf dispatch <cycle>` verb is a dispatch: the gate answers there too.

        Without it, `pf dispatch` would be the way around a gate -- the same session the `next` path
        refused would open one command later. The refusal writes nothing and opens nothing, and the
        prompt the last close wrote is still sitting there for a retry once the gate is satisfied.
        """
        sb = self.coding_line()
        sb.next()                                  # pins NODE and writes its prompt (no gate yet)
        prompt = sb.root / '.pf' / 'meta' / 'launch' / 'cyc' / NODE / 'prompt.txt'
        self.assertTrue(prompt.is_file(), 'the fixture has no prompt to start')
        write_feature(self.features(), 'gate', block_decl(), BLOCK_IMPL)

        refused = sb.dispatch(cli=True, check=False)

        self.assertEqual(2, refused.returncode, (refused.stdout, refused.stderr))
        self.assertIn('a feature gate blocked this dispatch', refused.stderr)
        self.assertIn('the fixture gate says not now', refused.stderr)
        self.assertEqual('active', sb.cycle()['status'], 'a refused dispatch parked the line')
        self.assertTrue(prompt.is_file(), 'a refused dispatch consumed the prompt')
        self.assertEqual(['prompt.txt'], sorted(path.name for path in prompt.parent.iterdir()),
                         'a refused dispatch started something')

    def test_a_gate_with_no_gate_installed_changes_nothing(self):
        """The same verb with an empty features directory dispatches as it always did -- the gate is
        additive, and the drill switch keeps the session itself in the report."""
        sb = self.coding_line()
        sb.next()
        self.features()

        proc = sb.dispatch(cli=True, check=False)

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn('dry-run:', proc.stdout, 'the dispatch did not report the call it made')
        self.assertNotIn('gate blocked', proc.stderr)

    def test_a_gate_that_raises_is_skipped_with_one_warning(self):
        """A broken gate must not stop somebody's project: one `WARNING:` line on stderr, and the
        dispatch goes through as if the feature had said nothing."""
        sb = self.coding_line()
        write_feature(self.features(), 'gate', block_decl(), BOOM_IMPL)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            action = sb.plan()

        self.assertEqual('dispatch', action['action'], action)
        self.assertIn('WARNING: feature gate failed', err.getvalue())
        self.assertIn('the fixture gate is broken', err.getvalue())


class TestThePickTiming(FeatureBase):
    """The second timing an insertion may occupy: a pick with nothing in hand yet."""

    def test_a_pick_feature_inserts_in_front_of_the_first_dispatch(self):
        """A pick-time insertion is the same graph edit with no close behind it: the node enters the
        graph with no parent (so it is ready at once), nothing is pinned, no prompt is written, the
        line stays active -- and the node is in the same ranking every other node is.

        The insertion is where that `next` stops: it answers `insert_node`, the caller commits and
        pushes, and the step after it is the one that picks and dispatches by the usual ranking.
        """
        sb = self.pick_line()
        write_feature(self.features(), 'pickfix', insert_decl('pickfix', timing='pick'),
                      insert_impl())

        planned = sb.plan()

        self.assertEqual('insert_node', planned['action'], planned)
        self.assertNotIn('prompt', planned, 'an insertion is not a dispatch')
        self.assertNotIn('prompt_text', planned)
        self.assertEqual([], sb.holding_ids(), 'the pick pinned a node before the insertion landed')
        self.assertEqual('active', sb.cycle()['status'])
        self.assertEqual(2, len(sb.dag()['nodes']), 'plan wrote the node it only promised')
        self.assertIn('in front of', ' '.join(planned['steps']))

        walked = sb.next()

        self.assertEqual('insert_node', walked['action'], walked)
        nodes = sb.dag()['nodes']
        made = [uid for uid in nodes if uid not in (NODE, NODE2)]
        self.assertEqual(1, len(made), f'the pick inserted no node: {nodes}')
        self.assertEqual([], nodes[made[0]]['dependencies'],
                         'a pick insertion has no parent: it is ready when it exists')
        self.assertIn(FIXTURE_TAG, nodes[made[0]]['tags'])
        self.assertIn('coding', nodes[made[0]]['tags'],
                      'a node outside the line\'s filter set is invisible to that line\'s pick')
        self.assertTrue((sb.root / '.pf' / 'nodes' / made[0] / 'spec.md').is_file())

        # the step after the insertion dispatches the node the pick had chosen; the inserted node is
        # in that same ranking, and it is the top row once that node is closed
        dispatched = sb.next()
        self.assertEqual('dispatch', dispatched['action'], dispatched)
        self.assertEqual(NODE, dispatched['node_id'])
        self.assertEqual([NODE], sb.holding_ids())

        sb.write_signal('cyc', dispatched['node_id'], 'completed', 'done', {'deliverable': 'x'})
        after = sb.close_round(dispatched['node_id'])
        self.assertEqual('dispatch', after['action'], after)
        self.assertEqual(made[0], after['node_id'], 'the inserted node is not in the same ranking')
        self.assertEqual([made[0]], sb.holding_ids())

    def test_an_always_answering_pick_feature_inserts_one_node_per_step(self):
        """A pick insertion ends the step: one answer, one node, and the call comes back.

        The fixture here answers every time it is asked -- nothing de-dupes it -- which is exactly
        the shape that used to make one `next` insert eight nodes and never reach a dispatch. Now
        each `next` inserts at most one: the graph cannot grow by a burst, and the caller gets the
        graph edit to commit before the next step. The pick is re-asked on every step, so the
        ranking decides which node runs only once the feature stops answering; a pick feature that
        keeps answering asks for one more node, one step at a time -- and staying quiet while one of
        its own nodes is open is the feature's own obligation (a dedupe tag is the mechanism; see
        `docs/conduct-state.md`, the features section), never something the core guesses for it.
        """
        sb = self.pick_line()
        write_feature(self.features(), 'always', insert_decl('always', timing='pick'),
                      ALWAYS_OFFER_IMPL)

        sizes = [len(sb.dag()['nodes'])]
        for step in range(3):
            action = sb.next()
            sizes.append(len(sb.dag()['nodes']))
            self.assertIn(action['action'], ('insert_node', 'dispatch'), action)

        self.assertEqual(3, sizes[1], f'the first `next` did not insert exactly one node: {sizes}')
        diffs = [after - before for before, after in zip(sizes, sizes[1:])]
        self.assertTrue(all(0 <= step <= 1 for step in diffs),
                        f'a `next` inserted more than one node: {sizes}')

    def test_a_pick_feature_is_not_asked_at_a_close(self):
        """A feature occupies ONE cell of the two-axis table. A `pick`-timing folder is not asked at a
        close, whatever its implementation would answer there."""
        sb = self.coding_line()
        write_feature(self.features(), 'pickfix', insert_decl('pickfix', timing='pick'),
                      insert_impl())
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done', {'deliverable': 'x'})

        action = sb.close_round(NODE)

        self.assertEqual('dispatch', action['action'], action)
        self.assertEqual(2, len(sb.dag()['nodes']),
                         'a pick-timing feature inserted at a close it never claimed')


class TestABrokenOfferIsContained(FeatureBase):
    """A feature that cannot produce its node costs its own offer, never the close.

    The contract's rule (`extensions.md`, 「运行时宽容，体检严格」): a feature that explodes at its
    timing prints one `WARNING:` line, contributes nothing that pass, and the pipeline carries on. The
    offer is where a feature explodes, and the text builder is the part of it that runs anyway -- so
    both halves are pinned here: the shape check (`feature_host._usable_offer`, a wrong field) and the
    containment around the eager text evaluation (`actions._insertions_due`, a raise).
    """

    def test_a_builder_that_raises_is_skipped_and_the_close_still_dispatches(self):
        """A well-shaped offer whose `spec_of` raises: `next --after` answers anyway -- the node is
        settled, the graph does not grow, one WARNING says whose offer it was, and the line is not
        left waiting for a retry that would fail the same way."""
        sb = self.finished_head()
        write_feature(self.features(), 'boom', insert_decl('boom'), BOOM_OFFER_IMPL)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            action = sb.close_round(NODE)

        self.assertEqual('dispatch', action['action'], action)
        self.assertEqual(NODE2, action['node_id'])
        self.assertEqual(2, len(sb.dag()['nodes']), 'the skipped offer edited the graph anyway')
        self.assertEqual([NODE], sb.path_nodes(), 'the node was not closed')
        self.assertEqual([NODE2], sb.holding_ids())
        self.assertTrue((sb.root / '.pf' / 'meta' / 'launch' / 'cyc' / NODE2
                         / 'prompt.txt').is_file(), 'the close did not ask for the next session')
        lines = [line for line in err.getvalue().splitlines() if line.startswith('WARNING:')]
        self.assertEqual(1, len(lines), f'the feature did not get exactly one warning: {lines}')
        self.assertIn('feature boom', lines[0])
        self.assertIn('the template is broken', lines[0])
        # ... and the raise is not a permanent state: the line goes on with an ordinary step
        self.assertEqual('dispatch', sb.plan()['action'])

    def test_a_bad_shaped_offer_is_skipped_by_field(self):
        """`tags` as one string and a `title` that is not a string: the shape is checked before the
        graph edit exists, so each is refused with a warning naming the field, and the close answers
        as the ordinary one it would have been with no feature installed."""
        sb = self.finished_head()
        root = self.features()
        write_feature(root, 'wrong-tags', insert_decl('wrong-tags'), WRONG_TAGS_IMPL)
        write_feature(root, 'wrong-title', insert_decl('wrong-title'), WRONG_TITLE_IMPL)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            action = sb.close_round(NODE)

        self.assertEqual('dispatch', action['action'], action)
        self.assertEqual(2, len(sb.dag()['nodes']), 'a refused offer still edited the graph')
        self.assertEqual([NODE2], sb.holding_ids())
        text = err.getvalue()
        self.assertIn('feature wrong-tags', text)
        self.assertIn('`tags` must be a list of strings', text)
        self.assertIn('feature wrong-title', text)
        self.assertIn('`title` must be a non-empty string', text)

    def test_the_text_is_built_once_so_the_mint_never_asks_the_feature_again(self):
        """The builder answers on its first call and raises on any later one: the close inserts the
        node and writes its spec from that one text, with the minted uuid filled in.

        This is what keeps a raise out of `mint`, which runs after the close has landed -- the one
        moment the pipeline cannot take back.
        """
        sb = self.finished_head()
        write_feature(self.features(), 'once', insert_decl('once'), ONCE_IMPL)

        action = sb.close_round(NODE)

        self.assertEqual('insert_node', action['action'], action)
        made = action['node_id']
        nodes = sb.dag()['nodes']
        self.assertEqual(3, len(nodes), f'the node was not inserted: {action}')
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')
        self.assertIn(made, spec, 'the spec was not rewritten with the minted uuid')
        self.assertNotIn('%DUID%', spec, 'the spec still carries the placeholder')

    def test_a_pick_timing_builder_that_raises_still_opens_the_dispatch(self):
        """The same containment at the other insertion cell: the pick's offer raises, so the pick is
        the ordinary one -- the ranked node is pinned and dispatched, and nothing is inserted."""
        sb = self.pick_line()
        write_feature(self.features(), 'boom-pick', insert_decl('boom-pick', timing='pick'),
                      BOOM_OFFER_IMPL)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            action = sb.next()

        self.assertEqual('dispatch', action['action'], action)
        self.assertEqual(NODE, action['node_id'])
        self.assertEqual(2, len(sb.dag()['nodes']), 'the skipped pick offer edited the graph')
        self.assertEqual([NODE], sb.holding_ids())
        lines = [line for line in err.getvalue().splitlines() if line.startswith('WARNING:')]
        self.assertEqual(1, len(lines), f'the feature did not get exactly one warning: {lines}')
        self.assertIn('feature boom-pick', lines[0])


class TestTheOffersFields(FeatureBase):
    """`fields` is the feature's own data: the action's own cells are not a door into the answer.

    The shape check (`feature_host._usable_offer`) refuses a `fields` that is not a dict; what it
    cannot know is which keys belong to the action. `actions._insertions_due` drops those by name
    (`RESERVED_FIELDS`) with one `WARNING:` each, so a feature cannot rename the close's answer --
    the line it belongs to, the kind of action it is, the note or the steps a caller reads.
    """

    def test_an_offer_cannot_rewrite_the_actions_own_cells(self):
        """An offer whose `fields` carries `cycle` / `action` / `note` / `steps` after a real close:
        each reserved key is dropped with a warning naming it, and the action a caller reads is the
        close's own -- an `insert_node` for the line it settled, with the offer's own `note` and the
        core's steps, not the feature's."""
        sb = self.finished_head()
        write_feature(self.features(), 'liar', insert_decl('liar'), RESERVED_FIELDS_IMPL)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            action = sb.close_round(NODE)

        self.assertEqual('insert_node', action['action'], action)
        self.assertEqual('cyc', action['cycle'], 'an offer renamed the line the close settled')
        self.assertEqual('the fixture asked for this node', action['note'])
        self.assertIn('insert a node with the generated spec', ' '.join(action['steps']))
        self.assertEqual({'metric': 'fixture'}, action['insertions'][0]['fields'],
                         'a reserved key survived in the nested insertion')
        self.assertEqual(3, len(sb.dag()['nodes']), 'the close did not insert its node')
        self.assertEqual([NODE], sb.path_nodes())

        text = err.getvalue()
        for key in ('action', 'cycle', 'note', 'steps'):
            self.assertIn(f"{key!r} is the action's own cell", text)
        self.assertEqual(4, text.count('field is reserved'),
                         f'one warning per dropped key was not printed: {text}')


class TestADoorMayNotEndTheProcess(FeatureBase):
    """`sys.exit()` is a raise written differently, and the runtime contains it the same way.

    `SystemExit` is a `BaseException`, so on its own it walks through every containment there is and
    takes the step with it: nothing written, no line on stderr, and the exit code the feature chose.
    The validator already refuses it (a door may not raise on a stand-in project), so this is the
    runtime saying the same thing at each of the three doors a feature's own code runs behind: the
    gate call, the offer call, and the text builder the offer handed over.
    """

    def test_a_gate_that_exits_the_process_is_skipped_with_one_warning(self):
        """The gate calls `sys.exit`: the dispatch goes through as if it had said nothing, and the
        process carrying the line is still the tool's, not the feature's."""
        sb = self.coding_line()
        write_feature(self.features(), 'exit-gate', block_decl('exit-gate'), EXIT_GATE_IMPL)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            action = sb.plan()

        self.assertEqual('dispatch', action['action'], action)
        self.assertIn('WARNING: feature exit-gate failed', err.getvalue())
        self.assertIn('the fixture gate wants the process to end', err.getvalue())

    def test_an_offer_that_exits_the_process_is_skipped_and_the_close_still_dispatches(self):
        """The offer ends the process before it has an answer: the close answers as the ordinary one
        -- the node is settled, the graph does not grow, one warning names the offer -- and the step
        the line was waiting for is taken."""
        sb = self.finished_head()
        write_feature(self.features(), 'exit-offer', insert_decl('exit-offer'), EXIT_OFFER_IMPL)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            action = sb.close_round(NODE)

        self.assertEqual('dispatch', action['action'], action)
        self.assertEqual(NODE2, action['node_id'])
        self.assertEqual(2, len(sb.dag()['nodes']), 'the skipped offer edited the graph anyway')
        self.assertEqual([NODE], sb.path_nodes(), 'the node was not closed')
        lines = [line for line in err.getvalue().splitlines() if line.startswith('WARNING:')]
        self.assertEqual(1, len(lines), f'the feature did not get exactly one warning: {lines}')
        self.assertIn('feature exit-offer', lines[0])
        self.assertIn('the fixture offer wants the process to end', lines[0])

    def test_a_builder_that_exits_the_process_costs_its_offer_and_nothing_else(self):
        """The builder ends the process where the eager text was to come from: that offer contributes
        nothing, and the close still settles the node and dispatches the next one."""
        sb = self.finished_head()
        write_feature(self.features(), 'exit-build', insert_decl('exit-build'), EXIT_BUILDER_IMPL)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            action = sb.close_round(NODE)

        self.assertEqual('dispatch', action['action'], action)
        self.assertEqual(NODE2, action['node_id'])
        self.assertEqual(2, len(sb.dag()['nodes']), 'the skipped offer edited the graph anyway')
        self.assertEqual([NODE], sb.path_nodes(), 'the node was not closed')
        lines = [line for line in err.getvalue().splitlines() if line.startswith('WARNING:')]
        self.assertEqual(1, len(lines), f'the feature did not get exactly one warning: {lines}')
        self.assertIn('feature exit-build', lines[0])
        self.assertIn('the fixture template wants the process to end', lines[0])
