"""The shipped CLI driven as real processes, plus the failures an unattended run cannot recover from.

Every case here builds a throwaway project in a temp dir and runs the real `state-manage.py` against
it: the flag spelling, the rendering and the exit code are the subject, so nothing here can be
answered in-process. `conduct_pipeline.py`'s own entries are in `cases_cli_entries.py`; the spawner
scripts as real processes are in `cases_spawn_shell.py`.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE, pipeline  # noqa: E402
from harness import (  # noqa: E402
    STATE_MANAGE, WorkerSession, finish_node, patched, run_argv,
    _dag_hook as harness_dag_hook,
)

__all__ = ['TestCLIEntryPoints', 'TestRobustness']


class TestCLIEntryPoints(Base):
    """Tests whose subject is the shipped CLI itself: real processes, no shortcuts."""

    def test_bare_next_writes_and_plan_does_not(self):
        """`--plan` is read-only over the same decision; a bare `next` is what carries it out."""
        sb = self.make(cli=True)
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()

        planned = self.cli(sb, 'next', '--plan', '--json')
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual(json.loads(planned.stdout)['action'], 'dispatch')
        self.assertIsNone(sb.read_signal('cyc', NODE), 'the plan wrote a signal')

        applied = self.cli(sb, 'next', '--json')
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(json.loads(applied.stdout)['node_id'], NODE)
        self.assertEqual(sb.current_node(), NODE)

    def test_the_close_flags_reach_the_planner(self):
        """The closing call's levers are the CLI's `--after` plus the decision flags; driven as a
        process, since the flags are the subject. A flag the tool does not have is refused before
        anything is written.
        """
        sb = self.make(cli=True)
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        waiting = finish_node(sb)

        unknown = self.cli(sb, 'next', '--json', '--after', waiting['node_id'], '--verdict', 'x')
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn('unrecognized arguments', unknown.stderr)
        self.assertEqual(sb.path(), [])                   # the refusal closed nothing

        stopped = self.cli(sb, 'next', '--json', '--after', waiting['node_id'], '--stop',
                           '--note', 'wrapping up')
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(json.loads(stopped.stdout)['action'], 'stop_pipeline')
        self.assertEqual(sb.cycle()['status'], 'inactive')

    def test_cycle_hold_and_cycle_close_are_the_two_state_commands(self):
        """The CLI surface that pins a node and then closes it: `cycle hold`, `cycle close`.

        Driven as processes: these two verbs are what `conduct_pipeline` itself runs, so their spelling
        is the pipeline's contract with the machine.
        """
        sb = self.make(cli=True)
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.create_cycle('cyc')
        self.assertIsNone(sb.current_node())

        pinned = self.cli(sb, 'cycle', 'hold', 'cyc', NODE)
        self.assertEqual(pinned.returncode, 0, pinned.stderr)
        self.assertEqual(sb.current_node(), NODE)
        self.assertTrue(sb.cycle()['holding'][0]['since'])

        closed = self.cli(sb, 'cycle', 'close', 'cyc', NODE, '--result', 'completed')
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertIsNone(sb.current_node())
        step = sb.path()[0]
        self.assertEqual(step['node'], NODE)
        self.assertEqual(step['result'], 'completed')

    def test_the_rendered_output_says_what_the_action_is(self):
        """The human-facing rendering is what a session reads when it is not using --json."""
        sb = self.make(cli=True)
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        proc = self.cli(sb, 'next', '--plan')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('action: dispatch', proc.stdout)
        self.assertIn(NODE, proc.stdout)

    def test_the_rendered_ask_names_the_way_back_into_service(self):
        """A question with no visible answer is a dead end: the resume command lives in `hint`, so
        the rendering has to print it -- the reader who is not using `--json` is the one who needs it.
        """
        sb = self.make(cli=True)
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb._tool('cycle', 'status', 'cyc', 'inactive')

        proc = self.cli(sb, 'next', '--cycle', 'cyc', '--plan')

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('action: ask_user', proc.stdout)
        self.assertIn('cycle status cyc active', proc.stdout)

    def test_the_rendered_output_carries_what_needs_a_human(self):
        """The `attention` list has to survive the non-`--json` path too.

        A session that reads the rendering instead of the JSON would otherwise be back to the bug
        this list was added to fix: a node parked, stated nowhere it can see. The stop package is
        where the loose ends ride, so the close is what is driven here.
        """
        sb = self.make(cli=True)
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        WorkerSession(sb, fail_nodes={NODE}).work(sb.next())
        waiting = sb.next_cli()[0]
        self.assertEqual(waiting['action'], 'wait')

        proc = self.cli(sb, 'next', '--after', waiting['node_id'])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('attention: node_shelved', proc.stdout)
        self.assertIn('revive', proc.stdout)

    def test_the_reading_commands_report_the_node_in_hand(self):
        """`cycle current` / `cycle show` are how a human looks at a run.

        `cycle show` must answer before the node has reported anything: the cycle pins the node when
        the step OPENS, so the reading is available from the start.
        """
        sb = self.make(cli=True)
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        current = self.cli(sb, 'cycle', 'current')
        self.assertEqual(current.returncode, 0, current.stderr)
        self.assertIn(NODE, current.stdout)
        self.assertIn('Steps walked: 0', current.stdout)

        shown = self.cli(sb, 'cycle', 'show', 'cyc', NODE)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn(NODE, shown.stdout)
        self.assertIn('signal: (none)', shown.stdout)

        sb.write_signal('cyc', NODE, 'completed', 'done')
        after = self.cli(sb, 'cycle', 'show', 'cyc', NODE)
        self.assertIn('signal: completed', after.stdout)

    def test_next_help_documents_the_surface(self):
        proc = run_argv([STATE_MANAGE, 'next', '--help'], check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in ('--plan', '--after', '--continue', '--stop', '--node', '--spawn'):
            self.assertIn(flag, proc.stdout)
        for gone in ('--record', '--theme-verdict', '--decision-mode', '--verdict', '--round',
                     '--stage', '--launch-mode'):
            self.assertNotIn(gone, proc.stdout)
        # The contract body at the tail (the one source is `conduct_pipeline.ACTIONS`): it hangs off
        # the moment help is printed, so this reading pins both "help still carries it" and "the
        # short `-h` flag reached the same action".
        self.assertIn('the full set of actions', proc.stdout)
        self.assertIn('Invariant:', proc.stdout)

    def test_next_short_help_carries_the_contract_too(self):
        """`-h` and `--help` are the same action: the short one carries the contract body too."""
        proc = run_argv([STATE_MANAGE, 'next', '-h'], check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('the full set of actions', proc.stdout)


class TestRobustness(Base):
    """The failures an unattended run cannot recover from have to be loud and bounded."""

    def test_a_hung_child_fails_instead_of_blocking_forever(self):
        """No command may wait without a bound; a hung child is unrecoverable overnight.

        The bound is lowered to a few milliseconds for the test, so the child never actually gets
        to hang: what is under test is that `_run` enforces whatever `CMD_TIMEOUT` says, not that
        the shipped number is 120.
        """
        sb = self.make()
        old = pipeline.CMD_TIMEOUT
        pipeline.CMD_TIMEOUT = 0.05
        self.addCleanup(setattr, pipeline, 'CMD_TIMEOUT', old)
        started = time.perf_counter()
        with self.assertRaises(RuntimeError) as caught:
            pipeline._run([sys.executable, '-c', 'import time; time.sleep(30)'], sb.root)
        self.assertLess(time.perf_counter() - started, 10)
        self.assertIn('did not finish within 0.05s', str(caught.exception))

    def test_a_half_applied_action_says_so(self):
        """A command failing midway leaves the state half-applied; the error must say so.

        The closing call is the action with several commands in it (close the node, mark it in the
        DAG, park the cycle or pin the next node), so it is where a mid-way failure is reachable at
        all -- and the message has to say how far it got.
        """
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.next()                                 # dispatch: no commands, nothing to run
        sb.write_signal('cyc', NODE, 'completed', 'done', {'deliverable': 'x'})

        # Drive the close with the sandbox's own command runner, so the commands under test are the
        # real ones but no process is started per command.
        failure = {'on': None, 'n': 0}

        def exploding(cmd, cwd):
            failure['n'] += 1
            if failure['n'] == failure['on']:
                raise RuntimeError('command failed: boom')
            return sb._run_tool_command(cmd, cwd)

        with harness_dag_hook(sb.root), patched(pipeline, '_run', exploding):
            failure['on'], failure['n'] = 2, 0     # `cycle close` landed; the DAG mark blew up
            with self.assertRaises(RuntimeError) as caught:
                pipeline.apply(sb.root, 'cyc', self_verified=True)
        self.assertIn('command failed', str(caught.exception))
        self.assertIn('already ran', str(caught.exception))
        self.assertIn('half-applied', str(caught.exception))