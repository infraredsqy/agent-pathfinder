"""`pf` 这道门前面的两块：Windows 上的 `pf.cmd`，和 `doctor` 认的项目根。

两块都长在 pf 这一层，量的是这一层的两条规矩：

  * `pf.cmd` 把收到的命令原样交给 `pf.py`，恰好一次：探到哪个解释器就用哪个，它的退出码就是
    `pf` 的退出码。非零退出尤其要稳 —— `pf handoff` / `pf merge-ledger` 是实弹命令，被后备
    解释器再跑一遍就是第二发。
  * `doctor` 报的项目根，与它转发的那些工具（`pf dag` / `pf issue`）读记录用的根是同一个：
    cwd 就是根，唯一的例外是 linked worktree 里回主树。两边读数对得上，是这条用例证的东西。
  * `help` 与 `--help` 印的是同一份正文（parser 那一份），命令清单不抄第二遍。

每个用例在 temp 里造自己那份东西（一份假 `pf.py` 加一条真的 `pf.cmd`，或一个一次性 git 仓库
加一条 worktree），跑真 CLI、跑完清掉；不碰真项目，不开会话，一条命令都在 5 s 档内。

    python conduct/tests/cases_bin.py            # 这个文件单独跑
    python conduct/tests/test_conduct_pipeline.py    # 跟全套件一起（注册见那份文件的 import 段）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness_env import TOOLS_DIR, _child_env

__all__ = ['TestPfCmdShim', 'TestDoctorRoot', 'TestCommandList']

PYTHON = sys.executable
BIN = TOOLS_DIR / 'bin'
PF = BIN / 'pf.py'
SHIM = BIN / 'pf.cmd'
GIT = shutil.which('git')
CMD = os.environ.get('COMSPEC', 'cmd.exe')

#: 假的 `pf.py`：每跑一次往 `runs.txt` 追一行（记下收到的 argv），然后按 `FAKE_PF_EXIT` 退出。
#: 一份假的就够了：这几条量的是「跑了几次、退出码是谁的」这两件读数，pf.py 自己干了什么活
#: 由真的那份负责。
FAKE_PF = '''\
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, 'runs.txt'), 'a', encoding='utf-8') as fh:
    fh.write(' '.join(sys.argv[1:]) + chr(10))
sys.exit(int(os.environ.get('FAKE_PF_EXIT', '0')))
'''


def _git_env():
    return dict(_child_env(), **{
        'GIT_AUTHOR_NAME': 'pf bin test', 'GIT_AUTHOR_EMAIL': 'bin@example.invalid',
        'GIT_COMMITTER_NAME': 'pf bin test', 'GIT_COMMITTER_EMAIL': 'bin@example.invalid',
    })


@unittest.skipUnless(os.name == 'nt', 'pf.cmd is the Windows shim')
class TestPfCmdShim(unittest.TestCase):
    """`pf.cmd`：一条命令恰好跑一次，退出码原样出去。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-cmd-')
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # 真 shim + 假 pf.py：shim 找的是自己旁边那个 `pf.py`，所以拷进 temp 就能整条驱动。
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
        return subprocess.run([CMD, '/c', str(self.dir / 'pf.cmd'), *args],
                              cwd=str(self.dir), capture_output=True, text=True,
                              encoding='utf-8', errors='replace', env=environ)
    # ── cases ────────────────────────────────────────────────────────────────
    def test_a_zero_exit_runs_the_command_once(self):
        proc = self.shim('version')
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(['version'], self.runs())

    def test_a_nonzero_exit_runs_the_command_once_and_keeps_its_code(self):
        """非零退出：不许被后备解释器再跑一遍，退出码也不许换人（两发 handoff 就是两发）。"""
        proc = self.shim('issue', 'top', exit_code='3')
        self.assertEqual(3, proc.returncode, proc.stderr)
        self.assertEqual(['issue top'], self.runs())

    def test_no_interpreter_on_path_runs_nothing(self):
        """PATH 上没有解释器：说一声就收，退出码是 shim 自己的 127，一回都不跑。"""
        env = _child_env()
        env['PATH'] = ''
        proc = self.shim('doctor', env=env)
        self.assertEqual(127, proc.returncode)
        self.assertEqual([], self.runs(), 'no interpreter was found, so nothing may run')
        self.assertIn('python', proc.stderr)


@unittest.skipUnless(GIT, 'git is not on PATH')
class TestDoctorRoot(unittest.TestCase):
    """`doctor` 的项目根与它转发的工具是同一个：worktree 里两边都回主树。"""

    UID = '11111111-2222-3333-4444-555555555555'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-doctor-')
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.main = base / 'main'
        self.main.mkdir()
        self.git('init', '-b', 'main')

        ledger = self.main / '.pf'
        (ledger / 'nodes' / self.UID).mkdir(parents=True)
        (ledger / '.gitignore').write_text('meta/\nworktrees/\ntemp/\n', encoding='utf-8')
        (ledger / '.gitattributes').write_text('** -text\n', encoding='utf-8')
        (ledger / 'AGENTS.md').write_text('# 记录目录\n', encoding='utf-8')
        (ledger / 'ideas.json').write_text(json.dumps({'nodes': {self.UID: {
            'title': '隔着 worktree 也要看得见', 'status': 'todo', 'mode': 'auto',
            'dependencies': []}}}, ensure_ascii=False, indent=2), encoding='utf-8')
        (ledger / 'nodes' / self.UID / 'spec.md').write_text('# spec\n', encoding='utf-8')
        # 合并驱动按登记过的那一种写（相对形式）：体检项就只剩「根对不对」这一件。
        self.git('config', 'merge.pf.driver', 'pf merge-ledger %O %A %B %P')

        # `.pf/` 不进库：worktree 是完整检出，但只检出进了库的那些 —— 所以 worktree 里没有
        # 记录目录，往上找 `.git` 目录的走法也找不到主树（这条 worktree 是个兄弟目录）。
        (self.main / 'README.md').write_text('# main\n', encoding='utf-8')
        self.git('add', 'README.md')
        self.git('commit', '-m', 'seed', '--no-gpg-sign')
        self.wt = base / 'wt'
        self.git('worktree', 'add', str(self.wt), '-b', 'pf/wt')

    # ── helpers ──────────────────────────────────────────────────────────────
    def git(self, *args):
        proc = subprocess.run([GIT, *args], cwd=str(self.main), capture_output=True, text=True,
                              encoding='utf-8', errors='replace', env=_git_env())
        self.assertEqual(0, proc.returncode,
                         f'git {" ".join(args)}:\n{proc.stdout}{proc.stderr}')
        return proc

    def pf(self, *args, cwd):
        return subprocess.run([PYTHON, '-X', 'utf8', str(PF), *[str(a) for a in args]],
                              cwd=str(cwd), capture_output=True, text=True,
                              encoding='utf-8', errors='replace', env=_child_env())

    def reported_root(self, proc):
        """`doctor` 印的那一行项目根，读回来。"""
        line = next(l for l in proc.stdout.splitlines() if l.startswith('项目根：'))
        return Path(line.split('：', 1)[1].strip())

    def dag_root(self, cwd):
        """`pf dag open` 印的绝对路径里那个根 —— 转发过去的那把工具实际认的根。

        `<root>/.pf/nodes/<id>/spec.md` 往上四层就是它。
        """
        proc = self.pf('dag', 'open', self.UID, cwd=cwd)
        self.assertEqual(0, proc.returncode, proc.stderr)
        return Path(proc.stdout.strip()).parents[3]

    # ── cases ────────────────────────────────────────────────────────────────
    def test_doctor_and_the_tool_it_fronts_share_the_root_in_a_worktree(self):
        """worktree 里：doctor 报的根，就是 `pf dag` 读到节点的那个根。"""
        self.assertTrue(self.dag_root(self.wt).samefile(self.main),
                        'pf dag 没有回主树 —— 这条用例的前提没成立')
        doctor = self.pf('doctor', cwd=self.wt)
        self.assertTrue(self.reported_root(doctor).samefile(self.main),
                        f'doctor 报的根不是主树：\n{doctor.stdout}')
        self.assertTrue(self.reported_root(doctor).samefile(self.dag_root(self.wt)),
                        'doctor 与 pf dag 认的不是同一个根')
        self.assertEqual(0, doctor.returncode, doctor.stdout)

    def test_the_two_roots_agree_inside_the_project_too(self):
        """主树里：两边同样对得上（回主树那条例外不影响平常这一路）。"""
        self.assertTrue(self.reported_root(self.pf('doctor', cwd=self.main)).samefile(
            self.dag_root(self.main)))
        self.assertTrue(self.reported_root(self.pf('doctor', cwd=self.main)).samefile(self.main))


class TestCommandList(unittest.TestCase):
    """命令清单只有一份：`<子命令> --help` 印什么，`help` 就印什么。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-help-')
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def pf(self, *args):
        return subprocess.run([PYTHON, '-X', 'utf8', str(PF), *args], cwd=str(self.dir),
                              capture_output=True, text=True, encoding='utf-8',
                              errors='replace', env=_child_env())

    def test_issue_help_prints_the_parser_help(self):
        verb, flag = self.pf('issue', 'help'), self.pf('issue', '--help')
        self.assertEqual(0, verb.returncode, verb.stderr)
        self.assertEqual(0, flag.returncode, flag.stderr)
        self.assertEqual(flag.stdout, verb.stdout)
        self.assertIn('weight', verb.stdout)

    def test_pf_help_names_every_word_it_routes(self):
        """`--help` 的那两段列的是路由表里的词 —— 加一个动词只改表里那一行。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location('pf_bin_entry', PF)
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)

        text = self.pf('--help').stdout
        routed = [row[0] for row in entry.SELF_VERBS]
        routed += [row[0] for row in entry.FORWARD_VERBS]
        routed += [row[0] for row in entry.FORWARD_GROUPS]
        for name in routed:
            self.assertIn(name, text, f'{name} 在路由表里，`pf --help` 却不列它')


if __name__ == '__main__':
    unittest.main()