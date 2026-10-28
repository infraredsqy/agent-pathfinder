"""The pieces in front of `pf`'s door: `pf.cmd` on Windows, the project root `doctor` names, and the
plugins `doctor` says are installed.

All three live in the pf layer, and what is measured is the rules of that layer:

  * `pf.cmd` hands the command it receives to `pf.py` as it is, exactly once: whichever interpreter
    it finds is the one used, and that interpreter's exit code is `pf`'s exit code. A non-zero exit
    especially has to hold -- `pf handoff` / `pf merge-ledger` are live-fire commands, and being run
    again by the fallback interpreter is a second shot.
  * in PowerShell the word `pf` is the `.ps1` shim, not the `.cmd` beside it, and the argv it was
    handed reaches the tool whole -- on both hosts, the one a skill session runs and Windows
    PowerShell 5.1. That is what the handoff line the skill writes down needs: cmd.exe hands a batch
    file the caller's rendering with its double quotes still in it (`%*` reads back
    `-o {"deliverable":"x y"}`), but with the argument boundaries already cut (`%1` and `"%~1"` are
    `-o`), and the CRT parsing `python.exe`'s command line eats a quote as a delimiter -- so
    `pf handoff ... -o '{"deliverable":"…"}'` reached the tool as `{deliverable:…}`. The probed
    interpreter, the single run and the exit code hold here too.
  * the project root `doctor` reports is the same root the tools it forwards (`pf dag` / `pf issue`)
    read the ledger by: cwd is the root, the one exception being that a linked worktree goes back to
    the main tree. That the two readings agree is what these cases prove.
  * `doctor`'s `plugins:` section: what the scan found, how each one works (its own summary and the
    knobs it declares), the dedupe tags and methods each plugin shows the outside, zero installed for
    an empty plugins directory, a declaration problem counted
    in the exit code, a declaration that claims a built-in setting's name (reported by the one door
    that sees both tables, with the built-in cell still in force), and an `impl.py` that cannot even
    compile (the doctor loads each installed one once, so a broken implementation is named here and
    not discovered at the next close). The scan is pointed at a throwaway plugins directory, so
    these cases say nothing about which plugins this checkout happens to ship -- an empty
    `plugins/` is a legal tree and the suite stays green with it.
  * the plugins `doctor` says are installed (`TestDoctorPlugins`), and the words those plugins
    contribute (`TestPluginCommands`): a folder's declaration may claim a word, `--help` prints it and
    the router hands the rest of the line to that folder's implementation, while a declaration that is
    broken, an implementation that is missing, a folder that was deleted, and a word the core already
    answers each lose the word rather than the door.
  * `help` and `--help` print the same body (the parser's own), and the command list is not copied a
    second time: the listing and the router are both grown from the entry's one routing table.

Every case builds its own thing in a temp dir (a fake `pf.py` plus a real `pf.cmd`, a throwaway git
repository plus one worktree, or a project plus a plugins directory), runs the real CLI and cleans up
afterwards; no real project is touched, no session is opened, and every command stays inside the 5 s
budget.

    python conduct/tests/cases_bin.py            # this file on its own
    python conduct/tests/test_conduct_pipeline.py    # with the whole suite (registration is in that
                                                     file's import section)
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (GIT, TOOLS_DIR, _child_env, _run_command, git, init_markers, requires_plugin,
                     run_argv, write_skill)

__all__ = ['TestPfCmdShim', 'TestPfShimInPowerShell', 'TestPfShimInWindowsPowerShell',
           'TestDoctorRoot', 'TestDoctorPlugins', 'TestPluginCommands', 'TestCommandList']

PYTHON = sys.executable
BIN = TOOLS_DIR / 'bin'
PF = BIN / 'pf.py'
SHIM = BIN / 'pf.cmd'
CMD = os.environ.get('COMSPEC', 'cmd.exe')

#: The shim PowerShell takes instead, when it sits beside `pf.cmd`: a bare word resolves to the `.ps1`
#: first, in both PS hosts. It is what carries an argument with a double quote in it whole -- the whole
#: of the handoff line's JSON.
PS1 = BIN / 'pf.ps1'
PWSH = shutil.which('pwsh')

#: The other host that takes the `.ps1`: Windows PowerShell 5.1, which does not say how it renders a
#: native command's arguments and so renders them the way cmd.exe does. The shim builds the command line
#: itself there, which is why this host is measured rather than assumed from the `pwsh` run.
WINDOWS_POWERSHELL = shutil.which('powershell')

#: The method the default fixture declaration names, and the skill folder the fixture writes beside it.
#: A fixture writes its own skill instead of borrowing the name of one this checkout happens to ship:
#: the skill lives in the plugin folder, so the two are one thing to write.
FIXTURE_METHOD = 'pf-fixture-skill'

#: A fake `pf.py`: every run appends one line to `runs.txt` (recording the argv it received) and then
#: exits with `FAKE_PF_EXIT`. One fake is enough: these cases measure "how many times it ran, and
#: whose exit code it is" -- what `pf.py` itself does is the real one's business.
FAKE_PF = '''\
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, 'runs.txt'), 'a', encoding='utf-8') as fh:
    fh.write(' '.join(sys.argv[1:]) + chr(10))
sys.exit(int(os.environ.get('FAKE_PF_EXIT', '0')))
'''

#: A second fake `pf.py`, for the cases that read argument boundaries: one JSON array per run, so one
#: argument carrying a space cannot be read back as two (the space-joined line above cannot tell). It
#: stands at the same door -- whatever the shim hands over is what it records, and its code is the
#: command's.
FAKE_PF_ARGV = '''\
import json
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, 'runs.txt'), 'a', encoding='utf-8') as fh:
    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + chr(10))
sys.exit(int(os.environ.get('FAKE_PF_EXIT', '0')))
'''

#: A third fake `pf.py`: the same one-JSON-array-per-run record as `FAKE_PF_ARGV`, plus one line on
#: stdout. Whether the host's own capture of a call still sees the child is read off that line, so the
#: fixture that measures it needs a child with something to say.
FAKE_PF_TICKET = '''\
import json
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, 'runs.txt'), 'a', encoding='utf-8') as fh:
    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + chr(10))
print('TICKET-LINE')
sys.exit(int(os.environ.get('FAKE_PF_EXIT', '0')))
'''

#: A fake `py` (the Python launcher): records the whole command the shim handed it, then hands the
#: part after `-3 -X utf8` to `FAKE_PYTHON`. This one measures "which branch is taken when
#: `where python` finds nothing, and what is handed over".
PY_STANDIN = '''\
@echo off
setlocal
>>"%~dp0py-runs.txt" echo %*
shift
shift
shift
"%FAKE_PYTHON%" -X utf8 "%1" %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%
'''


def pf(*args, cwd):
    """The real `pf` entry, as a real process, run from `cwd` -- the forwarding layer and the shim in front of it share this one."""
    return run_argv([PF, *[str(a) for a in args]], cwd=cwd, check=False)


@unittest.skipUnless(os.name == 'nt', 'pf.cmd is the Windows shim')
class TestPfCmdShim(unittest.TestCase):
    """`pf.cmd`: one command runs exactly once, and the exit code goes out as it came in."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-cmd-')
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # A real shim plus a fake pf.py: the shim looks for the `pf.py` beside itself, so copying it
        # into temp is enough to drive the whole thing.
        shutil.copyfile(SHIM, self.dir / 'pf.cmd')
        (self.dir / 'pf.py').write_text(FAKE_PF, encoding='utf-8')
        self.runs_file = self.dir / 'runs.txt'

    # ── helpers ──────────────────────────────────────────────────────────────
    def runs(self):
        """Every run of the fake pf.py, in order, as the argv it received."""
        if not self.runs_file.exists():
            return []
        return self.runs_file.read_text(encoding='utf-8').splitlines()

    def shim(self, *args, exit_code='0', env=None):
        environ = _child_env() if env is None else env
        environ['FAKE_PF_EXIT'] = exit_code
        return _run_command([CMD, '/c', str(self.dir / 'pf.cmd'), *[str(a) for a in args]],
                            cwd=self.dir, env=environ, check=False)
    # ── cases ────────────────────────────────────────────────────────────────
    def test_a_zero_exit_runs_the_command_once(self):
        proc = self.shim('version')
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(['version'], self.runs())

    def test_a_nonzero_exit_runs_the_command_once_and_keeps_its_code(self):
        """A non-zero exit: the fallback interpreter must not run it again, and the exit code must
        not change hands (two handoffs are two shots)."""
        proc = self.shim('issue', 'top', exit_code='3')
        self.assertEqual(3, proc.returncode, proc.stderr)
        self.assertEqual(['issue top'], self.runs())

    def test_no_interpreter_on_path_runs_nothing(self):
        """No interpreter on PATH: say so and stop, the exit code is the shim's own 127, and nothing
        runs at all."""
        env = _child_env()
        env['PATH'] = ''
        proc = self.shim('doctor', env=env)
        self.assertEqual(127, proc.returncode)
        self.assertEqual([], self.runs(), 'no interpreter was found, so nothing may run')
        self.assertIn('python', proc.stderr)

    def test_the_py_launcher_is_the_fallback_that_runs_it(self):
        """`where python` finds nothing and `where py` finds it: the py branch is taken, the command
        still runs once, and the exit code is still the command's.

        `System32` is left on PATH because `where` needs it: with PATH emptied entirely `where`
        itself cannot start, and what is measured then is whether the probe is there at all, not the
        interpreter.
        """
        (self.dir / 'py.cmd').write_text(PY_STANDIN, encoding='utf-8')
        env = _child_env()
        env['PATH'] = os.pathsep.join([str(Path(os.environ['SystemRoot']) / 'System32'),
                                       str(self.dir)])
        env['FAKE_PYTHON'] = PYTHON
        proc = self.shim('issue', 'top', exit_code='3', env=env)

        self.assertEqual(3, proc.returncode, proc.stderr)
        self.assertEqual(['issue top'], self.runs(), 'the py branch must run exactly once too')
        handed = (self.dir / 'py-runs.txt').read_text(encoding='utf-8')
        self.assertIn('-3', handed, f'the whole command the shim handed to py: {handed!r}')
        self.assertIn('-X utf8', handed)
        self.assertIn('pf.py', handed)


class _PfShimCases:
    """In PowerShell the word `pf` is the `.ps1` shim -- and the argv it was handed reaches the tool
    whole.

    A bare word resolves to the `.ps1` before the `.cmd` beside it, and that is what the handoff line
    the skill writes down needs: cmd.exe hands a batch file the caller's rendering with its double
    quotes still in it, but with the argument boundaries already cut, and the CRT parsing the target's
    command line eats a quote as a delimiter -- so `pf handoff ... -o '{"deliverable":"…"}'` reached
    `pf.py` as `{deliverable:…}`. The shim's own contract is measured here the way the cases above
    measure `pf.cmd`'s: probed interpreter, one run, the command's exit code.

    The host is the subclass's `HOST`: the two hosts that resolve the `.ps1` render a native command's
    arguments differently, so each is measured rather than taken for the other.
    """

    #: The PowerShell host these cases are run in, named by the subclass.
    HOST = None

    #: The line the skill writes down, and the argv it names: a note with a space, and a JSON payload
    #: carrying double quotes and a space inside a value.
    PAYLOAD = '{"deliverable":"x y","files":["a b"]}'
    DOC_LINE = "pf handoff cycle-1 node-1 completed -s 'the note' -o '" + PAYLOAD + "'"
    DOC_ARGV = ['handoff', 'cycle-1', 'node-1', 'completed', '-s', 'the note', '-o', PAYLOAD]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-shim-ps-')
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # Both real shims into temp, so which one PowerShell takes is this case's own reading; the
        # fake `pf.py` records the argv, so nothing here depends on the real tool.
        shutil.copyfile(SHIM, self.dir / 'pf.cmd')
        shutil.copyfile(PS1, self.dir / 'pf.ps1')
        (self.dir / 'pf.py').write_text(FAKE_PF_ARGV, encoding='utf-8')

    # ── helpers ──────────────────────────────────────────────────────────────
    def runs(self, directory=None):
        """Every run of the fake pf.py, in order, as the argv it received -- boundaries exact."""
        path = (self.dir if directory is None else directory) / 'runs.txt'
        if not path.exists():
            return []
        return [json.loads(line)
                for line in path.read_text(encoding='utf-8').splitlines()]

    def shim(self, command, exit_code='0', env=None, path=None, shim_dir=None):
        """One `HOST` process running `command`, this case's directory first on PATH.

        `path` replaces the whole PATH: that is how "no python stands on it" is built, and System32 is
        left in it for the same reason the `pf.cmd` cases leave it in. `shim_dir` replaces "this case's
        directory" as the fixture the bare word resolves to -- the case that measures a checkout path
        carrying a space builds its fixture one directory down, inside such a name.
        """
        fixture = self.dir if shim_dir is None else shim_dir
        environ = _child_env() if env is None else env
        environ['FAKE_PF_EXIT'] = exit_code
        if path is None:
            environ['PATH'] = os.pathsep.join([str(fixture), environ['PATH']])
        else:
            environ['PATH'] = path
        return _run_command([self.HOST, '-NoProfile', '-Command', command], cwd=fixture,
                            env=environ, check=False)

    def bare_path(self):
        """The shim directory and System32, and nothing else: no python stands on this PATH."""
        return os.pathsep.join([str(self.dir), str(Path(os.environ['SystemRoot']) / 'System32')])

    # ── cases ────────────────────────────────────────────────────────────────
    def test_power_shell_takes_the_ps1_shim_and_the_doc_line_arrives_whole(self):
        """The bare word on PATH, the way the skill writes it: the doc line's JSON arrives at the tool
        with its own double quotes, and the shim PowerShell took for it is the `.ps1`."""
        proc = self.shim(self.DOC_LINE)

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual([self.DOC_ARGV], self.runs(),
                         'an argument of the doc line did not arrive as it was written')

        found = self.shim('(Get-Command pf).Source')
        self.assertEqual(0, found.returncode, found.stderr)
        self.assertTrue(found.stdout.strip().lower().endswith('pf.ps1'),
                        f'PowerShell did not take the .ps1 shim: {found.stdout.strip()!r}')

    def test_an_empty_argument_arrives_as_an_empty_argument(self):
        """An empty argument is an argument: the entry is in the argv, and it is empty. Windows
        PowerShell 5.1 renders one as nothing at all, so it is one of the shapes the shim does not
        leave to the host."""
        proc = self.shim("pf keep '' tail")

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual([['keep', '', 'tail']], self.runs(),
                         'the empty argument did not arrive as an argument')

    def test_a_checkout_path_carrying_a_space_still_leaves_the_caller_its_own_streams(self):
        """The fixture one directory down, under a name carrying a space -- the checkout shape where
        the shim's own `-X utf8 <pf.py>` prefix is whitespace-carrying too.

        That prefix is the shim's business, not an argument of the call, so an ordinary call (none of
        the three shapes that force the built line) is still handed to `&` and `$x = pf ...` sees the
        child. Counting the prefix would put every call of such a checkout on the built line, where
        the host's capture goes blind: the marker would land on the console and `$x` would come back
        empty. The host that decides this is Windows PowerShell 5.1 -- `pwsh` names its renderer and
        takes `&` either way -- so this case runs in both and reads sharpest in one.

        The doc line, whose own arguments are what force the built line, has to stay whole here too:
        that reading is what keeping the branch buys, and it may not be traded away for the capture.
        """
        fixture = self.dir / 'dir with space'
        fixture.mkdir()
        shutil.copyfile(SHIM, fixture / 'pf.cmd')
        shutil.copyfile(PS1, fixture / 'pf.ps1')
        (fixture / 'pf.py').write_text(FAKE_PF_TICKET, encoding='utf-8')

        proc = self.shim('$x = pf handoff --form zzz; "CAPTURED=[$x]"', shim_dir=fixture)

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn('CAPTURED=[TICKET-LINE]', proc.stdout,
                      "the host's own capture did not see an ordinary call's child")
        self.assertEqual([['handoff', '--form', 'zzz']], self.runs(fixture))

        (fixture / 'runs.txt').unlink(missing_ok=True)
        doc = self.shim(self.DOC_LINE, shim_dir=fixture)

        self.assertEqual(0, doc.returncode, doc.stderr)
        self.assertEqual([self.DOC_ARGV], self.runs(fixture),
                         'the doc line did not arrive whole from a path carrying a space')

    def test_the_ps1_shim_runs_the_command_once_and_keeps_its_code(self):
        """A non-zero exit: the fallback interpreter must not run it again, and the exit code must not
        change hands -- `pf.cmd`'s contract, kept by the shim PowerShell actually takes."""
        proc = self.shim('pf issue top; exit $LASTEXITCODE', exit_code='3')

        self.assertEqual(3, proc.returncode, proc.stderr)
        self.assertEqual([['issue', 'top']], self.runs())

    def test_no_python_leaves_the_py_launcher_as_the_one_interpreter(self):
        """`where python` finds nothing and the launcher is there: the py branch is taken with `-3 -X
        utf8`, the command still runs once, and the exit code is still the command's."""
        (self.dir / 'py.cmd').write_text(PY_STANDIN, encoding='utf-8')
        env = _child_env()
        env['FAKE_PYTHON'] = PYTHON
        proc = self.shim('pf issue top; exit $LASTEXITCODE', exit_code='3', env=env,
                         path=self.bare_path())

        self.assertEqual(3, proc.returncode, proc.stderr)
        self.assertEqual([['issue', 'top']], self.runs(), 'the py branch must run exactly once too')
        handed = (self.dir / 'py-runs.txt').read_text(encoding='utf-8')
        self.assertIn('-3', handed, f'the whole command the shim handed the launcher: {handed!r}')
        self.assertIn('-X utf8', handed)
        self.assertIn('pf.py', handed)

    def test_no_interpreter_on_path_says_so_and_runs_nothing(self):
        """No interpreter at all: the shim says so and stops with its own 127, and nothing runs."""
        proc = self.shim('pf doctor; exit $LASTEXITCODE', path=self.bare_path())

        self.assertEqual(127, proc.returncode, f'{proc.stdout}\n{proc.stderr}')
        self.assertEqual([], self.runs(), 'no interpreter was found, so nothing may run')
        self.assertIn('python', proc.stderr)


@unittest.skipUnless(os.name == 'nt', 'the two shims in front of `pf` are the Windows ones')
@unittest.skipUnless(PWSH, 'pwsh is the host a skill session runs `pf` in')
class TestPfShimInPowerShell(_PfShimCases, unittest.TestCase):
    """The `.ps1` shim as `pwsh` takes it: the argv it was handed reaches the tool whole, once, with
    the command's exit code."""

    HOST = PWSH


@unittest.skipUnless(os.name == 'nt', 'the two shims in front of `pf` are the Windows ones')
@unittest.skipUnless(WINDOWS_POWERSHELL, 'Windows PowerShell is the other host that takes the `.ps1`')
class TestPfShimInWindowsPowerShell(_PfShimCases, unittest.TestCase):
    """The same cases on Windows PowerShell 5.1, whose own rendering of a native command's arguments is
    not the CRT's: the doc line is measured on that host too -- the reading that was missing when the
    JSON lost its quotes inside it."""

    HOST = WINDOWS_POWERSHELL


@unittest.skipUnless(GIT, 'git is not on PATH')
class TestDoctorRoot(unittest.TestCase):
    """`doctor`'s project root is the same as the tools it forwards: inside a worktree both go back
    to the main tree."""

    UID = '11111111-2222-3333-4444-555555555555'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-doctor-')
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.main = base / 'main'
        self.main.mkdir()
        git(self.main, 'init', '-b', 'main')

        ledger = init_markers(self.main)
        (ledger / 'nodes' / self.UID).mkdir(parents=True)
        (ledger / 'AGENTS.md').write_text('# ledger\n', encoding='utf-8')
        (ledger / 'ideas.json').write_text(json.dumps({'nodes': {self.UID: {
            'title': 'visible even across a worktree', 'status': 'todo', 'mode': 'auto',
            'dependencies': []}}}, ensure_ascii=False, indent=2), encoding='utf-8')
        (ledger / 'nodes' / self.UID / 'spec.md').write_text('# spec\n', encoding='utf-8')
        # The merge driver is written the registered way (relative form): the only check left is
        # "is the root right".
        git(self.main, 'config', 'merge.pf.driver', 'pf merge-ledger %O %A %B %P')

        # `.pf/` is not in the repository: a worktree is a full checkout, but only of what was
        # committed -- so the worktree has no ledger, and walking up for a `.git` directory does not
        # find the main tree either (this worktree is a sibling directory).
        (self.main / 'README.md').write_text('# main\n', encoding='utf-8')
        git(self.main, 'add', 'README.md')
        git(self.main, 'commit', '-m', 'seed', '--no-gpg-sign')
        self.wt = base / 'wt'
        git(self.main, 'worktree', 'add', str(self.wt), '-b', 'pf/wt')

    # ── helpers ──────────────────────────────────────────────────────────────
    def reported_root(self, proc):
        """The project-root line `doctor` prints, read back."""
        line = next(l for l in proc.stdout.splitlines() if l.startswith('project root: '))
        return Path(line.split(': ', 1)[1].strip())

    def dag_root(self, cwd):
        """The root in the absolute path `pf dag open` prints -- the root the forwarded tool really
        knows.

        `<root>/.pf/nodes/<id>/spec.md`, four levels up, is it.
        """
        proc = pf('dag', 'open', self.UID, cwd=cwd)
        self.assertEqual(0, proc.returncode, proc.stderr)
        return Path(proc.stdout.strip()).parents[3]

    # ── cases ────────────────────────────────────────────────────────────────
    def test_doctor_and_the_tool_it_fronts_share_the_root_in_a_worktree(self):
        """Inside a worktree: the root doctor reports is the root `pf dag` read the node by."""
        self.assertTrue(self.dag_root(self.wt).samefile(self.main),
                        'pf dag did not go back to the main tree -- this case\'s premise does not hold')
        doctor = pf('doctor', cwd=self.wt)
        self.assertTrue(self.reported_root(doctor).samefile(self.main),
                        f'doctor reported a root that is not the main tree:\n{doctor.stdout}')
        self.assertTrue(self.reported_root(doctor).samefile(self.dag_root(self.wt)),
                        'doctor and pf dag do not know the same root')
        self.assertEqual(0, doctor.returncode, doctor.stdout)

    def test_the_two_roots_agree_inside_the_project_too(self):
        """In the main tree: the two agree just the same (the go-back exception does not affect the
        ordinary path)."""
        self.assertTrue(self.reported_root(pf('doctor', cwd=self.main)).samefile(
            self.dag_root(self.main)))
        self.assertTrue(self.reported_root(pf('doctor', cwd=self.main)).samefile(self.main))


class TestDoctorPlugins(unittest.TestCase):
    """`doctor`'s `plugins:` section: what the scan found, and what a declaration's problems cost.

    `PF_PLUGINS_DIR` is pointed at a throwaway directory in every case, so what is asserted is the
    doctor's own reporting -- never which plugins this checkout ships. The project is a clean one
    (init markers plus the usage note), so the exit code is the plugins' alone.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-doctor-plugins-')
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.project = base / 'project'
        ledger = init_markers(self.project)
        (ledger / 'AGENTS.md').write_text('# ledger\n', encoding='utf-8')
        self.plugins = base / 'plugins'
        self.plugins.mkdir()
        self.home = base / 'home'
        self.home.mkdir()

    # ── helpers ──────────────────────────────────────────────────────────────
    def doctor(self, plugins=None):
        env = _child_env()
        env['PF_PLUGINS_DIR'] = str(self.plugins if plugins is None else plugins)
        env['PF_HOME_DIR'] = str(self.home)
        return run_argv([PF, 'doctor'], cwd=self.project, env=env, check=False)

    def add_plugin(self, name='auditor', declaration=None, impl=None):
        """One plugin folder under the temp plugins directory, with the skill its method names.

        `impl` replaces the trivial implementation a case does not care about: the doctor has to call
        every installed `impl.py` once, and load it once on top of that, so what a broken one looks
        like is a case of its own. The default declaration names a method the fixture's own folder
        carries a skill for -- a skill lives in the plugin folder, so a fixture without one would make
        every case here fail for a reason none of them is about.
        """
        folder = self.plugins / name
        folder.mkdir()
        (folder / 'impl.py').write_text(
            impl if impl is not None else 'def offer(ctx):\n    return None\n', encoding='utf-8')
        (folder / 'plugin.json').write_text(json.dumps(declaration or {
            'name': name, 'timing': 'close', 'action': 'insert',
            'knobs': {'audit_limit': {'kind': 'number',
                                      'summary': 'the audited minutes the line must read below'}},
            'readings': {'audit_minutes': {'source': 'ticket', 'when_knob': 'audit_limit',
                                           'ask': 'the minutes a full audit took'}},
            'metrics': {'audit_minutes': {'line': 'audit_limit', 'tag': 'audit:minutes',
                                          'method': FIXTURE_METHOD, 'order': 0}},
        }), encoding='utf-8')
        if declaration is None:
            write_skill(folder, FIXTURE_METHOD)
        return folder

    def pf(self, *args):
        env = _child_env()
        env['PF_PLUGINS_DIR'] = str(self.plugins)
        env['PF_HOME_DIR'] = str(self.home)
        return run_argv([PF, *args], cwd=self.project, env=env, check=False)

    # ── cases ────────────────────────────────────────────────────────────────
    def test_an_installed_plugin_is_listed_with_its_tags_and_methods(self):
        self.add_plugin()

        proc = self.doctor()

        self.assertEqual(0, proc.returncode, proc.stdout)
        self.assertIn('plugins: 1 installed', proc.stdout)
        self.assertIn('auditor', proc.stdout)
        self.assertIn('audit:minutes', proc.stdout, 'the dedupe tag the outside writes down')
        self.assertIn(FIXTURE_METHOD, proc.stdout, 'the method name its nodes point at')
        self.assertIn('the audited minutes the line must read below', proc.stdout,
                      'the knob it declares, in the declaration\'s own words')

    def test_an_empty_plugins_directory_reports_zero_installed(self):
        proc = self.doctor()

        self.assertEqual(0, proc.returncode, proc.stdout)
        self.assertIn('plugins: 0 installed', proc.stdout,
                      'the core is whole with no plugin at all, and the doctor says so')
        self.assertIn(str(self.plugins), proc.stdout, 'and it says where it looked')

    def test_a_declaration_problem_is_counted_in_the_exit_code(self):
        clean = self.doctor()
        self.assertEqual(0, clean.returncode, clean.stdout)

        folder = self.add_plugin('broken', {'name': 'broken', 'timing': 'nope'})
        broken = self.doctor()

        self.assertNotEqual(0, broken.returncode,
                            f'a broken declaration must reach the exit code:\n{broken.stdout}')
        self.assertIn('broken', broken.stdout)
        self.assertIn('not one of', broken.stdout,
                      'the problem says what the declaration would have to be')
        self.assertTrue((folder / 'plugin.json').is_file())

    def test_a_declaration_that_claims_a_builtin_name_is_reported(self):
        """A declaration that claims a core setting's name is the merge's one silence: the built-in
        cell keeps working (a broken declaration may not redefine what a setting means), the declared
        one is unreachable, and `pf doctor` -- the one door that sees both tables -- says so."""
        self.add_plugin('thief', {
            'name': 'thief', 'timing': 'close', 'action': 'insert',
            'knobs': {'max_agents': {'kind': 'word', 'values': ['one', 'many'],
                                     'summary': 'how many agents, but a word'}},
            'readings': {}, 'metrics': {},
        })

        proc = self.doctor()

        self.assertNotEqual(0, proc.returncode,
                            f'a shadowed built-in name must reach the exit code:\n{proc.stdout}')
        self.assertIn("'max_agents'", proc.stdout, 'the problem names the knob that collided')
        self.assertIn("'thief'", proc.stdout, 'and the declaration that tried')
        # the built-in cell still wins: the setting is an integer, so a word is refused
        self.assertEqual('3\n', self.pf('config', 'get', 'max_agents').stdout)
        self.assertNotEqual(0, self.pf('config', 'set', 'max_agents', 'one').returncode,
                            'the declared word vocabulary reached the built-in setting')

    def test_a_declared_layers_cell_off_the_chain_is_reported(self):
        """A knob's `layers` cell is data the declaration owns; a layer name that is not on the chain
        would narrow the knob to a layer nothing resolves, so the doctor names it and the knob keeps
        the whole chain until the declaration is fixed."""
        self.add_plugin('narrow', {
            'name': 'narrow', 'timing': 'close', 'action': 'insert',
            'knobs': {'audit_limit': {'kind': 'number', 'layers': ['line']}},
            'readings': {}, 'metrics': {},
        })

        proc = self.doctor()

        self.assertNotEqual(0, proc.returncode,
                            f'a layers cell off the chain must reach the exit code:\n{proc.stdout}')
        self.assertIn('audit_limit', proc.stdout)
        self.assertIn("'line'", proc.stdout, 'the problem names the layer nobody resolves')
        # the knob is still reachable from the whole chain: the value writes and reads back
        self.assertEqual(0, self.pf('config', 'set', 'audit_limit', '5').returncode)

    def test_a_broken_implementation_is_reported(self):
        """`pf doctor` loads every installed `impl.py` once: a plugin whose code cannot even compile
        has to be named on the doctor's list, not discovered at the next close."""
        clean = self.doctor()
        self.assertEqual(0, clean.returncode, clean.stdout)

        self.add_plugin('syntax', impl='def offer(ctx)\n    return None\n')

        broken = self.doctor()

        self.assertNotEqual(0, broken.returncode,
                            f'a broken implementation must reach the exit code:\n{broken.stdout}')
        self.assertIn("'syntax'", broken.stdout)
        self.assertIn('failed to load', broken.stdout,
                      'the sentence says the code, not the declaration, is what broke')

    def test_a_method_without_a_card_is_reported(self):
        """The doctor runs the validator's skill check: a method a declaration names has to have its
        `SKILL.md` in a skill folder some installed plugin ships, or a generated node points a session
        at nothing."""
        self.add_plugin('cardless', {
            'name': 'cardless', 'timing': 'close', 'action': 'insert',
            'knobs': {'audit_limit': {'kind': 'number'}},
            'readings': {'audit_minutes': {'source': 'ticket', 'when_knob': 'audit_limit'}},
            'metrics': {'audit_minutes': {'line': 'audit_limit', 'tag': 'audit:cardless',
                                          'method': 'pf-no-such-skill', 'order': 0}},
        })

        proc = self.doctor()

        self.assertNotEqual(0, proc.returncode,
                            f'a method skill that is missing must reach the exit code:\n{proc.stdout}')
        self.assertIn("'cardless'", proc.stdout, 'the problem names the plugin')
        self.assertIn("'pf-no-such-skill'", proc.stdout, 'and the method nobody has a skill for')

    def test_an_implementation_that_raises_is_reported(self):
        """The doctor really calls each implementation once, against a stand-in project: a plugin
        that explodes at its door is named here, not discovered at somebody's next close."""
        self.add_plugin('boom', impl='def offer(ctx):\n    raise RuntimeError("boom")\n')

        proc = self.doctor()

        self.assertNotEqual(0, proc.returncode,
                            f'an implementation that raises must reach the exit code:\n{proc.stdout}')
        self.assertIn("'boom'", proc.stdout, 'the problem names the plugin')
        self.assertIn('raised on a stand-in project', proc.stdout,
                      'the sentence says the code answered with an exception')


class TestPluginCommands(unittest.TestCase):
    """The words installed plugins contribute: listed by `--help`, routed by `main()`, and gone again
    when the folder or the declaration that carried them is.

    `PF_PLUGINS_DIR` is pointed at a throwaway directory in every case, so what is measured is the
    entry layer's own routing -- never which plugins this checkout happens to ship. The project is a
    clean one (init markers plus the usage note) and this machine's settings layer is a throwaway too,
    so a healthy fixture's `pf doctor` has nothing but the plugins to report.

    A plugin's command is answered by that folder's implementation, run as a script: the fixture below
    appends the argv it was handed to a file beside itself, which is how a case reads whether the word
    went along and whether the fixture ran at all.
    """

    #: The word the fixture contributes, and the line its declaration prints for it.
    WORD = 'pf-fixture-cmd'
    LINE = 'the fixture word, which the declaration names'

    #: A word wider than the listing's word-column floor (30 columns), and the line beside it: the
    #: declaration may claim a word of any length, so the listing has to lay itself out around one.
    LONG_WORD = 'pf-' + 'w' * 28
    LONG_LINE = 'the long word, which the declaration names'

    #: and one wider than the listing itself, where no room is left beside the word column at all.
    HUGE_WORD = 'pf-' + 'w' * 97

    #: A plugin implementation that records the argv it was handed. Its second entry (the timing door)
    #: answers "nothing this pass", which is what keeps it a legal declaration either way.
    CLI_IMPL = '''\
import os
import sys


def offer(ctx):
    return None


def _record(text):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, 'ran.txt'), 'a', encoding='utf-8') as fh:
        fh.write(text + chr(10))


if __name__ == '__main__':
    _record(' '.join(sys.argv[1:]))
    print('the fixture answered: ' + ' '.join(sys.argv[1:]))
'''

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-plugin-commands-')
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.project = base / 'project'
        ledger = init_markers(self.project)
        (ledger / 'AGENTS.md').write_text('# ledger\n', encoding='utf-8')
        self.plugins = base / 'plugins'
        self.plugins.mkdir()
        self.home = base / 'home'
        self.home.mkdir()

    # ── helpers ──────────────────────────────────────────────────────────────
    def add_plugin(self, name='probe', commands=None, impl=None, **decl):
        """One plugin folder under the throwaway plugins directory; returns it.

        The default declaration carries no timing and no action: a plugin that contributes a word is a
        legal declaration as it stands, which is the smallest shape `pf <word>` can be handed to.
        """
        folder = self.plugins / name
        folder.mkdir()
        (folder / 'impl.py').write_text(impl if impl is not None else self.CLI_IMPL,
                                        encoding='utf-8')
        (folder / 'plugin.json').write_text(json.dumps({
            'name': name, 'summary': f'{name}, a fixture', 'commands': commands or {},
            **decl}, ensure_ascii=False), encoding='utf-8')
        return folder

    def write_declaration(self, folder, decl):
        (folder / 'plugin.json').write_text(json.dumps(decl, ensure_ascii=False), encoding='utf-8')

    def pf(self, *args):
        env = _child_env()
        env['PF_PLUGINS_DIR'] = str(self.plugins)
        env['PF_HOME_DIR'] = str(self.home)
        return run_argv([PF, *[str(a) for a in args]], cwd=self.project, env=env, check=False)

    def doctor(self):
        return self.pf('doctor')

    def ran(self, name='probe'):
        """The argv lines the fixture recorded, in order (`[]` when its implementation never ran)."""
        path = self.plugins / name / 'ran.txt'
        return path.read_text(encoding='utf-8').splitlines() if path.exists() else []

    # ── cases ────────────────────────────────────────────────────────────────
    def test_a_word_a_plugin_contributes_is_listed_and_runs(self):
        """A plugin that carries one command and nothing else: `pf --help` prints the word with the
        declaration's own line, `pf <word> <rest>` reaches the folder's implementation with the word
        and the rest, and `pf doctor` -- which has nothing else to report -- says it is installed and
        lists the word it brings.
        """
        self.add_plugin('probe', {self.WORD: {'summary': self.LINE}})

        help_text = self.pf('--help')
        self.assertEqual(0, help_text.returncode, help_text.stderr)
        self.assertIn(self.WORD, help_text.stdout, 'the help does not list the word')
        self.assertIn(self.LINE, help_text.stdout, 'the help does not print the declaration\'s line')

        ran = self.pf(self.WORD, 'alpha', 'beta')
        self.assertEqual(0, ran.returncode, ran.stderr)
        self.assertIn(f'the fixture answered: {self.WORD} alpha beta', ran.stdout)
        self.assertEqual([f'{self.WORD} alpha beta'], self.ran())

        doctor = self.doctor()
        self.assertEqual(0, doctor.returncode,
                         f'a healthy word-carrying plugin is a problem:\n{doctor.stdout}')
        self.assertIn(f'commands: {self.WORD}', doctor.stdout,
                      'the doctor does not say what the folder brings')

    def test_a_word_wider_than_the_word_column_still_stands_on_its_own(self):
        """Nothing bounds how long a declared word is (`_command_word` asks only that one token is a
        word), so the listing lays its word column out by the widest word in it: the sense opens one
        blank after that word, the row reads back as the word alone, and the token read out of the
        listing is a word the router takes.

        Glued together -- `word` + `summary` as one token -- the row reads as a word nobody declared,
        which is what the listing guard in `TestCommandList` hands the router.
        """
        self.add_plugin('probe', {self.LONG_WORD: {'summary': self.LONG_LINE}})

        help_text = self.pf('--help')
        self.assertEqual(0, help_text.returncode, help_text.stderr)
        lines = help_text.stdout.splitlines()
        row = next((line for line in lines if line[12:].startswith(self.LONG_WORD)), None)
        self.assertIsNotNone(row, 'the declaration\'s word is not in the listing')
        self.assertEqual(' ', row[12 + len(self.LONG_WORD)],
                         'no blank between the word and its own line: the row reads as one token')
        self.assertIn(self.LONG_LINE, row, 'the declaration\'s line is not beside the word')
        token = row[12:].split()[0]
        self.assertEqual(self.LONG_WORD, token, 'the word column does not read back as the word')
        self.assertEqual(0, self.pf(token, 'x').returncode,
                         'the token the listing reads out is not a word `pf` routes')

        # the plugin section's title follows the same column: it stays the rest of the title, rather
        # than standing in the word column as a word of its own
        title = next(line for line in lines if line.startswith('  plugins:'))
        continuation = lines[lines.index(title) + 1]
        self.assertIn('is its own', continuation)
        self.assertEqual('', continuation[:12 + len(self.LONG_WORD) + 1].strip(),
                         'the title\'s second line stands inside the word column')

        # and the word is of no bounded length: one wider than the listing itself still leaves the
        # listing answering, with the word whole and the sense beside it
        self.write_declaration(self.plugins / 'probe', {
            'name': 'probe', 'summary': 'probe, a fixture',
            'commands': {self.HUGE_WORD: {'summary': self.LONG_LINE}}})
        huge = self.pf('--help')
        self.assertEqual(0, huge.returncode, huge.stderr)
        huge_token = next(line[12:].split()[0] for line in huge.stdout.splitlines()
                          if line[12:].startswith(self.HUGE_WORD))
        self.assertEqual(self.HUGE_WORD, huge_token, 'a very wide word is read back glued to its line')
        self.assertEqual(0, self.pf(huge_token, 'x').returncode)

    def test_a_word_that_does_not_travel_is_handed_the_rest_of_the_line(self):
        """The declaration decides whether its own word goes along in the argv: with the cell off, the
        implementation is handed the rest of the line and nothing else."""
        self.add_plugin('probe', {self.WORD: {'summary': self.LINE, 'keep_name': False}})

        ran = self.pf(self.WORD, 'alpha', 'beta')

        self.assertEqual(0, ran.returncode, ran.stderr)
        self.assertEqual(['alpha beta'], self.ran())

    def test_a_broken_declaration_loses_the_word(self):
        """The word is a declaration's, so it goes when the declaration cannot be acted on: written
        wrongly, the word is out of both the listing and the router, and typing it is the refusal a
        word nobody contributed gets.

        `pf --help` still answers and exits 0, and `pf doctor` -- which still answers -- names the
        plugin and what is wrong with it.
        """
        folder = self.add_plugin('probe', {self.WORD: {'summary': self.LINE}})
        self.assertEqual(0, self.pf('--help').returncode, 'the fixture does not start healthy')

        bad = {'name': 'probe', 'summary': 'a fixture', 'timing': 'lunch',
               'commands': {self.WORD: {'summary': self.LINE}}}
        self.write_declaration(folder, bad)

        help_text = self.pf('--help')
        self.assertEqual(0, help_text.returncode, help_text.stderr)
        self.assertNotIn(self.WORD, help_text.stdout)
        gone = self.pf(self.WORD)
        self.assertEqual(2, gone.returncode, 'a word with a broken declaration was routed')
        self.assertIn('does not recognize', gone.stderr)
        doctor = self.doctor()
        self.assertIn('pf command: ', doctor.stdout, 'the doctor stopped answering')
        self.assertIn('project root: ', doctor.stdout, 'the doctor stopped answering')
        self.assertIn("plugin 'probe'", doctor.stdout, 'the doctor did not name the plugin')
        self.assertIn("timing 'lunch'", doctor.stdout, 'the doctor did not name what is wrong')

    def test_a_word_whose_implementation_is_gone_keeps_its_place_and_fails_loudly(self):
        """The declaration is sound here and the code is not: the word is the declaration's, and the
        listing reads declarations only, so `pf --help` still prints it and the router still takes it.

        Typing that word is where the missing file is found out: a non-zero exit, one sentence on
        stderr naming the plugin and the file, and the fixture ran nothing.
        """
        folder = self.add_plugin('probe', {self.WORD: {'summary': self.LINE}})
        (folder / 'impl.py').unlink()

        help_text = self.pf('--help')
        self.assertEqual(0, help_text.returncode, help_text.stderr)
        self.assertIn(self.WORD, help_text.stdout, 'a sound declaration lost its word with its code')
        self.assertIn(self.LINE, help_text.stdout)

        typed = self.pf(self.WORD)
        self.assertNotEqual(0, typed.returncode, 'a word whose code is gone answered as a command')
        self.assertIn("plugin 'probe'", typed.stderr, 'the failure did not name the plugin')
        self.assertIn('impl.py', typed.stderr, 'the failure did not name the file that is gone')
        self.assertEqual([], self.ran(), 'the fixture ran an implementation that is not there')

        doctor = self.doctor()
        self.assertIn("plugin 'probe'", doctor.stdout)
        self.assertIn('no impl.py in', doctor.stdout)

    def test_an_implementation_that_cannot_load_keeps_its_word_and_fails_loudly(self):
        """Code that cannot even be loaded is asked for when its word is typed, so it is one sentence
        naming the plugin and the error rather than a word that quietly goes.

        `pf --help` still answers, exits 0 and prints the word; `pf doctor` still names the plugin and
        says the code is what broke; nothing of the fixture ran.
        """
        self.add_plugin('syntax', {self.WORD: {'summary': self.LINE}},
                        impl='def offer(ctx)\n    return None\n')

        help_text = self.pf('--help')
        self.assertEqual(0, help_text.returncode, help_text.stderr)
        self.assertIn(self.WORD, help_text.stdout, 'unloadable code took the word off the listing')

        typed = self.pf(self.WORD)
        self.assertNotEqual(0, typed.returncode, 'a word with unloadable code answered as a command')
        self.assertIn("plugin 'syntax'", typed.stderr, 'the failure did not name the plugin')
        self.assertIn('failed to load', typed.stderr, 'the failure did not say the code is what broke')
        self.assertEqual([], self.ran('syntax'))

        doctor = self.doctor()
        self.assertIn("plugin 'syntax'", doctor.stdout)
        self.assertIn('failed to load', doctor.stdout)

    def test_a_plugin_that_stops_keeps_its_word_and_its_own_code(self):
        """A `sys.exit` in the folder's own code is an answer, not a table that broke: the word stays
        listed and routed, typing it comes back with the code the folder chose, and stderr says which
        folder stopped.
        """
        self.add_plugin('stop', {'pf-fixture-stop': {'summary': 'the fixture line'}},
                        impl='import sys\n\nsys.exit(9)\n')

        help_text = self.pf('--help')
        self.assertEqual(0, help_text.returncode, help_text.stderr)
        self.assertIn('pf-fixture-stop', help_text.stdout, 'a stopping folder lost its word')

        stopped = self.pf('pf-fixture-stop')
        self.assertEqual(9, stopped.returncode, 'the code the folder chose did not come back')
        self.assertIn("plugin 'stop'", stopped.stderr, 'the stop did not name the plugin')
        self.assertIn('exit code 9', stopped.stderr)

    def test_a_removed_folder_leaves_the_help_exactly_as_it_was(self):
        """The folder is the plugin's identity: while it is there the help carries its word, and once
        it is gone the help is byte-for-byte the one this checkout prints with no plugin at all -- and
        the word is out of the router as well.
        """
        empty = self.pf('--help')
        self.assertEqual(0, empty.returncode, empty.stderr)
        folder = self.add_plugin('probe', {self.WORD: {'summary': self.LINE}})
        with_word = self.pf('--help').stdout
        self.assertIn(self.WORD, with_word, 'the fixture does not start with its word listed')

        shutil.rmtree(folder)

        after = self.pf('--help')
        self.assertEqual(0, after.returncode, after.stderr)
        self.assertNotIn(self.WORD, after.stdout)
        self.assertEqual(empty.stdout, after.stdout,
                         'the help is not the one it printed with no plugin installed')
        self.assertEqual(2, self.pf(self.WORD).returncode)
        self.assertEqual([], self.ran())

    def test_a_word_the_core_already_answers_is_reported_and_the_core_keeps_it(self):
        """Static priority: a declaration claiming a word this checkout already answers contributes
        nothing -- its words are out of the listing, the core's rows keep routing, and the plugin's
        implementation never runs.

        `pf doctor` is the one door holding both tables, so it is where the declaration that lost is
        named, word by word.
        """
        self.add_plugin('clash', {'next': {'summary': 'the clash would like this one'},
                                  'doctor': {'summary': 'and this one'},
                                  'cycle': {'summary': 'this group too'}})

        help_text = self.pf('--help')
        self.assertEqual(0, help_text.returncode, help_text.stderr)
        for line in ('the clash would like this one', 'and this one', 'this group too'):
            self.assertNotIn(line, help_text.stdout, 'a losing declaration reached the help')

        doctor = self.doctor()
        for word in ('doctor', 'next', 'cycle'):
            self.assertIn(f"plugin 'clash': command '{word}' is a word this checkout already answers",
                          doctor.stdout, f'the collision on {word!r} was not reported')

        # the routing still goes to the core's own row, on both tables and on a self verb
        self.assertIn('pf command: ', self.doctor().stdout)
        self.assertIn('no_cycle', self.pf('next').stdout)
        self.assertEqual(0, self.pf('cycle', '--help').returncode)
        self.assertEqual([], self.ran('clash'), 'the clash answered a word the core owns')


class TestCommandList(unittest.TestCase):
    """The command list has one source: whatever `<subcommand> --help` prints is what `help`
    prints.

    The words `pf` itself answers are the static tables; the words an installed plugin contributes are
    merged in behind them by the entry's one `_routing()`. Both readers -- the listing and the router
    -- take the rows from there, and the two guards below are what says so: a row injected into that
    table has to show up in the help text and be routed by `main()`, which a second copy of the list
    could not do for both, and the listing read back has to yield words `main()` takes, which a word
    printed from anywhere else -- or a row glued to its sense, and read back as one token -- would not.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-help-')
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    @staticmethod
    def entry():
        import importlib.util
        spec = importlib.util.spec_from_file_location('pf_bin_entry', PF)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @requires_plugin('issue')
    def test_issue_help_prints_the_parser_help(self):
        """The word a folder contributes answers `--help` with the folder's own tool's text: `pf
        <word> --help` is that tool's parser talking, which is what a plugin's word means."""
        verb, flag = pf('issue', 'help', cwd=self.dir), pf('issue', '--help', cwd=self.dir)
        self.assertEqual(0, verb.returncode, verb.stderr)
        self.assertEqual(0, flag.returncode, flag.stderr)
        self.assertEqual(flag.stdout, verb.stdout)
        self.assertIn('weight', verb.stdout)

    def test_pf_help_names_every_word_it_routes(self):
        """`--help`'s sections list the words in the routing table -- adding a verb changes only that
        one row, and a word a plugin contributes arrives through the same table."""
        entry = self.entry()
        table = entry._routing()

        text = pf('--help', cwd=self.dir).stdout
        routed = [row[0] for row in entry.SELF_VERBS]
        routed += [row[0] for row in table['verbs']]
        routed += [row[0] for row in table['plugins']]
        routed += [row[0] for row in table['groups']]
        for name in routed:
            self.assertIn(name, text, f'{name} is in the routing table, but `pf --help` omits it')

    def test_the_help_and_the_router_are_grown_from_one_table(self):
        """The two readers walk one table: a row injected into `_routing()` is printed by `usage()`
        and routed by `main()`, and a word nobody contributed is refused.

        A second copy of the list -- the listing written by hand beside the router -- would show the
        injected row in one of the two and not the other, which is what this case is for.
        """
        entry = self.entry()
        built = entry._routing
        row = ('pf-injected-word', str(self.dir / 'nobody-shipped-this.py'), True,
               'the injected row nobody shipped')
        entry._routing = lambda: {**built(), 'plugins': (*built()['plugins'], row)}
        routed = []
        entry._plugin_run = lambda plugin, target, keep_name, verb, rest: (
            routed.append((plugin, str(target), verb, tuple(rest))), 0)[1]
        try:
            self.assertIn(row[0], entry.usage(), 'the listing did not print the table it walks')
            self.assertEqual(0, entry.main([row[0], 'x']))
            self.assertEqual([(Path(row[1]).parent.name, row[1], row[0], ('x',))], routed,
                             'the router did not take the word from the same table')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(2, entry.main(['pf-nobody-contributed-this']),
                                 'a word that is not in the table was routed anyway')
        finally:
            entry._routing = built

    @staticmethod
    def printed_words(entry, text):
        """The words `pf --help` prints, read off the listing's own layout.

        A row stands at the listing's word margin; the shape note above the listing and the closing
        lines below it start at the left, and a wrapped sense -- or the plugins' title standing over
        the words -- leaves the word column blank. None of those carries a word, and every other row's
        first token is the word it prints, taken whole: a row glued to its sense reads back as one
        token, which is what the caller hands the router.
        """
        words = []
        for line in text.splitlines():
            if not line.startswith(' ' * entry.USAGE_MARGIN):
                continue
            if not line[entry.USAGE_MARGIN:entry.USAGE_COLUMN].strip():
                continue
            token = line[entry.USAGE_MARGIN:].split()[0]
            if not token.startswith('('):          # a section's own title, standing over its words
                words.append(token)
        return words

    def test_every_word_the_listing_prints_is_a_word_the_router_takes(self):
        """The guard above, read the other way: the words `pf --help` prints are words `main()` routes.

        Both readers walk one table, so a word printed beside it -- a listing grown from a second
        source, a row glued to its sense and read back as one token -- is a word the router refuses;
        that direction is what the first listing guard cannot see. The doors are stand-ins here
        (`_run`, `_plugin_run`, the self handlers): whether a word routes at all is the question,
        never what answering it would do.
        """
        entry = self.entry()
        built = entry._routing
        row = ('pf-injected-word', str(self.dir / 'nobody-shipped-this.py'), True,
               'the injected row nobody shipped')
        entry._routing = lambda: {**built(), 'plugins': (*built()['plugins'], row)}
        taken = []
        entry._run = lambda *argv: (taken.append(1), 0)[1]
        entry._plugin_run = lambda plugin, target, keep_name, verb, rest: (taken.append(1), 0)[1]
        entry._SELF_HANDLERS = {name: (lambda rest: (taken.append(1), 0)[1])
                                for name, _display, _blurb, _fn in entry.SELF_VERBS}
        try:
            printed = self.printed_words(entry, entry.usage())
            table = entry._routing()
            rows = ([name for name, *_rest in entry.SELF_VERBS]
                    + [name for name, *_rest in table['verbs']]
                    + [name for name, *_rest in table['groups']]
                    + [name for name, *_rest in table['plugins']])
            for word in printed:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertNotEqual(2, entry.main([word]),
                                        f'`pf --help` prints `{word}`, and the router refuses it')
            self.assertEqual(len(printed), len(taken),
                             'a word the listing prints never reached a door')
            self.assertEqual(sorted(rows), sorted(printed),
                             'the listing and the routing table do not carry the same words')
        finally:
            entry._routing = built


if __name__ == '__main__':
    unittest.main()