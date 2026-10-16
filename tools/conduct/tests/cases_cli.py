"""The outside-facing surfaces: the shipped CLI entry points driven as real processes, the
failures an unattended run cannot recover from, the parity between the files that describe the
same pipeline, and the concepts the rewrite deleted (which must not come back through a side door).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import ACTION_VOCABULARY, Base, NODE, NODE2, normalise, pipeline, skill_of  # noqa: E402
from harness import (  # noqa: E402
    CONDUCT_DIR, PIPELINE_PATH, SIGNAL_KEYS, SKILLS, STATE_MANAGE, WorkerSession, finish_node,
    run_state_manage,
    _dag_hook as harness_dag_hook,
)
from harness_env import _fresh_module  # noqa: E402

__all__ = ['TestCLIEntryPoints', 'TestRobustness', 'TestOneSpawner', 'TestTheSpawnerShell']

SPAWN_DIR = CONDUCT_DIR / 'spawn'
SPAWN_COMMON = SPAWN_DIR / 'spawn-common.ps1'


def _marker_module():
    """`spawn/marker.py`: 标记的落点与记录形状那一处正本（壳侧按同一份形状自己写）。"""
    return _fresh_module('pf_marker_case', SPAWN_DIR / 'marker.py')


def _ps(value) -> str:
    """一条 PowerShell 单引号字面量（值里的单引号翻倍）。"""
    return "'" + str(value).replace("'", "''") + "'"


def _source_files():
    """Every Python file the tools ship (the tests are not among them).

    The guard at the end of this file asks which of these builds `dsh-spawn.ps1`'s argv, so the list
    is deliberately every source file rather than the one or two that do it today.
    """
    return ([CONDUCT_DIR / 'ledger-merge.py']
            + [path for cluster in ('pipeline', 'spawn')
               for path in sorted((CONDUCT_DIR / cluster).glob('*.py'))]
            + sorted((CONDUCT_DIR / 'conduct_pipeline_impl').glob('*.py'))
            + sorted((CONDUCT_DIR / 'conduct_state').glob('*.py'))
            + sorted((CONDUCT_DIR / 'ledger_merge').glob('*.py')))


class TestCLIEntryPoints(Base):
    """Tests whose subject is the shipped CLI itself: real processes, no shortcuts."""

    def cli(self, sb, *args):
        # PYTHONIOENCODING, or the tool's Chinese output goes out in the console codepage and the
        # parent loses the whole stream (see harness._child_env).
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        return subprocess.run([sys.executable, str(STATE_MANAGE), *args],
                              cwd=str(sb.root), capture_output=True, text=True, encoding='utf-8',
                              errors='replace', env=env)

    def pipeline_cli(self, sb, *args):
        """`conduct_pipeline.py` as a real process: the module's own entry, driven the shipped way."""
        return subprocess.run([sys.executable, str(PIPELINE_PATH), *[str(a) for a in args]],
                              capture_output=True, text=True, encoding='utf-8', errors='replace')

    def test_bare_next_writes_and_plan_does_not(self):
        """`--plan` is read-only over the same decision; a bare `next` is what carries it out."""
        sb = self.make(cli=True)
        sb.add_node(NODE, '界面设计', mode='auto')
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
        process, since the flags are the subject. A flag the tool no longer has is refused before
        anything is written.
        """
        sb = self.make(cli=True)
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        waiting = finish_node(sb)

        unknown = self.cli(sb, 'next', '--json', '--after', waiting['node_id'], '--verdict', 'x')
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn('unrecognized arguments', unknown.stderr)
        self.assertEqual(sb.path(), [])                   # the refusal closed nothing

        stopped = self.cli(sb, 'next', '--json', '--after', waiting['node_id'], '--stop',
                           '--note', '收工')
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(json.loads(stopped.stdout)['action'], 'stop_pipeline')
        self.assertEqual(sb.cycle()['status'], 'inactive')

    def test_cycle_hold_and_cycle_close_are_the_two_state_commands(self):
        """The CLI surface that pins a node and then closes it: `cycle hold`, `cycle close`.

        Driven as processes: these two verbs are what `conduct_pipeline` itself runs, so their spelling
        is the pipeline's contract with the machine.
        """
        sb = self.make(cli=True)
        sb.add_node(NODE, '界面设计', mode='auto')
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
        sb.add_node(NODE, '界面设计', mode='auto')
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
        sb.add_node(NODE, '界面设计', mode='auto')
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
        sb.add_node(NODE, '界面设计', mode='auto')
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
        sb.add_node(NODE, '界面设计', mode='auto')
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
        proc = subprocess.run([sys.executable, str(STATE_MANAGE), 'next', '--help'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in ('--plan', '--after', '--continue', '--stop', '--node', '--spawn'):
            self.assertIn(flag, proc.stdout)
        for gone in ('--record', '--theme-verdict', '--decision-mode', '--verdict', '--round',
                     '--stage', '--launch-mode'):
            self.assertNotIn(gone, proc.stdout)
        # 尾巴上那段合同正文（`conduct_pipeline.ACTIONS` 的正本）：它挂在打印 help 的那一刻，
        # 所以这条读数同时钉住「help 还带着它」与「`-h` 那条短旗标也走到了同一个动作」。
        self.assertIn('action 全集', proc.stdout)
        self.assertIn('不变量', proc.stdout)

    def test_next_short_help_carries_the_contract_too(self):
        """`-h` 与 `--help` 是同一条动作：短的那条也要带上合同正文。"""
        proc = subprocess.run([sys.executable, str(STATE_MANAGE), 'next', '-h'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('action 全集', proc.stdout)

    def test_the_pipeline_module_prints_a_plan_as_json(self):
        """`conduct_pipeline.py plan` is the module's own read-only entry point.

        Driven as a real process with `--project`: this is the shipped CLI, so the encoding of its
        output and the exit code are part of what is under test.
        """
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        proc = subprocess.run([sys.executable, str(PIPELINE_PATH), 'plan', '--project', str(sb.root),
                               '--json'], capture_output=True, text=True, encoding='utf-8',
                              errors='replace')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        action = json.loads(proc.stdout)
        self.assertEqual(action['action'], 'dispatch')
        self.assertIsNone(sb.read_signal('cyc', NODE), 'the plan wrote a signal')

    def test_contract_subcommand_prints_the_action_vocabulary(self):
        """`conduct_pipeline.py contract` is the reference a downstream agent reads to learn the action
        names it must handle."""
        proc = subprocess.run([sys.executable, str(PIPELINE_PATH), 'contract'],
                              capture_output=True, text=True, encoding='utf-8')
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
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.next()                                 # dispatch: no commands, nothing to run
        sb.write_signal('cyc', NODE, 'completed', 'done', {'deliverable': 'x'})

        # Drive the close with the sandbox's own command runner, so the commands under test are the
        # real ones but no process is started per command.
        real_run = pipeline._run
        failure = {'on': None, 'n': 0}

        def exploding(cmd, cwd):
            failure['n'] += 1
            if failure['n'] == failure['on']:
                raise RuntimeError('command failed: boom')
            return sb._run_tool_command(cmd, cwd)

        with harness_dag_hook(sb.root):
            pipeline._run = exploding
            try:
                failure['on'], failure['n'] = 2, 0     # `cycle close` landed; the DAG mark blew up
                with self.assertRaises(RuntimeError) as caught:
                    pipeline.apply(sb.root, 'cyc', self_verified=True)
            finally:
                pipeline._run = real_run
        self.assertIn('command failed', str(caught.exception))
        self.assertIn('already ran', str(caught.exception))
        self.assertIn('half-applied', str(caught.exception))

class TestOneSpawner(Base):
    """One file builds `dsh-spawn.ps1`'s argv: every caller goes through it.

    That argv used to be spelled out in more than one place, and the copies had already separated:
    two ceilings (180s and 300s) and two shapes of dry-run line. What is pinned here is the
    *absence* of a second spelling, because a second argv reads perfectly well on its own; that is
    exactly what is wrong with it.

    Behaviour first (the file that would have to hold a spawn call), then one unambiguous fingerprint
    over every shipped source file -- not a list of words that also turn up in prose, and not the
    spawner's own name, which belongs in the callers' comments.
    """

    ARGV_FINGERPRINT = '-CycleId'

    def test_handoff_cannot_spawn(self):
        """`handoff.py` writes a ticket and stops there: no spawner, no call, no argv.

        It used to carry a `dispatch` branch for a case the planner never produces (a terminal ticket
        makes `record` answer `wait`), so the branch could only ever have fired the wrong way: a
        session started before the node that closed it was committed and pushed.
        """
        source = (CONDUCT_DIR / 'pipeline' / 'handoff.py').read_text(encoding='utf-8')
        for gone in ('SPAWNER_SCRIPT', 'spawner_script', 'spawn_session', self.ARGV_FINGERPRINT):
            self.assertNotIn(gone, source, f'{gone} is back in handoff.py')

    def test_the_argv_is_built_in_exactly_one_file(self):
        """Every shipped source file, asked the same question: who builds that argv?"""
        owners = sorted(path.name for path in _source_files()
                        if self.ARGV_FINGERPRINT in path.read_text(encoding='utf-8'))
        self.assertEqual(owners, ['conduct_spawn.py'],
                         'the argv for dsh-spawn.ps1 is built in more than one place')


class TestTheSpawnerShell(Base):
    """`spawn/spawn-common.ps1`: the half both spawners share, driven as a real process.

    The shell holds the three contracts the two scripts used to copy row by row -- the named
    parameters both declare, the prompt check that refuses before anything is queued, and the
    failure marker's write and clear. Two copies of that shell had already drifted apart.

    What is pinned here is that the shared half does its work on its own: pwsh starts, the shell is
    dot-sourced, and the only thing it can reach is a marker file under the sandbox -- no spawner
    script, no host call, no session.
    """

    CYCLE = 'shell-case'

    def probe(self, sb, body, *, marker=True, prompt=True, workspace=True, python=True, env=None):
        """One pwsh process: dot-source the shell, initialise it, then run `body`.

        The knobs are the shell's own contract. `marker=False` leaves `-MarkerPath` out (the shell
        must refuse), `prompt=False` aims `-PromptFile` at a file that is not there, `python=False`
        runs with no `-PythonExe` and no PATH on the process.
        """
        prompt_file = sb.root / 'prompt.txt'
        if prompt:
            prompt_file.write_text('# /pf-conduct drill\n', encoding='utf-8')
        marker_file = _marker_module().marker_path(sb.root, self.CYCLE)
        init = ['Initialize-Spawner', '-CycleId', _ps(self.CYCLE), '-PromptFile', _ps(prompt_file)]
        if marker:
            init += ['-MarkerPath', _ps(marker_file)]
        if workspace:
            init += ['-WorkspacePath', _ps(sb.root)]
        if python:
            init += ['-PythonExe', _ps(sys.executable)]
        # utf-8-sig: Windows PowerShell reads a BOM-less file in the console codepage, and this one
        # carries the shell's own Chinese words.
        script = sb.root / 'shell-probe.ps1'
        script.write_text('\n'.join(['. ' + _ps(SPAWN_COMMON), ' '.join(init), *body]) + '\n',
                          encoding='utf-8-sig')
        proc = subprocess.run(['pwsh', '-NoProfile', '-File', str(script)],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=dict(os.environ) if env is None else env)
        return proc, marker_file

    def pwsh_or_skip(self):
        if not shutil.which('pwsh'):
            self.skipTest('pwsh is not on PATH')

    def test_the_shell_refuses_to_run_without_a_marker_path(self):
        """落点由调用方给：没有 `-MarkerPath`，外壳当场拒绝，绝不猜一个位置写下去。"""
        self.pwsh_or_skip()
        sb = self.make()
        proc, _ = self.probe(sb, marker=False,
                             body=["Write-CallbackFailureMarker -Phase 'x' -Reason 'y'"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('no -MarkerPath', proc.stderr)

    def test_a_missing_prompt_leaves_the_marker_the_pipeline_reads(self):
        """没有 prompt 就没有会话：写下降级标记再退出，记录的形状与正本一致。"""
        self.pwsh_or_skip()
        sb = self.make()
        proc, path = self.probe(sb, prompt=False, body=['Assert-PromptFile'])
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(path.is_file(), '这一拍没能把降级标记写下来')
        record = json.loads(path.read_text(encoding='utf-8'))
        marker = _marker_module()
        self.assertEqual(sorted(marker.record(self.CYCLE, 'read-prompt', 'x')), sorted(record))
        self.assertEqual(record['cycle_id'], self.CYCLE)
        self.assertEqual(record['phase'], 'read-prompt')
        self.assertEqual(record['workspace'], str(sb.root))

    def test_a_present_prompt_passes_the_check_without_a_marker(self):
        """prompt 在位时同一句检查安静通过：它问的是「文件在不在」。"""
        self.pwsh_or_skip()
        sb = self.make()
        proc, path = self.probe(sb, body=['Assert-PromptFile'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(path.exists())

    def test_the_first_failure_is_the_one_that_stays(self):
        """一次失败只落一条：第一次的那条是根因，后面的不再覆盖它。"""
        self.pwsh_or_skip()
        sb = self.make()
        proc, path = self.probe(sb, body=[
            "Write-CallbackFailureMarker -Phase 'first-phase' -Reason '根因'",
            "Write-CallbackFailureMarker -Phase 'second-phase' -Reason '后面的' -Attempts 3"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        record = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(record['phase'], 'first-phase')
        self.assertEqual(record['reason'], '根因')
        self.assertEqual(record['attempts'], 0)

    def test_clearing_takes_the_stale_marker_away(self):
        """链续上以后旧标记没有意义了：清掉它，清一个不存在的标记也不算失败。"""
        self.pwsh_or_skip()
        sb = self.make()
        marker = _marker_module()
        path = marker.marker_path(sb.root, self.CYCLE)
        marker.write(path, marker.record(self.CYCLE, 'old-phase', '上一次的事故'))
        proc, _ = self.probe(sb, body=['Clear-CallbackFailureMarker', 'Clear-CallbackFailureMarker'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(path.exists(), '链续上了，旧标记还躺在盘上')
        self.assertIn('cleared a stale callback failure marker', proc.stdout)

    def no_path(self):
        """一台没有 python 的机器的环境：同一个环境，去掉 PATH。"""
        return {key: value for key, value in os.environ.items() if key.upper() != 'PATH'}

    def test_the_marker_gets_written_with_no_python_on_the_machine(self):
        """壳侧写标记不经过 python：解释器不在的那台机器上，这条写入照样落盘。

        这是那一层自己写盘的唯一理由 —— spawner 的运行点可能没有 python，而 PowerShell 没法 import
        `marker.py`；这条写入又恰恰是链断掉之后唯一留给人的入口。清空 PATH、也不给 `-PythonExe`，
        标记仍要出现。
        """
        self.pwsh_or_skip()
        sb = self.make()
        proc, path = self.probe(sb, python=False, env=self.no_path(), body=[
            "Write-CallbackFailureMarker -Phase 'no-python' -Reason '解释器不在' -Attempts 2"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(path.is_file(), '没有 python 就没写出标记：这条写入又依赖解释器了')
        self.assertEqual('解释器不在', json.loads(path.read_text(encoding='utf-8'))['reason'])

    def test_the_shape_the_shell_writes_is_the_python_side_one(self):
        """两份实现（`marker.py` 与壳侧）长出同一份记录：同一组键、同一句人话、同一个落点。

        形状没法共享（理由见上一条），所以这条用例是防漂移的那一半：哪一边改了字段名、改了那句
        人话、改了 attempts 的记法，它当场判红。
        """
        self.pwsh_or_skip()
        sb = self.make()
        proc, path = self.probe(sb, python=False, env=self.no_path(), body=[
            "Write-CallbackFailureMarker -Phase 'shape-phase' -Reason '同一份记录' -Attempts 2"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        written = json.loads(path.read_text(encoding='utf-8'))
        expected = _marker_module().record(self.CYCLE, 'shape-phase', '同一份记录', attempts=2,
                                           workspace=sb.root)
        self.assertEqual(normalise(written), normalise(expected),
                         '壳侧那份记录与 marker.record() 漂开了')

    def test_a_failed_write_leaves_no_temporary_file_behind(self):
        """改名重试耗尽时盘上不留残渣：临时文件只在这一拍里有意义。

        读者此刻正开着目标文件（Windows 上普通 `open()` 不带 FILE_SHARE_DELETE），改名会一直被拒 ——
        四次退避用尽后这一拍只警告一声，标记还是原来那份，标记目录里不许剩下 `.tmp-<pid>`。
        """
        self.pwsh_or_skip()
        sb = self.make()
        marker = _marker_module()
        path = marker.marker_path(sb.root, self.CYCLE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"stale": true}', encoding='utf-8')
        held = open(path, 'r', encoding='utf-8')
        self.addCleanup(held.close)

        proc, _ = self.probe(sb, body=[
            "Write-CallbackFailureMarker -Phase 'held' -Reason '读者不松手'"])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual('{"stale": true}', path.read_text(encoding='utf-8'),
                         '这一拍把标记写下去了？读者还开着那份文件')
        self.assertEqual([], sorted(item.name for item in path.parent.glob('*.tmp-*')),
                         '重试耗尽后临时文件留在了标记目录里')

    def test_a_directory_on_the_marker_path_is_a_write_failure(self):
        """落点被同名目录占着时不许谎报成功。

        那种情况下 `Move-Item` 会把临时文件搬进目录里、还不报错 —— 于是这一拍会喊「已写下降级标记」，
        而工具要读的那个文件根本不在。这里当场认失败：警告里点名落点是目录，目录里什么都不留。
        """
        self.pwsh_or_skip()
        sb = self.make()
        marker = _marker_module()
        path = marker.marker_path(sb.root, self.CYCLE)
        path.mkdir(parents=True, exist_ok=True)

        proc, _ = self.probe(sb, body=[
            "Write-CallbackFailureMarker -Phase 'dir' -Reason '落点被占'"])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('落点是目录', proc.stdout + proc.stderr)
        self.assertTrue(path.is_dir(), '这一拍把占位子的那个目录动了')
        self.assertEqual([], sorted(item.name for item in path.iterdir()),
                         '这一拍把标记搬进了那个目录里')
        self.assertEqual([], sorted(item.name for item in path.parent.glob('*.tmp-*')),
                         '这一拍在标记目录里留下了临时文件')
