"""The ledger and the notes under it: where they land, the init test, worktree back to main tree,
and the cross-process lock."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from shared_env import NODE, SHARED_DIR, SharedCase, _child_env, _fresh_module, _shared

__all__ = ['TestTheLedgerContract', 'TestTheLockSerialisesANote', 'TestTheSessionNote']

#: One child process that takes the project's lock and keeps it until its parent says go: the only
#: way to see another process's lock from this one. `<project> <shared dir>`.
_HOLD_THE_LOCK = '\n'.join([
    'import importlib.util, sys',
    'from pathlib import Path',
    'project, shared = Path(sys.argv[1]), Path(sys.argv[2])',
    "spec = importlib.util.spec_from_file_location('pf_shared_loader', shared / 'loader.py')",
    'loader = importlib.util.module_from_spec(spec)',
    'spec.loader.exec_module(loader)',
    "lock = loader.load_by_path('pf_shared_ledgerlock', shared / 'ledgerlock.py')",
    "with lock.ledger_lock(project / '.pf'):",
    "    print('held', flush=True)",
    '    sys.stdin.readline()          # the parent releases the lock by closing this line',
])


class TestTheLockSerialisesANote(SharedCase):
    """`session-note.append` holds the project's lock across its read-modify-write."""

    def test_a_note_write_waits_for_the_lock_another_process_holds(self):
        """The other process holds it until this one says go: the write has to be *still waiting*
        while it is held, and land once it is not."""
        sb = self.make()
        holder = subprocess.Popen([sys.executable, '-c', _HOLD_THE_LOCK,
                                   str(sb.root), str(SHARED_DIR)],
                                  cwd=str(sb.root), env=_child_env(),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding='utf-8')
        self.addCleanup(holder.kill)
        self.assertEqual('held', holder.stdout.readline().strip(),
                         'the child could not take the lock')

        note = _fresh_module('session_note_lock_case', SHARED_DIR / 'session-note.py')
        written = []
        writer = threading.Thread(target=lambda: written.append(
            note.append(sb.root, NODE, 'run', 'session-under-the-lock')), daemon=True)
        writer.start()
        writer.join(timeout=0.5)

        self.assertTrue(writer.is_alive(),
                        'the note was written without waiting for the project lock')

        holder.stdin.write('\n')
        holder.stdin.flush()
        writer.join(timeout=30)
        holder.communicate(timeout=30)

        self.assertFalse(writer.is_alive(), 'the write never completed after the lock was let go')
        self.assertEqual([True], written)
        self.assertEqual(['session-under-the-lock'],
                         note.sessions_of(note.read(sb.root, NODE), 'run'))

    def test_another_spelling_of_the_same_lock_does_not_wait_for_anybody(self):
        """The process takes the lock spelled absolutely, then nests with the same directory spelled
        relative to the working directory. It is the one lock file, so the nested acquire takes the
        depth this process already holds instead of sitting out the timeout against its own file
        handle."""
        sb = self.make()
        lock = _fresh_module('pf_lock_second_spelling_case', SHARED_DIR / 'ledgerlock.py')
        taken = sb.root / '.pf'
        other = Path(os.path.relpath(taken, Path.cwd()))
        self.assertNotEqual(str(taken), str(other), 'the two spellings have to differ to pin this')
        self.assertEqual(taken.resolve(), other.resolve(), 'the two spellings name one directory')

        started = time.monotonic()
        with lock.ledger_lock(taken):
            with lock.ledger_lock(other, timeout=0.5):
                inside = True

        self.assertTrue(inside)
        self.assertLess(time.monotonic() - started, 1.0,
                        'the nested acquire sat out the timeout on its own lock')

    def test_a_caller_that_already_holds_the_lock_notes_inside_it(self):
        """A dag command writes a node's note inside its own read-modify-write, and the note's own
        copy of the lock module is a different module object. The record is the process's, so a
        nested acquire takes no second file lock and waits for nobody."""
        sb = self.make()
        lock = _fresh_module('pf_lock_loaded_under_another_name', SHARED_DIR / 'ledgerlock.py')
        note = _fresh_module('session_note_reentrant_case', SHARED_DIR / 'session-note.py')

        started = time.monotonic()
        with lock.ledger_lock(sb.root / '.pf'):
            changed = note.append(sb.root, NODE, 'discussion', 'session-inside-the-lock')

        self.assertTrue(changed)
        self.assertLess(time.monotonic() - started, 1.0,
                        'the nested acquire took the file lock a second time')


class TestTheLedgerContract(SharedCase):
    """`tools/shared/ledger.py`: the fixed landing places, the init test, and the
    worktree-to-main-tree rule."""

    def setUp(self):
        super().setUp()
        self.ledger = _shared('pf_shared_ledger', 'ledger.py')

    def test_the_fixed_landing_points_sit_under_the_ledger_directory(self):
        """The four landing places each sit in the right spot under `<project>/.pf`, and the `*_of`
        group agrees with the project group on every one of them.

        What this holds off: a landing place off by one slot. A mistake like `nodes/` written to
        `<project>/nodes/` reports nothing at all; from then on there are two records that do not
        recognize each other, and which one a write lands in depends on which function it went
        through.
        """
        project = self.root / 'a-project'
        ledger = project / '.pf'

        self.assertEqual(ledger, self.ledger.ledger_dir_for(project))
        self.assertEqual(ledger / 'meta', self.ledger.meta_dir_for(project))
        self.assertEqual(ledger / 'nodes', self.ledger.nodes_dir_for(project))
        self.assertEqual(ledger / 'config.json', self.ledger.config_path_for(project))
        self.assertEqual(self.ledger.meta_dir_for(project), self.ledger.meta_dir_of(ledger))
        self.assertEqual(self.ledger.nodes_dir_for(project), self.ledger.nodes_dir_of(ledger))
        self.assertEqual(self.ledger.config_path_for(project), self.ledger.config_path_of(ledger))

    def test_init_is_judged_by_the_two_markers_and_by_nothing_else(self):
        """What `is_initialized` reads in three situations: both markers there, one missing, and the
        directory not there yet.

        What this holds off: the test widened by one slot. Once "the directory is there, so it is
        init-ed" holds, a project that was never init-ed takes a write too, and that write lands in
        a bare directory with no `.gitignore` (so dispatch artifacts enter history from then on) and
        no merge driver.
        """
        project = self.root / 'a-project'
        ledger = self.ledger.ledger_dir_for(project)

        self.assertFalse(self.ledger.is_initialized(ledger),
                         'the directory not being there yet counted as init-ed')

        ledger.mkdir(parents=True)
        self.assertFalse(self.ledger.is_initialized(ledger),
                         'an empty directory counted as init-ed')

        (ledger / '.gitignore').write_text('meta/\n', encoding='utf-8')
        self.assertFalse(self.ledger.is_initialized(ledger), 'one marker alone counted as init-ed')

        (ledger / '.gitattributes').write_text('** -text\n', encoding='utf-8')
        self.assertTrue(self.ledger.is_initialized(ledger),
                        'both markers are there and it does not count as init-ed')

    def test_a_linked_worktree_root_maps_back_to_the_main_tree(self):
        """A worktree root is a full checkout carrying a copy of `.pf/` as of the fork -- accept it
        and settling lands on the stale snapshot, the two parallel lines cannot see each other's
        claims, and both commands return success.
        """
        main = self.root / 'main'
        (main / self.ledger.LEDGER_NAME).mkdir(parents=True)
        worktree = self.root / 'wt'
        gitdir = main / '.git' / 'worktrees' / 'n1'
        gitdir.mkdir(parents=True)
        worktree.mkdir()
        (worktree / '.git').write_text(f'gitdir: {gitdir}', encoding='utf-8')

        self.assertEqual(main, self.ledger.resolve_project_root(worktree))

    def test_everything_that_is_not_a_linked_worktree_stays_where_it_is(self):
        """A plain directory, a `.git` that is a directory (a checkout root of its own), and a
        submodule-shaped `gitdir` (pointing at `.git/modules/...`) all stay in cwd.

        What this holds off: taking a submodule for a worktree. A submodule is supposed to carry a
        ledger of its own, and climbing from its `gitdir` up to the project above puts every write
        in the wrong project.
        """
        plain = self.root / 'plain'
        plain.mkdir()
        self.assertEqual(plain, self.ledger.resolve_project_root(plain))

        checkout = self.root / 'checkout'
        (checkout / '.git').mkdir(parents=True)
        self.assertEqual(checkout, self.ledger.resolve_project_root(checkout))

        main = self.root / 'main'
        submodule_gitdir = main / '.git' / 'modules' / 'sub'
        submodule_gitdir.mkdir(parents=True)
        submodule = self.root / 'sub'
        submodule.mkdir()
        (submodule / '.git').write_text(f'gitdir: {submodule_gitdir}', encoding='utf-8')
        self.assertEqual(submodule, self.ledger.resolve_project_root(submodule))

    def test_a_worktree_whose_main_tree_has_no_ledger_stays_where_it_is(self):
        """When the main tree has no ledger (it is not a pipeline project) nothing moves, so the
        later error speaks up on the worker's own cwd."""
        main = self.root / 'bare-main'
        gitdir = main / '.git' / 'worktrees' / 'n2'
        gitdir.mkdir(parents=True)
        worktree = self.root / 'wt2'
        worktree.mkdir()
        (worktree / '.git').write_text(f'gitdir: {gitdir}', encoding='utf-8')

        self.assertEqual(worktree, self.ledger.resolve_project_root(worktree))


class TestTheSessionNote(SharedCase):
    """`tools/shared/session-note.py`: the shape of the record on disk, and what a write-then-read
    reads back."""

    def test_the_record_holds_two_roles_and_a_version(self):
        """A note is two named lists and a version: `ROLES`'s two keys, and `VERSION` landing on
        `v`.

        What this holds off: the roles and the shape drifting apart. A reader asks `run` for "which
        session to open", the writer renames the key (or pushes in a third role), and the fetch
        always comes back empty -- an empty list looks exactly like "not written yet".
        """
        note = _fresh_module('pf_session_note_shape_case', SHARED_DIR / 'session-note.py')

        self.assertEqual(2, len(note.ROLES))
        self.assertEqual({'discussion', 'run'}, set(note.ROLES))
        self.assertEqual({'v', 'discussion', 'run'}, set(note.read(self.root, NODE)))
        self.assertEqual(note.VERSION, note.read(self.root, NODE)['v'])
        self.assertEqual([], note.sessions_of(note.read(self.root, NODE), 'run'))

    def test_a_written_session_comes_back_the_same_from_disk(self):
        """A session `append` wrote comes back as it is from `read` / `sessions_of`, by role;
        writing the same one twice lands one entry.

        What this holds off: a retry growing a duplicate entry. `next --spawn` re-run after a
        half-way failure would put the same run in the note twice, and a human leafing through the
        session history later reads it as two dispatches. The entry that stores `null` for `at=None`
        holds off a made-up moment: a made-up moment looks exactly like a real one in the record.
        """
        note = _fresh_module('pf_session_note_roundtrip_case', SHARED_DIR / 'session-note.py')

        self.assertTrue(note.append(self.root, NODE, 'run', 'sess_abc123'))
        self.assertFalse(note.append(self.root, NODE, 'run', 'sess_abc123'),
                         'the same one was written twice')
        self.assertTrue(note.append(self.root, NODE, 'discussion', 'dsh-session-1'))
        self.assertTrue(note.append(self.root, NODE, 'run', 'imported-session', at=None))

        landed = self.root / '.pf' / 'meta' / 'sessions' / f'{NODE}.json'
        self.assertTrue(landed.is_file(), 'the note did not land in meta/sessions/<node id>.json')
        self.assertEqual({'v', 'discussion', 'run'},
                         set(json.loads(landed.read_text(encoding='utf-8'))))

        back = note.read(self.root, NODE)
        self.assertEqual(['sess_abc123', 'imported-session'], note.sessions_of(back, 'run'))
        self.assertEqual(['dsh-session-1'], note.sessions_of(back, 'discussion'))
        self.assertEqual('zcode', back['run'][0]['host'],
                         'the host of the `sess_` prefixed entry was read wrong')
        self.assertEqual('dsh', back['discussion'][0]['host'])
        self.assertIsInstance(back['run'][0]['at'], str)
        self.assertIsNone(back['run'][1]['at'],
                          'the entry whose moment is unknown had one made up for it')

    def test_a_note_from_a_version_this_reader_does_not_know_reads_empty(self):
        """A version newer than this reader reads as an empty note: a value it cannot understand
        must not be passed on as a value it understood.

        What this holds off: a half read. The future note's fields would be explained by this
        version's reader in its own shape, and a value would look present while meaning the wrong
        thing.
        """
        note = _fresh_module('pf_session_note_version_case', SHARED_DIR / 'session-note.py')
        landed = self.root / '.pf' / 'meta' / 'sessions' / f'{NODE}.json'
        landed.parent.mkdir(parents=True, exist_ok=True)
        landed.write_text(json.dumps({'v': note.VERSION + 1,
                                      'discussion': [{'session': 'x'}], 'run': []}),
                          encoding='utf-8')

        back = note.read(self.root, NODE)

        self.assertEqual([], note.sessions_of(back, 'discussion'))
        self.assertEqual(note.VERSION, back['v'])