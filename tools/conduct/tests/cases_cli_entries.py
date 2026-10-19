"""`conduct_pipeline.py`'s own entries, and the one file that builds the spawner's argv.

The module has two entry surfaces of its own -- `plan` / `apply` / `contract` as a process -- and the
first half drives them the shipped way against a throwaway project. The second half is the guard over
the shipped sources: exactly one file may spell `dsh-spawn.ps1`'s argv, because a second spelling
reads perfectly well on its own.

The state-manage surface (the CLI a session actually calls) is in `cases_cli.py`; the spawner scripts
as real processes are in `cases_spawn_shell.py`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE, NODE2  # noqa: E402
from harness import CONDUCT_DIR, PIPELINE_PATH, run_argv, shipped_sources  # noqa: E402

__all__ = ['TestPipelineModuleEntries', 'TestOneSpawner']


class TestPipelineModuleEntries(Base):
    """`conduct_pipeline.py` as a real process: the module's own entry points, driven the shipped way.

    These are not the state-manage surface: `plan` / `apply` / `contract` are how a launcher (and the
    module's own documented usage) reaches the pipeline, so their spelling, their encoding and their
    exit codes are part of what is under test.
    """

    def test_the_pipeline_module_prints_a_plan_as_json(self):
        """`conduct_pipeline.py plan` is the module's own read-only entry point.

        Driven as a real process with `--project`: this is the shipped CLI, so the encoding of its
        output and the exit code are part of what is under test.
        """
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        proc = self.pipeline_cli(sb, 'plan', '--project', sb.root, '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        action = json.loads(proc.stdout)
        self.assertEqual(action['action'], 'dispatch')
        self.assertIsNone(sb.read_signal('cyc', NODE), 'the plan wrote a signal')

    def test_contract_subcommand_prints_the_action_vocabulary(self):
        """`conduct_pipeline.py contract` is the reference a downstream agent reads to learn the action
        names it must handle."""
        proc = run_argv([PIPELINE_PATH, 'contract'], check=False)
        self.assertEqual(proc.returncode, 0,
                         f'conduct_pipeline.py contract failed:\n{proc.stdout}\n{proc.stderr}')
        for name in ('dispatch', 'open_node', 'maintenance_due', 'stop_pipeline', 'wait', 'no_cycle'):
            self.assertIn(name, proc.stdout)
        for gone in ('verify_round', 'open_round', 'stage'):
            self.assertNotIn(gone, proc.stdout)

    def test_the_two_front_ends_print_the_same_preview(self):
        """The same plan, printed the same by both front ends -- even "the preview cannot be run"
        follows the one implementation.

        The plan for an empty-handed line (nothing in hand) is `open_node`, and it carries the command
        that pins somebody: the preview only reports "still settling", both front ends have to report
        the same one, and neither carries `commands`.
        """
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.create_cycle('cyc')

        mine = self.pipeline_cli(sb, 'plan', '--project', sb.root, '--json')
        theirs = self.cli(sb, 'next', '--plan', '--json')

        self.assertEqual(mine.returncode, 0, mine.stderr)
        self.assertEqual(theirs.returncode, 0, theirs.stderr)
        action = json.loads(mine.stdout)
        self.assertEqual(action, json.loads(theirs.stdout))
        self.assertEqual(action['action'], 'open_node')
        self.assertTrue(action.get('settling'), 'the preview did not report "still settling"')
        self.assertNotIn('commands', action, 'the preview handed the commands over')

    def test_apply_is_the_settle_the_worker_asked_for(self):
        """`conduct_pipeline.py apply` is that one settle `pf next --after` performs: it reads the
        note in the slot in hand.

        This entry's own help says `--after` runs it, so both entries give the same answer for the
        same state: the slot in hand has filed its note, so it settles -- one step recorded in path,
        the DAG marked completed, and the line closed out by the stop rule.
        """
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.write_signal('cyc', NODE, 'completed', 'pf-conduct finished')

        proc = self.pipeline_cli(sb, 'apply', '--project', sb.root, '--json')

        self.assertEqual(proc.returncode, 0, proc.stderr)
        action = json.loads(proc.stdout)
        self.assertEqual(action['action'], 'stop_pipeline', action)
        self.assertEqual([step['result'] for step in sb.path()], ['completed'])
        self.assertEqual(sb.dag_status(NODE), 'completed')
        self.assertEqual(sb.cycle()['status'], 'inactive')

    def test_apply_names_the_node_it_settles(self):
        """With two slots in hand, `apply --after <node>` says which slot this settle is for.

        Without `--after` this entry cannot read whom to settle -- more than one slot is in hand, so
        that pass reports `wait` alone and writes not one byte; named, it is the same thing as
        `pf next --after`: one step recorded in path, the DAG marked completed.
        """
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.add_node(NODE2, 'another line', mode='auto')
        sb.start_cycle(parallel=2)
        sb.write_signal('cyc', NODE2, 'completed', 'pf-conduct finished')

        blind = self.pipeline_cli(sb, 'apply', '--project', sb.root, '--json')

        self.assertEqual(blind.returncode, 0, blind.stderr)
        self.assertEqual('wait', json.loads(blind.stdout)['action'], blind.stdout)
        self.assertEqual([], sb.path(), 'the pass that named nobody settled a slot')

        named = self.pipeline_cli(sb, 'apply', '--project', sb.root, '--after', NODE2, '--json')

        self.assertEqual(named.returncode, 0, named.stderr)
        self.assertEqual([step['node'] for step in sb.path()], [NODE2])
        self.assertEqual(sb.dag_status(NODE2), 'completed')

    def test_the_closing_answers_are_refused_without_a_preview(self):
        """A closing answer is refused where there is no question to settle -- the same rule as
        `pf next`.

        The refusal's reason: with no question to settle, taking the answer would make it look as if
        that word had been accepted -- the pipeline moved not one byte, yet the command reports
        success.
        """
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        before = sb.state()

        refused = self.pipeline_cli(sb, 'apply', '--project', sb.root, '--stop')

        self.assertEqual(refused.returncode, 2, refused.stdout)
        self.assertIn('closing call', refused.stderr)
        self.assertEqual(sb.state(), before, 'the refused command moved the state')

        theirs = self.cli(sb, 'next', '--stop')
        self.assertEqual(theirs.returncode, 2, theirs.stdout)
        self.assertIn('closing call', theirs.stderr)


class TestOneSpawner(Base):
    """One file builds `dsh-spawn.ps1`'s argv: every caller goes through it.

    What is pinned here is that there is one spelling of that argv, because a second spelling reads
    perfectly well on its own; that is exactly what is wrong with it.

    Behaviour first (the file that would have to hold a spawn call), then one unambiguous fingerprint
    over every shipped source file -- not a list of words that also turn up in prose, and not the
    spawner's own name, which belongs in the callers' comments.
    """

    ARGV_FINGERPRINT = '-CycleId'

    def test_handoff_cannot_spawn(self):
        """`handoff.py` writes a ticket and stops there: no spawner, no call, no argv. A session
        started before the node that closed it was committed and pushed is the failure this rules
        out; the planner produces no case that asks for one (a terminal ticket makes `record` answer
        `wait`).
        """
        source = (CONDUCT_DIR / 'handoff.py').read_text(encoding='utf-8')
        for gone in ('SPAWNER_SCRIPT', 'spawner_script', 'spawn_session', self.ARGV_FINGERPRINT):
            self.assertNotIn(gone, source, f'{gone} is back in handoff.py')

    def test_the_argv_is_built_in_exactly_one_file(self):
        """Every shipped source file, asked the same question: who builds that argv?"""
        owners = sorted(path.name for path in shipped_sources()
                        if self.ARGV_FINGERPRINT in path.read_text(encoding='utf-8'))
        self.assertEqual(owners, ['conduct_spawn.py'],
                         'the argv for dsh-spawn.ps1 is built in more than one place')