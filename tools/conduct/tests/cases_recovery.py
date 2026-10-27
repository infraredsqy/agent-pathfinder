"""The levers that survive: the worker's own handoff, the signal guards, `clean-temp`, and the
cycle guards.

Nothing here rewinds a pointer or migrates an old layout: those levers belonged to the stage/round
model and are gone with it. What is left is the retry path (clear the ticket, pin the node again),
the two commands a worker runs, and the sweep that clears the one place process records go.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE, NODE2, NODE3  # noqa: E402
from harness import HANDOFF_PY, SIGNAL_PY, run_handoff, run_signal  # noqa: E402

__all__ = ['TestRetry', 'TestHandoff', 'TestSignalGuards', 'TestCleanTemp']


class TestRetry(Base):
    """A node parked `shelved` is retried by clearing its ticket and pinning it again -- the live repair.

    There is no `rewind` to reach for: the pointer is one node, and putting a node back into flight
    is a tool command (`cycle retry`, then `cycle hold`), not a session editing state.
    """

    def test_the_retry_pair_is_named_in_the_wait_note(self):
        """A `wait` that cannot say how to unwind itself is a dead end, so the note carries both
        commands of the documented retry."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.next()                                   # nothing reported yet: a node in flight
        sb.write_signal('cyc', NODE, 'pending', 'retry is ready')

        action = sb.next()

        self.assertEqual(action['action'], 'wait')
        self.assertIn(f'cycle retry cyc {NODE}', action['note'])
        self.assertIn(f'cycle hold cyc {NODE}', action['note'])

    def test_clearing_the_ticket_and_pinning_again_redispatches_the_node(self):
        """The whole retry, driven as processes: the stale ticket is gone, so the next reader
        answers `dispatch` for that same node instead of calling it finished."""
        sb = self.make(cli=True)
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'failed', 'the first run produced nothing')

        sb._tool('cycle', 'retry', 'cyc', NODE, '--summary', 'retry', cli=True)
        self.assertEqual(sb.read_signal('cyc', NODE)['status'], 'pending')

        sb.signal_path('cyc', NODE).unlink()        # the retry's own state, cleared for good
        sb._tool('cycle', 'hold', 'cyc', NODE, cli=True)
        action = sb.plan()

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE)
        self.assertEqual(sb.path(), [], 'the retry must not rewrite the accounting')

    def test_dropping_the_ticket_is_what_lets_the_retry_be_dispatched(self):
        """The retry's last step: take that note out.

        `running` is a red light too -- rewriting it without taking it out leaves `next` answering
        `wait` forever (the wait note lists exactly those three commands, and `--drop` is on the
        first of them). Once it is out, the same node reads as "nothing reported yet" again, and
        only then does the retry get up.
        """
        sb = self.make(cli=True)
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'pending', 'the previous run did not finish')

        sb._tool('cycle', 'retry', 'cyc', NODE, '--drop', cli=True)
        self.assertIsNone(sb.read_signal('cyc', NODE), 'no note may be left on disk after --drop')

        sb._tool('cycle', 'hold', 'cyc', NODE, cli=True)
        action = sb.plan()

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE)

    def test_a_failed_step_stays_in_the_path_with_its_own_result(self):
        """Retrying later never rewrites what happened: the step keeps the result the ticket
        reported, and the DAG status is put back only by the tool that owns it."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'failed', 'produced nothing')
        sb.close_round(NODE)

        self.assertEqual(sb.path()[0]['result'], 'failed')
        self.assertEqual(sb.dag_status(NODE), 'shelved')

    def test_the_redone_node_hands_off_again_and_the_path_keeps_both_verdicts(self):
        """The recovery section's redo chain walked to the end: the redone session's second note is
        not refused again.

        The `failed` verdict stays on the path as history, and the redo's `completed` is appended
        as a second step -- a walk revisiting a node is legal. The redo's delivery happens while
        the line is stopped: it only records (closes the slot, marks the DAG) and writes no line
        status.
        """
        sb = self.make(cli=True)
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.handoff('cyc', NODE, 'failed', summary='the first run produced nothing')
        sb.next_cli('--after', NODE)
        self.assertEqual(sb.path()[0]['result'], 'failed')

        sb.add_node(NODE, 'UI design', status='todo', mode='auto')   # revive: put it back to todo
        sb._tool('cycle', 'retry', 'cyc', NODE, '--drop', cli=True)
        sb._tool('cycle', 'hold', 'cyc', NODE, cli=True)

        sb.handoff('cyc', NODE, 'completed', summary='the redo delivered')
        sb.next_cli('--after', NODE)

        self.assertEqual([s['result'] for s in sb.path()], ['failed', 'completed'])
        self.assertEqual(sb.dag_status(NODE), 'completed')
        self.assertEqual(sb.cycle()['status'], 'inactive',
                         'a redo delivery on a stopped line only records; the line keeps the '
                         'status it was given')


class TestHandoff(Base):
    """`handoff.py <cycle> <node> <status>`: one command writes the ticket and stops there.

    It opens no session and closes nothing: closing the node is the worker's own
    `next --after <node>`, which is the one call that settles it. Driven as a real process -- this
    file IS the shipped surface for the end of a node.
    """

    def started(self):
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        return sb, sb.next()

    def outputs(self, node=NODE):
        return {'deliverable': '.pf/nodes/%s/pf-conduct.md' % node, 'acceptance': ['a'],
                'touched': []}

    def test_it_writes_the_ticket_and_says_how_to_close_the_node(self):
        sb, first = self.started()

        proc = sb.handoff('cyc', NODE, 'completed', 'pf-conduct finished', self.outputs())

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('the node is not closed yet', proc.stdout)
        self.assertIn(f'next --after {NODE}', proc.stdout)
        self.assertIn('no session was spawned', proc.stdout)
        # The ticket is on disk and nothing was consumed: only the close moves the cycle.
        self.assertEqual(sb.read_signal('cyc', NODE)['status'], 'completed')
        self.assertEqual(sb.current_node(), NODE)
        self.assertEqual(sb.path(), [])
        self.assertEqual(sb.next()['action'], 'wait')

    def test_it_never_asks_a_spawner_for_anything(self):
        """No test may ever call dsh-spawn.ps1, and this command stops short of asking one for anything."""
        sb, first = self.started()
        proc = sb.handoff('cyc', NODE, 'completed', 'done', self.outputs(), dry_run=True)
        self.assertNotIn('dry-run', proc.stdout)
        self.assertIn('no session was spawned', proc.stdout)

    def test_a_failed_report_still_reaches_the_disk(self):
        """A worker that reports `failed` still gets its report written: it is the record that the
        node was attempted, and the closing call is what reads it."""
        sb, first = self.started()

        proc = sb.handoff('cyc', NODE, 'failed', 'I produced nothing',
                          {'deliverable': '', 'acceptance': []})

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sb.read_signal('cyc', NODE)['summary'], 'I produced nothing')
        self.assertEqual(sb.current_node(), NODE)
        self.assertEqual(sb.close_round(NODE)['action'], 'stop_pipeline')
        self.assertEqual(sb.dag_status(NODE), 'shelved')

    def test_outputs_that_are_not_a_json_object_are_refused(self):
        """`-o` must be a JSON object: a bare list parses as JSON and is still the wrong shape, so
        the command refuses before it writes a ticket."""
        sb, first = self.started()
        proc = run_handoff(sb.root, 'cyc', NODE, 'completed', summary='x', outputs='[]',
                           check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('JSON object', proc.stderr)
        self.assertIsNone(sb.read_signal('cyc', NODE), 'a bad payload wrote a ticket anyway')

    def test_a_second_ticket_over_writes_the_first_one(self):
        """Until the node is closed, reporting again is the worker correcting itself -- and the
        ticket it leaves is the one the closing call reads."""
        sb, first = self.started()
        sb.handoff('cyc', NODE, 'completed', 'first time', self.outputs())
        sb.handoff('cyc', NODE, 'completed', 'second time', self.outputs())

        self.assertEqual(sb.read_signal('cyc', NODE)['summary'], 'second time')
        self.assertEqual(sb.path(), [])

    def test_a_ticket_with_no_session_spawned_leaves_no_marker(self):
        """The marker file is for a BROKEN chain; a healthy ticket must not leave one."""
        sb, first = self.started()
        proc = sb.handoff('cyc', NODE, 'completed', 'done', self.outputs())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((sb.root / '.pf' / 'meta' / 'CONDUCT-CALLBACK-FAILED-cyc.json').exists())

    def test_the_ticket_write_is_the_only_thing_that_lands(self):
        """Handing in a note lands that one note and nothing else: the other slot in hand does not
        get a prompt filled in because of this run.

        That other slot has neither a note nor a `started` stamp right now -- exactly the state
        that "write the next prompt in" would pick. The handoff command reads the next step once
        and hands it back; landing a prompt is the close's job.
        """
        sb = self.make()
        sb.add_node(NODE, 'A', mode='auto')
        sb.add_node(NODE2, 'B', mode='auto')
        sb.start_cycle(tags=['coding'])
        sb._tool('cycle', 'hold', 'cyc', NODE2, cli=True)
        self.assertEqual(sorted(sb.holding_ids()), sorted([NODE, NODE2]),
                         'both slots are not in hand')

        proc = sb.handoff('cyc', NODE, 'completed', 'done', self.outputs())

        self.assertEqual(proc.returncode, 0, proc.stderr)
        launch = sb.root / '.pf' / 'meta' / 'launch'
        written = sorted(p.parts[-2] for p in launch.rglob('prompt.txt')) if launch.exists() else []
        self.assertEqual(written, [], 'handing in a note wrote the next prompt')
        self.assertIsNone(sb.read_signal('cyc', NODE2), 'the other slot grew a note out of nowhere')
        self.assertEqual(sb.path(), [], "this run touched the line's path")
        self.assertEqual(sb.holding_ids(), [NODE, NODE2],
                         'this run touched the two slots in hand')

    def test_it_reads_the_next_step_instead_of_driving_it(self):
        """Handing in a note reads the next step through the read-only `plan`; the writer's
        `record` is outside this command's path.

        `record` is the conductor's mechanical routine: it applies, and it fills prompts in for
        the slots in hand. The handoff session reads the next step once and hands it back --
        closing a slot, picking the next one and landing the next prompt are the close's job. The
        one place it writes itself is the degradation marker (`marker.record`): the stamp left for
        a human to take over when the note cannot be written.
        """

        def receiver(node):
            """How the receiving end is written (`marker` inside `marker.record`)."""
            return getattr(node.value, 'id', getattr(node.value, 'attr', ''))

        calls = {f'{receiver(node.func)}.{node.func.attr}'
                 for node in ast.walk(ast.parse(HANDOFF_PY.read_text(encoding='utf-8')))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertEqual(sorted(call for call in calls if call.endswith('.record')), ['marker.record'],
                         'handoff.py calls the writer\'s record: handing in a note would write itself')
        self.assertIn('pipeline.plan', calls, 'handoff.py did not read the next step')


class TestSignalGuards(Base):
    """`signal.py <cycle> <node> <status>` and what the pipeline reads back off disk."""

    def test_the_payload_carries_the_cycle_and_the_node(self):
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()

        run_signal(sb.root, 'cyc', NODE, 'completed', 'done', {'deliverable': 'x'})

        sig = sb.read_signal('cyc', NODE)
        self.assertEqual(sig['cycle'], 'cyc')
        self.assertEqual(sig['node'], NODE)
        self.assertEqual(sig['status'], 'completed')
        self.assertEqual(sig['summary'], 'done')
        self.assertEqual(sig['outputs'], {'deliverable': 'x'})

    def test_a_running_ticket_is_a_wait_and_never_a_green_light(self):
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.next()
        # `signal.py` writes only completed / failed / stopped; a `running` ticket is what
        # `cycle retry` leaves behind while a retry is being prepped.
        sb.write_signal('cyc', NODE, 'pending', 'still running')

        action = sb.next()

        self.assertEqual(action['action'], 'wait')
        self.assertFalse(action.get('applied'))
        self.assertEqual(sb.path(), [])

    def test_a_ticket_that_is_not_json_fails_loudly_and_changes_nothing(self):
        """A broken file on disk is a broken chain: the tool stops instead of guessing, and it
        writes nothing while doing so."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_raw_signal('cyc', NODE, payload='{ this is not json')
        before = sb.tree_hashes()

        with self.assertRaises((ValueError, RuntimeError)):
            sb.next()
        self.assertEqual(sb.tree_hashes(), before)

    def test_an_absent_ticket_means_the_node_is_still_being_dispatched(self):
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.next()
        self.assertIsNone(sb.read_signal('cyc', NODE))
        self.assertEqual(sb.plan()['action'], 'dispatch')


class TestCleanTemp(Base):
    """`clean-temp`: sweeps each slot's own `temp/`, skipping the slot a line is holding.

    "Who is held" is a fact read out of this project's `state.json`, through the gate that reads
    the records: if it cannot be read, the command stops and touches no temp at all. Reading it as
    "no node is held" would let `--yes` delete a temp that is in use, on the strength of that
    reading.
    """

    def test_a_held_nodes_temp_survives_the_sweep_unless_forced(self):
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.add_temp(NODE)                       # the slot that is working right now

        proc = sb.clean_temp(yes=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertTrue(sb.temp_dir(NODE).is_dir(), 'a node in hand was swept')

        sb._tool('clean-temp', '--project', str(sb.root), '--yes', '--force')
        self.assertFalse(sb.temp_dir(NODE).exists(), '--force takes a live node anyway')

    def test_an_unreadable_state_is_not_read_as_nobody_holds_anything(self):
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.add_temp(NODE)
        (sb.root / '.pf' / 'state.json').write_text('{ not json', encoding='utf-8')

        proc = sb.clean_temp(yes=True, check=False)

        self.assertNotEqual(0, proc.returncode,
                            'an unreadable state must not be taken as saying nothing')
        self.assertIn('state.json', proc.stderr)
        self.assertTrue(sb.temp_dir(NODE).is_dir(),
                        'with state.json unreadable, no temp may be touched')

    def test_the_listing_run_names_what_it_would_sweep_and_removes_nothing(self):
        """Without `--yes`: it lists what could be swept and stops, not moving a byte on disk --
        including the slot that really would be swept."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.add_node(NODE2, 'another slot', mode='auto')
        sb.start_cycle()
        sb.add_temp(NODE, 'held.txt')                  # held: named only, not swept
        sb.add_temp(NODE2, 'free.txt')                 # not held: this run does not sweep it either
        before = sb.tree_hashes()

        proc = sb.clean_temp()

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn(NODE2[:8], proc.stdout, 'the sweepable slot must be named')
        self.assertIn(NODE[:8], proc.stdout, 'the skipped slot must be named too')
        self.assertEqual(before, sb.tree_hashes(), 'a run without --yes moved something on disk')

    def test_a_named_node_is_swept_and_nothing_outside_its_temp_moves(self):
        """`--node` sweeps only the named slot: the spec, state.json, ideas.json and other nodes'
        temps do not move a character."""
        sb = self.make()
        for uid, title in ((NODE, 'Write code'), (NODE2, 'Write copy'), (NODE3, 'third slot')):
            sb.add_node(uid, title, mode='auto')
        sb.start_cycle()
        held = sb.current_node()
        target, untouched = [uid for uid in (NODE, NODE2, NODE3) if uid != held]
        for uid in (held, target, untouched):
            sb.add_temp(uid, 'probe.txt')
        ledger = {str(path): path.read_bytes() for path in
                  (sb.root / '.pf' / 'state.json', sb.root / '.pf' / 'ideas.json',
                   sb.root / '.pf' / 'nodes' / target / 'spec.md')}

        proc = sb.clean_temp(node=target, yes=True)

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertFalse(sb.temp_dir(target).exists(), 'the named slot must be swept')
        self.assertTrue(sb.temp_dir(held).is_dir(), 'the other, held slot must not move')
        self.assertTrue(sb.temp_dir(untouched).is_dir(), 'the unnamed slot must not move')
        self.assertEqual(ledger, {path: Path(path).read_bytes() for path in ledger},
                         'records outside temp were touched')


