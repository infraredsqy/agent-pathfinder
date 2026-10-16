"""The node: what one cycle step IS, and the closing call that ends it.

Everything here is about the surviving shape, in the terms the model is stated in: `state.json`
holds the whole cycle (there is no per-round file and no second pointer), one node's work is done
when its terminal signal is on disk, the DAG's node status is the single source of truth for "did
it succeed", and the only thing that ends a step is the worker's own `next --after <node>`.

Deleted concepts have no cases here, on purpose: a stage index, a pointer move, `advance`,
`catch-up`, `rewind`, `open-round` / `close-round`, `migrate` / `migrate-v3`, legacy layouts and
the artifact-key validation are all gone, so a test for any of them would be testing a sentence
that no longer exists.
"""

from __future__ import annotations

import ast
import importlib.util as importlib_util
import json
import sys
import traceback
import types
from pathlib import Path

from cases_base import NODE, NODE2, NODE3, Base, added, pipeline, skill_of
from harness import (Closer, DAG_MANAGE, Driver, STATE_MANAGE, WorkerSession, _dag_hook,
                     finish_node, run_dag, stop_after)
from harness_env import CONDUCT_DIR, _fresh_module


def _state_cycles_module():
    """`conduct_state` 是带相对导入的包，不能按单文件加载：照 state-manage.py 自己的引导，
    以包形式按路径整包装进来，取装好的 cycles 子模块。装前先清同名旧条目，本文件里的
    两次加载互不串。
    """
    name = 'conduct_state_for_vocab'
    for stale in [n for n in sys.modules if n == name or n.startswith(name + '.')]:
        del sys.modules[stale]
    pkg_dir = STATE_MANAGE.parent.parent / 'conduct_state'
    spec = importlib_util.spec_from_file_location(
        name, pkg_dir / '__init__.py', submodule_search_locations=[str(pkg_dir)])
    mod = importlib_util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod.cycles


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
        """停手只有一句话可写：`inactive`。以前这里要在 paused 和 completed 之间按「图上还剩没剩
        活」选一个，而那问的是当时的图 —— 图会长大，停下来的线该不该接着跑是人说了算的事。"""
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
    收尾后照 `auto` 接着走。以前还有一层「这条线是什么 launch mode」的天花板，那个天花板已经
    删掉了：同一个判断只该有一个出处。
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
                         '停手照旧把线转成 inactive；放回服务的那条命令在 reason 里')
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
        """shelved 的节点一个依赖都没满足：它在局外等人，等它的活照旧跑不了 —— `continue` 说的是这
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


class TestStartCycle(Base):
    def test_start_cycle_pins_the_node_the_rule_picks(self):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()

        self.assertEqual(sb.current_node(), NODE)
        self.assertEqual(sb.cycle()['status'], 'active')

    def test_start_cycle_refuses_an_empty_graph(self):
        sb = self.make()
        proc = sb.start_cycle(check=False)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('ready', proc.stderr)
        self.assertEqual((sb.state() or {'cycles': []})['cycles'], [])

    def test_a_cycle_with_no_node_in_hand_takes_the_top_ready_row(self):
        """多个就绪不停线：排名平时在每次收尾独自定人（`pick_node` 拿第一行），线的第一棒拿同一行。
        要点名的入口在人手上（`--node`、`cycle hold`），不在这拍反问。"""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '另一条支线', mode='auto')
        sb.create_cycle(cycle_id='cyc')

        action = sb.plan()
        self.assertEqual(action['action'], 'open_node')
        self.assertEqual(action['node_id'], NODE)


# ── 选择层与 tag 过滤（原 cases_tags / cases_weight 并来）─────────────────────

class TestNodeTags(Base):
    """节点 tag + cycle 分流：一条 cycle 在哪些节点里挑人。

    判据只有一条 —— 节点一个 tag 都没有，或者它的 tag 与 cycle 的过滤集相交，它就在这条线的
    选材范围里。过滤只落在「挑下一个节点」这一步：正在跑的节点照跑完，DAG 的依赖关系一个字
    不改（挡住只影响挑谁，不动状态、不标 failed、不动依赖边）。本线挑不到、别的线有人就绪时
    停下，把「另一条线要不要开工」交回人手上。

    没有过滤集时（cycle 不带 tags）这些用例一个都不该改变既有的挑选结果 —— 这一条由整套测试
    守，`test_an_untagged_cycle_picks_what_it_always_picked` 再单独钉一次。那个形状如今只剩存量
    记录这一个入口：`cycle start` / `cycle create` 对新调用一律要 `--tags`。
    """

    K = 'dddddddd-4444-4444-8444-dddddddddddd'          # coding
    W = 'eeeeeeee-5555-4555-8555-eeeeeeeeeeee'          # writing
    P = 'ffffffff-6666-4666-8666-ffffffffffff'          # 没打 tag

    def code_and_words(self, cli=False):
        """一条 coding 的活 + 一条 writing 的活，都是 auto（否则停手判据先响，轮不到 tag）。

        `W` 依赖 `K`，所以图里最要紧的是 `K`：一条线先挑谁，要看 DAG 的排序，这里让它确定下来。
        """
        sb = self.make(cli=cli)
        sb.add_node(self.K, '写代码', mode='auto', tags=['coding'])
        sb.add_node(self.W, '写文案', mode='auto', tags=['writing'], deps=[self.K])
        return sb

    # ── 判据表第一行：过滤集里有就绪节点 → 照旧按 DAG 的排序挑第一名 ──────────

    def test_the_filter_set_only_drops_rows_from_the_dag_ranking(self):
        """过滤集只删行，排序还是 `dag-manage` 那一处实现。

        图里排第一的节点带 `writing`，本条的过滤集是 `coding` —— 被删的是**它**，被挑的是过滤
        集里排第一的那个。如果 conduct_pipeline 自己又写了一份优先级规则，这条断言就会挑到别的
        节点上（排序只准有一份实现，这是 `ranked_ready_nodes` 一直守着的规矩）。
        """
        sb = self.make()
        sb.add_node(self.W, '写文案', mode='auto', tags=['writing'])
        sb.add_node('aaaa1111-6666-4666-8666-ffffffffffff', '文案的后续', mode='auto',
                    deps=[self.W])                      # 让 W 成为图里最要紧的那个（有人等它）
        sb.add_node(self.K, '写代码', mode='auto', tags=['coding'])
        sb.add_node(self.P, '没打 tag 的活', mode='auto')

        sb.start_cycle(tags=['coding'])
        self.assertEqual(sb.current_node(), self.K)
        self.assertEqual(sb.cycle()['tags'], ['coding'])

    def test_an_untagged_node_stays_in_every_filter_set(self):
        """没有 tag 的节点在任何过滤集下都留在选材范围里。

        保守方向：漏挡只多跑一个节点，误挡会让一条线的活永远没人做。所以本条过滤集是 `art`
        （图里没有一个节点带 `art`），挑到的仍然是没有 tag 的那个。
        """
        sb = self.make()
        sb.add_node(self.K, '写代码', mode='auto', tags=['coding'])
        plain = sb.add_node(self.P, '没打 tag 的活', mode='auto')
        sb.start_cycle(tags=['art'])

        self.assertEqual(sb.current_node(), plain)
        # `exclude` is the node this step's own cycle is holding: it belongs to the caller, and
        # every other row is still judged by the filter set alone.
        self.assertEqual([n['id'] for n in pipeline.ranked_ready_nodes(sb.root, tags=['art'],
                                                                   exclude=plain)],
                         [])
        self.assertEqual([n['id'] for n in pipeline.tag_blocked_ready_nodes(sb.root, ['art'])],
                         [self.K])

    # ── 判据表第二行：本线没有、别的线有 → 停，并把话说全 ────────────────────

    def test_a_cross_line_stop_names_the_nodes_and_the_command(self):
        """跨线就绪时停下，reason 点名：哪些节点就绪、被哪些 tag 挡住、另开一条线的整条命令。

        这句 reason 会被原样念给用户听，所以它要能一个人读完就知道下一步敲什么。停在这里的理由
        是把选择权交回人手上：一条线的活做完了，另一条线要不要开工、什么时候开工，由他说了算。
        """
        sb = self.code_and_words()
        sb.start_cycle(tags=['coding'])
        node = sb.current_node()
        self.assertEqual(node, self.K)

        waiting = finish_node(sb)
        closed = sb.close_round(waiting['node_id'])

        self.assertEqual(closed['action'], 'stop_pipeline')
        reason = closed['reason']
        self.assertIn('写文案', reason)                     # 哪个节点就绪
        self.assertIn(self.W, reason)
        self.assertIn('writing', reason)                    # 被哪个 tag 挡住
        self.assertIn('cycle start', reason)                # 另一条线怎么开
        self.assertIn('--tags writing', reason)
        self.assertNotIn('--launch-mode', reason, 'the deleted lever is back in the suggested command')
        self.assertNotIn('图里没有 ready 节点', reason)

        # 被挡住 ≠ 依赖没满足：状态、依赖边、一个 shelved 都没动。
        self.assertEqual(sb.dag_status(self.W), 'todo')
        self.assertEqual(sb.node(self.W)['dependencies'], [self.K])
        # 停手只有一句话：inactive。本线的活干完了，另一条线还有人在等开工 —— 而「还有活」这件事
        # 不写在线的状态里（图会长大，一条停下来的线该不该接着跑是人说了算），所以这一停同样是
        # inactive，而接那条线的路照 reason 里给的命令真敲一遍就该成。
        self.assertEqual(sb.cycle()['status'], 'inactive')
        # 接那条线的路，照 reason 里给的命令真敲一遍：不加 --force 就该成。`check=True` 意味着
        # 被拒会当场抛出来，所以下面两行断言成立就说明这一发过了。
        sb.start_cycle('other', title='另一条线', tags=['writing'])
        self.assertEqual(sb.cycle('other')['status'], 'active')
        self.assertEqual(sb.cycle('other')['tags'], ['writing'])
        self.assertEqual(waiting['node_id'], node)

    def test_the_second_pick_path_says_the_same_thing(self):
        """过滤只删行：收尾之后，同一个状态从两个入口读出来是同一套过滤的结果。

        本线从头到尾都只认 `coding`（那个没打 tag 的活也在范围里，所以它先被挑中）；收尾之后
        盘上还剩一个打了 `coding` 的节点 —— 它在范围里、也还就绪，这就是第二个入口要挑的那个。
        """
        sb = self.make()
        sb.add_node(self.K, '写代码', mode='auto', tags=['coding'])
        sb.add_node(self.P, '没打 tag 的活', mode='auto')
        for i in range(3):
            sb.add_node('aaaa000%d-6666-4666-8666-ffffffffffff' % i, f'等它的第 {i} 个',
                        mode='auto', deps=[self.P])
        pinned = sb.start_cycle(tags=['coding'], check=False)

        self.assertEqual(pinned.returncode, 0, pinned.stderr)
        self.assertEqual(sb.current_node(), self.P, '图里最要紧的是那个没打 tag 的活')
        sb.write_signal('cyc', self.P, 'completed', 'done')
        sb.close_round(self.P)

        # 第二个入口读的是同一张图、同一套过滤
        self.assertEqual(sb.current_node(), self.K, '本线里就绪的正是那个 coding 节点')
        self.assertEqual([n['id'] for n in pipeline.tag_blocked_ready_nodes(sb.root, ['coding'])], [])
        self.assertEqual(sb.dag_status(self.P), 'completed')
        self.assertEqual(sb.dag_status(self.K), 'todo', '被挑中不等于被跑过')

    def test_nothing_ready_anywhere_keeps_the_old_sentence(self):
        """没有过滤集挡住谁的时候，停下来说的还是「图里没有就绪节点」那句话。"""
        sb = self.make()
        sb.add_node(self.K, '写代码', mode='auto')
        sb.create_cycle(cycle_id='cyc')
        sb.set_current_node(self.K)
        sb.write_signal('cyc', self.K, 'completed', 'done')
        action = sb.close_round(self.K)

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertIn('就绪节点', action['reason'])
        self.assertNotIn('tag', action['reason'])
        self.assertEqual(sb.cycle()['status'], 'inactive')

    def test_an_untagged_cycle_picks_what_it_always_picked(self):
        """空过滤集的 cycle 与这个功能没出现时逐字节相同：整张图都是范围。

        这条分支如今只剩一个入口 —— **存量记录**：`cycle start` / `cycle create` 对新调用一律要
        `--tags`，所以这里把不带 tags 的那条线按老记录的形状写到盘上，再照常驱动它。空集不是死代码：
        机器上 pull 下来的老 cycle 长这样，而它对挑选结果的影响必须仍是零。
        """
        sb = self.make()
        sb.add_node(self.W, '写文案', mode='auto', tags=['writing'])
        sb.add_node(self.K, '写代码', mode='auto', tags=['coding'])
        sb.start_cycle(tags=['coding'])
        # 老记录那份形状：这条线不带 tags（那个键根本不出现，不是空 list），此刻钉在 W 上。
        # 老记录里钉着谁就是谁，与它身上有没有 tag 无关。
        state = sb.state()
        state['cycles'][0].pop('tags', None)
        state['cycles'][0]['holding'] = [{'id': self.W, 'since': '2026-10-01T00:00:00Z'}]
        sb.rewrite_state(state)

        self.assertNotIn('tags', sb.cycle(), 'the fixture wrote the key back')
        self.assertEqual(sb.current_node(), self.W)
        self.assertEqual([n['id'] for n in pipeline.tag_blocked_ready_nodes(sb.root, None)], [])
        # With a filter set both rows would still be in range here (neither is in the other's set),
        # and with none, the whole graph is: the only row out of reach is the one already in hand.
        self.assertEqual({n['id'] for n in pipeline.ranked_ready_nodes(sb.root, tags=None)},
                         {self.K})

    def test_a_running_node_is_never_interrupted_by_the_filter_set(self):
        """过滤只在挑选那一步读：本条已经钉在手上的节点照跑完，与它身上的 tag 无关。"""
        sb = self.make()
        sb.add_node(self.K, '写代码', mode='auto', tags=['coding'])
        sb.start_cycle(tags=['coding'])
        self.assertEqual(sb.current_node(), self.K)

        # 过滤集被改宽/改窄都不该动已经在手上的那个节点
        sb.rewrite_cycle(tags=['writing'])
        self.assertEqual(sb.current_node(), self.K)
        self.assertEqual(sb.plan()['node_id'], self.K)

    def test_a_named_node_outside_the_filter_set_runs_and_says_so(self):
        """`--node` 是机主亲手点的名，过滤集管不到它 —— 但它得说出来，别让人以为过滤集没生效。"""
        sb = self.code_and_words()
        sb.start_cycle(tags=['coding'])
        sb.write_signal('cyc', self.K, 'completed', 'done')
        action = sb.close_round(self.K, node=self.W)

        self.assertEqual(sb.current_node(), self.W)
        self.assertIn('过滤集', action.get('note', ''),
                      f'a hand-named node outside the filter must be said out loud: {action}')

    def test_a_named_node_that_is_not_ready_is_refused(self):
        """点名一个根本跑不了的节点：停下，并把点名的 id 写进 reason，而不是悄悄换一个节点跑。"""
        sb = self.make()
        sb.add_node(self.K, '写代码', mode='auto', tags=['coding'])
        absent = 'absent-9999-4999-8999-999999999999'
        sb.start_cycle(tags=['coding'])
        sb.write_signal('cyc', self.K, 'completed', 'done')
        stopped = sb.close_round(self.K, node=absent)

        self.assertEqual(stopped['action'], 'stop_pipeline')
        self.assertIn(absent, stopped['reason'])
        self.assertIsNone(sb.current_node())

    def test_a_named_node_outside_the_filter_set_is_reported_as_such(self):
        """点名的节点确实就绪、只是不在过滤集里：照跑，但把「它不在范围里」说出来。"""
        sb = self.code_and_words()
        sb.start_cycle(tags=['coding'])
        sb.write_signal('cyc', self.K, 'completed', 'done')
        action = sb.close_round(self.K, node=self.W)

        self.assertEqual(sb.current_node(), self.W)
        self.assertIn('过滤集', action.get('note', ''),
                      f'a hand-named node outside the filter must be said out loud: {action}')

    def test_the_cycle_keeps_its_filter_set_for_every_later_step(self):
        """过滤集属于**这条线**，跟着它走到最后一步：后面每一格都照它挑。"""
        sb = self.make()
        a = sb.add_node(self.K, '写代码 A', mode='auto', tags=['coding'])
        b = sb.add_node('aaaa2222-6666-4666-8666-ffffffffffff', '写代码 B', mode='auto',
                        tags=['coding'])
        sb.add_node(self.W, '写文案', mode='auto', tags=['writing'])
        sb.start_cycle(tags=['coding'])
        terminal, driver = self.run_to_stop(sb)

        picked = [node for node, _ in driver.dispatched]
        self.assertEqual(picked, [a, b])
        self.assertEqual(terminal['action'], 'stop_pipeline')
        self.assertIn(self.W, terminal['reason'])
        self.assertEqual(sb.cycle()['tags'], ['coding'])

    def test_start_cycle_tags_reach_the_pick_and_the_record(self):
        """`--tags` 必须传进挑选那一步，也留在 cycle 记录里。

        挑选发生在 cycle 建起来之前，所以过滤集只落在记录上就太晚了：第一步就会挑到别的线的
        节点，而后面每一格都继承一条「头一个节点来路不明」的线。
        """
        sb = self.make(cli=True)
        sb.add_node(self.W, '写文案', mode='auto', tags=['writing'])
        sb.add_node(self.K, '写代码', mode='auto', tags=['coding'])

        sb.start_cycle(tags=['coding'])

        self.assertEqual(sb.current_node(), self.K)
        self.assertEqual(sb.cycle()['tags'], ['coding'])

    def test_start_cycle_refuses_when_the_filter_set_matches_nothing(self):
        """过滤集里一个就绪节点都没有时，拒绝要说清「图里有活，只是不归这条线管」。"""
        sb = self.make(cli=True)
        sb.add_node(self.W, '写文案', mode='auto', tags=['writing'])
        sb.add_node(self.K, '写代码', mode='auto', tags=['coding'])

        proc = sb._tool('cycle', 'start', 'img', '--tags', 'art', check=False, cli=True)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('art', proc.stderr)
        self.assertIn(self.W, proc.stderr)
        self.assertIn(self.K, proc.stderr)
        self.assertEqual((sb.state() or {'cycles': []})['cycles'], [])

    def test_create_cycle_keeps_a_lone_tag_whole(self):
        """`cycle create --tags art` 记下的是 `['art']`。

        字符串按字符拆开会得到 `['a','r','t']`：过滤集里于是躺着 `a`、`r`、`t` 三个词，图上没有
        任何节点对得上，而记录里看不出来 —— 这条线挑不到任何东西，原因却写在看不见的地方。
        两个记录入口（`cycle create` 与 `cycle start`）都要落到同一份 list 上。
        """
        sb = self.make()
        sb.add_node(self.W, '写文案', mode='auto', tags=['writing'])

        sb._tool('cycle', 'create', 'img', '--tags', 'art', '--force', cli=True)

        self.assertEqual(sb.cycle('img')['tags'], ['art'])

    # ── 图上给节点打 tag：dag-manage 那一半 ─────────────────────────────────

    def test_dag_manage_writes_reads_and_clears_node_tags(self):
        """`add --tags` / `update --tags` / `update --clear-tags` / `list --tags`，全部走真 CLI。

        词表外的 tag 警告一行就收下 —— 词表摆在人面前给人看，engine 照单全收。
        """
        sb = self.make()

        def add(*args):
            return run_dag(sb.root, 'add', *args).stdout.splitlines()[0].split(': ')[1].strip()

        code = add('写代码', '--tags', 'coding')
        words = add('写文案', '--tags', 'writing')
        plain = add('没打 tag 的活')

        self.assertEqual(sb.node(code)['tags'], ['coding'])
        listed = {n['id'] for n in json.loads(run_dag(sb.root, 'list', '--tags', 'coding').stdout)}
        self.assertEqual(listed, {code, plain})            # 没 tag 的那个处处可见

        unknown = run_dag(sb.root, 'list', '--tags', 'misc')
        self.assertIn('不在词表', unknown.stderr)           # 警告一行，走 stderr
        self.assertEqual({n['id'] for n in json.loads(unknown.stdout)}, {plain})

        # 词表外的 tag 照收：真冒出第四类工作时，写进去就能用。
        fourth = run_dag(sb.root, 'add', '第四类活', '--tags', 'misc')
        self.assertIn('不在词表', fourth.stderr)
        fourth_id = fourth.stdout.splitlines()[0].split(': ')[1].strip()
        self.assertEqual(sb.node(fourth_id)['tags'], ['misc'])
        self.assertIn(fourth_id,
                      {n['id'] for n in json.loads(run_dag(sb.root, 'list', '--tags',
                                                           'misc').stdout)})

        run_dag(sb.root, 'update', words, '--tags', 'art')
        self.assertEqual(sb.node(words)['tags'], ['art'])
        run_dag(sb.root, 'update', words, '--clear-tags')
        self.assertNotIn('tags', sb.node(words))
        after = json.loads(run_dag(sb.root, 'list', '--tags', 'art').stdout)
        self.assertEqual({n['id'] for n in after}, {plain, words})

    def test_a_tag_filter_never_touches_the_graph(self):
        """过滤是「挑谁」的事：`list --tags` 不改状态、不改依赖，也不给节点判死。

        挡住一个节点 = 这一格不挑它。它照旧 `todo`、照旧 ready、依赖边原样。
        """
        sb = self.make()
        run_dag(sb.root, 'add', '写代码', '--tags', 'coding')
        run_dag(sb.root, 'add', '写文案', '--tags', 'writing')
        before = sb.dag()
        run_dag(sb.root, 'list', '--tags', 'coding', '--ready', '-n', '0')
        self.assertEqual(sb.dag(), before)

    def test_both_tools_spell_the_tag_vocabulary_the_same_way(self):
        """词表是一份约定，摆在 dag-manage（管节点）和 state-manage（管 cycle 的范围）两处。

        两边分头长出自己的词，就会出现「节点写 `coding`、过滤集写 `code`」这种图上看不出异常的
        局面 —— 一条线的活静悄悄地永远挑不到。所以这三个词由这一条用例比对：两个
        工具各自装进来的常量按值比对，不锚在源码的字面写法上（重命名、换行都不该误报）。
        """
        dag_mod = _fresh_module('dag_manage_for_vocab', DAG_MANAGE)
        cycles_mod = _state_cycles_module()

        self.assertEqual(dag_mod.VALID_TAGS, ('writing', 'coding', 'art'))
        self.assertEqual(dag_mod.VALID_TAGS, cycles_mod.VALID_TAGS)

    def test_the_borrowed_cycle_policy_tables_read_the_same_by_value(self):
        """impl 借的是 `conduct_state/cycles.py` 那一份正本，两个包各自装载同一个文件。

        这里钉的是那条边界：两份读数**按值相等**（`==`）。两个包各按自己的名字装载，拿到的是两个
        模块对象，所以跨包 `is` 不成立，谁也不该拿 `is` 去比这两份；要改这张表，改的是
        `conduct_state/cycles.py` 那个文件本身。
        """
        cycles_mod = _state_cycles_module()

        self.assertEqual(pipeline.POLICY_DEFAULTS, cycles_mod.POLICY_DEFAULTS)
        self.assertEqual(pipeline.POLICY_VALUES, cycles_mod.POLICY_VALUES)
        self.assertEqual(pipeline.POLICY_NUMBERS, cycles_mod.POLICY_NUMBERS)
        self.assertEqual(pipeline.POLICY_COUNTS, cycles_mod.POLICY_COUNTS)
        self.assertEqual(pipeline.POLICY_WORDS, cycles_mod.POLICY_WORDS)


# ═══════════════════════════════════════════════════════════════════════════════
#  Cycle selection
# ═══════════════════════════════════════════════════════════════════════════════

class TestCycleSelection(Base):
    def test_no_cycle_does_not_invent_one(self):
        sb = self.make()
        action = sb.plan()
        self.assertEqual(action['action'], 'no_cycle')
        self.assertFalse(action.get('commands'))
        self.assertFalse(sb.next().get('applied'))
        state = sb.state() or {'cycles': []}      # nothing was written, not even an empty state
        self.assertEqual(state['cycles'], [])

    def test_unknown_cycle_id_is_not_a_new_cycle(self):
        sb = self.make()
        sb.create_cycle('one')
        action = sb.plan_for('nope')
        self.assertEqual(action['action'], 'ask_user')
        self.assertEqual(action['requested'], 'nope')

    def test_two_active_cycles_asks(self):
        """The planner asks about two active cycles -- a state `cycle create` refuses to create
        without `--force`. Forged with `--force`, because the planner still has to answer for a
        machine that already has two, and `ask_user` is the answer: an unattended run cannot pick."""
        sb = self.make()
        sb.create_cycle('one')
        sb.create_cycle('two', force=True)
        action = sb.plan()
        self.assertEqual(action['action'], 'ask_user')
        self.assertEqual(sorted(action['candidates']), ['one', 'two'])

    def test_a_line_out_of_service_is_asked_about_not_silently_restarted(self):
        """Taking a line out of service is a human's decision to stop driving it; the planner hands
        back the one command that puts it back in, and drives nothing."""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.set_cycle_status('cyc', 'inactive')
        self.assertEqual(sb.cycle()['status'], 'inactive')

        action = sb.plan_for('cyc')
        self.assertEqual(action['action'], 'ask_user')
        self.assertIn('not active', action['question'])
        self.assertIn('cycle status cyc active', action['hint'])


class TestNodeWeight(Base):
    """`dag-manage --weight` 那一半，以及它落到调用行上的那一格。"""

    # ── 图上那个键 ───────────────────────────────────────────────────────────

    def test_only_light_lands_the_key(self):
        """`add --weight light` 落键；不写、或显式写 standard，记录里就是今天这份形状。

        存储形状本身就是兼容策略：老图没有这个键、standard 节点没有这个键，两者读作同一件事。
        """
        sb = self.make()

        light = added(run_dag(sb.root, 'add', '轻活', '--weight', 'light'))
        plain = added(run_dag(sb.root, 'add', '标准活'))
        stated = added(run_dag(sb.root, 'add', '显式标准', '--weight', 'standard'))

        self.assertEqual(sb.node(light)['weight'], 'light')
        self.assertNotIn('weight', sb.node(plain))
        self.assertNotIn('weight', sb.node(stated))

    def test_update_moves_the_key_both_ways(self):
        """`update --weight` 两档都管：写 light 落键，写 standard 让这个键从记录里消失。"""
        sb = self.make()
        node = added(run_dag(sb.root, 'add', '活'))

        run_dag(sb.root, 'update', node, '--weight', 'light')
        self.assertEqual(sb.node(node)['weight'], 'light')

        run_dag(sb.root, 'update', node, '--weight', 'standard')
        self.assertNotIn('weight', sb.node(node))

    def test_list_and_info_echo_it_only_for_light(self):
        """回显同 tags：没写过这个键的节点，`list` / `info` 的输出与今天逐字节相同。"""
        sb = self.make()
        light = added(run_dag(sb.root, 'add', '轻活', '--weight', 'light'))
        plain = added(run_dag(sb.root, 'add', '标准活'))

        listed = {n['id']: n for n in json.loads(run_dag(sb.root, 'list').stdout)}
        self.assertEqual(listed[light]['weight'], 'light')
        self.assertNotIn('weight', listed[plain])

        self.assertEqual(json.loads(run_dag(sb.root, 'info', light).stdout)['weight'], 'light')
        self.assertNotIn('weight', json.loads(run_dag(sb.root, 'info', plain).stdout))

    def test_the_word_outside_the_vocabulary_is_refused_at_the_command_line(self):
        """词表外的词由 argparse 挡下：两档是个闭集，写错了当场报，别落进记录等下一次读。"""
        sb = self.make()
        refused = run_dag(sb.root, 'add', '活', '--weight', 'heavy', check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('invalid choice', refused.stderr)

    def test_an_unknown_word_on_disk_reads_as_standard(self):
        """记录里写着别的词（手改的、以后新增的、老机器不认识的）→ 读的人按 standard 走。

        读不准就往保守那一边倒：多派一个人干活，别让 conductor 因为一个错字亲自上阵。
        读发生在会话那一侧（`info` 的输出 + skill 里的规则），所以这里只核 `info` 不回显它。
        """
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto', weight='misc')

        self.assertNotIn('weight', json.loads(run_dag(sb.root, 'info', NODE).stdout))

    # ── 它落到调用行上的那一格 ───────────────────────────────────────────────

    def invocation_line(self, sb) -> str:
        """派发这个节点时 prompt 的第一行 —— 运行时的判据就长在这一行上。"""
        sb.start_cycle()
        action = sb.next()
        self.assertEqual('dispatch', action['action'], action)
        return action['prompt_text'].splitlines()[0]

    def test_the_invocation_line_carries_no_weight_token(self):
        """调用行只有调用本身与 `[pipeline]`：重量与 mode 都是节点自己的属性，会话读 `info`。"""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto', weight='light')

        self.assertEqual(self.invocation_line(sb),
                         f'/pf-conduct {NODE} [pipeline]')


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
        就换一套模块对象（`fresh=True`），`sys.modules` 里那一份未必是这里跑的那一份。漏掉一层，
        那一层的读就数不着 —— 指令那一趟的 scope 判定（`instruct._scope_facts`）正是这样被漏计过
        一次。每一层拿的都是自己 import 时绑定的那个函数对象，所以每一趟整份读图恰好记一次。记的
        是调用者的函数名（不记行号，改一行注释不该判红）。
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

    这两条直接调读取端，不经过沙箱的规划入口，所以自己套一层 `_dag_hook`：排序照旧由 dag-manage
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
