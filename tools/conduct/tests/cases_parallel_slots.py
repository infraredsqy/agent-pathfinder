"""一条线的 `parallel` 策略：开局同时握几格、收尾怎么把腾出来的格子补上。

上限写在线自己身上（`policies.parallel`），开局那一次挑人与以后每一次收尾补格取的是同一个数：
一格里派发的是「现在握着的所有节点」，补格只补空位（`parallel` 减还握着的），图里给不出就不补。

两条线之间的那些机械闸（写盘互斥锁、worktree 回主树、跨 cycle 的 pin 守卫、SPAWN.claim）
在 `cases_parallel.py`。

    python conduct/tests/cases_parallel_slots.py       # 这个文件单独跑
    python conduct/tests/test_conduct_pipeline.py      # 跟全套件一起
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import NODE, NODE2, NODE3, Base  # noqa: E402

__all__ = ['TestParallelSlots']


class TestParallelSlots(Base):
    """一条线的 `parallel` 策略：它同时握几个节点，收尾腾出来的格子当场补上。"""

    def two_ready(self, parallel=None):
        """两个互不依赖的就绪节点：`parallel` 决定开局时同时握几个。"""
        sb = self.make()
        sb.add_node(NODE, '甲', mode='auto')
        sb.add_node(NODE2, '乙', mode='auto')
        sb.start_cycle(tags=['coding'])
        if parallel is not None:
            sb.set_policy('cyc', 'parallel', parallel)
        return sb

    def test_the_default_is_one_slot(self):
        """没配 `parallel` 就是 1：抽签按旧样子一次一格，多出来的格子不自己开。"""
        sb = self.two_ready()
        cycle = sb.cycle()
        self.assertEqual(cycle.get('policies', {}).get('parallel', 1), 1)
        self.assertEqual(len(sb.holding_ids()), 1, 'the default must pin exactly one node')

    def test_the_start_pins_the_slots_the_line_states(self):
        """开局就说清这条线握几格：`cycle start --parallel 2` 当场握上两格，一次派发两份 prompt。

        同一个数落在 `policies.parallel` 上（`cycle policy` 写的是那一格），所以开局这一次挑到
        几格、以后每一次收尾补几格取的是同一个数。
        """
        sb = self.make()
        sb.add_node(NODE, '甲', mode='auto')
        sb.add_node(NODE2, '乙', mode='auto')
        sb.start_cycle(tags=['coding'], parallel=2)

        self.assertEqual(sb.cycle()['policies']['parallel'], 2)
        self.assertEqual(sorted(sb.holding_ids()), sorted([NODE, NODE2]), '开局该握上两格')

        action = sb.next()

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(sorted(n['node_id'] for n in action['nodes']), sorted([NODE, NODE2]))
        for item in action['nodes']:
            self.assertTrue(Path(item['prompt']).is_file(), item['prompt'])

    def test_the_start_refuses_a_parallel_that_is_not_a_positive_integer(self):
        """开局给的那个数按 `cycle policy` 的同一个口径判：0 当场被拒，`off` 落回缺省。"""
        sb = self.make()
        sb.add_node(NODE, '甲', mode='auto')
        sb.add_node(NODE2, '乙', mode='auto')

        refused = sb.start_cycle(tags=['coding'], parallel=0, check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('parallel', refused.stderr)
        self.assertEqual([], [c['id'] for c in (sb.state() or {'cycles': []})['cycles']],
                         '被拒的开局不该留下一条线')

        sb.start_cycle(tags=['coding'], parallel='off')
        self.assertEqual(len(sb.holding_ids()), 1, '`off` 落回缺省：一次一格')
        self.assertNotIn('parallel', sb.cycle().get('policies') or {})

    def test_parallel_two_pins_two_and_dispatches_one_action_for_both(self):
        """`parallel=2`：开局把两个就绪节点都握上，一次派发同时开出两份 prompt。"""
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
        """手里两格、关掉一格：填充把下一格就绪的补上 —— 线自己接着跑，不留空位。"""
        sb = self.make()
        sb.add_node(NODE, '甲', mode='auto')
        sb.add_node(NODE2, '乙', mode='auto')
        sb.add_node(NODE3, '丙', mode='auto')
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'parallel', 2)
        sb.set_current_node(NODE2)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done')

        first = sb.close_round(NODE)

        # 关掉一格腾出空位，而图里还有就绪的活：同一次收尾把它补上并派发 —— 手里重新是满的，
        # 派发那一格带的是「现在握着的所有节点」（刚补上的那个在 `nodes` 里）。
        self.assertEqual(first['action'], 'dispatch')
        self.assertEqual(first['nodes'][0]['node_id'], NODE2,
                         'the dispatch names every node in hand, not only the fresh one')
        self.assertEqual(sorted(sb.holding_ids()), sorted([NODE2, NODE3]),
                         'the slot the close freed must be refilled')

    def test_a_close_fills_every_free_slot(self):
        """手里一格、关掉它时图里有两个就绪：三个空位该补两个 —— 两个都得落盘。

        空位是 `parallel` 减手里还握着的；名单取到几个就 pin 几个。只 pin 名单头一个的话，
        第二个格会静静地空着：动作里的 `nodes` 报了它，手里的手却没有它。
        """
        sb = self.make()
        for uid, name in ((NODE, '甲'), (NODE2, '乙'), (NODE3, '丙')):
            sb.add_node(uid, name, mode='auto')
        sb.start_cycle(tags=['coding'])           # 开局按缺省 parallel=1 只钉一个
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
        """手里满三格、关掉一格：只补腾出来的那一格，线不会越过 `parallel`。

        补格不是无脑补三个 —— 空位跟手里还在跑的格数挂钩（`parallel` 减还握着的），
        图里给得出几个就补几个，给不出就不补。
        """
        sb = self.make()
        four = (NODE, NODE2, NODE3, 'dddddddd-4444-4444-8444-dddddddddddd')
        for i, uid in enumerate(four):
            sb.add_node(uid, f'节点 {i}', mode='auto')
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'parallel', 3)
        closing = sb.current_node()
        rest = [u for u in four if u != closing]
        sb.set_current_node(rest[0])
        sb.set_current_node(rest[1])              # 手里三格（满）
        sb.write_signal('cyc', closing, 'completed', 'done')

        action = sb.close_round(closing)

        self.assertEqual(len(sb.holding_ids()), 3, 'the hand stays at the cap')
        self.assertEqual(sorted(sb.holding_ids()), sorted(rest),
                         'only the freed slot is filled, and it is filled')
        self.assertEqual(sorted(n['node_id'] for n in action['nodes']), sorted(rest))

    def test_a_close_with_nothing_to_fill_keeps_the_line_in_service(self):
        """手里两格、图里没有能补的：收掉这一格，线留在服务中 —— 那一格的收尾自己接着跑。

        补不上有两种：图里真没活了（线停手等人），和线里还有别的节点在飞（这条线没停，接力棒在
        那些节点自己的收尾上）。后者也 park 的话，它们的收尾只做登记，腾出来的活再没人接。
        """
        sb = self.make()
        sb.add_node(NODE, '甲', mode='auto')
        sb.add_node(NODE2, '乙', mode='auto')
        sb.add_node(NODE3, '丙', mode='auto', deps=[NODE2])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'parallel', 2)
        sb.set_current_node(NODE)
        sb.set_current_node(NODE2)
        sb.write_signal('cyc', NODE, 'completed', 'done')

        first = sb.close_round(NODE)

        self.assertEqual(first['action'], 'stop_pipeline')
        self.assertEqual(sb.cycle()['status'], 'active', '手里还有别的节点在飞，这一格不停这条线')
        self.assertEqual(sb.holding_ids(), [NODE2])
        self.assertIn(NODE2[:8], first['reason'], 'reason 点名手里还有谁')

        sb.write_signal('cyc', NODE2, 'completed', 'done')
        second = sb.close_round(NODE2)

        self.assertEqual(second['action'], 'dispatch')
        self.assertEqual(sb.holding_ids(), [NODE3], '那一格的收尾把丙补上并派发')

    def stub(self, sb):
        """The stand-in spawner these cases aim the real spawn call at (never a host one)."""
        return sb.fake_spawner('ok-spawner.ps1')

    def test_a_started_node_is_never_handed_to_the_spawner_twice(self):
        """已经开出去的那一格不再交给 spawner：收尾重规划出来的整份名单里带着它，跳过它。

        `started` 是成功派发留下的记号（prompt 被会话拿走、claim 撤掉、目录留着当落点）。没有
        它，一次补格就会把在跑的兄弟节点再开一个会话 —— 同一个节点、同一个工作树，收不回来。
        """
        sb = self.make()
        for uid, name in ((NODE, '甲'), (NODE2, '乙'), (NODE3, '丙')):
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
            self.assertTrue((launch / node / 'started').is_file(), f'{node[:8]}: 记号没落下')
            self.assertFalse((launch / node / 'prompt.txt').exists(), f'{node[:8]}: prompt 该被拿走')

        sb.write_signal('cyc', NODE, 'completed', 'done')
        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual([n['node_id'] for n in action['nodes']], [NODE3],
                         '派发名单里只有在等开工的那一格')
        self.assertFalse((launch / NODE2 / 'prompt.txt').exists(),
                         '在跑的乙，prompt 不该被补回来 —— 补回来就等于说它还没开')

        action, _ = sb.spawn_next('--cycle', 'cyc', spawner=self.stub(sb), dry_run=False)
        self.assertEqual([n['node_id'] for n in action['nodes']], [NODE3], '在跑的乙不进名单')

    def test_with_every_session_in_hand_running_the_answer_is_a_wait(self):
        """手里每一格都开出去了：`plan` 答 `wait`，派发则响亮拒掉（并把重开那一格的路念出来）。"""
        sb = self.make()
        sb.add_node(NODE, '甲', mode='auto')
        sb.start_cycle(tags=['coding'])
        sb.next()
        self.assertEqual(
            sb.spawn_next('--cycle', 'cyc', spawner=self.stub(sb), dry_run=False)[1].returncode, 0)

        action = sb.plan()

        self.assertEqual(action['action'], 'wait', '会话都在跑，没有可派的动作')
        self.assertIn('条子', action['note'])

        proc = sb.dispatch(spawner=self.stub(sb), dry_run=False, check=False)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('cycle retry', proc.stderr, '拒绝里要给重开那一格的路')

    def test_the_limit_is_the_line_own_policy(self):
        """上限写在线自己身上：`cycle policy cyc parallel 2` 生效，0 与负数被拒。"""
        sb = self.two_ready()
        sb.set_policy('cyc', 'parallel', 2)
        self.assertEqual(sb.cycle()['policies']['parallel'], 2)
        refused = sb.set_policy('cyc', 'parallel', 0, check=False)
        self.assertNotEqual(refused.returncode, 0)

    def test_the_dispatch_sweep_opens_one_session_per_node_it_holds(self):
        """`dispatch <cycle>` 是一条扫射：手里每个节点各开一个会话，各自吃自己的 prompt。"""
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