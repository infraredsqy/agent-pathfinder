"""Running the maintenance plugin's line: the graph edit through the real processes, the walk, the form.

`TestMaintenanceCli` puts the close back on the real `dag-manage` / `state-manage` processes (the new
node's uuid only exists in that process's stdout), `TestMaintenanceDrive` walks a whole line from the
crossing to the far end, and `TestTheForm` pins what the line asks a worker for in its ticket (the
declaration's `ask` text, read back through `ticket_readings`).

Every class here drives the shipped declaration and therefore carries
`requires_plugin('maintenance')`: with that folder deleted the pipeline still closes and dispatches,
and these behaviour cases skip instead of failing. What the plugin does NOT depend on -- the
mechanism itself -- is pinned in `cases_insertion.py` and `cases_plugin.py`.

When the insert happens, which line it happens on, and how many nodes one metric gets all live in
`cases_maintenance.py`.

    python -m unittest conduct.tests.cases_maintenance_run    # run this file on its own
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import NODE, NODE2, Base  # noqa: E402
from cases_maintenance import (SelfHealingWorker, TEST_LINE, coding_line,  # noqa: E402
                               set_config)
from harness import HANDOFF_PY, _run_proc, requires_plugin  # noqa: E402

__all__ = ['TestMaintenanceCli', 'TestMaintenanceDrive', 'TestTheForm']


@requires_plugin('maintenance')
class TestMaintenanceCli(Base):
    """The graph edit goes through **the real processes**: the uuid `dag-manage add` prints exists only in
    its stdout.

    The sandbox answers `dag-manage` calls in this one process by default (saving a process start), and
    the cases above take that fast path; capturing a child's output is a different thing on the two
    paths -- this one puts the whole closing pass back on the real CLI, so the new node's uuid is
    printed by a real process and read back by `_capture`.

    It also pins the split the whole plugin is about, through the shipped surfaces: the closing call
    inserts and **opens nothing**, and the NEXT call is the one that picks the node up.
    """

    def test_the_insertion_opens_nothing_and_the_next_dispatch_picks_it(self):
        sb = self.make(cli=True)
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'test_seconds_limit', 60)
        sb.next()                                  # the real `next` CLI pins NODE and dispatches it
        sb.handoff('cyc', NODE, 'completed', 'done',
                   {'deliverable': 'x', 'reading': {'test_seconds': 400}})

        action, proc = sb.next_cli('--after', NODE)

        # 1. the close inserts: the node, its spec, and the two edges, through the real processes
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(action['action'], 'insert_node')
        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 3, f'the maintenance node was not inserted: {action}')
        made = action['node_id']
        self.assertEqual(nodes[made]['dependencies'], [NODE])
        self.assertEqual(nodes[NODE2]['dependencies'], [made])
        self.assertTrue((sb.root / '.pf' / 'nodes' / made / 'spec.md').is_file())
        # 2. ... and opens nothing: no prompt, no pin, nothing on the line
        self.assertEqual(sb.holding_ids(), [], 'the insertion pinned a node')
        self.assertFalse((sb.root / '.pf' / 'meta' / 'launch' / 'cyc' / made).exists())
        self.assertNotIn('prompt', action)
        # 3. the NEXT call is what dispatches it -- the pick decides, not the insertion
        dispatched, proc = sb.next_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(dispatched['action'], 'dispatch')
        self.assertEqual(dispatched['node_id'], made)
        self.assertEqual(sb.current_node(), made)
        # Dispatching stops at the prompt file: that is what `dispatch <cycle>` reads, and this case
        # asserts exactly "the file is there, holding the maintenance node's invocation line". **No
        # `dispatch` call and no `--spawn`** -- that path works and would really open a session (the
        # dispatch-semantics layer is guarded by `TestDispatchVerb`'s fake spawner).
        self.assertTrue(Path(dispatched['prompt']).is_file())
        self.assertEqual(Path(dispatched['prompt']).parent.name, made)
        self.assertIn(made, Path(dispatched['prompt']).read_text(encoding='utf-8'))

    def test_even_a_spawn_asked_for_opens_nothing(self):
        """`next --after <node> --spawn` still opens no session when the close inserted a node.

        The one flag that makes a closing call one-and-done keys off the answer being a dispatch, and
        an insertion is not one: the graph grows, and the dispatch path is never walked. This is the
        invariant at its sharpest -- not even a caller asking for a session gets one.
        """
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'test_seconds_limit', 60)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': 400}})

        action, proc = sb.spawn_next('--after', NODE)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(action['action'], 'insert_node')
        self.assertNotIn('spawned', action, 'the closing call opened a session for an insertion')
        self.assertEqual(len(sb.dag()['nodes']), 3)
        self.assertEqual(sb.holding_ids(), [])


@requires_plugin('maintenance')
class TestMaintenanceDrive(Base):
    """A whole line walked to the end: the maintenance node is dispatched as usual, closes as usual,
    then the line goes on to the one it deferred."""

    def test_the_driver_walks_through_the_inserted_node_and_on_to_the_deferred_one(self):
        """An unattended run: A reports an over-the-line reading, so the maintenance node runs before B and
        then B goes on.

        The maintenance node repaid that debt (the reading it reports back is under the line), so this
        run inserts **one** node and closes -- this case pins both "what was inserted is an ordinary
        node, dispatched as usual and closed as usual" and "once repaid, nothing more is inserted".
        """
        sb = coding_line(self, policies=[('test_seconds_limit', 60)], dispatch=False)

        # every session carries a reading in its note; the maintenance node reports the repaired number
        terminal, driver = self.run_to_stop(
            sb, worker=SelfHealingWorker(sb, {'test_seconds': 400}))

        made = [n for n, _ in driver.dispatched if n not in (NODE, NODE2)]
        self.assertEqual(len(made), 1,
                         f'the maintenance node was never dispatched: {driver.dispatched}')
        maintenance_node = made[0]
        self.assertEqual(sb.path_nodes(), [NODE, maintenance_node, NODE2],
                         'the maintenance node runs between the node it was inserted after and '
                         'the node that was already picked')
        self.assertEqual(terminal['action'], 'stop_pipeline')
        self.assertEqual(sb.dag()['nodes'][maintenance_node]['status'], 'completed')
        self.assertEqual(sb.dag()['nodes'][NODE2]['status'], 'completed')
        # the inserted node is an ordinary node: it is dispatched itself and delivers its own note
        self.assertEqual(sb.read_signal('cyc', maintenance_node)['status'], 'completed')

    def test_a_plain_run_never_produces_an_insertion_action(self):
        """With no threshold, no insertion ever appears on the line: this channel is additive, and
        shut it leaves no trace at all."""
        sb = self.make()
        sb.add_chain(2)
        sb.start_cycle(tags=['coding'])
        _, driver = self.run_to_stop(sb)

        self.assertNotIn('insert_node', driver.kinds())
        self.assertEqual(len(sb.dag()['nodes']), 2)

    def test_the_closing_call_keeps_the_worker_out_of_the_graph_edit(self):
        """The closing call hands back an insertion and no commands the caller could run again
        (invariant 6): the graph edit is the tool's business."""
        sb = coding_line(self, policies=[('test_seconds_limit', 60)], dispatch=False)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': 400}})

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'insert_node')
        self.assertFalse(action.get('commands'), 'the caller must have nothing left to run')
        # the tool's own two intermediates do not leak out
        self.assertNotIn('add_command', action)
        self.assertNotIn('spec_of', action)
        self.assertTrue(action['applied'])
        # what this settle did is checkable line by line: create the node, drop the old edge, wire the two
        # new edges -- and NOT one `cycle hold`: the insertion does not move the line's hand
        did = ' '.join(action['did'])
        self.assertIn('dag-manage.py add', did)
        self.assertIn('undepend', did)
        self.assertIn('depend', did)
        self.assertNotIn('cycle hold', did, 'an insertion must not pin a node')
        # the node it made is the node it reports, and nothing was opened for it
        self.assertNotIn('opened_node', action, 'an insertion is not an open')
        self.assertEqual(sb.holding_ids(), [])

    def test_the_worker_can_report_a_reading_through_the_real_handoff(self):
        """`reading` is an ordinary `outputs` key on the note: `handoff.py` takes it verbatim.

        The close reads only the ones it recognizes (`test_seconds`); the pool slot is computed by the
        issue tool even when it is written on the note.
        """
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'test_seconds_limit', 60)
        sb.next()
        sb.handoff('cyc', NODE, 'completed', 'done',
                   {'deliverable': 'x', 'reading': {'test_seconds': 400, 'issue_pool_weight': 3}})

        ticket = sb.read_signal('cyc', NODE)
        self.assertEqual(ticket['outputs']['reading'],
                         {'test_seconds': 400, 'issue_pool_weight': 3})
        inserted = sb.close_round(NODE)
        self.assertEqual(len(sb.dag()['nodes']), 3)
        made = inserted['node_id']
        # only the metric that crossed comes in; the one that did not stays where it is
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')
        self.assertIn('test_seconds', spec)
        self.assertIn('"reading": {"test_seconds"', spec)
        self.assertNotIn('issue_pool_weight', (sb.dag()['nodes'][made]['title']))

@requires_plugin('maintenance')
class TestTheForm(Base):
    """`handoff.py --form <cycle>`: what this line wants is printed on the spot by the note's
    receiver, and nothing can be written into it.

    The readings a form asks for are the declarations' own: which keys (`ticket_readings`, narrowed by
    each reading's `when_knob`) and which words (the declared `ask`) are merged out of the installed
    plugins, so this class is the other end of the declaration from `cases_plugin.py`'s registry
    cases.
    """

    def form(self, sb, cycle='cyc') -> str:
        return _run_proc(sb.root, [HANDOFF_PY, '--form', cycle]).stdout

    def a_line(self, **policies):
        """One coding node on a started line, with `policies` written onto the cycle."""
        return coding_line(self, policies=policies.items(), dispatch=False, pair=False)

    def test_a_line_without_numeric_policies_asks_for_nothing(self):
        """A project with no line configured (the `minimal` tier is one) gets a skeleton with no reading
        slot."""
        text = self.form(self.a_line())

        self.assertIn('-o \'{"deliverable"', text)
        self.assertNotIn('"reading"', text)

    def test_the_test_line_asks_for_the_wall_clock(self):
        text = self.form(self.a_line(test_seconds_limit=60))

        self.assertIn('"reading":{"test_seconds":', text)

    def test_a_threshold_stated_on_the_project_layer_is_asked_too(self):
        """The form reads the whole chain, like the close does: a `test_seconds_limit` written on the
        project layer (no line policy at all) is still a threshold this line is measured against."""
        sb = self.a_line()                     # no cycle policy whatsoever
        set_config(sb, TEST_LINE, 60)

        text = self.form(sb)

        self.assertIn('"reading":{"test_seconds":', text)

    def test_a_threshold_on_the_nodes_own_layer_is_asked_when_the_node_is_named(self):
        """The form resolves with the same context the close judges with: a threshold written on the
        node about to be settled is in force there, so naming that node asks for the reading. With no
        node named the answer stays the chain's own (this line and wider), exactly as before."""
        sb = self.a_line()                     # no cycle policy, no project threshold
        set_config(sb, TEST_LINE, 60, '--node', NODE)

        asked = _run_proc(sb.root, [HANDOFF_PY, '--form', 'cyc', NODE]).stdout
        unnamed = self.form(sb)

        self.assertIn('"reading":{"test_seconds":', asked)
        self.assertNotIn('"reading"', unnamed,
                         'a form that names no node claimed a layer a node states')

    def test_the_pool_line_never_becomes_a_ticket_slot(self):
        """The pool slot is computed by the issue tool: this line configured it, yet the key still does not
        appear on the form."""
        text = self.form(self.a_line(issue_pool_weight_limit=8))

        self.assertNotIn('issue_pool_weight', text)
        self.assertNotIn('"reading"', text)

    def test_the_form_writes_nothing(self):
        sb = self.a_line(test_seconds_limit=60)
        before = (sb.root / '.pf' / 'state.json').read_text(encoding='utf-8')

        self.form(sb)

        self.assertEqual((sb.root / '.pf' / 'state.json').read_text(encoding='utf-8'), before)
        signals = (sb.root / '.pf' / 'signals')
        self.assertEqual(sorted(signals.rglob('*.json')) if signals.exists() else [], [])

