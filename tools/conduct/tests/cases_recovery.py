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
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.next()                                   # nothing reported yet: a node in flight
        sb.write_signal('cyc', NODE, 'pending', '重试已备好')

        action = sb.next()

        self.assertEqual(action['action'], 'wait')
        self.assertIn(f'cycle retry cyc {NODE}', action['note'])
        self.assertIn(f'cycle hold cyc {NODE}', action['note'])

    def test_clearing_the_ticket_and_pinning_again_redispatches_the_node(self):
        """The whole retry, driven as processes: the stale ticket is gone, so the next reader
        answers `dispatch` for that same node instead of calling it finished."""
        sb = self.make(cli=True)
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'failed', '第一次没做出来')

        sb._tool('cycle', 'retry', 'cyc', NODE, '--summary', '重试', cli=True)
        self.assertEqual(sb.read_signal('cyc', NODE)['status'], 'pending')

        sb.signal_path('cyc', NODE).unlink()        # the retry's own state, cleared for good
        sb._tool('cycle', 'hold', 'cyc', NODE, cli=True)
        action = sb.plan()

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE)
        self.assertEqual(sb.path(), [], 'the retry must not rewrite the accounting')

    def test_dropping_the_ticket_is_what_lets_the_retry_be_dispatched(self):
        """重试的最后一步：把那张条子拿掉。

        `running` 也是红灯 —— 只把它重写一遍、不拿掉，`next` 永远答 `wait`（wait 的 note 里列的
        就是那三条，`--drop` 在头一条上）。拿掉之后同一个节点重新读作「还没报」，重试才起得来。
        """
        sb = self.make(cli=True)
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'pending', '上一次没跑完')

        sb._tool('cycle', 'retry', 'cyc', NODE, '--drop', cli=True)
        self.assertIsNone(sb.read_signal('cyc', NODE), '--drop 之后盘上不该还有条子')

        sb._tool('cycle', 'hold', 'cyc', NODE, cli=True)
        action = sb.plan()

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE)

    def test_a_failed_step_stays_in_the_path_with_its_own_result(self):
        """Retrying later never rewrites what happened: the step keeps the result the ticket
        reported, and the DAG status is put back only by the tool that owns it."""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'failed', '没做出来')
        sb.close_round(NODE)

        self.assertEqual(sb.path()[0]['result'], 'failed')
        self.assertEqual(sb.dag_status(NODE), 'shelved')

    def test_the_redone_node_hands_off_again_and_the_path_keeps_both_verdicts(self):
        """§8 的重做链走到底：重做的会话再次交条子不再被拒。

        failed 的判决留在 path 上作历史，重做的 completed 追加为第二步——行走的重访是合法的。
        重做交付发生在线停着的时候：只做登记（收格、标 DAG），线的状态不写。
        """
        sb = self.make(cli=True)
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.handoff('cyc', NODE, 'failed', summary='第一次没做出来')
        sb.next_cli('--after', NODE)
        self.assertEqual(sb.path()[0]['result'], 'failed')

        sb.add_node(NODE, '界面设计', status='todo', mode='auto')   # revive：放回 todo
        sb._tool('cycle', 'retry', 'cyc', NODE, '--drop', cli=True)
        sb._tool('cycle', 'hold', 'cyc', NODE, cli=True)

        sb.handoff('cyc', NODE, 'completed', summary='重做成了')
        sb.next_cli('--after', NODE)

        self.assertEqual([s['result'] for s in sb.path()], ['failed', 'completed'])
        self.assertEqual(sb.dag_status(NODE), 'completed')
        self.assertEqual(sb.cycle()['status'], 'inactive',
                         '停着的线上重做交付只做登记；线的状态保持人给它的那个')


class TestHandoff(Base):
    """`handoff.py <cycle> <node> <status>`: one command writes the ticket and stops there.

    It opens no session and closes nothing: closing the node is the worker's own
    `next --after <node>`, which is the one call that settles it. Driven as a real process -- this
    file IS the shipped surface for the end of a node.
    """

    def started(self):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        return sb, sb.next()

    def outputs(self, node=NODE):
        return {'deliverable': '.pf/nodes/%s/pf-conduct.md' % node, 'acceptance': ['a'],
                'touched': []}

    def test_it_writes_the_ticket_and_says_how_to_close_the_node(self):
        sb, first = self.started()

        proc = sb.handoff('cyc', NODE, 'completed', 'pf-conduct 做完了', self.outputs())

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
        """No test may ever call dsh-spawn.ps1, and this command no longer wants to."""
        sb, first = self.started()
        proc = sb.handoff('cyc', NODE, 'completed', 'done', self.outputs(), dry_run=True)
        self.assertNotIn('dry-run', proc.stdout)
        self.assertIn('no session was spawned', proc.stdout)

    def test_a_failed_report_still_reaches_the_disk(self):
        """A worker that reports `failed` still gets its report written: it is the record that the
        node was attempted, and the closing call is what reads it."""
        sb, first = self.started()

        proc = sb.handoff('cyc', NODE, 'failed', '我什么都没做出来',
                          {'deliverable': '', 'acceptance': []})

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sb.read_signal('cyc', NODE)['summary'], '我什么都没做出来')
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
        sb.handoff('cyc', NODE, 'completed', '第一次', self.outputs())
        sb.handoff('cyc', NODE, 'completed', '第二次', self.outputs())

        self.assertEqual(sb.read_signal('cyc', NODE)['summary'], '第二次')
        self.assertEqual(sb.path(), [])

    def test_a_ticket_with_no_session_spawned_leaves_no_marker(self):
        """The marker file is for a BROKEN chain; a healthy ticket must not leave one."""
        sb, first = self.started()
        proc = sb.handoff('cyc', NODE, 'completed', 'done', self.outputs())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((sb.root / '.pf' / 'meta' / 'CONDUCT-CALLBACK-FAILED-cyc.json').exists())

    def test_the_ticket_write_is_the_only_thing_that_lands(self):
        """交条子这一拍只落那一张条子：手里另一格不因为这一趟而被补上 prompt。

        另一格此刻既没有条子、也没有 `started` 记号 —— 正是「补写下一份 prompt」会挑中的那个
        状态。交条子的命令读一遍下一步就交回去，落 prompt 是收尾那一拍的事。
        """
        sb = self.make()
        sb.add_node(NODE, '甲', mode='auto')
        sb.add_node(NODE2, '乙', mode='auto')
        sb.start_cycle(tags=['coding'])
        sb._tool('cycle', 'hold', 'cyc', NODE2, cli=True)
        self.assertEqual(sorted(sb.holding_ids()), sorted([NODE, NODE2]), '两格没都在手里')

        proc = sb.handoff('cyc', NODE, 'completed', 'done', self.outputs())

        self.assertEqual(proc.returncode, 0, proc.stderr)
        launch = sb.root / '.pf' / 'meta' / 'launch'
        written = sorted(p.parts[-2] for p in launch.rglob('prompt.txt')) if launch.exists() else []
        self.assertEqual(written, [], '交条子这一拍写出了下一份 prompt')
        self.assertIsNone(sb.read_signal('cyc', NODE2), '另一格凭空多了一张条子')
        self.assertEqual(sb.path(), [], '这一拍动了线的路径')
        self.assertEqual(sb.holding_ids(), [NODE, NODE2], '这一拍动了手里那两格')

    def test_it_reads_the_next_step_instead_of_driving_it(self):
        """交条子读下一步走的是只读的 `plan`；`record` 那条写方在这条命令的路外。

        `record` 是 conductor 的机械例行：它 apply、它替手里的格补 prompt。交条子的会话读一遍
        下一步就交回去 —— 收格、挑下一格、落下一份 prompt 都是收尾那一拍的事。上面那条钉住这一
        拍的效果，这条钉住落笔那条路压根不在它手上。
        """
        called = {node.func.attr for node in ast.walk(ast.parse(
            HANDOFF_PY.read_text(encoding='utf-8')))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertNotIn('record', called, 'handoff.py 调上了写方：交条子会落笔')
        self.assertIn('plan', called, 'handoff.py 没读下一步')


class TestSignalGuards(Base):
    """`signal.py <cycle> <node> <status>` and what the pipeline reads back off disk."""

    def test_the_payload_carries_the_cycle_and_the_node(self):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
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
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.next()
        # `signal.py` writes only completed / failed / stopped; a `running` ticket is what
        # `cycle retry` leaves behind while a retry is being prepped.
        sb.write_signal('cyc', NODE, 'pending', '还在跑')

        action = sb.next()

        self.assertEqual(action['action'], 'wait')
        self.assertFalse(action.get('applied'))
        self.assertEqual(sb.path(), [])

    def test_a_ticket_that_is_not_json_fails_loudly_and_changes_nothing(self):
        """A broken file on disk is a broken chain: the tool stops instead of guessing, and it
        writes nothing while doing so."""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_raw_signal('cyc', NODE, payload='{ this is not json')
        before = sb.tree_hashes()

        with self.assertRaises((ValueError, RuntimeError)):
            sb.next()
        self.assertEqual(sb.tree_hashes(), before)

    def test_an_absent_ticket_means_the_node_is_still_being_dispatched(self):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.next()
        self.assertIsNone(sb.read_signal('cyc', NODE))
        self.assertEqual(sb.plan()['action'], 'dispatch')


class TestCleanTemp(Base):
    """`clean-temp`：扫每一格自己的 `temp/`，被某条线握着的那一格跳过。

    「谁被握着」是从这个项目的 `state.json` 读出来的事实，走的是读记录的那道门：读不懂就停下，
    一个 temp 都不动。把它读成「没有一个节点被握着」，`--yes` 就会照着这句话把正在用的 temp 删掉。
    """

    def test_a_held_nodes_temp_survives_the_sweep_unless_forced(self):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.add_temp(NODE)                       # 正在干活的那一格

        proc = sb.clean_temp(yes=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertTrue(sb.temp_dir(NODE).is_dir(), 'a node in hand was swept')

        sb._tool('clean-temp', '--project', str(sb.root), '--yes', '--force')
        self.assertFalse(sb.temp_dir(NODE).exists(), '--force takes a live node anyway')

    def test_an_unreadable_state_is_not_read_as_nobody_holds_anything(self):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.add_temp(NODE)
        (sb.root / '.pf' / 'state.json').write_text('{ not json', encoding='utf-8')

        proc = sb.clean_temp(yes=True, check=False)

        self.assertNotEqual(0, proc.returncode, '读不懂 state，这一趟不许当它没说过话')
        self.assertIn('state.json', proc.stderr)
        self.assertTrue(sb.temp_dir(NODE).is_dir(), 'state.json 读不懂时一个 temp 都不许动')


