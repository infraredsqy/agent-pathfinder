"""The pieces in front of `pf`'s door: `pf.cmd` on Windows, the project root `doctor` names, and the
features `doctor` says are installed.

All three live in the pf layer, and what is measured is the rules of that layer:

  * `pf.cmd` hands the command it receives to `pf.py` as it is, exactly once: whichever interpreter
    it finds is the one used, and that interpreter's exit code is `pf`'s exit code. A non-zero exit
    especially has to hold -- `pf handoff` / `pf merge-ledger` are live-fire commands, and being run
    again by the fallback interpreter is a second shot.
  * the project root `doctor` reports is the same root the tools it forwards (`pf dag` / `pf issue`)
    read the ledger by: cwd is the root, the one exception being that a linked worktree goes back to
    the main tree. That the two readings agree is what these cases prove.
  * `doctor`'s `features:` section: what the scan found, the dedupe tags and methods each feature
    shows the outside, zero installed for an empty features directory, a declaration problem counted
    in the exit code, a declaration that claims a built-in setting's name (reported by the one door
    that sees both tables, with the built-in cell still in force), and an `impl.py` that cannot even
    compile (the doctor loads each installed one once, so a broken implementation is named here and
    not discovered at the next close). The scan is pointed at a throwaway features directory, so
    these cases say nothing about which features this checkout happens to ship -- an empty
    `features/` is a legal tree and the suite stays green with it.
  * `help` and `--help` print the same body (the parser's own), and the command list is not copied a
    second time.

Every case builds its own thing in a temp dir (a fake `pf.py` plus a real `pf.cmd`, a throwaway git
repository plus one worktree, or a project plus a features directory), runs the real CLI and cleans up
afterwards; no real project is touched, no session is opened, and every command stays inside the 5 s
budget.

    python conduct/tests/cases_bin.py            # this file on its own
    python conduct/tests/test_conduct_pipeline.py    # with the whole suite (registration is in that
                                                     file's import section)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import GIT, TOOLS_DIR, _child_env, _run_command, git, init_markers, run_argv

__all__ = ['TestPfCmdShim', 'TestDoctorRoot', 'TestDoctorFeatures', 'TestCommandList']

PYTHON = sys.executable
BIN = TOOLS_DIR / 'bin'
PF = BIN / 'pf.py'
SHIM = BIN / 'pf.cmd'
CMD = os.environ.get('COMSPEC', 'cmd.exe')

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


class TestDoctorFeatures(unittest.TestCase):
    """`doctor`'s `features:` section: what the scan found, and what a declaration's problems cost.

    `PF_FEATURES_DIR` is pointed at a throwaway directory in every case, so what is asserted is the
    doctor's own reporting -- never which features this checkout ships. The project is a clean one
    (init markers plus the usage note), so the exit code is the features' alone.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-doctor-features-')
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.project = base / 'project'
        ledger = init_markers(self.project)
        (ledger / 'AGENTS.md').write_text('# ledger\n', encoding='utf-8')
        self.features = base / 'features'
        self.features.mkdir()
        self.home = base / 'home'
        self.home.mkdir()

    # ── helpers ──────────────────────────────────────────────────────────────
    def doctor(self, features=None):
        env = _child_env()
        env['PF_FEATURES_DIR'] = str(self.features if features is None else features)
        env['PF_HOME_DIR'] = str(self.home)
        return run_argv([PF, 'doctor'], cwd=self.project, env=env, check=False)

    def add_feature(self, name='auditor', declaration=None, impl=None):
        """One feature folder under the temp features directory.

        `impl` replaces the trivial implementation a case does not care about: the doctor has to call
        every installed `impl.py` once, and load it once on top of that, so what a broken one looks
        like is a case of its own. The method the default declaration names is one whose card exists
        in the `skills/` tree: a card that is missing is a problem the doctor reports, and a fixture
        that carried one would make every case here fail for a reason none of them is about.
        """
        folder = self.features / name
        folder.mkdir()
        (folder / 'impl.py').write_text(
            impl if impl is not None else 'def offer(ctx):\n    return None\n', encoding='utf-8')
        (folder / 'feature.json').write_text(json.dumps(declaration or {
            'name': name, 'timing': 'close', 'action': 'insert',
            'knobs': {'audit_limit': {'kind': 'number'}},
            'readings': {'audit_minutes': {'source': 'ticket', 'when_knob': 'audit_limit',
                                           'ask': 'the minutes a full audit took'}},
            'metrics': {'audit_minutes': {'line': 'audit_limit', 'tag': 'audit:minutes',
                                          'method': 'pf-test-audit', 'order': 0}},
        }), encoding='utf-8')
        return folder

    def pf(self, *args):
        env = _child_env()
        env['PF_FEATURES_DIR'] = str(self.features)
        env['PF_HOME_DIR'] = str(self.home)
        return run_argv([PF, *args], cwd=self.project, env=env, check=False)

    # ── cases ────────────────────────────────────────────────────────────────
    def test_an_installed_feature_is_listed_with_its_tags_and_methods(self):
        self.add_feature()

        proc = self.doctor()

        self.assertEqual(0, proc.returncode, proc.stdout)
        self.assertIn('features: 1 installed', proc.stdout)
        self.assertIn('auditor', proc.stdout)
        self.assertIn('audit:minutes', proc.stdout, 'the dedupe tag the outside writes down')
        self.assertIn('pf-test-audit', proc.stdout, 'the method name its nodes point at')

    def test_an_empty_features_directory_reports_zero_installed(self):
        proc = self.doctor()

        self.assertEqual(0, proc.returncode, proc.stdout)
        self.assertIn('features: 0 installed', proc.stdout,
                      'the core is whole with no feature at all, and the doctor says so')
        self.assertIn(str(self.features), proc.stdout, 'and it says where it looked')

    def test_a_declaration_problem_is_counted_in_the_exit_code(self):
        clean = self.doctor()
        self.assertEqual(0, clean.returncode, clean.stdout)

        folder = self.add_feature('broken', {'name': 'broken', 'timing': 'nope'})
        broken = self.doctor()

        self.assertNotEqual(0, broken.returncode,
                            f'a broken declaration must reach the exit code:\n{broken.stdout}')
        self.assertIn('broken', broken.stdout)
        self.assertIn('not one of', broken.stdout,
                      'the problem says what the declaration would have to be')
        self.assertTrue((folder / 'feature.json').is_file())

    def test_a_declaration_that_claims_a_builtin_name_is_reported(self):
        """A declaration that claims a core setting's name is the merge's one silence: the built-in
        cell keeps working (a broken declaration may not redefine what a setting means), the declared
        one is unreachable, and `pf doctor` -- the one door that sees both tables -- says so."""
        self.add_feature('thief', {
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
        self.add_feature('narrow', {
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
        """`pf doctor` loads every installed `impl.py` once: a feature whose code cannot even compile
        has to be named on the doctor's list, not discovered at the next close."""
        clean = self.doctor()
        self.assertEqual(0, clean.returncode, clean.stdout)

        self.add_feature('syntax', impl='def offer(ctx)\n    return None\n')

        broken = self.doctor()

        self.assertNotEqual(0, broken.returncode,
                            f'a broken implementation must reach the exit code:\n{broken.stdout}')
        self.assertIn("'syntax'", broken.stdout)
        self.assertIn('failed to load', broken.stdout,
                      'the sentence says the code, not the declaration, is what broke')

    def test_a_method_without_a_card_is_reported(self):
        """The doctor runs the validator's card check: a method a declaration names has to have its
        `SKILL.md` in one of the language trees, or a generated node points a session at nothing."""
        self.add_feature('cardless', {
            'name': 'cardless', 'timing': 'close', 'action': 'insert',
            'knobs': {'audit_limit': {'kind': 'number'}},
            'readings': {'audit_minutes': {'source': 'ticket', 'when_knob': 'audit_limit'}},
            'metrics': {'audit_minutes': {'line': 'audit_limit', 'tag': 'audit:cardless',
                                          'method': 'pf-no-such-card', 'order': 0}},
        })

        proc = self.doctor()

        self.assertNotEqual(0, proc.returncode,
                            f'a method card that is missing must reach the exit code:\n{proc.stdout}')
        self.assertIn("'cardless'", proc.stdout, 'the problem names the feature')
        self.assertIn("'pf-no-such-card'", proc.stdout, 'and the method nobody has a card for')

    def test_an_implementation_that_raises_is_reported(self):
        """The doctor really calls each implementation once, against a stand-in project: a feature
        that explodes at its door is named here, not discovered at somebody's next close."""
        self.add_feature('boom', impl='def offer(ctx):\n    raise RuntimeError("boom")\n')

        proc = self.doctor()

        self.assertNotEqual(0, proc.returncode,
                            f'an implementation that raises must reach the exit code:\n{proc.stdout}')
        self.assertIn("'boom'", proc.stdout, 'the problem names the feature')
        self.assertIn('raised on a stand-in project', proc.stdout,
                      'the sentence says the code answered with an exception')


class TestCommandList(unittest.TestCase):
    """The command list has one source: whatever `<subcommand> --help` prints is what `help`
    prints."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-help-')
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_issue_help_prints_the_parser_help(self):
        verb, flag = pf('issue', 'help', cwd=self.dir), pf('issue', '--help', cwd=self.dir)
        self.assertEqual(0, verb.returncode, verb.stderr)
        self.assertEqual(0, flag.returncode, flag.stderr)
        self.assertEqual(flag.stdout, verb.stdout)
        self.assertIn('weight', verb.stdout)

    def test_pf_help_names_every_word_it_routes(self):
        """`--help`'s two sections list the words in the routing table -- adding a verb changes only
        that one row."""
        import importlib.util
        spec = importlib.util.spec_from_file_location('pf_bin_entry', PF)
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)

        text = pf('--help', cwd=self.dir).stdout
        routed = [row[0] for row in entry.SELF_VERBS]
        routed += [row[0] for row in entry.FORWARD_VERBS]
        routed += [row[0] for row in entry.FORWARD_GROUPS]
        for name in routed:
            self.assertIn(name, text, f'{name} is in the routing table, but `pf --help` omits it')


if __name__ == '__main__':
    unittest.main()