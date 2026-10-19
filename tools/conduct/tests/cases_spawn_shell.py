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

#: The shape of the moment the shell writes: always 6 fractional digits, a literal `Z`. The
#: judgement lands on the shape, not on a sample -- one sample matching only says these two values
#: were equal once; a shape that drifted to `+00:00` or lost a digit would pass all the same.
DETECTED_AT_SHAPE = r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$'


def _marker_module():
    """`spawn/marker.py`: the one source for where the marker lands and what its record looks like
    (the shell writes the same shape itself)."""
    return _fresh_module('pf_marker_case', SPAWN_DIR / 'marker.py')


def _clock():
    """`shared/clock.py`: the one source for the moment's shape -- loaded by path out of
    `tools/shared/`, so no format literal is copied here."""
    return shared_loader().load_shared('clock')


def _ps(value) -> str:
    """One PowerShell single-quoted literal (a single quote inside the value is doubled)."""
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
        # utf-8-sig: Windows PowerShell reads a BOM-less file in the console codepage, so a
        # non-ASCII path or value would be misread.
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
        """The landing place comes from the caller: with no `-MarkerPath` the shell refuses on the
        spot, and never guesses a place to write to."""
        self.pwsh_or_skip()
        sb = self.make()
        proc, _ = self.probe(sb, marker=False,
                             body=["Write-CallbackFailureMarker -Phase 'x' -Reason 'y'"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('no -MarkerPath', proc.stderr)

    def test_a_missing_prompt_leaves_the_marker_the_pipeline_reads(self):
        """No prompt means no session: write the degradation marker and exit, with the record's
        shape matching the one source."""
        self.pwsh_or_skip()
        sb = self.make()
        proc, path = self.probe(sb, prompt=False, body=['Assert-PromptFile'])
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(path.is_file(), 'this beat failed to write the degradation marker')
        record = json.loads(path.read_text(encoding='utf-8'))
        marker = _marker_module()
        self.assertEqual(sorted(marker.record(self.CYCLE, 'read-prompt', 'x')), sorted(record))
        self.assertEqual(record['cycle_id'], self.CYCLE)
        self.assertEqual(record['phase'], 'read-prompt')
        self.assertEqual(record['workspace'], str(sb.root))

    def test_a_present_prompt_passes_the_check_without_a_marker(self):
        """With the prompt in place the same check passes quietly: all it asks is whether the file
        is there."""
        self.pwsh_or_skip()
        sb = self.make()
        proc, path = self.probe(sb, body=['Assert-PromptFile'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(path.exists())

    def test_the_first_failure_is_the_one_that_stays(self):
        """One failure leaves one record: the first one is the root cause, and later ones do not
        overwrite it."""
        self.pwsh_or_skip()
        sb = self.make()
        proc, path = self.probe(sb, body=[
            "Write-CallbackFailureMarker -Phase 'first-phase' -Reason 'root cause'",
            "Write-CallbackFailureMarker -Phase 'second-phase' -Reason 'a later one' -Attempts 3"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        record = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(record['phase'], 'first-phase')
        self.assertEqual(record['reason'], 'root cause')
        self.assertEqual(record['attempts'], 0)

    def test_clearing_takes_the_stale_marker_away(self):
        """Once the chain is joined again the old marker means nothing: clear it, and clearing a
        marker that is not there is not a failure either."""
        self.pwsh_or_skip()
        sb = self.make()
        marker = _marker_module()
        path = marker.marker_path(sb.root, self.CYCLE)
        marker.write(path, marker.record(self.CYCLE, 'old-phase', 'the previous accident'))
        proc, _ = self.probe(sb, body=['Clear-CallbackFailureMarker', 'Clear-CallbackFailureMarker'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(path.exists(), 'the chain is joined again, yet the old marker is still on disk')
        self.assertIn('cleared a stale callback failure marker', proc.stdout)

    def no_path(self):
        """The environment of a machine with no python: this same environment with PATH taken out."""
        return {key: value for key, value in os.environ.items() if key.upper() != 'PATH'}

    def test_the_marker_gets_written_with_no_python_on_the_machine(self):
        """The shell's marker write does not go through python: on a machine with no interpreter
        that write still lands on disk.

        That is the only reason this layer writes its own disk -- the point a spawner runs on may
        have no python, and PowerShell cannot import `marker.py`; and this write is exactly the one
        door left to a human after the chain breaks. Empty PATH and no `-PythonExe`, and the marker
        must still appear.
        """
        self.pwsh_or_skip()
        sb = self.make()
        proc, path = self.probe(sb, python=False, env=self.no_path(), body=[
            "Write-CallbackFailureMarker -Phase 'no-python' -Reason 'no interpreter' -Attempts 2"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(path.is_file(), 'the no-python beat wrote no marker: this write needs python')
        self.assertEqual('no interpreter', json.loads(path.read_text(encoding='utf-8'))['reason'])

    def test_the_shape_the_shell_writes_is_the_python_side_one(self):
        """Two implementations (`marker.py` and the shell) grow the same record: the same key set,
        the same sentence, the same landing place.

        The shape cannot be shared (see the case above), so this case is the anti-drift half: if
        either side renames a field, changes that sentence, or changes how attempts is written, it
        goes red on the spot. `detected_at`'s **shape** is pinned here too: the equivalence assertion
        above folds every `_at` field to `<ts>` (`normalise`), which says nothing about the moment's
        shape, so the moment the shell wrote is judged on its own -- always 6 fractional digits, a
        literal `Z`, and readable back by `clock.ISO_FMT`.
        """
        self.pwsh_or_skip()
        sb = self.make()
        proc, path = self.probe(sb, python=False, env=self.no_path(), body=[
            "Write-CallbackFailureMarker -Phase 'shape-phase' -Reason 'the same record' -Attempts 2"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        written = json.loads(path.read_text(encoding='utf-8'))
        expected = _marker_module().record(self.CYCLE, 'shape-phase', 'the same record', attempts=2,
                                           workspace=sb.root)
        self.assertEqual(normalise(written), normalise(expected),
                         'the shell record drifted from marker.record()')
        moment = written['detected_at']
        self.assertRegex(moment, DETECTED_AT_SHAPE,
                         'the moment the shell wrote is not the microsecond shape clock defines')
        self.assertIsNotNone(datetime.strptime(moment, _clock().ISO_FMT),
                             'clock.ISO_FMT cannot read back the moment the shell wrote')

    def test_a_failed_write_leaves_no_temporary_file_behind(self):
        """When the rename retries run out no residue is left on disk: the temp file only means
        something inside this beat.

        A reader has the target file open right now (on Windows a plain `open()` carries no
        FILE_SHARE_DELETE), so the rename is refused again and again -- once the four backoffs are
        used up this beat only warns, the marker is still the one on disk, and no `.tmp-<pid>` may
        be left in the marker directory.
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
            "Write-CallbackFailureMarker -Phase 'held' -Reason 'the reader will not let go'"])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual('{"stale": true}', path.read_text(encoding='utf-8'),
                         'did this beat write the marker? the reader still has that file open')
        self.assertEqual([], sorted(item.name for item in path.parent.glob('*.tmp-*')),
                         'the temp file stayed in the marker directory after the retries ran out')

    def test_a_directory_on_the_marker_path_is_a_write_failure(self):
        """A directory of the same name on the landing place must not be reported as success.

        In that case `Move-Item` moves the temp file into the directory and reports no error -- so
        this beat would shout "the degradation marker was written" while the file the tool reads is
        not there at all. It fails on the spot here: the warning names the landing place as a
        directory, and nothing is left inside the directory.
        """
        self.pwsh_or_skip()
        sb = self.make()
        marker = _marker_module()
        path = marker.marker_path(sb.root, self.CYCLE)
        path.mkdir(parents=True, exist_ok=True)

        proc, _ = self.probe(sb, body=[
            "Write-CallbackFailureMarker -Phase 'dir' -Reason 'the landing place is taken'"])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('the landing place is a directory', proc.stdout + proc.stderr)
        self.assertTrue(path.is_dir(), 'this beat touched the directory that occupied the place')
        self.assertEqual([], sorted(item.name for item in path.iterdir()),
                         'this beat moved the marker into that directory')
        self.assertEqual([], sorted(item.name for item in path.parent.glob('*.tmp-*')),
                         'this beat left a temp file in the marker directory')


class TestDSHSpawnFailures(Base):
    """`spawn/dsh-spawn.ps1`: DSH's own failure beat, driven as a real process.

    The whole chain's next hand-off rests on this script, so every beat that cannot open a session
    must leave the degradation marker. A malformed base URL is the beat this half breaks on
    **before any call** (the URL is judged as soon as it is read), so that run reaches no endpoint --
    and the marker still lands on disk.
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
        self.assertTrue(marker.is_file(), 'the malformed-base-URL beat left no degradation marker')
        record = json.loads(marker.read_text(encoding='utf-8'))
        self.assertEqual(record['cycle_id'], self.CYCLE)
        self.assertEqual(record['phase'], 'auth')