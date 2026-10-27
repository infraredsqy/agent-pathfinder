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
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import ACTION_VOCABULARY, Base, NODE, NODE2, NODE3, pipeline, normalise  # noqa: E402
from harness import (  # noqa: E402
    CONDUCT_DIR, Sandbox, WorkerSession, plugins_dir, patched, write_plugin,
    _dag_hook as harness_dag_hook,
)

__all__ = [
    'TestNodeOwnershipAcrossCycles',
    'TestContract',
    'TestPurity',
    'TestOneChildProcessPath',
    'TestTheInsertionActionShape',
]


class TestNodeOwnershipAcrossCycles(Base):
    """One node belongs to one cycle at a time: a close is aimed by node, and a pick leaves what
    another line is holding alone.

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
        a = sb.add_node(NODE, 'UI design', mode='auto')
        bridge = sb.add_node(NODE2, 'middle', mode='auto', deps=[a])
        sb.start_cycle('one')
        tail = sb.add_node(NODE3, 'close out', mode='auto')
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
        """Another line being busy is not a fault of this line, so the two reasons must not read
        alike: a reader who cannot tell them apart goes hunting for a tag that is not there."""
        sb, a, bridge, tail = self.two_lines()
        self.hold(sb, 'other', tail)
        sb.set_cycle_status('other', 'active')
        action = sb.plan_for('other')

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertIn('another cycle already occupies', action['reason'])
        self.assertNotIn('filter set', action['reason'])

    def test_an_inactive_line_settles_its_own_node_and_stays_out_of_service(self):
        """The close gate does not look at the line's status: a stopped line is still pinned on the
        node it never closed out, and it stays pinned until it does.

        But this gate only records -- it books the step, marks the DAG, and writes not one word to
        the status the human gave the line (inactive). `cycle hold` never runs: a line out of
        service is not handed a next node to pick.
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
        """`--continue` says "keep this line going", and this line is not tool-driven right now: the
        close happens, carrying on does not."""
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
        """A name a human typed must not be swallowed in silence: `--node` is not opened on this
        line, and the reason has to name the node that lost out."""
        sb, a, bridge, tail = self.two_lines()
        self.deliver(sb, 'one', a)
        sb.set_cycle_status('one', 'inactive')

        settled, proc = sb.next_cli('--after', a, '--node', bridge)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(settled['action'], 'stop_pipeline')
        self.assertIsNone(sb.current_node('one'), f'{bridge} was opened anyway')
        self.assertIn(bridge, settled['reason'])
        self.assertIn('was not opened', settled['reason'])
        self.assertEqual(sb.cycle('one')['status'], 'inactive')

    def test_a_next_aimed_at_an_inactive_line_neither_picks_nor_dispatches(self):
        """A driven `next` does not touch a line that is not active -- whether or not another line
        is running.

        The judgement rides on this line's own status: another line may be running, and a stopped
        line is still not picked from and not dispatched -- the human never put it to work.
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
        """With no line in service at all, the answer must not be "no cycle": the lines are right
        there, stopped, and possibly still pinned on a node."""
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

        The plugin that inserts nodes writes the graph through this stand-in (`add` / `depend` /
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
        """`--scope`'s vocabulary comes from the one source of the judgement, and the CLI keeps no
        second copy of its own.

        Pinned to a local copy, a fourth scope added to the judgement would be refused by the CLI at
        the argparse step, and the borrowed judgement would never get to run -- two vocabularies,
        two answers.
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

        self.assertEqual(2, len(scopes), '`instruction add` and `list` each carry one --scope')
        for choices in scopes:
            self.assertEqual(list(pipeline.SCOPES), choices)

    def test_next_reads_the_parsed_line_itself(self):
        """`cmd_next` / `cmd_dispatch` each take the parsed namespace alone and read the fields off
        it themselves.

        With a string of positional arguments (12 of them) on the forwarding line, adding one flag
        to `next` meant touching the parser, that line and the function's signature at once; most of
        them re-derived a default argparse already has, and two computed the same answer twice. Cut
        down to one namespace, a new flag is one parser line plus the line that reads it.
        """
        source = (CONDUCT_DIR / 'state' / 'nextcmd.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        for name in ('cmd_next', 'cmd_dispatch'):
            fn = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == name)
            self.assertEqual([a.arg for a in fn.args.args], ['args'],
                             f'{name} still takes the loose positional arguments')

    def test_the_prompt_text_builder_takes_the_node_id_alone(self):
        """`build_prompt_text`: the input to this line is the node id, nothing else.

        The dispatch text does not vary with the project or the line (the node's own mode, weight
        and spec landing spot are read by the session off `dag-manage.py info`), so the signature's
        three parameters should keep one -- the extra ones read as an illusion that "this value
        affects the text". How this line is spelled also lives in one place: the export surface
        carries no second name that builds it.
        """
        self.assertEqual(['node_id'],
                         list(inspect.signature(pipeline.build_prompt_text).parameters))
        self.assertEqual(f'/pf-conduct {NODE} [pipeline]\n', pipeline.build_prompt_text(NODE))
        with self.assertRaises(ValueError):
            pipeline.build_prompt_text(None)
        self.assertFalse(hasattr(pipeline, 'prompt_for'),
                         'the export surface has gained a second name that builds this line')

    def test_the_impl_package_keeps_the_one_export_list(self):
        """The export surface is the one table in `pipeline/__init__.py`.

        A second module-level list has no reader, yet grows apart from the package's: when the two
        disagree about the names, nothing raises. With no list left in the layer, there is no second
        place to drift.
        """
        def carries_a_list(path):
            for node in ast.parse(path.read_text(encoding='utf-8')).body:
                if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == '__all__' for t in node.targets):
                    return True
            return False

        carrying = [path.name for path in
                    sorted((CONDUCT_DIR / 'pipeline').glob('*.py'))
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
        """`_snapshot` answers one shape: on disk or off it, all three slots are there and mean the
        same thing.

        The contract run reads it by `[0]` / `[1]` for what it writes into `progress`; with the
        meanings shifted in the other shape, the absent-line run would read something else -- with
        no line on disk every slot answers "nothing".
        """
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()

        on_disk = pipeline._snapshot(sb.root, 'cyc')
        absent = pipeline._snapshot(sb.root, 'no-such-cycle')

        self.assertEqual(3, len(on_disk))
        self.assertEqual(len(on_disk), len(absent), '_snapshot\'s two paths do not answer one shape')
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
            sb.add_node(NODE, 'UI design', mode='auto')
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
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        before = sb.tree_hashes()
        action = sb.plan()
        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(sb.tree_hashes(), before)

    def test_plan_builds_a_prompt_without_writing_it(self):
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        action = sb.plan()
        self.assertTrue(action['prompt_text'])
        self.assertFalse(Path(action['prompt']).exists(),
                         'the plan wrote the prompt file it only promised')

    def test_plan_writes_nothing_on_the_close_path_either(self):
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done')
        before = sb.tree_hashes()
        action = sb.plan()
        self.assertEqual(action['action'], 'wait')
        self.assertEqual(sb.tree_hashes(), before)

    def test_apply_is_what_writes_the_prompt_file(self):
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        action = sb.next()
        self.assertEqual(Path(action['prompt']).read_text(encoding='utf-8'), action['prompt_text'])


class TestOneChildProcessPath(Base):
    """There is one road to a child process: the entry's `_run` / `_capture` (the two halves of the
    same seam).

    What `seam` promises is "patch the entry's names and it holds everywhere". With one call that
    copies the `subprocess.run` spelling for itself, that promise is void: whoever patches the entry
    as documented cannot catch it, and has to monkey-patch the stdlib's `subprocess` process-wide
    (taking a lock for it, and explaining "it has to go on the shared module").
    """

    def test_the_dag_ranking_goes_through_the_seam(self):
        """The child-process call that ranks the ready set goes through the seam: patching the
        entry's `_capture` catches it.

        `dag-manage list` is the package's one child-process call made to *see output* (an
        insertion's `add` is the other), and it used to carry a copy of the
        `subprocess.run` spelling of its own; this case pins that it goes through the seam.
        """
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.create_cycle('cyc')            # nothing in hand: a pick asks the ready set, which asks the ranking
        seen = []
        real = pipeline._capture

        def spy(argv, cwd):
            seen.append([str(a) for a in argv])
            return real(argv, cwd)

        with harness_dag_hook(sb.root), patched(pipeline, '_capture', spy):
            action = pipeline.plan(sb.root, 'cyc')

        self.assertEqual(action['action'], 'open_node', action)
        self.assertTrue([argv for argv in seen if Path(argv[1]).name == 'dag-manage.py'],
                        f'the ranking call did not go through the seam: {seen}')

    def test_no_logic_module_reaches_a_child_process_on_its_own(self):
        """No module in the package may start a child process on its own: the calls it starts itself
        are the ones the seam's patch cannot catch.

        This reads the source with comments and strings gone (the AST) rather than looking for
        literals -- a mention of `subprocess` in prose does not count, using it does.
        """
        offenders = []
        for path in sorted((CONDUCT_DIR / 'pipeline').glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            imported = {alias.name for node in ast.walk(tree)
                        if isinstance(node, (ast.Import, ast.ImportFrom))
                        for alias in node.names}
            if 'subprocess' in used | imported:
                offenders.append(path.name)
        self.assertEqual(offenders, [], f'these modules start a child process themselves, around the seam: {offenders}')


# ═══════════════════════════════════════════════════════════════════════════════
#  the insertion action: one vocabulary, one shape, and nothing left to run
# ═══════════════════════════════════════════════════════════════════════════════

#: An insertion source the cases below plug in themselves: one file, one offer, no core change. It
#: offers once per close and once per pick (the tag keeps it from offering twice), so the same
#: fixture serves both timings without depending on any shipped declaration.
FIXTURE_IMPL = '''
TAG = 'fixture:insertion-shape'


def offer(ctx):
    if ctx.nodes_with_tag(TAG):
        return None
    return {
        'title': 'Inserted by the fixture',
        'tags': [TAG],
        'spec_of': lambda node_id: f'# fixture\\n\\nwritten for {node_id}\\n',
        'fields': {'metric': 'fixture'},
        'why': 'the fixture measured 42, over its line',
        'note': 'the fixture asked for this node',
    }
'''


def fixture_decl(timing='close'):
    return {'name': 'fixture', 'summary': 'the insertion shape fixture', 'timing': timing,
            'action': 'insert', 'knobs': {}, 'readings': {}, 'metrics': {}}


def _places(value, key: str):
    """Every place `key` appears as a dict key, at any depth (the preview scrub is "anywhere")."""
    if isinstance(value, dict):
        if key in value:
            yield value
        for inner in value.values():
            yield from _places(inner, key)
    elif isinstance(value, list):
        for inner in value:
            yield from _places(inner, key)


class TestTheInsertionActionShape(Base):
    """`insert_node` is one action kind with one shape, whatever the source and whatever the timing.

    Three things are pinned, and they are the three a caller is handed:

      * the vocabulary: the action kinds are the entry's own table (`conduct_pipeline.ACTIONS`), and
        `insert_node` is one of them;
      * the planned shape: an action that has not run yet still carries the commands and the text
        builder (`apply` consumes them), but a PREVIEW must not hand any of that to a caller -- not
        at the top, and not inside each nested `insertions[*]`;
      * the walk: an insertion is where a `next` stops -- the pick's graph edit comes back as
        `insert_node`, the caller commits, and the next step dispatches -- so the caller always sees
        the graph edit before the session, and no single call grows the graph by a burst: one plugin
        answers with one node, and the calls in one step are the plugins'.
    """

    def fixture(self, sb, timing='close'):
        """Point the plugin scan at one hand-written folder for the test's whole life."""
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(plugins_dir(sb.root / 'plugins'))
        write_plugin(sb.root / 'plugins', 'fixture', fixture_decl(timing), FIXTURE_IMPL)
        return sb.root / 'plugins'

    def test_the_action_vocabulary_is_the_entrys_own_table(self):
        """The caller's list (`cases_base.ACTION_VOCABULARY`) and the tool's table are one list.

        Two copies drift silently: a caller that has not learned a kind the tool can return has no
        branch for it, and nothing raises until the day it happens.
        """
        self.assertEqual(set(ACTION_VOCABULARY), set(pipeline.ACTIONS),
                         'the caller vocabulary and the entry table disagree')
        self.assertIn(pipeline.INSERT_ACTION, pipeline.ACTIONS)
        self.assertEqual('insert_node', pipeline.INSERT_ACTION)

    def test_the_sources_reason_is_printed_as_written_and_the_landing_spot_is_the_cores(self):
        """The two lines a reader gets beside an insertion: `why` as the source wrote it, and where
        the node went as the core read it off the graph it had just edited.

        The second used to be the source's to state, which made the renderer depend on key names no
        contract named: a source that spelled them differently lost the line and nothing said so. The
        core knows where it put the node, so it is the core that says it.
        """
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done', {'deliverable': 'x'})
        self.fixture(sb)

        planned = pipeline.plan(sb.root, 'cyc', after=NODE, self_verified=True)
        text = pipeline.render_action(dict(planned), as_json=False, preview=True)

        self.assertIn('the fixture measured 42, over its line', text,
                      f'the source\'s own sentence is not printed as written: {text}')
        self.assertIn(f'Write code B  ({NODE2})', text,
                      f'the landing spot is not the core\'s reading of the graph: {text}')
        # ... and the free-shape dict still travels untouched: it is data, not a rendering instruction
        self.assertEqual({'metric': 'fixture'}, planned['insertions'][0]['fields'])

    def test_a_previewed_insertion_hands_no_runbook_to_the_caller(self):
        """`next --plan` on a close a plugin answers with an insertion: the JSON carries no
        `commands`, no `add_command` and no `spec_of` **anywhere**, at any depth.

        A preview is not executable -- that is the invariant `render_action` owns -- and an insertion
        carries its graph edits twice (mirrored at the top and once per insertion). Scrubbing only
        the top would leave the side door open; and `spec_of` is a callable, so a preview that kept
        it would not even serialize.
        """
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done', {'deliverable': 'x'})
        self.fixture(sb)

        planned = pipeline.plan(sb.root, 'cyc', after=NODE, self_verified=True)
        self.assertEqual('insert_node', planned['action'], planned)
        self.assertTrue(planned.get('commands'), 'the close action carries the edits that must run')
        self.assertTrue(planned['insertions'][0].get('add_command'),
                        'the nested copy lost the `add` the writer runs')

        text = pipeline.render_action(planned, as_json=True, preview=True)
        action = json.loads(text)

        found = [key for key in ('commands', 'add_command', 'spec_of')
                 for _ in _places(action, key)]
        self.assertEqual([], found, f'a previewed insertion still hands over {found}: {text}')
        self.assertTrue(action.get('settling'),
                        'a preview that did not run must report that it is still settling')
        # what a reader DOES need is still there, at both levels
        self.assertTrue(action['spec'])
        self.assertEqual(['Inserted by the fixture'], [i['title'] for i in action['insertions']])
        self.assertTrue(action['insertions'][0]['spec_path'])
        self.assertEqual({'metric': 'fixture'}, action['insertions'][0]['fields'])

    def test_a_pick_insertion_is_a_graph_edit_and_ends_the_step(self):
        """A pick-time source: the plan is an insertion that pins nothing, and the walk applies it.

        A pick has no close commands to run with it, so the graph's own growth is the only progress
        there is -- and the walk stops there and answers `insert_node`: the caller commits and
        pushes, and the step after it picks and dispatches. One plugin answers with one node, so the
        graph never grows by a burst from it.
        """
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.create_cycle('cyc', tags=['coding'])
        self.fixture(sb, timing='pick')

        planned = sb.plan()

        self.assertEqual('insert_node', planned['action'], planned)
        self.assertNotIn('prompt', planned, 'an insertion is not a dispatch')
        self.assertNotIn('prompt_text', planned)
        self.assertEqual([], sb.holding_ids(), 'the pick pinned a node before the insertion landed')
        self.assertEqual('active', sb.cycle()['status'])
        self.assertEqual(1, len(sb.dag()['nodes']), 'plan wrote the node it only promised')

        walked = sb.next()

        self.assertEqual('insert_node', walked['action'],
                         f'a bare next did not stop on the insertion itself: {walked}')
        nodes = sb.dag()['nodes']
        made = [uid for uid in nodes if uid != NODE]
        self.assertEqual(1, len(made), f'the fixture inserted no node: {nodes}')
        self.assertEqual([], nodes[made[0]]['dependencies'],
                         'a pick insertion has no parent: it is ready when it exists')
        self.assertTrue((sb.root / '.pf' / 'nodes' / made[0] / 'spec.md').is_file(),
                        'the inserted node has no text')

        dispatched = sb.next()

        self.assertEqual('dispatch', dispatched['action'], dispatched)
        self.assertEqual(NODE, dispatched['node_id'],
                         'the step after the insertion did not dispatch the picked node')
