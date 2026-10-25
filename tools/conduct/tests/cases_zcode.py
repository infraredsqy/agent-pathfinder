"""The second spawner: which host opens the next session, and what the ZCode half writes.

`dsh-spawn.ps1` opens a DSH session over the DSH web API. A ZCode session cannot be started that way
and still be visible: ZCode's task list is a projection seeded once inside its agent host, with no
re-read path for a local workspace, so a session started from outside never reaches the GUI. The
ZCode half therefore queues the node into the desktop's own cron scheduler -- one row in
`~/.zcode/v2/tasks-index.sqlite` -- and waits for the session the desktop creates from it.

What is pinned here: the choice between the two spawners (an explicit setting, then the host, then
the historical default), the shape of that one row, and the two loud refusals (no prompt file, a
time too far in the past). Nothing here starts a session: `ZCODE_DATA_BASE_DIR` points the
dispatcher at a sandbox database, which is the same knob a drill uses.
"""
from __future__ import annotations

import ast
import importlib.util as importlib_util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import pipeline
from harness import _child_env, run_argv  # noqa: E402

__all__ = ['TestSpawnerChoice', 'TestQueueDedup', 'TestSessionMatch']

CONDUCT_DIR = Path(pipeline.__file__).resolve().parent
DISPATCHER = CONDUCT_DIR / 'spawn' / 'zcode-dispatch.py'
SPAWNER = CONDUCT_DIR / 'spawn' / 'zcode-spawn.ps1'


def run_dispatcher(args, env):
    """The ZCode dispatcher as a real process, against the sandbox database `env` names.

    Through the suite's one child runner, so the decoding and the non-zero exit read like every
    other tool call; the return code is the caller's to check (this one refuses in several places).
    """
    return run_argv([DISPATCHER, *map(str, args)], env=env, check=False)


def _host():
    """`spawn/host.py`, loaded by path -- where the host's own answer lives.

    The probe is asked of THIS module: the two hosts, the two spawner paths and the process-tree walk
    sit next to the scripts they name, and the entry keeps one seam name (`SPAWNER_SCRIPT`) out of it.
    """
    spec = importlib_util.spec_from_file_location('pf_spawn_host_case', CONDUCT_DIR / 'spawn' / 'host.py')
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def marker_path(root, cycle):
    """The landing place `spawn/marker.py` computes: the test follows the one source, and never
    assembles that path again here."""
    spec = importlib_util.spec_from_file_location('pf_spawn_marker_case',
                                                  CONDUCT_DIR / 'spawn' / 'marker.py')
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.marker_path(root, cycle)


# The two tables the dispatcher touches, as the desktop ships them. Spelled out rather than trimmed:
# a column this row forgets is exactly the failure the DDL is here to catch. The three project
# tables are the shape `project_for` reads its one link from.
AUTOMATIONS_DDL = """
CREATE TABLE automations (
  automation_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
  cron_expr TEXT NOT NULL, prompt TEXT NOT NULL, model TEXT, provider TEXT,
  mode TEXT, thought_level TEXT, model_selection TEXT,
  workspace_key TEXT NOT NULL, workspace_path TEXT NOT NULL, workspace_identity TEXT,
  target_task_id TEXT, bot_delivery_target TEXT,
  project_id TEXT, host_id TEXT, additional_directories TEXT,
  location_kind TEXT NOT NULL DEFAULT 'local', recurring INTEGER NOT NULL DEFAULT 1,
  max_runs INTEGER, end_at INTEGER, schedule_rule TEXT,
  schedule_edited_by_user INTEGER NOT NULL DEFAULT 0,
  run_count INTEGER NOT NULL DEFAULT 0, scheduled_run_count INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1, lifecycle_status TEXT NOT NULL DEFAULT 'active',
  next_run_at INTEGER, last_run_at INTEGER, running INTEGER NOT NULL DEFAULT 0,
  claimed_at INTEGER, dispatch_status TEXT NOT NULL DEFAULT 'idle',
  dispatch_attempts INTEGER NOT NULL DEFAULT 0, retry_at INTEGER, last_error TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)
"""

TASKS_DDL = """
CREATE TABLE tasks (
  workspace_key TEXT NOT NULL, workspace_path TEXT NOT NULL, workspace_identity TEXT,
  task_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', task_status TEXT,
  provider TEXT, mode TEXT NOT NULL DEFAULT 'build', model TEXT,
  migration_source TEXT, forked_from_task_id TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  unread_at INTEGER, last_unread_at INTEGER NOT NULL DEFAULT 0,
  pinned INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0, title_overridden INTEGER NOT NULL DEFAULT 0,
  meta_json TEXT NOT NULL DEFAULT '{}', searchable_text TEXT NOT NULL DEFAULT '',
  cron_automation_id TEXT, off_peak_task_id TEXT,
  host_id TEXT, root_path TEXT, project_id TEXT,
  PRIMARY KEY (workspace_key, task_id))
"""

PROJECT_DDL = """
CREATE TABLE hosts (
  host_id TEXT PRIMARY KEY, kind TEXT NOT NULL, target_json TEXT,
  membership_id TEXT, position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE projects (
  project_id TEXT PRIMARY KEY, host_id TEXT NOT NULL, name TEXT NOT NULL,
  name_overridden INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 0,
  pinned INTEGER NOT NULL DEFAULT 0, pinned_at INTEGER,
  deleted INTEGER NOT NULL DEFAULT 0, deleted_at INTEGER,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE project_roots (
  project_id TEXT NOT NULL, root_path TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0, is_primary INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (project_id, root_path));
"""


#: The node the queue-dedup cases queue a prompt for: a prompt under `launch/<node_id>/` is the
#: shape the dispatcher reads an owning node out of. The spawner-choice cases keep theirs at the
#: project root, which is what a hand-run prompt looks like.
QUEUED_NODE = 'dddddddd-4444-4444-8444-dddddddddddd'


def zcode_sandbox(test, node_id=None):
    """A throwaway ZCode host: the desktop's two tables, one workspace, one prompt file.

    `test` is the case the temp directory is registered with. Returns (the database, the workspace,
    the prompt file, the child environment) -- the four things every dispatcher case hands on.
    """
    root = Path(tempfile.mkdtemp(prefix='conduct-zcode-'))
    test.addCleanup(shutil.rmtree, root, ignore_errors=True)
    (root / 'sandbox' / '.zcode' / 'v2').mkdir(parents=True)
    db = root / 'sandbox' / '.zcode' / 'v2' / 'tasks-index.sqlite'
    con = sqlite3.connect(str(db))
    con.executescript(AUTOMATIONS_DDL)
    con.executescript(TASKS_DDL)
    con.executescript(PROJECT_DDL)
    con.commit()
    con.close()
    ws = root / 'ws'
    (ws / '.pf').mkdir(parents=True)
    prompt = (root / 'launch' / node_id / 'prompt.txt') if node_id else (root / 'prompt.txt')
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text('# /pf-conduct drill\n', encoding='utf-8')
    env = dict(_child_env(), ZCODE_DATA_BASE_DIR=str(root / 'sandbox'),
               DSH_CONDUCT_SPAWN_AT='')
    return db, ws, prompt, env


def dispatcher():
    """The dispatcher, imported -- for the constants a test should *follow*, not pin.

    The model lives behind a one-word switch (`MODEL_CHOICES` / `DEFAULT_MODEL`); a test that
    hard-codes the active name would turn flipping that switch into a test edit.
    """
    spec = importlib_util.spec_from_file_location('zcode_dispatch', DISPATCHER)
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSpawnerChoice(unittest.TestCase):
    """One spawner per host, and "which one" is answered in exactly one place."""

    # ── picking the spawner ──────────────────────────────────────────────────

    def test_the_two_names_map_to_the_two_scripts(self):
        host = _host()
        self.assertEqual(host.spawn_path('dsh').name, 'dsh-spawn.ps1')
        self.assertEqual(host.spawn_path('zcode').name, 'zcode-spawn.ps1')
        self.assertEqual(host.spawn_path('ZCODE').name, 'zcode-spawn.ps1')
        self.assertEqual(host.spawn_path(r'C:\tmp\mine.ps1').name, 'mine.ps1')

    def test_an_explicit_setting_beats_the_host(self):
        host = _host()
        with mock.patch.dict(os.environ, {'DSH_CONDUCT_SPAWNER': 'zcode'}, clear=True), \
                mock.patch.object(host, 'under_zcode', return_value=False):
            self.assertEqual(host.default_spawner().name, 'zcode-spawn.ps1')

    def test_dsh_answers_for_itself(self):
        """DSH injects `DSH_SESSION_ID` into every shell; that is its own answer, and it comes first."""
        host = _host()
        with mock.patch.dict(os.environ, {'DSH_SESSION_ID': 'sess_x'}, clear=True), \
                mock.patch.object(host, 'under_zcode', return_value=True):
            self.assertEqual(host.default_spawner().name, 'dsh-spawn.ps1')

    def test_a_zcode_ancestor_picks_the_zcode_spawner(self):
        host = _host()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(host, 'under_zcode', return_value=True):
            self.assertEqual(host.default_spawner().name, 'zcode-spawn.ps1')

    def test_the_default_stays_dsh(self):
        """No setting, no host marker: a human at a prompt keeps the behaviour DSH always had."""
        host = _host()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(host, 'under_zcode', return_value=False):
            self.assertEqual(host.default_spawner().name, 'dsh-spawn.ps1')

    def test_the_probe_lives_next_to_the_scripts_it_names(self):
        """The host probe has exactly one home, `spawn/host.py`; the beat a chain runs for
        itself goes through the pipeline's door.

        The seam module (`conduct_pipeline.py`) is the one a caller patches: the probe lives apart
        from it, so any change to the probe lands in its own file. `chain-spawn.py` is only a CLI
        shell, and the beat that opens a session lands on the pipeline's door (it carries the session
        note and the marker's write and clear; the claim one is out of reach -- that chain's prompt
        is not in the launch directory, see `chain-spawn.py`'s module docstring).
        """
        entry = (CONDUCT_DIR / 'conduct_pipeline.py').read_text(encoding='utf-8')
        imported = {alias.name for node in ast.walk(ast.parse(entry))
                    if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
        self.assertNotIn('ctypes', imported, 'the host probe lives in the seam module')

        tree = ast.parse((CONDUCT_DIR / 'spawn' / 'chain-spawn.py').read_text(encoding='utf-8'))
        # The second door may leave no trace at all in this file: aliases, inline calls, module
        # names and path strings all count. Searched case-sensitively, so `DSH_CONDUCT_SPAWNER`-style
        # environment variable names stay apart from the module.
        texts = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        texts |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        texts |= {node.value for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertEqual([text for text in sorted(texts) if 'conduct_spawn' in text], [],
                         'the second door: conduct_spawn shows up in chain-spawn.py again')

        # Pinned from the other end too: the beat that opens a session lands on the pipeline. The
        # scan above checks that it calls no other door; this one checks that the door it calls is
        # that one -- an alias retargeted, with one end caught by each.
        bound = {}
        for node in sorted((n for n in ast.walk(tree)
                            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                            and isinstance(n.value, ast.Call)
                            and isinstance(n.value.func, ast.Name)
                            and n.value.func.id == '_loaded' and n.value.args),
                           key=lambda n: n.lineno):
            parts = sorted((part for part in ast.walk(node.value.args[0])
                            if isinstance(part, ast.Constant) and isinstance(part.value, str)),
                           key=lambda part: (part.lineno, part.col_offset))
            bound[node.targets[0].id] = parts[-1].value if parts else ''
        receivers = {node.func.value.id for node in ast.walk(tree)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                     and node.func.attr == 'spawn_session' and isinstance(node.func.value, ast.Name)}
        self.assertTrue(receivers, 'chain-spawn calls no spawn_session')
        for name in sorted(receivers):
            self.assertEqual(bound.get(name), 'conduct_pipeline.py',
                             f'{name}.spawn_session must be bound to the pipeline door')

        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertNotIn('default_spawner', called, 'picking the spawner belongs to that door')
        self.assertNotIn('argv_for', called, 'the spawner argv is assembled by conduct_spawn')

    # ── the line-policy tier ─────────────────────────────────────────────────
    # `resolve_spawner`'s whole priority: the CLI naming it > this line's `spawner` policy >
    # environment variable > host > `dsh`. A line policy is a decision on disk
    # (`cycle policy <cycle> spawner …`), so it beats this machine's environment variable and host
    # probe -- a probe that fails intermittently must not send a line pinned to zcode back to DSH.

    def policy_root(self, policies):
        """A minimal project: `.pf/state.json` with one cycle, carrying `policies` (or none)."""
        root = Path(tempfile.mkdtemp(prefix='conduct-spawner-policy-'))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / '.pf').mkdir()
        cycle = {'id': 'cyc', 'status': 'active', 'holding': []}
        if policies is not None:
            cycle['policies'] = policies
        (root / '.pf' / 'state.json').write_text(
            json.dumps({'cycles': [cycle]}), encoding='utf-8')
        return root

    def test_a_line_policy_beats_the_machine_and_the_host(self):
        """The line policy beats everything on this machine: the environment variable and the seam
        both say zcode, and the line still goes dsh."""
        root = self.policy_root({'spawner': 'dsh'})
        with mock.patch.dict(os.environ, {'DSH_CONDUCT_SPAWNER': 'zcode'}, clear=True), \
                mock.patch.object(pipeline, 'SPAWNER_SCRIPT', _host().SPAWN_SCRIPTS['zcode']):
            self.assertEqual(pipeline.resolve_spawner(root, 'cyc').name, 'dsh-spawn.ps1')

    def test_the_per_call_flag_beats_the_line_policy(self):
        root = self.policy_root({'spawner': 'dsh'})
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(pipeline.resolve_spawner(root, 'cyc', 'zcode').name,
                             'zcode-spawn.ps1')

    def test_a_line_without_the_policy_reaches_the_seam(self):
        """No policy on the line: the machine answers exactly as before the policy existed.

        The seam (`SPAWNER_SCRIPT`), not a fresh `default_spawner()`: that is the replacement point
        `writer.spawn_session` already honours, and replacing it must survive a line that never
        named a policy.
        """
        root = self.policy_root(None)
        with mock.patch.object(pipeline, 'SPAWNER_SCRIPT', _host().SPAWN_SCRIPTS['zcode']), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(pipeline.resolve_spawner(root, 'cyc').name, 'zcode-spawn.ps1')

    def test_an_unknown_cycle_reads_as_no_policy(self):
        """A cycle id nothing holds (a caller's typo) must not decide: the seam answers.

        Compared against the seam itself, not a spelled-out script name: whichever host this
        suite runs under computed `SPAWNER_SCRIPT`, and the point is only that the typo changed
        nothing.
        """
        root = self.policy_root({'spawner': 'dsh'})
        self.assertEqual(pipeline.resolve_spawner(root, 'no-such-cycle'), pipeline.SPAWNER_SCRIPT)

    def test_off_is_a_word_the_writer_side_clears(self):
        """`cycle policy <cycle> spawner off` removes the key; the read side then reports nothing."""
        root = self.policy_root({'spawner': 'zcode'})
        self.assertEqual(pipeline.cycle_policy(pipeline.find_cycle(
            pipeline.load_state(root), 'cyc')).get('spawner'), 'zcode')
        root2 = self.policy_root(None)
        self.assertIsNone(pipeline.cycle_policy(pipeline.find_cycle(
            pipeline.load_state(root2), 'cyc')).get('spawner'))

    # ── that one row ─────────────────────────────────────────────────────────

    def test_it_writes_one_row_that_means_one_session(self):
        db, ws, prompt, env = zcode_sandbox(self)
        proc = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        automation_id = proc.stdout.strip().splitlines()[0]
        self.assertTrue(automation_id.startswith('automation-'), proc.stdout)
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        row = dict(con.execute('select * from automations').fetchone())
        con.close()
        self.assertEqual(row['recurring'], 0)
        self.assertEqual(row['max_runs'], 1)
        self.assertEqual(row['enabled'], 1)
        self.assertEqual(row['running'], 0)
        self.assertEqual(row['lifecycle_status'], 'active')
        self.assertEqual(row['dispatch_status'], 'idle')
        self.assertEqual(row['location_kind'], 'local')
        # The model is named, not inherited: a node session runs unattended, so "whatever the GUI's
        # picker last pointed at" is not an answer a caller can budget. Which name it is comes from
        # the switch, and the row must carry exactly that -- including the `$level` the desktop
        # insists on.
        dispatch = dispatcher()
        self.assertIn(dispatch.DEFAULT_MODEL, dispatch.MODEL_CHOICES.values(),
                      'the switch must point at a named choice, not a string typed inline')
        self.assertEqual(row['model_selection'],
                         dispatch.selection_json(dispatch.DEFAULT_MODEL))
        # Full access, and it is not a preference: a node that stops to ask before its first edit
        # has nobody to answer it.
        self.assertEqual(row['mode'], 'yolo')
        self.assertEqual(row['workspace_key'], str(ws))
        self.assertEqual(row['workspace_path'], str(ws))
        self.assertEqual(row['prompt'], prompt.read_text(encoding='utf-8'))
        self.assertLess(abs(row['next_run_at'] - int(time.time() * 1000)), 60_000)
        self.assertEqual(row['title'], prompt.parent.name)

    def test_the_model_can_be_pinned(self):
        """Naming the model is how a caller decides whose quota a background node spends."""
        db, ws, prompt, env = zcode_sandbox(self)
        proc = run_dispatcher(['--prompt-file', prompt, '--workspace', ws,
                               '--model', 'deepseek/deepseek-flash$max', '--no-verify'], env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        con = sqlite3.connect(str(db))
        stored = con.execute('select model_selection from automations').fetchone()[0]
        con.close()
        self.assertEqual(json.loads(stored),
                         {'providerId': 'deepseek', 'modelId': 'deepseek-flash',
                          'options': {'reasoningLevel': 'max'}})

    def test_following_the_workspace_pick_is_still_available(self):
        """`workspace` is the one encoding that means "follow whatever model this workspace uses"."""
        db, ws, prompt, env = zcode_sandbox(self)
        proc = run_dispatcher(['--prompt-file', prompt, '--workspace', ws,
                               '--model', 'workspace', '--no-verify'], env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        con = sqlite3.connect(str(db))
        stored = con.execute('select model_selection from automations').fetchone()[0]
        con.close()
        self.assertEqual(stored, 'null')

    def test_an_unreadable_model_is_refused(self):
        """The desktop refuses a column it cannot parse, three steps later and far from here."""
        db, ws, prompt, env = zcode_sandbox(self)
        for bad in ('deepseek', 'deepseek/', '/flash', '{"providerId":"x"}',
                    '{"providerId":"a","modelId":"b","extra":1}'):
            proc = run_dispatcher(['--prompt-file', prompt, '--workspace', ws,
                                   '--model', bad, '--no-verify'], env)
            self.assertNotEqual(proc.returncode, 0, f'{bad!r} should have been refused')
            self.assertIn('--model', proc.stderr)

    def test_the_permission_mode_can_be_turned_down(self):
        db, ws, prompt, env = zcode_sandbox(self)
        proc = run_dispatcher(['--prompt-file', prompt, '--workspace', ws,
                               '--mode', 'plan', '--no-verify'], env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        con = sqlite3.connect(str(db))
        self.assertEqual(con.execute('select mode from automations').fetchone()[0], 'plan')
        con.close()

    def test_a_prompt_that_is_not_there_is_refused(self):
        db, ws, prompt, env = zcode_sandbox(self)
        proc = run_dispatcher(['--prompt-file', prompt.parent / 'nope.txt',
                                    '--workspace', ws, '--no-verify'], env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('prompt file not found', proc.stderr)
        con = sqlite3.connect(str(db))
        self.assertEqual(con.execute('select count(*) from automations').fetchone()[0], 0)
        con.close()

    def test_a_time_too_far_past_is_refused(self):
        """The desktop drops a row more than five minutes overdue as a misfire, so refuse it here."""
        db, ws, prompt, env = zcode_sandbox(self)
        proc = run_dispatcher(['--prompt-file', prompt, '--workspace', ws,
                               '--at', '2020-01-01T00:00', '--no-verify'], env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('misfire', proc.stderr)

    def test_a_time_in_the_future_is_not_waited_for(self):
        db, ws, prompt, env = zcode_sandbox(self)
        started = time.monotonic()
        proc = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--at', '+30m'], env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(time.monotonic() - started, 10,
                        'a row scheduled for later must not be waited on now')
        con = sqlite3.connect(str(db))
        when = con.execute('select next_run_at from automations').fetchone()[0]
        con.close()
        self.assertGreater(when - int(time.time() * 1000), 29 * 60_000)

    # ── the wrapper script ───────────────────────────────────────────────────

    def test_the_spawner_ends_with_its_spawned_line(self):
        """The pipeline reads the spawner's last line; the marker must not be left behind.

        The environment is also how `mode` / `model` reach the spawner: the pipeline calls
        `state-manage.py`, whose argv to a spawner is the same three named parameters for both
        hosts, so anything else the spawner needs travels in the environment.
        """
        if not shutil.which('pwsh'):
            self.skipTest('pwsh is not on PATH')
        db, ws, prompt, env = zcode_sandbox(self)
        env = {**env, 'DSH_CONDUCT_SPAWN_MODE': 'yolo',
               'DSH_CONDUCT_SPAWN_MODEL': 'deepseek/deepseek-flash$max'}
        marker = marker_path(ws, 'cyc-case')
        proc = subprocess.run(['pwsh', '-NoProfile', '-File', str(SPAWNER),
                               '-CycleId', 'cyc-case', '-PromptFile', str(prompt),
                               '-WorkspacePath', str(ws), '-MarkerPath', str(marker), '-NoVerify'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip().splitlines()[-1].startswith('spawned: automation '),
                        proc.stdout)
        self.assertFalse(marker.exists(), 'after a successful dispatch the old degradation marker is still on disk')
        con = sqlite3.connect(str(db))
        mode, selection = con.execute('select mode, model_selection from automations').fetchone()
        con.close()
        self.assertEqual(mode, 'yolo')
        self.assertEqual(json.loads(selection),
                         {'providerId': 'deepseek', 'modelId': 'deepseek-flash',
                          'options': {'reasoningLevel': 'max'}})

    def test_the_spawner_records_a_failure_the_way_the_pipeline_reads_it(self):
        if not shutil.which('pwsh'):
            self.skipTest('pwsh is not on PATH')
        db, ws, prompt, env = zcode_sandbox(self)
        marker = marker_path(ws, 'cyc-case')
        proc = subprocess.run(['pwsh', '-NoProfile', '-File', str(SPAWNER),
                               '-CycleId', 'cyc-case', '-PromptFile', str(prompt.parent / 'nope.txt'),
                               '-WorkspacePath', str(ws), '-MarkerPath', str(marker), '-NoVerify'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(marker.is_file(), 'a failed spawn must leave the marker the pipeline reads')
        record = json.loads(marker.read_text(encoding='utf-8'))
        self.assertEqual(record['cycle_id'], 'cyc-case')
        self.assertEqual(record['phase'], 'read-prompt')
        # The host's own facts get a slot of their own, `extra`: dsh puts api_base there, this one
        # puts the database it queues through.
        self.assertEqual(record['extra'], {'db': str(db)})
        self.assertEqual(set(record), {'cycle_id', 'phase', 'attempts', 'reason', 'workspace',
                                       'extra', 'detected_at', 'note'})

    def test_a_host_with_no_python_still_leaves_the_marker(self):
        """ZCode's half needs python to queue; the beat that cannot find it still leaves the
        degradation marker per contract 2 and exits.

        The shell writes the marker itself (no python involved), so that beat can put the record on
        disk -- the guarantee the HEAD version already had.
        """
        if not shutil.which('pwsh'):
            self.skipTest('pwsh is not on PATH')
        _, ws, prompt, env = zcode_sandbox(self)
        no_path = {key: value for key, value in env.items() if key.upper() != 'PATH'}
        marker = marker_path(ws, 'cyc-case')
        proc = subprocess.run(['pwsh', '-NoProfile', '-File', str(SPAWNER),
                               '-CycleId', 'cyc-case', '-PromptFile', str(prompt),
                               '-WorkspacePath', str(ws), '-MarkerPath', str(marker), '-NoVerify'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=no_path)
        self.assertNotEqual(proc.returncode, 0, 'python was not found, yet it exited as a success')
        self.assertTrue(marker.is_file(), 'the no-python beat left no degradation marker')
        self.assertEqual('find-python', json.loads(marker.read_text(encoding='utf-8'))['phase'])


class TestQueueDedup(unittest.TestCase):
    """A second row for the same node is the road to two sessions on one node: rows in flight
    and sessions still warm both have to be blocked."""

    def test_a_second_row_for_the_same_node_is_refused(self):
        db, ws, prompt, env = zcode_sandbox(self, QUEUED_NODE)
        first = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('already queued', second.stderr)
        con = sqlite3.connect(str(db))
        count = con.execute('select count(*) from automations').fetchone()[0]
        con.close()
        self.assertEqual(count, 1, 'the refused queue must not leave a row behind')

    def test_an_explicit_override_queues_anyway(self):
        db, ws, prompt, env = zcode_sandbox(self, QUEUED_NODE)
        first = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify',
                                 '--allow-existing'], env)
        self.assertEqual(second.returncode, 0, second.stderr)

    def test_a_claimed_row_is_no_longer_pending(self):
        """Once the desktop has claimed the row and the claim is past the freshness window (and no
        session was written either), the documented salvage re-queue is legal."""
        db, ws, prompt, env = zcode_sandbox(self, QUEUED_NODE)
        first = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(first.returncode, 0, first.stderr)
        con = sqlite3.connect(str(db))
        con.execute('update automations set claimed_at=?, run_count=1, dispatch_status=?',
                    (int(time.time() * 1000) - 11 * 60 * 1000, 'dispatched'))
        con.commit()
        con.close()
        again = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(again.returncode, 0, again.stderr)

    def test_a_claim_just_made_with_no_session_blocks(self):
        """The narrow gap between claim and commit: the row has just been claimed and the session
        is not written yet, so a second queue slips past both old checks."""
        db, ws, prompt, env = zcode_sandbox(self, QUEUED_NODE)
        first = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(first.returncode, 0, first.stderr)
        con = sqlite3.connect(str(db))
        con.execute('update automations set claimed_at=?, run_count=1, dispatch_status=?',
                    (int(time.time() * 1000), 'dispatched'))
        con.commit()
        con.close()
        second = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('was just claimed', second.stderr)
        con = sqlite3.connect(str(db))
        count = con.execute('select count(*) from automations').fetchone()[0]
        con.close()
        self.assertEqual(count, 1, 'the refused queue must not leave a row behind')

    def test_a_fresh_session_blocks_and_an_old_one_does_not(self):
        """The race where verification times out: the session did get created -- a session under ten
        minutes old blocks the re-queue, and an older one counts as dead and goes through."""
        db, ws, prompt, env = zcode_sandbox(self, QUEUED_NODE)
        first = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(first.returncode, 0, first.stderr)
        automation_id = first.stdout.strip().splitlines()[0]
        now = int(time.time() * 1000)
        con = sqlite3.connect(str(db))
        con.execute('update automations set claimed_at=?, run_count=1, dispatch_status=?',
                    (now, 'dispatched'))
        con.execute('insert into tasks (workspace_key, workspace_path, task_id, created_at, '
                    'updated_at, cron_automation_id) values (?,?,?,?,?,?)',
                    (str(ws), str(ws), 'sess_fresh', now, now, automation_id))
        con.commit()
        con.close()

        blocked = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('already exists', blocked.stderr)

        con = sqlite3.connect(str(db))
        con.execute('update tasks set created_at=? where task_id=?',
                    (now - 11 * 60 * 1000, 'sess_fresh'))
        con.commit()
        con.close()
        allowed = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)


class TestSessionMatch(unittest.TestCase):
    """The waiter's matcher, against the row shapes both desktop generations write.

    Since v3.16 the host-side createTask carries no cron identity: `cron_automation_id` stays NULL
    and `meta_json` has no `cronAutomationId`. A matcher reading only the column therefore reported
    "no session appeared" while the session ran headless -- and the caller's retry booted a second
    one onto the same node. These cases pin the two matchers and the dedup checks against the
    column-less shape, without unpinning the column shape older desktops still write.
    """

    PROMPT_TITLE = '# /pf-conduct drill'  # zcode_sandbox writes this prompt

    def queued(self):
        """One queued row for QUEUED_NODE: (db, workspace, prompt, env, automation_id)."""
        db, ws, prompt, env = zcode_sandbox(self, QUEUED_NODE)
        proc = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return db, ws, prompt, env, proc.stdout.strip().splitlines()[0]

    def insert_task(self, db, ws, task_id, created_at, title='', cron_id=None):
        """A host-written tasks row, in either generation's shape (title-only, or cron column)."""
        con = sqlite3.connect(str(db))
        con.execute('insert into tasks (workspace_key, workspace_path, task_id, title, '
                    'created_at, updated_at, cron_automation_id) values (?,?,?,?,?,?,?)',
                    (str(ws), str(ws), task_id, title, created_at, created_at, cron_id))
        con.commit()
        con.close()

    def add_project(self, db, ws, project_id='proj-1', deleted=0):
        """One desktop project whose single root is this workspace."""
        now = int(time.time() * 1000)
        con = sqlite3.connect(str(db))
        con.execute("insert or ignore into hosts (host_id, kind) values ('local', 'local')")
        con.execute('insert into projects (project_id, host_id, name, created_at, updated_at, '
                    'deleted) values (?,?,?,?,?,?)', (project_id, 'local', 'proj', now, now,
                                                      deleted))
        con.execute('insert into project_roots (project_id, root_path) values (?,?)',
                    (project_id, str(ws)))
        con.commit()
        con.close()

    # ── the project the GUI hangs the session under ──────────────────────────

    def test_the_row_carries_the_workspace_project(self):
        """A workspace the desktop keeps one project for: the queued row names it and the local
        host, so the session is born where the GUI can render it."""
        db, ws, prompt, env = zcode_sandbox(self, QUEUED_NODE)
        self.add_project(db, ws)
        proc = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        con = sqlite3.connect(str(db))
        project_id, host_id = con.execute(
            'select project_id, host_id from automations').fetchone()
        con.close()
        self.assertEqual(project_id, 'proj-1')
        self.assertEqual(host_id, 'local')

    def test_a_workspace_without_a_project_queues_unlinked(self):
        db, ws, prompt, env = zcode_sandbox(self, QUEUED_NODE)
        proc = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        con = sqlite3.connect(str(db))
        project_id, host_id = con.execute(
            'select project_id, host_id from automations').fetchone()
        con.close()
        self.assertIsNone(project_id)
        self.assertIsNone(host_id)

    def test_an_ambiguous_project_is_left_unlinked(self):
        """Two projects on one root: the desktop's own rule reads that as not disambiguated."""
        db, ws, prompt, env = zcode_sandbox(self, QUEUED_NODE)
        self.add_project(db, ws, 'proj-1')
        self.add_project(db, ws, 'proj-2')
        proc = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        con = sqlite3.connect(str(db))
        project_id, = con.execute('select project_id from automations').fetchone()
        con.close()
        self.assertIsNone(project_id)

    def test_the_wait_links_the_session_row_to_the_project(self):
        """The link-back beat: the session lands unlinked (the v3.16 shape), the dispatcher's wait
        sees it and fills the project -- the row then renders in the GUI's project list."""
        db, ws, prompt, env = zcode_sandbox(self, QUEUED_NODE)
        self.add_project(db, ws)
        proc = subprocess.Popen(
            [sys.executable, '-X', 'utf8', str(DISPATCHER), '--prompt-file', str(prompt),
             '--workspace', str(ws), '--verify-seconds', '20'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
            errors='replace', env=env)
        time.sleep(2.0)
        self.insert_task(db, ws, 'session_mid', int(time.time() * 1000),
                         title=self.PROMPT_TITLE)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn('session: session_mid', out)
        con = sqlite3.connect(str(db))
        project_id, = con.execute(
            'select project_id from tasks where task_id=?', ('session_mid',)).fetchone()
        con.close()
        self.assertEqual(project_id, 'proj-1')

    def test_the_link_back_never_overwrites_a_project_the_desktop_set(self):
        db, ws, prompt, env = zcode_sandbox(self, QUEUED_NODE)
        self.add_project(db, ws, 'proj-desktop')
        now = int(time.time() * 1000)
        self.insert_task(db, ws, 'session_linked', now, title=self.PROMPT_TITLE)
        con = sqlite3.connect(str(db))
        con.execute('update tasks set project_id=? where task_id=?',
                    ('proj-already', 'session_linked'))
        con.commit()
        con.close()
        module = dispatcher()
        module.INDEX_PATH = db  # the helper opens the module path; point it at the sandbox
        module.link_task_project(str(ws), 'session_linked')
        con = sqlite3.connect(str(db))
        project_id, = con.execute(
            'select project_id from tasks where task_id=?', ('session_linked',)).fetchone()
        con.close()
        self.assertEqual(project_id, 'proj-already')

    # ── the two matchers ─────────────────────────────────────────────────────

    def sandbox_db(self, db):
        """A connection to the sandbox database, for calling the matcher directly.

        `task_row` takes the connection -- the one seam that does not read the module-level
        `INDEX_PATH`, which (module loaded in this process) points at this host's real library.
        """
        return sqlite3.connect(str(db))

    def test_a_row_without_the_cron_identity_is_found_by_its_title(self):
        """The v3.16 shape: the column is NULL, the title is the prompt's first line."""
        db, ws, prompt, env, automation_id = self.queued()
        now = int(time.time() * 1000)
        con = self.sandbox_db(db)
        self.assertIsNone(
            dispatcher().task_row(con, str(ws), automation_id, self.PROMPT_TITLE, now - 2000),
            'nothing has landed yet')
        self.insert_task(db, ws, 'session_new', now, title=self.PROMPT_TITLE)
        self.assertEqual(
            dispatcher().task_row(con, str(ws), automation_id, self.PROMPT_TITLE, now - 2000),
            'session_new')
        con.close()

    def test_a_row_with_the_cron_identity_is_still_found_by_the_column(self):
        """The pre-v3.16 shape: the column carries the id even when the title differs."""
        db, ws, prompt, env, automation_id = self.queued()
        now = int(time.time() * 1000)
        self.insert_task(db, ws, 'sess_old', now - 30_000, title='something else',
                         cron_id=automation_id)
        con = self.sandbox_db(db)
        self.assertEqual(
            dispatcher().task_row(con, str(ws), automation_id, self.PROMPT_TITLE, now - 60_000),
            'sess_old')
        con.close()

    def test_rows_created_before_the_queue_moment_do_not_count(self):
        """The title matcher must not mistake an older session of the same node for this one."""
        db, ws, prompt, env, automation_id = self.queued()
        now = int(time.time() * 1000)
        self.insert_task(db, ws, 'session_stale', now - 10 * 60_000, title=self.PROMPT_TITLE)
        con = self.sandbox_db(db)
        self.assertIsNone(
            dispatcher().task_row(con, str(ws), automation_id, self.PROMPT_TITLE, now - 2000))
        con.close()

    def test_the_claim_beat_counts_any_generation_of_the_session(self):
        """The claimed row's own question is "has a session been written at all", not "how recent":
        a task row older than the claim (a retry of a dispatch whose session already existed) must
        not read as the sessionless race."""
        db, ws, prompt, env, automation_id = self.queued()
        now = int(time.time() * 1000)
        con = sqlite3.connect(str(db))
        con.execute('update automations set claimed_at=?, run_count=1, dispatch_status=?',
                    (now, 'dispatched'))
        con.commit()
        con.close()
        self.insert_task(db, ws, 'session_old', now - 11 * 60_000, title=self.PROMPT_TITLE)
        again = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(again.returncode, 0, again.stderr)

    def test_a_title_matched_fresh_session_blocks_the_requeue(self):
        """The end-to-end shape that booted duplicate sessions: the session landed (title match,
        NULL column) and is fresh, so the claimed beat passes it and the freshness beat refuses."""
        db, ws, prompt, env, automation_id = self.queued()
        now = int(time.time() * 1000)
        con = sqlite3.connect(str(db))
        con.execute('update automations set claimed_at=?, run_count=1, dispatch_status=?',
                    (now, 'dispatched'))
        con.commit()
        con.close()
        self.insert_task(db, ws, 'session_new', now, title=self.PROMPT_TITLE)
        second = run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('already exists', second.stderr)

    # ── the wait the pipeline leans on ───────────────────────────────────────

    def test_the_wait_sees_the_session_land_mid_poll(self):
        """Row queued, tasks row written two seconds later with a NULL cron column: the wait
        catches it and reports the session -- the beat whose silent failure doubled sessions."""
        db, ws, prompt, env = zcode_sandbox(self, QUEUED_NODE)
        proc = subprocess.Popen(
            [sys.executable, '-X', 'utf8', str(DISPATCHER), '--prompt-file', str(prompt),
             '--workspace', str(ws), '--verify-seconds', '20'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
            errors='replace', env=env)
        time.sleep(2.0)
        self.insert_task(db, ws, 'session_mid', int(time.time() * 1000),
                         title=self.PROMPT_TITLE)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn('session: session_mid', out)

    def test_the_wait_gives_up_loudly_when_nothing_lands(self):
        db, ws, prompt, env = zcode_sandbox(self, QUEUED_NODE)
        started = time.monotonic()
        proc = run_dispatcher(['--prompt-file', prompt, '--workspace', ws,
                               '--verify-seconds', '2'], env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('no session appeared', proc.stderr)
        self.assertLess(time.monotonic() - started, 30, 'the wait must honour its own deadline')


if __name__ == '__main__':
    unittest.main()
