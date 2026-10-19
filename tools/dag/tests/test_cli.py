"""`dag-manage.py`'s command surface: how a prefix is decided, where the spec lands, and how it ends
when a note cannot be deleted.

The CLI cases drive a real process (one command at a time, a temp project); the in-process ones
load the module and call `cmd_*` directly, to watch the two things no output shows: importing binds
no project, and the note is best effort.

How to run (either works):

    python -m unittest discover -s dag/tests -t .      (cwd in tools/)
    python dag/tests/test_cli.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

DAG = Path(__file__).resolve().parent.parent / 'dag-manage.py'
TOOLS = DAG.parent.parent
#: UTC, microseconds, a literal `Z`. Hard-coded here, not read from `clock.ISO_FMT`: reading it
#: would let the assertion fall back with it once the source drops to seconds -- exactly the drift
#: this case exists to stop.
SHAPE = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z'


def load_clock():
    """`tools/shared/clock.py`: the one source of the recorded timestamp shape."""
    spec = importlib.util.spec_from_file_location('pf_shared_clock_case',
                                                  TOOLS / 'shared' / 'clock.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


clock = load_clock()

#: Two ids one prefix matches uniquely / fails to match, and one that collides with neither: the
#: verdict is plain from a prefix alone.
A = 'aaaaaaaa-1111-4111-8111-111111111111'
B = 'aaaaaaaa-2222-4222-8222-222222222222'
OTHER = 'bbbbbbbb-3333-4333-8333-333333333333'


def child_env() -> dict:
    """Every child process leaves as UTF-8; the host's session variable is dropped along the way, so
    whether a note exists is the case's word."""
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    env.pop('DSH_SESSION_ID', None)
    return env


def run(project: Path, *argv):
    proc = subprocess.run([sys.executable, '-X', 'utf8', str(DAG), *[str(a) for a in argv]],
                          cwd=str(project), capture_output=True, text=True,
                          encoding='utf-8', errors='replace', env=child_env())
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def load_module():
    """A fresh copy of `dag-manage.py` loaded by path -- one per case, so bindings do not leak."""
    spec = importlib.util.spec_from_file_location('dag_manage_case', DAG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ProjectCase(unittest.TestCase):
    """An init-ed empty project: the ledger is laid out, and the graph can be written directly or
    grown by real commands."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='dag-cli-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        ledger = self.root / '.pf'
        ledger.mkdir(parents=True, exist_ok=True)
        (ledger / '.gitignore').write_text('meta/\n', encoding='utf-8')
        (ledger / '.gitattributes').write_text('** -text\n', encoding='utf-8')

    def write_graph(self, nodes: dict):
        (self.root / '.pf' / 'ideas.json').write_text(
            json.dumps({'nodes': nodes}, ensure_ascii=False), encoding='utf-8')

    def graph(self) -> dict:
        return json.loads((self.root / '.pf' / 'ideas.json').read_text(encoding='utf-8'))

    def add(self, title: str) -> str:
        code, out, err = run(self.root, 'add', title)
        self.assertEqual(0, code, err or out)
        return out.split(':', 1)[1].split()[0]


class TestTheHelpVerb(ProjectCase):
    """`help` and `--help` are one body: the verb table is the one in this parser."""

    def test_help_prints_the_parser_help(self):
        verb, flag = run(self.root, 'help'), run(self.root, '--help')

        self.assertEqual(0, verb[0], verb[2])
        self.assertEqual(0, flag[0], flag[2])
        self.assertEqual(flag[1], verb[1])
        self.assertIn('validate', verb[1], 'the verb table was not printed')

    def test_the_verb_lists_itself(self):
        self.assertIn('help', run(self.root, 'help')[1])


class TestThePrefixVerdict(ProjectCase):
    """One prefix reaches one verdict on every verb: a unique match acts on that slot, a miss
    stops."""

    def setUp(self):
        super().setUp()
        self.write_graph({
            A: {'title': 'one slot', 'status': 'todo', 'dependencies': []},
            B: {'title': 'another slot', 'status': 'todo', 'dependencies': []},
            OTHER: {'title': 'third slot', 'status': 'todo', 'dependencies': []},
        })

    def test_an_ambiguous_prefix_stops_every_verb(self):
        for argv in (['rm', 'aaaaaaaa'],
                     ['undepend', 'aaaaaaaa', OTHER],
                     ['depend', 'aaaaaaaa', '--on', OTHER],
                     ['depend', OTHER, '--on', 'aaaaaaaa'],
                     ['status', 'aaaaaaaa', 'completed'],
                     ['update', 'aaaaaaaa', '--title', 'x'],
                     ['revive', 'aaaaaaaa'],
                     ['info', 'aaaaaaaa'],
                     ['context', 'aaaaaaaa'],
                     ['open', 'aaaaaaaa']):
            with self.subTest(argv=argv):
                code, out, err = run(self.root, *argv)
                self.assertEqual(1, code, out or err)
                self.assertIn('matches multiple nodes', err)

    def test_a_unique_prefix_reaches_the_node_and_moves_it(self):
        code, out, err = run(self.root, 'rm', 'bbbbbbbb')
        self.assertEqual(0, code, err or out)
        self.assertIn(f'Removed {OTHER}', out, 'it must report the id of that slot in the records')
        self.assertNotIn(OTHER, self.graph()['nodes'])

    def test_a_unique_prefix_records_the_full_id_on_the_edge(self):
        code, out, err = run(self.root, 'depend', 'bbbbbbbb', '--on', 'aaaaaaaa-1111')
        self.assertEqual(0, code, err or out)
        self.assertEqual([A], self.graph()['nodes'][OTHER]['dependencies'])

    def test_a_unique_prefix_takes_the_edge_back_out(self):
        code, out, err = run(self.root, 'depend', OTHER, '--on', A)
        self.assertEqual(0, code, err or out)

        code, out, err = run(self.root, 'undepend', 'bbbbbbbb', 'aaaaaaaa-1111')

        self.assertEqual(0, code, err or out)
        self.assertEqual([], self.graph()['nodes'][OTHER]['dependencies'])

    def test_a_dangling_edge_comes_out_by_its_stored_id_or_a_prefix_of_it(self):
        """A node the graph does not have can still be named on an edge: fixing a dangling
        reference is this command's job."""
        self.write_graph({
            OTHER: {'title': 'third slot', 'status': 'todo',
                    'dependencies': ['dddddddd-4444-4444-8444-444444444444']},
        })

        code, out, err = run(self.root, 'undepend', 'bbbbbbbb', 'dddddddd')

        self.assertEqual(0, code, err or out)
        self.assertEqual([], self.graph()['nodes'][OTHER]['dependencies'])

    def test_removing_a_node_takes_its_id_out_of_every_other_node(self):
        code, out, err = run(self.root, 'depend', OTHER, '--on', A)
        self.assertEqual(0, code, err or out)

        code, out, err = run(self.root, 'rm', 'aaaaaaaa-1111', '--force')

        self.assertEqual(0, code, err or out)
        self.assertEqual([], self.graph()['nodes'][OTHER]['dependencies'],
                         'the removed id is still hanging off another node')

    def test_a_prefix_that_matches_nobody_is_not_found(self):
        code, out, err = run(self.root, 'info', 'cccccccc')
        self.assertEqual(1, code)
        self.assertIn('not found', err)


class TestTheDependencyVerdict(ProjectCase):
    """The edge to take out goes through the same three-way verdict: a unique match comes out, an
    ambiguous one lists the candidates, and an unknown one says it does not recognize it."""

    #: Two ids written on an edge but absent from the graph, sharing one prefix -- the edge side's
    #: "ambiguous" can only come from here.
    D1 = 'dddddddd-1111-4111-8111-111111111111'
    D2 = 'dddddddd-2222-4222-8222-222222222222'

    def setUp(self):
        super().setUp()
        self.write_graph({
            A: {'title': 'one slot', 'status': 'todo', 'dependencies': []},
            OTHER: {'title': 'third slot', 'status': 'todo', 'dependencies': [self.D1, self.D2]},
        })

    def deps(self) -> list:
        return self.graph()['nodes'][OTHER]['dependencies']

    def test_an_ambiguous_dependency_prefix_is_refused_and_the_edges_stay(self):
        code, out, err = run(self.root, 'undepend', OTHER, 'dddddddd')

        self.assertEqual(1, code, out or err)
        self.assertIn('matches multiple dependencies', err)
        self.assertIn(self.D1, err, 'the candidates must be listed')
        self.assertEqual([self.D1, self.D2], self.deps(), 'an ambiguous prefix takes out no edge')

    def test_a_dependency_that_names_nobody_is_refused(self):
        code, out, err = run(self.root, 'undepend', OTHER, 'ffffffff')

        self.assertEqual(1, code, out or err)
        self.assertIn('not found', err)
        self.assertEqual([self.D1, self.D2], self.deps())

    def test_a_dependency_that_is_absent_while_the_node_is_here_is_a_no_op(self):
        """The pipeline takes out an edge that is often not there when it inserts a maintenance node
        (`maintenance.insertion` step 3), and that step wants exactly the quiet outcome of "no such
        edge"."""
        code, out, err = run(self.root, 'undepend', OTHER, A)

        self.assertEqual(0, code, err or out)
        self.assertEqual([self.D1, self.D2], self.deps())


class TestTheSpecLanding(ProjectCase):
    """`add` reports and `open` prints one landing place, and the pipeline gives it."""

    def pipeline_nodes(self, mod):
        """The pipeline module that computes the landing place -- the one the dag side asks (same
        name, loaded by path the same way)."""
        pkg = mod._loader().load_by_path('pf_dag_pipeline',
                                         mod.CONDUCT_IMPL / '__init__.py',
                                         package_dir=mod.CONDUCT_IMPL, register=True, reuse=True)
        return pkg.nodes

    def test_add_and_open_print_one_path(self):
        code, out, err = run(self.root, 'add', 'one slot')
        self.assertEqual(0, code, err or out)
        uid = out.split(':', 1)[1].split()[0]
        reported = out.splitlines()[1].split(': ', 1)[1]

        code, opened, err = run(self.root, 'open', uid)

        self.assertEqual(0, code, err or opened)
        self.assertEqual(reported, opened)
        self.assertTrue(opened.replace('\\', '/').endswith(f'.pf/nodes/{uid}/spec.md'), opened)

    def test_both_verbs_take_the_path_from_the_pipeline(self):
        """Swap the pipeline's place for a sentinel: whatever `add` and `open` print must be the
        sentinel.

        If the dag side pieced together `nodes/<uid>/spec.md` itself (wherever that lands), the
        sentinel could not be printed -- this case is the red line against that fallback: it catches
        where the landing place comes from, not what the path looks like.
        """
        nodes = self.pipeline_nodes(load_module())
        original = nodes.node_spec_path
        self.addCleanup(setattr, nodes, 'node_spec_path', original)
        sentinel = 'SENTINEL/where-the-pipeline-says/spec.md'
        nodes.node_spec_path = lambda project, uid: sentinel

        mod = load_module()
        mod.configure(self.root)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'DSH_SESSION_ID': ''}):
            with contextlib.redirect_stdout(out):
                mod.cmd_add(mod.argparse.Namespace(
                    title='one slot', description=[], desc=None, depends_on=None,
                    mode=None, weight=None, tags=None))
        printed = out.getvalue()
        self.assertIn(sentinel, printed,
                      f'`add` never asked the pipeline for the landing place: {printed!r}')
        uid = printed.split(':', 1)[1].split()[0]

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mod.cmd_open(mod.argparse.Namespace(uuid=uid))

        self.assertEqual(sentinel, out.getvalue().strip(),
                         '`open` never asked the pipeline for the landing place')


class TestTheNodeTimestamp(ProjectCase):
    """The moment a new node is written down: UTC, microseconds, a literal `Z` -- the one source is
    `tools/shared/clock.py`."""

    def test_a_new_node_carries_the_tree_timestamp_shape(self):
        uid = self.add('one slot')

        code, out, err = run(self.root, 'info', uid, '--verbose')

        self.assertEqual(0, code, err or out)
        row = json.loads(out)
        for key in ('created_at', 'updated_at'):
            with self.subTest(key=key):
                value = row[key]
                self.assertRegex(value, SHAPE, f"{key}'s timestamp shape is wrong: {value!r}")
                self.assertIsNotNone(datetime.strptime(value, clock.ISO_FMT),
                                     f'source {clock.ISO_FMT!r} does not recognize {value!r}')


class TestTheModuleBindsNothingAtImport(ProjectCase):
    """The path is the caller's word: importing this file binds no project."""

    def test_a_fresh_module_points_at_no_project(self):
        mod = load_module()

        self.assertIsNone(mod.LEDGER_DIR)
        self.assertIsNone(mod.IDEAS_JSON)

    def test_a_command_without_a_project_says_so(self):
        mod = load_module()
        err = io.StringIO()

        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            mod.cmd_list(mod.argparse.Namespace(status=None, ready=False, verbose=False,
                                                limit=0, tags=None))

        self.assertEqual(1, caught.exception.code)
        self.assertIn('configure', err.getvalue())

    def test_configure_reads_the_project_the_caller_named(self):
        """cwd is elsewhere: whoever is named is the one read -- this case's process has cwd in
        tools/, and neither temp project is there."""
        other = Path(tempfile.mkdtemp(prefix='dag-cli-other-'))
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        (other / '.pf').mkdir(parents=True, exist_ok=True)
        (other / '.pf' / 'ideas.json').write_text(
            json.dumps({'nodes': {A: {'title': 'elsewhere', 'status': 'todo',
                                      'dependencies': []}}}), encoding='utf-8')
        self.write_graph({B: {'title': 'here', 'status': 'todo', 'dependencies': []}})
        mod = load_module()

        mod.configure(other)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mod.cmd_list(mod.argparse.Namespace(status=None, ready=False, verbose=False,
                                                limit=0, tags=None))

        listed = json.loads(out.getvalue())
        self.assertEqual([A], [e['id'] for e in listed])


class TestTheSessionNoteIsBestEffort(ProjectCase):
    """The note is plumbing: whether it cannot be written or cannot be deleted, it warns one line
    and the graph edit lands anyway."""

    def boom(self):
        """A session-note that blows up on load: both actions in the module go through this."""
        class Broken:
            def drop(self, project, node_id):
                raise OSError('the note cannot be loaded')

            def append(self, project, node_id, role, session):
                raise OSError('the note cannot be loaded')

        return Broken()

    def test_a_note_that_cannot_be_removed_does_not_fail_rm(self):
        self.write_graph({A: {'title': 'one slot', 'status': 'todo', 'dependencies': []}})
        mod = load_module()
        mod.configure(self.root)
        mod._session_note = self.boom
        err = io.StringIO()

        with contextlib.redirect_stderr(err):
            mod.cmd_rm(mod.argparse.Namespace(uuid=A[:8], force=False))

        self.assertNotIn(A, self.graph()['nodes'], 'the node must be gone from the graph')
        self.assertIn('Warning', err.getvalue())

    def test_a_note_that_cannot_be_written_does_not_fail_add(self):
        mod = load_module()
        mod.configure(self.root)
        mod._session_note = self.boom
        args = mod.argparse.Namespace(title='one slot', description=[], desc=None, depends_on=None,
                                      mode=None, weight=None, tags=None)
        err = io.StringIO()

        with mock.patch.dict(os.environ, {'DSH_SESSION_ID': 'session-x'}):
            with contextlib.redirect_stderr(err):
                mod.cmd_add(args)

        self.assertEqual(1, len(self.graph()['nodes']), 'the node must have landed in the graph')
        self.assertIn('Warning', err.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)