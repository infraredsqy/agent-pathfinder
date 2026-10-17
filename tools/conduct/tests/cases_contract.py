"""Node ownership across cycles, the two driving paths, and the guards each of them cost a bug.

`TestContract.test_the_fast_path_and_the_cli_agree` is the case that keeps the in-process fast path
and the shipped CLI agreeing file for file; `TestPurity` pins "plan writes nothing"; the guards are
the invariants that survive from the old model. The closing call itself (one worker ending the node
it just ran, and what a close refuses) lives in `cases_close.py`.
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import ACTION_VOCABULARY, Base, NODE, NODE2, NODE3, pipeline, normalise  # noqa: E402
from harness import (  # noqa: E402
    CONDUCT_DIR, DAG_MANAGE, Sandbox, WorkerSession, patched,
    _dag_hook as harness_dag_hook,
)

__all__ = [
    'TestNodeOwnershipAcrossCycles',
    'TestContract',
    'TestPurity',
    'TestOneChildProcessPath',
]


class TestNodeOwnershipAcrossCycles(Base):
    """一个节点一次只归一条 cycle：收尾认节点，挑人不碰别人正拿着的。

    `--after` is aimed by NODE, not by cycle: a node belongs to exactly one line, so the closing call
    can settle the right line without being told its name.
    """

    def two_lines(self):
        """Two auto cycles: `one` on a two-node line, `other` on a single-node line (`tail`).

        Built on the fast in-process path on purpose: the subject is the closing call the CLI
        runs, and only those calls go through the real process (`next_cli`). The fixture's own
        cycle start / cycle status / close steps are scaffolding the tool consumes off disk,
        and the fast path writes the same files the CLI would (one case in `TestContract` keeps
        the two paths honest).
        """
        sb = self.make()
        a = sb.add_node(NODE, '界面设计', mode='auto')
        bridge = sb.add_node(NODE2, '中段', mode='auto', deps=[a])
        sb.start_cycle('one')
        tail = sb.add_node(NODE3, '收尾', mode='auto')
        sb.start_cycle('other', force=True)
        return sb, a, bridge, tail

    def deliver(self, sb, cycle_id, node_id, status='completed'):
        sb.write_signal(cycle_id, node_id, status, 'done')

    def hold(self, sb, cycle_id, node_id, status='completed'):
        """Deliver the node AND close it, so the cycle is free to pick again.

        `cycle hold` refuses to re-pin a cycle that is still holding a node, so a test that
        wants the next pick has to end the step the way the pipeline does.
        """
        self.deliver(sb, cycle_id, node_id, status)
        sb.close_node_call(node_id, cycle_id=cycle_id)

    def test_after_settles_its_own_cycle_when_another_one_is_active(self):
        """The node names its own line. Reading the cycle off "the single active one" would answer
        about a step that is not the caller's -- and the caller is the one session that must not
        have to know cycle bookkeeping."""
        sb, a, bridge, tail = self.two_lines()
        self.deliver(sb, 'one', a)

        settled, proc = sb.next_cli('--after', a)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sb.path_nodes('one'), [a])
        self.assertEqual(sb.path_nodes('other'), [])
        self.assertEqual(sb.current_node('other'), tail)

    def test_the_next_pick_does_not_take_a_node_another_cycle_is_holding(self):
        """A node another line is pinned on is work this one cannot pick, even though the DAG calls
        it ready (its dependencies are met).

        The line goes out of service on that stop (every stop does), so looking again is the human
        act of putting it back in -- the same command the stop reason names.
        """
        sb, a, bridge, tail = self.two_lines()
        self.assertEqual(sb.current_node('other'), tail)

        # The second line finishes its own node; the row behind the shelved one is still shelved.
        self.hold(sb, 'other', tail)
        self.assertEqual(sb.cycle('other')['status'], 'inactive')
        sb.set_cycle_status('other', 'active')
        action = sb.plan_for('other')

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertIn(a, action['reason'])
        self.assertIn('one', action['reason'])
        self.assertEqual(sb.dag_status(a), 'todo', 'nothing was marked by a mere pick')
        # and the shelved node is not in this line's ready set at all
        self.assertNotIn(a, [n['id'] for n in pipeline.ranked_ready_nodes(sb.root)])
        self.assertEqual(pipeline.node_holders(sb.root).get(a), 'one')

    def test_a_shelved_node_is_named_as_shelved(self):
        """别的线正忙不属于本线的故障：两句 reason 读起来必须不一样，否则读的人会去查根本不在
        路上的 tag。"""
        sb, a, bridge, tail = self.two_lines()
        self.hold(sb, 'other', tail)
        sb.set_cycle_status('other', 'active')
        action = sb.plan_for('other')

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertIn('别的 cycle 占着', action['reason'])
        self.assertNotIn('过滤集', action['reason'])

    def test_an_inactive_line_settles_its_own_node_and_stays_out_of_service(self):
        """收尾那道门不看线的状态：一条停住的线照样钉着它没收尾的节点，收不掉就永远钉在那儿。

        但这条门只做记录 —— 记录这一步、标了 DAG，线的状态保持人给它的那个（inactive），一个
        字都不写。`cycle hold` 没有跑：不给一条不在服务中的线挑下一个节点。
        """
        sb, a, bridge, tail = self.two_lines()
        self.deliver(sb, 'one', a)
        sb.set_cycle_status('one', 'inactive')
        self.assertEqual(sb.cycle('one')['status'], 'inactive')
        before = sb.state()

        settled, proc = sb.next_cli('--after', a)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'stop_pipeline')
        self.assertEqual(sb.path_nodes('one'), [a])
        self.assertEqual(sb.dag_status(a), 'completed')
        self.assertIsNone(sb.current_node('one'), 'a node was picked for an inactive line')
        self.assertEqual(sb.cycle('one')['status'], 'inactive',
                         'the tool wrote a cycle status the human owns')
        self.assertEqual(sb.current_node('other'), tail, 'the other line was disturbed')
        self.assertNotIn('cycle hold', ' '.join(settled.get('did', [])))
        self.assertNotIn('cycle status', ' '.join(settled.get('did', [])))
        self.assertNotEqual(before, sb.state(), 'only the accounting should have moved')

    def test_a_continue_cannot_pry_an_inactive_line_open(self):
        """`--continue` 说的是「这条线接着走」，而这条线现在不归工具驱动：收尾照做，接着走照拒。"""
        sb, a, bridge, tail = self.two_lines()
        self.deliver(sb, 'one', a)
        sb.set_cycle_status('one', 'inactive')

        settled, proc = sb.spawn_next('--after', a, '--continue')

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'stop_pipeline')
        self.assertEqual(sb.path_nodes('one'), [a])
        self.assertIsNone(sb.current_node('one'))
        self.assertEqual(sb.cycle('one')['status'], 'inactive')
        self.assertNotIn('spawned', settled, 'a session was started for an inactive line')
        self.assertIn('cycle status one active', settled['reason'],
                      'the stop reason has to name the one command that resumes the line')

    def test_a_node_named_on_an_inactive_line_is_not_opened_and_the_reason_says_so(self):
        """人敲出来的名字不许被悄悄吞掉：这条线上 `--node` 不会被打开，reason 必须点出那个名字。"""
        sb, a, bridge, tail = self.two_lines()
        self.deliver(sb, 'one', a)
        sb.set_cycle_status('one', 'inactive')

        settled, proc = sb.next_cli('--after', a, '--node', bridge)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'stop_pipeline')
        self.assertIsNone(sb.current_node('one'), f'{bridge} was opened anyway')
        self.assertIn(bridge, settled['reason'])
        self.assertIn('没有被打开', settled['reason'])
        self.assertEqual(sb.cycle('one')['status'], 'inactive')

    def test_a_next_aimed_at_an_inactive_line_neither_picks_nor_dispatches(self):
        """驱动型 `next` 不碰一条不是 active 的线 —— 不管别的线在不在跑。

        判据落在这条线自己的状态上：别的线在跑，一条停住的线照样不许被挑人、被派发 —— 人没让它开工。
        """
        sb, a, bridge, tail = self.two_lines()
        sb.set_cycle_status('one', 'inactive')
        before = sb.state()

        aimed = sb.plan_for('one')
        self.assertEqual(aimed['action'], 'ask_user')
        self.assertIn('not active', aimed['question'])
        self.assertIn('cycle status one active', aimed['hint'])
        self.assertEqual(sb.state(), before, 'a driven next wrote something')

        # the same refusal through the shipped process, which is where the flag is spelled
        cli_action, proc = sb.next_cli('--cycle', 'one', '--plan', plan=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(cli_action['action'], 'ask_user')
        self.assertIn('cycle status one active', cli_action['hint'])
        self.assertEqual(sb.state(), before)
        self.assertFalse(cli_action.get('commands'))

        # The other line is still in service, and a bare `next` drives THAT one -- the parked line
        # stays exactly as the human left it.
        bare = sb.plan()
        self.assertNotEqual(bare['action'], 'ask_user')
        self.assertEqual(bare['cycle'], 'other')
        self.assertEqual(sb.current_node('one'), a, 'the parked line moved')
        self.assertEqual(sb.current_node('other'), tail)

    def test_a_bare_next_with_every_line_out_of_service_names_the_way_back(self):
        """一条线都没有在跑时，报的不能是「没有 cycle」：线就在那儿停着（可能还钉着节点）。"""
        sb, a, bridge, tail = self.two_lines()
        sb.set_cycle_status('one', 'inactive')
        sb.set_cycle_status('other', 'inactive')

        action = sb.plan()

        self.assertEqual(action['action'], 'ask_user')
        self.assertEqual(sorted(action['candidates']), ['one', 'other'])
        self.assertIn('cycle status', action['hint'])
        self.assertEqual(sb.cycle('other')['status'], 'inactive')


class TestContract(Base):
    """The two driving paths agree, and no action ever hands the caller a command."""

    def real_parser(self):
        """`state-manage.py`'s own argparse tree, read the way the harness's tables read it.

        The module's `main()` builds the parser and immediately dispatches on it, so the parser is
        caught by standing in for `parse_args`: it is handed a namespace that names no command, and
        `main()` then prints the top-level help and exits -- both swallowed here. Nothing is run and
        no process is started; what comes back is the real parser object.
        """
        import argparse
        import contextlib
        import io

        import harness_env

        mod = harness_env._fresh_module('state_manage_parser_probe', harness_env.STATE_MANAGE)
        caught = {}

        def grab(parser, *a, **kw):
            caught['parser'] = parser
            return argparse.Namespace(command=None)     # no verb: `main()` only prints the help

        with patched(argparse.ArgumentParser, 'parse_args', grab), \
                patched(sys, 'argv', [str(harness_env.STATE_MANAGE)]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                mod.main()
        return caught['parser']

    def dag_parser(self):
        """`dag-manage.py`'s own argparse tree, built by the module's own `_build_parser()`.

        Nothing is patched and nothing is run: the builder is a module-level function, and this is
        the parser the shipped process parses its own argv with.
        """
        import harness_env

        mod = harness_env._fresh_module('dag_manage_parser_probe', harness_env.DAG_MANAGE)
        return mod._build_parser()

    @staticmethod
    def read_parser(parser):
        """One parser as `(positionals, flags, sub-verbs, int-typed destinations, nargs='*' ones)`.

        `flags` is the harness's own `{flag: (namespace attribute, takes a value, default)}`, and
        `sub-verbs` is the same reading of a group's sub-parsers (`{}` for a leaf verb). The last two
        are what a stand-in has to cast or fill differently from the shape alone: a destination the
        CLI declares `type=int`, and a positional declared `nargs='*'`.
        """
        import argparse
        positionals, flags, subs, ints, star = [], {}, {}, set(), set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                subs = {name: TestContract.read_parser(sp) for name, sp in action.choices.items()}
                continue
            if action.type is int:
                ints.add(action.dest)
            if not action.option_strings:                 # `-h` is an option; a positional is not
                positionals.append(action.dest)
                if action.nargs == '*':
                    star.add(action.dest)
                continue
            if action.dest == 'help':
                continue
            for flag in action.option_strings:
                flags[flag] = (action.dest, action.nargs != 0, action.default)
        return tuple(positionals), flags, subs, ints, star

    def test_the_harness_restates_the_real_parser(self):
        """The fast path's one restatement of the CLI is pinned against the CLI, item by item.

        `FastStateManage._SPEC` (and the two groups' nested tables) says which positional is called
        what, which flag lands in which namespace attribute, and what its default is. Nothing else
        in the suite would catch that restatement drifting: the fast path would simply read an
        attribute the shipped parser never writes, and every case driving it would keep passing.
        """
        import harness_fast
        fast = harness_fast.FastStateManage

        verbs = self.read_parser(self.real_parser())[2]     # the top-level sub-parsers
        top = set(fast._VERBS)
        self.assertTrue(top <= set(verbs),
                        f'the harness drives verbs the CLI has not: {top - set(verbs)}')

        ints = set()
        for verb, (positionals, flags) in fast._SPEC.items():
            self.assertIn(verb, top, f'{verb} is restated but not driven')
            self.assertEqual((positionals, flags), verbs[verb][:2], verb)
            ints |= verbs[verb][3]
        for group, table in (('cycle', fast._CYCLE_SPEC),
                             ('instruction', fast._INSTRUCTION_SPEC)):
            self.assertIn(group, top, f'{group} is restated but not driven')
            for verb, (positionals, flags) in table.items():
                self.assertEqual((positionals, flags), verbs[group][2][verb][:2],
                                 f'{group} {verb}')
                ints |= verbs[group][2][verb][3]
        # Every restated verb has the call that runs it: a table naming a verb nothing answers would
        # only fail at the moment some case happens to drive it.
        self.assertEqual(set(fast._SPEC), set(fast._CALLS))
        self.assertEqual(set(fast._CYCLE_SPEC), set(fast._CYCLE_CALLS))
        # The destinations the CLI declares `type=int` have to be cast here too: a string through
        # this harness compares as the wrong type instead of failing visibly.
        self.assertTrue(ints <= set(harness_fast._INT_FLAGS),
                        f'declared type=int but not cast: {ints - set(harness_fast._INT_FLAGS)}')

    def test_the_harness_restates_the_dag_parser(self):
        """The in-process dag-manage stand-in is pinned against dag-manage's own parser, item by item.

        `InProcessDagManage` answers the argv the pipeline builds for dag-manage by running
        dag-manage's own `cmd_*`; only the process start is gone. What it restates is the argv shape:
        which positional is called what, which flag lands in which namespace attribute, what its
        default is, which positional swallows the rest, which destinations are cast to `int`, and
        which command a verb reaches.

        The maintenance channel writes the graph through this stand-in (`add` / `depend` /
        `undepend`), minting a real uuid and writing real edges, so a drift in the restatement --
        `--mode`'s default becoming `auto`, say -- writes different nodes than the shipped process
        would while every fast-path case stays green. The one case that drives the real processes
        for that channel reads the new edges and statuses back, never the new node's own attributes.
        """
        import argparse

        import harness_fast
        dag = harness_fast.InProcessDagManage

        parser = self.dag_parser()
        choices = next(action for action in parser._actions
                       if isinstance(action, argparse._SubParsersAction)).choices
        verbs = self.read_parser(parser)[2]         # the same reading the state-manage pin uses

        self.assertTrue(set(dag._SPEC) <= set(verbs),
                        f'the harness drives verbs dag-manage has not: {set(dag._SPEC) - set(verbs)}')
        self.assertEqual(set(dag._SPEC), set(dag._CALLS), 'a served verb has no command behind it')

        int_flags, int_positionals = set(), set()
        for verb, (positionals, flags) in dag._SPEC.items():
            real_positionals, real_flags, _subs, ints, star = verbs[verb]
            self.assertEqual((positionals, flags), (real_positionals, real_flags), verb)
            # A `nargs='*'` positional defaults to the empty list and swallows the run where it
            # stands: a stand-in that reads one token would cut the description short.
            self.assertEqual(set(dag._VARIADIC.get(verb, ())), star, verb)
            int_positionals |= set(positionals) & ints
            int_flags |= ints - set(positionals)
            # The verb reaches the command dag-manage's own parser binds to it (`_CALLS` names it,
            # and `handle` reaches it by that name on its own copy of the module).
            bound = choices[verb]._defaults.get('func')
            self.assertIsNotNone(bound, f'{verb} has no command bound to it')
            self.assertEqual(dag._CALLS[verb], bound.__name__, verb)

        self.assertEqual(set(dag._INT_POSITIONALS), int_positionals,
                         f'declared type=int but not cast: {int_positionals}')
        self.assertEqual(set(dag._INT_FLAGS), int_flags,
                         f'declared type=int but not cast: {int_flags}')

    def test_the_cli_scope_vocabulary_is_the_pipelines_own(self):
        """`--scope` 的词表来自判定的正本，CLI 没有自己的第二份。

        钉死在本地副本上的话，给判定加第四个 scope 之后 CLI 先在 argparse 那一步把合法的值拒了，
        借来的判定根本轮不到跑 —— 两个词表，两个答案。
        """
        import argparse

        parser = self.real_parser()
        scopes = []

        def walk(p):
            for action in p._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for sub in action.choices.values():
                        walk(sub)
                elif action.option_strings and action.dest == 'scope':
                    scopes.append(list(action.choices))

        walk(parser)

        self.assertEqual(2, len(scopes), '`instruction add` 与 `instruction list` 各有一处 --scope')
        for choices in scopes:
            self.assertEqual(list(pipeline.SCOPES), choices)

    def test_next_reads_the_parsed_line_itself(self):
        """`cmd_next` / `cmd_dispatch` 各只收解析好的那一份，字段自己读。

        转发那一行摆着一串位置实参（12 个）的话，给 `next` 加一个旗标就要同时改 parser、那一行、
        函数的签名；其中大半是重推 argparse 已有的缺省，另有两个是同一个答案算两遍。收成一个名字
        空间之后，加旗标就是 parser 一行加读它的那一行。
        """
        source = (CONDUCT_DIR / 'conduct_state' / 'nextcmd.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        for name in ('cmd_next', 'cmd_dispatch'):
            fn = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == name)
            self.assertEqual([a.arg for a in fn.args.args], ['args'],
                             f'{name} 收的还是散开的那些实参')

    def test_the_prompt_text_builder_takes_the_node_id_alone(self):
        """`build_prompt_text`：这一行的输入就是节点 id。

        派发文本不随项目或随线而变（节点自己的 mode、权重、spec 落点由会话读 `dag-manage.py info`），
        所以签名的三个形参只该留一个 —— 多出来的形参读起来是「这个值会影响文本」的错觉。这一行怎么
        拼也只有一处：导出面上不带第二个拼它的名字。
        """
        self.assertEqual(['node_id'],
                         list(inspect.signature(pipeline.build_prompt_text).parameters))
        self.assertEqual(f'/pf-conduct {NODE} [pipeline]\n', pipeline.build_prompt_text(NODE))
        with self.assertRaises(ValueError):
            pipeline.build_prompt_text(None)
        self.assertFalse(hasattr(pipeline, 'prompt_for'),
                         '导出面上又多了一个拼这行的名字')

    def test_the_impl_package_keeps_the_one_export_list(self):
        """导出面只有 `conduct_pipeline_impl/__init__.py` 那一张表。

        模块级的第二张名单没有读者，却跟包的那张分头长：两边列的名字各说各话时，谁也没报错。层里
        不留名单，就没有第二处可漂。
        """
        def carries_a_list(path):
            for node in ast.parse(path.read_text(encoding='utf-8')).body:
                if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == '__all__' for t in node.targets):
                    return True
            return False

        carrying = [path.name for path in
                    sorted((CONDUCT_DIR / 'conduct_pipeline_impl').glob('*.py'))
                    if carries_a_list(path)]
        self.assertEqual(['__init__.py'], carrying)

    def test_no_action_in_a_whole_run_carries_commands(self):
        """The one rule that survived every rewrite, plus what a whole run is made of: the actions
        a caller ever sees are the two driving kinds -- `dispatch` to open a step, `stop_pipeline`
        to end the run."""
        sb = self.make()
        sb.add_chain(3)
        sb.start_cycle()
        _, driver = self.run_to_stop(sb)

        self.assertEqual(driver.no_commands_anywhere(), [])
        self.assertIn('dispatch', driver.kinds())
        self.assertIn('stop_pipeline', driver.kinds())

    def test_every_action_the_conductor_can_receive_is_one_it_knows(self):
        sb = self.make()
        sb.add_chain(2)
        sb.start_cycle()
        final, driver = self.run_to_stop(sb)

        known = set(ACTION_VOCABULARY) | {'open_node'}
        # `open_node` is the planner's own step and never reaches a caller: what a caller sees is
        # the dispatch behind it, which `apply` produces by re-planning.
        seen = set(driver.kinds()) | {final['action']}
        self.assertTrue(seen <= known, f'unknown action(s): {seen - known}')
        self.assertNotIn('open_node', seen)

    def test_the_tools_own_progress_claims_match_the_disk(self):
        """`applied` has to mean something moved: the driver compares its own snapshots, and this
        pins that the claim is not made on a no-op."""
        sb = self.make()
        sb.add_chain(2)
        sb.start_cycle()
        _, driver = self.run_to_stop(sb)

        applied = [a for a, _, _ in driver.applied]
        self.assertTrue(applied)
        for action, before, after in driver.applied:
            self.assertNotEqual(before, after, action)

    def test_the_snapshot_answers_one_shape(self):
        """`_snapshot` 只有一种形状：这条线在不在盘上，三个位置都在、位置的含义也一样。

        契约那一趟按 `[0]` / `[1]` 读它写进 `progress`，另一个形状里含义错开的话，缺线那一趟读到的
        就是别的东西 —— 缺线时每个位置答的都是「没有」。
        """
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()

        on_disk = pipeline._snapshot(sb.root, 'cyc')
        absent = pipeline._snapshot(sb.root, 'no-such-cycle')

        self.assertEqual(3, len(on_disk))
        self.assertEqual(len(on_disk), len(absent), '_snapshot 的两条路答的不是一个形状')
        self.assertEqual((None, None, None), absent)

    def test_the_fast_path_and_the_cli_agree(self):
        """The in-process path and the shipped processes produce the same project.

        The suite drives the real modules in process for speed (see harness.py); this case is what
        keeps that from drifting from the shipped surface, file for file.
        """
        fast, slow = Sandbox(), Sandbox(cli=True)
        self.addCleanup(fast.close)
        self.addCleanup(slow.close)
        for sb in (fast, slow):
            sb.add_node(NODE, '界面设计', mode='auto')
            sb.start_cycle()

        for sb in (fast, slow):
            action = sb.next()
            self.assertEqual(action['action'], 'dispatch')
            WorkerSession(sb).work(action)
            waiting = sb.next()
            self.assertEqual(waiting['action'], 'wait')
            sb.close_round(waiting['node_id'])

        self.assertEqual(normalise(fast.state()), normalise(slow.state()))
        for rel in ('.pf/signals/cyc/%s.json' % NODE, '.pf/ideas.json'):
            self.assertEqual(normalise(json.loads(fast.tree_contents()[rel])),
                             normalise(json.loads(slow.tree_contents()[rel])), rel)
        self.assertEqual(fast.path_nodes(), slow.path_nodes())


class TestPurity(Base):
    """`plan` is read-only: it reads state and returns an action, and writes nothing."""

    def test_plan_writes_nothing_at_all(self):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        before = sb.tree_hashes()
        action = sb.plan()
        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(sb.tree_hashes(), before)

    def test_plan_builds_a_prompt_without_writing_it(self):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        action = sb.plan()
        self.assertTrue(action['prompt_text'])
        self.assertFalse(Path(action['prompt']).exists(),
                         'the plan wrote the prompt file it only promised')

    def test_plan_writes_nothing_on_the_close_path_either(self):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done')
        before = sb.tree_hashes()
        action = sb.plan()
        self.assertEqual(action['action'], 'wait')
        self.assertEqual(sb.tree_hashes(), before)

    def test_apply_is_what_writes_the_prompt_file(self):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        action = sb.next()
        self.assertEqual(Path(action['prompt']).read_text(encoding='utf-8'), action['prompt_text'])


class TestOneChildProcessPath(Base):
    """子进程只有一条通路：入口的 `_run` / `_capture`（同一条缝的两半）。

    `seam` 许下的承诺是「patch 入口的那几个名字，到处都生效」。有一条调用自己复制一遍
    `subprocess.run` 的写法时，那份承诺就不成立：按文档 patch 入口的人拦不到它，只能在 stdlib
    的 `subprocess` 上做进程级猴子补丁（并为此加锁、并解释「必须打在共享模块上」）。
    """

    def test_the_dag_ranking_goes_through_the_seam(self):
        """就绪集的排序那次子进程调用走接缝：patch 入口的 `_capture` 就拦得住它。

        `dag-manage list` 是包内唯一一次「要看输出」的子进程调用（维护通道的 `add` 是另一次），
        它自己复制过一份 `subprocess.run` 的写法，这一条钉住它从缝里走。
        """
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.create_cycle('cyc')            # 手里没有节点：挑人要先问就绪集，就绪集要先问排序
        seen = []
        real = pipeline._capture

        def spy(argv, cwd):
            seen.append([str(a) for a in argv])
            return real(argv, cwd)

        with harness_dag_hook(sb.root):
            pipeline._capture = spy
            try:
                action = pipeline.plan(sb.root, 'cyc')
            finally:
                pipeline._capture = real

        self.assertEqual(action['action'], 'open_node', action)
        self.assertTrue([argv for argv in seen if Path(argv[1]).name == 'dag-manage.py'],
                        f'排序那次调用没走接缝：{seen}')

    def test_no_logic_module_reaches_a_child_process_on_its_own(self):
        """包里的模块一个都不许自己起子进程：自己起的那些调用，patch 接缝就拦不到。

        看去掉注释与字符串后的源码（AST），不是找字面量 —— 说明里提一句 `subprocess` 不算数，
        真的用上它才算。
        """
        offenders = []
        for path in sorted((CONDUCT_DIR / 'conduct_pipeline_impl').glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            imported = {alias.name for node in ast.walk(tree)
                        if isinstance(node, (ast.Import, ast.ImportFrom))
                        for alias in node.names}
            if 'subprocess' in used | imported:
                offenders.append(path.name)
        self.assertEqual(offenders, [], f'这些模块自己起了子进程，绕过了接缝：{offenders}')
