"""`install.ps1`: the one door every user runs, exercised against a fake checkout.

The script links two kinds of skill into `~/.agents/skills` -- the core's own `skills/<lang>/pf-*`
and the ones a plugin ships in its own folder -- puts `pf` on PATH, and finishes with a self-check.
Nothing else in this tree tests it, so a regression here is one a user meets before any test does.

Each case builds a throwaway checkout in a temp dir (its own `skills/`, its own `plugins/`, a stub
`pf.cmd` where the self-check's `pf doctor` would run, and a fake home), runs a copy of the real
script with `$HOME` rewritten to that temp home and `-SkipPath`, and looks at the junctions it made.
The real `~/.agents/skills` and the real PATH are never touched, and the script's own `pf doctor`
half is a stub: what is under test is the linking, which is the half nothing else covers.

    python conduct/tests/cases_install.py            # this file on its own
    python conduct/tests/test_conduct_pipeline.py    # with the whole suite (registration is in that
                                                     # file's import section)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import TOOLS_DIR  # noqa: E402

__all__ = ['TestTheInstaller']

REPO = TOOLS_DIR.parent
PWSH = shutil.which('pwsh')

#: The stub the self-check's `pf doctor` reaches: the checkout here is a shell of folders, so the
#: tool half has nothing to say about it, and this case is about the junctions.
STUB_PF_CMD = '@echo off\r\nexit /b 0\r\n'

#: A plugin whose declaration names one skill of its own: the smallest folder the installer owes a
#: link to, so "every owner's skills are linked" has an owner other than the core to point at.
DEMO_DECL = '''\
{
  "name": "%(name)s",
  "summary": "a fixture",
  "timing": "close",
  "action": "insert",
  "knobs": {"%(name)s_on": {"kind": "switch", "default": "on", "summary": "on"},
            "%(name)s_limit": {"kind": "number", "summary": "the line"}},
  "readings": {"%(name)s_seen": {"source": "ticket"}},
  "metrics": {"%(name)s_seen": {"line": "%(name)s_limit", "tag": "%(name)s:seen",
                                "method": "%(method)s"}}
}
'''

QUIET_IMPL = 'def offer(ctx):\n    return None\n'


def _new(path: Path, text: str = '') -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


@unittest.skipUnless(PWSH and os.name == 'nt', 'install.ps1 is a PowerShell script, run on Windows')
class TestTheInstaller(unittest.TestCase):
    """One fake checkout per case, and the real script run against it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-install-case-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.checkout = self.root / 'checkout'
        self.home = self.root / 'home'
        (self.home / '.agents' / 'skills').mkdir(parents=True)
        self.skills = self.home / '.agents' / 'skills'
        # the core's own skills: the two the script would link on any install
        for name in ('pf-conduct', 'pf-route'):
            _new(self.checkout / 'skills' / 'en' / name / 'SKILL.md', f'# {name}\n')
        _new(self.checkout / 'skills' / 'en' / 'shared' / 'pf' / 'philosophy.md', '# pf\n')
        # the self-check's `pf doctor`: a stub, because this checkout is folders and nothing else
        _new(self.checkout / 'tools' / 'bin' / 'pf.cmd', STUB_PF_CMD)
        shutil.copyfile(TOOLS_DIR / 'bin' / 'pf.py', self.checkout / 'tools' / 'bin' / 'pf.py')

    def plugin(self, name: str, method: str, declaration: str | None = None) -> Path:
        """One plugin folder shipping `skills/en/<method>`, and the declaration that names it."""
        folder = self.checkout / 'plugins' / name
        text = declaration if declaration is not None else DEMO_DECL % {'name': name, 'method': method}
        _new(folder / 'plugin.json', text)
        _new(folder / 'impl.py', QUIET_IMPL)
        _new(folder / 'skills' / 'en' / method / 'SKILL.md', f'# {method}\n')
        return folder

    def install(self, *flags: str) -> subprocess.CompletedProcess:
        """Run a copy of the real script: `$HOME` rewritten to this case's fake home, PATH untouched.

        The rewrite is the only reason this is a copy. `$HOME` is an automatic variable PowerShell
        will not let a caller assign, and the script's three uses of it are exactly the parts a test
        must redirect: the skills root, the install marker and the language record.
        """
        source = (REPO / 'install.ps1').read_text(encoding='utf-8')
        # the copy lives IN the checkout: the script resolves the repository from its own folder, so a
        # copy anywhere else would be told the wrong tree
        script = _new(self.checkout / 'install.ps1', source.replace('$HOME', f"'{self.home}'"))
        command = [PWSH, '-NoProfile', '-NonInteractive', '-File', str(script), '-SkipPath', *flags]
        # the console codepage decides how pwsh renders a thrown error to stderr; decode permissively
        # so a GBK-rendered refusal comes back as a string with the ASCII assertions intact
        return subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace',
                              timeout=120, cwd=str(self.checkout),
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    # ── the two owners, linked ───────────────────────────────────────────────
    def test_the_install_links_the_core_and_every_plugins_skills(self):
        """Section 1 links the core's `pf-*`, section 2 each plugin's own folder, and both land.

        The second owner is the half that had no test: a plugin's skill is linked from inside that
        plugin's folder, so removing the folder is what removes the link.
        """
        self.plugin('demo', 'pf-demo-skill')

        run = self.install()

        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        for name in ('pf-conduct', 'pf-route', 'pf-demo-skill'):
            with self.subTest(skill=name):
                self.assertTrue((self.skills / name / 'SKILL.md').is_file(),
                                f'{name} does not resolve to a readable skill: {run.stdout}')
        self.assertTrue((self.skills / 'shared' / 'pf' / 'philosophy.md').is_file(),
                        f'shared/pf is not in place: {run.stdout}')
        self.assertIn('plugin skills visible here: 1', run.stdout)

    def test_a_plugin_folder_that_is_gone_takes_its_links_with_it(self):
        """Delete the folder, re-run, and the link it owed is gone while the others stay.

        This is what "installed and removed with the plugin" means, and it is the one promise of the
        whole arrangement: a dead junction is invisible to the count of what a session can read.
        """
        folder = self.plugin('demo', 'pf-demo-skill')
        self.assertEqual(0, self.install().returncode)
        shutil.rmtree(folder)

        run = self.install()

        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertFalse((self.skills / 'pf-demo-skill').exists(),
                         f'the link outlived its plugin: {run.stdout}')
        self.assertTrue((self.skills / 'pf-conduct' / 'SKILL.md').is_file(),
                        'the core skills went with the plugin')
        self.assertIn('plugin skills visible here: 0', run.stdout)

    def test_a_plugin_may_not_take_a_core_skills_name(self):
        """Two owners, one name: refused before a junction is written, and both sides are named.

        A plugin method that repeats a core skill's name would have section 2 write the same link
        section 1 just made -- the core skill unreachable under its own name, the run reporting
        success, and every re-run repeating it. `pf doctor` cannot see it: it compares plugins with
        each other, and a core skill is not a plugin.
        """
        self.plugin('thief', 'pf-route')

        run = self.install()

        self.assertNotEqual(0, run.returncode, 'a stolen skill name was accepted')
        output = run.stdout + run.stderr
        self.assertIn("'thief'", output, output)
        self.assertIn("'pf-route'", output, 'the refusal does not name the skill it protects')
        self.assertIn('core skill', output, output)
        # refused before anything was written: no junction at all, and the core keeps its own folder
        self.assertFalse((self.skills / 'shared').exists(),
                         f'the run linked something before refusing: {run.stdout}')


if __name__ == '__main__':
    unittest.main()
