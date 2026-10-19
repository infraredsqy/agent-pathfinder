"""What the shared layer owes the rest of the tree once more than one tool leans on it.

`tools/shared/` holds what more than one tool needs, and every file in it is loaded by path. The
cases here are the ones that need the shipped tree itself or a second tool: one door for loading by
path, one reader/writer for a record, one wording for a record that cannot be read, one marker
contract. What `shared/` proves about itself -- the loader's three knobs, the bytes a record lands,
the lock, the ledger contract -- lives in `tools/shared/tests/` and is not asked of this suite.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE  # noqa: E402
from harness import (HANDOFF_PY, SIGNAL_PY, TOOLS_DIR, _child_env, _fresh_module, _run_proc,
                     init_markers, load_pipeline, run_dag, run_signal, shared_loader,
                     shipped_sources)  # noqa: E402

__all__ = ['TestOneSharedDoor', 'TestOneReaderOneWording', 'TestTheMarkerWaitsOutAReader',
           'TestTheMarkerContract', 'TestOneWriterPerRecord']

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


def _definitions():
    """(file, name) for every top-level function and class the shipped tools define."""
    for path in shipped_sources():
        for node in ast.parse(path.read_text(encoding='utf-8')).body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                yield path, node.name


def _top_level_assignments(path: Path):
    """(name, value node) for every module-level assignment in one shipped file."""
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        else:
            continue
        for target in targets:
            yield target.id, node.value


def _literal_owners(words):
    """Which shipped file spells each of `words` as a string constant: `relative/path:word`, sorted.

    A literal is judged as a whole value, never as a substring: a help text or a docstring that
    mentions a path in prose is not a second spelling of it.
    """
    found = set()
    for path in shipped_sources():
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Constant) and node.value in words:
                found.add(f'{_relative(path)}:{node.value}')
    return sorted(found)


def _timestamp_spellings():
    """Every shipped file that states a `%Y-%m-%dT…` format, with how many times."""
    counts = {}
    for path in shipped_sources():
        hits = [node for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and '%Y-%m-%dT' in node.value]
        if hits:
            counts[_relative(path)] = len(hits)
    return counts


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


class TestOneSharedDoor(unittest.TestCase):
    """What the tree loads with, writes with and reads with: one of each, defined once."""

    def test_nothing_reaches_a_module_through_sys_path(self):
        """A neighbour is loaded by path. Putting the tool tree on `sys.path` is what hands a second
        caller the first caller's modules."""
        offenders = set()
        for path in shipped_sources():
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
        sites = {path: _path_load_calls(path) for path in shipped_sources()}
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
        """`_replace_with_retry` / `_unlink_with_retry` / `_shared` are the names the copies carried."""
        left = sorted(f'{_relative(path)}:{name}' for path, name in _definitions()
                      if name in ('_replace_with_retry', '_unlink_with_retry', '_shared'))
        self.assertEqual([], left)

    def test_the_shared_door_is_defined_once(self):
        """`load_shared`：`shared/` 里某个文件按目录取的那一道门，只许在 `shared/loader.py` 定义。

        与上面那条按名字去重的判据不同，这一条按**条数**判：一份名字 → 文件的映射会把第二个
        定义悄悄盖掉，而第二个定义正是要拦的东西。
        """
        owners = sorted(f'{_relative(path)}:{name}' for path, name in _definitions()
                        if name == 'load_shared')
        self.assertEqual(['shared/loader.py:load_shared'], owners,
                         '`load_shared` 被定义到了第二处：两道门各带一半的约定')

    def test_the_ledger_contract_has_one_home(self):
        """记录目录的契约只有一份正本：`shared/ledger.py`。

        判据落在三处：那两个名字的**赋值**、那两个**拼法**（`.pf` 与设置文件的名字）的字符串常量、
        以及 worktree 回主树那条判定的定义。别处只许取用（`from .store import LEDGER_NAME`、
        store 的 `__getattr__`），不许自带一份 —— 一旦自带，改一处就不够了，而这正是这套纪律
        要拦住的那件事：写下那第二份的人，下一次改动只会改到其中一处。
        """
        assigned = sorted(f'{_relative(path)}:{name}' for path in shipped_sources()
                          for name, _value in _top_level_assignments(path)
                          if name in ('LEDGER_NAME', 'INIT_MARKERS'))
        with self.subTest(judged='两个名字的赋值'):
            self.assertEqual(['shared/ledger.py:INIT_MARKERS', 'shared/ledger.py:LEDGER_NAME'],
                             assigned, '这两个名字被赋值到了第二处：那就是第二份要与正本对齐的拼法')

        with self.subTest(judged='两个拼法的字符串常量'):
            self.assertEqual(['shared/ledger.py:.pf', 'shared/ledger.py:config.json'],
                             _literal_owners(('.pf', 'config.json')),
                             '这两个拼法被当成路径拼到了别处（只看整个字符串常量的值，帮助文本里'
                             '提及路径的散文落在判据之外）')

        with self.subTest(judged='worktree 回主树那条判定'):
            roots = sorted(_relative(path) for path, name in _definitions()
                           if name == 'resolve_project_root')
            self.assertEqual(['shared/ledger.py'], roots,
                             '这条判定被写到了第二处：记录目录底下的路径又要多一处对齐')

    def test_the_timestamp_shapes_have_one_home(self):
        """时刻的形状只有一份正本：`shared/clock.py`（微秒与秒级各一个常量、各一个「现在」）。

        `writer.py` 那两处是既存例外，也是这里唯一允许的第二处：它们写的是 `SPAWN.claim` /
        `started` 那两枚纯文本记号里的一行日志（`<pid> started <时刻>`），不是任何一条记录的时刻 ——
        形状一样，读它的人与改它的动机却是另一件事。数量钉住，是为了让「再加一处」也必须先经过
        这条用例。
        """
        self.assertEqual({'shared/clock.py': 2, 'conduct/conduct_pipeline_impl/writer.py': 2},
                         _timestamp_spellings(),
                         '记录的格式字符串长出了第二份正本')

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
        for path in shipped_sources():
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


class TestTheMarkerWaitsOutAReader(Base):
    """The broken-chain marker goes through the shared primitive: a reader that lets go mid-retry
    costs nothing."""

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


class TestTheMarkerContract(Base):
    """断链降级标记：记录形状与落点的正本在 `spawn/marker.py`。

    标记是人工发现「链断在这里」的唯一入口，所以这一组盯三件事：文件名与记录形状只在正本里拼一次
    （两个 spawner 都不再自拼落点）、python 侧写它走的是全树那条带重试的原子写、条子那一拍与 spawner
    那一拍交出的是同一个形状。壳侧那份实现是物理上无法共享的例外（运行点可能没有 python），它的形状
    由 `cases_cli.py` 的等价性用例钉住。
    """

    def test_the_marker_is_spelled_in_exactly_one_file(self):
        owners = sorted({_relative(path)
                         for path in set(shipped_sources()) | set(_spawn_sources())
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
        """`.pf` 一改名，标记跟着记录走 —— 派发侧不再有一份自己的拼法。

        名字的正本在 `shared/ledger.py`，所以改名就改在那里：`marker.py` 算落点、handoff 那一拍
        写标记，两边都按它算，谁也没有第二份可以各自漂。
        """
        sb = self.make()
        ledger = _shared('pf_shared_ledger', 'ledger.py')
        marker = _marker_module()
        handoff = _fresh_module('handoff_marker_ledger_case', HANDOFF_PY)
        self.addCleanup(setattr, ledger, 'LEDGER_NAME', ledger.LEDGER_NAME)
        ledger.LEDGER_NAME = '.pf-moved'

        with contextlib.redirect_stderr(io.StringIO()):
            handoff._write_marker(sb.root, 'cyc', 'advance', '搬过家')

        path = marker.marker_path(sb.root, 'cyc')
        self.assertEqual('.pf-moved', path.parent.parent.name)
        self.assertTrue(path.is_file(), '标记落在了工具不读的位置（自拼 `.pf` 的那条路）')

    def test_the_two_writers_leave_one_shape(self):
        """`handoff` 那一拍写下的标记，与正本的形状、那句人话一字不差。"""
        sb = self.make()
        marker = _marker_module()
        handoff = _fresh_module('handoff_marker_shape_case', HANDOFF_PY)

        with contextlib.redirect_stderr(io.StringIO()):
            handoff._write_marker(sb.root, 'cyc', 'plan', '条子那一拍断了')

        written = json.loads(marker.marker_path(sb.root, 'cyc').read_text(encoding='utf-8'))
        self.assertEqual(sorted(marker.record('cyc', 'plan', 'x')), sorted(written))
        self.assertEqual(marker.DEFAULT_NOTE, written['note'])
        self.assertEqual({}, written['extra'], '没有宿主事实时也给一个空槽，形状不随调用方变')
        self.assertEqual('cyc', written['cycle_id'])
        self.assertEqual('plan', written['phase'])

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

        # 绑定还在原处：不点名落点的那一笔落在绑定指向的项目里。
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