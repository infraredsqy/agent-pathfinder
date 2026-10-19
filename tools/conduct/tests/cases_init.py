"""`init`: the one setup act, and the door it closes.

What is worth a case here, and why each of them fails silently otherwise:

  * `--local` really takes the directory out of the project's repository -- through the CLONE's own
    `info/exclude`, so no teammate sees a line about a directory they do not have
  * `--commit` takes that line back out, and re-running either way does not stack a second block
  * `--own-repo` really gives `.pf/` a repository of its own
  * `init` writes the ledger's usage note, and re-running it is idempotent
  * a `.pf/` without the two marker files is refused by every mutating command, and refused
    BEFORE anything is created -- the lock's own `mkdir` is what would otherwise leave an unignored
    ledger in the tree for the next `git add -A` to sweep up
  * the markers `init` writes on disk are the ones the gate reads off the contract -- the two sides
    agreeing today is a coincidence until one of them moves
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import GIT, STATE_MANAGE, git, run_argv, shared_loader  # noqa: E402

__all__ = ['TestInit']

MARK = '# pf 工作系统'


def run_init(root: Path, *args):
    return run_argv([STATE_MANAGE, 'init', '--project', str(root), *args], check=False)


def ledger_contract():
    """`tools/shared/ledger.py` 正本：闸门的记号与判定都从它这里读，一个字面量都不抄。"""
    return shared_loader().load_shared('ledger')


def run_state(root: Path, *args):
    return run_argv([STATE_MANAGE, *args], cwd=root, check=False)


@unittest.skipUnless(GIT, 'git is not on PATH')
class TestInit(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='conduct-init-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'proj'
        self.root.mkdir(parents=True)
        git(self.root, 'init', '-b', 'main')

    def excluded(self) -> bool:
        return git(self.root, 'check-ignore', '-q', '.pf', check=False).returncode == 0

    def exclude_path(self) -> Path:
        common = git(self.root, 'rev-parse', '--git-common-dir').stdout.strip()
        return (self.root / common).resolve() / 'info' / 'exclude'

    def test_local_is_the_default_and_keeps_the_directory_out_of_the_repo(self):
        proc = run_init(self.root)
        self.assertEqual(0, proc.returncode, proc.stderr)

        ledger = self.root / '.pf'
        self.assertTrue((ledger / '.gitignore').is_file())
        self.assertTrue((ledger / '.gitattributes').is_file())
        self.assertTrue((ledger / '.git').is_dir(), 'local mode gives the ledger its own history')
        self.assertTrue(self.excluded(), 'the clone must ignore it')
        self.assertIn(MARK, self.exclude_path().read_text(encoding='utf-8'))
        self.assertEqual('', git(self.root, 'status', '--porcelain').stdout,
                         'an ignored `.pf/` must leave the project repository status clean')

    def test_commit_takes_the_exclude_line_back_out(self):
        run_init(self.root)
        proc = run_init(self.root, '--commit')
        self.assertEqual(0, proc.returncode, proc.stderr)

        self.assertNotIn(MARK, self.exclude_path().read_text(encoding='utf-8'))
        self.assertFalse(self.excluded(), 'commit mode means the project tracks it')
        # The directory itself survives the mode switch: switching is not a migration.
        self.assertTrue((self.root / '.pf' / '.gitignore').is_file())

    def test_going_local_untracks_a_directory_the_project_already_committed(self):
        """忽略行管不了已经 track 的文件：本地档必须把它从 index 里退出来，否则档位在撒谎。"""
        run_init(self.root, '--commit')
        (self.root / '.pf' / 'ideas.json').write_text('{}', encoding='utf-8')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-m', 'seed', '--no-gpg-sign')
        self.assertTrue(git(self.root, 'ls-files', '--', '.pf').stdout.strip(),
                        'the fixture must start out tracked')

        proc = run_init(self.root, '--local', '--no-own-repo')
        self.assertEqual(0, proc.returncode, proc.stderr)

        self.assertEqual('', git(self.root, 'ls-files', '--', '.pf').stdout.strip(),
                         'local mode means the project repository lets it go')
        self.assertTrue((self.root / '.pf' / 'ideas.json').is_file(),
                        'untracking is not deleting: the files stay on disk')
        self.assertTrue(self.excluded())

    def test_re_running_never_stacks_a_second_ignore_block(self):
        run_init(self.root)
        run_init(self.root)
        body = self.exclude_path().read_text(encoding='utf-8')
        self.assertEqual(1, body.count(MARK))
        self.assertEqual(1, body.count('/.pf/'))

    def test_no_own_repo_leaves_plain_ignored_files(self):
        proc = run_init(self.root, '--no-own-repo')
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertFalse((self.root / '.pf' / '.git').exists())
        self.assertTrue(self.excluded(), 'still local, just without a history of its own')

    def test_a_pf_nobody_initialized_is_refused_before_anything_is_written(self):
        ledger = self.root / '.pf'
        (ledger / 'nodes').mkdir(parents=True)          # the shape, none of the markers
        # 使用约定不是 init 标记：只有它、没有那两枚文件时，照样算没 init 过
        (ledger / 'AGENTS.md').write_text('# 手写的约定\n', encoding='utf-8')

        proc = run_state(self.root, 'cycle', 'start', 'cyc', '--title', 't', '--tags', 'coding')

        self.assertNotEqual(0, proc.returncode)
        self.assertIn('init', proc.stderr, 'the refusal must hand over the command that fixes it')
        self.assertFalse((ledger / 'meta').exists(),
                         'the refusal must come before the lock creates anything')
        self.assertFalse((ledger / 'state.json').exists())

    def test_init_writes_the_ledger_usage_note(self):
        """使用约定归工具：任何宿主上 init 过的项目都该拿到同一份，且重跑幂等。"""
        run_init(self.root)
        note = self.root / '.pf' / 'AGENTS.md'
        self.assertTrue(note.is_file(), 'init writes the usage note into the ledger')
        body = note.read_text(encoding='utf-8')
        self.assertIn('要改它，先问用户', body)
        run_init(self.root)
        self.assertEqual(body, note.read_text(encoding='utf-8'),
                         're-running init rewrites the same note, it never stacks or drifts')

    def test_the_ledger_directory_a_project_commits_carries_its_own_ignores(self):
        run_init(self.root, '--commit', '--no-own-repo')
        body = (self.root / '.pf' / '.gitignore').read_text(encoding='utf-8')
        for name in ('meta/', 'worktrees/', 'temp/'):
            self.assertIn(name, body,
                          'the tool machinery must stay out of the project history too')

    def test_the_markers_init_writes_are_the_ones_the_gate_reads(self):
        """`init` 落在盘上的记号，与闸门 `INIT_MARKERS` / `is_initialized` 查的记号是同一份。

        两个方向都挡。正本多一枚记号而 `init` 不写它：`is_initialized` 对这个刚 init 过的目录判
        False。`init` 改名或漏写一枚：盘上少一枚，同一个判定也判 False。两个方向一致时两边各自
        安静；一旦分开，症状离开 `init` 跑到工具深处 —— 每个写命令都拒绝一个明明 init 过的项目
        （或者反过来，一个没 init 的目录被放行），报出来的错与真正该改的那一处隔着几层调用。
        这一条经真 CLI 跑 `init`，记号从正本按路径读，一个字面量都不抄。
        """
        proc = run_init(self.root)
        self.assertEqual(0, proc.returncode, proc.stderr)

        contract = ledger_contract()
        ledger = self.root / '.pf'

        self.assertTrue(contract.is_initialized(ledger),
                        '闸门说这个目录没 init 过：init 写的记号与正本查的记号分了家')
        for marker in contract.INIT_MARKERS:
            self.assertTrue((ledger / marker).is_file(),
                            f'闸门要查 {marker}，`init` 没有把它写到盘上')


if __name__ == '__main__':
    unittest.main()
