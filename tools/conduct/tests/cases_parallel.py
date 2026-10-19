"""`parallel=2` 的机械闸：写盘互斥锁、worktree 回主树、一个节点一次只许一个会话、
一次认领只归一条线。

两条 cycle 线并排跑（`并行：worktree` 模式）时，这几件事是并发的：

  * 四份整文件读-改-写的记录（state / ideas / instructions / issues）被两个进程同时走 ——
    锁不住就互相整份覆盖。这里用**真进程**对撞：四个 `instruct add` 同时起跑，四条都得在。
  * worker 的 cwd 落在 `.pf/worktrees/<短id>` 里，那里有一份 fork 时刻的 `.pf/` 检出 ——
    记录命令认了它就是两条线各记各的。这里用**真 git worktree** 证明：cwd 在 worktree 里，
    结转与图标记落主树，worktree 里那份检出一个字节不动；收尾时不经 handoff.py 直接跑的
    signal.py 同样落主树。
  * 同一节点的派发被双触发（定时抢救撞上在途派发）时，第二个必须被拒 —— 开出两个会话的
    代价没法回收。这里用 SPAWN.claim 的五个面证明：在途拒收、活 pid 不论多旧都拒、
    失败放行、死 pid 的条子按时效判——没到 30 分钟照拒，陈旧了才接管。
  * 两条线在收尾交叠窗口各自 pin 同一节点，会开出双 session 且永远收不了尾——这里证明
    锁内的跨 cycle 守卫把第二个 pin 响亮拒掉，同一条线的重 pin（重启语义）放行。

`parallel` 策略那一半（一条线同时握几格、收尾怎么补格）在 `cases_parallel_slots.py`；
fork-worktree 与它的收尾兜底闸在 `cases_fork.py`（真 git、真 CLI，单独一个文件才压得住
单文件耗时；跑法与注册见那份文件的文件头）。

    python conduct/tests/cases_parallel.py         # 这个文件单独跑
    python conduct/tests/test_conduct_pipeline.py      # 跟全套件一起
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import NODE, pipeline
from harness import (DAG_MANAGE, GIT, SIGNAL_PY, STATE_MANAGE, Sandbox, _child_env, git,
                     init_markers, run_argv)

__all__ = ['TestLedgerLock', 'TestWorktreeRedirect', 'TestSpawnClaim', 'TestOneNodeOneCycle']

PYTHON = sys.executable
CONDUCT_DIR = Path(pipeline.__file__).resolve().parent


class TestLedgerLock(unittest.TestCase):
    """四个并发 `instruction add`：锁住读-改-写，四条全落；没有锁时后写者整份覆盖。"""

    def test_concurrent_instruction_adds_all_survive(self):
        tmp = tempfile.TemporaryDirectory(prefix='conduct-lock-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        # init 的两枚文件：锁只肯落在 init 过的 `.pf/` 里，这四发并发写都要拿它。
        init_markers(root)
        env = _child_env()
        # 四个进程同时起跑：读-改-写的窗口彼此重叠，锁把它们排成队。
        procs = [subprocess.Popen(
            [PYTHON, '-X', 'utf8', str(STATE_MANAGE), 'instruction', 'add',
             '--text', f'并发条目 {n}', '--scope', 'global', '--said-by', 'parallel-test'],
            cwd=str(root), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace')
            for n in range(4)]
        outs = [p.communicate() for p in procs]
        for n, (proc, (out, err)) in enumerate(zip(procs, outs)):
            self.assertEqual(proc.returncode, 0,
                             f'add #{n} failed: {err}')
        data = json.loads((root / '.pf' / 'instructions.json').read_text(encoding='utf-8'))
        texts = [i['text'] for i in data['instructions']]
        self.assertEqual(sorted(texts), sorted(f'并发条目 {n}' for n in range(4)),
                         'a concurrent writer was erased: the lock did not hold')
        ids = [i['id'] for i in data['instructions']]
        self.assertEqual(len(set(ids)), len(ids), 'two entries minted the same id')


@unittest.skipUnless(GIT, 'git is not on PATH')
class TestWorktreeRedirect(unittest.TestCase):
    """cwd 在 linked worktree 里时，记录命令回主树；worktree 里的检出不动。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='conduct-wt-')
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / 'main'
        self.repo.mkdir(parents=True)
        git(self.repo, 'init', '-b', 'main')
        # init 的两枚文件。这个仓库是"提交档"：`.pf/` 跟着一起进库，靠 `.pf/.gitignore`
        # 把 meta/ 与 worktrees/ 挡在外面。
        pf = init_markers(self.repo)
        (pf / 'state.json').write_text(json.dumps(
            {'cycles': [{'id': 'cyc', 'title': 't', 'status': 'active', 'holding': [],
                         'path': []}]}, indent=2), encoding='utf-8')
        (pf / 'ideas.json').write_text(json.dumps(
            {'nodes': {NODE: {'title': '节点', 'status': 'todo', 'mode': 'auto',
                              'dependencies': []}}}, indent=2), encoding='utf-8')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-m', 'seed', '--no-gpg-sign')
        git(self.repo, 'worktree', 'add', '.pf/worktrees/w1', '-b', 'pf/w1')
        self.wt = self.repo / '.pf' / 'worktrees' / 'w1'

    def _run(self, tool, *args):
        return run_argv([tool, *args], cwd=self.wt, check=False)

    def test_a_ledger_write_from_the_worktree_lands_in_the_main_tree(self):
        proc = self._run(STATE_MANAGE, 'cycle', 'hold', 'cyc', NODE)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        state = json.loads((self.repo / '.pf' / 'state.json').read_text(encoding='utf-8'))
        self.assertEqual([h['id'] for h in state['cycles'][0]['holding']], [NODE],
                         'the close landed in the worktree stale copy, not the main tree')
        stale = json.loads((self.wt / '.pf' / 'state.json').read_text(encoding='utf-8'))
        self.assertEqual(stale['cycles'][0]['holding'], [],
                         'the worktree checkout was written to')

    def test_a_dag_write_from_the_worktree_lands_in_the_main_tree(self):
        proc = self._run(DAG_MANAGE, 'status', NODE, 'completed')
        self.assertEqual(proc.returncode, 0, proc.stderr)

        dag = json.loads((self.repo / '.pf' / 'ideas.json').read_text(encoding='utf-8'))
        self.assertEqual(dag['nodes'][NODE]['status'], 'completed')
        stale = json.loads((self.wt / '.pf' / 'ideas.json').read_text(encoding='utf-8'))
        self.assertEqual(stale['nodes'][NODE]['status'], 'todo')

    def test_the_standalone_signal_script_lands_in_the_main_tree(self):
        """worker 在 worktree 里直接跑 signal.py（不经 handoff.py）时，条子也必须落主树。

        handoff.py 自己先解主树再传 base_dir；signal.py 单飞时没人替它解，只能靠它自己对
        worktree 的认知——这里是那份认知的证明。
        """
        proc = run_argv([SIGNAL_PY, 'cyc', NODE, 'completed', '-s', 'worktree 收尾', '-o', '{}'],
                        cwd=self.wt, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        sig = json.loads((self.repo / '.pf' / 'signals' / 'cyc' / f'{NODE}.json')
                         .read_text(encoding='utf-8'))
        self.assertEqual('completed', sig['status'])
        self.assertFalse((self.wt / '.pf' / 'signals' / 'cyc' / f'{NODE}.json').exists(),
                         'the signal landed in the worktree stale copy, not the main tree')


class TestSpawnClaim(unittest.TestCase):
    """SPAWN.claim 的五个面：在途拒收、活 pid 不论多旧都拒、失败放行、死 pid 按时效判——新鲜的拒、陈旧的了断。"""

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.sb.add_node(NODE, '界面设计', mode='auto')
        self.sb.start_cycle()
        self.prompt = (self.sb.root / '.pf' / 'meta' / 'launch' / 'cyc' / NODE / 'prompt.txt')
        self.prompt.parent.mkdir(parents=True, exist_ok=True)
        self.prompt.write_text('/pf-conduct test\n', encoding='utf-8')
        self.claim = self.prompt.parent / 'SPAWN.claim'

    def stub(self, name='ok-spawner.ps1', body=None):
        """A stand-in spawner, never a host one."""
        return self.sb.fake_spawner(name, body=body)

    def _dispatch(self, spawner=None, dry_run=False):
        """One `dispatch cyc`, through the harness's spawn entry, aimed at a stand-in spawner.

        The claim is only taken (and therefore only checked) where a spawn is really attempted:
        `spawn_session` skips it on the drill rail, so every case here runs on the other one -- and
        the only thing that rail can reach is this stub.
        """
        return self.sb.dispatch(spawner=spawner or self.stub(), dry_run=dry_run, check=False)

    def test_an_in_flight_spawn_refuses_a_second_dispatch(self):
        self.claim.write_text('99999 started just-now', encoding='utf-8')
        proc = self._dispatch()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('SPAWN.claim', proc.stderr, 'the refusal must name the claim it tripped on')

    def test_a_live_pid_refuses_the_dispatch_even_when_the_claim_is_stale(self):
        """claim 上的 pid 还活着，派发就可能真的还在飞：不论多旧都不许接管。"""
        self.claim.write_text(f'{os.getpid()} started long-ago', encoding='utf-8')
        old = time.time() - 31 * 60
        os.utime(self.claim, (old, old))
        proc = self._dispatch()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('SPAWN.claim', proc.stderr)
        self.assertTrue(self.claim.exists(), "a live pid's claim must not be taken over")

    def test_a_dead_pid_falls_back_to_the_staleness_rule(self):
        """写 claim 的进程已经死了：接管回归 30 分钟时效——新鲜的拒、陈旧的了断。"""
        finished = subprocess.Popen([PYTHON, '-c', 'pass'])
        finished.wait()
        self.claim.write_text(f'{finished.pid} started long-ago', encoding='utf-8')
        old = time.time() - 31 * 60
        os.utime(self.claim, (old, old))
        proc = self._dispatch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.claim.exists(),
                         'the successful spawn cleans the launch dir, claim included')

    def test_a_dead_pid_with_a_fresh_claim_refuses_the_dispatch(self):
        """死 pid 但条子新鲜：时效没到就还不算陈旧，接管不许提前 —— 派发被拒，条子留在原地。"""
        finished = subprocess.Popen([PYTHON, '-c', 'pass'])
        finished.wait()
        self.claim.write_text(f'{finished.pid} started just-now', encoding='utf-8')
        proc = self._dispatch()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('SPAWN.claim', proc.stderr, 'the refusal must name the claim it tripped on')
        self.assertTrue(self.claim.exists(), 'a refused dispatch must leave the claim alone')

    def test_a_failed_spawn_releases_the_claim(self):
        proc = self._dispatch(spawner=self.stub('failing-spawner.ps1', body='exit 1\n'))
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.claim.exists(),
                         'a failed spawn must leave the prompt free for the manual retry')


class TestOneNodeOneCycle(unittest.TestCase):
    """cycle hold 的跨 cycle 守卫：一个节点一次只归一条线，第二条线 pin 上去要被点名拒掉。"""

    def setUp(self):
        self.sb = Sandbox(cli=True)
        self.addCleanup(self.sb.close)
        self.sb.add_node(NODE, '界面设计', mode='auto')
        self.sb.create_cycle('cyc')
        self.sb.create_cycle('cyc2', force=True)

    def test_a_node_shelved_by_another_cycle_refuses_the_pin(self):
        self.sb.set_current_node(NODE, cycle_id='cyc')
        proc = self.sb.set_current_node(NODE, cycle_id='cyc2', check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(NODE, proc.stderr, 'the refusal must name the node')
        self.assertIn('cyc', proc.stderr, 'the refusal must name the cycle that holds it')
        holders = [c['id'] for c in self.sb.state()['cycles']
                   if NODE in [h['id'] for h in c.get('holding') or []]]
        self.assertEqual(['cyc'], holders, 'the second pin must not have landed')

    def test_repinning_the_same_id_on_its_own_cycle_stays_legal(self):
        """同一条线对同一 id 的重 pin 是重启语义，守卫不许挡它。"""
        self.sb.set_current_node(NODE, cycle_id='cyc')
        proc = self.sb.set_current_node(NODE, cycle_id='cyc', check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == '__main__':
    unittest.main()