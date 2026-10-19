"""Which node a line picks: where the line starts, the filter set it reads, and the weight.

`cycle start` / `cycle select` 这一摊子：起点怎么定、过滤集管到哪一步、weight 怎么合成 —— 都在
挑选那一步读。收尾那一头（一个节点怎么算完、怎么收口）住在同目录的 `cases_node.py`。

    python -m unittest conduct.tests.cases_select        # 这个文件单独跑
"""

from __future__ import annotations

import importlib.util as importlib_util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import NODE, NODE2, Base, added, pipeline  # noqa: E402
from harness import DAG_MANAGE, STATE_MANAGE, _fresh_module, finish_node, run_dag  # noqa: E402


def _state_cycles_module():
    """`state` 是带相对导入的包，不能按单文件加载：照 state-manage.py 自己的引导，
    以包形式按路径整包装进来，取装好的 cycles 子模块。装前先清同名旧条目，本文件里的
    两次加载互不串。
    """
    name = 'pf_state_for_vocab'
    for stale in [n for n in sys.modules if n == name or n.startswith(name + '.')]:
        del sys.modules[stale]
    pkg_dir = STATE_MANAGE.parent / 'state'
    spec = importlib_util.spec_from_file_location(
        name, pkg_dir / '__init__.py', submodule_search_locations=[str(pkg_dir)])
    mod = importlib_util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod.cycles


# ═══════════════════════════════════════════════════════════════════════════════
#  起点、过滤集、weight：挑选那一步读的东西
# ═══════════════════════════════════════════════════════════════════════════════

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

    # ── 判据表第一行：过滤集里有就绪节点 → 按 DAG 的排序挑第一名 ──────────

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

        挡住一个节点 = 这一格不挑它。它仍是 `todo`、仍是 ready、依赖边原样。
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
        """impl 借的是 `state/cycles.py` 那一份正本，两个包各自装载同一个文件。

        这里钉的是那条边界：两份读数**按值相等**（`==`）。两个包各按自己的名字装载，拿到的是两个
        模块对象，所以跨包 `is` 不成立，谁也不该拿 `is` 去比这两份；要改这张表，改的是
        `state/cycles.py` 那个文件本身。
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

