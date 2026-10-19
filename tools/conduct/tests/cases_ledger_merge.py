"""`ledger-merge.py`: the git merge driver for the ledger under `.pf/`.

Run it either way:

    python conduct/tests/cases_ledger_merge.py      # this file alone
    python conduct/tests/test_conduct_pipeline.py       # with the rest of the suite (it star-imports this)

Cases here build their own throwaway directories and never use the `Sandbox`: the driver is a
standalone program with two inputs -- three files, and the tools it borrows its reader and writers
from, so one fixture of its own is the whole of what it needs. What they do share is the leaf level
the whole suite stands on (`harness_env`): the paths, the child environment, the one loader, the
decoder, the process runner, and the two files `init` writes. What is pinned:

  * the three-way rules, one case per rule in the driver's AUTO-MERGE / FLAG table -- decided by
    the driver's own `main()` in this process, because the rules track the ledger's shape and
    change often, and a process start per rule case buys nothing over the same code called here
  * a conflict NEVER writes `<ours>` and exits non-zero (git then keeps ours and stops)
  * the merged file is byte-for-byte what `state.store` / `dag-manage.py` would have written
  * the program itself, through the shipped process (`TestDriverContract`, and it alone): the argv
    surface (`--dry-run`, the `%P` fourth argument), the exit codes, a file that is not a ledger
    refused -- the interface git actually invokes
  * a real `git merge`, through a real `.gitattributes` + `merge.pf.driver`
    (`cases_ledger_merge_git.py`)
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (CONDUCT_DIR, DAG_MANAGE, TOOLS_DIR, _child_env, _decode, init_markers,
                     run_argv, shared_loader)  # noqa: E402

MERGE_PY = CONDUCT_DIR / 'ledger-merge.py'
MERGE_PKG = CONDUCT_DIR / 'ledger_merge'

NODE1 = 'aaaa1111-0000-4000-8000-000000000001'
NODE2 = 'bbbb2222-0000-4000-8000-000000000002'
NODE3 = 'cccc3333-0000-4000-8000-000000000003'
EDGE_X, EDGE_Y, EDGE_Z = 'edge-x', 'edge-y', 'edge-z'


# ═══════════════════════════════════════════════════════════════════════════════
#  The driver and the tools it must agree with, loaded by path
# ═══════════════════════════════════════════════════════════════════════════════

#: The entry, and the package it forwards to. Both come through the one loader, the package under the
#: name the entry loads it by and `reuse`d: `MERGE` here is the object `DRIVER.main()` runs in, and
#: that is what makes `MERGE.store().bind_paths()` below aim THIS case's run at its own project.
LOADER = shared_loader()
DRIVER = LOADER.load_by_path('ledger_merge_under_test', MERGE_PY, register=True, reuse=True)
MERGE = LOADER.load_by_path('pf_conduct_ledger_merge', MERGE_PKG / '__init__.py',
                            package_dir=MERGE_PKG, register=True, reuse=True)
STORE = LOADER.load_by_path('pf_state_ledger_merge_test',
                            CONDUCT_DIR / 'state' / '__init__.py',
                            package_dir=CONDUCT_DIR / 'state',
                            register=True, reuse=True).store


def tool_bytes(data, sort_keys=False) -> bytes:
    """A ledger file exactly as the tools write it: CRLF, indent=2, no trailing newline."""
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=sort_keys)
    return text.replace('\n', os.linesep).encode('utf-8')


# ═══════════════════════════════════════════════════════════════════════════════
#  Builders: the records the ledger actually holds
# ═══════════════════════════════════════════════════════════════════════════════

#: The short names the cases are written with, so a test reads as `ideas(aaaa=...)` and still keys
#: the graph by the real uuid.
UUID_OF = {'aaaa': NODE1, 'bbbb': NODE2, 'cccc': NODE3}


def ideas(**nodes):
    """`ideas(aaaa=node('one'))` -- the graph, keyed by the uuid each short name stands for."""
    return {'nodes': {UUID_OF[name]: row for name, row in nodes.items()}}


def node(title='one', status='todo', deps=(), updated='2026-10-01T00:00:00.000000Z',
         created='2026-09-01T00:00:00.000000Z', **extra):
    row = {'title': title, 'description': '', 'status': status, 'mode': 'auto',
           'dependencies': list(deps), 'created_at': created, 'updated_at': updated}
    row.update(extra)
    return row


def step(node_id, started, ended, result='completed'):
    return {'node': node_id, 'started_at': started, 'ended_at': ended, 'result': result}


def cycle(path=(), status='active', updated='2026-10-01T01:00:00Z', **extra):
    entry = {'id': 'cyc', 'title': 'cyc', 'status': status, 'holding': [], 'path': list(path),
             'created_at': '2026-09-01T00:00:00Z', 'updated_at': updated}
    entry.update(extra)
    return {'cycles': [entry]}


def instruction(**changes):
    # The account's own shape, plus fields an older tool left behind: records in the wild still carry
    # the likes of `resolved_by` / `superseded_by`, so the fixture does too; nothing claims them any
    # more (see `TestInstructionsMerge`).
    entry = {'id': 'i-0001', 'text': 't', 'scope': 'global', 'target': 'stage', 'state': 'live',
             'cycle': None, 'node': None, 'said_by': 'user', 'at': '2026-09-01T00:00:00Z',
             'resolved_at': None, 'resolved_by': None, 'superseded_by': None, 'note': None}
    entry.update(changes)
    return {'instructions': [entry]}


def signal(status='pending', **extra):
    ticket = {'cycle': 'cyc', 'node': NODE1, 'status': status, 'summary': '',
              'outputs': {}, 'timestamp': '2026-10-01T00:00:00Z'}
    ticket.update(extra)
    return ticket


# ═══════════════════════════════════════════════════════════════════════════════
#  The fixture: one throwaway project, one ledger file per side
# ═══════════════════════════════════════════════════════════════════════════════

class LedgerCase(unittest.TestCase):
    """`base/`, `ours/`, `theirs/`, each holding this case's ledger file.

    A side passed as `None` gets NO file at all: that is how "one side added it" and "one side does
    not have it" are expressed, and the driver has to cope with both.
    """

    LEDGER = '.pf/ideas.json'
    SORT_KEYS = True
    #: The rule matrix decides in this process; only the program contract pins the shipped process.
    #: See the module docstring for the split's rationale.
    REAL_PROCESS = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='ledger-merge-test-')
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # init's two marker files: the write-back takes the project's lock too, and the lock only
        # lands in an init-ed `.pf/`.
        init_markers(self.root)

    # ── the fixture ──────────────────────────────────────────────────────────
    def side_path(self, side: str) -> Path:
        return self.root / side / self.LEDGER

    def write_side(self, side: str, data) -> Path:
        path = self.side_path(side)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(tool_bytes(data, sort_keys=self.SORT_KEYS))
        return path

    def write_sides(self, base, ours, theirs):
        for side, data in (('base', base), ('ours', ours), ('theirs', theirs)):
            if data is not None:
                self.write_side(side, data)

    def stamp(self, side: str, mtime: float):
        """Set one side's file mtime -- the signals rule reads it (%A/%B recency)."""
        os.utime(self.side_path(side), (mtime, mtime))

    def run_driver(self, dry=False):
        """The driver on this case's rail: the shipped process, or `main()` in this one."""
        if self.REAL_PROCESS:
            return self.run_driver_process(dry)
        return self.run_driver_in_process(dry)

    def run_driver_process(self, dry=False):
        """The driver as git runs it: a real process, cwd = the throwaway root.

        git runs the driver with the merging repo as cwd, and the driver's ledger-lock path is
        resolved from that cwd, so the lock file lands here instead of next to the test runner.
        """
        argv = [MERGE_PY]
        if dry:
            argv.append('--dry-run')
        argv += [self.side_path('base'), self.side_path('ours'), self.side_path('theirs')]
        return run_argv(argv, cwd=self.root, check=False)

    def run_driver_in_process(self, dry=False):
        """The same argv, decided in this process: `main()` itself, output captured.

        The process gets its project from cwd at store-import time; the store this module binds reads
        its bindings at call time, so the call re-points it at THIS case's throwaway project first and
        the ledger lock lands in it -- exactly where the process would have put it.
        """
        argv = [str(self.side_path('base')), str(self.side_path('ours')),
                str(self.side_path('theirs'))]
        if dry:
            argv.insert(0, '--dry-run')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            MERGE.store().bind_paths(self.root.resolve() / '.pf')
            code = DRIVER.main(argv)
        return subprocess.CompletedProcess(argv, code, out.getvalue(), err.getvalue())

    def merge(self, base, ours, theirs, dry=False):
        """Run the driver on this case's rail. Returns (CompletedProcess, the path git calls %A)."""
        self.write_sides(base, ours, theirs)
        return self.run_driver(dry=dry), self.side_path('ours')

    def merged(self, ours_path: Path):
        return _decode(ours_path)

    def assertRefused(self, proc, ours_path: Path, needle: str, ours_data=None):
        """A conflict: non-zero, a line naming the field, and `<ours>` untouched on disk."""
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn(needle, proc.stderr)
        if ours_data is not None:
            self.assertEqual(tool_bytes(ours_data, sort_keys=self.SORT_KEYS),
                             ours_path.read_bytes(), '<ours> was modified on a conflict')


# ═══════════════════════════════════════════════════════════════════════════════
#  ideas.json
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdeasMerge(LedgerCase):
    """The graph: nodes are unioned, a status goes up a rung, edges union only when additive."""

    def test_a_node_only_one_side_has_is_taken(self):
        base = ideas(aaaa=node('one'))
        mins = ideas(aaaa=node('one'), bbbb=node('two'))          # ours added a node
        theirs = ideas(aaaa=node('one'), cccc=node('three'))      # theirs added another

        proc, ours_path = self.merge(base, mins, theirs)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual({NODE1, NODE2, NODE3}, set(self.merged(ours_path)['nodes']))

    def test_a_deleted_node_stays_deleted_when_the_other_side_left_it_alone(self):
        """`dag-manage rm` on one machine must not be undone by a pull on the other."""
        base = ideas(aaaa=node('one'), bbbb=node('two'))
        for deleted_by, ours, theirs in (('ours', ideas(aaaa=node('one')), base),
                                         ('theirs', base, ideas(aaaa=node('one')))):
            with self.subTest(deleted_by=deleted_by):
                proc, ours_path = self.merge(base, ours, theirs)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual({NODE1}, set(self.merged(ours_path)['nodes']))

    def test_a_deleted_node_that_the_other_side_edited_is_a_conflict(self):
        base = ideas(aaaa=node('one'), bbbb=node('two'))
        mins = ideas(aaaa=node('one'))                       # ours ran `dag-manage rm`
        theirs = ideas(aaaa=node('one'),
                       bbbb=node(title='two, renamed', updated='2026-10-05T00:00:00.000000Z'))

        proc, ours_path = self.merge(base, mins, theirs)

        self.assertRefused(proc, ours_path, 'delete vs edit collided', ours_data=mins)

    def test_a_node_both_sides_deleted_is_gone(self):
        base = ideas(aaaa=node('one'), bbbb=node('two'))
        both = ideas(aaaa=node('one'))

        proc, ours_path = self.merge(base, both, both)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual({NODE1}, set(self.merged(ours_path)['nodes']))

    def test_a_node_the_base_never_had_is_still_added_when_both_sides_have_it(self):
        """An add/add on the same uuid: an addition, not a deletion, so the fields merge.

        (`dependencies` is left equal on both sides on purpose: an add/add with different edges has
        no ancestor to read an added edge against, and that is a conflict -- see the case above.)
        """
        base = {'nodes': {}}
        mins = ideas(aaaa=node('one'))
        theirs = ideas(aaaa=node(title='theirs title', mode='manual',
                                 updated='2026-10-05T00:00:00.000000Z'))

        proc, ours_path = self.merge(base, mins, theirs)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        merged = self.merged(ours_path)['nodes'][NODE1]
        self.assertEqual('theirs title', merged['title'])
        self.assertEqual('manual', merged['mode'])
        self.assertEqual([], merged['dependencies'])

    def test_an_edge_the_merge_leaves_dangling_is_a_conflict(self):
        """One side deleted a node, the other added an edge to it: `validate` would say no_dangling,
        and the pipeline reads a dangling edge as SATISFIED -- so it must not merge silently."""
        base = ideas(aaaa=node('one'), cccc=node('three'))
        mins = ideas(cccc=node('three'))                    # ours ran `dag-manage rm` on NODE1
        theirs = ideas(aaaa=node('one'), cccc=node('three', deps=[NODE1]))

        proc, ours_path = self.merge(base, mins, theirs)

        self.assertRefused(proc, ours_path, 'a dependency was deleted by the merge', ours_data=mins)
        self.assertIn('undepend', proc.stderr)              # the line says how to get out

    def test_an_edge_that_was_already_dangling_is_not_the_merges_business(self):
        """Both sides already carry the broken edge: that graph was broken before the merge."""
        base = ideas(aaaa=node('one'), cccc=node('three'))
        both = ideas(cccc=node('three', deps=[NODE1]))      # NODE1 gone, the edge is not

        proc, ours_path = self.merge(base, both, both)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([NODE1], self.merged(ours_path)['nodes'][NODE3]['dependencies'])

    def test_status_goes_up_the_rung(self):
        base = ideas(aaaa=node('one', status='todo'))
        for ours_status, theirs_status, expected in (('todo', 'completed', 'completed'),
                                                     ('completed', 'todo', 'completed'),
                                                     ('shelved', 'todo', 'shelved')):
            with self.subTest(ours=ours_status, theirs=theirs_status):
                proc, ours_path = self.merge(
                    base, ideas(aaaa=node('one', status=ours_status)),
                    ideas(aaaa=node('one', status=theirs_status)))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(expected, self.merged(ours_path)['nodes'][NODE1]['status'])

    def test_edges_added_on_both_sides_are_unioned(self):
        base = ideas(aaaa=node('one', deps=[EDGE_X]))
        proc, ours_path = self.merge(base, ideas(aaaa=node('one', deps=[EDGE_X, EDGE_Y])),
                                     ideas(aaaa=node('one', deps=[EDGE_X, EDGE_Z])))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([EDGE_X, EDGE_Y, EDGE_Z],
                         self.merged(ours_path)['nodes'][NODE1]['dependencies'])

    def test_an_edge_removed_on_one_side_is_a_conflict(self):
        base = ideas(aaaa=node('one', deps=[EDGE_X]))
        mins = ideas(aaaa=node('one', deps=[]))

        proc, ours_path = self.merge(base, mins, ideas(aaaa=node('one', deps=[EDGE_X, EDGE_Y])))

        self.assertRefused(proc, ours_path, 'field dependencies', ours_data=mins)
        self.assertIn('removed', proc.stderr)              # the line names what each side did

    def test_edges_without_a_base_are_a_conflict(self):
        """An add/add gives no ancestor: an added edge cannot be told from a removed one."""
        mins = ideas(aaaa=node('one', deps=[EDGE_Y]))
        proc, ours_path = self.merge(None, mins, ideas(aaaa=node('one', deps=[EDGE_Z])))
        self.assertRefused(proc, ours_path, 'dependencies', ours_data=mins)

    def test_a_title_both_sides_edited_follows_updated_at(self):
        base = ideas(aaaa=node('one'))
        mins = ideas(aaaa=node(title='ours title', updated='2026-10-02T00:00:00.000000Z'))
        theirs = ideas(aaaa=node(title='theirs title', updated='2026-10-05T00:00:00.000000Z'))

        proc, ours_path = self.merge(base, mins, theirs)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual('theirs title', self.merged(ours_path)['nodes'][NODE1]['title'])

    def test_updated_at_takes_the_later_and_created_at_the_earlier(self):
        base = ideas(aaaa=node('one'))
        ours = ideas(aaaa=node('one', updated='2026-10-09T00:00:00.000000Z',
                               created='2026-09-20T00:00:00.000000Z'))
        theirs = ideas(aaaa=node('one', updated='2026-10-03T00:00:00.000000Z',
                                 created='2026-08-01T00:00:00.000000Z'))

        proc, ours_path = self.merge(base, ours, theirs)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        merged = self.merged(ours_path)['nodes'][NODE1]
        self.assertEqual('2026-10-09T00:00:00.000000Z', merged['updated_at'])
        self.assertEqual('2026-08-01T00:00:00.000000Z', merged['created_at'])

    def test_a_field_with_no_rule_of_its_own_is_a_conflict(self):
        base = ideas(aaaa=node('one'))
        mins = ideas(aaaa=node('one', note='ours'))
        proc, ours_path = self.merge(base, mins, ideas(aaaa=node('one', note='theirs')))
        self.assertRefused(proc, ours_path, 'field note', ours_data=mins)


# ═══════════════════════════════════════════════════════════════════════════════
#  state.json
# ═══════════════════════════════════════════════════════════════════════════════

class TestStateMerge(LedgerCase):
    """The cycles: `path` is append-only."""

    LEDGER = '.pf/state.json'
    SORT_KEYS = False

    def test_a_path_that_is_a_prefix_of_the_other_is_extended(self):
        base = cycle([step(NODE1, '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z')])
        theirs = cycle([step(NODE1, '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z'),
                        step(NODE2, '2026-10-01T01:00:00Z', '2026-10-01T02:00:00Z')])

        proc, ours_path = self.merge(base, base, theirs)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([NODE1, NODE2],
                         [s['node'] for s in self.merged(ours_path)['cycles'][0]['path']])

    def test_two_tails_after_a_shared_prefix_are_unioned_in_walk_order(self):
        shared = step(NODE1, '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z')
        base = cycle([shared])
        ours = cycle([shared, step(NODE2, '2026-10-01T01:00:00Z', '2026-10-01T05:00:00Z')])
        theirs = cycle([shared, step(NODE3, '2026-10-01T03:00:00Z', '2026-10-01T04:00:00Z')])

        proc, ours_path = self.merge(base, ours, theirs)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        # ordered by `ended_at`, so theirs' step lands in the middle
        self.assertEqual([NODE1, NODE3, NODE2],
                         [s['node'] for s in self.merged(ours_path)['cycles'][0]['path']])

    def test_one_node_with_two_results_is_a_conflict(self):
        base = cycle([step(NODE1, '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z')])
        ours = cycle([step(NODE1, '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z'),
                      step(NODE2, '2026-10-01T01:00:00Z', '2026-10-01T02:00:00Z', 'failed')])
        theirs = cycle([step(NODE1, '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z'),
                        step(NODE2, '2026-10-01T01:00:00Z', '2026-10-01T02:00:00Z', 'completed')])

        proc, ours_path = self.merge(base, ours, theirs)

        self.assertRefused(proc, ours_path, 'path', ours_data=ours)

    def test_one_node_with_the_same_result_twice_is_no_quarrel(self):
        """Both sides recorded the same result for one node, a few seconds apart at the closing
        moment: keeping the earlier entry in the union is enough.

        The rule judges the `result` of the same node on the two sides; judging the whole entry
        would turn two machines recording one node as completed, each with its own closing moment,
        into a pull that needs a human to adjudicate -- the rule finding work for people itself.
        """
        shared = step(NODE1, '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z')
        ours = cycle([shared, step(NODE2, '2026-10-01T01:00:00Z', '2026-10-01T02:00:00Z')])
        theirs = cycle([shared, step(NODE2, '2026-10-01T01:00:00Z', '2026-10-01T09:00:00Z')])

        proc, ours_path = self.merge(cycle([shared]), ours, theirs)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = self.merged(ours_path)['cycles'][0]['path']
        self.assertEqual([NODE1, NODE2], [s['node'] for s in path])
        self.assertEqual('2026-10-01T02:00:00Z', path[1]['ended_at'])

    def test_a_status_only_one_side_changed_is_taken(self):
        base = cycle([], status='active')
        proc, ours_path = self.merge(base, cycle([], status='inactive'),
                                     cycle([], status='active', title='theirs'))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual('inactive', self.merged(ours_path)['cycles'][0]['status'])
        self.assertEqual('theirs', self.merged(ours_path)['cycles'][0]['title'])

    def test_a_cycle_only_one_side_has_is_taken(self):
        base = {'cycles': []}
        theirs = {'cycles': [dict(cycle([])['cycles'][0], id='other')]}

        proc, ours_path = self.merge(base, cycle([]), theirs)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(['cyc', 'other'], [c['id'] for c in self.merged(ours_path)['cycles']])

    def test_updated_at_takes_the_later_stamp(self):
        base = cycle([])
        ours = cycle([], updated='2026-10-08T00:00:00Z')
        theirs = cycle([], updated='2026-10-04T00:00:00Z')
        proc, ours_path = self.merge(base, ours, theirs)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual('2026-10-08T00:00:00Z', self.merged(ours_path)['cycles'][0]['updated_at'])


# ═══════════════════════════════════════════════════════════════════════════════
#  instructions.json
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstructionsMerge(LedgerCase):
    """The account: whole-field edits merge, a resolution moves as one act, a removal is a deletion
    of the graph's kind."""

    LEDGER = '.pf/instructions.json'
    SORT_KEYS = False

    def two_entries(self):
        """One account with two instructions: the ancestor the removal cases start from."""
        first = instruction()['instructions'][0]
        return {'instructions': [first, dict(first, id='i-0002', text='t2')]}

    def without(self, account, ins_id, at='2026-10-02T00:00:00Z'):
        """The same account after one entry was removed: the entry is gone, the trace is not."""
        return {'instructions': [e for e in account['instructions'] if e['id'] != ins_id],
                'removed': [{'id': ins_id, 'at': at}]}

    def test_each_side_edits_a_different_field(self):
        """The regression that matters: `cycle`/`node`/`resolved_at` are null on BOTH sides, and a
        pair of equal nulls must never read as "both changed it"."""
        base = instruction()
        ours = instruction(note='ours note')
        theirs = instruction(text='theirs text')

        proc, ours_path = self.merge(base, ours, theirs)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self.merged(ours_path)['instructions'][0]
        self.assertEqual('ours note', entry['note'])
        self.assertEqual('theirs text', entry['text'])
        self.assertIsNone(entry['cycle'])
        self.assertIsNone(entry['node'])
        self.assertIsNone(entry['resolved_at'])

    def test_the_same_field_both_sides_edited_is_a_conflict(self):
        base = instruction()
        ours = instruction(text='ours text')
        proc, ours_path = self.merge(base, ours, instruction(text='theirs text'))
        self.assertRefused(proc, ours_path, 'field text', ours_data=ours)

    def test_the_resolution_moves_as_one_act(self):
        base = instruction()
        ours = instruction(state='withdrawn', resolved_at='2026-10-02T00:00:00Z', note='ours')
        theirs = instruction(state='withdrawn', resolved_at='2026-10-03T00:00:00Z')

        proc, ours_path = self.merge(base, ours, theirs)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self.merged(ours_path)['instructions'][0]
        self.assertEqual('withdrawn', entry['state'])
        self.assertEqual('2026-10-03T00:00:00Z', entry['resolved_at'])  # theirs is the newer act
        self.assertEqual('ours', entry['note'])                     # a hand-written note is kept

    def test_a_legacy_resolved_by_is_ordinary_data(self):
        """The hand an archive used to record: nothing writes it any more, and a record still
        carrying it merges like any other field -- one side's edit is taken, two sides' edits are a
        human's call."""
        base = instruction(resolved_by=None)
        ours = instruction(resolved_by='boundary')
        theirs = instruction(note='theirs note')

        proc, ours_path = self.merge(base, ours, theirs)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual('boundary', self.merged(ours_path)['instructions'][0]['resolved_by'])

        proc, ours_path = self.merge(base, ours, instruction(resolved_by='owner'))

        self.assertRefused(proc, ours_path, 'field resolved_by', ours_data=ours)

    def test_the_same_id_added_by_both_sides_is_a_conflict(self):
        base = {'instructions': []}
        ours = instruction(text='ours new')
        proc, ours_path = self.merge(base, ours, instruction(text='theirs new'))
        self.assertRefused(proc, ours_path, 'both sides added', ours_data=ours)

    def test_an_entry_only_one_side_has_is_taken(self):
        base = {'instructions': []}
        theirs = {'instructions': [dict(instruction()['instructions'][0], id='i-0002')]}

        proc, ours_path = self.merge(base, instruction(), theirs)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(['i-0001', 'i-0002'],
                         [e['id'] for e in self.merged(ours_path)['instructions']])

    def test_a_note_is_never_silently_overwritten(self):
        base = instruction()
        ours = instruction(note='ours note')
        proc, ours_path = self.merge(base, ours, instruction(state='withdrawn',
                                                            resolved_at='2026-10-01T00:00:00Z'))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual('ours note', self.merged(ours_path)['instructions'][0]['note'])

    def test_the_old_bookkeeping_fields_are_ordinary_data(self):
        """`applied` / `applied_rounds` / `superseded_by`: an older tool's fields, nothing more.

        A record still carrying them merges without a word -- they travel as plain fields, and one
        side's edit is taken exactly like any other field's. Two sides that changed the same one to
        different values is an ordinary conflict: the driver keeps no rule of its own for them, so
        there is no silently unioned log to hand back.
        """
        log = [{'state': 'applied', 'at': '2026-10-01T00:00:00Z', 'note': 'old', 'cycle': 'cyc'}]
        base = instruction(applied=[], applied_rounds=[], superseded_by=None)
        ours = instruction(applied=log, applied_rounds=log, superseded_by='i-0002')
        # The other side edits an unrelated field and leaves the old ones exactly as they were.
        theirs = instruction(applied=[], applied_rounds=[], note='theirs note')

        proc, ours_path = self.merge(base, ours, theirs)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self.merged(ours_path)['instructions'][0]
        self.assertEqual(log, entry['applied'])
        self.assertEqual(log, entry['applied_rounds'])
        self.assertEqual('i-0002', entry['superseded_by'])
        self.assertEqual('theirs note', entry['note'])

        both = instruction(applied=log + [{'at': '2026-10-02T00:00:00Z'}], applied_rounds=log)
        rival = instruction(applied=log + [{'at': '2026-10-03T00:00:00Z'}], applied_rounds=log)
        proc, ours_path = self.merge(base, both, rival)

        self.assertRefused(proc, ours_path, 'field applied', ours_data=both)

    def test_a_removed_entry_stays_removed_when_the_other_side_left_it_alone(self):
        """A removal on one machine must not be undone by a pull on the other -- the same rule the
        graph has, for the same reason: a human really runs this command."""
        base = self.two_entries()
        removed = self.without(base, 'i-0001')

        for removed_by, ours, theirs in (('ours', removed, base), ('theirs', base, removed)):
            with self.subTest(removed_by=removed_by):
                proc, ours_path = self.merge(base, ours, theirs)

                self.assertEqual(proc.returncode, 0, proc.stderr)
                merged = self.merged(ours_path)
                self.assertEqual(['i-0002'], [e['id'] for e in merged['instructions']])
                self.assertEqual(['i-0001'], [t['id'] for t in merged['removed']])

    def test_a_removed_entry_that_the_other_side_edited_is_a_conflict(self):
        """One side says "take it off", the other says something about the entry itself: that is a
        human's call, exactly as it is for a node."""
        base = self.two_entries()
        ours = self.without(base, 'i-0001')
        theirs = {'instructions': [dict(base['instructions'][0], note='theirs note'),
                                   base['instructions'][1]]}

        proc, ours_path = self.merge(base, ours, theirs)

        self.assertRefused(proc, ours_path, 'delete vs edit collided', ours_data=ours)

    def test_removals_on_both_sides_are_unioned(self):
        """Each machine took a different entry off: both removals stand, and both traces survive --
        the traces are all that is left to remember those ids ever existed."""
        base = self.two_entries()
        ours = self.without(base, 'i-0001')
        theirs = self.without(base, 'i-0002', at='2026-10-03T00:00:00Z')

        proc, ours_path = self.merge(base, ours, theirs)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        merged = self.merged(ours_path)
        self.assertEqual([], merged['instructions'])
        self.assertEqual(['i-0001', 'i-0002'], sorted(t['id'] for t in merged['removed']))

    def test_the_earlier_of_two_removals_of_one_entry_is_the_moment_kept(self):
        """Both machines removed the same entry: one trace, and the moment it left the account
        first."""
        base = self.two_entries()
        ours = self.without(base, 'i-0001', at='2026-10-02T00:00:00Z')
        theirs = self.without(base, 'i-0001', at='2026-10-03T00:00:00Z')

        proc, ours_path = self.merge(base, ours, theirs)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        merged = self.merged(ours_path)
        self.assertEqual(['i-0002'], [e['id'] for e in merged['instructions']])
        self.assertEqual([{'id': 'i-0001', 'at': '2026-10-02T00:00:00Z'}], merged['removed'])

    def test_a_revive_merges_clean_when_the_other_side_left_it_alone(self):
        """Putting an entry back writes the fields one code path owns, so with the other side
        untouched the act moves whole: no half-archived record, and nothing to settle by hand. A
        field an older tool left on the record is not part of the act, and the merge leaves it."""
        base = instruction(state='withdrawn', resolved_at='2026-10-02T00:00:00Z',
                           resolved_by='boundary')
        revived = instruction(state='live', resolved_at=None, resolved_by='boundary')

        for revived_by, ours, theirs in (('ours', revived, base), ('theirs', base, revived)):
            with self.subTest(revived_by=revived_by):
                proc, ours_path = self.merge(base, ours, theirs)

                self.assertEqual(proc.returncode, 0, proc.stderr)
                entry = self.merged(ours_path)['instructions'][0]
                self.assertEqual('live', entry['state'])
                self.assertIsNone(entry['resolved_at'])
                self.assertEqual('boundary', entry['resolved_by'])

    def test_a_revive_against_a_rival_withdrawal_is_a_legible_conflict(self):
        """The one shape no rule can decide: putting an entry back CLEARS the stamp, so a revive on
        one machine and a fresh withdrawal on the other leave nothing in the record to order the two
        owner acts by. The refusal has to say that, not blame a missing tie-break."""
        base = instruction(state='withdrawn', resolved_at='2026-10-02T00:00:00Z',
                           resolved_by='boundary')
        ours = instruction(state='live', resolved_at=None, resolved_by='boundary')
        theirs = instruction(state='withdrawn', resolved_at='2026-10-03T00:00:00Z',
                             resolved_by='boundary')

        proc, ours_path = self.merge(base, ours, theirs)

        self.assertRefused(proc, ours_path, 'two owner acts collided', ours_data=ours)


# ═══════════════════════════════════════════════════════════════════════════════
#  signals
# ═══════════════════════════════════════════════════════════════════════════════

class TestSignalMerge(LedgerCase):
    """A ticket is one report: recency decides it, and a terminal status is privileged."""

    LEDGER = f'.pf/signals/cyc/{NODE1}.json'
    SORT_KEYS = False

    def test_a_terminal_status_beats_a_running_one(self):
        proc, ours_path = self.merge(signal('pending'), signal('pending', summary='still going'),
                                     signal('completed', summary='done'))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual('completed', self.merged(ours_path)['status'])

    def test_a_running_status_never_beats_a_terminal_one(self):
        """Even when the `running` side is the newer file: terminal wins on its own."""
        self.write_sides(signal('pending'), signal('failed', summary='gave up'),
                         signal('pending', summary='retrying'))
        self.stamp('ours', 1000.0)
        self.stamp('theirs', 2000.0)                        # theirs is the NEWER file

        proc = self.run_driver()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual('failed', self.merged(self.side_path('ours'))['status'])

    def test_recency_decides_between_two_equal_ranked_statuses(self):
        sides = (signal('pending'), signal('completed', summary='ours'),
                 signal('completed', summary='theirs'))
        self.write_sides(*sides)
        self.stamp('ours', 1000.0)
        self.stamp('theirs', 2000.0)
        self.assertEqual(0, self.run_driver().returncode)
        self.assertEqual('theirs', self.merged(self.side_path('ours'))['summary'])

        self.write_sides(*sides)
        self.stamp('ours', 3000.0)
        self.stamp('theirs', 2000.0)
        self.assertEqual(0, self.run_driver().returncode)
        self.assertEqual('ours', self.merged(self.side_path('ours'))['summary'])

        self.write_sides(*sides)
        self.stamp('ours', 2000.0)
        self.stamp('theirs', 2000.0)                        # a tie goes to ours
        self.assertEqual(0, self.run_driver().returncode)
        self.assertEqual('ours', self.merged(self.side_path('ours'))['summary'])

    def test_a_ticket_is_never_a_conflict(self):
        proc, ours_path = self.merge(signal('completed', summary='old'),
                                     signal('completed', summary='ours',
                                            outputs={'deliverable': 'a'}),
                                     signal('failed', summary='theirs',
                                            outputs={'deliverable': 'b'}))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_signal_only_one_side_has_is_taken(self):
        proc, ours_path = self.merge(None, None, signal('completed', summary='theirs'))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual('theirs', self.merged(ours_path)['summary'])


# ═══════════════════════════════════════════════════════════════════════════════
#  The CLI contract: dry-run, a missing side, the bytes, the file it refuses
# ═══════════════════════════════════════════════════════════════════════════════

class TestDriverContract(LedgerCase):
    """The driver as a PROGRAM: the boundary git actually invokes, so the shipped process.

    What a process start buys here and nowhere else: the argv surface (`--dry-run`, the `%P`
    fourth argument, temp-file names that carry no ledger name), the exit codes git reads, and a
    file that is neither a ledger by name nor by shape refused untouched. The two dispatch
    decisions that need no process read the loaded module directly.
    """

    REAL_PROCESS = True

    def test_dry_run_decides_and_writes_nothing(self):
        base = ideas(aaaa=node('one'))
        mins = ideas(aaaa=node('one', status='shelved'), bbbb=node('two'))
        theirs = ideas(aaaa=node('one', status='completed'), cccc=node('three'))
        before = self.write_side('ours', mins).read_bytes()

        proc, ours_path = self.merge(base, mins, theirs, dry=True)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('dry-run', proc.stdout)
        self.assertIn('only ours has it', proc.stdout)          # the union decision
        self.assertIn('only theirs has it', proc.stdout)
        self.assertIn('status takes the higher rung', proc.stdout)            # the status rung
        self.assertIn('no conflicts; would have written ', proc.stdout)
        self.assertEqual(before, ours_path.read_bytes(), '--dry-run wrote to <ours>')

    def test_dry_run_still_reports_a_conflict(self):
        base = ideas(aaaa=node('one', deps=[EDGE_X]))
        mins = ideas(aaaa=node('one', deps=[]))
        before = self.write_side('ours', mins).read_bytes()

        proc, ours_path = self.merge(base, mins, ideas(aaaa=node('one', deps=[EDGE_Y])), dry=True)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('dependencies', proc.stderr)
        self.assertEqual(before, ours_path.read_bytes())

    def test_a_missing_theirs_takes_ours(self):
        mins = ideas(aaaa=node('one'), bbbb=node('two'))
        proc, ours_path = self.merge(ideas(aaaa=node('one')), mins, None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(set(mins['nodes']), set(self.merged(ours_path)['nodes']))

    def test_a_missing_ours_takes_theirs(self):
        theirs = ideas(aaaa=node('one'), bbbb=node('two'))
        proc, ours_path = self.merge(ideas(aaaa=node('one')), None, theirs)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(set(theirs['nodes']), set(self.merged(ours_path)['nodes']))

    def test_a_file_that_is_not_a_ledger_is_refused(self):
        path = self.root / 'notes.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('hello', encoding='utf-8')
        other = self.root / 'plain.json'
        other.write_text('{"hello": "world"}', encoding='utf-8')
        for target in (path, other):
            with self.subTest(target=target.name):
                proc = run_argv([MERGE_PY, target, target, target], cwd=self.root, check=False)
                self.assertEqual(2, proc.returncode)
                self.assertEqual('hello' if target is path else '{"hello": "world"}',
                                 target.read_text(encoding='utf-8'))

    def test_a_file_that_is_not_json_is_refused_with_exit_2(self):
        """A side that cannot be read: rc=2, that file untouched, the driver and the reader each say
        a line, and both lines name which file it is."""
        self.write_side('base', ideas(aaaa=node('one')))
        broken = self.side_path('ours')
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text('{not json', encoding='utf-8')
        before = broken.read_bytes()

        proc = run_argv([MERGE_PY, self.side_path('base'), broken, self.side_path('theirs')],
                        cwd=self.root, check=False)

        self.assertEqual(2, proc.returncode, proc.stderr)
        self.assertIn('parse failure', proc.stderr)          # the shared reader's words
        self.assertIn('is not valid JSON; nothing is written', proc.stderr)  # and the driver's own line
        self.assertIn(broken.name, proc.stderr)
        self.assertEqual(before, broken.read_bytes(), 'a refused read still touched the file')

    def test_the_ledger_name_is_the_dispatch(self):
        self.assertEqual('ideas', MERGE._kind_of('.pf/ideas.json'))
        self.assertEqual('state', MERGE._kind_of(r'C:\repo\.pf\state.json'))
        self.assertEqual('instructions', MERGE._kind_of('.pf/instructions.json'))
        self.assertEqual('signal', MERGE._kind_of('.pf/signals/cyc/aaaa1111.json'))
        self.assertIsNone(MERGE._kind_of('.pf/notes.json'))

    def test_the_shape_is_the_dispatch_when_there_is_no_name(self):
        """Under git `%O`/`%A`/`%B` are `.merge_file_XXXXXXXX`; `%P` names the file, but the driver
        still has to work if somebody configures it without `%P`."""
        self.assertEqual('ideas', MERGE._kind_from_shape(ideas(aaaa=node('one'))))
        self.assertEqual('state', MERGE._kind_from_shape(cycle([])))
        self.assertEqual('instructions', MERGE._kind_from_shape(instruction()))
        self.assertEqual('signal', MERGE._kind_from_shape(signal('completed')))
        self.assertEqual('state', MERGE._kind_from_shape(None, {'cycles': []}))
        self.assertIsNone(MERGE._kind_from_shape({}, None))
        self.assertIsNone(MERGE._kind_from_shape({'hello': 'world'}))

    def test_help_prints_the_driver_spelling_of_the_git_arguments(self):
        """`--help` is the manual for the git command line: `%O`/`%A`/`%B`/`%P` are printed as
        written, and the command itself returns rc=0.

        argparse treats every `help=` as a format string (`%` must be written `%%`), and one missed
        doubling makes `--help` raise `ValueError`: the passage a person who cannot recognize the
        driver in the git config leans on is exactly this one.
        """
        proc = run_argv([MERGE_PY, '--help'], cwd=self.root, check=False)

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual('', proc.stderr)
        for printed in ("git's %O -- the common ancestor", "git's %A -- the merged result is written",
                        "git's %B -- the other branch",
                        'a git merge driver: %O %A %B [%P]'):
            self.assertIn(printed, proc.stdout)

    def test_the_fourth_argument_names_the_file(self):
        """`git config ... %O %A %B %P`: the temp-file names carry no ledger name, `%P` does."""
        base = ideas(aaaa=node('one'))
        mins = ideas(aaaa=node('one'), bbbb=node('two'))
        theirs = ideas(aaaa=node('one', status='completed'))
        self.write_sides(base, mins, theirs)
        # aim the driver at the temp-file names git actually uses, and name the file with %P
        temp = self.root / '.merge_file_ABC123'
        temp.write_bytes(tool_bytes(mins, sort_keys=True))
        proc = run_argv([MERGE_PY, self.side_path('base'), temp, self.side_path('theirs'),
                         '.pf/ideas.json'], cwd=self.root, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        merged = _decode(temp)
        self.assertEqual({NODE1, NODE2}, set(merged['nodes']))     # ours' two nodes, unioned
        self.assertEqual('completed', merged['nodes'][NODE1]['status'])


class TestTheBytesAreTheToolsOwn(LedgerCase):
    """The merged file must be byte-for-byte what the tool would have written.

    This is the whole reason the driver asks `state.store` and dag-manage for a record's own
    bytes instead of doing its own `json.dumps`: a ledger that differs by one byte from what the tools
    write would show up as a change on every single merge, forever. The reference is the file the
    OWNER's writer really lands, so a writer that moves on its own turns these cases red.
    """

    def reference(self, kind: str, data, path: Path) -> bytes:
        """Write `data` with the writer that OWNS that file, and hand back the bytes it left."""
        if kind == 'ideas':
            dag = LOADER.load_by_path('ledger_merge_test_dag_manage', DAG_MANAGE,
                                      register=True, reuse=True)
            dag.LEDGER_DIR = path.parent
            dag.DATA_DIR = path.parent
            dag.NODES_DIR = path.parent / 'nodes'
            dag.IDEAS_JSON = path
            dag._write(data)
        else:
            STORE._atomic_write(path, data)
        return path.read_bytes()

    def check(self, kind: str, ledger: str, base, ours, theirs, sort_keys: bool):
        self.LEDGER, self.SORT_KEYS = ledger, sort_keys
        proc, ours_path = self.merge(base, ours, theirs)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        written = ours_path.read_bytes()
        reference = self.reference(kind, _decode(ours_path),
                                   self.root / 'reference' / Path(ledger).name)
        self.assertEqual(reference, written, f'{kind}: the merged bytes are not the tools\' bytes')
        self.assertNotIn(b'\n', written.replace(b'\r\n', b''), 'not CRLF throughout')
        self.assertFalse(written.endswith(b'\n'), 'a trailing newline appeared')

    def test_ideas_json_bytes(self):
        self.check('ideas', '.pf/ideas.json', ideas(aaaa=node('one')),
                   ideas(aaaa=node('one'), bbbb=node('two')),
                   ideas(aaaa=node('one', status='completed')), True)

    def test_state_json_bytes(self):
        shared = step(NODE1, '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z')
        self.check('state', '.pf/state.json', cycle([shared]),
                   cycle([shared, step(NODE2, '2026-10-01T01:00:00Z', '2026-10-01T02:00:00Z')]),
                   cycle([shared, step(NODE3, '2026-10-01T03:00:00Z', '2026-10-01T04:00:00Z')]),
                   False)

    def test_instructions_json_bytes(self):
        self.check('instructions', '.pf/instructions.json', instruction(),
                   instruction(note='ours note'), instruction(text='theirs text'), False)

    def test_signal_bytes(self):
        self.check('signal', f'.pf/signals/cyc/{NODE1}.json', signal('pending'),
                   signal('pending', summary='ours'), signal('completed', summary='theirs'), False)


class TestTheDoorsGiveTheWritersBytes(unittest.TestCase):
    """A door's bytes and the bytes its writer lands are one thing, whichever file it is.

    `store.record_bytes` / `dag-manage.ideas_bytes` are what the merge driver compares the merged
    result against ("would this write change anything at all") and lands. That comparison holds while
    the door and the writer agree byte for byte; either side moving alone leaves the driver treating
    its own idea of the format as the file's, which is a change on every single merge. These two cases
    name both sides side by side, so a writer that moves alone turns them red.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ledger-merge-door-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _graph(self):
        return LOADER.load_by_path('ledger_merge_test_dag_manage', DAG_MANAGE,
                                   register=True, reuse=True)

    def test_the_store_door_gives_the_bytes_its_writer_lands(self):
        """`record_bytes` and `_atomic_write` each state the same data once, and the two must be
        identical."""
        data = cycle([step(NODE1, '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z')])
        landed = self.root / 'reference' / 'state.json'

        STORE._atomic_write(landed, data)

        self.assertEqual(landed.read_bytes(), STORE.record_bytes(data))

    def test_the_graph_door_gives_the_bytes_its_writer_lands(self):
        """`ideas_bytes` and `_write`, the same; where `_write` lands is named by this call (the
        three bindings are moved into temp)."""
        data = ideas(aaaa=node('one'), bbbb=node('two'))
        landed = self.root / 'reference' / '.pf' / 'ideas.json'
        dag = self._graph()
        dag.LEDGER_DIR = landed.parent
        dag.DATA_DIR = landed.parent
        dag.NODES_DIR = landed.parent / 'nodes'
        dag.IDEAS_JSON = landed

        dag._write(data)

        self.assertEqual(landed.read_bytes(), dag.ideas_bytes(data))


class TestAMissingToolIsAUsageError(LedgerCase):
    """A tool the driver was pointed at by name that holds nothing: one line, exit 2, file untouched.

    `DSH_CONDUCT_DAG_MANAGE` / `DSH_CONDUCT_STATE_DIR` name where the graph's writer and the store
    live. The driver reads the record's bytes from them for the "would this write change anything"
    question, so both a merge with something to write and one with nothing to write reach for the tool
    -- and a wrong path gets the same answer either way: a person fixes the command or the setting.
    Four shapes, one per kind crossed with the two merge outcomes, plus the control that the same
    fixture still merges when the tool is where the variable did not have to point it.
    """

    def _merge_with(self, ledger: str, env_name: str, base, ours, theirs, env=None):
        """The shipped driver as a process, with `env_name` aimed wherever the caller says."""
        self.LEDGER = ledger
        self.SORT_KEYS = ledger.endswith('ideas.json')
        self.write_sides(base, ours, theirs)
        argv = [MERGE_PY, self.side_path('base'), self.side_path('ours'), self.side_path('theirs'),
                ledger]
        child = dict(_child_env(), **(env or {}))
        return run_argv(argv, cwd=self.root, env=child, check=False)

    def _nowhere(self, env_name: str):
        """The environment that aims `env_name` at a path holding no such tool."""
        return {env_name: str(self.missing_tool())}

    def missing_tool(self):
        """The path those environments point at: what the refusal line has to name."""
        return self.root / 'nowhere' / 'the-tool'

    def assertUsageError(self, proc, ours_data):
        """One line naming the path that came up empty, exit 2, `<ours>` exactly as this case wrote it."""
        self.assertEqual(2, proc.returncode, proc.stdout)
        self.assertNotIn('Traceback', proc.stderr)
        self.assertEqual(1, len(proc.stderr.strip().splitlines()), proc.stderr)
        self.assertIn('ledger-merge:', proc.stderr)
        # The line spells the missing path either plainly or through the OS error's own `repr`
        # (that one doubles every backslash); flatten the repr form before matching.
        self.assertIn(str(self.missing_tool()), proc.stderr.replace('\\\\', '\\'),
                      'a person has to read which path came up empty off this one line')
        self.assertEqual(tool_bytes(ours_data, sort_keys=self.SORT_KEYS),
                         self.side_path('ours').read_bytes(), '<ours> was touched')

    def test_a_missing_store_with_nothing_to_write_is_a_usage_error(self):
        same = cycle([step(NODE1, '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z')])
        proc = self._merge_with('.pf/state.json', 'DSH_CONDUCT_STATE_DIR', same, same, same,
                                env=self._nowhere('DSH_CONDUCT_STATE_DIR'))
        self.assertUsageError(proc, same)

    def test_a_missing_store_with_something_to_write_is_a_usage_error(self):
        base = cycle([step(NODE1, '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z')])
        theirs = cycle([step(NODE2, '2026-10-01T01:00:00Z', '2026-10-01T02:00:00Z')])
        proc = self._merge_with('.pf/state.json', 'DSH_CONDUCT_STATE_DIR', base, base, theirs,
                                env=self._nowhere('DSH_CONDUCT_STATE_DIR'))
        self.assertUsageError(proc, base)

    def test_a_missing_graph_tool_with_nothing_to_write_is_a_usage_error(self):
        same = ideas(aaaa=node('one'))
        proc = self._merge_with('.pf/ideas.json', 'DSH_CONDUCT_DAG_MANAGE', same, same, same,
                                env=self._nowhere('DSH_CONDUCT_DAG_MANAGE'))
        self.assertUsageError(proc, same)

    def test_a_missing_graph_tool_with_something_to_write_is_a_usage_error(self):
        base = ideas(aaaa=node('one'))
        theirs = ideas(aaaa=node('one'), cccc=node('three'))
        proc = self._merge_with('.pf/ideas.json', 'DSH_CONDUCT_DAG_MANAGE', base, base, theirs,
                                env=self._nowhere('DSH_CONDUCT_DAG_MANAGE'))
        self.assertUsageError(proc, base)

    def test_the_same_merge_lands_when_the_tool_is_there(self):
        """The control: with the variable left at its default (the tool in its factory location),
        the same fixture merges and lands as before."""
        base = ideas(aaaa=node('one'))
        theirs = ideas(aaaa=node('one'), cccc=node('three'))

        proc = self._merge_with('.pf/ideas.json', 'DSH_CONDUCT_DAG_MANAGE', base, base, theirs)

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual({NODE1, NODE3}, set(_decode(self.side_path('ours'))['nodes']))


# `cases_ledger_merge_git.py` holds the REAL-git cases (merge / merge conflict / rebase). They live in
# their own module because a git process per step priced this file over the repo's per-file budget;
# `test_conduct_pipeline.py` star-imports both, so the suite still runs them in parallel with everything
# else. See that module's docstring for the split's one-line rationale.


if __name__ == '__main__':
    unittest.main(verbosity=2)
