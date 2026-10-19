"""The closing call: one worker ends the node it just ran, and what the close refuses.

`next --after <node_id>` is the only thing that ends a step. The outcome is read off the signal the
worker already wrote; what that call still decides is whether to carry on, which node to aim at, and
why. The guards next to it are the states a close may NOT be settled over -- no ticket at all,
somebody else's node, a node that already left the line.

The picking side (start / filter set / weight) lives in `cases_select.py`; the line's holding and the
two driven paths live in `cases_contract.py`.

    python -m unittest conduct.tests.cases_close         # this file on its own
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import NODE, NODE2, NODE3, Base  # noqa: E402
from harness import Driver, WorkerSession  # noqa: E402

__all__ = ['TestWorkerClosesItsOwnNode', 'TestCloseGuards']


class TestWorkerClosesItsOwnNode(Base):
    """`next --after <node_id>`: the worker closes the node it just ran.

    The outcome is read off the signal the worker already wrote, so closing is one call that says
    "the node I just finished is done; carry on or stop" -- and a node that has not finished answers
    `wait` rather than being taken as delivered.
    """

    def finished_node(self, sb, cli=False):
        """Pin the node, let its worker report `completed`."""
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        first = sb.next()
        WorkerSession(sb).work(first)
        return first

    def test_it_closes_the_node_and_spawns_the_next_one(self):
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.add_node(NODE2, 'another line', mode='auto')
        sb.start_cycle()
        first = sb.next()
        WorkerSession(sb).work(first)

        settled, proc = sb.spawn_next('--after', first['node_id'])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'dispatch')
        self.assertEqual(settled['node_id'], NODE2)
        self.assertTrue(settled.get('spawned'), 'the closing call did not run --spawn')
        self.assertIn('dry-run', settled['spawned'])
        self.assertEqual(sb.path_nodes(), [NODE])
        self.assertEqual(sb.current_node(), NODE2)

    def test_the_last_node_stops_the_run(self):
        """No next node means stop: the same outcome the planner computes either way, and the
        `attention` list still reaches whoever is reading."""
        sb = self.make()
        first = self.finished_node(sb)

        settled, proc = sb.next_cli('--after', first['node_id'])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'stop_pipeline')
        self.assertEqual(sb.cycle()['status'], 'inactive')
        self.assertEqual(sb.path_nodes(), [NODE])

    def test_a_node_that_is_still_running_is_not_closed(self):
        """A `running` signal is never a green light -- the rule every path enforces."""
        sb = self.make()
        first = self.finished_node(sb)
        sb.write_signal('cyc', NODE, 'pending', 'not finished yet')

        settled, proc = sb.next_cli('--after', first['node_id'])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'wait')
        self.assertEqual(sb.current_node(), NODE)
        self.assertEqual(sb.path(), [])

    def test_naming_the_wrong_node_changes_nothing(self):
        """The guard runs before every write, so a mistyped node id leaves the run untouched."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.add_node(NODE2, 'another line', mode='auto')
        sb.start_cycle()
        sb.set_current_node(NODE)
        sb.write_signal('cyc', NODE, 'completed', 'done')
        before = sb.tree_hashes()

        _, proc = sb.next_cli('--after', NODE2, check=False)

        self.assertEqual(proc.returncode, 2)
        self.assertIn('does not name', proc.stderr)
        self.assertEqual(sb.tree_hashes(), before)
        self.assertEqual(sb.path(), [])

    def test_a_non_delivery_parks_the_node_shelved_and_stops(self):
        """A worker that reports `failed` is asking to be looked at, and the DAG's node status is
        where that shows: the node becomes `shelved`, out of play until a human revives it."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.add_node(NODE2, 'another line', mode='auto')
        sb.start_cycle()
        first = sb.next()
        WorkerSession(sb, fail_nodes={NODE}).work(first)

        settled, proc = sb.next_cli('--after', first['node_id'])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'stop_pipeline')
        self.assertEqual(sb.dag_status(NODE), 'shelved')
        self.assertIn(NODE, [a.get('node_id') for a in settled.get('attention', [])])

    def test_a_closed_node_is_not_closed_twice(self):
        """The second call has nothing to settle and must say so rather than closing something
        again. On a one-node run the cycle is finished, which is the honest reason."""
        sb = self.make()
        first = self.finished_node(sb)
        sb.next_cli('--after', first['node_id'])
        before = sb.tree_hashes()

        _, proc = sb.next_cli('--after', first['node_id'], check=False)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('already closed', proc.stderr)
        self.assertEqual(sb.tree_hashes(), before)

    def test_a_node_nobody_rounds_is_refused_by_name(self):
        """`--after` is aimed by node alone, so an id that is not any cycle's current node has to
        be refused by name -- with a reading of where every cycle actually stands."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()

        _, proc = sb.next_cli('--after', NODE3, check=False)

        self.assertEqual(proc.returncode, 2)
        self.assertIn(NODE3, proc.stderr)
        self.assertIn(NODE, proc.stderr)
        self.assertEqual(sb.path(), [])
class TestCloseGuards(Base):
    """A node's step may only be closed over work that actually reports."""

    def test_a_close_arriving_before_the_worker_reports_is_refused(self):
        """No ticket means nothing to settle. The tool must not read "no signal" as "delivered":
        closing here would throw away the work the session is still doing."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.next()
        before = sb.tree_hashes()

        action = sb.close_node_call(NODE, decision='continue')

        self.assertEqual(action['action'], 'wait')
        self.assertFalse(action.get('applied'))
        self.assertEqual(sb.tree_hashes(), before)
        self.assertEqual(sb.path(), [])
        self.assertEqual(sb.current_node(), NODE)

    def test_a_running_signal_is_not_a_green_light(self):
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'pending', 'retry is ready')
        action = sb.next()

        self.assertEqual(action['action'], 'wait')
        self.assertIn('cycle retry', action['note'])
        self.assertIn(NODE, action['note'])

    def test_the_close_reports_a_half_open_state_stably(self):
        """Asked twice with the ticket on disk and the close not yet run, the answer is the same
        and nothing is consumed -- the whole point of `wait` being a stable state."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done')
        before = sb.tree_hashes()

        first, second = sb.next(), sb.next()

        self.assertEqual(first['close_with'], second['close_with'])
        self.assertEqual(sb.tree_hashes(), before)

    def test_a_whole_run_of_nodes_closed_by_their_own_workers(self):
        """Three auto nodes, each closed by its own worker's call: the DAG drains, the path
        lists every step in order, and nothing needs a human.

        Two invariants ride along with the drain: the dispatches come out in chain order (a run
        IS the line), and the last close takes the line out of service (`inactive`) -- the
        uniform end-of-line write, whatever is left in the graph."""
        sb = self.make()
        ids = sb.add_chain(3)
        sb.start_cycle()
        final, driver = self.run_to_stop(sb)

        self.assertEqual(final['action'], 'stop_pipeline')
        self.assertEqual(sb.path_nodes(), ids)
        self.assertEqual(driver.nodes_closed, ids)
        self.assertEqual([sb.dag_status(uid) for uid in ids], ['completed'] * 3)
        self.assertEqual([uid for uid, _ in driver.dispatched], ids)   # dispatch order = chain order
        self.assertEqual(sb.cycle()['status'], 'inactive')             # the close takes the line off

    def test_the_cycle_can_be_driven_by_the_cli_alone(self):
        """The shipped path end to end: `next --after <node> --spawn` closes one node and pins the
        next, with no session ever started (the dry-run switch)."""
        sb = self.make(cli=True)
        ids = sb.add_chain(2)
        sb.start_cycle()
        driver = Driver(sb, worker=WorkerSession(sb))
        final = driver.run()

        self.assertEqual(final['action'], 'stop_pipeline')
        self.assertEqual(sb.path_nodes(), ids)
