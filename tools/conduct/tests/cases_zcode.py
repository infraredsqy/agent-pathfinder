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

__all__ = ['TestSpawnerChoice', 'TestQueueDedup']

CONDUCT_DIR = Path(pipeline.__file__).resolve().parent.parent
DISPATCHER = CONDUCT_DIR / 'spawn' / 'zcode-dispatch.py'
SPAWNER = CONDUCT_DIR / 'spawn' / 'zcode-spawn.ps1'


def _host():
    """`spawn/host.py`, loaded by path -- where the host's own answer lives.

    The probe is asked of THIS module: the two hosts, the two spawner paths and the process-tree walk
    sit next to the scripts they name, and the entry keeps one seam name (`SPAWNER_SCRIPT`) out of it.
    """
    spec = importlib_util.spec_from_file_location('pf_spawn_host_case', CONDUCT_DIR / 'spawn' / 'host.py')
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# The two tables the dispatcher touches, as the desktop ships them. Spelled out rather than trimmed:
# a column this row forgets is exactly the failure the DDL is here to catch.
AUTOMATIONS_DDL = """
CREATE TABLE automations (
  automation_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
  cron_expr TEXT NOT NULL, prompt TEXT NOT NULL, model TEXT, provider TEXT,
  mode TEXT, thought_level TEXT, model_selection TEXT,
  workspace_key TEXT NOT NULL, workspace_path TEXT NOT NULL, workspace_identity TEXT,
  target_task_id TEXT, bot_delivery_target TEXT,
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
  PRIMARY KEY (workspace_key, task_id))
"""


class TestSpawnerChoice(unittest.TestCase):
    """两边各一份 spawner，而"用哪一份"只在一处回答。"""

    # ── 选路 ─────────────────────────────────────────────────────────────────

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
        """宿主探测只有 `spawn/host.py` 这一个家，而挑 spawner 的链不必加载整条管线。

        接缝那个模块（`conduct_pipeline.py`）是调用方 patch 的那一个：探测与它分开住，任何一次探测
        的改动才落在自己那份文件里，而不是落在「测试要 patch 接缝」的那份上。`chain-spawn.py` 只要
        「用哪一份 spawner」这一个答案，为它加载整条管线（连实现包）是另一头的代价。
        """
        entry = (CONDUCT_DIR / 'pipeline' / 'conduct_pipeline.py').read_text(encoding='utf-8')
        imported = {alias.name for node in ast.walk(ast.parse(entry))
                    if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
        self.assertNotIn('ctypes', imported, '宿主探测住在接缝模块里')

        chain = (CONDUCT_DIR / 'spawn' / 'chain-spawn.py').read_text(encoding='utf-8')
        self.assertNotIn('conduct_pipeline', chain, '挑 spawner 要加载整条管线')
        self.assertIn("'host.py'", chain)

    # ── 线策略这一级 ─────────────────────────────────────────────────────────
    # `resolve_spawner` 的整条优先级：CLI 点名 > 这条线的 `spawner` 策略 > 环境变量 > 宿主 >
    # `dsh`。线策略是一份落盘的决定（`cycle policy <cycle> spawner …`），所以它压过这台机器
    # 的环境变量与宿主探测 —— 探测偶发失败也不该把一条钉死 zcode 的线送回 DSH。

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
        """线策略压过这台机器上的一切：环境变量与那道缝都说 zcode，线仍然走 dsh。"""
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

    # ── 那一行 ───────────────────────────────────────────────────────────────

    def sandbox(self):
        root = Path(tempfile.mkdtemp(prefix='conduct-zcode-'))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / 'sandbox' / '.zcode' / 'v2').mkdir(parents=True)
        db = root / 'sandbox' / '.zcode' / 'v2' / 'tasks-index.sqlite'
        con = sqlite3.connect(str(db))
        con.execute(AUTOMATIONS_DDL)
        con.execute(TASKS_DDL)
        con.commit()
        con.close()
        ws = root / 'ws'
        (ws / '.pf').mkdir(parents=True)
        prompt = root / 'prompt.txt'
        prompt.write_text('# /pf-conduct drill\n', encoding='utf-8')
        env = {**os.environ, 'ZCODE_DATA_BASE_DIR': str(root / 'sandbox'),
               'DSH_CONDUCT_SPAWN_AT': ''}
        return db, ws, prompt, env

    def run_dispatcher(self, args, env):
        return subprocess.run([sys.executable, '-X', 'utf8', str(DISPATCHER), *map(str, args)],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=env)

    def dispatcher(self):
        """The dispatcher, imported -- for the constants a test should *follow*, not pin.

        The model lives behind a one-word switch (`MODEL_CHOICES` / `DEFAULT_MODEL`); a test that
        hard-codes the active name would turn flipping that switch into a test edit.
        """
        spec = importlib_util.spec_from_file_location('zcode_dispatch', DISPATCHER)
        module = importlib_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_it_writes_one_row_that_means_one_session(self):
        db, ws, prompt, env = self.sandbox()
        proc = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
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
        dispatcher = self.dispatcher()
        self.assertIn(dispatcher.DEFAULT_MODEL, dispatcher.MODEL_CHOICES.values(),
                      'the switch must point at a named choice, not a string typed inline')
        self.assertEqual(row['model_selection'],
                         dispatcher.selection_json(dispatcher.DEFAULT_MODEL))
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
        db, ws, prompt, env = self.sandbox()
        proc = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws,
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
        db, ws, prompt, env = self.sandbox()
        proc = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws,
                                    '--model', 'workspace', '--no-verify'], env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        con = sqlite3.connect(str(db))
        stored = con.execute('select model_selection from automations').fetchone()[0]
        con.close()
        self.assertEqual(stored, 'null')

    def test_an_unreadable_model_is_refused(self):
        """The desktop refuses a column it cannot parse, three steps later and far from here."""
        db, ws, prompt, env = self.sandbox()
        for bad in ('deepseek', 'deepseek/', '/flash', '{"providerId":"x"}',
                    '{"providerId":"a","modelId":"b","extra":1}'):
            proc = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws,
                                        '--model', bad, '--no-verify'], env)
            self.assertNotEqual(proc.returncode, 0, f'{bad!r} should have been refused')
            self.assertIn('--model', proc.stderr)

    def test_the_permission_mode_can_be_turned_down(self):
        db, ws, prompt, env = self.sandbox()
        proc = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws,
                                    '--mode', 'plan', '--no-verify'], env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        con = sqlite3.connect(str(db))
        self.assertEqual(con.execute('select mode from automations').fetchone()[0], 'plan')
        con.close()

    def test_a_prompt_that_is_not_there_is_refused(self):
        db, ws, prompt, env = self.sandbox()
        proc = self.run_dispatcher(['--prompt-file', prompt.parent / 'nope.txt',
                                    '--workspace', ws, '--no-verify'], env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('prompt file not found', proc.stderr)
        con = sqlite3.connect(str(db))
        self.assertEqual(con.execute('select count(*) from automations').fetchone()[0], 0)
        con.close()

    def test_a_time_too_far_past_is_refused(self):
        """The desktop drops a row more than five minutes overdue as a misfire, so refuse it here."""
        db, ws, prompt, env = self.sandbox()
        proc = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws,
                                    '--at', '2020-01-01T00:00', '--no-verify'], env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('misfire', proc.stderr)

    def test_a_time_in_the_future_is_not_waited_for(self):
        db, ws, prompt, env = self.sandbox()
        started = time.monotonic()
        proc = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--at', '+30m'], env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(time.monotonic() - started, 10,
                        'a row scheduled for later must not be waited on now')
        con = sqlite3.connect(str(db))
        when = con.execute('select next_run_at from automations').fetchone()[0]
        con.close()
        self.assertGreater(when - int(time.time() * 1000), 29 * 60_000)

    # ── 包装脚本 ─────────────────────────────────────────────────────────────

    def test_the_spawner_ends_with_its_spawned_line(self):
        """The pipeline reads the spawner's last line; the marker must not be left behind.

        The environment is also how `mode` / `model` reach the spawner: the pipeline calls
        `state-manage.py`, whose argv to a spawner is the same three named parameters for both
        hosts, so anything else the spawner needs travels in the environment.
        """
        if not shutil.which('pwsh'):
            self.skipTest('pwsh is not on PATH')
        db, ws, prompt, env = self.sandbox()
        env = {**env, 'DSH_CONDUCT_SPAWN_MODE': 'yolo',
               'DSH_CONDUCT_SPAWN_MODEL': 'deepseek/deepseek-flash$max'}
        proc = subprocess.run(['pwsh', '-NoProfile', '-File', str(SPAWNER),
                               '-CycleId', 'cyc-case', '-PromptFile', str(prompt),
                               '-WorkspacePath', str(ws), '-NoVerify'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip().splitlines()[-1].startswith('spawned: automation '),
                        proc.stdout)
        self.assertFalse((ws / '.pf' / 'meta' / 'CONDUCT-CALLBACK-FAILED-cyc-case.json').exists())
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
        db, ws, prompt, env = self.sandbox()
        proc = subprocess.run(['pwsh', '-NoProfile', '-File', str(SPAWNER),
                               '-CycleId', 'cyc-case', '-PromptFile', str(prompt.parent / 'nope.txt'),
                               '-WorkspacePath', str(ws), '-NoVerify'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=env)
        self.assertNotEqual(proc.returncode, 0)
        marker = ws / '.pf' / 'meta' / 'CONDUCT-CALLBACK-FAILED-cyc-case.json'
        self.assertTrue(marker.is_file(), 'a failed spawn must leave the marker the pipeline reads')
        record = json.loads(marker.read_text(encoding='utf-8'))
        self.assertEqual(record['cycle_id'], 'cyc-case')
        self.assertEqual(record['phase'], 'read-prompt')
        self.assertEqual(set(record), {'cycle_id', 'phase', 'attempts', 'reason', 'workspace',
                                       'db', 'detected_at', 'note'})


class TestQueueDedup(unittest.TestCase):
    """同一节点排第二行是两个会话上同一节点的路：在途的行、还热的会话都要挡。"""

    def sandbox(self, node_id='dddddddd-4444-4444-8444-dddddddddddd'):
        root = Path(tempfile.mkdtemp(prefix='conduct-zcode-dedup-'))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / 'sandbox' / '.zcode' / 'v2').mkdir(parents=True)
        db = root / 'sandbox' / '.zcode' / 'v2' / 'tasks-index.sqlite'
        con = sqlite3.connect(str(db))
        con.execute(AUTOMATIONS_DDL)
        con.execute(TASKS_DDL)
        con.commit()
        con.close()
        ws = root / 'ws'
        (ws / '.pf').mkdir(parents=True)
        prompt = root / 'launch' / node_id / 'prompt.txt'
        prompt.parent.mkdir(parents=True)
        prompt.write_text('# /pf-conduct drill\n', encoding='utf-8')
        env = {**os.environ, 'ZCODE_DATA_BASE_DIR': str(root / 'sandbox'),
               'DSH_CONDUCT_SPAWN_AT': ''}
        return db, ws, prompt, env

    def run_dispatcher(self, args, env):
        return subprocess.run([sys.executable, '-X', 'utf8', str(DISPATCHER), *map(str, args)],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=env)

    def test_a_second_row_for_the_same_node_is_refused(self):
        db, ws, prompt, env = self.sandbox()
        first = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('already queued', second.stderr)
        con = sqlite3.connect(str(db))
        count = con.execute('select count(*) from automations').fetchone()[0]
        con.close()
        self.assertEqual(count, 1, 'the refused queue must not leave a row behind')

    def test_an_explicit_override_queues_anyway(self):
        db, ws, prompt, env = self.sandbox()
        first = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify',
                                      '--allow-existing'], env)
        self.assertEqual(second.returncode, 0, second.stderr)

    def test_a_claimed_row_is_no_longer_pending(self):
        """行被桌面认领、且认领已过新鲜窗口（会话也没写出来）之后，文档规定的抢救重排是合法的。"""
        db, ws, prompt, env = self.sandbox()
        first = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(first.returncode, 0, first.stderr)
        con = sqlite3.connect(str(db))
        con.execute('update automations set claimed_at=?, run_count=1, dispatch_status=?',
                    (int(time.time() * 1000) - 11 * 60 * 1000, 'dispatched'))
        con.commit()
        con.close()
        again = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(again.returncode, 0, again.stderr)

    def test_a_claim_just_made_with_no_session_blocks(self):
        """认领到落库之间的窄缝：行刚被认领、会话还没写出来，第二个 queue 两个旧检查都落空。"""
        db, ws, prompt, env = self.sandbox()
        first = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(first.returncode, 0, first.stderr)
        con = sqlite3.connect(str(db))
        con.execute('update automations set claimed_at=?, run_count=1, dispatch_status=?',
                    (int(time.time() * 1000), 'dispatched'))
        con.commit()
        con.close()
        second = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('was just claimed', second.stderr)
        con = sqlite3.connect(str(db))
        count = con.execute('select count(*) from automations').fetchone()[0]
        con.close()
        self.assertEqual(count, 1, 'the refused queue must not leave a row behind')

    def test_a_fresh_session_blocks_and_an_old_one_does_not(self):
        """验证超时的竞态：会话其实建出来了——十 分钟内的会话挡住重排，再老的算死会话放行。"""
        db, ws, prompt, env = self.sandbox()
        first = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
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

        blocked = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('already exists', blocked.stderr)

        con = sqlite3.connect(str(db))
        con.execute('update tasks set created_at=? where task_id=?',
                    (now - 11 * 60 * 1000, 'sess_fresh'))
        con.commit()
        con.close()
        allowed = self.run_dispatcher(['--prompt-file', prompt, '--workspace', ws, '--no-verify'], env)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)


if __name__ == '__main__':
    unittest.main()
