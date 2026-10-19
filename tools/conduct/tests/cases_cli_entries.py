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
        sb.add_node(NODE, '界面设计', mode='auto')
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
        """同一份计划，两个前端印出来的是同一份 —— 连「预览不可执行」也跟着同一份实现。

        空手的线（手里没有节点）那一问的计划是 `open_node`，它带着钉人的命令：预览只报「还在落
        定」，两个前端都得报同一份，而且都不带 `commands`。
        """
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.create_cycle('cyc')

        mine = self.pipeline_cli(sb, 'plan', '--project', sb.root, '--json')
        theirs = self.cli(sb, 'next', '--plan', '--json')

        self.assertEqual(mine.returncode, 0, mine.stderr)
        self.assertEqual(theirs.returncode, 0, theirs.stderr)
        action = json.loads(mine.stdout)
        self.assertEqual(action, json.loads(theirs.stdout))
        self.assertEqual(action['action'], 'open_node')
        self.assertTrue(action.get('settling'), '预览没报「还在落定」')
        self.assertNotIn('commands', action, '预览把命令交出来了')

    def test_apply_is_the_settle_the_worker_asked_for(self):
        """`conduct_pipeline.py apply` 就是 `pf next --after` 那一次结算：它读手里那格的条子。

        这条入口的说明写着 `--after` 跑的就是它，所以两个入口对同一份状态给出同一个结果：手里那格
        交了条子，它就结算 —— path 记一步、DAG 标 completed、线按停手规则收摊。
        """
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.write_signal('cyc', NODE, 'completed', 'pf-conduct 做完了')

        proc = self.pipeline_cli(sb, 'apply', '--project', sb.root, '--json')

        self.assertEqual(proc.returncode, 0, proc.stderr)
        action = json.loads(proc.stdout)
        self.assertEqual(action['action'], 'stop_pipeline', action)
        self.assertEqual([step['result'] for step in sb.path()], ['completed'])
        self.assertEqual(sb.dag_status(NODE), 'completed')
        self.assertEqual(sb.cycle()['status'], 'inactive')

    def test_apply_names_the_node_it_settles(self):
        """手里握着两格时，`apply --after <node>` 说出这次结算的是哪一格。

        不给 `--after` 时这条入口读不出该结算谁 —— 手里不止一格，所以那一趟只报 `wait`，一个字节
        都不写；点名之后与 `pf next --after` 是同一件事：path 记一步、DAG 标 completed。
        """
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '另一条支线', mode='auto')
        sb.start_cycle(parallel=2)
        sb.write_signal('cyc', NODE2, 'completed', 'pf-conduct 做完了')

        blind = self.pipeline_cli(sb, 'apply', '--project', sb.root, '--json')

        self.assertEqual(blind.returncode, 0, blind.stderr)
        self.assertEqual('wait', json.loads(blind.stdout)['action'], blind.stdout)
        self.assertEqual([], sb.path(), '没点名的那一趟结算了一格')

        named = self.pipeline_cli(sb, 'apply', '--project', sb.root, '--after', NODE2, '--json')

        self.assertEqual(named.returncode, 0, named.stderr)
        self.assertEqual([step['node'] for step in sb.path()], [NODE2])
        self.assertEqual(sb.dag_status(NODE2), 'completed')

    def test_the_closing_answers_are_refused_without_a_preview(self):
        """收尾的回答没有可结算的那一问时被拒 —— 与 `pf next` 同一条口径。

        拒绝的判据是：没有可结算的那一问时，照收会让人以为那句话被采纳了 —— 管线一个字节都没动，
        命令却报成功。
        """
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        before = sb.state()

        refused = self.pipeline_cli(sb, 'apply', '--project', sb.root, '--stop')

        self.assertEqual(refused.returncode, 2, refused.stdout)
        self.assertIn('closing call', refused.stderr)
        self.assertEqual(sb.state(), before, '被拒的那条命令动了状态')

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