"""Tests for chain-spawn.py -- the generic one-session spawner behind skill-run chains.

No live fire: the seam the chain hands its session to (`chain-spawn._start`) is stood in front of, and
the dry-run path never reaches a spawner at all. Which spawner a host picks is `spawn/host.py`'s own
tested territory; here the choice is pinned through `DSH_CONDUCT_SPAWNER` -- the override
`default_spawner` answers first.

    python -m unittest cases_chain_spawn            # from this directory
"""
from __future__ import annotations

import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import patched  # noqa: E402

CONDUCT_DIR = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location('chain_spawn', CONDUCT_DIR / 'spawn' / 'chain-spawn.py')
chain_spawn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chain_spawn)

PROMPT = '/text-repair 4\n\n上一轮 r5：top 0.22 → 0.18，还剩 31 条。\n'
FAKE_SPAWNER = 'Z:/fake/spawner.ps1'
SPAWN_LINE = 'spawned: session session-fake (stage)'


class ChainCase(unittest.TestCase):
    """A project directory holding one chain prompt, the way text-repair lays it out."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='chain-spawn-test-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.project = self.root / 'project'
        self.prompt = self.project / '.pf' / 'repair' / 'chain' / 'next-r5.txt'
        self.prompt.parent.mkdir(parents=True)
        self.prompt.write_text(PROMPT, encoding='utf-8')

    def run_tool(self, *extra, line=SPAWN_LINE, error=None, real_door=False):
        """The real main(), with the door it starts a session through stood in front of.

        The stand-in records what it was handed and answers `line`, or raises `error`. `real_door`
        leaves the real one in place, for the case whose subject is the dry-run line itself. Returns
        (exit code, the recorded calls, stdout, stderr).

        `--workspace` is always named, defaulting to this case's own temp project: the tool's own
        default is the CURRENT DIRECTORY, so a case that left it out would aim the door at the
        checkout it was started from.
        """
        calls = []

        def start(workspace, cycle_id, prompt_file, dry_run):
            calls.append({'workspace': str(workspace), 'cycle_id': cycle_id,
                          'prompt_file': str(prompt_file), 'dry_run': dry_run})
            if error is not None:
                raise RuntimeError(error)
            return line

        if '--workspace' not in extra:
            extra = (*extra, '--workspace', str(self.project))
        out, err = StringIO(), StringIO()
        door = contextlib.nullcontext() if real_door else patched(chain_spawn, '_start', start)
        with door, mock.patch.dict(os.environ, {'DSH_CONDUCT_SPAWNER': FAKE_SPAWNER}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = chain_spawn.main(['--prompt-file', str(self.prompt), *extra])
        return code, calls, out.getvalue(), err.getvalue()


class TestSpawn(ChainCase):
    def test_the_prompt_the_cycle_and_the_workspace_reach_the_pipelines_door(self):
        code, calls, out, _ = self.run_tool('--workspace', str(self.project))

        self.assertEqual(0, code)
        self.assertEqual([{'workspace': str(self.project), 'cycle_id': 'next-r5',
                           'prompt_file': str(self.prompt), 'dry_run': False}], calls)
        self.assertEqual(SPAWN_LINE + '\n', out)

    def test_explicit_cycle_id_wins_over_the_stem(self):
        _, calls, _, _ = self.run_tool('--cycle-id', 'text-repair-r5')
        self.assertEqual('text-repair-r5', calls[0]['cycle_id'])

    def test_dry_run_prints_the_argv_and_starts_nothing(self):
        """`--dry-run` prints the argv the spawner WOULD have got, and starts nothing.

        Nothing here stands in front of the door: the real one prints the line. What proves no
        spawner ran is that `FAKE_SPAWNER` does not exist -- a call would raise, `main()` would
        report it on stderr and exit 1, and neither happens.
        """
        code, _, out, err = self.run_tool('--dry-run', real_door=True)

        self.assertEqual(0, code, err)
        self.assertIn('dry-run: pwsh', out)
        self.assertIn(str(Path(FAKE_SPAWNER)), out)
        self.assertIn('-CycleId next-r5', out)
        self.assertIn(str(self.prompt), out)


class TestFailure(ChainCase):
    def test_a_refused_spawn_exits_nonzero_with_the_spawners_words(self):
        code, _, _, err = self.run_tool(error='pwsh boom')

        self.assertEqual(1, code)
        self.assertIn('pwsh boom', err)
        self.assertIn('CONDUCT-CALLBACK-FAILED-next-r5.json', err,
                      '人工接手的入口要指得出来')
        self.assertIn(str(self.prompt), err)


if __name__ == '__main__':
    unittest.main(verbosity=2)