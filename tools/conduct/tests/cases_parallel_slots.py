"""A line's `parallel` policy: how many slots it holds at the start, and how a close refills the
slots it freed.

The cap lives on the line itself (`policies.parallel`), and the start's pick and every later
close's refill read the same number: one slot dispatches "every node in hand right now", and a
refill fills only the free slots (`parallel` minus what is still held); if the graph cannot supply
them, nothing is filled.

The mechanical gates between two lines (the on-disk mutex, the worktree return to the main tree,
the cross-cycle pin guard, SPAWN.claim) are in `cases_parallel.py`.

    python conduct/tests/cases_parallel_slots.py       # run this file alone
    python conduct/tests/test_conduct_pipeline.py      # with the whole suite
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import NODE, NODE2, NODE3, Base  # noqa: E402

__all__ = ['TestParallelSlots']


class TestParallelSlots(Base):
    """A line's `parallel` policy: how many nodes it holds at once, and the slots a close frees are
    refilled on the spot."""

    def two_ready(self, parallel=None):
        """Two ready nodes with no dependency between them: `parallel` decides how many are held
        at the start."""
        sb = self.make()
        sb.add_node(NODE, 'A', mode='auto')
        sb.add_node(NODE2, 'B', mode='auto')
        sb.start_cycle(tags=['coding'])
        if parallel is not None:
            sb.set_policy('cyc', 'parallel', parallel)
        return sb

    def test_the_default_is_one_slot(self):
        """With no `parallel` configured it is 1: the draw runs one slot at a time as before, and
        extra slots do not open on their own."""
        sb = self.two_ready()
        cycle = sb.cycle()
        self.assertEqual(cycle.get('policies', {}).get('parallel', 1), 1)
        self.assertEqual(len(sb.holding_ids()), 1, 'the default must pin exactly one node')

    def test_the_start_pins_the_slots_the_line_states(self):
        """The start says how many slots the line holds: `cycle start --parallel 2` pins two slots
        on the spot and dispatches two prompts at once.

        The same number lands on `policies.parallel` (`cycle policy` writes that one), so the
        start's pick and every later close's refill read the same number.
        """
        sb = self.make()
        sb.add_node(NODE, 'A', mode='auto')
        sb.add_node(NODE2, 'B', mode='auto')
        sb.start_cycle(tags=['coding'], parallel=2)

        self.assertEqual(sb.cycle()['policies']['parallel'], 2)
        self.assertEqual(sorted(sb.holding_ids()), sorted([NODE, NODE2]),
                         'the start must pin both slots')

        action = sb.next()

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(sorted(n['node_id'] for n in action['nodes']), sorted([NODE, NODE2]))
        for item in action['nodes']:
            self.assertTrue(Path(item['prompt']).is_file(), item['prompt'])

    def test_the_start_refuses_a_parallel_that_is_not_a_positive_integer(self):
        """The number given at the start is judged by the same rule as `cycle policy`: 0 is
        refused on the spot, and `off` falls back to the default."""
        sb = self.make()
        sb.add_node(NODE, 'A', mode='auto')
        sb.add_node(NODE2, 'B', mode='auto')

        refused = sb.start_cycle(tags=['coding'], parallel=0, check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('parallel', refused.stderr)
        self.assertEqual([], [c['id'] for c in (sb.state() or {'cycles': []})['cycles']],
                         'a refused start must not leave a line behind')

        sb.start_cycle(tags=['coding'], parallel='off')
        self.assertEqual(len(sb.holding_ids()), 1,
                         '`off` falls back to the default: one slot at a time')
        self.assertNotIn('parallel', sb.cycle().get('policies') or {})

    def test_parallel_two_pins_two_and_dispatches_one_action_for_both(self):
        """`parallel=2`: the start pins both ready nodes, and one dispatch opens both prompts at
        once."""
        sb = self.two_ready(parallel=2)
        # the policy lands after the start: pin the second slot explicitly
        sb.set_current_node(NODE2)
        action = sb.next()
        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(sorted(n['node_id'] for n in action['nodes']), sorted([NODE, NODE2]))
        for item in action['nodes']:
            self.assertTrue(Path(item['prompt']).is_file(), item['prompt'])
            self.assertTrue(item['prompt_text'])
        self.assertEqual(sorted(sb.holding_ids()), sorted([NODE, NODE2]))

    def test_a_close_fills_the_slot_it_freed(self):
        """Two slots in hand, one closed: the fill takes up the next ready one -- the line carries
        on by itself, with no empty slot left."""
        sb = self.make()
        sb.add_node(NODE, 'A', mode='auto')
        sb.add_node(NODE2, 'B', mode='auto')
        sb.add_node(NODE3, 'C', mode='auto')
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'parallel', 2)
        sb.set_current_node(NODE2)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done')

        first = sb.close_round(NODE)

        # Closing one slot frees a place and the graph still has ready work: the same close fills
        # it and dispatches -- the hand is full again, and the dispatch carries "every node in
        # hand right now" (the freshly filled one is in `nodes`).
        self.assertEqual(first['action'], 'dispatch')
        self.assertEqual(first['nodes'][0]['node_id'], NODE2,
                         'the dispatch names every node in hand, not only the fresh one')
        self.assertEqual(sorted(sb.holding_ids()), sorted([NODE2, NODE3]),
                         'the slot the close freed must be refilled')

    def test_a_close_fills_every_free_slot(self):
        """One slot in hand; closing it leaves two ready in the graph: of the three free slots two
        must be filled -- and both must land on disk.

        The free count is `parallel` minus what is still held; as many are pinned as the list
        yields. Pinning only the head of that list would leave the second slot quietly empty: the
        action's `nodes` would name it while the hand would not hold it.
        """
        sb = self.make()
        for uid, name in ((NODE, 'A'), (NODE2, 'B'), (NODE3, 'C')):
            sb.add_node(uid, name, mode='auto')
        sb.start_cycle(tags=['coding'])           # the start pins one only, on the default parallel=1
        sb.set_policy('cyc', 'parallel', 3)
        closing = sb.current_node()
        rest = sorted({NODE, NODE2, NODE3} - {closing})
        sb.write_signal('cyc', closing, 'completed', 'done')

        action = sb.close_round(closing)

        self.assertEqual(sorted(sb.holding_ids()), rest,
                         'every freed slot must be refilled, not only the head of the fill')
        self.assertEqual(sorted(n['node_id'] for n in action['nodes']), rest,
                         'the dispatch names every node in hand')
        for item in action['nodes']:
            self.assertTrue(Path(item['prompt']).is_file(), item['prompt'])

    def test_a_close_fills_the_free_slots_and_no_more(self):
        """Three slots in hand, one closed: only the freed slot is filled, and the line never goes
        past `parallel`.

        Filling is not blindly three: the free count is tied to how many slots are still running
        (`parallel` minus what is still held), so as many are filled as the graph can supply, and
        none when it cannot.
        """
        sb = self.make()
        four = (NODE, NODE2, NODE3, 'dddddddd-4444-4444-8444-dddddddddddd')
        for i, uid in enumerate(four):
            sb.add_node(uid, f'Node {i}', mode='auto')
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'parallel', 3)
        closing = sb.current_node()
        rest = [u for u in four if u != closing]
        sb.set_current_node(rest[0])
        sb.set_current_node(rest[1])              # three slots in hand (full)
        sb.write_signal('cyc', closing, 'completed', 'done')

        action = sb.close_round(closing)

        self.assertEqual(len(sb.holding_ids()), 3, 'the hand stays at the cap')
        self.assertEqual(sorted(sb.holding_ids()), sorted(rest),
                         'only the freed slot is filled, and it is filled')
        self.assertEqual(sorted(n['node_id'] for n in action['nodes']), sorted(rest))

    def test_a_close_with_nothing_to_fill_keeps_the_line_in_service(self):
        """Two slots in hand, nothing in the graph to fill with: that slot is closed and the line
        stays in service -- the slot's own close carries on.

        Failing to fill has two shapes: the graph really has no work left (the line stops and
        waits for a human), and other nodes of this line are still in flight (the line has not
        stopped; the baton is on those nodes' own closes). If the latter parked too, their closes
        would only record, and nobody would pick up the work they freed.
        """
        sb = self.make()
        sb.add_node(NODE, 'A', mode='auto')
        sb.add_node(NODE2, 'B', mode='auto')
        sb.add_node(NODE3, 'C', mode='auto', deps=[NODE2])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'parallel', 2)
        sb.set_current_node(NODE)
        sb.set_current_node(NODE2)
        sb.write_signal('cyc', NODE, 'completed', 'done')

        first = sb.close_round(NODE)

        self.assertEqual(first['action'], 'stop_pipeline')
        self.assertEqual(sb.cycle()['status'], 'active',
                         'other nodes are still in flight, so this slot does not stop the line')
        self.assertEqual(sb.holding_ids(), [NODE2])
        self.assertIn(NODE2[:8], first['reason'], 'the reason names who is still held')

        sb.write_signal('cyc', NODE2, 'completed', 'done')
        second = sb.close_round(NODE2)

        self.assertEqual(second['action'], 'dispatch')
        self.assertEqual(sb.holding_ids(), [NODE3], 'that close fills in C and dispatches')

    def stub(self, sb):
        """The stand-in spawner these cases aim the real spawn call at (never a host one)."""
        return sb.fake_spawner('ok-spawner.ps1')

    def test_a_started_node_is_never_handed_to_the_spawner_twice(self):
        """A slot already opened is never handed to the spawner again: the close re-plans a full
        list, this one is in it, and it is skipped.

        `started` is the stamp a successful dispatch leaves (the session takes the prompt, the
        claim is withdrawn, the directory stays as the landing spot). Without it, one refill would
        open a second session onto a running sibling -- same node, same worktree, not reclaimable.
        """
        sb = self.make()
        for uid, name in ((NODE, 'A'), (NODE2, 'B'), (NODE3, 'C')):
            sb.add_node(uid, name, mode='auto')
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'parallel', 2)
        sb.set_current_node(NODE)
        sb.set_current_node(NODE2)
        sb.next()
        launch = sb.root / '.pf' / 'meta' / 'launch' / 'cyc'

        _opened, opened = sb.spawn_next('--cycle', 'cyc', spawner=self.stub(sb), dry_run=False)
        self.assertEqual(opened.returncode, 0, opened.stderr)
        for node in (NODE, NODE2):
            self.assertTrue((launch / node / 'started').is_file(),
                            f'{node[:8]}: the stamp did not land')
            self.assertFalse((launch / node / 'prompt.txt').exists(),
                             f'{node[:8]}: the prompt must have been taken')

        sb.write_signal('cyc', NODE, 'completed', 'done')
        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual([n['node_id'] for n in action['nodes']], [NODE3],
                         'the dispatch list holds only the slot waiting to start')
        self.assertFalse((launch / NODE2 / 'prompt.txt').exists(),
                         'for B, which is running, the prompt must not be filled back in -- '
                         'filling it back in would say it had not started')

        action, _ = sb.spawn_next('--cycle', 'cyc', spawner=self.stub(sb), dry_run=False)
        self.assertEqual([n['node_id'] for n in action['nodes']], [NODE3],
                         'a running B does not enter the list')

    def test_with_every_session_in_hand_running_the_answer_is_a_wait(self):
        """Every slot in hand has been opened: `plan` answers `wait`, and a dispatch refuses
        loudly (naming the way to reopen that slot)."""
        sb = self.make()
        sb.add_node(NODE, 'A', mode='auto')
        sb.start_cycle(tags=['coding'])
        sb.next()
        self.assertEqual(
            sb.spawn_next('--cycle', 'cyc', spawner=self.stub(sb), dry_run=False)[1].returncode, 0)

        action = sb.plan()

        self.assertEqual(action['action'], 'wait',
                         'every session is running, so there is no action to dispatch')
        self.assertIn('wait for their notes', action['note'])

        proc = sb.dispatch(spawner=self.stub(sb), dry_run=False, check=False)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('cycle retry', proc.stderr, 'the refusal must name the way to reopen that slot')

    def test_the_limit_is_the_line_own_policy(self):
        """The cap lives on the line itself: `cycle policy cyc parallel 2` takes effect, and 0 and
        negatives are refused."""
        sb = self.two_ready()
        sb.set_policy('cyc', 'parallel', 2)
        self.assertEqual(sb.cycle()['policies']['parallel'], 2)
        refused = sb.set_policy('cyc', 'parallel', 0, check=False)
        self.assertNotEqual(refused.returncode, 0)

    def test_the_dispatch_sweep_opens_one_session_per_node_it_holds(self):
        """`dispatch <cycle>` is a sweep: every node in hand opens a session, each taking its own
        prompt."""
        sb = self.two_ready(parallel=2)
        sb.set_current_node(NODE2)
        sb.next()
        _action, opened = sb.spawn_next(spawner=self.stub(sb), dry_run=False)
        self.assertEqual(opened.returncode, 0, opened.stderr)
        for node in (NODE, NODE2):
            self.assertFalse((sb.root / '.pf' / 'meta' / 'launch' / 'cyc' / node
                              / 'SPAWN.claim').exists(),
                             f'{node}: a started session must drop its launch dir')


if __name__ == '__main__':
    unittest.main()