"""记录目录与它底下的便条：落点、init 判据、worktree 回主树，以及那把跨进程锁。"""

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
    """`tools/shared/ledger.py`：固定落点、init 判据、worktree 回主树那条判定。"""

    def setUp(self):
        super().setUp()
        self.ledger = _shared('pf_shared_ledger', 'ledger.py')

    def test_the_fixed_landing_points_sit_under_the_ledger_directory(self):
        """四个落点各自落在 `<project>/.pf` 底下的正确位置，`*_of` 那一组与项目那一组给出同一处。

        挡的是落点错一格：`nodes/` 写到 `<project>/nodes/` 这种错不报任何东西，只是从此有两份
        互不相认的记录，写到哪一份取决于这一笔走的是哪个函数。
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
        """`is_initialized` 在两枚记号齐全、缺一枚、目录还不存在三种情形下的读数。

        挡的是判据放宽一格：一旦「目录在就算 init 过」，没 init 的项目也会收下一次写，那一笔落进
        一个没有 `.gitignore`（派发物从此进历史）也没有合并驱动的裸目录里。
        """
        project = self.root / 'a-project'
        ledger = self.ledger.ledger_dir_for(project)

        self.assertFalse(self.ledger.is_initialized(ledger), '目录还不存在就算 init 过了')

        ledger.mkdir(parents=True)
        self.assertFalse(self.ledger.is_initialized(ledger), '空目录就算 init 过了')

        (ledger / '.gitignore').write_text('meta/\n', encoding='utf-8')
        self.assertFalse(self.ledger.is_initialized(ledger), '只有一枚记号就算 init 过了')

        (ledger / '.gitattributes').write_text('** -text\n', encoding='utf-8')
        self.assertTrue(self.ledger.is_initialized(ledger), '两枚记号齐全却不算 init 过')

    def test_a_linked_worktree_root_maps_back_to_the_main_tree(self):
        """worktree 根是完整检出，里面有一份 fork 时刻的 `.pf/` 快照 —— 认了它，结转就落在旧快照
        上，两条并行线互相看不见对方的认领，而两条命令都成功返回。
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
        """普通目录、`.git` 是目录（自己的检出根）、submodule 形态的 `gitdir`（指向
        `.git/modules/...`）三种都留在 cwd。

        挡的是把 submodule 当 worktree：submodule 里本来就该有一份自己的记录目录，顺着它的
        `gitdir` 爬到上面那个项目去，写下的每一笔都落错了项目。
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
        """主树里没有记录目录（不是管线项目）时不挪，让后面的错误当场响在 worker 的 cwd 上。"""
        main = self.root / 'bare-main'
        gitdir = main / '.git' / 'worktrees' / 'n2'
        gitdir.mkdir(parents=True)
        worktree = self.root / 'wt2'
        worktree.mkdir()
        (worktree / '.git').write_text(f'gitdir: {gitdir}', encoding='utf-8')

        self.assertEqual(worktree, self.ledger.resolve_project_root(worktree))


class TestTheSessionNote(SharedCase):
    """`tools/shared/session-note.py`：盘上那份记录的形状，与写进去再读回来的读数。"""

    def test_the_record_holds_two_roles_and_a_version(self):
        """一条便条由两个具名列表与一个版本号组成：`ROLES` 两个键、`VERSION` 落在 `v` 上。

        挡的是角色与形状漂开：读者按 `run` 取「该开哪个会话」，写下的人换了个键名（或塞进第三种
        角色），取回来永远是空的 —— 空列表看起来和「还没写过」一模一样。
        """
        note = _fresh_module('pf_session_note_shape_case', SHARED_DIR / 'session-note.py')

        self.assertEqual(2, len(note.ROLES))
        self.assertEqual({'discussion', 'run'}, set(note.ROLES))
        self.assertEqual({'v', 'discussion', 'run'}, set(note.read(self.root, NODE)))
        self.assertEqual(note.VERSION, note.read(self.root, NODE)['v'])
        self.assertEqual([], note.sessions_of(note.read(self.root, NODE), 'run'))

    def test_a_written_session_comes_back_the_same_from_disk(self):
        """`append` 写下的会话，`read` / `sessions_of` 按角色原样读回来；同一条写两遍只落一条。

        挡的是重试长出重复条目：`next --spawn` 在一次半途失败后重跑，便条里同一次运行出现两次，
        人事后翻会话历史时把它读成两次派发。`at=None` 存 `null` 那一笔挡的是编一个时刻出来：
        编出来的时刻与真的时刻在记录里长得一样。
        """
        note = _fresh_module('pf_session_note_roundtrip_case', SHARED_DIR / 'session-note.py')

        self.assertTrue(note.append(self.root, NODE, 'run', 'sess_abc123'))
        self.assertFalse(note.append(self.root, NODE, 'run', 'sess_abc123'), '同一条写了两遍')
        self.assertTrue(note.append(self.root, NODE, 'discussion', 'dsh-session-1'))
        self.assertTrue(note.append(self.root, NODE, 'run', 'imported-session', at=None))

        landed = self.root / '.pf' / 'meta' / 'sessions' / f'{NODE}.json'
        self.assertTrue(landed.is_file(), '便条没落进 meta/sessions/<节点 id>.json')
        self.assertEqual({'v', 'discussion', 'run'},
                         set(json.loads(landed.read_text(encoding='utf-8'))))

        back = note.read(self.root, NODE)
        self.assertEqual(['sess_abc123', 'imported-session'], note.sessions_of(back, 'run'))
        self.assertEqual(['dsh-session-1'], note.sessions_of(back, 'discussion'))
        self.assertEqual('zcode', back['run'][0]['host'], '`sess_` 前缀那一条的宿主认错了')
        self.assertEqual('dsh', back['discussion'][0]['host'])
        self.assertIsInstance(back['run'][0]['at'], str)
        self.assertIsNone(back['run'][1]['at'], '不知道时刻的那一笔被编了一个时刻出来')

    def test_a_note_from_a_version_this_reader_does_not_know_reads_empty(self):
        """比这份读者新的版本号读成空便条：读不懂的值不许当成读懂的值往下传。

        挡的是半读：未来那份便条的字段被这一版读者按自己的形状解释出来，值看着有、含义是错的。
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