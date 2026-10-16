"""The closing call's own contract, node ownership across cycles, the two driving paths, and the
guards each of them cost a bug.

`TestContract.test_the_fast_path_and_the_cli_agree` is the case that keeps the in-process fast path
and the shipped CLI agreeing file for file; `TestPurity` pins "plan writes nothing"; the guards are
the invariants that survive from the old model.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import ACTION_VOCABULARY, Base, NODE, NODE2, NODE3, pipeline, normalise  # noqa: E402
from harness import Driver, Sandbox, WorkerSession, finish_node  # noqa: E402

__all__ = [
    'TestWorkerClosesItsOwnNode',
    'TestNodeOwnershipAcrossCycles',
    'TestContract',
    'TestPurity',
    'TestCloseGuards',
]


class TestWorkerClosesItsOwnNode(Base):
    """`next --after <node_id>`: the worker closes the node it just ran.

    A boundary session used to be the only actor that could turn a finished step into a verdict.
    The outcome is read off the signal the worker already wrote, so what is left is one call that
    says "the node I just finished is done; carry on or stop" -- and a node that has not finished
    answers `wait` rather than being taken as delivered.
    """

    def finished_node(self, sb, cli=False):
        """Pin the node, let its worker report `completed`."""
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        first = sb.next()
        WorkerSession(sb).work(first)
        return first

    def test_it_closes_the_node_and_spawns_the_next_one(self):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '另一条支线', mode='auto')
        sb.start_cycle()
        first = sb.next()
        WorkerSession(sb).work(first)

        settled, proc = sb.next_cli('--after', first['node_id'], '--spawn')

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'dispatch')
        self.assertEqual(settled['node_id'], NODE2)
        self.assertTrue(settled.get('spawned'), 'the closing call did not run --spawn')
        self.assertIn('dry-run', settled['spawned'])
        self.assertEqual(sb.path_nodes(), [NODE])
        self.assertEqual(sb.current_node(), NODE2)

    def test_the_last_node_stops_the_run(self):
        """No next node means stop: the same outcome the planner computes either way, and the
        `attention` list still reaches whoever is reading."""
        sb = self.make()
        first = self.finished_node(sb)

        settled, proc = sb.next_cli('--after', first['node_id'])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'stop_pipeline')
        self.assertEqual(sb.cycle()['status'], 'inactive')
        self.assertEqual(sb.path_nodes(), [NODE])

    def test_a_node_that_is_still_running_is_not_closed(self):
        """A `running` signal is never a green light -- the rule every path enforces."""
        sb = self.make()
        first = self.finished_node(sb)
        sb.write_signal('cyc', NODE, 'pending', '还没跑完')

        settled, proc = sb.next_cli('--after', first['node_id'])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'wait')
        self.assertEqual(sb.current_node(), NODE)
        self.assertEqual(sb.path(), [])

    def test_naming_the_wrong_node_changes_nothing(self):
        """The guard runs before every write, so a mistyped node id leaves the run untouched."""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '另一条支线', mode='auto')
        sb.start_cycle()
        sb.set_current_node(NODE)
        sb.write_signal('cyc', NODE, 'completed', 'done')
        before = sb.tree_hashes()

        _, proc = sb.next_cli('--after', NODE2, check=False)

        self.assertEqual(proc.returncode, 2)
        self.assertIn('does not name', proc.stderr)
        self.assertEqual(sb.tree_hashes(), before)
        self.assertEqual(sb.path(), [])

    def test_a_non_delivery_parks_the_node_shelved_and_stops(self):
        """A worker that reports `failed` is asking to be looked at, and the DAG's node status is
        where that shows: the node becomes `shelved`, out of play until a human revives it."""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '另一条支线', mode='auto')
        sb.start_cycle()
        first = sb.next()
        WorkerSession(sb, fail_nodes={NODE}).work(first)

        settled, proc = sb.next_cli('--after', first['node_id'])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'stop_pipeline')
        self.assertEqual(sb.dag_status(NODE), 'shelved')
        self.assertIn(NODE, [a.get('node_id') for a in settled.get('attention', [])])

    def test_a_closed_node_is_not_closed_twice(self):
        """The second call has nothing to settle and must say so rather than closing something
        again. On a one-node run the cycle is finished, which is the honest reason."""
        sb = self.make()
        first = self.finished_node(sb)
        sb.next_cli('--after', first['node_id'])
        before = sb.tree_hashes()

        _, proc = sb.next_cli('--after', first['node_id'], check=False)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('already closed', proc.stderr)
        self.assertEqual(sb.tree_hashes(), before)

    def test_a_node_nobody_rounds_is_refused_by_name(self):
        """`--after` is aimed by node alone, so an id that is not any cycle's current node has to
        be refused by name -- with a reading of where every cycle actually stands."""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()

        _, proc = sb.next_cli('--after', NODE3, check=False)

        self.assertEqual(proc.returncode, 2)
        self.assertIn(NODE3, proc.stderr)
        self.assertIn(NODE, proc.stderr)
        self.assertEqual(sb.path(), [])


class TestNodeOwnershipAcrossCycles(Base):
    """一个节点一次只归一条 cycle：收尾认节点，挑人不碰别人正拿着的。

    `--after` is aimed by NODE, not by cycle: a round used to be the unit, and the node that round
    carried is unique, so the closing call can settle the right line without being told its name.
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

        settled, proc = sb.next_cli('--after', a, '--continue', '--spawn')

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

        以前这里的判据是「这个项目有没有 active 的 cycle」：别的线 active 时，一条停住的线会被
        当成可以驱动，于是它被挑人、被派发，而人从来没让它开工。
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

        real_parse, real_argv = argparse.ArgumentParser.parse_args, sys.argv
        argparse.ArgumentParser.parse_args = grab
        sys.argv = [str(harness_env.STATE_MANAGE)]
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    mod.main()
        finally:
            argparse.ArgumentParser.parse_args = real_parse
            sys.argv = real_argv
        return caught['parser']

    @staticmethod
    def read_parser(parser):
        """One parser as `(positionals, flags, sub-verbs, int-typed destinations)`.

        `flags` is the harness's own `{flag: (namespace attribute, takes a value, default)}`, and
        `sub-verbs` is the same reading of a group's sub-parsers (`{}` for a leaf verb).
        """
        import argparse
        positionals, flags, subs, ints = [], {}, {}, set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                subs = {name: TestContract.read_parser(sp) for name, sp in action.choices.items()}
                continue
            if not action.option_strings:                 # `-h` is an option; a positional is not
                positionals.append(action.dest)
                continue
            if action.dest == 'help':
                continue
            if action.type is int:
                ints.add(action.dest)
            for flag in action.option_strings:
                flags[flag] = (action.dest, action.nargs != 0, action.default)
        return tuple(positionals), flags, subs, ints

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


class TestCloseGuards(Base):
    """A node's step may only be closed over work that actually reports."""

    def test_a_close_arriving_before_the_worker_reports_is_refused(self):
        """No ticket means nothing to settle. The tool must not read "no signal" as "delivered":
        closing here would throw away the work the session is still doing."""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.next()
        before = sb.tree_hashes()

        action = sb.close_node_call(NODE, decision='continue')

        self.assertEqual(action['action'], 'wait')
        self.assertFalse(action.get('applied'))
        self.assertEqual(sb.tree_hashes(), before)
        self.assertEqual(sb.path(), [])
        self.assertEqual(sb.current_node(), NODE)

    def test_a_running_signal_is_not_a_green_light(self):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'pending', '重试已备好')
        action = sb.next()

        self.assertEqual(action['action'], 'wait')
        self.assertIn('cycle retry', action['note'])
        self.assertIn(NODE, action['note'])

    def test_the_close_reports_a_half_open_state_stably(self):
        """Asked twice with the ticket on disk and the close not yet run, the answer is the same
        and nothing is consumed -- the whole point of `wait` being a stable state."""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done')
        before = sb.tree_hashes()

        first, second = sb.next(), sb.next()

        self.assertEqual(first['close_with'], second['close_with'])
        self.assertEqual(sb.tree_hashes(), before)

    def test_a_whole_run_of_nodes_closed_by_their_own_workers(self):
        """Three auto nodes, each closed by its own worker's call: the DAG drains, the path
        lists every step in order, and nothing needs a human.

        Two invariants ride along with the drain: the dispatches come out in chain order (a run
        IS the line), and the last close takes the line out of service (`inactive`) -- the
        uniform end-of-line write, whatever is left in the graph."""
        sb = self.make()
        ids = sb.add_chain(3)
        sb.start_cycle()
        final, driver = self.run_to_stop(sb)

        self.assertEqual(final['action'], 'stop_pipeline')
        self.assertEqual(sb.path_nodes(), ids)
        self.assertEqual(driver.nodes_closed, ids)
        self.assertEqual([sb.dag_status(uid) for uid in ids], ['completed'] * 3)
        self.assertEqual([uid for uid, _ in driver.dispatched], ids)   # 派发顺序=链序
        self.assertEqual(sb.cycle()['status'], 'inactive')             # 收尾把线下线

    def test_the_cycle_can_be_driven_by_the_cli_alone(self):
        """The shipped path end to end: `next --after <node> --spawn` closes one node and pins the
        next, with no session ever started (the dry-run switch)."""
        sb = self.make(cli=True)
        ids = sb.add_chain(2)
        sb.start_cycle()
        driver = Driver(sb, worker=WorkerSession(sb))
        final = driver.run()

        self.assertEqual(final['action'], 'stop_pipeline')
        self.assertEqual(sb.path_nodes(), ids)
