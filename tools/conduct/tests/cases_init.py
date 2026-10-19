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

MARK = '# pf working system'


def run_init(root: Path, *args):
    return run_argv([STATE_MANAGE, 'init', '--project', str(root), *args], check=False)


def ledger_contract():
    """`tools/shared/ledger.py` is the one source: the gate's markers and its verdict are both read
    from it, not one literal copied."""
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
        """An ignore line cannot govern a file that is already tracked: the local kind must take it back
        out of the index, or the mode is lying."""
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
        # The usage note is not an init marker: with only it and neither of the two marker files,
        # the directory still counts as never init-ed
        (ledger / 'AGENTS.md').write_text('# a hand-written note\n', encoding='utf-8')

        proc = run_state(self.root, 'cycle', 'start', 'cyc', '--title', 't', '--tags', 'coding')

        self.assertNotEqual(0, proc.returncode)
        self.assertIn('init', proc.stderr, 'the refusal must hand over the command that fixes it')
        self.assertFalse((ledger / 'meta').exists(),
                         'the refusal must come before the lock creates anything')
        self.assertFalse((ledger / 'state.json').exists())

    def test_init_writes_the_ledger_usage_note(self):
        """The usage note belongs to the tool: every project init-ed on any harness must get the same
        one, and re-running is idempotent."""
        run_init(self.root)
        note = self.root / '.pf' / 'AGENTS.md'
        self.assertTrue(note.is_file(), 'init writes the usage note into the ledger')
        body = note.read_text(encoding='utf-8')
        self.assertIn('To change it, ask the user first', body)
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
        """The markers `init` lands on disk are the same ones the gate's `INIT_MARKERS` /
        `is_initialized` look for.

        Both directions are guarded. The one source gains a marker and `init` does not write it:
        `is_initialized` says False for a directory just init-ed. `init` renames one or drops one:
        the disk has one fewer, and the same verdict is False too. While the two agree, each is
        quiet; once they part, the symptom leaves `init` and moves deep into the tool -- every
        mutating command refuses a project that is plainly init-ed (or the other way round, a
        directory that was never init-ed is let through), and the error it reports sits several
        calls away from the one place that really needs changing. This case runs `init` through
        the real CLI and reads the markers from the one source by path, not one literal copied.
        """
        proc = run_init(self.root)
        self.assertEqual(0, proc.returncode, proc.stderr)

        contract = ledger_contract()
        ledger = self.root / '.pf'

        self.assertTrue(contract.is_initialized(ledger),
                        'the gate says this directory was never init-ed: the markers `init` '
                        'writes and the ones the one source reads have parted ways')
        for marker in contract.INIT_MARKERS:
            self.assertTrue((ledger / marker).is_file(),
                            f'the gate looks for {marker}, and `init` never wrote it to disk')


if __name__ == '__main__':
    unittest.main()
