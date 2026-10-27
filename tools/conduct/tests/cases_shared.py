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
from harness import (HANDOFF_PY, PLUGINS_DIR, SIGNAL_PY, TOOLS_DIR, _child_env, _fresh_module,  # noqa: E402
                     _run_proc, init_markers, load_pipeline, requires_plugin, run_dag, run_signal,
                     shared_loader, shipped_sources)  # noqa: E402

__all__ = ['TestOneSharedDoor', 'TestOneReaderOneWording', 'TestTheMarkerWaitsOutAReader',
           'TestTheMarkerContract', 'TestOneWriterPerRecord']

CONDUCT_DIR = TOOLS_DIR / 'conduct'
SHARED_DIR = TOOLS_DIR / 'shared'
#: The pool tool, which moved into its plugin's own folder: a plugin is a folder with the tool it
#: needs inside it, so the only thing that points at it from outside is a path into `plugins/`.
ISSUE_MANAGE = PLUGINS_DIR / 'issue' / 'tools' / 'issue_manager.py'
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
        """`load_shared`: the door that takes a file in `shared/` by directory, defined only in
        `shared/loader.py`.

        Unlike the by-name deduplication above, this one judges by **count**: a name -> file mapping
        quietly lets the second definition shadow the first, and that second definition is exactly
        what is being blocked.
        """
        owners = sorted(f'{_relative(path)}:{name}' for path, name in _definitions()
                        if name == 'load_shared')
        self.assertEqual(['shared/loader.py:load_shared'], owners,
                         '`load_shared` is defined in a second place: two doors each holding half '
                         'the contract')

    def test_the_ledger_contract_has_one_home(self):
        """The ledger's contract has one source: `shared/ledger.py`.

        The judgement lands on three things: the **assignment** of those two names, the string
        constants of those two **spellings** (`.pf` and the settings file's name), and the
        definition of the worktree-to-main-tree rule. Elsewhere they may only be taken by reference
        (`from .store import LEDGER_NAME`, store's `__getattr__`), never carried along -- once
        carried, changing one place is not enough, and that is what this discipline is here to
        block: whoever writes that second copy will only touch one of them next time.
        """
        assigned = sorted(f'{_relative(path)}:{name}' for path in shipped_sources()
                          for name, _value in _top_level_assignments(path)
                          if name in ('LEDGER_NAME', 'INIT_MARKERS'))
        with self.subTest(judged='the two names assigned'):
            self.assertEqual(['shared/ledger.py:INIT_MARKERS', 'shared/ledger.py:LEDGER_NAME'],
                             assigned, 'these two names are assigned in a second place: that is a '
                             'second spelling to keep aligned with the source')

        with self.subTest(judged='the two spellings as string constants'):
            self.assertEqual(['shared/ledger.py:.pf', 'shared/ledger.py:config.json'],
                             _literal_owners(('.pf', 'config.json')),
                             'these two spellings are assembled as paths somewhere else (only the '
                             'whole value of a string constant counts -- prose in a help text that '
                             'mentions a path falls outside the judgement)')

        with self.subTest(judged='the worktree-to-main-tree rule'):
            roots = sorted(_relative(path) for path, name in _definitions()
                           if name == 'resolve_project_root')
            self.assertEqual(['shared/ledger.py'], roots,
                             'this rule is written in a second place: paths under the ledger gain '
                             'one more place to align')

    def test_the_timestamp_shapes_have_one_home(self):
        """The moment's shape has one source: `shared/clock.py` (one constant and one "now" each
        for microseconds and seconds).

        The two in `writer.py` are a pre-existing exception, and the only second place allowed here:
        they write one log line inside the `SPAWN.claim` / `started` plain-text markers
        (`<pid> started <moment>`), not the moment of any record -- the shape is the same, but who
        reads it and why it would change are another matter. The count is pinned so that "one more
        place" has to come through this case first.
        """
        self.assertEqual({'shared/clock.py': 2, 'conduct/pipeline/writer.py': 2},
                         _timestamp_spellings(),
                         'the record format string has grown a second source')

    def test_one_reader_named_read_json_is_left(self):
        """Two `_read_json` with two failure conventions was the drift; the tree keeps the CLI
        layer's door and every other reader asks the shared one."""
        owners = sorted(_relative(path) for path, name in _definitions() if name == '_read_json')
        self.assertEqual(['conduct/state/store.py'], owners)

    def test_the_replace_reaches_the_filesystem_from_one_place(self):
        """`os.replace` has one call site: `shared/jsonio.py`.

        The rename into place is one primitive: whoever writes, and whether it is JSON or text,
        changes that one place (`replace_with_retry`). With a copy per file, every later change to
        `replace_with_retry` misses the others.
        """
        callers = []
        for path in shipped_sources():
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'replace'
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == 'os'):
                    callers.append(f'{_relative(path)}:{node.lineno}')

        self.assertEqual(1, len(callers), f'more than one call site: {callers}')
        self.assertTrue(callers[0].startswith('shared/jsonio.py:'), callers)

    def test_git_is_run_from_one_place(self):
        """`subprocess.run(['git', ...])` has one call site: `shared/gitcmd.py`.

        The three places that ask git (`pf doctor` asks about the merge driver, `init` asks whether
        this is a repo, a parallel fork asks whether a branch is taken) ask the same question in the
        same spelling: cwd, decoding, whether to raise. Written out three times, one change to the
        decoding or the timeout lands in only one of them; gathered into one, asking git has a
        single answer.
        """
        callers = []
        for path in shipped_sources():
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'run'
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == 'subprocess'):
                    continue
                if not (node.args and isinstance(node.args[0], ast.List) and node.args[0].elts):
                    continue
                first = node.args[0].elts[0]
                if isinstance(first, ast.Constant) and first.value == 'git':
                    callers.append(f'{_relative(path)}:{node.lineno}')

        self.assertEqual(['shared/gitcmd.py'],
                         sorted({caller.split(':')[0] for caller in callers}),
                         f'the way git is asked is spread over more than one place: {callers}')

    def test_the_spawner_module_is_one_copy_per_process(self):
        """`writer._spawner()`: one dispatch asks it three times (start that session, read the
        session id, read the host), and all three get the same module object -- the loader's
        `register` + `reuse` pair, the same road `spawn/host.py` takes.
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

    @requires_plugin('issue')
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
    """The broken-chain degradation marker: the record's shape and its landing spot have one source
    in `spawn/marker.py`.

    The marker is the only door through which a human finds out that "the chain broke here", so this
    group watches three things: the file name and the record shape are assembled once, in the source
    (neither spawner assembles the landing spot any more); the python side writes it through the
    tree's retrying atomic write; and the note's beat and the spawner's beat hand over the same
    shape. The shell-side implementation is the exception that physically cannot be shared (the
    runtime may have no python), and its shape is pinned by the equivalence case in
    `cases_spawn_shell.py`.
    """

    def test_the_marker_is_spelled_in_exactly_one_file(self):
        owners = sorted({_relative(path)
                         for path in set(shipped_sources()) | set(_spawn_sources())
                         if 'CONDUCT-CALLBACK-FAILED' in path.read_text(encoding='utf-8')})
        self.assertEqual(['conduct/spawn/marker.py'], owners,
                         'the marker file name is copied into a second place: that is how changing '
                         'one place misses the other')

    def test_the_spawners_never_assemble_the_marker_path(self):
        """None of `.pf` / `meta` / the file-name template belongs on the spawner side: argv hands
        the landing spot in."""
        for path in sorted(SPAWN_DIR.glob('*.ps1')):
            source = path.read_text(encoding='utf-8')
            for gone in ('.pf', 'meta', 'CONDUCT-CALLBACK-FAILED'):
                self.assertNotIn(gone, source, f'{path.name} assembles the marker landing spot again')

    def test_one_shell_serves_both_spawners_and_the_hosts_stay_explicit(self):
        """One shell (`spawn-common.ps1`); the host differences stay in their own thin shells, whose
        argument surface is still their own."""
        shell = SPAWN_DIR / 'spawn-common.ps1'
        self.assertTrue(shell.is_file(), 'the shared shell is not there')
        sources = {path.name: path.read_text(encoding='utf-8')
                   for path in sorted(SPAWN_DIR.glob('*.ps1'))}
        owners = sorted(name for name, source in sources.items()
                        if 'function Write-CallbackFailureMarker' in source)
        self.assertEqual(['spawn-common.ps1'], owners, 'more than one implementation writes the marker')

        dsh, zcode = sources['dsh-spawn.ps1'], sources['zcode-spawn.ps1']
        for name, source in (('dsh-spawn.ps1', dsh), ('zcode-spawn.ps1', zcode)):
            self.assertIn("'spawn-common.ps1'", source, f'{name} does not dot-source the shared shell')
            for shared in ('-CycleId', '-PromptFile', '-WorkspacePath', '-MarkerPath'):
                self.assertIn(shared, source, f'{name} is missing the shared argument {shared}')
        self.assertIn('-WebUrl', dsh)
        self.assertNotIn('-WebUrl', zcode)
        self.assertIn('-NoVerify', zcode)
        self.assertNotIn('-NoVerify', dsh)

    def test_the_marker_path_follows_the_ledger_name(self):
        """Rename `.pf` and the marker follows the ledger -- the dispatch side has no spelling of
        its own any more.

        The name's source is `shared/ledger.py`, so a rename happens there: `marker.py` computes the
        landing spot and the handoff beat writes the marker, both off that one name, and neither has
        a second copy free to drift.
        """
        sb = self.make()
        ledger = _shared('pf_shared_ledger', 'ledger.py')
        marker = _marker_module()
        handoff = _fresh_module('handoff_marker_ledger_case', HANDOFF_PY)
        self.addCleanup(setattr, ledger, 'LEDGER_NAME', ledger.LEDGER_NAME)
        ledger.LEDGER_NAME = '.pf-moved'

        with contextlib.redirect_stderr(io.StringIO()):
            handoff._write_marker(sb.root, 'cyc', 'advance', 'the ledger moved')

        path = marker.marker_path(sb.root, 'cyc')
        self.assertEqual('.pf-moved', path.parent.parent.name)
        self.assertTrue(path.is_file(),
                        'the marker landed where the tool does not read (the path that assembles '
                        '`.pf` itself)')

    def test_the_two_writers_leave_one_shape(self):
        """The marker the `handoff` beat writes: the same shape and the same sentence as the source,
        word for word."""
        sb = self.make()
        marker = _marker_module()
        handoff = _fresh_module('handoff_marker_shape_case', HANDOFF_PY)

        with contextlib.redirect_stderr(io.StringIO()):
            handoff._write_marker(sb.root, 'cyc', 'plan', 'the plan beat broke')

        written = json.loads(marker.marker_path(sb.root, 'cyc').read_text(encoding='utf-8'))
        self.assertEqual(sorted(marker.record('cyc', 'plan', 'x')), sorted(written))
        self.assertEqual(marker.DEFAULT_NOTE, written['note'])
        self.assertEqual({}, written['extra'],
                         'with no host facts it still gets an empty slot: the shape does not follow '
                         'the caller')
        self.assertEqual('cyc', written['cycle_id'])
        self.assertEqual('plan', written['phase'])

    def test_the_write_is_the_retrying_one(self):
        """Writing the marker does not go through a bare `os.replace`: a reader has the target file
        open right now, and the retry has to save it."""
        sb = self.make()
        marker = _marker_module()
        path = marker.marker_path(sb.root, 'cyc')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"stale": true}', encoding='utf-8')
        held = open(path, 'r', encoding='utf-8')
        self.addCleanup(held.close)
        threading.Timer(0.2, held.close).start()

        marker.write(path, marker.record('cyc', 'advance', 'the reader let go', workspace=sb.root))

        self.assertEqual('the reader let go', json.loads(path.read_text(encoding='utf-8'))['reason'])


class TestOneWriterPerRecord(Base):
    """One signal writer, and the record it lands is the store's own."""

    def test_signal_py_carries_no_writer_of_its_own(self):
        source = (CONDUCT_DIR / 'signal.py').read_text(encoding='utf-8')
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
        """An explicit landing spot is this write's own business: the directory the caller names
        takes this write, and this copy's binding does not move a word.

        This store is shared within a process: move the binding to your own project on the way past
        while writing a note, and the next caller that forgets `base_dir` silently writes into the
        previous project.
        """
        sb = self.make()
        other = self.make()
        signal_mod = _fresh_module('conduct_signal_base_dir_case', SIGNAL_PY)
        store = signal_mod._store()
        store.bind_paths(sb.root / '.pf')

        signal_mod.write_signal('cyc', NODE, 'completed', 'B', {},
                                base_dir=other.root / '.pf' / 'signals')

        self.assertTrue((other.root / '.pf' / 'signals' / 'cyc' / f'{NODE}.json').is_file(),
                        'the explicit-landing-spot write did not land in the directory it named')
        self.assertFalse((sb.root / '.pf' / 'signals' / 'cyc' / f'{NODE}.json').exists(),
                         'the explicit-landing-spot write went into the project the binding points at')
        self.assertEqual(store.LEDGER_DIR, sb.root / '.pf', 'this write moved this copy\'s binding')

        # The binding is still where it was: the write that names no landing spot lands in the
        # project the binding points at.
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
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()

        run_signal(sb.root, 'cyc', NODE, 'completed', 'done', {'deliverable': 'x'})

        sig = sb.read_signal('cyc', NODE)
        self.assertEqual('completed', sig['status'])
        self.assertEqual({'deliverable': 'x'}, sig['outputs'])