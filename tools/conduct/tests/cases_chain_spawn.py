"""Tests for chain-spawn.py -- the generic one-session spawner behind skill-run chains.

Part of the suite's serial tail, for the same process-level-mock reason as the rest of it:
`test_conduct_pipeline.py` star-imports this and runs it AFTER the parallel pool. Alone:

    python -m unittest cases_chain_spawn            # from this directory

No live fire: the pwsh call is mocked at `subprocess.run` (the same seam the spawn cases use),
and the dry-run path never reaches it at all. Which spawner the host picks is `conduct_pipeline.py`'s
own tested territory; here the choice is pinned through `DSH_CONDUCT_SPAWNER` -- the override
`_default_spawner` answers first.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

CONDUCT_DIR = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location('chain_spawn', CONDUCT_DIR / 'spawn' / 'chain-spawn.py')
chain_spawn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chain_spawn)

PROMPT = '/text-repair 4\n\n上一轮 r5：top 0.22 → 0.18，还剩 31 条。\n'
FAKE_SPAWNER = 'Z:/fake/spawner.ps1'


class ChainCase(unittest.TestCase):
    """A project directory holding one chain prompt, the way text-repair lays it out."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='chain-spawn-test-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.project = self.root / 'project'
        self.prompt = self.project / '.pf' / 'repair' / 'chain' / 'next-r5.txt'
        self.prompt.parent.mkdir(parents=True)
        self.prompt.write_text(PROMPT, encoding='utf-8')

    def run_tool(self, *extra, done=None):
        """The real main(), with the pwsh call swapped for `done` (a successful spawn by default)."""
        done = done or subprocess.CompletedProcess(args=[], returncode=0, stderr='',
                                                   stdout='spawned: session session-fake (stage)\n')
        out, err = StringIO(), StringIO()
        with mock.patch('subprocess.run', return_value=done) as run, \
                mock.patch.dict(os.environ, {'DSH_CONDUCT_SPAWNER': FAKE_SPAWNER}), \
                mock.patch('sys.stdout', new=out), \
                mock.patch('sys.stderr', new=err):
            code = chain_spawn.main(['--prompt-file', str(self.prompt), *extra])
        return code, run, out.getvalue(), err.getvalue()


class TestSpawn(ChainCase):
    def test_spawns_the_prompt_through_the_named_spawner(self):
        code, run, out, _ = self.run_tool('--workspace', str(self.project))
        self.assertEqual(0, code)
        argv = run.call_args.args[0]
        self.assertEqual(['pwsh', '-NoProfile', '-File', str(Path(FAKE_SPAWNER))], argv[:4])
        self.assertEqual('next-r5', argv[argv.index('-CycleId') + 1])
        self.assertEqual(str(self.prompt), argv[argv.index('-PromptFile') + 1])
        self.assertEqual(str(self.project), argv[argv.index('-WorkspacePath') + 1])
        self.assertEqual('spawned: session session-fake (stage)\n', out)

    def test_explicit_cycle_id_wins_over_the_stem(self):
        _, run, _, _ = self.run_tool('--cycle-id', 'text-repair-r5')
        self.assertEqual('text-repair-r5', run.call_args.args[0][run.call_args.args[0].index('-CycleId') + 1])

    def test_dry_run_prints_the_argv_and_starts_nothing(self):
        with mock.patch.dict(os.environ, {'DSH_CONDUCT_SPAWNER': FAKE_SPAWNER}), \
                mock.patch('subprocess.run') as run, \
                mock.patch('sys.stdout', new=StringIO()) as out:
            code = chain_spawn.main(['--prompt-file', str(self.prompt), '--dry-run'])
        self.assertEqual(0, code)
        run.assert_not_called()
        self.assertIn('dry-run: pwsh', out.getvalue())
        self.assertIn(str(Path(FAKE_SPAWNER)), out.getvalue())


class TestFailure(ChainCase):
    def test_a_refused_spawn_exits_nonzero_with_the_spawners_words(self):
        refused = subprocess.CompletedProcess(args=[], returncode=3, stderr='pwsh boom', stdout='')
        code, _, _, err = self.run_tool(done=refused)
        self.assertEqual(1, code)
        self.assertIn('pwsh boom', err)
        self.assertIn('CONDUCT-CALLBACK-FAILED-next-r5.json', err,
                      '人工接手的入口要指得出来')
        self.assertIn(str(self.prompt), err)


if __name__ == '__main__':
    unittest.main(verbosity=2)
