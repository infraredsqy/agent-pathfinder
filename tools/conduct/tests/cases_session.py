"""`pf session`: which conversation produced a node, which ones ran it, and where that is written.

A session id is machine-local -- a conversation lives on the machine that opened it -- so it does not
ride on the record: `ideas.json` is shared, and an id in it is a value that is guaranteed dead on
every other machine, and one that cannot be told apart from "that session was deleted here". The ids
live in `.pf/meta/sessions/<node id>.json` (shape in `tools/shared/session-note.py`).

These cases pin the handoff end to end, on a throwaway project, with the real tools:

  * `dag-manage add` notes the session the node was born in, and writes NO session field on the node
  * `dag-manage info` reads the note back; `rm` takes the note with the node
  * a dispatch notes the session it opened -- and a dry run notes nothing
  * `pf session show|list` read the notes; `import` folds the older field in, once, and leaves the
    record byte-for-byte alone
  * the two spawner result-line shapes the note is filled from

No session is ever started here. The dispatch cases use a stub spawner that prints the contract line
and does nothing else -- the spawn path is live fire, and a test never touches it.
"""

from __future__ import annotations

import ast
import importlib.util as importlib_util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import NODE, NODE2, Base, added  # noqa: E402
from harness import (CONDUCT_DIR, TOOLS_DIR, _fresh_module, run_dag, run_state_manage,
                     shipped_sources)  # noqa: E402

__all__ = ['TestAddNotesTheBirthSession', 'TestTheRunNoteAtDispatch', 'TestTheSpawnerResultLine',
           'TestTheNoteItself', 'TestSessionReadsAndImport', 'TestTheRecordDoors',
           'TestNoLayerDefinesANameTwice']

#: The session id the parent shell reports -- what `dag-manage add` reads out of the environment.
SESSION = 'session-11111111-1111-4111-8111-111111111111'
#: A ZCode-hosted one: its ids have their own shape, and the note labels them by it.
ZCODE_SESSION = 'sess_22222222-2222-4222-8222-222222222222'


def note_of(sb, node_id=NODE) -> dict:
    """One node's note off the disk, unread -- the bytes the tools wrote, not a reader's opinion."""
    path = sb.root / '.pf' / 'meta' / 'sessions' / f'{node_id}.json'
    return json.loads(path.read_text(encoding='utf-8'))


def ids_of(note: dict, role: str) -> list:
    return [entry['session'] for entry in note[role]]


class TestAddNotesTheBirthSession(Base):
    """`dag add`: the discussion the node was routed in goes to the note, not onto the node."""

    def add_in_session(self, sb, session=SESSION, *extra):
        with mock.patch.dict(os.environ, {'DSH_SESSION_ID': session}):
            return run_dag(sb.root, 'add', 'UI design', *extra)

    def test_it_notes_the_session_and_writes_no_field_on_the_node(self):
        sb = self.make()

        uid = added(self.add_in_session(sb))

        note = note_of(sb, uid)
        self.assertEqual(1, note['v'])
        self.assertEqual([SESSION], ids_of(note, 'discussion'))
        self.assertEqual('dsh', note['discussion'][0]['host'])
        self.assertEqual([], note['run'], 'a node has not been run yet')
        self.assertNotIn('dsh_session', sb.dag()['nodes'][uid],
                         'the id is machine-local and must not ride on the record')

    def test_a_host_that_reports_nothing_still_makes_a_node(self):
        """A hand-typed host, or one that injects no DSH_SESSION_ID: the node is still born, there
        is just no note to write."""
        sb = self.make()
        with mock.patch.dict(os.environ, {}):
            os.environ.pop('DSH_SESSION_ID', None)
            uid = added(run_dag(sb.root, 'add', 'a hand-typed node'))

        self.assertIn(uid, sb.dag()['nodes'])
        self.assertFalse((sb.root / '.pf' / 'meta' / 'sessions' / f'{uid}.json').exists())

    def test_a_zcode_shaped_id_is_labelled_zcode(self):
        """`sess_…` is ZCode's id shape (`spawn/zcode-spawn.ps1` reads its own line off that
        prefix): the note labels the host by shape, and the panel learns from it that it cannot
        open that one."""
        sb = self.make()

        uid = added(self.add_in_session(sb, ZCODE_SESSION))

        self.assertEqual('zcode', note_of(sb, uid)['discussion'][0]['host'])

    def test_info_reads_the_note_back(self):
        sb = self.make()
        uid = added(self.add_in_session(sb))

        payload = json.loads(run_dag(sb.root, 'info', uid).stdout)

        self.assertEqual({'discussion': [SESSION]},
                         {role: ids_of(payload['sessions'], role)
                          for role in ('discussion', 'run') if payload['sessions'][role]})
        self.assertNotIn('dsh_session', payload, '`info` reads the sessions it knows, not a per-node spawner field')

    def test_rm_takes_the_note_with_the_node(self):
        sb = self.make()
        uid = added(self.add_in_session(sb))
        note = sb.root / '.pf' / 'meta' / 'sessions' / f'{uid}.json'
        self.assertTrue(note.is_file())

        run_dag(sb.root, 'rm', uid)

        self.assertFalse(note.exists(), 'a removed node leaves no note behind')


class TestTheRunNoteAtDispatch(Base):
    """The session a dispatch opens is noted at spawn time -- and a dry run notes nothing."""

    def pinned(self):
        """A cycle holding NODE, with its prompt already written (what `next` does)."""
        sb = self.ready_sandbox(cli=True)
        action = sb.next()
        self.assertEqual('dispatch', action['action'])
        return sb, action

    def stub(self, sb, line='spawned: session session-run-1 (node session (prompt=x))'):
        """A spawner that prints the contract line and opens nothing."""
        return sb.fake_spawner('stub-spawner.ps1', line=line)

    def test_a_real_dispatch_notes_the_session_it_opened(self):
        sb, _action = self.pinned()

        proc = sb.dispatch(spawner=self.stub(sb), dry_run=False)

        self.assertEqual(0, proc.returncode, proc.stderr)
        note = note_of(sb)
        self.assertEqual(['session-run-1'], ids_of(note, 'run'))
        self.assertEqual('dsh', note['run'][0]['host'])
        self.assertEqual([], note['discussion'], 'a run is not a discussion')
        self.assertTrue(note['run'][0]['at'], 'a run knows when it was opened')

    def test_the_discussion_and_the_runs_do_not_mix(self):
        """One node, both roles: `discussion` and `run` keep their own lists, so a reader need not
        filter first."""
        sb, _action = self.pinned()
        with mock.patch.dict(os.environ, {'DSH_SESSION_ID': SESSION}):
            run_dag(sb.root, 'add', 'another slot')     # a note for a DIFFERENT node
        self.assertTrue((sb.root / '.pf' / 'meta' / 'sessions').is_dir())

        sb.dispatch(spawner=self.stub(sb), dry_run=False)

        note = note_of(sb)
        self.assertEqual([], ids_of(note, 'discussion'))
        self.assertEqual(['session-run-1'], ids_of(note, 'run'))

    def test_a_dry_run_notes_nothing(self):
        """The drill stays a drill: the harness's switch opens nothing, so there is no id to note --
        and the launch dir stays where a human can retry it."""
        sb, _action = self.pinned()

        proc = sb.dispatch(cli=True)

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertFalse((sb.root / '.pf' / 'meta' / 'sessions').exists(),
                         'a dry run must not leave a note')


class TestTheSpawnerResultLine(unittest.TestCase):
    """The one line the note's `run` ids come from, in both hosts' shapes."""

    def setUp(self):
        self.spawn = _fresh_module('conduct_spawn_under_test',
                                   CONDUCT_DIR / 'spawn' / 'conduct_spawn.py')

    def test_the_dsh_shape(self):
        self.assertEqual('session-abc',
                         self.spawn.session_id_of('spawned: session session-abc (node session (prompt=x))'))

    def test_the_zcode_shape(self):
        self.assertEqual('sess_abc',
                         self.spawn.session_id_of('spawned: automation automation-1 '
                                                  '(session sess_abc, node session, prompt=x)'))

    def test_a_zcode_line_without_a_session_carries_none(self):
        """When ZCode cannot prove one itself it prints no session: no id means nothing to note, and
        inventing one is not allowed."""
        self.assertIsNone(self.spawn.session_id_of('spawned: automation automation-1 '
                                                   '(node session, prompt=x)'))

    def test_a_dry_run_line_carries_none(self):
        self.assertIsNone(self.spawn.session_id_of('dry-run: pwsh -File dsh-spawn.ps1 -CycleId cyc'))

    def test_the_host_is_read_off_the_scripts_own_name(self):
        self.assertEqual('dsh', self.spawn.host_of(CONDUCT_DIR / 'spawn' / 'dsh-spawn.ps1'))
        self.assertEqual('zcode', self.spawn.host_of(CONDUCT_DIR / 'spawn' / 'zcode-spawn.ps1'))
        self.assertIsNone(self.spawn.host_of(Path('someones-own-spawner.ps1')))


class TestSessionReadsAndImport(Base):
    """`pf session show|list` read the notes; `import` folds the older field in and stops there."""

    def seeded(self, cli=True):
        """A project whose record still carries the old field, plus one header-only id."""
        sb = self.make(cli=cli)
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.add_node(NODE2, 'another slot', mode='auto')
        dag = sb.dag()
        dag['nodes'][NODE]['dsh_session'] = SESSION
        (sb.root / '.pf' / 'ideas.json').write_text(
            json.dumps(dag, ensure_ascii=False, indent=2), encoding='utf-8')
        spec = sb.root / '.pf' / 'nodes' / NODE2 / 'spec.md'
        spec.write_text(f'# another slot\n\n> discussion: dsh-session:{ZCODE_SESSION}\n\nbody\n',
                        encoding='utf-8')
        return sb

    def test_show_names_the_node_and_both_roles(self):
        sb = self.seeded(cli=False)
        run_state_manage(sb.root, 'session', 'import')

        payload = json.loads(run_state_manage(sb.root, 'session', 'show', NODE, '--json').stdout)

        self.assertEqual(NODE, payload['node'])
        self.assertEqual([SESSION], ids_of(payload, 'discussion'))
        self.assertEqual([], payload['run'])

    def test_show_answers_empty_rather_than_failing(self):
        """Nothing noted is answered as nothing noted: an empty reading is an answer too, and it is
        exactly what the panel asks."""
        sb = self.make(cli=False)
        sb.add_node(NODE, 'UI design', mode='auto')

        proc = run_state_manage(sb.root, 'session', 'show', NODE, '--json')

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual({'node': NODE, 'v': 1, 'discussion': [], 'run': []}, json.loads(proc.stdout))

    def test_show_takes_a_prefix_and_refuses_an_ambiguous_one(self):
        sb = self.make(cli=False)
        sb.add_node(NODE, 'UI design', mode='auto')

        self.assertEqual(NODE, json.loads(
            run_state_manage(sb.root, 'session', 'show', NODE[:8], '--json').stdout)['node'])
        proc = run_state_manage(sb.root, 'session', 'show', 'zzzz', '--json', check=False)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn('no node starts with', proc.stderr)

    def test_list_is_keyed_by_node_id(self):
        sb = self.seeded(cli=False)
        run_state_manage(sb.root, 'session', 'import')

        listed = json.loads(run_state_manage(sb.root, 'session', 'list', '--json').stdout)

        self.assertEqual([SESSION, ZCODE_SESSION],
                         [ids_of(listed[NODE], 'discussion')[0], ids_of(listed[NODE2], 'discussion')[0]])
        self.assertEqual('zcode', listed[NODE2]['discussion'][0]['host'],
                         'a `sess_…` id came from the other host, and the note says so')

    def test_import_reads_both_old_sources_and_marks_them_ledger(self):
        sb = self.seeded(cli=False)

        proc = run_state_manage(sb.root, 'session', 'import')

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual([SESSION], ids_of(note_of(sb, NODE), 'discussion'))
        self.assertEqual([ZCODE_SESSION], ids_of(note_of(sb, NODE2), 'discussion'),
                         'the id a spec header carried is imported too')
        entry = note_of(sb, NODE)['discussion'][0]
        self.assertEqual('ledger', entry['source'],
                         'an imported id may name a session on another machine, and says so')
        self.assertIsNone(entry['at'], 'the old record does not say when; the note does not invent it')

    def test_import_leaves_the_record_byte_for_byte_alone(self):
        sb = self.seeded(cli=False)
        before = (sb.root / '.pf' / 'ideas.json').read_bytes()

        run_state_manage(sb.root, 'session', 'import')

        self.assertEqual(before, (sb.root / '.pf' / 'ideas.json').read_bytes(),
                         'the field stays where it is, dead; nothing rewrites the record')

    def test_import_twice_is_a_no_op(self):
        sb = self.seeded(cli=False)
        run_state_manage(sb.root, 'session', 'import')
        once = note_of(sb, NODE)

        proc = run_state_manage(sb.root, 'session', 'import')

        self.assertEqual(once, note_of(sb, NODE))
        self.assertIn('0 session id(s) as discussion', proc.stdout)

    def test_import_dry_run_writes_nothing(self):
        sb = self.seeded(cli=False)
        before = sb.tree_hashes()

        proc = run_state_manage(sb.root, 'session', 'import', '--dry-run')

        self.assertIn('would note', proc.stdout)
        self.assertEqual(before, sb.tree_hashes(), 'a dry run writes nothing')


class TestTheRecordDoors(Base):
    """The four things under `.pf/` have one door each: every door takes `project`, and the
    caller says whose project to read.

    `session` (the graph, `nodes/<id>/spec.md`, `meta/sessions/`), `instruction` and `maintenance`
    all take their record from here -- assembling the path locally would give one record a second
    failure mode (one place shouts when it reads badly, the other silently reads an empty set).
    """

    def state_package(self):
        """`state` is a package with relative imports: load the whole package by path (following
        state-manage.py's bootstrap)."""
        name = 'pf_state_for_doors'
        for stale in [n for n in sys.modules if n == name or n.startswith(name + '.')]:
            del sys.modules[stale]
        pkg_dir = CONDUCT_DIR / 'state'
        spec = importlib_util.spec_from_file_location(
            name, pkg_dir / '__init__.py', submodule_search_locations=[str(pkg_dir)])
        mod = importlib_util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_every_door_answers_for_the_project_it_is_told(self):
        mine, other = self.make(), self.make()
        mine.add_node(NODE, 'my node', mode='auto')
        mine.create_cycle('cyc')
        mine.instruct('a constraint on this side')
        other.add_node(NODE2, 'the other node', mode='auto')
        other.start_cycle('other')
        other.write_signal('other', NODE2, 'completed', 'done')
        other.instruct('a constraint on the other side')

        store = self.state_package().store
        store.bind_paths(str(mine.root / '.pf'))        # the in-process binding stays where it is

        self.assertEqual({'cyc'}, {c['id'] for c in store._read_state()['cycles']})
        self.assertEqual({'other'}, {c['id'] for c in store._read_state(str(other.root))['cycles']})
        self.assertEqual({NODE2}, set(store._read_dag(str(other.root))))
        self.assertEqual(['a constraint on the other side'],
                         [i['text'] for i in store._read_instructions(str(other.root))['instructions']])
        self.assertEqual('completed', store._read_signal('other', NODE2, str(other.root))['status'])
        self.assertEqual(other.signal_path('other', NODE2),
                         store._signal_path('other', NODE2, str(other.root)))


class TestTheNoteItself(unittest.TestCase):
    """The note file's own contract: one file per node, two named lists, an id noted once."""

    def setUp(self):
        import tempfile
        self.note = _fresh_module('session_note_under_test', TOOLS_DIR / 'shared' / 'session-note.py')
        self.tmp = tempfile.TemporaryDirectory(prefix='session-note-case-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_the_file_holds_one_node_two_roles(self):
        self.note.append(self.root, NODE, 'discussion', SESSION, at='2026-10-01T00:00:00Z')
        self.note.append(self.root, NODE, 'run', 'session-run-1')

        raw = json.loads((self.root / '.pf' / 'meta' / 'sessions' / f'{NODE}.json')
                         .read_text(encoding='utf-8'))

        self.assertEqual({'v', 'discussion', 'run'}, set(raw), 'the shape is exactly this much')
        self.assertEqual([SESSION], ids_of(raw, 'discussion'))
        self.assertEqual(['session-run-1'], ids_of(raw, 'run'))

    def test_the_same_id_is_not_noted_twice(self):
        self.assertTrue(self.note.append(self.root, NODE, 'run', 'session-run-1'))

        self.assertFalse(self.note.append(self.root, NODE, 'run', 'session-run-1'),
                         'a retried spawn must not leave the same run twice')

        self.assertEqual(['session-run-1'], ids_of(self.note.read(self.root, NODE), 'run'))

    def test_the_role_is_checked_loudly(self):
        """A third role is the caller's bug; taking it silently would write a note nobody reads."""
        with self.assertRaises(ValueError):
            self.note.append(self.root, NODE, 'review', 'session-x')

    def test_an_unreadable_file_reads_as_empty_and_never_raises(self):
        """A note is a machine-local, disposable pipeline artifact: unreadable reads as absent, and
        a tool must never die on it."""
        path = self.root / '.pf' / 'meta' / 'sessions' / f'{NODE}.json'
        path.parent.mkdir(parents=True)
        for body in ('{not json', json.dumps({'v': 99, 'discussion': []}),
                     json.dumps({'v': 1, 'discussion': [{'host': 'dsh'}]})):
            path.write_text(body, encoding='utf-8')
            with self.subTest(body=body):
                self.assertEqual([], ids_of(self.note.read(self.root, NODE), 'discussion'))

    def test_drop_forgets_one_note(self):
        self.note.append(self.root, NODE, 'run', 'session-run-1')

        self.assertTrue(self.note.drop(self.root, NODE))

        self.assertFalse(self.note.drop(self.root, NODE), 'dropping twice is a no-op')
        self.assertEqual([], self.note.sessions_of(self.note.read(self.root, NODE), 'run'))


class TestNoLayerDefinesANameTwice(unittest.TestCase):
    """A top-level name defined twice in one module is a silent loser: the second wins, and a reader
    opening the file reads the first. This keeps them from coming back.
    """

    def test_the_packages_have_no_duplicate_top_level_definitions(self):
        duplicates = {}
        for module in shipped_sources():
            names = [node.name for node in ast.parse(module.read_text(encoding='utf-8')).body
                     if isinstance(node, (ast.FunctionDef, ast.ClassDef))]
            twice = sorted({name for name in names if names.count(name) > 1})
            if twice:
                duplicates[str(module)] = twice

        self.assertEqual({}, duplicates, 'a name defined twice at module scope')
