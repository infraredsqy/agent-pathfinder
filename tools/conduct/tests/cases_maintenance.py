"""The maintenance channel: thresholds in the cycle's policies, and the node they insert.

The rule this file is built around: the judgement happens at the CLOSE, each metric's number comes
from wherever its `source` says (`test_seconds` from the ticket's `reading` key, `issue_pool_weight`
from the issue tool that owns the pool), the lines come from the cycle's own policies, and a
crossing line puts a maintenance node between the node just closed and the node the close had
already picked. A project that configures no line gets the close it always got -- that is the
regression this channel lives or dies by, and it is the first case here.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE, NODE2, NODE3, pipeline, normalise
from harness import HANDOFF_PY, InProcessDagManage, TOOLS_DIR, WorkerSession, _run_proc

__all__ = ['TestMaintenanceDedup', 'TestMaintenanceChannel', 'TestMaintenanceThresholds', 'TestMaintenanceScope',
           'TestMaintenanceCli', 'TestMaintenanceDrive', 'TestTheForm']

#: The tool that owns the issue pool; `pool_weight()` asks it for the number through the same CLI.
ISSUE_MANAGE = TOOLS_DIR / 'issue' / 'issue_manager.py'


def seed_pool(sb, *issues) -> None:
    """把条目放进池子，走它自己的命令（这一格夹具与 `pool_weight()` 读的是同一份正本）。"""
    for item in issues:
        _run_proc(sb.root, [ISSUE_MANAGE, 'add', '--id', item['id'], '--title', item['id'],
                            '--severity', item['severity'],
                            '--decision', item.get('decision', 'autofix')])


class ReadingWorker(WorkerSession):
    """A worker whose ticket carries a maintenance reading -- what `handoff.py -o` takes verbatim.

    `reading` is a plain `outputs` key, so nothing about the report path changes: the
    worker measures, writes down what it measured, and the close is what reads it back.
    """

    def __init__(self, sb, reading, cycle='cyc', **kw):
        super().__init__(sb, cycle=cycle, **kw)
        self.reading = dict(reading)

    def outputs_for(self, action, deliverable, node):
        return {**super().outputs_for(action, deliverable, node), 'reading': self.reading_for(node)}

    def reading_for(self, node):
        return dict(self.reading)


class SelfHealingWorker(ReadingWorker):
    """The same session, and the one difference that matters: it fixes what it was sent for.

    A maintenance node exists because a measurement crossed its line, and its whole scope is to pull
    THAT measurement back under it. A worker that reported the same over-the-line number after doing
    the work would be saying it delivered while the debt stood -- so it reports the fixed number,
    which is also the only way to tell a channel that inserts one node from a channel that inserts
    one node forever.
    """

    #: The reading the maintenance node reports: back under the line it was inserted for.
    FIXED = 20

    def __init__(self, sb, reading, fixed=None, cycle='cyc', **kw):
        super().__init__(sb, reading, cycle=cycle, **kw)
        self.fixed = dict(fixed if fixed is not None else {'test_seconds': self.FIXED})
        # Each instance starts from the real graph: `last_added` is whatever the last `add` in this
        # process minted, which belongs to another sandbox's test as often as not.
        self.maintenance_nodes = set()

    def work(self, action):
        # Every `add` the tool runs mints exactly one node, and that node is a maintenance node
        # the maintenance channel is the only caller). `Sb.next()` answers in-process here, so the uuid the
        # graph just grew is readable without parsing anything.
        if InProcessDagManage.last_added:
            self.maintenance_nodes.add(InProcessDagManage.last_added)
        return super().work(action)

    def reading_for(self, node):
        if node in self.maintenance_nodes:
            return dict(self.fixed)
        return dict(self.reading)


class TestMaintenanceChannel(Base):
    """The channel itself: nothing configured (byte-for-byte the old close), a crossed line, and a
    reading under its line."""

    LINE = 120

    def two_coding_nodes(self, reading=None, thresholds=True, cli=False):
        """A two-node coding line, pinned on the first of them, with one worker's report ready.

        The DAG is a real one -- `NODE` the head, `NODE2` waiting for it -- because the insertion
        rewrites the edge BETWEEN what was closed and what the close picks, and a pair with no edge
        between them would exercise only the no-op half of that.
        """
        sb = self.make(cli=cli)
        sb.add_node(NODE, '写代码 A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, '写代码 B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        if thresholds:
            sb.set_policy('cyc', 'test_seconds_limit', self.LINE)
        outputs = {'deliverable': 'x'}
        if reading is not None:
            outputs['reading'] = {'test_seconds': reading}
        sb.next()                                  # the dispatch for NODE
        sb.write_signal('cyc', NODE, 'completed', 'done', outputs)
        return sb

    # ── 回归底线：一个数都不配，收尾与这个通道出现之前逐字节相同 ──────────────

    def test_without_a_threshold_the_close_and_the_graph_are_untouched(self):
        """项目一个数都不配时，这条通道整个不生效：返回值、图、记录与今天逐字节相同。

        两个项目跑同一条线，唯一的差别是其中一个在 cycle 上多配了一根线。多配的那一个照样**不插**
        （读数没越线），于是两份记录必须一字不差 —— 配置那一格除外（它本来就该不同），剩下的口径照
        `TestNodeTags.test_an_untagged_cycle_picks_what_it_always_picked`：时间戳是合法差异，
        别的一个字都不许变。
        """
        control = self.two_coding_nodes(reading=10, thresholds=False)
        configured = self.two_coding_nodes(reading=10, thresholds=True)

        control_action = control.close_round(NODE)
        configured_action = configured.close_round(NODE)

        self.assertEqual(control_action['action'], 'dispatch')
        self.assertEqual(normalise(control_action), normalise(configured_action),
                         'a line that is not crossed must change nothing at all')
        self.assertEqual(configured.current_node(), NODE2, 'the close still pins its own pick')
        self.assertEqual(len(configured.dag()['nodes']), 2, 'no node was inserted')
        def walked(data):
            """记录里去掉配置那一格：收尾写了什么，全看剩下的部分。"""
            data = normalise(data)
            for cycle in data.get('cycles') or []:
                cycle.pop('policies', None)
            return data

        for rel in ('.pf/ideas.json', '.pf/state.json'):
            left = walked(json.loads(control.tree_contents()[rel]))
            right = walked(json.loads(configured.tree_contents()[rel]))
            self.assertEqual(left, right, f'{rel} must be what it always was')

    def test_a_ticket_carrying_no_reading_never_inserts_anything(self):
        """读数由会话自报：条子上没有 `reading` 时，阈值写得再明白也无事发生。"""
        sb = self.two_coding_nodes(reading=None)

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 2)

    def test_a_reading_under_the_line_inserts_nothing(self):
        """读数没越线：这是对照组 —— 同一个项目、同一根线，只把读数压到线下。"""
        sb = self.two_coding_nodes(reading=self.LINE - 1)

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(sb.current_node(), NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 2, 'nothing crossed, nothing inserted')
        self.assertEqual(sb.path_nodes(), [NODE])

    # ── 越线：图上真的多一个节点，边是对的，被钉住的是新节点 ──────────────────

    def test_a_crossed_line_inserts_a_node_between_the_close_and_its_pick(self):
        """读数达到线：收尾那次结算在「刚收掉的节点」与「它原本挑中的节点」之间插一个新节点。

        一次断言把四件事钉住：图上多了一个节点、两条新边接对了、旧的那条**直接**边没了、被钉住的
        是刚插进来的那个 —— 而原本挑中的那个还在图里、还是 `todo`。
        """
        sb = self.two_coding_nodes(reading=self.LINE + 40)

        action = sb.close_round(NODE)

        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 3, 'the maintenance node was not inserted')
        made = sb.current_node()
        self.assertNotIn(made, (NODE, NODE2))

        # 边：旧节点 → 维护节点 → 原本挑中的节点，而旧的那条直接边不在了
        self.assertEqual(nodes[made]['dependencies'], [NODE],
                         'the maintenance node must be the closed node\'s direct dependent')
        self.assertEqual(nodes[NODE2]['dependencies'], [made],
                         'the picked node must now wait for the maintenance node')
        self.assertNotIn(NODE2, nodes[NODE]['dependencies'])
        self.assertEqual(nodes[NODE]['status'], 'completed')
        self.assertEqual(nodes[NODE2]['status'], 'todo', '被挑中不等于被跑过')
        self.assertEqual(nodes[made]['status'], 'todo')
        self.assertEqual(sb.path_nodes(), [NODE])

        # 走法与 dispatch 一样：prompt 已落盘、动作带 prompt_text / node_id
        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], made)
        self.assertTrue(action['prompt_text'])
        self.assertTrue(Path(action['prompt']).is_file())
        self.assertEqual(Path(action['prompt']).parent.name, made)

    def test_the_reported_prompt_path_is_what_the_writer_wrote(self):
        """维护那一格报出来的 prompt 路径，与写口算出来的那一处逐字相同。

        新节点的 uuid 是 `dag-manage add` 现场才有的，所以这条路径在收尾那一趟被算过两回：写
        prompt 的那一回与报给调用方的那一回。两处必须落在同一个文件上。
        """
        sb = self.two_coding_nodes(reading=self.LINE + 40)

        action = sb.close_round(NODE)

        made = sb.current_node()
        expected = pipeline.prompts.launch_prompt_path(sb.root, 'cyc', made)
        self.assertEqual(Path(action['prompt']), expected)
        self.assertTrue(expected.is_file())
        self.assertEqual(sb.prompt_of(action), f'/pf-conduct {made} [pipeline]\n')

    def test_the_generated_spec_lands_where_the_one_path_says(self):
        """生成 spec 的落点由 `nodes.node_spec_path` 一处定。

        写下去的那一份、spec 末尾自己报的那一句、写口算出来的那一处，三者是同一个文件。
        """
        sb = self.two_coding_nodes(reading=self.LINE + 40)

        sb.close_round(NODE)

        made = sb.current_node()
        landed = Path(pipeline.nodes.node_spec_path(sb.root, made))
        self.assertTrue(landed.is_file(), f'{landed} 不在写口说的那个地方')
        self.assertIn(f'（本 spec 落在：{landed.as_posix()}）',
                      landed.read_text(encoding='utf-8'))

    def test_the_spec_path_is_built_in_one_file(self):
        """实现包里拼 `spec.md` 这一段的文件只有 `nodes.py`。"""
        impl = TOOLS_DIR / 'conduct' / 'conduct_pipeline_impl'
        sites = sorted(path.name for path in sorted(impl.glob('*.py'))
                       if "'spec.md'" in path.read_text(encoding='utf-8'))

        self.assertEqual(['nodes.py'], sites)

    def test_a_crossed_line_only_defers_the_node_it_picked(self):
        """多格线撞上维护：延后的只有被挑中的那一格，同一趟补上来的其余格照常钉在手里。

        补格名单是整个交出去的（`parallel` 减手里还握着的），维护节点只顶替被挑中的那一格；
        名单里剩下的如果没人 pin，就会静静地空着 —— 它们已经就绪，却既不在手里也不在图里等人。
        """
        sb = self.make()
        sb.add_node(NODE, '写代码 A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, '写代码 B', mode='auto', tags=['coding'], deps=[NODE])
        sb.add_node(NODE3, '写代码 C', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])            # 缺省 parallel=1：开局只钉一个
        sb.set_policy('cyc', 'parallel', 3)
        sb.set_policy('cyc', 'test_seconds_limit', self.LINE)
        sb.next()                                  # NODE 的 dispatch
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': self.LINE + 40}})

        action = sb.close_round(NODE)

        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 4, 'the maintenance node was not inserted')
        made = [u for u in nodes if u not in (NODE, NODE2, NODE3)][0]
        deferred = [u for u in (NODE2, NODE3) if nodes[u]['dependencies'] == [made]]
        self.assertEqual(len(deferred), 1, 'exactly one fill node waits behind the maintenance node')
        keep = [u for u in (NODE2, NODE3) if u != deferred[0]][0]
        self.assertEqual(nodes[keep]['dependencies'], [NODE], 'the rest of the fill is untouched')
        self.assertEqual(nodes[keep]['status'], 'todo')
        self.assertEqual(sorted(sb.holding_ids()), sorted([keep, made]),
                         'the maintenance node takes the freed slot; the rest of the fill stays')
        self.assertEqual(sorted(n['node_id'] for n in action['nodes']), sorted([keep, made]))

    def test_the_inserted_node_carries_why_what_scope_and_a_mechanical_verdict(self):
        """维护节点的 spec 由工具生成：为什么被插进来、这次的范围、以及一条机械可判的完成判定。"""
        sb = self.two_coding_nodes(reading=self.LINE + 40)

        sb.close_round(NODE)
        made = sb.current_node()
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')
        node = sb.dag()['nodes'][made]

        # 为什么：哪个度量、读到多少、线在哪
        self.assertIn('test_seconds', spec)
        self.assertIn(f'{self.LINE + 40:g}s', spec)
        self.assertIn(f'{self.LINE:g}s', spec)
        self.assertIn('test_seconds_limit', spec)
        # 范围：这次只做这一件事
        self.assertIn('只做这件事', spec)
        # 怎么干：点名这一类债归哪份方法卡
        self.assertIn('怎么干', spec)
        self.assertIn('方法：test-audit', spec)
        # 完成判定：拉回线下，并给出可跑的命令
        self.assertIn('完成判定', spec)
        self.assertIn('低于', spec)
        self.assertIn('实测', spec)
        # spec 里不写实现细节：模板不猜函数名 / 文件路径 / 行号
        self.assertNotIn('.py', spec)
        # 图上的那个节点一行说清它是什么
        self.assertIn('维护：', node['title'])
        self.assertIn(str(self.LINE + 40), node['title'])

    def test_the_spec_and_the_note_name_the_line_and_the_field_it_came_from(self):
        """线是多少、读的是条目上的哪个字段：spec 与收尾那句 note 都点名，人回头查就查这里。"""
        sb = self.two_coding_nodes(reading=self.LINE + 5)
        action = sb.close_round(NODE)
        made = sb.current_node()
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')

        self.assertEqual(len(sb.dag()['nodes']), 3)
        self.assertIn('test_seconds_limit', spec)
        self.assertIn(f'达到这条线策略里的线 {self.LINE:g}s', action['note'])

    def test_the_reading_exactly_on_the_line_is_over_it(self):
        """线是能接受的最后一个数：读数正好落在线上的那一刻，债就已经到顶了。"""
        sb = self.two_coding_nodes(reading=self.LINE)

        sb.close_round(NODE)

        self.assertEqual(len(sb.dag()['nodes']), 3)

    def test_the_inserted_node_is_the_one_the_next_step_dispatches(self):
        """插完接着走：再问一次管线，它派发的就是维护节点，而那个被推迟的节点还在等。"""
        sb = self.two_coding_nodes(reading=self.LINE + 40)
        sb.close_round(NODE)
        made = sb.current_node()

        again = sb.plan()
        self.assertEqual(again['action'], 'dispatch')
        self.assertEqual(again['node_id'], made)
        self.assertIn(made, sb.prompt_of(again))
        self.assertEqual(sb.dag()['nodes'][NODE2]['status'], 'todo')


class TestMaintenanceThresholds(Base):
    """两条线住在 cycle 的 `policies` 里：与 `tags` / `when_shelved` 同一格，写它用 `cycle policy`。"""

    def test_the_lines_are_the_lines_own_policies(self):
        """两条线写在 cycle 的 `policies` 上，指令清单里一个字都没多 —— 记录留给有人说过的话。"""
        sb = self.make()
        sb.add_node(NODE, '写代码', mode='auto', tags=['coding'])
        sb.start_cycle(tags=['coding'])

        sb.set_policy('cyc', 'test_seconds_limit', 120)
        sb.set_policy('cyc', 'issue_pool_weight_limit', 16)

        self.assertEqual(pipeline.thresholds_for(sb.cycle()),
                         {'test_seconds_limit': 120, 'issue_pool_weight_limit': 16})
        self.assertEqual(sb.instruct_list(), [], 'the account is for things somebody said')

    def test_a_line_without_them_carries_none(self):
        """没配的线身上根本没有这两个键：`thresholds_for` 读到空，通道对它就是不生效。"""
        sb = self.make()
        sb.add_node(NODE, '写代码', mode='auto', tags=['coding'])
        sb.start_cycle(tags=['coding'])

        self.assertEqual(pipeline.thresholds_for(sb.cycle()), {})
        self.assertIsNone(sb.cycle().get('policies'))

    def test_the_numeric_lines_come_from_the_one_policy_table(self):
        """数值型那两条线取自 `conduct_state.cycles.POLICY_NUMBERS` 那一份正本。

        整数型的 `parallel` 是同格的另一类键，不在其中；一个数值线都没配时这份读数是空的 ——
        `crossed` 的非空门靠的就是它。
        """
        stated = {'parallel': 3, 'test_seconds_limit': 120}
        self.assertEqual(pipeline.thresholds_for({'id': 'c', 'policies': stated}),
                         {'test_seconds_limit': 120}, '`parallel` 不是这条通道读的键')
        self.assertEqual(pipeline.thresholds_for({'id': 'c', 'policies': {'parallel': 3}}), {},
                         '只配了整数型策略时这份读数该是空的')
        self.assertEqual(
            pipeline.thresholds_for({'id': 'c', 'policies': {'test_seconds_limit': 1,
                                                            'issue_pool_weight_limit': 2}}),
            {'test_seconds_limit': 1, 'issue_pool_weight_limit': 2})


class TestMaintenanceScope(Base):
    """领域限定：两个度量归代码这一支，别的线读得到同一个数，但不动。"""

    def test_a_line_that_is_not_about_code_reads_the_number_and_does_nothing(self):
        """别的线读得到同一个数（阈值与读数都在手上），但不动 —— 这两个度量归代码这一支。"""
        sb = self.make()
        sb.add_node(NODE, '写文案', mode='auto', tags=['writing'])
        sb.add_node(NODE2, '写文案 2', mode='auto', tags=['writing'], deps=[NODE])
        sb.start_cycle(tags=['writing'])
        sb.set_policy('cyc', 'test_seconds_limit', 1)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': 9999}})

        # 读数与线都在手上，越线也照算：不动的理由是这条线不属于代码那一支
        ticket = sb.read_signal('cyc', NODE)
        self.assertEqual(pipeline.reading_of(ticket), {'test_seconds': 9999.0})
        self.assertEqual(pipeline.thresholds_for(sb.cycle())['test_seconds_limit'], 1)
        self.assertIsNone(pipeline.crossed(sb.root, sb.cycle(), ticket))

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 2)

    def test_the_issue_pool_line_inserts_too(self):
        """第二个度量走的是同一条路：池子加权和越线，一样插一个维护节点。

        这一格不向会话要数：夹具把池子摆好，收尾那一刻由 issue 工具当场把它算出来。
        """
        sb = self.make()
        sb.add_node(NODE, '写代码 A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, '写代码 B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'issue_pool_weight_limit', 8)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done', {'deliverable': 'x'})
        seed_pool(sb, {'id': 'debt-p0', 'severity': 'P0'}, {'id': 'debt-p2', 'severity': 'P2'})

        sb.close_round(NODE)

        made = sb.current_node()
        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 3)
        self.assertEqual(nodes[made]['dependencies'], [NODE])
        self.assertEqual(nodes[NODE2]['dependencies'], [made])
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')
        self.assertIn('issue_pool_weight', spec)
        self.assertIn('issue_pool_weight_limit', spec)
        # 怎么干指到清池子的那一份卡，连工具一起点名
        self.assertIn('怎么干', spec)
        self.assertIn('方法：pf-issue-repair', spec)
        self.assertIn('pf issue weight', spec)
        # 这个数从哪来：spec 明说是工具算的，条子里不填它
        self.assertIn('weight', spec)
        self.assertIn('条子里不用填它', spec)
        self.assertNotIn('"reading": {"issue_pool_weight', spec)


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

    def two_coding_nodes(self):
        """一条两个节点的 coding 线，开好机、配好线：`NODE2` 等 `NODE`。

        `cycle start` 要排在那条指令之前：`scope=cycle` 的条目要有一个 line 可绑，这是它在
        `instruction add` 那一步就定下来的。
        """
        sb = self.make()
        sb.add_node(NODE, '写代码 A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, '写代码 B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'test_seconds_limit', 60)
        return sb

    def test_the_driver_walks_through_the_inserted_node_and_on_to_the_deferred_one(self):
        """无人值守这一趟：A 报了一个越线的读数，于是 B 之前先跑维护节点，再接着跑 B。

        维护节点把那笔债还上了（它报回来的读数在线下），所以这一趟插**一个**节点就收尾 ——
        这条用例同时钉住「插进来的是普通节点、照常被派发、照常收尾」和「还上了就不再插」。
        """
        sb = self.two_coding_nodes()

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
        sb = self.two_coding_nodes()
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
        sb = self.make()
        sb.add_node(NODE, '写代码', mode='auto', tags=['coding'])
        sb.start_cycle(tags=['coding'])
        for key, value in policies.items():
            sb.set_policy('cyc', key, value)
        return sb

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


class TestMaintenanceDedup(Base):
    """同一度量同时只许一个未了结的维护节点：债已经有人在还，就不再插第二个。"""
    LINE = TestMaintenanceChannel.LINE

    def two_coding_nodes(self, reading):
        sb = self.make()
        sb.add_node(NODE, '写代码 A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, '写代码 B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'test_seconds_limit', self.LINE)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': reading}})
        return sb

    def test_a_metric_already_in_hand_is_not_inserted_twice(self):
        """维护节点还在局中（todo）时，同一个度量再越线也不再插 —— 修的人只有一个。"""
        sb = self.two_coding_nodes(reading=self.LINE + 40)
        first = sb.close_round(NODE)
        self.assertEqual(first['action'], 'dispatch')
        made = sb.current_node()
        self.assertNotIn(made, (NODE, NODE2))
        self.assertEqual(sb.node(made)['status'], 'todo')

        # 这一次关的是维护节点自己，它的条子又报了一个越线的数：不插第二个，收尾照走。
        sb.write_signal('cyc', made, 'completed', 'still over',
                        {'deliverable': 'x', 'reading': {'test_seconds': self.LINE + 10}})
        second = sb.close_round(made)
        self.assertEqual(second['action'], 'dispatch')
        self.assertEqual(len(sb.dag()['nodes']), 3, 'a second maintenance node was inserted')
        self.assertEqual(sb.current_node(), NODE2, 'the deferred pick still came next')

    def test_a_finished_maintenance_node_frees_the_metric_again(self):
        """维护节点交出去了（completed）而债又越线：下一个越线的读数照常插新节点。"""
        sb = self.make()
        sb.add_node(NODE, '写代码 A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, '写代码 B', mode='auto', tags=['coding'], deps=[NODE])
        sb.add_node(NODE3, '写代码 C', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'test_seconds_limit', self.LINE)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': self.LINE + 40}})
        # 一次一格地走（parallel 先压到 1）：每一次收尾只推进自己这一个节点，后面的判定看得清。
        sb.set_policy('cyc', 'parallel', 1)
        first = sb.close_round(NODE)
        made = first['node_id']
        self.assertNotIn(made, (NODE, NODE2))

        # 维护节点跑完（读数在线下）→ 它手里的活交出去了，这个度量重新可插
        sb.write_signal('cyc', made, 'completed', 'fixed',
                        {'deliverable': 'x', 'reading': {'test_seconds': 10}})
        sb.close_round(made)
        sb.set_current_node(NODE2)
        # 再往后，别的节点又报了一个越线的数：债还在，就得再进来一趟
        sb.write_signal('cyc', NODE2, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': self.LINE + 50}})
        sb.close_round(NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 4,
                         'a repaired metric that crosses again must insert a fresh node')

    def test_the_insert_lands_after_the_node_is_closed(self):
        """图编辑的顺序：老节点先标掉，维护节点才创建 —— 去重读图时不能把刚交的节点算在局内。"""
        sb = self.two_coding_nodes(reading=self.LINE + 40)
        sb.close_round(NODE)
        self.assertEqual(sb.node(NODE)['status'], 'completed')
        self.assertEqual(sb.path_nodes(), [NODE])
