"""The shared layer: one loader, one reader/writer, one lock -- and the records they serve.

`tools/shared/` holds what more than one tool needs, and every file in it is loaded by path. These
cases pin the three properties the rest of the tree leans on: a file is loaded through the one
loader (and nothing else in the shipped tree loads by path), a record that cannot be read says so in
the same words wherever it is read, and a note's read-modify-write holds the project's ledger lock.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE  # noqa: E402
from harness import _run_proc, run_dag, run_signal  # noqa: E402
from harness_env import (HANDOFF_PY, SIGNAL_PY, TOOLS_DIR, _child_env, _fresh_module, init_markers,
                         load_pipeline, shared_loader)  # noqa: E402

__all__ = ['TestTheOneLoader', 'TestOneSharedDoor', 'TestOneReaderOneWording',
           'TestTheLockSerialisesANote', 'TestTheMarkerWaitsOutAReader', 'TestTheMarkerContract',
           'TestOneWriterPerRecord']

CONDUCT_DIR = TOOLS_DIR / 'conduct'
SHARED_DIR = TOOLS_DIR / 'shared'
ISSUE_MANAGE = TOOLS_DIR / 'issue' / 'issue_manager.py'
SPAWN_DIR = CONDUCT_DIR / 'spawn'


def _spawn_sources():
    """The spawner half of the tree, both languages: the Python files there and the PowerShell ones."""
    return sorted(SPAWN_DIR.glob('*.py')) + sorted(SPAWN_DIR.glob('*.ps1'))


def _marker_module():
    """`spawn/marker.py`, loaded the way its callers load it -- one copy per process."""
    return shared_loader().load_by_path('conduct_spawn_marker', SPAWN_DIR / 'marker.py',
                                         register=True, reuse=True)


def _shared(name, filename):
    return shared_loader().load_by_path(name, SHARED_DIR / filename, register=True, reuse=True)


def _shipped_sources():
    """Every Python file the tools ship. The tests are not among them: the harness keeps its own
    fresh-load helper on purpose, and a case's own loader is not a shipped mechanism."""
    return ([CONDUCT_DIR / 'ledger-merge.py']
            + [path for cluster in ('pipeline', 'spawn')
               for path in sorted((CONDUCT_DIR / cluster).glob('*.py'))]
            + sorted((CONDUCT_DIR / 'conduct_pipeline_impl').glob('*.py'))
            + sorted((CONDUCT_DIR / 'conduct_state').glob('*.py'))
            + sorted((CONDUCT_DIR / 'ledger_merge').glob('*.py'))
            + sorted((TOOLS_DIR / 'dag').glob('*.py'))
            + sorted((TOOLS_DIR / 'issue').glob('*.py'))
            + sorted(SHARED_DIR.glob('*.py'))
            + sorted((TOOLS_DIR / 'bin').glob('*.py')))


def _definitions():
    """(file, name) for every top-level function and class the shipped tools define."""
    for path in _shipped_sources():
        for node in ast.parse(path.read_text(encoding='utf-8')).body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                yield path, node.name


def _path_load_calls(path: Path):
    """Every `spec_from_file_location(...)` call site in one shipped file, as AST nodes."""
    return [node for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'spec_from_file_location']


def _relative(path: Path) -> str:
    return str(path.relative_to(TOOLS_DIR)).replace('\\', '/')


def _init_ledger(root: Path) -> Path:
    """A `.pf/` carrying the two files `init` writes, plus the issue tool's own directory."""
    ledger = init_markers(root)
    (ledger / 'issues').mkdir(parents=True, exist_ok=True)
    return ledger


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


class TestTheOneLoader(unittest.TestCase):
    """`tools/shared/loader.py`: the three knobs, and what each one is for."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-loader-case-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.loader = shared_loader()

    def _source(self, filename='pf_case_module.py', body='VALUE = 1\n'):
        path = self.root / filename
        path.write_text(body, encoding='utf-8')
        return path

    def test_the_default_is_a_copy_of_its_own_that_touches_nothing(self):
        path = self._source()

        first = self.loader.load_by_path('pf_case_private', path)
        second = self.loader.load_by_path('pf_case_private', path)

        self.assertIsNot(first, second, 'each load at the default is a module of its own')
        self.assertEqual(1, first.VALUE)
        self.assertNotIn('pf_case_private', sys.modules, 'the default load registers nothing')

    def test_fresh_drops_what_an_earlier_load_registered(self):
        path = self._source()
        first = self.loader.load_by_path('pf_case_fresh', path, register=True)
        self.assertIs(sys.modules['pf_case_fresh'], first)

        second = self.loader.load_by_path('pf_case_fresh', path, register=True, fresh=True)

        self.assertIsNot(second, first, 'fresh shares no module object with the last load')
        self.assertIs(sys.modules['pf_case_fresh'], second)

    def test_reuse_hands_back_the_copy_already_registered_for_this_file(self):
        path = self._source()

        first = self.loader.load_by_path('pf_case_reuse', path, register=True, reuse=True)
        second = self.loader.load_by_path('pf_case_reuse', path, register=True, reuse=True)

        self.assertIs(first, second, 'reuse is how one process keeps one copy of a file')

        other = self._source('pf_case_reuse_other.py', 'VALUE = 2\n')
        third = self.loader.load_by_path('pf_case_reuse', other, register=True, reuse=True)

        self.assertIsNot(third, first, 'another file under the name is a module, not a reuse')
        self.assertEqual(2, third.VALUE)

    def test_a_package_resolves_its_own_submodules(self):
        package = self.root / 'pf_case_package'
        package.mkdir()
        (package / '__init__.py').write_text('from . import leaf\nMARK = leaf.MARK\n',
                                             encoding='utf-8')
        (package / 'leaf.py').write_text("MARK = 'leaf'\n", encoding='utf-8')

        module = self.loader.load_by_path('pf_case_package', package / '__init__.py',
                                          package_dir=package)

        self.assertEqual('leaf', module.MARK)
        self.assertIs(sys.modules['pf_case_package'], module, 'a package is registered to load')


class TestOneSharedDoor(unittest.TestCase):
    """What the tree loads with, writes with and reads with: one of each, defined once."""

    def test_nothing_reaches_a_module_through_sys_path(self):
        """A neighbour is loaded by path. Putting the tool tree on `sys.path` is what hands a second
        caller the first caller's modules."""
        offenders = set()
        for path in _shipped_sources():
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if (isinstance(node, ast.Attribute) and node.attr == 'path'
                        and isinstance(node.value, ast.Name) and node.value.id == 'sys'):
                    offenders.add(f'{_relative(path)}: sys.path')
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'import_module'):
                    offenders.add(f'{_relative(path)}: importlib.import_module')
        self.assertEqual(set(), offenders)

    def test_every_path_load_in_the_shipped_tree_goes_through_the_one_loader(self):
        """Counted at the call sites themselves: a shipped file carries at most one load-by-path
        call, so a file that already has its bootstrap cannot gain a second one beside it. The only
        site that does not name the shared loader is the loader's own, which takes the name as an
        argument."""
        sites = {path: _path_load_calls(path) for path in _shipped_sources()}
        crowded = sorted(f'{_relative(path)}: {len(found)} load-by-path call sites'
                         for path, found in sites.items() if len(found) > 1)
        self.assertEqual([], crowded, 'a shipped file loads by path more than once')

        elsewhere = sorted({_relative(path) for path, found in sites.items() for node in found
                            if not (node.args and isinstance(node.args[0], ast.Constant)
                                    and node.args[0].value == 'pf_shared_loader')})
        self.assertEqual(['shared/loader.py'], elsewhere,
                         'a file loads a module without going through tools/shared/loader.py')

    def test_the_shared_primitives_are_defined_once(self):
        owners = {name: _relative(path) for path, name in _definitions()
                  if name in ('atomic_write_json', 'read_json_strict', 'replace_with_retry',
                              'unlink_with_retry', 'load_by_path')}
        self.assertEqual({'atomic_write_json': 'shared/jsonio.py',
                          'read_json_strict': 'shared/jsonio.py',
                          'replace_with_retry': 'shared/jsonio.py',
                          'unlink_with_retry': 'shared/jsonio.py',
                          'load_by_path': 'shared/loader.py'}, owners)

    def test_the_per_file_copies_of_those_primitives_are_gone(self):
        """`_replace_with_retry` and `_unlink_with_retry` are the names the copies carried."""
        left = sorted(f'{_relative(path)}:{name}' for path, name in _definitions()
                      if name in ('_replace_with_retry', '_unlink_with_retry'))
        self.assertEqual([], left)

    def test_one_reader_named_read_json_is_left(self):
        """Two `_read_json` with two failure conventions was the drift; the tree keeps the CLI
        layer's door and every other reader asks the shared one."""
        owners = sorted(_relative(path) for path, name in _definitions() if name == '_read_json')
        self.assertEqual(['conduct/conduct_state/store.py'], owners)

    def test_the_replace_reaches_the_filesystem_from_one_place(self):
        """`os.replace` 的调用点只有 `shared/jsonio.py` 一处。

        换名落盘是同一个原语：谁来写、写的是 JSON 还是文本，改的都是那一处（`replace_with_retry`）。
        各文件自带一份的话，`replace_with_retry` 往后的每一次修改都会漏掉别人。
        """
        callers = []
        for path in _shipped_sources():
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'replace'
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == 'os'):
                    callers.append(f'{_relative(path)}:{node.lineno}')

        self.assertEqual(1, len(callers), f'调用点不止一处：{callers}')
        self.assertTrue(callers[0].startswith('shared/jsonio.py:'), callers)

    def test_the_spawner_module_is_one_copy_per_process(self):
        """`writer._spawner()`：一次派发问它三次（开那一次会话、读会话 id、读宿主），三次拿到的是
        同一个模块对象 —— 装载器的 `register` + `reuse` 一对，和 `spawn/host.py` 走的是同一条。
        """
        writer = load_pipeline().writer

        first = writer._spawner()

        self.assertIs(first, writer._spawner())
        self.assertIs(sys.modules['conduct_spawn'], first)


class TestOneReaderOneWording(Base):
    """A `.pf/` record that cannot be read: the same words, whichever tool asks."""

    def _broken(self, root: Path, *parts, body=b'\xff\xfe not utf-8 at all'):
        path = root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def test_the_pipeline_reads_a_non_utf8_record_with_the_shared_words(self):
        sb = self.make()
        self._broken(sb.root, '.pf', 'state.json')

        with self.assertRaises(ValueError) as caught:
            load_pipeline().load_state(sb.root)

        self.assertEqual('JsonReadError', type(caught.exception).__name__)
        self.assertIn('state.json is not valid UTF-8', str(caught.exception))

    def test_the_graph_tool_refuses_a_non_utf8_graph_instead_of_dying(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-nonutf8-dag-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        _init_ledger(root)
        self._broken(root, '.pf', 'ideas.json')

        proc = run_dag(root, 'list', check=False)

        self.assertEqual(1, proc.returncode)
        self.assertIn('ideas.json is not valid UTF-8', proc.stderr)
        self.assertNotIn('Traceback', proc.stderr)

    def test_the_pool_tool_refuses_a_non_utf8_pool_instead_of_dying(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-nonutf8-issue-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        _init_ledger(root)
        self._broken(root, '.pf', 'issues', 'issues.json')

        proc = _run_proc(root, [ISSUE_MANAGE, 'list'], check=False)

        self.assertEqual(1, proc.returncode)
        self.assertIn('issues.json is not valid UTF-8', proc.stderr)
        self.assertNotIn('Traceback', proc.stderr)

    def test_a_graph_that_is_not_json_keeps_the_validate_hint(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-badjson-dag-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        _init_ledger(root)
        self._broken(root, '.pf', 'ideas.json', body=b'{not json')

        proc = run_dag(root, 'list', check=False)

        self.assertEqual(1, proc.returncode)
        self.assertIn('ideas.json parse failure', proc.stderr)
        self.assertIn('Hint: run "validate"', proc.stderr)


class TestTheLockSerialisesANote(Base):
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


class TestTheMarkerWaitsOutAReader(Base):
    """The broken-chain marker goes through the shared primitive: a reader that lets go mid-retry
    costs nothing, and a reader that never lets go is an error with no leftovers."""

    def test_a_reader_that_lets_go_mid_retry_still_gets_the_marker(self):
        sb = self.make()
        handoff = _fresh_module('handoff_marker_case', HANDOFF_PY)
        marker = sb.root / '.pf' / 'meta' / 'CONDUCT-CALLBACK-FAILED-cyc.json'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"stale": true}', encoding='utf-8')
        held = open(marker, 'r', encoding='utf-8')
        self.addCleanup(held.close)
        threading.Timer(0.2, held.close).start()
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            handoff._write_marker(sb.root, 'cyc', 'advance', 'the pipeline refused the signal')

        record = json.loads(marker.read_text(encoding='utf-8'))
        self.assertEqual('the pipeline refused the signal', record['reason'])
        self.assertIn('WARNING: conduct handoff failed', stderr.getvalue())

    def test_a_reader_that_never_lets_go_fails_bounded_and_leaves_no_temp(self):
        sb = self.make()
        jsonio = _shared('pf_shared_jsonio', 'jsonio.py')
        target = sb.root / '.pf' / 'meta' / 'held.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"stale": true}', encoding='utf-8')
        held = open(target, 'r', encoding='utf-8')
        self.addCleanup(held.close)

        started = time.monotonic()
        with self.assertRaises(PermissionError):
            jsonio.atomic_write_json(target, {'fresh': True})
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 3.0, 'the retry is bounded, it does not hang')
        self.assertEqual([], sorted(p.name for p in target.parent.iterdir() if p.suffix == '.tmp'),
                         'a failed write leaves its temp file behind')
        self.assertEqual({'stale': True}, json.loads(target.read_text(encoding='utf-8')))


class TestTheMarkerContract(Base):
    """断链降级标记：记录形状与落点的正本在 `spawn/marker.py`。

    标记是人工发现「链断在这里」的唯一入口，所以这一组盯三件事：文件名与记录形状只在正本里拼一次
    （两个 spawner 都不再自拼落点）、python 侧写它走的是全树那条带重试的原子写、条子那一拍与 spawner
    那一拍交出的是同一个形状。壳侧那份实现是物理上无法共享的例外（运行点可能没有 python），它的形状
    由 `cases_cli.py` 的等价性用例钉住。
    """

    def test_the_marker_is_spelled_in_exactly_one_file(self):
        owners = sorted({_relative(path)
                         for path in set(_shipped_sources()) | set(_spawn_sources())
                         if 'CONDUCT-CALLBACK-FAILED' in path.read_text(encoding='utf-8')})
        self.assertEqual(['conduct/spawn/marker.py'], owners,
                         '标记的文件名被抄到了第二处：改一处漏一处的正是这种抄写')

    def test_the_spawners_never_assemble_the_marker_path(self):
        """`.pf` / `meta` / 文件名模板一个都不该出现在 spawner 侧：落点由 argv 交进来。"""
        for path in sorted(SPAWN_DIR.glob('*.ps1')):
            source = path.read_text(encoding='utf-8')
            for gone in ('.pf', 'meta', 'CONDUCT-CALLBACK-FAILED'):
                self.assertNotIn(gone, source, f'{path.name} 又自拼起标记的落点了')

    def test_one_shell_serves_both_spawners_and_the_hosts_stay_explicit(self):
        """外壳一份（`spawn-common.ps1`），宿主差异留各自的薄壳：参数面还是自己的。"""
        shell = SPAWN_DIR / 'spawn-common.ps1'
        self.assertTrue(shell.is_file(), '共用外壳不在')
        sources = {path.name: path.read_text(encoding='utf-8')
                   for path in sorted(SPAWN_DIR.glob('*.ps1'))}
        owners = sorted(name for name, source in sources.items()
                        if 'function Write-CallbackFailureMarker' in source)
        self.assertEqual(['spawn-common.ps1'], owners, '写标记的实现不止一份')

        dsh, zcode = sources['dsh-spawn.ps1'], sources['zcode-spawn.ps1']
        for name, source in (('dsh-spawn.ps1', dsh), ('zcode-spawn.ps1', zcode)):
            self.assertIn("'spawn-common.ps1'", source, f'{name} 没有 dot-source 共用外壳')
            for shared in ('-CycleId', '-PromptFile', '-WorkspacePath', '-MarkerPath'):
                self.assertIn(shared, source, f'{name} 少了共用参数 {shared}')
        self.assertIn('-WebUrl', dsh)
        self.assertNotIn('-WebUrl', zcode)
        self.assertIn('-NoVerify', zcode)
        self.assertNotIn('-NoVerify', dsh)

    def test_the_marker_path_follows_the_ledger_name(self):
        """`.pf` 一改名，标记跟着记录走 —— 派发侧不再有一份自己的拼法。"""
        sb = self.make()
        store = shared_loader().load_by_path('pf_conduct_state_store',
                                              CONDUCT_DIR / 'conduct_state' / 'store.py',
                                              register=True, reuse=True)
        marker = _marker_module()
        handoff = _fresh_module('handoff_marker_ledger_case', HANDOFF_PY)
        self.addCleanup(setattr, store, 'LEDGER_NAME', store.LEDGER_NAME)
        store.LEDGER_NAME = '.pf-moved'

        with contextlib.redirect_stderr(io.StringIO()):
            handoff._write_marker(sb.root, 'cyc', 'advance', '搬过家')

        path = marker.marker_path(sb.root, 'cyc')
        self.assertEqual('.pf-moved', path.parent.parent.name)
        self.assertTrue(path.is_file(), '标记落在了工具不读的位置（自拼 `.pf` 的那条路）')

    def test_the_two_writers_leave_one_shape(self):
        """条子那一拍写下的标记，与正本的形状、那句人话一字不差。"""
        sb = self.make()
        marker = _marker_module()
        handoff = _fresh_module('handoff_marker_shape_case', HANDOFF_PY)

        with contextlib.redirect_stderr(io.StringIO()):
            handoff._write_marker(sb.root, 'cyc', 'advance', '条子那一拍断了')

        written = json.loads(marker.marker_path(sb.root, 'cyc').read_text(encoding='utf-8'))
        self.assertEqual(sorted(marker.record('cyc', 'advance', 'x')), sorted(written))
        self.assertEqual(marker.DEFAULT_NOTE, written['note'])
        self.assertEqual({}, written['extra'], '没有宿主事实时也给一个空槽，形状不随调用方变')
        self.assertEqual('cyc', written['cycle_id'])
        self.assertEqual('advance', written['phase'])

    def test_the_write_is_the_retrying_one(self):
        """写标记走的不是裸 `os.replace`：读者此刻正开着目标文件，重试该把它救回来。"""
        sb = self.make()
        marker = _marker_module()
        path = marker.marker_path(sb.root, 'cyc')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"stale": true}', encoding='utf-8')
        held = open(path, 'r', encoding='utf-8')
        self.addCleanup(held.close)
        threading.Timer(0.2, held.close).start()

        marker.write(path, marker.record('cyc', 'advance', '读者松手了', workspace=sb.root))

        self.assertEqual('读者松手了', json.loads(path.read_text(encoding='utf-8'))['reason'])


class TestOneWriterPerRecord(Base):
    """One signal writer, and the record it lands is the store's own."""

    def test_signal_py_carries_no_writer_of_its_own(self):
        source = (CONDUCT_DIR / 'pipeline' / 'signal.py').read_text(encoding='utf-8')
        for gone in ('os.replace', 'tempfile', '%Y-%m-%dT%H:%M:%SZ', 'json.dump'):
            self.assertNotIn(gone, source, f'signal.py writes the record itself again ({gone})')
        self.assertIn('_write_signal', source, 'signal.py writes the record through the store')

    def test_the_signal_comes_out_in_the_stores_shape(self):
        sb = self.make()
        signal_mod = _fresh_module('conduct_signal_record_case', SIGNAL_PY)

        signal_mod.write_signal('cyc', NODE, 'completed', 'done', {'deliverable': 'x'},
                                base_dir=sb.root / '.pf' / 'signals')

        sig = json.loads((sb.root / '.pf' / 'signals' / 'cyc' / f'{NODE}.json')
                         .read_text(encoding='utf-8'))
        self.assertEqual(['cycle', 'node', 'outputs', 'status', 'summary', 'timestamp'], sorted(sig))
        self.assertEqual('done', sig['summary'])
        self.assertIsNotNone(datetime.strptime(sig['timestamp'], signal_mod._store().ISO_FMT),
                             'the moment is the store format, not a literal spelled here again')

    def test_a_named_base_dir_serves_one_write_and_leaves_the_binding_alone(self):
        """显式落点是这一笔的事：调用方点名的目录收下这一笔，本拷贝的绑定一个字不动。

        这份 store 在一个进程里是共用的：写一笔条子顺手把绑定挪到自己的项目，下一个漏传
        `base_dir` 的调用方就静默写进上一个项目里去。
        """
        sb = self.make()
        other = self.make()
        signal_mod = _fresh_module('conduct_signal_base_dir_case', SIGNAL_PY)
        store = signal_mod._store()
        store.bind_paths(sb.root / '.pf')

        signal_mod.write_signal('cyc', NODE, 'completed', 'B', {},
                                base_dir=other.root / '.pf' / 'signals')

        self.assertTrue((other.root / '.pf' / 'signals' / 'cyc' / f'{NODE}.json').is_file(),
                        '显式落点那一笔没落在点名的目录里')
        self.assertFalse((sb.root / '.pf' / 'signals' / 'cyc' / f'{NODE}.json').exists(),
                         '显式落点那一笔写进了绑定指向的项目')
        self.assertEqual(store.LEDGER_DIR, sb.root / '.pf', '这一笔把本拷贝的绑定挪走了')

        # 绑定还在原处：不点名落点的那一笔照旧落在绑定指向的项目里。
        store._write_signal('cyc', NODE, 'failed', 'A', {})
        landed = json.loads((sb.root / '.pf' / 'signals' / 'cyc' / f'{NODE}.json')
                            .read_text(encoding='utf-8'))
        self.assertEqual(['A', 'failed'], [landed['summary'], landed['status']])

    def test_an_outputs_that_is_not_an_object_ends_the_write(self):
        sb = self.make()
        signal_mod = _fresh_module('conduct_signal_guard_case', SIGNAL_PY)
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                signal_mod.write_signal('cyc', NODE, 'completed', 'done', ['not', 'an', 'object'],
                                        base_dir=sb.root / '.pf' / 'signals')

        self.assertEqual(1, caught.exception.code)
        self.assertIn('outputs must be a JSON object', stderr.getvalue())
        self.assertFalse((sb.root / '.pf' / 'signals' / 'cyc' / f'{NODE}.json').exists(),
                         'a refused ticket wrote a file')

    def test_the_worker_path_still_lands_the_ticket(self):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()

        run_signal(sb.root, 'cyc', NODE, 'completed', 'done', {'deliverable': 'x'})

        sig = sb.read_signal('cyc', NODE)
        self.assertEqual('completed', sig['status'])
        self.assertEqual({'deliverable': 'x'}, sig['outputs'])