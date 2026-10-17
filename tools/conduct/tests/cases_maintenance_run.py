"""Running the maintenance line: the graph edit through the real processes, the walk, the form.

`TestMaintenanceCli` puts the close back on the real `dag-manage` / `state-manage` processes (the new
node's uuid only exists in that process's stdout), `TestMaintenanceDrive` walks a whole line from the
crossing to the far end, and `TestTheForm` pins what the line asks a worker for in its ticket.

什么时候插、插在哪条线上、同一个度量插几个，住在 `cases_maintenance.py`。

    python -m unittest conduct.tests.cases_maintenance_run    # 这个文件单独跑
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import NODE, NODE2, Base  # noqa: E402
from cases_maintenance import SelfHealingWorker, coding_line  # noqa: E402
from harness import HANDOFF_PY, _run_proc  # noqa: E402

__all__ = ['TestMaintenanceCli', 'TestMaintenanceDrive', 'TestTheForm']


class TestMaintenanceCli(Base):
    """改图走的是**真的那些进程**：`dag-manage add` 打的 uuid 只有它的 stdout 里有。

    沙箱默认把 `dag-manage` 的调用在这一个进程里答掉（省一个进程启动），上面那些用例走的就是那条
    快路；而抓取子进程输出这一步，两条路上是两种东西 —— 这一条把整趟收尾放回真 CLI 上跑，
    新节点的 uuid 由真进程印出来、由 `_capture` 读回来。
    """

    def test_the_graph_edit_runs_through_the_real_processes(self):
        sb = self.make(cli=True)
        sb.add_node(NODE, '写代码 A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, '写代码 B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'test_seconds_limit', 60)
        sb.next()                                  # the real `next` CLI pins NODE and dispatches it
        sb.handoff('cyc', NODE, 'completed', 'done',
                   {'deliverable': 'x', 'reading': {'test_seconds': 400}})

        action, proc = sb.next_cli('--after', NODE)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(action['action'], 'dispatch')
        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 3, f'the maintenance node was not inserted: {action}')
        made = action['node_id']
        self.assertEqual(nodes[made]['dependencies'], [NODE])
        self.assertEqual(nodes[NODE2]['dependencies'], [made])
        # 派发这一步到 prompt 文件为止：`dispatch <cycle>` 读的就是它，而这条用例断言的就是
        # 「那个文件在、里面是维护节点的调用行」。**不调 `dispatch`，也不传 `--spawn`** ——
        # 那条通路是通的，它会真的开出会话（派发语义那一层由 `TestDispatchVerb` 的 fake spawner 守）。
        self.assertTrue(Path(action['prompt']).is_file())
        self.assertEqual(Path(action['prompt']).parent.name, made)
        self.assertIn(made, Path(action['prompt']).read_text(encoding='utf-8'))
        self.assertTrue((sb.root / '.pf' / 'nodes' / made / 'spec.md').is_file())
        self.assertEqual(sb.current_node(), made)


class TestMaintenanceDrive(Base):
    """整条线跑到底：维护节点照常被派发、照常收尾，然后接着走它推迟的那一个。"""

    def test_the_driver_walks_through_the_inserted_node_and_on_to_the_deferred_one(self):
        """无人值守这一趟：A 报了一个越线的读数，于是 B 之前先跑维护节点，再接着跑 B。

        维护节点把那笔债还上了（它报回来的读数在线下），所以这一趟插**一个**节点就收尾 ——
        这条用例同时钉住「插进来的是普通节点、照常被派发、照常收尾」和「还上了就不再插」。
        """
        sb = coding_line(self, policies=[('test_seconds_limit', 60)], dispatch=False)

        # 每个会话都在条子里带上读数；维护节点报的是修好之后的数
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
        # 插进来的那个节点是个普通的节点：它自己也被派发、也交条子
        self.assertEqual(sb.read_signal('cyc', maintenance_node)['status'], 'completed')

    def test_a_plain_run_never_produces_a_maintenance_action(self):
        """没有阈值时整条线里不会出现维护动作：这条通道是加法的，关着时一点痕迹都没有。"""
        sb = self.make()
        sb.add_chain(2)
        sb.start_cycle(tags=['coding'])
        _, driver = self.run_to_stop(sb)

        self.assertNotIn('maintenance_due', driver.kinds())
        self.assertEqual(len(sb.dag()['nodes']), 2)

    def test_the_closing_call_keeps_the_worker_out_of_the_graph_edit(self):
        """收尾那次调用返回的走法只有 `dispatch` 与 `stop_pipeline`：改图是工具的事，
        调用方手里没有可以再跑一遍的命令（不变量 6）。"""
        sb = coding_line(self, policies=[('test_seconds_limit', 60)], dispatch=False)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': 400}})

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertFalse(action.get('commands'), 'the caller must have nothing left to run')
        # 工具自己那两样中间物不外泄
        self.assertNotIn('add_command', action)
        self.assertNotIn('spec_of', action)
        self.assertTrue(action['applied'])
        # 这次结算做了什么，逐条可查：建节点、拆旧边、连两条新边、钉指针
        did = ' '.join(action['did'])
        self.assertIn('dag-manage.py add', did)
        self.assertIn('undepend', did)
        self.assertIn('depend', did)
        self.assertIn('cycle hold', did)
        # 插进来的那个节点被记为这一次开出来的节点
        self.assertEqual(action['opened_node'], sb.current_node())

    def test_the_worker_can_report_a_reading_through_the_real_handoff(self):
        """`reading` 是条子上普通的一个 `outputs` 键：`handoff.py` 原样收下。

        收尾只读它认的那几个（`test_seconds`）；池子那一格即便写在条子上也照样由 issue 工具算。
        """
        sb = self.make()
        sb.add_node(NODE, '写代码 A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, '写代码 B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'test_seconds_limit', 60)
        sb.next()
        sb.handoff('cyc', NODE, 'completed', 'done',
                   {'deliverable': 'x', 'reading': {'test_seconds': 400, 'issue_pool_weight': 3}})

        ticket = sb.read_signal('cyc', NODE)
        self.assertEqual(ticket['outputs']['reading'],
                         {'test_seconds': 400, 'issue_pool_weight': 3})
        sb.close_round(NODE)
        self.assertEqual(len(sb.dag()['nodes']), 3)
        made = sb.current_node()
        # 只有越线的那个度量进来，没越线的那个留在原地
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')
        self.assertIn('test_seconds', spec)
        self.assertIn('"reading": {"test_seconds"', spec)
        self.assertNotIn('issue_pool_weight', (sb.dag()['nodes'][made]['title']))

class TestTheForm(Base):
    """`handoff.py --form <cycle>`：这条线要什么，由收条子的那一方现印，写不进去任何东西。"""

    def form(self, sb, cycle='cyc') -> str:
        return _run_proc(sb.root, [HANDOFF_PY, '--form', cycle]).stdout

    def a_line(self, **policies):
        """One coding node on a started line, with `policies` written onto the cycle."""
        return coding_line(self, policies=policies.items(), dispatch=False, pair=False)

    def test_a_line_without_numeric_policies_asks_for_nothing(self):
        """没配线的项目（`minimal` 档就是这种）拿到一张没有读数格子的骨架。"""
        text = self.form(self.a_line())

        self.assertIn('-o \'{"deliverable"', text)
        self.assertNotIn('"reading"', text)

    def test_the_test_line_asks_for_the_wall_clock(self):
        text = self.form(self.a_line(test_seconds_limit=60))

        self.assertIn('"reading":{"test_seconds":', text)

    def test_the_pool_line_never_becomes_a_ticket_slot(self):
        """池子那格由 issue 工具算：这条线配了它，表单里也不出现这个键。"""
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

