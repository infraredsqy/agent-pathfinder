"""The spawner scripts driven as real processes: the shell half both hosts share, and DSH's own
failure beat.

`spawn/spawn-common.ps1` holds the three contracts both spawner scripts share -- the named parameters
both declare, the prompt check that refuses before anything is queued, and the failure marker's write
and clear. What is pinned here is that the shared half does its work on its own: pwsh starts, the
shell is dot-sourced, and the only thing it can reach is a marker file under the sandbox -- no
spawner script, no host call, no session.

`TestDSHSpawnFailures` drives `dsh-spawn.ps1` itself: a malformed base URL is the beat that breaks
BEFORE any call (the URL is judged as soon as it is read), so that run reaches no endpoint -- and the
marker still lands on disk.

`spawn/dsh-spawn.ps1`'s argv has one builder, and that guard is in `cases_cli_entries.py`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, normalise  # noqa: E402
from harness import CONDUCT_DIR, _fresh_module, shared_loader  # noqa: E402

__all__ = ['TestTheSpawnerShell', 'TestDSHSpawnFailures']

SPAWN_DIR = CONDUCT_DIR / 'spawn'
SPAWN_COMMON = SPAWN_DIR / 'spawn-common.ps1'

#: 壳侧写下的那个时刻该长成的样子：恒 6 位小数、字面 `Z`。判据落在形状上，不落在样本上 ——
#: 一个样本相等只说明这一次的两个值一样，形状漂到 `+00:00` 或短一位它也照样绿。
DETECTED_AT_SHAPE = r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$'


def _marker_module():
    """`spawn/marker.py`: 标记的落点与记录形状那一处正本（壳侧按同一份形状自己写）。"""
    return _fresh_module('pf_marker_case', SPAWN_DIR / 'marker.py')


def _clock():
    """`shared/clock.py`: 时刻形状的正本 —— 按路径从 `tools/shared/` 取，这里不抄格式字面量。"""
    return shared_loader().load_shared('clock')


def _ps(value) -> str:
    """一条 PowerShell 单引号字面量（值里的单引号翻倍）。"""
    return "'" + str(value).replace("'", "''") + "'"


class TestTheSpawnerShell(Base):
    """`spawn/spawn-common.ps1`: the half both spawners share, driven as a real process.

    The shell holds the three contracts both spawner scripts share -- the named parameters both
    declare, the prompt check that refuses before anything is queued, and the failure marker's write
    and clear.

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
        人话、改了 attempts 的记法，它当场判红。`detected_at` 的**形状**也钉在这里：上面那条等价
        断言会把每个 `_at` 字段折成 `<ts>`（`normalise`），对时刻的形状一个字都没说，所以壳侧写下
        的那个时刻自己判一次 —— 恒 6 位小数、字面 `Z`，并且能被 `clock.ISO_FMT` 读回来。
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
        moment = written['detected_at']
        self.assertRegex(moment, DETECTED_AT_SHAPE,
                         '壳侧写下的时刻不是 clock 的微秒形状：恒 6 位小数、字面 `Z`')
        self.assertIsNotNone(datetime.strptime(moment, _clock().ISO_FMT),
                             '壳侧写下的时刻 clock.ISO_FMT 读不回来')

    def test_a_failed_write_leaves_no_temporary_file_behind(self):
        """改名重试耗尽时盘上不留残渣：临时文件只在这一拍里有意义。

        读者此刻正开着目标文件（Windows 上普通 `open()` 不带 FILE_SHARE_DELETE），改名会一直被拒 ——
        四次退避用尽后这一拍只警告一声，标记仍是落盘的那一份，标记目录里不许剩下 `.tmp-<pid>`。
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


class TestDSHSpawnFailures(Base):
    """`spawn/dsh-spawn.ps1`：DSH 这一半自己的失败拍，真进程驱动。

    整条链的下一棒全押在这个脚本上，所以开不出会话的每一拍都要留下降级标记。基址不成形是这一半
    在**任何调用之前**就断掉的那一拍（URL 读到就判），所以这一趟碰不到任何端点 —— 而标记照样落盘。
    """

    CYCLE = 'dsh-base-case'

    def test_a_malformed_base_url_leaves_the_marker(self):
        if not shutil.which('pwsh'):
            self.skipTest('pwsh is not on PATH')
        sb = self.make()
        prompt = sb.root / 'prompt.txt'
        prompt.write_text('# /pf-conduct drill\n', encoding='utf-8')
        marker = _marker_module().marker_path(sb.root, self.CYCLE)
        proc = subprocess.run(['pwsh', '-NoProfile', '-File', str(SPAWN_DIR / 'dsh-spawn.ps1'),
                               '-CycleId', self.CYCLE, '-PromptFile', str(prompt),
                               '-WorkspacePath', str(sb.root), '-MarkerPath', str(marker),
                               '-WebUrl', 'http://:8080'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(marker.is_file(), '基址不成形那一拍没留下降级标记')
        record = json.loads(marker.read_text(encoding='utf-8'))
        self.assertEqual(record['cycle_id'], self.CYCLE)
        self.assertEqual(record['phase'], 'auth')