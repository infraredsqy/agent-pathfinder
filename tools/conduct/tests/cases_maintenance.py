"""The maintenance plugin: a crossed debt line inserts a node at the close.

This file drives the plugin the way the core does -- through the declaration, not through a name the
core holds. The declaration (`plugins/maintenance/plugin.json`) supplies the knobs
(`test_seconds_limit`, `issue_pool_weight_limit`, and the `maintenance` switch), the dedupe tag and
the method name; its `impl.py` is the judgement; and the graph edit is the core's `insertion.py`.
Nothing here reads a `pipeline.maintenance` any more -- that module is gone, and this is the
behaviour that replaced it.

The rule the file is built around is unchanged: the judgement happens at the CLOSE, each metric's
number comes from wherever its declared `source` says (`test_seconds` from the ticket's `reading`
key, `issue_pool_weight` from the issue tool that owns the pool), the lines arrive as knobs from the
whole resolution chain (node -> line -> project -> machine -> factory), and a crossing line puts a
maintenance node between the node just closed and the node the close had already picked. **The
insertion is a graph edit and nothing else**: no session is opened, no prompt is written and the
line's hand is not moved -- the next dispatch picks the new node up by the usual ranking. A project
that configures no line gets the close it always got -- that is the regression this plugin lives or
dies by, and it is the first case here.

Every case that needs the shipped declaration carries `requires_plugin('maintenance')`: deleting
that folder is a supported state of this tree (the core keeps closing and dispatching), so the
behaviour cases skip rather than fail there. The mechanism cases that must run without it point
`PF_PLUGINS_DIR` at their own folder -- `cases_plugin.py`.

The side where this line really runs (the graph edit through real processes, a whole line walked to
the end, the form the note's receiver sees) lives in `cases_maintenance_run.py`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE, NODE2, NODE3, pipeline, normalise
from harness import (CONDUCT_DIR, InProcessDagManage, STATE_MANAGE, TOOLS_DIR, WorkerSession,
                     _fresh_module, _run_proc, requires_plugin, run_state_manage)

__all__ = ['TestMaintenanceDedup', 'TestMaintenanceChannel', 'TestMaintenanceThresholds',
           'TestMaintenanceScope', 'TestMaintenanceSwitch', 'ReadingWorker', 'SelfHealingWorker',
           'coding_line']

#: The tool that owns the issue pool; the plugin asks it for the pool's number through the same CLI.
ISSUE_MANAGE = TOOLS_DIR / 'issue' / 'issue_manager.py'

#: The line the maintenance cases write onto a cycle (`test_seconds_limit`, a declared knob), and the
#: number their tickets report around it.
LINE = 120

#: The two names the declaration owns and this file therefore has to state once.
SWITCH = 'maintenance'
TEST_LINE = 'test_seconds_limit'
POOL_LINE = 'issue_pool_weight_limit'
TEST_TAG = 'maint:test_seconds'


def seed_pool(sb, *issues) -> None:
    """Put items into the pool through its own command (the same source the plugin reads)."""
    for item in issues:
        _run_proc(sb.root, [ISSUE_MANAGE, 'add', '--id', item['id'], '--title', item['id'],
                            '--severity', item['severity'],
                            '--decision', item.get('decision', 'autofix')])


def set_config(sb, key, value, *extra):
    """Write a knob into the project through its own command (the same chain this plugin reads).

    `--global` goes through the sandbox's own home directory (`Sandbox.home`), and `--node <id>` /
    `--cycle <id>` at the layer each names: the layer a case writes is the layer `pf config` writes.
    """
    return run_state_manage(sb.root, 'config', 'set', key, value, *extra)


def coding_line(test, *, policies=(), reading=None, dispatch=True, pair=True):
    """A coding line, started and pinned on its head node, with a ticket ready for it.

    The DAG is a real one -- `NODE` the head, `NODE2` waiting for it -- because the insertion
    rewrites the edge BETWEEN the node that was closed and the node the close had already picked: a
    pair with no edge between them would exercise only the no-op half of that.

    `policies` writes the line's own numbers (`test_seconds_limit`, `issue_pool_weight_limit`),
    `dispatch` walks the line one step and leaves the head's ticket carrying `reading`, and `pair`
    adds the second node -- a case whose subject is the line's own settings takes just the head.
    """
    sb = test.make()
    sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
    if pair:
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
    sb.start_cycle(tags=['coding'])
    for key, value in policies:
        sb.set_policy('cyc', key, value)
    if dispatch:
        sb.next()                                  # the dispatch for NODE
        outputs = {'deliverable': 'x'}
        if reading is not None:
            outputs['reading'] = {'test_seconds': reading}
        sb.write_signal('cyc', NODE, 'completed', 'done', outputs)
    return sb


def close_offers(sb, *, target=None, ticket=None):
    """What the plugins offer at this close, through the core's own door.

    The judgement is asked exactly the way `_close_own_node` asks it: one `Context` carrying the line,
    the node being closed, that node's ticket and the graph. No graph edit is made here -- the offer
    list is the whole answer this reads.
    """
    return pipeline.insertions('close', pipeline.Context(
        sb.root, cycle=sb.cycle(), node_id=NODE,
        sig=ticket if ticket is not None else sb.read_signal('cyc', NODE),
        dag=sb.dag(), target=target or {'id': NODE2, 'title': 'Write code B'}))


class ReadingWorker(WorkerSession):
    """A worker whose ticket carries a maintenance reading -- what `handoff.py -o` takes verbatim.

    `reading` is a plain `outputs` key, so nothing about the report path changes: the worker
    measures, writes down what it measured, and the close is what reads it back.
    """

    def __init__(self, sb, reading, cycle='cyc', **kw):
        super().__init__(sb, cycle=cycle, **kw)
        self.reading = dict(reading)

    def outputs_for(self, action, deliverable, node):
        return {**super().outputs_for(action, deliverable, node), 'reading': self.reading_for(node)}

    def reading_for(self, node):
        return dict(self.reading)


class SelfHealingWorker(ReadingWorker):
    """The same session, and the one difference that matters: it fixes what it was sent for.

    A maintenance node exists because a measurement crossed its line, and its whole scope is to pull
    THAT measurement back under it. A worker that reported the same over-the-line number after doing
    the work would be saying it delivered while the debt stood -- so it reports the fixed number,
    which is also the only way to tell a plugin that inserts one node from one that inserts forever.
    """

    #: The reading the maintenance node reports: back under the line it was inserted for.
    FIXED = 20

    def __init__(self, sb, reading, fixed=None, cycle='cyc', **kw):
        super().__init__(sb, reading, cycle=cycle, **kw)
        self.fixed = dict(fixed if fixed is not None else {'test_seconds': self.FIXED})
        # Each instance starts from the real graph: `last_added` is whatever the last `add` in this
        # process minted, which belongs to another sandbox's test as often as not.
        self.maintenance_nodes = set()

    def work(self, action):
        # Every `add` the tool runs mints exactly one node, and that node is an inserted one (a
        # plugin's insertion is the only caller). `Sb.next()` answers in-process here, so the uuid
        # the graph just grew is readable without parsing anything.
        if InProcessDagManage.last_added:
            self.maintenance_nodes.add(InProcessDagManage.last_added)
        return super().work(action)

    def reading_for(self, node):
        if node in self.maintenance_nodes:
            return dict(self.fixed)
        return dict(self.reading)


@requires_plugin(SWITCH)
class TestMaintenanceChannel(Base):
    """The plugin itself: a project with nothing configured, a crossed line, and a reading under
    its line."""

    # ── the regression floor: with no number configured, the close carries not one word more ─────

    def test_without_a_threshold_the_close_and_the_graph_are_untouched(self):
        """With not one number configured in the project, this plugin is wholly inert: the return value, the
        graph and the ledger are byte-for-byte what they are without it.

        Two projects walk the same line, the one difference being that one of them wrote one more line
        onto its cycle. The configured one still inserts **nothing** (the reading is under the line),
        so the two ledgers must match word for word -- the policies slot aside (it is supposed to
        differ). The rest
        follows `TestNodeTags.test_an_untagged_cycle_picks_what_it_always_picked`: a timestamp is a
        legitimate difference, and nothing else may change by a word.
        """
        def walked(data):
            """Drop the policies slot from the ledger: what the close wrote is all in what is left."""
            data = normalise(data)
            for cycle in data.get('cycles') or []:
                cycle.pop('policies', None)
            return data

        control = coding_line(self, reading=10)
        configured = coding_line(self, policies=[(TEST_LINE, LINE)], reading=10)

        control_action = control.close_round(NODE)
        configured_action = configured.close_round(NODE)

        self.assertEqual(control_action['action'], 'dispatch')
        self.assertEqual(normalise(control_action), normalise(configured_action),
                         'a line that is not crossed must change nothing at all')
        self.assertEqual(configured.current_node(), NODE2, 'the close still pins its own pick')
        self.assertEqual(len(configured.dag()['nodes']), 2, 'no node was inserted')

        for rel in ('.pf/ideas.json', '.pf/state.json'):
            left = walked(json.loads(control.tree_contents()[rel]))
            right = walked(json.loads(configured.tree_contents()[rel]))
            self.assertEqual(left, right, f'{rel} must be what it always was')

    def test_a_ticket_carrying_no_reading_never_inserts_anything(self):
        """The reading is self-reported by the session: with no `reading` on the note, the clearest line
        does nothing."""
        sb = coding_line(self, policies=[(TEST_LINE, LINE)], reading=None)

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 2)

    def test_a_reading_under_the_line_inserts_nothing(self):
        """The reading is under the line: this is the control -- same project, same line, only the reading
        pressed below it."""
        sb = coding_line(self, policies=[(TEST_LINE, LINE)], reading=LINE - 1)

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(sb.current_node(), NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 2, 'nothing crossed, nothing inserted')
        self.assertEqual(sb.path_nodes(), [NODE])

    # ── crossing: the graph really grows a node, the edges are right, the new one is not pinned ────

    def test_a_crossed_line_inserts_a_node_between_the_close_and_its_pick(self):
        """The reading reaches the line: the close's settle inserts a new node between the node just closed
        and the node it had picked -- **the graph and nothing else**.

        One assertion pins five things: the graph grew one node, the two new edges are wired right,
        the old **direct** edge is gone, the answer is an insertion rather than a dispatch, and the
        line put down only the node it closed -- its hand is empty, because an insertion pins
        nothing. Which node runs next is the next dispatch's question
        (`test_the_next_dispatch_picks_the_inserted_node`).
        """
        sb = coding_line(self, policies=[(TEST_LINE, LINE)], reading=LINE + 40)

        action = sb.close_round(NODE)

        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 3, 'the maintenance node was not inserted')
        made = action['node_id']
        self.assertNotIn(made, (NODE, NODE2))

        # the edges: old node -> maintenance node -> the original pick, and the old direct edge is gone
        self.assertEqual(nodes[made]['dependencies'], [NODE],
                         'the maintenance node must be the closed node\'s direct dependent')
        self.assertEqual(nodes[NODE2]['dependencies'], [made],
                         'the picked node must now wait for the maintenance node')
        self.assertNotIn(NODE2, nodes[NODE]['dependencies'])
        self.assertEqual(nodes[NODE]['status'], 'completed')
        self.assertEqual(nodes[NODE2]['status'], 'todo', 'being picked is not being run')
        self.assertEqual(nodes[made]['status'], 'todo')
        self.assertEqual(sb.path_nodes(), [NODE])

        # the declaration's dedupe tag is what the outside writes to name this debt, and it rides
        # with the line's own filter set
        self.assertIn(TEST_TAG, nodes[made]['tags'])
        self.assertIn('coding', nodes[made]['tags'])

        # an insertion is not a dispatch: no prompt, and the hand stays where the close left it
        self.assertEqual(action['action'], 'insert_node')
        self.assertNotIn('prompt', action)
        self.assertNotIn('prompt_text', action)
        self.assertEqual(sb.holding_ids(), [], 'an insertion must not pin a node')
        self.assertEqual(sb.cycle()['status'], 'active', 'an insertion must not park the line')

    def test_an_insertion_opens_no_session(self):
        """The whole difference from a dispatch, read off the disk: no prompt file, no started stamp.

        The inserted node's launch directory does not exist at all. The prompt is written by the
        dispatch that picks the node, one step later -- which is also why `next --after … --spawn`
        cannot open a session here: the answer is not a dispatch.
        """
        sb = coding_line(self, policies=[(TEST_LINE, LINE)], reading=LINE + 40)

        action = sb.close_round(NODE)

        made = action['node_id']
        launch = sb.root / '.pf' / 'meta' / 'launch' / 'cyc'
        self.assertFalse((launch / made).exists(),
                         f'an insertion wrote a prompt file for {made}: {launch / made}')
        self.assertFalse(pipeline.started_marker(sb.root, 'cyc', made).is_file(),
                         'an insertion stamped a session as started')
        # what it DID write: the node's own text, where the one path says it lands
        self.assertEqual(Path(action['spec']),
                         Path(pipeline.nodes.node_spec_path(sb.root, made)))
        self.assertTrue(Path(action['spec']).is_file())

    def test_the_generated_spec_lands_where_the_one_path_says(self):
        """Where the generated spec lands is settled in one place, `nodes.node_spec_path`.

        The copy that was written, the sentence the spec reports at its own end, and the one the
        writer computed are one and the same file.
        """
        sb = coding_line(self, policies=[(TEST_LINE, LINE)], reading=LINE + 40)

        action = sb.close_round(NODE)

        made = action['node_id']
        landed = Path(pipeline.nodes.node_spec_path(sb.root, made))
        self.assertTrue(landed.is_file(), f'{landed} is not where the writer said it would be')
        self.assertIn(f'(this spec lands at: {landed.as_posix()})',
                      landed.read_text(encoding='utf-8'))

    def test_the_spec_path_is_built_in_one_file(self):
        """In the implementation package, `nodes.py` is the only file that builds the `spec.md` segment."""
        impl = CONDUCT_DIR / 'pipeline'
        sites = sorted(path.name for path in sorted(impl.glob('*.py'))
                       if "'spec.md'" in path.read_text(encoding='utf-8'))

        self.assertEqual(['nodes.py'], sites)

    def test_a_crossed_line_only_defers_the_node_it_picked(self):
        """A multi-slot line meets maintenance: only the picked slot is deferred, the rest of the fill that
        came up in the same pass is pinned as usual, and the insertion takes no slot at all.

        The fill list is handed over whole (`parallel` minus what is still held), and the maintenance
        node stands in only for the picked slot; if nobody pins the rest of the list it just sits
        empty -- those nodes are ready, yet neither held nor waiting in the graph for anyone. The
        inserted node itself is NOT held: it waits in the graph for the next pick, which is what keeps
        "insert" and "start running" apart.
        """
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.add_node(NODE3, 'Write code C', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])            # default parallel=1: the start pins one slot only
        sb.set_policy('cyc', 'parallel', 3)
        sb.set_policy('cyc', TEST_LINE, LINE)
        sb.next()                                  # the dispatch for NODE
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': LINE + 40}})

        action = sb.close_round(NODE)

        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 4, 'the maintenance node was not inserted')
        made = action['node_id']
        self.assertNotIn(made, (NODE, NODE2, NODE3))
        deferred = [u for u in (NODE2, NODE3) if nodes[u]['dependencies'] == [made]]
        self.assertEqual(len(deferred), 1, 'exactly one fill node waits behind the maintenance node')
        keep = [u for u in (NODE2, NODE3) if u != deferred[0]][0]
        self.assertEqual(nodes[keep]['dependencies'], [NODE], 'the rest of the fill is untouched')
        self.assertEqual(nodes[keep]['status'], 'todo')
        self.assertEqual(sb.holding_ids(), [keep],
                         'the rest of the fill keeps its slot, and the inserted node takes none')

    def test_the_inserted_node_carries_why_what_scope_and_a_mechanical_verdict(self):
        """The maintenance node's spec is generated by the plugin: why it was inserted, the scope of this
        pass, and a mechanically judgeable completion rule."""
        sb = coding_line(self, policies=[(TEST_LINE, LINE)], reading=LINE + 40)

        action = sb.close_round(NODE)
        made = action['node_id']
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')
        node = sb.dag()['nodes'][made]

        # why: which metric, what it read, where the line is
        self.assertIn('test_seconds', spec)
        self.assertIn(f'{LINE + 40:g}s', spec)
        self.assertIn(f'{LINE:g}s', spec)
        self.assertIn(TEST_LINE, spec)
        self.assertIn('Why this node was inserted', spec)
        # scope: this pass does one thing only
        self.assertIn('this pass does one thing only', spec)
        # how: name which method skill owns this kind of debt -- the declaration's own method
        self.assertIn('How to do it', spec)
        self.assertIn('method: pf-test-audit', spec)
        self.assertIn('~/.agents/skills/pf-test-audit/SKILL.md', spec)
        # completion rule: pull it back under the line, and give a command that can be run
        self.assertIn('Completion rule (mechanically judgeable)', spec)
        self.assertIn('**below**', spec)
        self.assertIn('**measure**', spec)
        # the spec carries no implementation detail: the template guesses no function name / file path /
        # line number
        self.assertNotIn('.py', spec)
        # the node on the graph says what it is in one line
        self.assertIn('Maintenance: ', node['title'])
        self.assertIn(str(LINE + 40), node['title'])

    def test_the_spec_and_the_note_name_the_line_and_the_knob_it_came_from(self):
        """How much the line is and which knob it was read from: both the spec and the close's note name
        them, and that is where a human looks them up.

        The wording moved from "this line's policies" to the resolution chain ("in force for this
        close"): the threshold may be stated on the line, the project, the machine or a node, and the
        value the close read is the one that counts -- so the node has to say which knob that was.
        """
        sb = coding_line(self, policies=[(TEST_LINE, LINE)], reading=LINE + 5)
        action = sb.close_round(NODE)
        made = action['node_id']
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')

        self.assertEqual(len(sb.dag()['nodes']), 3)
        self.assertIn('the line in force for this close', spec)
        self.assertIn(f'**{LINE:g}s**', spec)
        self.assertIn(f'(the `{TEST_LINE}` knob', spec)
        self.assertIn(f'over the line {LINE:g}s in force for this close', action['note'])
        self.assertIn(f'(the `{TEST_LINE}` knob)', action['note'])

    def test_the_reading_exactly_on_the_line_is_over_it(self):
        """The line is the last acceptable number: the moment the reading lands exactly on it, the debt is
        already at its limit."""
        sb = coding_line(self, policies=[(TEST_LINE, LINE)], reading=LINE)

        sb.close_round(NODE)

        self.assertEqual(len(sb.dag()['nodes']), 3)

    def test_the_next_dispatch_picks_the_inserted_node(self):
        """Keep walking after the insert: the next dispatch picks the maintenance node by the usual
        ranking and writes its prompt, while the deferred node still waits.

        This is the other half of the split the insertion made: the graph grew a node, and WHICH node
        runs next is a decision of the picking rules -- not of whoever inserted it.
        """
        sb = coding_line(self, policies=[(TEST_LINE, LINE)], reading=LINE + 40)
        inserted = sb.close_round(NODE)
        made = inserted['node_id']

        again = sb.next()

        self.assertEqual(again['action'], 'dispatch')
        self.assertEqual(again['node_id'], made)
        self.assertEqual(sb.holding_ids(), [made], 'the pick is what pins it')
        self.assertIn(made, sb.prompt_of(again))
        self.assertTrue(Path(again['prompt']).is_file())
        self.assertEqual(sb.dag()['nodes'][NODE2]['status'], 'todo')


@requires_plugin(SWITCH)
class TestMaintenanceSwitch(Base):
    """The plugin's switch: an ordinary word knob on the resolution chain, and never set = on.

    The switch used to be a settings key with a second copy of its name on the reading side. Now it is
    the declaration's own `enable_knob`, merged into the one knob table and enforced by
    `plugin_host` for every plugin alike -- so this class drives the same close through each layer of
    the chain, differing only in which layer states `off`.
    """

    def crossed_line(self, *, layer=None, value=None):
        """A two-node coding line: the line is configured, the note carries an over-the-line reading, and
        the close can walk at any time.

        `layer` names where the switch is written: `project` (the default), `global`, `cycle` or
        `node`; `value=None` writes nothing at all (the never-set case).
        """
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', TEST_LINE, LINE)
        if value is not None:
            if layer == 'global':
                set_config(sb, SWITCH, value, '--global')
            elif layer == 'cycle':
                sb.set_policy('cyc', SWITCH, value)
            elif layer == 'node':
                set_config(sb, SWITCH, value, '--node', NODE)
            else:
                set_config(sb, SWITCH, value)
        sb.next()                                  # the dispatch for NODE
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': LINE + 40}})
        return sb

    def assert_inserted(self, sb, action):
        """It was inserted: the graph grew a node, the two edges are wired right, no hand moved."""
        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 3, 'the maintenance node was not inserted')
        made = action['node_id']
        self.assertNotIn(made, (NODE, NODE2))
        self.assertEqual(nodes[made]['dependencies'], [NODE])
        self.assertEqual(nodes[NODE2]['dependencies'], [made])
        self.assertEqual(action['action'], 'insert_node')
        self.assertEqual(sb.holding_ids(), [], 'an insertion pins nothing')

    def assert_not_inserted(self, sb, action):
        """Not inserted: the close walks its original pick as usual, and the graph grows nothing."""
        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(sb.current_node(), NODE2)
        self.assertEqual(sb.path_nodes(), [NODE])
        self.assertEqual(len(sb.dag()['nodes']), 2, 'nothing may be inserted')
        self.assertEqual(sb.dag()['nodes'][NODE2]['dependencies'], [NODE],
                         'the edge the plugin would have rewritten is untouched')

    def test_a_project_that_never_set_it_inserts_the_node(self):
        """A project that never set this knob: a crossed line still inserts -- that is the factory default
        in the declaration, and the behaviour since this plugin appeared."""
        sb = self.crossed_line()

        self.assertEqual(pipeline.resolve_knob(sb.root, SWITCH, cycle=sb.cycle(), node=NODE), 'on')
        self.assert_inserted(sb, sb.close_round(NODE))

    def test_the_knob_is_the_one_the_declaration_names(self):
        """The name, the kind and the factory value all come out of the declaration: there is no second
        copy of them on the reading side any more.

        `enable_knob` is what the host enforces, and the merged table is where the CLI, the form and the
        doctor read the same cell from -- so one assertion on the merged table plus one on the
        declaration covers every door.
        """
        spec = pipeline.knob_specs()[SWITCH]
        self.assertEqual(spec['kind'], 'switch')
        self.assertEqual(spec['default'], 'on')
        self.assertEqual(pipeline.registry().load_all()[0]['name'], SWITCH)
        decl = pipeline.registry().load_all()[0]['decl']
        self.assertEqual(decl['enable_knob'], SWITCH)
        self.assertEqual(decl['timing'], 'close')
        self.assertEqual(decl['action'], 'insert')

    def test_the_switch_off_keeps_a_crossed_line_from_inserting(self):
        """A project that turned it off: the reading still crosses the line (the number is in hand), but
        the close inserts no new node."""
        sb = self.crossed_line(value='off')

        # the crossing is on the record: the reading is on the note, the line is in force, and both
        # are still in hand -- the switch shuts the judgement, not the measurement
        self.assertEqual(pipeline.resolve_knob(sb.root, SWITCH, cycle=sb.cycle(), node=NODE), 'off')
        self.assertEqual(close_offers(sb), [], 'the switch must shut the judgement')
        action = sb.close_round(NODE)

        self.assert_not_inserted(sb, action)

    def test_the_global_layer_can_turn_it_off_too(self):
        """The global layer counts too: it says `off`, the project says not a word, and this project inserts
        nothing."""
        sb = self.crossed_line(layer='global', value='off')

        self.assertEqual(pipeline.knob_sources(sb.root, cycle=sb.cycle())['maintenance'], 'global')
        self.assert_not_inserted(sb, sb.close_round(NODE))

    def test_the_cycle_layer_can_turn_it_off(self):
        """The line is a layer of its own: `cycle policy cyc maintenance off` shuts this line only, and the
        close reads it before the project layer."""
        sb = self.crossed_line(layer='cycle', value='off')

        sources = pipeline.knob_sources(sb.root, cycle=sb.cycle(), node=NODE)
        self.assertEqual(sources['maintenance'], 'cycle')
        self.assert_not_inserted(sb, sb.close_round(NODE))

    def test_the_node_layer_can_turn_it_off(self):
        """The narrowest layer wins: one node's own `config.json` says `off`, and that node's close does not
        insert even though every wider layer says on."""
        sb = self.crossed_line(layer='node', value='off')

        node_layer = sb.root / '.pf' / 'nodes' / NODE / 'config.json'
        self.assertEqual(json.loads(node_layer.read_text(encoding='utf-8')), {SWITCH: 'off'})
        sources = pipeline.knob_sources(sb.root, cycle=sb.cycle(), node=NODE)
        self.assertEqual(sources['maintenance'], 'node')
        self.assert_not_inserted(sb, sb.close_round(NODE))

    def test_the_project_layer_beats_the_global_one(self):
        """The project layer beats the global one: the global layer is off, the project writes `on`, and the
        project wins."""
        sb = self.crossed_line(layer='global', value='off')
        set_config(sb, SWITCH, 'on')

        self.assertEqual(pipeline.resolve_knob(sb.root, SWITCH, cycle=sb.cycle(), node=NODE), 'on')
        self.assert_inserted(sb, sb.close_round(NODE))


@requires_plugin(SWITCH)
class TestMaintenanceThresholds(Base):
    """The lines behind the two metrics are knobs, and they resolve on the whole chain -- not just in
    the cycle's own `policies` slot."""

    def test_the_lines_are_the_lines_own_policies(self):
        """The two lines are written onto the cycle's `policies`, and the instructions list gains not a word --
        the ledger is for things somebody said."""
        sb = self.make()
        sb.add_node(NODE, 'Write code', mode='auto', tags=['coding'])
        sb.start_cycle(tags=['coding'])

        sb.set_policy('cyc', TEST_LINE, 120)
        sb.set_policy('cyc', POOL_LINE, 16)

        self.assertEqual(pipeline.resolve_knob(sb.root, TEST_LINE, cycle=sb.cycle()), 120)
        self.assertEqual(pipeline.resolve_knob(sb.root, POOL_LINE, cycle=sb.cycle()), 16)
        self.assertEqual(pipeline.knob_sources(sb.root, cycle=sb.cycle())[TEST_LINE], 'cycle')
        self.assertEqual(sb.instruct_list(), [], 'the account is for things somebody said')

    def test_a_line_without_them_carries_none(self):
        """A line that never configured them does not carry these two keys at all: both resolve to nothing,
        and the plugin is simply inert for it."""
        sb = self.make()
        sb.add_node(NODE, 'Write code', mode='auto', tags=['coding'])
        sb.start_cycle(tags=['coding'])

        self.assertIsNone(pipeline.resolve_knob(sb.root, TEST_LINE, cycle=sb.cycle()))
        self.assertIsNone(pipeline.resolve_knob(sb.root, POOL_LINE, cycle=sb.cycle()))
        self.assertIsNone(sb.cycle().get('policies'))

    def test_a_threshold_stated_on_the_project_counts_too(self):
        """The chain is the point: a threshold written on the project layer (or the machine's) is the one
        the close reads, with no line policy at all -- "in force for this close", not "in this line's
        policies"."""
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        set_config(sb, TEST_LINE, LINE)            # the project layer, not `cycle policy`
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': LINE + 40}})

        sources = pipeline.knob_sources(sb.root, cycle=sb.cycle())
        self.assertEqual(sources[TEST_LINE], 'project')
        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'insert_node')
        self.assertEqual(len(sb.dag()['nodes']), 3)
        self.assertIn(f'(the `{TEST_LINE}` knob', action['note'])

    def test_a_wider_layer_is_overridden_by_the_line(self):
        """Narrow beats wide: the project says 200, the line says 60, and the line's 60 is what judges."""
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        set_config(sb, TEST_LINE, 200)
        sb.set_policy('cyc', TEST_LINE, 60)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': 100}})

        self.assertEqual(pipeline.resolve_knob(sb.root, TEST_LINE, cycle=sb.cycle()), 60)
        action = sb.close_round(NODE)              # 100 >= 60: over the line the close read

        self.assertEqual(action['action'], 'insert_node')
        self.assertIn('over the line 60s', action['note'])


@requires_plugin(SWITCH)
class TestMaintenanceScope(Base):
    """What the plugin reads, and from where: the ticket, the pool tool, and the code branch only."""

    def test_a_line_that_is_not_about_code_reads_the_number_and_does_nothing(self):
        """Another line reads the same number (both the line and the reading are in hand) but does nothing --
        these two metrics belong to the code branch."""
        sb = self.make()
        sb.add_node(NODE, 'Write copy', mode='auto', tags=['writing'])
        sb.add_node(NODE2, 'Write copy 2', mode='auto', tags=['writing'], deps=[NODE])
        sb.start_cycle(tags=['writing'])
        sb.set_policy('cyc', TEST_LINE, 1)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': 9999}})

        # the reading and the line are both in hand and the crossing counts: the reason for doing nothing
        # is that this line is not the code branch
        self.assertEqual(pipeline.resolve_knob(sb.root, TEST_LINE, cycle=sb.cycle()), 1)
        self.assertEqual(close_offers(sb), [])

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 2)

    def test_the_issue_pool_line_inserts_too(self):
        """The second metric takes the same road: the pool's weighted sum crosses its line and it also inserts
        a maintenance node.

        This slot asks no session for a number: the fixture lays out the pool, and the issue tool
        computes it on the spot at the close -- through `ctx.capture`, the plugin's own child process.
        """
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', POOL_LINE, 8)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done', {'deliverable': 'x'})
        seed_pool(sb, {'id': 'debt-p0', 'severity': 'P0'}, {'id': 'debt-p2', 'severity': 'P2'})

        inserted = sb.close_round(NODE)

        made = inserted['node_id']
        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 3)
        self.assertEqual(nodes[made]['dependencies'], [NODE])
        self.assertEqual(nodes[NODE2]['dependencies'], [made])
        self.assertIn('maint:issue_pool_weight', nodes[made]['tags'])
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')
        self.assertIn('issue_pool_weight', spec)
        self.assertIn(POOL_LINE, spec)
        # the "how" points at the pool-clearing skill, and names the tool along with it
        self.assertIn('How to do it', spec)
        self.assertIn('method: pf-issue-repair', spec)
        self.assertIn('pf issue weight', spec)
        # where this number comes from: the spec says outright that the tool computes it, and the note does
        # not carry it
        self.assertIn('weight', spec)
        self.assertIn('the note does not carry it', spec)
        self.assertNotIn('"reading": {"issue_pool_weight', spec)

    def test_the_number_comes_from_the_pool_tool(self):
        """The pool's number is asked of the tool that owns the pool, and it is not asked when the line is
        under its line: `ctx.capture` is the one road, so the child process count says which half ran."""
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done', {'deliverable': 'x'})
        seed_pool(sb, {'id': 'debt-p2', 'severity': 'P2'})

        # nothing configured: the plugin contributes nothing and starts no child
        ticket = sb.read_signal('cyc', NODE)
        calls = []
        real = pipeline._capture

        def spy(argv, cwd):
            calls.append([str(a) for a in argv])
            return real(argv, cwd)

        from harness import patched
        with patched(pipeline, '_capture', spy):
            self.assertEqual(close_offers(sb, ticket=ticket), [])
        self.assertEqual([], [c for c in calls if 'issue_manager.py' in ' '.join(c)],
                         'the pool tool was asked with nothing configured')

        # configure the pool line, over the pool's own weight: now the tool is asked
        sb.set_policy('cyc', POOL_LINE, 1)
        with patched(pipeline, '_capture', spy):
            offers = close_offers(sb, ticket=ticket)
        self.assertEqual(1, len(offers), offers)
        self.assertTrue([c for c in calls if 'issue_manager.py' in ' '.join(c)],
                        f'the pool number did not come from the pool tool: {calls}')


@requires_plugin(SWITCH)
class TestMaintenanceDedup(Base):
    """One metric may hold only one unsettled maintenance node at a time: once somebody is already
    repaying the debt, no second one is inserted."""

    def test_a_metric_already_in_hand_is_not_inserted_twice(self):
        """While a maintenance node is still in play (`todo`), the same metric crossing again inserts nothing
        more -- the fixer is one."""
        sb = coding_line(self, policies=[(TEST_LINE, LINE)], reading=LINE + 40)
        first = sb.close_round(NODE)
        self.assertEqual(first['action'], 'insert_node')
        made = first['node_id']
        self.assertNotIn(made, (NODE, NODE2))
        self.assertEqual(sb.node(made)['status'], 'todo')

        # the next dispatch is what pins it; then its own close reports another over-the-line number:
        # no second insert, and the close walks on.
        sb.next()
        self.assertEqual(sb.holding_ids(), [made])
        sb.write_signal('cyc', made, 'completed', 'still over',
                        {'deliverable': 'x', 'reading': {'test_seconds': LINE + 10}})
        second = sb.close_round(made)
        self.assertEqual(second['action'], 'dispatch')
        self.assertEqual(len(sb.dag()['nodes']), 3, 'a second maintenance node was inserted')
        self.assertEqual(sb.current_node(), NODE2, 'the deferred pick still came next')

    def test_a_finished_maintenance_node_frees_the_metric_again(self):
        """The maintenance node is delivered (`completed`) and the debt crosses again: the next over-the-line
        reading inserts a fresh node as usual."""
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.add_node(NODE3, 'Write code C', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', TEST_LINE, LINE)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': LINE + 40}})
        # walk one slot at a time (parallel pressed to 1 first): each close advances only its own node, so
        # the judgements after it are legible.
        sb.set_policy('cyc', 'parallel', 1)
        first = sb.close_round(NODE)
        made = first['node_id']
        self.assertNotIn(made, (NODE, NODE2))

        # the maintenance node ran (its reading is under the line) -> its work is delivered and this metric
        # is insertable again
        sb.next()                                  # the dispatch that pins what the insertion created
        self.assertEqual(sb.holding_ids(), [made])
        sb.write_signal('cyc', made, 'completed', 'fixed',
                        {'deliverable': 'x', 'reading': {'test_seconds': 10}})
        sb.close_round(made)
        sb.set_current_node(NODE2)
        # further on, another node reports another over-the-line number: the debt stands, so it must come in
        # for one more pass
        sb.write_signal('cyc', NODE2, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': LINE + 50}})
        sb.close_round(NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 4,
                         'a repaired metric that crosses again must insert a fresh node')

    def test_the_insert_lands_after_the_node_is_closed(self):
        """The order of the graph edit: the old node is marked first, only then is the maintenance node
        created -- the dedup graph read must not count the node just delivered as still in play."""
        sb = coding_line(self, policies=[(TEST_LINE, LINE)], reading=LINE + 40)
        sb.close_round(NODE)
        self.assertEqual(sb.node(NODE)['status'], 'completed')
        self.assertEqual(sb.path_nodes(), [NODE])

    def test_only_a_todo_node_holds_the_metric(self):
        """The dedup read is "still open", and `shelved` is not: a human who never revives one would leave
        the plugin mute forever if it counted."""
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.add_node('dddddddd-4444-4444-8444-dddddddddddd', 'an old maintenance pass',
                    status='shelved', tags=[TEST_TAG])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', TEST_LINE, LINE)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': LINE + 40}})

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'insert_node',
                         'a shelved maintenance node must not hold the metric')
        self.assertEqual(len(sb.dag()['nodes']), 4)

    def test_the_switch_has_no_second_table_of_its_own(self):
        """The writing side keeps no copy of this plugin's switch: the name reaches every door through
        the one merged table.

        A missing key used to be a `KeyError` in a copied spec table; now the door that used to copy it
        (`CONFIG_SPEC`) is gone with the old config module, and `knob_specs()` is the one source.
        """
        written = _fresh_module('state_manage_no_config_spec', STATE_MANAGE)
        self.assertFalse(hasattr(written, 'CONFIG_SPEC'),
                         'the state package has grown a second settings table again')
        self.assertEqual(pipeline.knob_specs()[SWITCH]['kind'], 'switch')
