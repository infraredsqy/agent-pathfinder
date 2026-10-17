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
from harness import (CONDUCT_DIR, Closer, Driver, WorkerSession,  # noqa: E402
                     _dag_hook, stop_after)


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
        sb.add_node(NODE2, '中途加的节点', mode='auto')
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
        sb.write_signal('cyc', NODE, 'completed', 'pf-conduct 做完了')
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
        """停手只有一句话可写：`inactive`。「图上还剩没剩活」问的是当时的图，而图会长大，停下来的
        线该不该接着跑是人说了算的事。"""
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
        sb.write_signal('cyc', NODE, 'failed', '我什么都没做出来')
        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertEqual(sb.dag_status(NODE), 'shelved')
        self.assertEqual(sb.path()[0]['result'], 'failed')
        self.assertEqual(sb.cycle()['status'], 'inactive')
        self.assertEqual(sb.read_signal('cyc', NODE)['summary'], '我什么都没做出来')
        self.assertIn('没有交付', action.get('reason', ''))

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
        sb.write_signal('cyc', NODE, 'stopped', '用户叫停了')
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

    **要不要接着走，只看节点自己的 mode**：`checkpoint` 在收尾那一拍停下交人；`manual` 的人闸
    在格子的会话里（整格与用户来回推进，条子上的 completed 就是用户那句「标成完成，继续」），
    收尾后照 `auto` 接着走；同一个判断只有这一处出处。
    """

    def test_auto_carries_on_to_the_node_behind_it(self):
        """三档里只有 `auto` 会自己开下一个节点：它是「全程不问、收尾自己接着走」那一档。"""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '接着做', mode='auto', deps=[NODE])
        sb.start_cycle()
        final, driver = self.run_to_stop(sb)

        self.assertEqual(final['action'], 'stop_pipeline')
        self.assertEqual(len(driver.dispatched), 2, '两个节点都被派出去过')
        self.assertEqual(sb.path_nodes(), [NODE, NODE2])

    def test_a_manual_node_carries_on_once_the_user_releases_it(self):
        """`manual` 的人闸在格子的会话里：整格就是与用户来回推进，条子上的 completed 就是
        用户那句「标成完成，继续」。收尾这一拍不再设第二道闸——线照 `auto` 一样接着走。"""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='manual', deps=())
        sb.add_node(NODE2, '下一个', mode='auto', deps=[NODE])
        sb.start_cycle()
        final, driver = self.run_to_stop(sb)

        self.assertEqual(final['action'], 'stop_pipeline')
        self.assertEqual(sb.path_nodes(), [NODE, NODE2])
        self.assertEqual(len(driver.dispatched), 2)

    def test_checkpoint_hands_back_to_the_user_at_the_end_of_its_node(self):
        """中间那一档：一轮循环跑完、记录收干净，然后停下来等人说一句「继续」。

        与 `auto` 的差别只有一处 —— 收尾不自己开下一个节点；节点内部的跑法一模一样。
        """
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='checkpoint')
        sb.add_node(NODE2, '接着做', mode='auto', deps=[NODE])
        sb.start_cycle()
        final, driver = self.run_to_stop(sb)

        self.assertEqual(final['action'], 'stop_pipeline')
        self.assertEqual(sb.path_nodes(), [NODE], '这个节点交出去了')
        self.assertEqual(sb.dag_status(NODE), 'completed')
        self.assertEqual(len(driver.dispatched), 1, '下一个节点没被开起来')
        self.assertEqual(sb.dag_status(NODE2), 'todo')
        self.assertIn('checkpoint', final['reason'])
        self.assertEqual(sb.cycle()['status'], 'inactive',
                         '停手把线转成 inactive；放回服务的那条命令在 reason 里')
        self.assertIn('cycle status cyc active', final['reason'])

    def test_a_continue_cannot_loosen_the_stop_rule(self):
        """The hole the rule exists to close: the closing call says carry on, the node's own mode
        says hand back to the user. The rule wins, and the worker is told why."""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='checkpoint')
        sb.add_node(NODE2, '下一个', mode='auto', deps=[NODE])
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
        sb.instruct('做完两个节点就停', scope='cycle', target='boundary', stop_after_nodes=2)
        final, _ = self.run_to_stop(sb)

        self.assertEqual(final['action'], 'stop_pipeline')
        self.assertEqual(sb.path_nodes(), ids[:2])
        self.assertEqual(sb.dag_status(ids[2]), 'todo')

    def test_the_second_close_is_where_a_cap_of_two_stops(self):
        """跑够 N 个就停: at the N-th close every one of them has already been delivered."""
        sb = self.make()
        ids = sb.add_chain(2)
        sb.start_cycle()
        sb.instruct('跑够一个就停', scope='cycle', target='boundary', stop_after_nodes=1)
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
        sb.add_node(NODE, '界面设计', mode='checkpoint')
        sb.start_cycle()
        final, _ = self.run_to_stop(sb)

        self.assertEqual(final['action'], 'stop_pipeline')
        self.assertIn('mode=checkpoint', final['reason'])

    def test_a_non_delivery_reaches_the_stop_package(self):
        """The worker's `failed` ticket is the verdict; what a human is handed is the node it left
        `shelved`, under `kind: node_shelved`."""
        sb = self.ready_sandbox()
        sb.write_signal('cyc', NODE, 'failed', '完全失败')
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
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '另一条支线', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'failed', '没做出来')
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

        sb.write_signal('cyc', NODE, 'failed', '完全失败')
        failed_plan = sb.plan()
        self.assertEqual(failed_plan['action'], 'wait')

        applied = sb.close_round(NODE)
        self.assertEqual(applied['action'], 'stop_pipeline')
        self.assertEqual(sb.dag_status(NODE), 'shelved')
        self.assertEqual([i['kind'] for i in applied['attention']], ['node_shelved'])


class TestShelvedPolicy(Base):
    """`when_shelved`: 节点收了尾却没交付（它于是变成 shelved）时，这条线停还是接着走。

    策略是**这条线**的属性（与 `tags` 同一类东西）：写在 cycle 记录里，跟着它走，缺省是 `stop`
    —— 读不准时停下等人，总比一条线自己跑下去强。
    """

    def pair(self):
        """Two auto nodes in a line, the cycle pinned on the first one."""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '接着做', mode='auto', deps=[NODE])
        sb.start_cycle()
        return sb

    def test_the_default_is_stop(self):
        """没写 policies 的 cycle 在记录里就是今天这份形状：默认 stop，节点标 shelved，线停下等人。"""
        sb = self.pair()
        self.assertNotIn('policies', sb.cycle())

        sb.write_signal('cyc', NODE, 'failed', '没做出来')
        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertEqual(sb.dag_status(NODE), 'shelved')
        self.assertEqual(sb.cycle()['status'], 'inactive')
        self.assertIsNone(sb.current_node())
        self.assertEqual([i['kind'] for i in action['attention']], ['node_shelved'])

    def test_continue_does_not_unblock_the_dependents_of_a_shelved_node(self):
        """shelved 的节点一个依赖都没满足：它在局外等人，等它的活跑不了 —— `continue` 说的是这
        条线接着走，没说这个节点算交付了。"""
        sb = self.pair()
        sb.set_policy('cyc', 'when_shelved', 'continue')
        self.assertEqual(sb.cycle()['policies'], {'when_shelved': 'continue'})

        sb.write_signal('cyc', NODE, 'failed', '这个没做出来')
        action = sb.close_round(NODE)

        self.assertEqual(sb.dag_status(NODE), 'shelved', 'the node was parked')
        self.assertEqual(sb.path()[0]['result'], 'failed', 'the step keeps the ticket word')
        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertEqual(sb.dag_status(NODE2), 'todo', 'the dependent node ran anyway')
        self.assertIn(NODE, [i.get('node_id') for i in action.get('attention', [])])

    def test_continue_walks_on_to_a_node_that_is_still_reachable(self):
        """`continue` 的意思就是「照交付了那样往下走」：图里另有就绪的活时，它真的被挑起来。"""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '另一条支线', mode='auto')
        sb.start_cycle()
        sb.set_policy('cyc', 'when_shelved', 'continue')

        sb.write_signal('cyc', NODE, 'failed', '这个没做出来')
        action = sb.close_round(NODE)

        self.assertEqual(sb.dag_status(NODE), 'shelved')
        self.assertEqual(sb.current_node(), NODE2)
        self.assertIn(action['action'], ('dispatch', 'open_node', 'wait'))
        self.assertEqual(sb.cycle()['status'], 'active', 'the line is still in service')

    def test_the_stop_rule_still_wins_under_continue(self):
        """策略只回答「非交付要不要接着走」，撬不动节点自己的 mode。"""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='checkpoint')
        sb.add_node(NODE2, '另一条支线', mode='auto')
        sb.start_cycle()
        sb.set_policy('cyc', 'when_shelved', 'continue')

        sb.write_signal('cyc', NODE, 'failed', '没做出来')
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
        bad_number = sb.set_policy('cyc', 'test_seconds_limit', 'soon', check=False)
        self.assertNotEqual(bad_number.returncode, 0)
        zero = sb.set_policy('cyc', 'test_seconds_limit', '0', check=False)
        self.assertNotEqual(zero.returncode, 0, 'a line of 0 fails every reading')
        self.assertNotIn('policies', sb.cycle(), 'a refused write still wrote something')

    def test_a_numeric_policy_lands_as_a_number_and_off_clears_it(self):
        """数值型策略（维护线那两条）：正数落盘为数字本身，`off` 把这一条去掉。"""
        sb = self.pair()
        proc = sb.set_policy('cyc', 'test_seconds_limit', '15', cli=True)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sb.cycle()['policies']['test_seconds_limit'], 15)
        self.assertIn('test_seconds_limit=15', proc.stdout)

        cleared = sb.set_policy('cyc', 'test_seconds_limit', 'off', cli=True)

        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertNotIn('test_seconds_limit', sb.cycle()['policies'])
        self.assertNotIn('test_seconds_limit', cleared.stdout)

    def test_set_policy_prints_the_effective_set(self):
        """写一条，报回来的是**生效后**的整份策略（人不用自己去拼默认值）。"""
        sb = self.pair()
        proc = sb.set_policy('cyc', 'when_shelved', 'continue', cli=True)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('when_shelved=continue', proc.stdout)
        self.assertEqual(sb.cycle()['policies'], {'when_shelved': 'continue'})

    def test_current_lists_the_lines_that_matter_and_their_policy(self):
        """`cycle current` 要列的是当下要紧的线：每条 active 的，**以及每条手上有节点的** —— 一条不在
        服务中的线停在半路，正是人必须看见的那种状态。"""
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
    """出局等人的节点（`shelved`）不进就绪集，也不从人头那份清单里消失。

    这个状态是收尾自己写的：一个节点没交付（条子 `failed` / `stopped`）就落到它身上，谁都不许
    开工，等一句 `pf dag revive` 把它放回去。所以两条都要钉住 —— 它不被当成可开工，也不静悄悄
    地从读者眼前掉出去。
    """

    def shelved_graph(self):
        """One parked node and one in play: both shapes this build really writes."""
        sb = self.make()
        sb.add_node(NODE, '出局等人', status='shelved', mode='auto')
        sb.add_node(NODE2, '在局中', status='todo', mode='auto')
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
#  节点没写 mode：兜底一处，四道门同一个词
# ═══════════════════════════════════════════════════════════════════════════════

class TestTheModeWord(Base):
    """`store.normalize_mode` 是这条兜底的唯一拼法。

    读盘回填（`load_dag`）、取值（`nodes.mode_for`）、挑选（`dag_ready_nodes`）、停手判据
    （`planner.stop_criterion`）四道门报出来的词必须一样：两处各持一份默认值时，加一个 mode 词
    就会分头漂。
    """

    def nodes_without_the_key(self):
        """一个记录里根本没有 `mode` 这个键的节点（手改的、老机器写的）。"""
        sb = self.make()
        sb.add_node(NODE, '界面设计')
        dag = sb.dag()
        del dag['nodes'][NODE]['mode']
        sb.write_dag(dag['nodes'])
        return sb

    def test_the_fallback_is_spelled_once_in_the_package(self):
        """源码里 `or DEFAULT_MODE` 只有一处 —— 加一个 mode 词时改的就是它。"""
        sites = []
        for path in sorted((CONDUCT_DIR / 'conduct_pipeline_impl').glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            sites += [f'{path.name}:{node.lineno}' for node in ast.walk(tree)
                      if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
                      and any(isinstance(value, ast.Name) and value.id == 'DEFAULT_MODE'
                              for value in node.values)]

        self.assertEqual(1, len(sites), f'兜底写了好几处：{sites}')
        self.assertTrue(sites[0].startswith('store.py:'), sites)

    def test_a_node_that_leaves_the_key_out_reads_as_auto_everywhere(self):
        """记录里没这个键时，四道门读出来的是同一个词。"""
        sb = self.nodes_without_the_key()
        sb.start_cycle()

        dag = pipeline.store.load_dag(sb.root)
        self.assertEqual('auto', dag['nodes'][NODE]['mode'], '读盘时没有回填')
        self.assertEqual('auto', pipeline.nodes.mode_for(NODE, dag), '取值那道门')
        self.assertEqual('auto', pipeline.dag_ready_nodes(sb.root)[0]['mode'], '就绪集里那一格')

        action = sb.next()
        self.assertEqual('dispatch', action['action'], action)
        self.assertEqual('auto', action['mode'], '派发报出来的那一格')

        must_stop, why = pipeline.stop_criterion(sb.root, sb.cycle(), {'id': NODE})
        self.assertFalse(must_stop, f'停手判据没走到 auto：{why}')


# ═══════════════════════════════════════════════════════════════════════════════
#  prompt 的落点：一处算法，报出来的那一份就是写下来的那一份
# ═══════════════════════════════════════════════════════════════════════════════

class TestThePromptPath(Base):
    """`.pf/meta/launch/…` 在实现包里只有 `prompts.launch_dir` 一处拼法。"""

    def test_the_launch_path_is_built_in_one_file(self):
        """拼 `launch` 这一段的文件只有 `prompts.py`。"""
        sources = {path.name: path.read_text(encoding='utf-8')
                   for path in sorted((CONDUCT_DIR / 'conduct_pipeline_impl').glob('*.py'))}
        sites = sorted(name for name, text in sources.items()
                       if "'launch'" in text or '"launch"' in text)

        self.assertEqual(['prompts.py'], sites)

    def test_the_dispatch_reports_the_path_the_writer_uses(self):
        """派发报出来的 prompt 路径，与写口算出来的那一处逐字相同。"""
        sb = self.make()
        sb.add_node(NODE, '界面设计')
        sb.start_cycle()

        action = sb.next()

        expected = pipeline.prompts.launch_prompt_path(sb.root, 'cyc', NODE)
        self.assertEqual('dispatch', action['action'], action)
        self.assertEqual(Path(action['prompt']), expected)
        self.assertTrue(expected.is_file(), '报出来的那一份就是写下来的那一份')


# ═══════════════════════════════════════════════════════════════════════════════
#  收尾这一趟：整份读图一次
# ═══════════════════════════════════════════════════════════════════════════════

class TestTheCloseReadsTheGraphOnce(Base):
    """收尾入口读一次图，判定按参数下传 —— 几条分支走的是同一份。"""

    def graph_readers(self):
        """把实现包**每一层自己绑定的** `load_dag` 都换成计数的包装，用例退出时还原。

        层是从手上这个入口模块的全局里找的，不写死名单，也不看 `sys.modules`：这个包每被装载一次
        就换一套模块对象（`fresh=True`），`sys.modules` 里那一份未必是这里跑的那一份。每一层拿的
        都是自己 import 时绑定的那个函数对象，所以每一趟整份读图恰好记一次。记的是调用者的函数名
        （不记行号，改一行注释不该判红）。

        这个找法有两处**不响**的盲区，撞上哪一处都是静静地少记一笔：

          * 层内别名绑定：某一层在**自己模块内**把 `load_dag` 绑到另一个名字上（`_read = load_dag`），
            入口模块的全局里就没有这个名字可换，那一层的读不计。
          * 新层不进包命名空间：新加的实现层没有挂进入口模块的全局（例如只被别的层 import），
            它连一次都不会被找到。

        边界是响的：`load_dag` 的调用者今天全在 `conduct_pipeline_impl` 包里（计数包装按栈帧的
        文件路径认调用者），包外多出一个直接调用者时那句 `next(...)` 当场抛 `StopIteration`，
        不会静静地少记一笔。
        """
        callers = []
        real = {}
        layers = [value for value in vars(pipeline).values()
                  if isinstance(value, types.ModuleType) and hasattr(value, 'load_dag')]
        self.assertGreaterEqual(len(layers), 5, f'没找全读图的层：{layers}')
        for layer in layers:
            real[layer] = layer.load_dag

            def counted(project, _real=real[layer], _callers=callers):
                frame = next(f for f in reversed(traceback.extract_stack())
                             if 'conduct_pipeline_impl' in f.filename and f.name != 'counted')
                _callers.append(frame.name)
                return _real(project)

            layer.load_dag = counted
        self.addCleanup(lambda: [setattr(layer, 'load_dag', fn) for layer, fn in real.items()])
        return callers

    def test_a_close_with_nothing_left_reads_the_graph_once(self):
        """手里最后一格收掉、图里没有活：这一趟只读一次图，读它的就是收尾入口。"""
        sb = self.make()
        sb.add_node(NODE, '界面设计')
        sb.start_cycle()
        sb.write_signal('cyc', NODE, 'completed', 'done')

        callers = self.graph_readers()
        action = sb.close_round(NODE)

        self.assertEqual('stop_pipeline', action['action'], action)
        self.assertEqual({'_close_own_node'}, set(callers), f'读图的调用者：{callers}')

    def test_a_close_that_stops_reads_the_graph_once_however_many_branches_it_walks(self):
        """停手这一趟走的分支最多（就绪集、blocked 理由、点名节点的 tag、停手判据里指令那一趟的
        scope 判定、attention 名单），读图的仍然只有收尾入口那一处。"""
        sb = self.make()
        sb.add_node(NODE, '界面设计')
        sb.add_node(NODE2, '出局等人', status='shelved')
        sb.start_cycle(tags=['coding'])
        sb.write_signal('cyc', NODE, 'completed', 'done')

        callers = self.graph_readers()
        action = sb.close_round(NODE, node=NODE2)

        self.assertEqual('stop_pipeline', action['action'], action)
        self.assertTrue(action['attention'], action)
        self.assertEqual({'_close_own_node'}, set(callers), f'读图的调用者：{callers}')

    def test_a_close_with_another_node_still_in_flight_reads_the_graph_once(self):
        """多格线手里还有一格在飞：这一格收掉、补不上第二格 → 停在「手里还有谁」那句上，读图一次。"""
        sb = self.make()
        sb.add_node(NODE, '甲')
        sb.add_node(NODE2, '乙')
        sb.start_cycle(parallel=2)
        sb.write_signal('cyc', NODE, 'completed', 'done')

        callers = self.graph_readers()
        action = sb.close_round(NODE)

        self.assertEqual('stop_pipeline', action['action'], action)
        self.assertIn(NODE2[:8], action['reason'], 'reason 该点名手里还有谁')
        self.assertEqual([NODE2], sb.holding_ids(), '在飞的那一格还握着')
        self.assertEqual({'_close_own_node'}, set(callers), f'读图的调用者：{callers}')

    def test_a_multi_slot_close_that_cannot_fill_reads_the_graph_once(self):
        """多格线手里就一格、图里没有第二格可补：走 blocked 理由那一支，读图一次。"""
        sb = self.make()
        sb.add_node(NODE, '甲')
        sb.start_cycle(parallel=2)
        sb.write_signal('cyc', NODE, 'completed', 'done')

        callers = self.graph_readers()
        action = sb.close_round(NODE)

        self.assertEqual('stop_pipeline', action['action'], action)
        self.assertEqual('图里没有就绪节点了', action['reason'])
        self.assertEqual({'_close_own_node'}, set(callers), f'读图的调用者：{callers}')

    def test_a_close_that_fills_a_slot_reads_the_graph_once(self):
        """补格那一趟：收尾读一次，当场补上来的那份 dispatch 自己再读一次。

        第二次读的是**派发那一格**（`_dispatch_many` 为 node 的标题与 mode 读图），与收尾这一趟
        无关；收尾自己的判定一律用下传的那一份。
        """
        sb = self.make()
        sb.add_node(NODE, '甲')
        sb.add_node(NODE2, '乙')
        sb.add_node(NODE3, '丙')
        sb.start_cycle()
        sb.set_policy('cyc', 'parallel', 2)
        sb.set_current_node(NODE2)
        sb.write_signal('cyc', NODE, 'completed', 'done')

        callers = self.graph_readers()
        action = sb.close_round(NODE)

        self.assertEqual('dispatch', action['action'], action)
        self.assertEqual(sorted(sb.holding_ids()), sorted([NODE2, NODE3]), '腾出来的那一格补上了')
        self.assertEqual({'_close_own_node', '_dispatch_many'}, set(callers),
                         f'读图的调用者：{callers}')

    def test_the_pick_path_reads_the_graph_once_too(self):
        """手里没有节点的那条路（开局挑人）也是一个入口读一次：过滤集挡住了就绪节点时，
        它走 blocked 理由与 attention，读图的仍然只有 `_pick_next` 一处。"""
        sb = self.make()
        sb.add_node(NODE, '写文案', tags=['writing'])
        sb.create_cycle('cyc', tags=['coding'])

        callers = self.graph_readers()
        action = sb.plan()

        self.assertEqual('stop_pipeline', action['action'], action)
        self.assertIn('writing', action['reason'])
        self.assertEqual({'_pick_next'}, set(callers), f'读图的调用者：{callers}')


# ═══════════════════════════════════════════════════════════════════════════════
#  就绪集：读取端不替调用方截断
# ═══════════════════════════════════════════════════════════════════════════════

class TestTheReadySetIsWhole(Base):
    """`ranked_ready_nodes` 不问就给整份就绪集：一次拿几格是线自己的事（`parallel`）。

    这两条直接调读取端，不经过沙箱的规划入口，所以自己套一层 `_dag_hook`：排序由 dag-manage
    自己的 `cmd_list` 回答（同一份实现），省掉的只是一次次进程启动。
    """

    def nine_ready(self):
        """九个互不依赖的就绪节点 —— 比任何「顺手写下的缺省行数」都多。"""
        sb = self.make()
        ids = [f'n{i}-0000-4000-8000-00000000000{i}' for i in range(9)]
        for i, uid in enumerate(ids):
            sb.add_node(uid, f'节点 {i}')
        return sb, ids

    def test_the_reader_hands_over_the_whole_ranking(self):
        """不问 limit 就是整份；要几行由调用方点名。"""
        sb, ids = self.nine_ready()

        with _dag_hook(sb.root):
            whole = pipeline.ranked_ready_nodes(sb.root)
            same = pipeline.ranked_ready_nodes(sb.root, tags=[])
            three = pipeline.ranked_ready_nodes(sb.root, limit=3)

        self.assertEqual(9, len(whole))
        self.assertEqual(9, len(same))
        self.assertEqual(3, len(three))

    def test_a_named_node_beyond_the_head_is_still_picked(self):
        """`--node` 点名的节点排在第几个都认：它就绪、名字又对得上，选出来的就是它。"""
        sb, ids = self.nine_ready()
        tail = ids[-1]

        with _dag_hook(sb.root):
            whole = pipeline.ranked_ready_nodes(sb.root)
            chosen = pipeline.pick_node(sb.root, tail)

        self.assertEqual(tail, whole[-1]['id'], '这条用例要的是排名尾巴上的那个节点')
        self.assertIsNotNone(chosen, '点名的节点在排名尾巴上就读不到了')
        self.assertEqual(tail, chosen['id'])
