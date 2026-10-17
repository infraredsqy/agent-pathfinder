"""The maintenance channel: thresholds in the cycle's policies, and the node they insert.

The rule this file is built around: the judgement happens at the CLOSE, each metric's number comes
from wherever its `source` says (`test_seconds` from the ticket's `reading` key, `issue_pool_weight`
from the issue tool that owns the pool), the lines come from the cycle's own policies, and a
crossing line puts a maintenance node between the node just closed and the node the close had
already picked. A project that configures no line gets the close it always got -- that is the
regression this channel lives or dies by, and it is the first case here.

这条线真的跑起来那一头（真进程改图、整条线走到底、收条子那一方看到的表单）住在
`cases_maintenance_run.py`。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE, NODE2, NODE3, pipeline, normalise
from harness import (InProcessDagManage, TOOLS_DIR, WorkerSession, _fresh_module, _run_proc,
                     run_state_manage, STATE_MANAGE)

__all__ = ['TestMaintenanceDedup', 'TestMaintenanceChannel', 'TestMaintenanceThresholds',
           'TestMaintenanceScope', 'TestIssueRepairSwitch']

#: The tool that owns the issue pool; `pool_weight()` asks it for the number through the same CLI.
ISSUE_MANAGE = TOOLS_DIR / 'issue' / 'issue_manager.py'

#: The line the maintenance cases write onto a cycle (`test_seconds_limit`), and the number their
#: tickets report around it.
LINE = 120


def seed_pool(sb, *issues) -> None:
    """把条目放进池子，走它自己的命令（这一格夹具与 `pool_weight()` 读的是同一份正本）。"""
    for item in issues:
        _run_proc(sb.root, [ISSUE_MANAGE, 'add', '--id', item['id'], '--title', item['id'],
                            '--severity', item['severity'],
                            '--decision', item.get('decision', 'autofix')])


def set_config(sb, key, value, *extra):
    """把一个设置写进项目，走它自己的命令（与这条通道读的是同一份正本）。

    `--global` 走的是沙箱自己那个家目录（`Sandbox.home`）—— 这个用例读的那一层，正是 `pf config`
    写的那一层。
    """
    return run_state_manage(sb.root, 'config', 'set', key, value, *extra)


def coding_line(test, *, policies=(), reading=None, dispatch=True, pair=True):
    """A coding line, started and pinned on its head node, with a ticket ready for it.

    The DAG is a real one -- `NODE` the head, `NODE2` waiting for it -- because the insertion
    rewrites the edge BETWEEN the node that was closed and the node the close had already picked: a
    pair with no edge between them would exercise only the no-op half of that.

    `policies` writes the cycle's own numbers (`test_seconds_limit`, `issue_pool_weight_limit`),
    `dispatch` walks the line one step and leaves the head's ticket carrying `reading`, and `pair`
    adds the second node -- a case whose subject is the line's own settings takes just the head.
    """
    sb = test.make()
    sb.add_node(NODE, '写代码 A', mode='auto', tags=['coding'])
    if pair:
        sb.add_node(NODE2, '写代码 B', mode='auto', tags=['coding'], deps=[NODE])
    sb.start_cycle(tags=['coding'])
    for key, value in policies:
        sb.set_policy('cyc', key, value)
    if dispatch:
        sb.next()                                  # the dispatch for NODE
        outputs = {'deliverable': 'x'}
        if reading is not None:
            outputs['reading'] = {'test_seconds': reading}
        sb.write_signal('cyc', NODE, 'completed', 'done', outputs)
    return sb


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
    """The channel itself: a project with nothing configured, a crossed line, and a reading under
    its line."""

    # ── 回归底线：一个数都不配时，收尾一个字都不多带 ──────────────

    def test_without_a_threshold_the_close_and_the_graph_are_untouched(self):
        """项目一个数都不配时，这条通道整个不生效：返回值、图、记录与不配它时逐字节相同。

        两个项目跑同一条线，唯一的差别是其中一个在 cycle 上多配了一根线。多配的那一个照样**不插**
        （读数没越线），于是两份记录必须一字不差 —— 配置那一格除外（它本来就该不同），剩下的口径照
        `TestNodeTags.test_an_untagged_cycle_picks_what_it_always_picked`：时间戳是合法差异，
        别的一个字都不许变。
        """
        def walked(data):
            """记录里去掉配置那一格：收尾写了什么，全看剩下的部分。"""
            data = normalise(data)
            for cycle in data.get('cycles') or []:
                cycle.pop('policies', None)
            return data

        control = coding_line(self, reading=10)
        configured = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=10)

        control_action = control.close_round(NODE)
        configured_action = configured.close_round(NODE)

        self.assertEqual(control_action['action'], 'dispatch')
        self.assertEqual(normalise(control_action), normalise(configured_action),
                         'a line that is not crossed must change nothing at all')
        self.assertEqual(configured.current_node(), NODE2, 'the close still pins its own pick')
        self.assertEqual(len(configured.dag()['nodes']), 2, 'no node was inserted')

        for rel in ('.pf/ideas.json', '.pf/state.json'):
            left = walked(json.loads(control.tree_contents()[rel]))
            right = walked(json.loads(configured.tree_contents()[rel]))
            self.assertEqual(left, right, f'{rel} must be what it always was')

    def test_a_ticket_carrying_no_reading_never_inserts_anything(self):
        """读数由会话自报：条子上没有 `reading` 时，阈值写得再明白也无事发生。"""
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=None)

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 2)

    def test_a_reading_under_the_line_inserts_nothing(self):
        """读数没越线：这是对照组 —— 同一个项目、同一根线，只把读数压到线下。"""
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE - 1)

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
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 40)

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
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 40)

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
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 40)

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
        sb.set_policy('cyc', 'test_seconds_limit', LINE)
        sb.next()                                  # NODE 的 dispatch
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': LINE + 40}})

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
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 40)

        sb.close_round(NODE)
        made = sb.current_node()
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')
        node = sb.dag()['nodes'][made]

        # 为什么：哪个度量、读到多少、线在哪
        self.assertIn('test_seconds', spec)
        self.assertIn(f'{LINE + 40:g}s', spec)
        self.assertIn(f'{LINE:g}s', spec)
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
        self.assertIn(str(LINE + 40), node['title'])

    def test_the_spec_and_the_note_name_the_line_and_the_field_it_came_from(self):
        """线是多少、读的是条目上的哪个字段：spec 与收尾那句 note 都点名，人回头查就查这里。"""
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 5)
        action = sb.close_round(NODE)
        made = sb.current_node()
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')

        self.assertEqual(len(sb.dag()['nodes']), 3)
        self.assertIn('test_seconds_limit', spec)
        self.assertIn(f'达到这条线策略里的线 {LINE:g}s', action['note'])

    def test_the_reading_exactly_on_the_line_is_over_it(self):
        """线是能接受的最后一个数：读数正好落在线上的那一刻，债就已经到顶了。"""
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE)

        sb.close_round(NODE)

        self.assertEqual(len(sb.dag()['nodes']), 3)

    def test_the_inserted_node_is_the_one_the_next_step_dispatches(self):
        """插完接着走：再问一次管线，它派发的就是维护节点，而那个被推迟的节点还在等。"""
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 40)
        sb.close_round(NODE)
        made = sb.current_node()

        again = sb.plan()
        self.assertEqual(again['action'], 'dispatch')
        self.assertEqual(again['node_id'], made)
        self.assertIn(made, sb.prompt_of(again))
        self.assertEqual(sb.dag()['nodes'][NODE2]['status'], 'todo')


class TestIssueRepairSwitch(Base):
    """这条通道的项目级开关：没设过＝开、写着 on＝开、写着 off＝即使越线也不插。

    开关由**工具**在收尾那一拍读，不由会话读 —— 所以三条用例走的是同一次收尾、同一个判定，差的
    只是项目设置里那一格。没设过的那一条是回归底线：它与写着 `on` 的那一条必须判出同一个东西。
    """

    LINE = LINE

    def crossed_line(self, issue_repair=None, global_value=None):
        """一条两个节点的 coding 线：配好线、条子上带着越线的读数，收尾随时可以走。"""
        sb = self.make()
        sb.add_node(NODE, '写代码 A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, '写代码 B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'test_seconds_limit', self.LINE)
        if global_value is not None:
            set_config(sb, pipeline.ISSUE_REPAIR_KEY, global_value, '--global')
        if issue_repair is not None:
            set_config(sb, pipeline.ISSUE_REPAIR_KEY, issue_repair)
        sb.next()                                  # NODE 的 dispatch
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': self.LINE + 40}})
        return sb

    def assert_inserted(self, sb, action):
        """插进来了：图上多一个节点、两条边接对、被钉住的是它。"""
        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 3, 'the maintenance node was not inserted')
        made = sb.current_node()
        self.assertNotIn(made, (NODE, NODE2))
        self.assertEqual(nodes[made]['dependencies'], [NODE])
        self.assertEqual(nodes[NODE2]['dependencies'], [made])
        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], made)

    def assert_not_inserted(self, sb, action):
        """没插：收尾照常走它原本挑中的那一个，图上不多任何节点。"""
        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(sb.current_node(), NODE2)
        self.assertEqual(sb.path_nodes(), [NODE])
        self.assertEqual(len(sb.dag()['nodes']), 2, 'nothing may be inserted')
        self.assertEqual(sb.dag()['nodes'][NODE2]['dependencies'], [NODE],
                         'the edge the channel would have rewritten is untouched')

    def test_a_project_that_never_set_it_inserts_the_node(self):
        """没设过这一键的项目：越线照插 —— 这是出厂默认，也是这条通道出现以来的行为。"""
        sb = self.crossed_line()

        self.assertTrue(pipeline.issue_repair_on(sb.root))
        action = sb.close_round(NODE)

        self.assert_inserted(sb, action)

    def test_the_switch_on_judges_exactly_what_a_project_that_never_set_it_judges(self):
        """写着 `on` 与没设过这一键：同一次收尾判出来的东西逐项相同（两个 uuid 除外）。"""
        unset = self.crossed_line()
        unset_hit = pipeline.crossed(unset.root, unset.cycle(), unset.read_signal('cyc', NODE))
        on = self.crossed_line(issue_repair='on')
        on_hit = pipeline.crossed(on.root, on.cycle(), on.read_signal('cyc', NODE))

        def without_spec(hit):
            """判定里的 `spec_of` 是个闭包（它拿的是新节点的 uuid），比它没有意义。"""
            return {k: v for k, v in hit.items() if k != 'spec_of'}

        self.assertIsNotNone(unset_hit)
        self.assertEqual(without_spec(on_hit), without_spec(unset_hit),
                         'writing the default down must not change the judgement')
        self.assertTrue(pipeline.issue_repair_on(on.root))
        self.assert_inserted(unset, unset.close_round(NODE))
        self.assert_inserted(on, on.close_round(NODE))

    def test_the_switch_off_keeps_a_crossed_line_from_inserting(self):
        """关掉它的项目：读数照样越线（判定读得到），但收尾一个新节点都不插。"""
        sb = self.crossed_line(issue_repair='off')
        ticket = sb.read_signal('cyc', NODE)

        # 越线这件事记录在案：读数在条子上、线在策略里，两样都还在手上
        self.assertEqual(pipeline.reading_of(ticket), {'test_seconds': float(self.LINE + 40)})
        self.assertEqual(pipeline.thresholds_for(sb.cycle())['test_seconds_limit'], self.LINE)
        self.assertFalse(pipeline.issue_repair_on(sb.root))
        self.assertIsNone(pipeline.crossed(sb.root, sb.cycle(), ticket),
                          'the switch must shut the judgement, not the measurement')
        action = sb.close_round(NODE)

        self.assert_not_inserted(sb, action)

    def test_the_global_layer_can_turn_it_off_too(self):
        """机器那一层也算数：全局写着 off、项目一个字没说，这个项目就不插。"""
        sb = self.crossed_line(global_value='off')

        self.assertFalse(pipeline.issue_repair_on(sb.root))
        self.assert_not_inserted(sb, sb.close_round(NODE))

    def test_the_project_layer_beats_the_global_one(self):
        """项目层盖全局层：全局关掉，项目自己写着 on，就照项目的来。"""
        sb = self.crossed_line(global_value='off', issue_repair='on')

        self.assertTrue(pipeline.issue_repair_on(sb.root))
        self.assert_inserted(sb, sb.close_round(NODE))

    def test_the_two_packages_spell_the_switch_the_same_way(self):
        """写侧（`pf config` 那张表）与读侧（这条通道）各有一份键名、词表与出厂值：这条比对它们。

        `conduct_pipeline_impl` 与 `conduct_state` 是两个按路径各自加载的包，import 一个进另一个会
        绕出一个环，所以两边各写一份 —— 两份分头漂移，就是一个项目明明关掉了通道、收尾却照插不误
        的来路。
        """
        spec = _fresh_module('state_manage_switch_probe', STATE_MANAGE).CONFIG_SPEC
        entry = spec[pipeline.ISSUE_REPAIR_KEY]      # 键名对不上就是这里 KeyError
        self.assertEqual(entry['kind'], 'choice')
        self.assertEqual(tuple(entry['values']), tuple(pipeline.ISSUE_REPAIR_VALUES))
        self.assertEqual(entry['default'], pipeline.ISSUE_REPAIR_DEFAULT)
        self.assertEqual(pipeline.ISSUE_REPAIR_DEFAULT, 'on',
                         'the channel is on unless a project says otherwise')


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


class TestMaintenanceDedup(Base):
    """同一度量同时只许一个未了结的维护节点：债已经有人在还，就不再插第二个。"""

    def test_a_metric_already_in_hand_is_not_inserted_twice(self):
        """维护节点还在局中（todo）时，同一个度量再越线也不再插 —— 修的人只有一个。"""
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 40)
        first = sb.close_round(NODE)
        self.assertEqual(first['action'], 'dispatch')
        made = sb.current_node()
        self.assertNotIn(made, (NODE, NODE2))
        self.assertEqual(sb.node(made)['status'], 'todo')

        # 这一次关的是维护节点自己，它的条子又报了一个越线的数：不插第二个，收尾照走。
        sb.write_signal('cyc', made, 'completed', 'still over',
                        {'deliverable': 'x', 'reading': {'test_seconds': LINE + 10}})
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
        sb.set_policy('cyc', 'test_seconds_limit', LINE)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done',
                        {'deliverable': 'x', 'reading': {'test_seconds': LINE + 40}})
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
                        {'deliverable': 'x', 'reading': {'test_seconds': LINE + 50}})
        sb.close_round(NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 4,
                         'a repaired metric that crosses again must insert a fresh node')

    def test_the_insert_lands_after_the_node_is_closed(self):
        """图编辑的顺序：老节点先标掉，维护节点才创建 —— 去重读图时不能把刚交的节点算在局内。"""
        sb = coding_line(self, policies=[('test_seconds_limit', LINE)], reading=LINE + 40)
        sb.close_round(NODE)
        self.assertEqual(sb.node(NODE)['status'], 'completed')
        self.assertEqual(sb.path_nodes(), [NODE])
