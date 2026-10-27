"""The node: what one cycle step IS, and the closing call that ends it.

Everything here is about the surviving shape, in the terms the model is stated in: `state.json`
holds the whole cycle (there is no per-round file and no second pointer), one node's work is done
when its terminal signal is on disk, the DAG's node status is the single source of truth for "did
it succeed", and the only thing that ends a step is the worker's own `next --after <node>`.
"""

from __future__ import annotations

import ast
import sys
import traceback
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import NODE, NODE2, NODE3, Base, pipeline, skill_of  # noqa: E402
from cases_maintenance import POOL, TICKET  # noqa: E402
from harness import (CONDUCT_DIR, Closer, Driver, WorkerSession,  # noqa: E402
                     _dag_hook, requires_plugin, stop_after)

#: The implementation package's own directory. `graph_readers` recognizes "a caller inside the
#: package" by the stack frame's **directory**, not by a substring of the file name: the paths of the
#: entry `conduct_pipeline.py` and of `state/pipeline_loader.py` both contain `pipeline`, and a
#: substring match would count them as inside too, silently loosening the boundary that "one caller
#: outside the package raises right there".
_IMPL_DIR = CONDUCT_DIR / 'pipeline'


class TestNodeShape(Base):
    """One node = one cycle step = one session, recorded in the cycle itself."""

    def test_a_pinned_cycle_dispatches_one_pf_conduct_session(self):
        sb = self.ready_sandbox()
        action = sb.next()

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(skill_of(action), 'pf-conduct')
        self.assertEqual(action['node_id'], NODE)
        # The dispatch names the node, not a cell: there is no index left in the action.
        self.assertNotIn('stage', action)
        self.assertNotIn('round', action)
        self.assertEqual(sb.current_node(), NODE)

    def test_the_cycle_record_holds_the_whole_cycle(self):
        """`state.json` and nothing else: no `loops/`, no per-round file, no second pointer."""
        sb = self.ready_sandbox()
        sb.next()
        cycle = sb.cycle()

        self.assertEqual([h['id'] for h in cycle['holding']], [NODE])
        self.assertTrue(cycle['holding'][0]['since'], 'opening a node did not stamp the clock')
        self.assertEqual(cycle.get('path'), [])
        self.assertFalse((sb.root / '.pf' / 'loops').exists(),
                         'the deleted per-round directory came back')
        # What a project's `.pf/` holds: the record itself, then the three things `meta/` carries
        # (this step's own prompt file, and the one lock the three tools share).
        self.assertEqual(sorted(p.name for p in (sb.root / '.pf').iterdir()),
                         ['.gitattributes', '.gitignore', 'ideas.json', 'meta', 'nodes',
                          'state.json'])
        self.assertEqual(sorted(p.name for p in (sb.root / '.pf' / 'meta').iterdir()),
                         ['launch', 'ledger.lock'])

    def test_the_node_is_recorded_when_the_step_opens_not_when_it_reports(self):
        """That is what lets a human add nodes mid-run without steering the step in flight."""
        sb = self.ready_sandbox()
        sb.next()
        sb.add_node(NODE2, 'a node added mid-run', mode='auto')
        sb.write_signal('cyc', NODE, 'completed', 'done')

        self.assertEqual(sb.current_node(), NODE)
        self.assertEqual(sb.next()['node_id'], NODE)

    def test_only_the_workers_own_close_ends_a_step(self):
        """With the ticket on disk and no closing call, the step is open -- stably, and for as
        many times as `next` is asked. A tool that closed it here would drop the worker's own
        answer on the floor."""
        sb = self.ready_sandbox()
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done')

        first = sb.next()
        self.assertEqual(first['action'], 'wait')
        self.assertIn('--after', first['close_with'])
        self.assertIn(NODE, first['close_with'])
        self.assertEqual(sb.current_node(), NODE)
        self.assertEqual(sb.path(), [])

        # asked again: same answer, and nothing on disk moved
        before = sb.tree_hashes()
        again = sb.next()
        self.assertEqual(again['action'], 'wait')
        self.assertEqual(again['close_with'], first['close_with'])
        self.assertEqual(sb.tree_hashes(), before)

    def test_the_close_appends_one_path_step_and_clears_the_pointer(self):
        sb = self.ready_sandbox()
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done')
        action = sb.close_round(NODE)

        self.assertNotEqual(action['action'], 'wait')
        path = sb.path()
        self.assertEqual(len(path), 1)
        self.assertEqual(path[0]['node'], NODE)
        self.assertEqual(path[0]['result'], 'completed')
        self.assertTrue(path[0]['started_at'] and path[0]['ended_at'])
        self.assertIsNone(sb.current_node())
        self.assertEqual(sb.dag_status(NODE), 'completed')

    def test_the_signal_stays_on_disk_after_the_close(self):
        """The ticket is the evidence that the node was attempted; closing is not deleting."""
        sb = self.ready_sandbox()
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done')
        sb.close_round(NODE)

        self.assertEqual(sb.read_signal('cyc', NODE)['status'], 'completed')


class TestSettleAndClose(Base):
    """`next --after <node>`: the worker's own closing call, and what it decides."""

    def pinned(self, extra_nodes=1):
        """A cycle pinned on NODE, with a ticket written by its worker."""
        sb = self.ready_sandbox(extra_nodes=extra_nodes)
        return sb

    def test_a_ticket_on_disk_is_what_means_the_work_is_done(self):
        """The signal IS the outcome. There is no verdict to ask for and nobody to ask."""
        sb = self.pinned()
        sb.write_signal('cyc', NODE, 'completed', 'pf-conduct finished')
        waiting = sb.next()

        self.assertEqual(waiting['action'], 'wait')
        self.assertEqual(waiting['node_id'], NODE)
        self.assertIn(NODE, waiting['close_with'])
        self.assertIn('--after', waiting['close_with'])

    def test_a_pass_closes_the_node_and_points_the_cycle_at_the_next_one(self):
        sb = self.pinned(extra_nodes=1)
        sb.write_signal('cyc', NODE, 'completed', 'done')
        action = sb.close_round(NODE)

        self.assertEqual(sb.path_nodes(), [NODE])
        self.assertEqual(sb.dag_status(NODE), 'completed')
        self.assertEqual(sb.current_node(), NODE2)
        # the closing call's own answer is the next node's dispatch, or the walk on to it
        self.assertIn(action['action'], ('dispatch', 'open_node', 'wait'))
        if action['action'] == 'dispatch':
            self.assertEqual(action['node_id'], NODE2)

    def test_a_stop_parks_a_cycle_that_still_has_work(self):
        sb = self.pinned(extra_nodes=1)
        sb.write_signal('cyc', NODE, 'completed', 'done')
        action = sb.close_round(NODE, decision='stop')

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertEqual(sb.cycle()['status'], 'inactive')
        self.assertEqual(sb.dag_status(NODE), 'completed')
        self.assertIsNone(sb.current_node(), 'the step that was closed is over, stop or not')
        self.assertEqual(sb.path_nodes(), [NODE])

    def test_a_stop_on_the_last_node_writes_the_same_status(self):
        """Stopping has one word to write: `inactive`. "Is there work left in the graph" asks about
        the graph at that moment, and a graph grows -- whether a stopped line should run on is a
        human's call."""
        sb = self.pinned(extra_nodes=0)
        sb.write_signal('cyc', NODE, 'completed', 'done')
        action = sb.close_round(NODE, decision='stop')

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertEqual(sb.cycle()['status'], 'inactive')

    def test_a_non_delivery_goes_shelved_in_the_dag_and_the_cycle_goes_inactive(self):
        """`failed` on the ticket is the worker saying it delivered nothing. The DAG's status is the
        single source of truth for that (the node becomes `shelved`: out of play, waiting for a human),
        the ticket says why in the worker's own words, and the run stops for a human."""
        sb = self.pinned()
        sb.write_signal('cyc', NODE, 'failed', 'I produced nothing')
        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertEqual(sb.dag_status(NODE), 'shelved')
        self.assertEqual(sb.path()[0]['result'], 'failed')
        self.assertEqual(sb.cycle()['status'], 'inactive')
        self.assertEqual(sb.read_signal('cyc', NODE)['summary'], 'I produced nothing')
        self.assertIn('did not deliver', action.get('reason', ''))

    def test_a_non_delivery_stops_even_if_the_close_said_continue(self):
        """The outcome is read off the ticket, so a `--continue` cannot launder a failure."""
        sb = self.pinned()
        sb.write_signal('cyc', NODE, 'failed', 'nothing delivered')
        action = sb.close_round(NODE, decision='continue')

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertEqual(sb.dag_status(NODE), 'shelved')

    def test_stopped_parks_the_node_and_stops_the_run(self):
        """A ticket that is not `completed` is the worker saying it did not deliver: the node is
        parked in the DAG (`shelved`) and the line stops. The step records the word the ticket used."""
        sb = self.pinned()
        sb.write_signal('cyc', NODE, 'stopped', 'the user stopped it')
        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertIn(sb.path()[0]['result'], ('stopped', 'failed'))
        self.assertEqual(sb.dag_status(NODE), 'shelved', 'a non-delivery parked the node')
        self.assertEqual(sb.cycle()['status'], 'inactive')

    def test_the_close_can_pick_the_next_node(self):
        sb = self.pinned(extra_nodes=1)
        sb.write_signal('cyc', NODE, 'completed', 'done')
        action = sb.close_round(NODE, node=NODE2)

        self.assertEqual(sb.current_node(), NODE2)
        self.assertIn(action['action'], ('dispatch', 'open_node', 'wait'))

    def test_a_node_the_close_names_but_that_is_not_ready_stops(self):
        """Naming a node nobody can run is the owner's mistake, not a reason to substitute one."""
        sb = self.pinned(extra_nodes=1)
        sb.write_signal('cyc', NODE, 'completed', 'done')
        action = sb.close_round(NODE, node='nope-0000-4000-8000-000000000000')

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertIn('nope-0000-4000-8000-000000000000', action['reason'])
        self.assertIsNone(sb.current_node())

    def test_the_next_pick_excludes_the_node_the_step_just_ran(self):
        """The step's own node is finished, so it can never be picked again by the same close."""
        sb = self.pinned(extra_nodes=1)
        sb.write_signal('cyc', NODE, 'completed', 'done')
        sb.close_round(NODE)

        self.assertEqual(sb.path_nodes(), [NODE])
        self.assertEqual(sb.current_node(), NODE2)


class TestStopRule(Base):
    """Instructions may only TIGHTEN the stop rule, never loosen it.

    **Whether to carry on depends on the node's own mode alone**: `checkpoint` stops at the closing
    beat and hands back to a human; `manual`'s gate is in the slot's session (the whole slot moves
    back and forth with the user, and the note's `completed` is the user's "mark it done, carry on"),
    so it carries on like `auto` after the close; this one judgement has this one source.
    """

    def test_auto_carries_on_to_the_node_behind_it(self):
        """Of the three tiers only `auto` opens the next node by itself: it is the tier that "asks
        nothing along the way and carries on by itself at the close"."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.add_node(NODE2, 'carry on', mode='auto', deps=[NODE])
        sb.start_cycle()
        final, driver = self.run_to_stop(sb)

        self.assertEqual(final['action'], 'stop_pipeline')
        self.assertEqual(len(driver.dispatched), 2, 'both nodes were dispatched')
        self.assertEqual(sb.path_nodes(), [NODE, NODE2])

    def test_a_manual_node_carries_on_once_the_user_releases_it(self):
        """`manual`'s gate is in the slot's session: the whole slot moves back and forth with the
        user, and the note's `completed` is the user's "mark it done, carry on". The closing beat
        sets no second gate -- the line carries on just like `auto`."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='manual', deps=())
        sb.add_node(NODE2, 'the next one', mode='auto', deps=[NODE])
        sb.start_cycle()
        final, driver = self.run_to_stop(sb)

        self.assertEqual(final['action'], 'stop_pipeline')
        self.assertEqual(sb.path_nodes(), [NODE, NODE2])
        self.assertEqual(len(driver.dispatched), 2)

    def test_checkpoint_hands_back_to_the_user_at_the_end_of_its_node(self):
        """The middle tier: one round of the cycle runs, the record closes clean, then it stops and
        waits for a human to say "carry on".

        The only difference from `auto` is one place -- the close does not open the next node
        itself; inside a node the run is exactly the same.
        """
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='checkpoint')
        sb.add_node(NODE2, 'carry on', mode='auto', deps=[NODE])
        sb.start_cycle()
        final, driver = self.run_to_stop(sb)

        self.assertEqual(final['action'], 'stop_pipeline')
        self.assertEqual(sb.path_nodes(), [NODE], 'this node was handed back')
        self.assertEqual(sb.dag_status(NODE), 'completed')
        self.assertEqual(len(driver.dispatched), 1, 'the next node was not opened')
        self.assertEqual(sb.dag_status(NODE2), 'todo')
        self.assertIn('checkpoint', final['reason'])
        self.assertEqual(sb.cycle()['status'], 'inactive',
                         'stopping turns the line inactive; the command that puts it back in '
                         'service is in the reason')
        self.assertIn('cycle status cyc active', final['reason'])

    def test_a_continue_cannot_loosen_the_stop_rule(self):
        """The hole the rule exists to close: the closing call says carry on, the node's own mode
        says hand back to the user. The rule wins, and the worker is told why."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='checkpoint')
        sb.add_node(NODE2, 'the next one', mode='auto', deps=[NODE])
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done')
        action = sb.close_round(NODE, decision='continue')

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertIn('mode=checkpoint', action['reason'])

    def test_a_boundary_instruction_can_tighten_it(self):
        sb = self.make()
        ids = sb.add_chain(3)
        sb.start_cycle()
        sb.instruct('stop after two nodes', scope='cycle', target='boundary', stop_after_nodes=2)
        final, _ = self.run_to_stop(sb)

        self.assertEqual(final['action'], 'stop_pipeline')
        self.assertEqual(sb.path_nodes(), ids[:2])
        self.assertEqual(sb.dag_status(ids[2]), 'todo')

    def test_the_second_close_is_where_a_cap_of_two_stops(self):
        """Stop after N: at the N-th close every one of them has already been delivered."""
        sb = self.make()
        ids = sb.add_chain(2)
        sb.start_cycle()
        sb.instruct('stop after one', scope='cycle', target='boundary', stop_after_nodes=1)
        final, driver = self.run_to_stop(sb)

        self.assertEqual(sb.path_nodes(), [ids[0]])
        self.assertEqual(len(driver.dispatched), 1)

    def test_a_stop_after_nodes_one_means_one_node(self):
        """The v1 defect this pins: an off-by-one in the cap ran one node too many."""
        sb = self.make()
        ids = sb.add_chain(3)
        sb.start_cycle()
        driver = Driver(sb, worker=WorkerSession(sb), closer=Closer(on_node=stop_after(1)))
        final = driver.run()

        self.assertEqual(final['action'], 'stop_pipeline')
        self.assertEqual(sb.path_nodes(), ids[:1])


class TestTheApprovalAtTheGate(Base):
    """The human's "continue" at a checkpoint gate, and what the tool writes down for it.

    The gate's own sentence is that the next node opens only once the human says continue, and the
    command they say it with -- putting the line back in service -- is also where that word becomes a
    record: the delivery the line stopped on is approved. The approval belongs to one delivery, so a
    delivery that comes again lapses the old one by itself.
    """

    def parked_at_the_gate(self, behind='auto'):
        """One checkpoint node with a node behind it, the line parked at the gate."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='checkpoint')
        sb.add_node(NODE2, 'carry on', mode=behind, deps=[NODE])
        sb.start_cycle()
        action, _ = self.run_to_stop(sb)
        return sb, action

    def test_releasing_the_line_approves_the_delivery_it_stopped_on(self):
        sb, action = self.parked_at_the_gate()

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertEqual(sb.cycle()['status'], 'inactive', 'the gate parks the line')
        self.assertNotIn('approved_at', sb.node(NODE))

        sb.set_cycle_status('cyc', 'active')

        node = sb.node(NODE)
        self.assertIn('approved_at', node)
        self.assertGreaterEqual(node['approved_at'], node['completed_at'],
                                'an approval has to be at or after the delivery it is about')
        self.assertEqual(node['updated_at'], node['approved_at'],
                         'the record changed, so its own moment has to move with it')
        self.assertNotIn('approved_at', sb.node(NODE2), 'the node behind the gate was approved too')

    def test_a_line_that_ended_on_an_auto_node_records_nothing(self):
        """Releasing a line is the gate's word, and there is no gate here: the node is left exactly
        as it is -- not one byte, its own moment included."""
        sb = self.ready_sandbox()
        sb.write_signal('cyc', NODE, 'completed', 'done')
        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertEqual(sb.cycle()['status'], 'inactive')
        before = sb.node(NODE)

        sb.set_cycle_status('cyc', 'active')

        self.assertEqual(before, sb.node(NODE),
                         'releasing a line that did not stop at a checkpoint gate wrote to the node')

    def test_releasing_an_already_approved_gate_writes_nothing_at_all(self):
        """The repeat: the approval is already the record of this delivery, so the second release is
        a no-op down to the node's own moment."""
        sb, _ = self.parked_at_the_gate()
        sb.set_cycle_status('cyc', 'active')
        approved = sb.node(NODE)

        sb.set_cycle_status('cyc', 'inactive')
        sb.set_cycle_status('cyc', 'active')

        self.assertEqual(approved, sb.node(NODE),
                         'the second release touched the node the first one had approved')

    def test_a_gate_whose_node_did_not_deliver_records_nothing(self):
        """The checkpoint node closed without delivering (its ticket said no, so it is shelved):
        there is no delivery an approval could be about, so releasing the line writes nothing."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='checkpoint')
        sb.add_node(NODE2, 'carry on', mode='auto', deps=[NODE])
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'failed', 'produced nothing')
        sb.close_round(NODE)

        self.assertEqual(sb.dag_status(NODE), 'shelved')
        self.assertEqual(sb.cycle()['status'], 'inactive')
        before = sb.node(NODE)

        sb.set_cycle_status('cyc', 'active')

        self.assertEqual(before, sb.node(NODE))

    def test_a_node_taken_off_the_graph_does_not_fail_the_release(self):
        """The gate's node can be gone by the time the human says continue (`rm` took it off the
        graph, or a merge did). The release is their command and it still stands."""
        sb, _ = self.parked_at_the_gate()
        self.assertEqual(sb.cycle()['status'], 'inactive')
        sb.write_dag({})

        sb.set_cycle_status('cyc', 'active')

        self.assertEqual(sb.cycle()['status'], 'active')
        self.assertEqual(sb.dag()['nodes'], {})

    def test_a_checkpoint_delivery_closed_on_a_line_already_out_of_service(self):
        """The other way a line comes to hold an unapproved checkpoint delivery: the human took the
        line out of service first, so the close only records (it writes not one word of the line's
        status). Their release is still the gate's word, and the delivery is approved from here."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='checkpoint')
        sb.add_node(NODE2, 'carry on', mode='auto', deps=[NODE])
        sb.start_cycle()
        sb.set_cycle_status('cyc', 'inactive')
        sb.write_signal('cyc', NODE, 'completed', 'done')

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertIn('only records', action['reason'])
        self.assertEqual(sb.cycle()['status'], 'inactive')
        self.assertEqual(sb.dag_status(NODE), 'completed')
        self.assertNotIn('approved_at', sb.node(NODE))

        sb.set_cycle_status('cyc', 'active')

        node = sb.node(NODE)
        self.assertIn('approved_at', node)
        self.assertGreaterEqual(node['approved_at'], node['completed_at'])

    def test_a_gate_a_wide_line_stops_at_with_another_node_in_flight(self):
        """A line `parallel` wide stops at this gate like any other, and the human's way back is the
        same one command -- so the delivery is approved from here too."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='checkpoint')
        sb.add_node(NODE2, 'in flight', mode='auto')
        sb.start_cycle(tags=['coding'], parallel=2)
        self.assertEqual(sorted(sb.holding_ids()), sorted([NODE, NODE2]))
        sb.write_signal('cyc', NODE, 'completed', 'done')

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertIn('checkpoint', action['reason'])
        self.assertEqual(sb.cycle()['status'], 'inactive')
        self.assertEqual(sb.holding_ids(), [NODE2], 'the other node is still in hand')

        sb.set_cycle_status('cyc', 'active')

        node = sb.node(NODE)
        self.assertIn('approved_at', node)
        self.assertGreaterEqual(node['approved_at'], node['completed_at'])
        self.assertNotIn('approved_at', sb.node(NODE2))


class TestAttention(Base):
    """What a human has to look at travels with the stop package; nothing else can carry it."""

    def kinds(self, action):
        return [item['kind'] for item in action.get('attention', [])]

    def test_a_run_that_ended_cleanly_has_nothing_look_at_it(self):
        """A node that delivered and was marked completed is not a loose end.

        The list exists so a human is told what is parked; a clean stop has nothing parked.
        """
        sb = self.make()
        sb.add_chain(2)
        sb.start_cycle()
        final, _ = self.run_to_stop(sb)

        self.assertEqual(final['action'], 'stop_pipeline')
        parked = [i for i in final.get('attention', []) if i.get('kind') == 'node_shelved']
        self.assertEqual([i['node_id'] for i in parked
                          if sb.dag_status(i['node_id']) == 'shelved'], [])

    def test_a_stop_names_the_rule_that_stopped_it(self):
        """The reason a run parks itself has to name the lever that did it -- a human reading
        "stopped" with no why has to go and reconstruct it from the state. That lever is the node's
        own mode now, and nothing else."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='checkpoint')
        sb.start_cycle()
        final, _ = self.run_to_stop(sb)

        self.assertEqual(final['action'], 'stop_pipeline')
        self.assertIn('mode=checkpoint', final['reason'])

    def test_a_non_delivery_reaches_the_stop_package(self):
        """The worker's `failed` ticket is the verdict; what a human is handed is the node it left
        `shelved`, under `kind: node_shelved`."""
        sb = self.ready_sandbox()
        sb.write_signal('cyc', NODE, 'failed', 'a total failure')
        action = sb.close_round(NODE)

        self.assertIn('node_shelved', self.kinds(action))
        entry = [i for i in action['attention'] if i['kind'] == 'node_shelved'][0]
        self.assertEqual(entry['node_id'], NODE)
        self.assertIn('dag-manage revive', entry['hint'])
        self.assertEqual(sb.dag_status(NODE), 'shelved')

    def test_a_node_parked_by_an_earlier_step_stays_visible(self):
        """A node left `shelved` in the DAG is still parked several steps later, and the stop
        package is the only place a session would ever hear about it."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.add_node(NODE2, 'another line', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'failed', 'produced nothing')
        first = sb.close_round(NODE)
        self.assertIn('node_shelved', self.kinds(first))

        # the human revives nothing; the line is put back in service by hand and walks the other node
        sb.set_cycle_status('cyc', 'active')
        sb.set_current_node(NODE2)
        sb.write_signal('cyc', NODE2, 'completed', 'done')
        second = sb.close_round(NODE2)

        self.assertIn('node_shelved', self.kinds(second))
        self.assertIn(NODE, [i.get('node_id') for i in second['attention']])

    def test_the_plan_and_the_close_agree_on_what_happens_next(self):
        """The planner is the closing call's own preview, and a terminal ticket is a stable state:
        `plan` answers `wait` with the command that closes it, whatever the ticket says -- the
        closing call is what turns it into an outcome."""
        sb = self.ready_sandbox()
        sb.write_signal('cyc', NODE, 'completed', 'done')
        planned = sb.plan()

        self.assertEqual(planned['action'], 'wait')
        self.assertEqual(planned['node_id'], NODE)
        self.assertIn(NODE, planned['close_with'])
        self.assertEqual(sb.path(), [], 'the preview consumed the ticket')

        sb.write_signal('cyc', NODE, 'failed', 'a total failure')
        failed_plan = sb.plan()
        self.assertEqual(failed_plan['action'], 'wait')

        applied = sb.close_round(NODE)
        self.assertEqual(applied['action'], 'stop_pipeline')
        self.assertEqual(sb.dag_status(NODE), 'shelved')
        self.assertEqual([i['kind'] for i in applied['attention']], ['node_shelved'])


class TestShelvedPolicy(Base):
    """`when_shelved`: when a node closes out without delivering (so it becomes shelved), does this
    line stop or carry on.

    The policy is a property of **this line** (the same kind of thing as `tags`): written in the
    cycle record, travelling with it, defaulting to `stop` -- when a reading is uncertain, stopping
    for a human beats a line running on by itself.
    """

    def pair(self):
        """Two auto nodes in a line, the cycle pinned on the first one."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.add_node(NODE2, 'carry on', mode='auto', deps=[NODE])
        sb.start_cycle()
        return sb

    def test_the_default_is_stop(self):
        """A cycle that never wrote `policies` has today's shape in the record: default `stop`, the
        node marked `shelved`, the line stopping for a human."""
        sb = self.pair()
        self.assertNotIn('policies', sb.cycle())

        sb.write_signal('cyc', NODE, 'failed', 'produced nothing')
        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertEqual(sb.dag_status(NODE), 'shelved')
        self.assertEqual(sb.cycle()['status'], 'inactive')
        self.assertIsNone(sb.current_node())
        self.assertEqual([i['kind'] for i in action['attention']], ['node_shelved'])

    def test_continue_does_not_unblock_the_dependents_of_a_shelved_node(self):
        """A `shelved` node satisfies no dependency: it waits out of play and the work waiting on it
        cannot run -- `continue` says this line carries on, not that this node counts as
        delivered."""
        sb = self.pair()
        sb.set_policy('cyc', 'when_shelved', 'continue')
        self.assertEqual(sb.cycle()['policies'], {'when_shelved': 'continue'})

        sb.write_signal('cyc', NODE, 'failed', 'this one produced nothing')
        action = sb.close_round(NODE)

        self.assertEqual(sb.dag_status(NODE), 'shelved', 'the node was parked')
        self.assertEqual(sb.path()[0]['result'], 'failed', 'the step keeps the ticket word')
        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertEqual(sb.dag_status(NODE2), 'todo', 'the dependent node ran anyway')
        self.assertIn(NODE, [i.get('node_id') for i in action.get('attention', [])])

    def test_continue_walks_on_to_a_node_that_is_still_reachable(self):
        """`continue` means "walk on as if it had delivered": when the graph holds other ready work,
        it really is picked up."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.add_node(NODE2, 'another line', mode='auto')
        sb.start_cycle()
        sb.set_policy('cyc', 'when_shelved', 'continue')

        sb.write_signal('cyc', NODE, 'failed', 'this one produced nothing')
        action = sb.close_round(NODE)

        self.assertEqual(sb.dag_status(NODE), 'shelved')
        self.assertEqual(sb.current_node(), NODE2)
        self.assertIn(action['action'], ('dispatch', 'open_node', 'wait'))
        self.assertEqual(sb.cycle()['status'], 'active', 'the line is still in service')

    def test_the_stop_rule_still_wins_under_continue(self):
        """The policy only answers "does a non-delivery carry on"; it cannot move the node's own
        mode."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='checkpoint')
        sb.add_node(NODE2, 'another line', mode='auto')
        sb.start_cycle()
        sb.set_policy('cyc', 'when_shelved', 'continue')

        sb.write_signal('cyc', NODE, 'failed', 'produced nothing')
        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertIn('mode=checkpoint', action['reason'])
        self.assertEqual(sb.cycle()['status'], 'inactive')
        self.assertIsNone(sb.current_node())

    def test_set_policy_validates_the_key_and_its_values(self):
        sb = self.pair()
        bad_key = sb.set_policy('cyc', 'on_lunch', 'continue', check=False)
        self.assertNotEqual(bad_key.returncode, 0)
        self.assertIn('when_shelved', bad_key.stderr)
        bad_value = sb.set_policy('cyc', 'when_shelved', 'maybe', check=False)
        self.assertNotEqual(bad_value.returncode, 0)
        self.assertIn('stop', bad_value.stderr)
        self.assertNotIn('policies', sb.cycle(), 'a refused write still wrote something')

    def a_refused_line(self, line):
        """A plugin's `number` knob is refused for a value its kind cannot be, and a line of 0 is
        refused too (a 0 line fails every reading).

        The key arrives through the registry, so the case that drives it skips when the folder
        declaring it is not installed -- deleting a plugin is a supported state, and the core's own
        keys above still cover the validation path without it.
        """
        sb = self.pair()
        bad_number = sb.set_policy('cyc', line, 'soon', check=False)
        self.assertNotEqual(bad_number.returncode, 0)
        zero = sb.set_policy('cyc', line, '0', check=False)
        self.assertNotEqual(zero.returncode, 0, 'a line of 0 fails every reading')
        self.assertNotIn('policies', sb.cycle(), 'a refused write still wrote something')

    @requires_plugin(TICKET.plugin)
    def test_a_feature_knob_is_validated_like_any_other(self):
        """`plugins/test-audit`'s line is a first-class policy key."""
        self.a_refused_line(TICKET.line)

    @requires_plugin(POOL.plugin)
    def test_the_other_feature_knob_is_validated_the_same_way(self):
        """`plugins/issue`'s line is one too: the core reads both out of the declaration, so neither
        is a special case."""
        self.a_refused_line(POOL.line)

    def a_numeric_policy_lands(self, line):
        """A plugin's numeric knob (the kind `number`): a positive number lands as the number itself,
        and `off` drops the entry."""
        sb = self.pair()
        proc = sb.set_policy('cyc', line, '15', cli=True)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sb.cycle()['policies'][line], 15)
        self.assertIn(f'{line}=15', proc.stdout)

        cleared = sb.set_policy('cyc', line, 'off', cli=True)

        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertNotIn(line, sb.cycle()['policies'])
        self.assertNotIn(line, cleared.stdout)

    @requires_plugin(TICKET.plugin)
    def test_a_numeric_policy_lands_as_a_number_and_off_clears_it(self):
        self.a_numeric_policy_lands(TICKET.line)

    @requires_plugin(POOL.plugin)
    def test_the_other_numeric_policy_lands_and_clears_the_same_way(self):
        self.a_numeric_policy_lands(POOL.line)

    def test_set_policy_prints_the_effective_set(self):
        """Write one, and what comes back is the whole **effective** policy set (a human need not
        assemble the defaults himself)."""
        sb = self.pair()
        proc = sb.set_policy('cyc', 'when_shelved', 'continue', cli=True)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('when_shelved=continue', proc.stdout)
        self.assertEqual(sb.cycle()['policies'], {'when_shelved': 'continue'})

    def test_current_lists_the_lines_that_matter_and_their_policy(self):
        """`cycle current` has to list the lines that matter right now: every active one, **and every
        one holding a node** -- a line out of service and parked mid-way is exactly the state a human
        must see."""
        sb = self.pair()
        sb.set_policy('cyc', 'when_shelved', 'continue')
        pinned = sb.current_node()

        active_view = sb._tool('cycle', 'current', cli=True).stdout
        self.assertIn('cyc', active_view)
        self.assertIn(pinned, active_view)
        self.assertIn('when_shelved=continue', active_view)
        self.assertIn('[active]', active_view)
        self.assertNotIn('Launch:', active_view)

        sb.set_cycle_status('cyc', 'inactive', cli=True)
        parked_view = sb._tool('cycle', 'current', cli=True).stdout
        self.assertIn('cyc', parked_view, 'a line parked mid-node is invisible again')
        self.assertIn(pinned, parked_view)
        self.assertIn('[inactive]', parked_view)


class TestShelvedNodes(Base):
    """A node out of play waiting for a human (`shelved`) does not enter the ready set, and does not
    vanish from the human-facing list either.

    This status is written by the close itself: a node that did not deliver (a `failed` / `stopped`
    note) lands on it, nobody may start it, and it waits for a `pf dag revive` to put it back. So
    both have to be pinned -- it is not treated as startable, and it does not quietly drop out of the
    reader's sight.
    """

    def shelved_graph(self):
        """One parked node and one in play: both shapes this build really writes."""
        sb = self.make()
        sb.add_node(NODE, 'shelved, waiting for a human', status='shelved', mode='auto')
        sb.add_node(NODE2, 'in play', status='todo', mode='auto')
        return sb

    def test_the_ready_set_leaves_the_parked_ones_out(self):
        sb = self.shelved_graph()
        ready = {n['id'] for n in pipeline.dag_ready_nodes(sb.root)}

        self.assertEqual(ready, {NODE2}, 'a parked node was offered as ready, or a todo one was not')

    def test_a_shelved_node_reaches_the_stop_package(self):
        sb = self.shelved_graph()
        sb.start_cycle()
        self.assertEqual(sb.current_node(), NODE2, 'the only node in play is the todo one')
        sb.write_signal('cyc', NODE2, 'completed', 'done')
        action = sb.close_round(NODE2)

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertEqual(sb.dag_status(NODE2), 'completed')
        self.assertEqual({i['node_id'] for i in action['attention']}, {NODE},
                         'a parked node fell out of the human-facing list')
        self.assertIn('revive', action['attention'][0]['hint'])



# ═══════════════════════════════════════════════════════════════════════════════
#  a node that did not write mode: one fallback, one word at four gates
# ═══════════════════════════════════════════════════════════════════════════════

class TestTheModeWord(Base):
    """`store.normalize_mode` is the one spelling of this fallback.

    The word reported at four gates has to be the same: the disk read's backfill (`load_dag`), the
    lookup (`nodes.mode_for`), the pick (`dag_ready_nodes`) and the stop rule
    (`planner.stop_criterion`) -- with a default held in two places, adding one mode word drifts them
    apart.
    """

    def nodes_without_the_key(self):
        """A node whose record has no `mode` key at all (hand-edited, or written by an old
        machine)."""
        sb = self.make()
        sb.add_node(NODE, 'UI design')
        dag = sb.dag()
        del dag['nodes'][NODE]['mode']
        sb.write_dag(dag['nodes'])
        return sb

    def test_the_fallback_is_spelled_once_in_the_package(self):
        """`or DEFAULT_MODE` appears exactly once in the source -- that is the one to change when
        adding a mode word."""
        sites = []
        for path in sorted((CONDUCT_DIR / 'pipeline').glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            sites += [f'{path.name}:{node.lineno}' for node in ast.walk(tree)
                      if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
                      and any(isinstance(value, ast.Name) and value.id == 'DEFAULT_MODE'
                              for value in node.values)]

        self.assertEqual(1, len(sites), f'the fallback is written in several places: {sites}')
        self.assertTrue(sites[0].startswith('store.py:'), sites)

    def test_a_node_that_leaves_the_key_out_reads_as_auto_everywhere(self):
        """With no such key in the record, the four gates read out the same word."""
        sb = self.nodes_without_the_key()
        sb.start_cycle()

        dag = pipeline.store.load_dag(sb.root)
        self.assertEqual('auto', dag['nodes'][NODE]['mode'], 'the disk read did not backfill')
        self.assertEqual('auto', pipeline.nodes.mode_for(NODE, dag), 'the lookup gate')
        self.assertEqual('auto', pipeline.dag_ready_nodes(sb.root)[0]['mode'],
                         'the slot in the ready set')

        action = sb.next()
        self.assertEqual('dispatch', action['action'], action)
        self.assertEqual('auto', action['mode'], 'the slot the dispatch reports')

        must_stop, why = pipeline.stop_criterion(sb.root, sb.cycle(), {'id': NODE})
        self.assertFalse(must_stop, f'the stop rule did not reach auto: {why}')


# ═══════════════════════════════════════════════════════════════════════════════
#  where the prompt lands: one algorithm, and what is reported is what was written
# ═══════════════════════════════════════════════════════════════════════════════

class TestThePromptPath(Base):
    """`.pf/meta/launch/...` is spelled in exactly one place in the implementation package:
    `prompts.launch_dir`."""

    def test_the_launch_path_is_built_in_one_file(self):
        """`prompts.py` is the only file that spells the `launch` segment."""
        sources = {path.name: path.read_text(encoding='utf-8')
                   for path in sorted((CONDUCT_DIR / 'pipeline').glob('*.py'))}
        sites = sorted(name for name, text in sources.items()
                       if "'launch'" in text or '"launch"' in text)

        self.assertEqual(['prompts.py'], sites)

    def test_the_dispatch_reports_the_path_the_writer_uses(self):
        """The prompt path the dispatch reports is character-for-character the one the write side
        computes."""
        sb = self.make()
        sb.add_node(NODE, 'UI design')
        sb.start_cycle()

        action = sb.next()

        expected = pipeline.prompts.launch_prompt_path(sb.root, 'cyc', NODE)
        self.assertEqual('dispatch', action['action'], action)
        self.assertEqual(Path(action['prompt']), expected)
        self.assertTrue(expected.is_file(), 'what is reported is what was written')


# ═══════════════════════════════════════════════════════════════════════════════
#  the closing pass: one whole read of the graph
# ═══════════════════════════════════════════════════════════════════════════════

class TestTheCloseReadsTheGraphOnce(Base):
    """The close entry reads the graph once and hands the judgement down as an argument -- several
    branches walk the same one."""

    def graph_readers(self):
        """Replace **every layer's own bound** `load_dag` in the implementation package with a
        counting wrapper, and put them back when the case exits.

        The layers are found in the globals of the entry module in hand, with no hard-coded list and
        without looking at `sys.modules`: this package gets a fresh set of module objects every time
        it is loaded (`fresh=True`), and the one in `sys.modules` need not be the one running here.
        Each layer has the function object it bound when it imported, so one whole graph read is
        counted exactly once per pass. What is recorded is the caller's function name (no line
        numbers: editing a comment must not turn it red).

        This way of finding them has two **silent** blind spots, and hitting either one quietly misses
        a count:

          * an alias bound inside a layer: a layer binds `load_dag` to another name **inside its own
            module** (`_read = load_dag`), so the entry module's globals have no such name to swap and
            that layer's read is not counted.
          * a new layer outside the package namespace: a newly added implementation layer is not
            attached to the entry module's globals (imported by another layer only, say), and it is
            never found even once.

        The boundary is loud: `load_dag`'s callers are all inside the `pipeline` package today (the
        counting wrapper recognizes a caller by the stack frame's **directory**, and `_IMPL_DIR` is
        that directory), so one extra direct caller outside the package makes that `next(...)` raise
        `StopIteration` right there instead of quietly missing a count -- the entry and
        `state/pipeline_loader.py` also contain the word `pipeline` in their paths, and a substring
        match would count them in, exactly the silence this boundary exists to block.
        """
        callers = []
        real = {}
        layers = [value for value in vars(pipeline).values()
                  if isinstance(value, types.ModuleType) and hasattr(value, 'load_dag')]
        self.assertGreaterEqual(len(layers), 5, f'did not find every graph-reading layer: {layers}')
        for layer in layers:
            real[layer] = layer.load_dag

            def counted(project, _real=real[layer], _callers=callers):
                frame = next(f for f in reversed(traceback.extract_stack())
                             if Path(f.filename).parent == _IMPL_DIR and f.name != 'counted')
                _callers.append(frame.name)
                return _real(project)

            layer.load_dag = counted
        self.addCleanup(lambda: [setattr(layer, 'load_dag', fn) for layer, fn in real.items()])
        return callers

    def test_a_close_with_nothing_left_reads_the_graph_once(self):
        """The last slot in hand is closed and the graph has no work: this pass reads the graph once,
        and the close entry is what reads it."""
        sb = self.make()
        sb.add_node(NODE, 'UI design')
        sb.start_cycle()
        sb.write_signal('cyc', NODE, 'completed', 'done')

        callers = self.graph_readers()
        action = sb.close_round(NODE)

        self.assertEqual('stop_pipeline', action['action'], action)
        self.assertEqual({'_close_own_node'}, set(callers), f'the graph readers: {callers}')

    def test_a_close_that_stops_reads_the_graph_once_however_many_branches_it_walks(self):
        """The stop pass walks the most branches (the ready set, the blocked reason, the named node's
        tag, the instruction pass's scope judgement inside the stop rule, the attention list), and the
        close entry is still the only reader of the graph."""
        sb = self.make()
        sb.add_node(NODE, 'UI design')
        sb.add_node(NODE2, 'shelved, waiting for a human', status='shelved')
        sb.start_cycle(tags=['coding'])
        sb.write_signal('cyc', NODE, 'completed', 'done')

        callers = self.graph_readers()
        action = sb.close_round(NODE, node=NODE2)

        self.assertEqual('stop_pipeline', action['action'], action)
        self.assertTrue(action['attention'], action)
        self.assertEqual({'_close_own_node'}, set(callers), f'the graph readers: {callers}')

    def test_a_close_with_another_node_still_in_flight_reads_the_graph_once(self):
        """A multi-slot line still has one slot in flight: this slot closes and a second cannot be
        filled -> it stops on the "who else is in hand" sentence, with one graph read."""
        sb = self.make()
        sb.add_node(NODE, 'first')
        sb.add_node(NODE2, 'second')
        sb.start_cycle(parallel=2)
        sb.write_signal('cyc', NODE, 'completed', 'done')

        callers = self.graph_readers()
        action = sb.close_round(NODE)

        self.assertEqual('stop_pipeline', action['action'], action)
        self.assertIn(NODE2[:8], action['reason'], 'the reason must name who else is in hand')
        self.assertEqual([NODE2], sb.holding_ids(), 'the slot in flight is still held')
        self.assertEqual({'_close_own_node'}, set(callers), f'the graph readers: {callers}')

    def test_a_multi_slot_close_that_cannot_fill_reads_the_graph_once(self):
        """A multi-slot line holds one slot and the graph has no second one to fill: it takes the
        blocked-reason branch, with one graph read."""
        sb = self.make()
        sb.add_node(NODE, 'first')
        sb.start_cycle(parallel=2)
        sb.write_signal('cyc', NODE, 'completed', 'done')

        callers = self.graph_readers()
        action = sb.close_round(NODE)

        self.assertEqual('stop_pipeline', action['action'], action)
        self.assertEqual('the graph has no ready node left', action['reason'])
        self.assertEqual({'_close_own_node'}, set(callers), f'the graph readers: {callers}')

    def test_a_close_that_fills_a_slot_reads_the_graph_once(self):
        """The fill pass: the close reads once, and the dispatch filled right there reads once more.

        The second read is the **dispatch slot**'s: the dispatch is built in `plan` (it is the same
        step for a close's refill and for a cycle's first pick), and the plugin gate is asked with
        the copy that step read. It has nothing to do with the closing pass; the close's own
        judgements always use the one handed down.
        """
        sb = self.make()
        sb.add_node(NODE, 'first')
        sb.add_node(NODE2, 'second')
        sb.add_node(NODE3, 'third')
        sb.start_cycle()
        sb.set_policy('cyc', 'parallel', 2)
        sb.set_current_node(NODE2)
        sb.write_signal('cyc', NODE, 'completed', 'done')

        callers = self.graph_readers()
        action = sb.close_round(NODE)

        self.assertEqual('dispatch', action['action'], action)
        self.assertEqual(sorted(sb.holding_ids()), sorted([NODE2, NODE3]),
                         'the slot that was freed has been filled')
        self.assertEqual({'_close_own_node', 'plan'}, set(callers),
                         f'the graph readers: {callers}')

    def test_the_pick_path_reads_the_graph_once_too(self):
        """The path with no node in hand (the opening pick) is also one entry with one read: when the
        filter set blocks the ready nodes it takes the blocked reason and attention, and `_pick_next`
        is still the only graph reader."""
        sb = self.make()
        sb.add_node(NODE, 'Write copy', tags=['writing'])
        sb.create_cycle('cyc', tags=['coding'])

        callers = self.graph_readers()
        action = sb.plan()

        self.assertEqual('stop_pipeline', action['action'], action)
        self.assertIn('writing', action['reason'])
        self.assertEqual({'_pick_next'}, set(callers), f'the graph readers: {callers}')


# ═══════════════════════════════════════════════════════════════════════════════
#  the closing decision: one entry, whatever the line holds
# ═══════════════════════════════════════════════════════════════════════════════

@requires_plugin(TICKET.plugin, POOL.plugin)
class TestTheCloseHasOneEntry(Base):
    """The judge that ends a node -- the stop rule, the close's commands, the stop answer, and the
    plugins' insertion -- is asked from exactly one function, for a line holding one node and for a
    line holding several alike.

    How many slots a line holds at once is a *parameter* of that one function; when it is answered
    in two places instead, every new judgement has to be hung twice, and the two ladders drift apart
    in exactly the corners no case looks at. This is the case that looks: it drives the closes that
    between them walk every branch, and records who asked.

    Two of those branches are closes a plugin answers with a node -- one per shipped metric -- which
    need both declarations installed; the class therefore skips when either folder is gone, and each
    metric's answer is walked here rather than only one of the two. The pin it carries is a
    code-shape one, and the shape does not change with a data folder.
    """

    #: The machinery a close is built from: the stop rule, the mechanical close list, the stop
    #: answer, and the plugins' two halves (the judgement the host is asked, and the action it
    #: becomes).
    MACHINERY = ('stop_criterion', '_close_commands', '_stop_action', '_feature_insertions',
                 '_insertions_due')

    def machinery_callers(self):
        """Replace that machinery with a wrapper recording its caller's function name, in **every**
        layer that bound a copy of it, and put them back when the case exits.

        The layers are found the same way `graph_readers` finds the graph readers, and for the same
        reason: this package gets a fresh set of module objects every time it is loaded, and a
        binding made by `from .actions import ...` lives in the importing layer, not in the entry
        module. What is recorded is the caller's function name (no line numbers: editing a comment
        must not turn it red).
        """
        callers, real = [], {}
        layers = [value for value in vars(pipeline).values()
                  if isinstance(value, types.ModuleType)
                  and any(hasattr(value, name) for name in self.MACHINERY)]
        self.assertGreaterEqual(len(layers), 2, f'did not find the close machinery: {layers}')
        for layer in layers:
            for name in self.MACHINERY:
                if not hasattr(layer, name):
                    continue
                real[(layer, name)] = getattr(layer, name)

                def counted(*a, _fn=real[(layer, name)], _name=name, **kw):
                    frame = next(f for f in reversed(traceback.extract_stack())
                                 if Path(f.filename).parent == _IMPL_DIR and f.name != 'counted')
                    callers.append((_name, frame.name))
                    return _fn(*a, **kw)

                setattr(layer, name, counted)
        self.addCleanup(lambda: [setattr(layer, name, fn) for (layer, name), fn in real.items()])
        return callers

    def one_slot_closes(self):
        """The closes a one-slot line can take: stop, open, the gate, a record-only close, a
        non-delivery, and a name that is not a ready node."""
        def stop_empty(sb):
            sb.add_node(NODE, 'UI design')
            sb.start_cycle()
            sb.write_signal('cyc', NODE, 'completed', 'done')
            return {'node': NODE}

        def open_next(sb):
            sb.add_node(NODE, 'UI design')
            sb.add_node(NODE2, 'the next one')
            sb.start_cycle()
            sb.write_signal('cyc', NODE, 'completed', 'done')
            return {'node': NODE}

        def checkpoint_gate(sb):
            sb.add_node(NODE, 'UI design', mode='checkpoint')
            sb.add_node(NODE2, 'the next one')
            sb.start_cycle()
            sb.write_signal('cyc', NODE, 'completed', 'done')
            return {'node': NODE}

        def line_out_of_service(sb):
            open_next(sb)
            sb.set_cycle_status('cyc', 'inactive')
            return {'node': NODE}

        def not_delivered(sb):
            sb.add_node(NODE, 'UI design')
            sb.add_node(NODE2, 'the next one')
            sb.start_cycle()
            sb.write_signal('cyc', NODE, 'failed', 'broke')
            return {'node': NODE}

        def named_not_ready(sb):
            sb.add_node(NODE, 'UI design')
            sb.add_node(NODE2, 'already done', status='completed')
            sb.start_cycle()
            sb.write_signal('cyc', NODE, 'completed', 'done')
            return {'node': NODE, 'node_arg': NODE2}

        return (('one slot, nothing left', stop_empty),
                ('one slot, the next one opens', open_next),
                ('one slot, the checkpoint gate', checkpoint_gate),
                ('one slot, the line out of service', line_out_of_service),
                ('one slot, not delivered', not_delivered),
                ('one slot, a node named by hand that is not ready', named_not_ready))

    def several_slot_closes(self):
        """The closes a line holding several slots can take: a refill, a slot still in flight with
        nothing to refill with, and a close each shipped plugin answers with a node."""
        def fill_one(sb):
            sb.add_node(NODE, 'UI design')
            sb.add_node(NODE2, 'the next one')
            sb.start_cycle()
            sb.set_policy('cyc', 'parallel', 2)
            sb.write_signal('cyc', NODE, 'completed', 'done')
            return {'node': NODE}

        def in_flight_no_fill(sb):
            sb.add_node(NODE, 'UI design')
            sb.add_node(NODE2, 'still running')
            sb.start_cycle()
            sb.set_policy('cyc', 'parallel', 2)
            sb.set_current_node(NODE2)
            sb.write_signal('cyc', NODE, 'completed', 'done')
            return {'node': NODE}

        def ticket_due(sb):
            """The same close, and the ticket's plugin answers it with a node: the branch that is
            only walked when the judgement hands something back."""
            sb.add_node(NODE, 'UI design', tags=['coding'])
            sb.add_node(NODE2, 'the next one', tags=['coding'], deps=[NODE])
            sb.start_cycle(tags=['coding'])
            sb.set_policy('cyc', 'parallel', 2)
            sb.set_policy('cyc', TICKET.line, TICKET.at)
            sb.write_signal('cyc', NODE, 'completed', 'done',
                            {'deliverable': 'x', 'reading': {TICKET.reading: TICKET.over}})
            return {'node': NODE}

        def pool_due(sb):
            """The same branch answered by the other plugin: the pool's number is not on the note, so
            the fixture lays it out in the store the issue tool reads instead."""
            sb.add_node(NODE, 'UI design', tags=['coding'])
            sb.add_node(NODE2, 'the next one', tags=['coding'], deps=[NODE])
            sb.start_cycle(tags=['coding'])
            sb.set_policy('cyc', 'parallel', 2)
            sb.set_policy('cyc', POOL.line, POOL.at)
            POOL.repay(sb, POOL.over)
            sb.write_signal('cyc', NODE, 'completed', 'done', {'deliverable': 'x'})
            return {'node': NODE}

        return (('several slots, the freed slot is refilled', fill_one),
                ('several slots, a slot still in flight', in_flight_no_fill),
                ('several slots, the ticket line answers with a node', ticket_due),
                ('several slots, the pool line answers with a node', pool_due))

    def test_every_close_decision_hangs_from_one_function(self):
        """One slot or several, stop or open: the close's whole judgement is asked from the one
        entry, so a new judgement point has one place to be hung -- and a second close ladder, of
        whatever shape, would show up here as a second caller."""
        cases = list(self.one_slot_closes()) + list(self.several_slot_closes())
        callers = self.machinery_callers()

        for name, build in cases:
            sb = self.make()
            close = build(sb)
            action = sb.close_round(close['node'], node=close.get('node_arg'))
            self.assertTrue(action.get('action'), f'{name}: the close returned no action')

        asked = {who for _, who in callers}
        self.assertEqual({'_close_own_node'}, asked,
                         f'something else is making the close decision: {sorted(callers)}')
        self.assertEqual(set(self.MACHINERY), {what for what, _ in callers},
                         'a piece of the close machinery was never asked -- the pin above is '
                         f'then one branch short: {sorted(callers)}')


# ═══════════════════════════════════════════════════════════════════════════════
#  the ready set: the read side does not truncate for its caller
# ═══════════════════════════════════════════════════════════════════════════════

class TestTheReadySetIsWhole(Base):
    """`ranked_ready_nodes` hands over the whole ready set when not asked: how many slots to take at
    once is the line's own business (`parallel`).

    These two call the read side directly, not through the sandbox's planning entry, so they wrap
    themselves in a `_dag_hook`: the ranking is answered by dag-manage's own `cmd_list` (the same
    implementation), and all that is saved is one process launch after another.
    """

    def nine_ready(self):
        """Nine ready nodes that depend on nothing -- more than any "default row count written down
        in passing"."""
        sb = self.make()
        ids = [f'n{i}-0000-4000-8000-00000000000{i}' for i in range(9)]
        for i, uid in enumerate(ids):
            sb.add_node(uid, f'Node {i}')
        return sb, ids

    def test_the_reader_hands_over_the_whole_ranking(self):
        """Not asking for a limit means the whole thing; how many rows is the caller's to name."""
        sb, ids = self.nine_ready()

        with _dag_hook(sb.root):
            whole = pipeline.ranked_ready_nodes(sb.root)
            same = pipeline.ranked_ready_nodes(sb.root, tags=[])
            three = pipeline.ranked_ready_nodes(sb.root, limit=3)

        self.assertEqual(9, len(whole))
        self.assertEqual(9, len(same))
        self.assertEqual(3, len(three))

    def test_a_named_node_beyond_the_head_is_still_picked(self):
        """A node named by `--node` is recognized wherever it ranks: it is ready and the name matches,
        so it is the one chosen."""
        sb, ids = self.nine_ready()
        tail = ids[-1]

        with _dag_hook(sb.root):
            whole = pipeline.ranked_ready_nodes(sb.root)
            chosen = pipeline.pick_node(sb.root, tail)

        self.assertEqual(tail, whole[-1]['id'], 'this case needs the node at the tail of the ranking')
        self.assertIsNotNone(chosen, 'a named node at the tail of the ranking was not found')
        self.assertEqual(tail, chosen['id'])
