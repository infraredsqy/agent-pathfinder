"""The maintenance channel: thresholds in the cycle's policies, and the node they insert.

The rule this file is built around: the judgement happens at the CLOSE, each metric's number comes
from wherever its `source` says (`test_seconds` from the ticket's `reading` key, `issue_pool_weight`
from the issue tool that owns the pool), the lines come from the cycle's own policies, and a
crossing line puts a maintenance node between the node just closed and the node the close had
already picked. **The insertion is a graph edit and nothing else**: no session is opened, no prompt
is written and the line's hand is not moved -- the next dispatch picks the new node up by the usual
ranking. A project that configures no line gets the close it always got -- that is the regression
this channel lives or dies by, and it is the first case here.

The side where this line really runs (the graph edit through real processes, a whole line walked
to the end, the form the note's receiver sees) lives in `cases_maintenance_run.py`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE, NODE2, NODE3, pipeline, normalise
from harness import (InProcessDagManage, TOOLS_DIR, WorkerSession, _fresh_module, _run_proc,
                     run_state_manage, STATE_MANAGE)

__all__ = ['TestMaintenanceDedup', 'TestMaintenanceChannel', 'TestMaintenanceThresholds',
           'TestMaintenanceScope', 'TestIssueRepairSwitch']

#: The tool that owns the issue pool; `pool_weight()` asks it for the number through the same CLI.
ISSUE_MANAGE = TOOLS_DIR / 'issue' / 'issue_manager.py'

#: The line the maintenance cases write onto a cycle (`test_seconds_limit`), and the number their
#: tickets report around it.
LINE = 120


def seed_pool(sb, *issues) -> None:
    """Put items into the pool through its own command (the same source `pool_weight()` reads)."""
    for item in issues:
        _run_proc(sb.root, [ISSUE_MANAGE, 'add', '--id', item['id'], '--title', item['id'],
                            '--severity', item['severity'],
                            '--decision', item.get('decision', 'autofix')])


def set_config(sb, key, value, *extra):
    """Write a setting into the project through its own command (the same source this channel reads).

    `--global` goes through the sandbox's own home directory (`Sandbox.home`) -- the layer this case
    reads is the layer `pf config` writes.
    """
    return run_state_manage(sb.root, 'config', 'set', key, value, *extra)


def coding_line(test, *, policies=(), reading=None, dispatch=True, pair=True):
    """A coding line, started and pinned on its head node, with a ticket ready for it.

    The DAG is a real one -- `NODE` the head, `NODE2` waiting for it -- because the insertion
    rewrites the edge BETWEEN the node that was closed and the node the close had already picked: a
    pair with no edge between them would exercise only the no-op half of that.

    `policies` writes the cycle's own numbers (`test_seconds_limit`, `issue_pool_weight_limit`),
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


class ReadingWorker(WorkerSession):
    """A worker whose ticket carries a maintenance reading -- what `handoff.py -o` takes verbatim.

    `reading` is a plain `outputs` key, so nothing about the report path changes: the
    worker measures, writes down what it measured, and the close is what reads it back.
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
    which is also the only way to tell a channel that inserts one node from a channel that inserts
    one node forever.
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
        # Every `add` the tool runs mints exactly one node, and that node is a maintenance node
        # (the maintenance channel is the only caller). `Sb.next()` answers in-process here, so the
        # uuid the graph just grew is readable without parsing anything.
        if InProcessDagManage.last_added:
            self.maintenance_nodes.add(InProcessDagManage.last_added)
        return super().work(action)

    def reading_for(self, node):
        if node in self.maintenance_nodes:
            return dict(self.fixed)
        return dict(self.reading)


class TestMaintenanceChannel(Base):
    """The channel itself: a project with nothing configured, a crossed line, and a reading under
    its line."""

    # ── the regression floor: with no number configured, the close carries not one word more ─────

    def test_without_a_threshold_the_close_and_the_graph_are_untouched(self):
        """With not one number configured in the project, this channel is wholly inert: the return value, the
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
        configured = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=10)

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
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=None)

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 2)

    def test_a_reading_under_the_line_inserts_nothing(self):
        """The reading is under the line: this is the control -- same project, same line, only the reading
        pressed below it."""
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE - 1)

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(sb.current_node(), NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 2, 'nothing crossed, nothing inserted')
        self.assertEqual(sb.path_nodes(), [NODE])

    # ── crossing: the graph really grows a node, the edges are right, the new one is pinned ────────

    def test_a_crossed_line_inserts_a_node_between_the_close_and_its_pick(self):
        """The reading reaches the line: the close's settle inserts a new node between the node just closed
        and the node it had picked -- **the graph and nothing else**.

        One assertion pins five things: the graph grew one node, the two new edges are wired right,
        the old **direct** edge is gone, the answer is an insertion rather than a dispatch, and the
        line put down only the node it closed -- its hand is empty, because an insertion pins
        nothing. Which node runs next is the next dispatch's question
        (`test_the_next_dispatch_picks_the_inserted_node`).
        """
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 40)

        action = sb.close_round(NODE)

        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 3, 'the maintenance node was not inserted')
        made = action['node_id']
        self.assertNotIn(made, (NODE, NODE2))

        # the edges: old node → maintenance node → the original pick, and the old direct edge is gone
        self.assertEqual(nodes[made]['dependencies'], [NODE],
                         'the maintenance node must be the closed node\'s direct dependent')
        self.assertEqual(nodes[NODE2]['dependencies'], [made],
                         'the picked node must now wait for the maintenance node')
        self.assertNotIn(NODE2, nodes[NODE]['dependencies'])
        self.assertEqual(nodes[NODE]['status'], 'completed')
        self.assertEqual(nodes[NODE2]['status'], 'todo', 'being picked is not being run')
        self.assertEqual(nodes[made]['status'], 'todo')
        self.assertEqual(sb.path_nodes(), [NODE])

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
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 40)

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
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 40)

        action = sb.close_round(NODE)

        made = action['node_id']
        landed = Path(pipeline.nodes.node_spec_path(sb.root, made))
        self.assertTrue(landed.is_file(), f'{landed} is not where the writer said it would be')
        self.assertIn(f'(this spec lands at: {landed.as_posix()})',
                      landed.read_text(encoding='utf-8'))

    def test_the_spec_path_is_built_in_one_file(self):
        """In the implementation package, `nodes.py` is the only file that builds the `spec.md` segment."""
        impl = TOOLS_DIR / 'conduct' / 'pipeline'
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
        sb.set_policy('cyc', 'test_seconds_limit', LINE)
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
        """The maintenance node's spec is generated by the tool: why it was inserted, the scope of this pass,
        and a mechanically judgeable completion rule."""
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 40)

        action = sb.close_round(NODE)
        made = action['node_id']
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')
        node = sb.dag()['nodes'][made]

        # why: which metric, what it read, where the line is
        self.assertIn('test_seconds', spec)
        self.assertIn(f'{LINE + 40:g}s', spec)
        self.assertIn(f'{LINE:g}s', spec)
        self.assertIn('test_seconds_limit', spec)
        # scope: this pass does one thing only
        self.assertIn('this pass does one thing only', spec)
        # how: name which method card owns this kind of debt
        self.assertIn('How to do it', spec)
        self.assertIn('method: pf-test-audit', spec)
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

    def test_the_spec_and_the_note_name_the_line_and_the_field_it_came_from(self):
        """How much the line is and which field on the note was read: both the spec and the close's note name
        them, and that is where a human looks them up."""
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 5)
        action = sb.close_round(NODE)
        made = action['node_id']
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')

        self.assertEqual(len(sb.dag()['nodes']), 3)
        self.assertIn('test_seconds_limit', spec)
        self.assertIn(f'over the line {LINE:g}s in this line\'s policies', action['note'])

    def test_the_reading_exactly_on_the_line_is_over_it(self):
        """The line is the last acceptable number: the moment the reading lands exactly on it, the debt is
        already at its limit."""
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE)

        sb.close_round(NODE)

        self.assertEqual(len(sb.dag()['nodes']), 3)

    def test_the_next_dispatch_picks_the_inserted_node(self):
        """Keep walking after the insert: the next dispatch picks the maintenance node by the usual
        ranking and writes its prompt, while the deferred node still waits.

        This is the other half of the split the insertion made: the graph grew a node, and WHICH node
        runs next is a decision of the picking rules -- not of whoever inserted it.
        """
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 40)
        inserted = sb.close_round(NODE)
        made = inserted['node_id']

        again = sb.next()

        self.assertEqual(again['action'], 'dispatch')
        self.assertEqual(again['node_id'], made)
        self.assertEqual(sb.holding_ids(), [made], 'the pick is what pins it')
        self.assertIn(made, sb.prompt_of(again))
        self.assertTrue(Path(again['prompt']).is_file())
        self.assertEqual(sb.dag()['nodes'][NODE2]['status'], 'todo')


class TestIssueRepairSwitch(Base):
    """This channel's project-level switch: never set = on, written `on` = on, written `off` = no insert
    even when a line is crossed.

    The switch is read by the **tool** at the closing beat, not by the session -- so the three cases
    walk the same close and the same judgement, differing only in that one slot of the project
    settings. The never-set one is the regression floor: it and the `on` one must judge exactly the
    same thing.
    """

    LINE = LINE

    def crossed_line(self, issue_repair=None, global_value=None):
        """A two-node coding line: the line is configured, the note carries an over-the-line reading, and the
        close can walk at any time."""
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'test_seconds_limit', self.LINE)
        if global_value is not None:
            set_config(sb, pipeline.ISSUE_REPAIR_KEY, global_value, '--global')
        if issue_repair is not None:
            set_config(sb, pipeline.ISSUE_REPAIR_KEY, issue_repair)
        sb.next()                                  # the dispatch for NODE
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': self.LINE + 40}})
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
                         'the edge the channel would have rewritten is untouched')

    def test_a_project_that_never_set_it_inserts_the_node(self):
        """A project that never set this key: a crossed line still inserts -- that is the factory default,
        and the behaviour since this channel appeared."""
        sb = self.crossed_line()

        self.assertTrue(pipeline.issue_repair_on(sb.root))
        action = sb.close_round(NODE)

        self.assert_inserted(sb, action)

    def test_the_switch_on_judges_exactly_what_a_project_that_never_set_it_judges(self):
        """Written `on` and never set: the same close judges item for item the same thing (the two uuids
        aside)."""
        unset = self.crossed_line()
        unset_hit = pipeline.crossed(unset.root, unset.cycle(), unset.read_signal('cyc', NODE))
        on = self.crossed_line(issue_repair='on')
        on_hit = pipeline.crossed(on.root, on.cycle(), on.read_signal('cyc', NODE))

        def without_spec(hit):
            """The `spec_of` in a hit is a closure (it takes the new node's uuid), so comparing it
            means nothing."""
            return {k: v for k, v in hit.items() if k != 'spec_of'}

        self.assertIsNotNone(unset_hit)
        self.assertEqual(without_spec(on_hit), without_spec(unset_hit),
                         'writing the default down must not change the judgement')
        self.assertTrue(pipeline.issue_repair_on(on.root))
        self.assert_inserted(unset, unset.close_round(NODE))
        self.assert_inserted(on, on.close_round(NODE))

    def test_the_switch_off_keeps_a_crossed_line_from_inserting(self):
        """A project that turned it off: the reading still crosses the line (the judgement can read it), but
        the close inserts no new node."""
        sb = self.crossed_line(issue_repair='off')
        ticket = sb.read_signal('cyc', NODE)

        # the crossing is on the record: the reading is on the note, the line is in the policies, and both
        # are still in hand
        self.assertEqual(pipeline.reading_of(ticket), {'test_seconds': float(self.LINE + 40)})
        self.assertEqual(pipeline.thresholds_for(sb.cycle())['test_seconds_limit'], self.LINE)
        self.assertFalse(pipeline.issue_repair_on(sb.root))
        self.assertIsNone(pipeline.crossed(sb.root, sb.cycle(), ticket),
                          'the switch must shut the judgement, not the measurement')
        action = sb.close_round(NODE)

        self.assert_not_inserted(sb, action)

    def test_the_global_layer_can_turn_it_off_too(self):
        """The global layer counts too: it says `off`, the project says not a word, and this project inserts
        nothing."""
        sb = self.crossed_line(global_value='off')

        self.assertFalse(pipeline.issue_repair_on(sb.root))
        self.assert_not_inserted(sb, sb.close_round(NODE))

    def test_the_project_layer_beats_the_global_one(self):
        """The project layer beats the global one: the global layer is off, the project writes `on`, and the
        project wins."""
        sb = self.crossed_line(global_value='off', issue_repair='on')

        self.assertTrue(pipeline.issue_repair_on(sb.root))
        self.assert_inserted(sb, sb.close_round(NODE))

    def test_the_two_packages_spell_the_switch_the_same_way(self):
        """The writing side (`pf config`'s table) and the reading side (this channel) each hold a copy of the
        key name, the vocabulary and the factory default: this case compares them.

        `pipeline` and `state` are two packages each loaded by path, and importing one into the other
        would loop, so each side writes its own copy -- two copies drifting apart is how a project that
        turned the channel off still gets an insert at the close.
        """
        spec = _fresh_module('state_manage_switch_probe', STATE_MANAGE).CONFIG_SPEC
        entry = spec[pipeline.ISSUE_REPAIR_KEY]      # a key-name mismatch is a KeyError right here
        self.assertEqual(entry['kind'], 'choice')
        self.assertEqual(tuple(entry['values']), tuple(pipeline.ISSUE_REPAIR_VALUES))
        self.assertEqual(entry['default'], pipeline.ISSUE_REPAIR_DEFAULT)
        self.assertEqual(pipeline.ISSUE_REPAIR_DEFAULT, 'on',
                         'the channel is on unless a project says otherwise')


class TestMaintenanceThresholds(Base):
    """The two lines live in the cycle's `policies`: the same slot as `tags` / `when_shelved`, written
    with `cycle policy`."""

    def test_the_lines_are_the_lines_own_policies(self):
        """The two lines are written onto the cycle's `policies`, and the instructions list gains not a word --
        the ledger is for things somebody said."""
        sb = self.make()
        sb.add_node(NODE, 'Write code', mode='auto', tags=['coding'])
        sb.start_cycle(tags=['coding'])

        sb.set_policy('cyc', 'test_seconds_limit', 120)
        sb.set_policy('cyc', 'issue_pool_weight_limit', 16)

        self.assertEqual(pipeline.thresholds_for(sb.cycle()),
                         {'test_seconds_limit': 120, 'issue_pool_weight_limit': 16})
        self.assertEqual(sb.instruct_list(), [], 'the account is for things somebody said')

    def test_a_line_without_them_carries_none(self):
        """A line that never configured them does not carry these two keys at all: `thresholds_for` reads
        empty, and the channel is simply inert for it."""
        sb = self.make()
        sb.add_node(NODE, 'Write code', mode='auto', tags=['coding'])
        sb.start_cycle(tags=['coding'])

        self.assertEqual(pipeline.thresholds_for(sb.cycle()), {})
        self.assertIsNone(sb.cycle().get('policies'))

    def test_the_numeric_lines_come_from_the_one_policy_table(self):
        """The two numeric lines come from the one source, `state.cycles.POLICY_NUMBERS`.

        The integer `parallel` is a different kind of key in the same slot and is not among them; with
        not one numeric line configured this reading is empty -- which is what `crossed`'s non-empty
        gate rests on.
        """
        stated = {'parallel': 3, 'test_seconds_limit': 120}
        self.assertEqual(pipeline.thresholds_for({'id': 'c', 'policies': stated}),
                         {'test_seconds_limit': 120}, '`parallel` is not a key this channel reads')
        self.assertEqual(pipeline.thresholds_for({'id': 'c', 'policies': {'parallel': 3}}), {},
                         'with only an integer policy configured this reading must be empty')
        self.assertEqual(
            pipeline.thresholds_for({'id': 'c', 'policies': {'test_seconds_limit': 1,
                                                            'issue_pool_weight_limit': 2}}),
            {'test_seconds_limit': 1, 'issue_pool_weight_limit': 2})


class TestMaintenanceScope(Base):
    """Domain scoping: the two metrics belong to the code branch, and another line reads the same
    number but does nothing about it."""

    def test_a_line_that_is_not_about_code_reads_the_number_and_does_nothing(self):
        """Another line reads the same number (both the line and the reading are in hand) but does nothing --
        these two metrics belong to the code branch."""
        sb = self.make()
        sb.add_node(NODE, 'Write copy', mode='auto', tags=['writing'])
        sb.add_node(NODE2, 'Write copy 2', mode='auto', tags=['writing'], deps=[NODE])
        sb.start_cycle(tags=['writing'])
        sb.set_policy('cyc', 'test_seconds_limit', 1)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': 9999}})

        # the reading and the line are both in hand and the crossing counts: the reason for doing nothing
        # is that this line is not the code branch
        ticket = sb.read_signal('cyc', NODE)
        self.assertEqual(pipeline.reading_of(ticket), {'test_seconds': 9999.0})
        self.assertEqual(pipeline.thresholds_for(sb.cycle())['test_seconds_limit'], 1)
        self.assertIsNone(pipeline.crossed(sb.root, sb.cycle(), ticket))

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 2)

    def test_the_issue_pool_line_inserts_too(self):
        """The second metric takes the same road: the pool's weighted sum crosses its line and it also inserts
        a maintenance node.

        This slot asks no session for a number: the fixture lays out the pool, and the issue tool
        computes it on the spot at the close.
        """
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'issue_pool_weight_limit', 8)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done', {'deliverable': 'x'})
        seed_pool(sb, {'id': 'debt-p0', 'severity': 'P0'}, {'id': 'debt-p2', 'severity': 'P2'})

        inserted = sb.close_round(NODE)

        made = inserted['node_id']
        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 3)
        self.assertEqual(nodes[made]['dependencies'], [NODE])
        self.assertEqual(nodes[NODE2]['dependencies'], [made])
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')
        self.assertIn('issue_pool_weight', spec)
        self.assertIn('issue_pool_weight_limit', spec)
        # the "how" points at the pool-clearing card, and names the tool along with it
        self.assertIn('How to do it', spec)
        self.assertIn('method: pf-issue-repair', spec)
        self.assertIn('pf issue weight', spec)
        # where this number comes from: the spec says outright that the tool computes it, and the note does
        # not carry it
        self.assertIn('weight', spec)
        self.assertIn('the note does not carry it', spec)
        self.assertNotIn('"reading": {"issue_pool_weight', spec)


class TestMaintenanceDedup(Base):
    """One metric may hold only one unsettled maintenance node at a time: once somebody is already
    repaying the debt, no second one is inserted."""

    def test_a_metric_already_in_hand_is_not_inserted_twice(self):
        """While a maintenance node is still in play (`todo`), the same metric crossing again inserts nothing
        more -- the fixer is one."""
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 40)
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
        sb.set_policy('cyc', 'test_seconds_limit', LINE)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': LINE + 40}})
        # walk one slot at a time (parallel pressed to 1 first): each close advances only its own node, so
        # the judgements after it are legible.
        sb.set_policy('cyc', 'parallel', 1)
        first = sb.close_round(NODE)
        made = first['node_id']
        self.assertNotIn(made, (NODE, NODE2))

        # the maintenance node ran (its reading is under the line) → its work is delivered and this metric
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
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 40)
        sb.close_round(NODE)
        self.assertEqual(sb.node(NODE)['status'], 'completed')
        self.assertEqual(sb.path_nodes(), [NODE])
