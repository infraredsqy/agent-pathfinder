"""parallel=2 的四道机械闸：写盘互斥锁、worktree 回主树、一个节点一次只许一个会话、
一次认领只归一条线。

两条 cycle 线并排跑（`并行：worktree` 模式）时，四件事是并发的：

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
    锁内的跨 cycle 守卫把第二个 pin 响亮拒掉，同一条线的重 pin（重启语义）照旧放行。

fork-worktree 与它的收尾兜底闸在 `cases_fork.py`（真 git、真 CLI，单独一个文件才压得住
单文件耗时；跑法与注册见那份文件的文件头）。

每个类自成一组，单独跑一个文件约 5 s，和全套件并排跑（跑法见 test_conduct_pipeline.py 的文件头）。

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

from cases_base import NODE, NODE2, NODE3, Base, pipeline
from harness import (DAG_MANAGE, GIT, SIGNAL_PY, STATE_MANAGE, Sandbox, _child_env, git,
                     init_markers, run_argv)

__all__ = ['TestLedgerLock', 'TestWorktreeRedirect', 'TestSpawnClaim', 'TestOneNodeOneCycle',
           'TestParallelSlots']

PYTHON = sys.executable
CONDUCT_DIR = Path(pipeline.__file__).resolve().parent.parent


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


class TestParallelSlots(Base):
    """一条线的 `parallel` 策略：它同时握几个节点，收尾腾出来的格子当场补上。"""

    def two_ready(self, parallel=None):
        """两个互不依赖的就绪节点：`parallel` 决定开局时同时握几个。"""
        sb = self.make()
        sb.add_node(NODE, '甲', mode='auto')
        sb.add_node(NODE2, '乙', mode='auto')
        sb.start_cycle(tags=['coding'])
        if parallel is not None:
            sb.set_policy('cyc', 'parallel', parallel)
        return sb

    def test_the_default_is_one_slot(self):
        """没配 `parallel` 就是 1：抽签按旧样子一次一格，多出来的格子不自己开。"""
        sb = self.two_ready()
        cycle = sb.cycle()
        self.assertEqual(cycle.get('policies', {}).get('parallel', 1), 1)
        self.assertEqual(len(sb.holding_ids()), 1, 'the default must pin exactly one node')

    def test_the_start_pins_the_slots_the_line_states(self):
        """开局就说清这条线握几格：`cycle start --parallel 2` 当场握上两格，一次派发两份 prompt。

        同一个数落在 `policies.parallel` 上（`cycle policy` 写的是那一格），所以开局这一次挑到
        几格、以后每一次收尾补几格取的是同一个数。
        """
        sb = self.make()
        sb.add_node(NODE, '甲', mode='auto')
        sb.add_node(NODE2, '乙', mode='auto')
        sb.start_cycle(tags=['coding'], parallel=2)

        self.assertEqual(sb.cycle()['policies']['parallel'], 2)
        self.assertEqual(sorted(sb.holding_ids()), sorted([NODE, NODE2]), '开局该握上两格')

        action = sb.next()

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(sorted(n['node_id'] for n in action['nodes']), sorted([NODE, NODE2]))
        for item in action['nodes']:
            self.assertTrue(Path(item['prompt']).is_file(), item['prompt'])

    def test_the_start_refuses_a_parallel_that_is_not_a_positive_integer(self):
        """开局给的那个数按 `cycle policy` 的同一个口径判：0 当场被拒，`off` 落回缺省。"""
        sb = self.make()
        sb.add_node(NODE, '甲', mode='auto')
        sb.add_node(NODE2, '乙', mode='auto')

        refused = sb.start_cycle(tags=['coding'], parallel=0, check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('parallel', refused.stderr)
        self.assertEqual([], [c['id'] for c in (sb.state() or {'cycles': []})['cycles']],
                         '被拒的开局不该留下一条线')

        sb.start_cycle(tags=['coding'], parallel='off')
        self.assertEqual(len(sb.holding_ids()), 1, '`off` 落回缺省：一次一格')
        self.assertNotIn('parallel', sb.cycle().get('policies') or {})

    def test_parallel_two_pins_two_and_dispatches_one_action_for_both(self):
        """`parallel=2`：开局把两个就绪节点都握上，一次派发同时开出两份 prompt。"""
        sb = self.two_ready(parallel=2)
        # the policy lands after the start: pin the second slot explicitly
        sb.set_current_node(NODE2)
        action = sb.next()
        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(sorted(n['node_id'] for n in action['nodes']), sorted([NODE, NODE2]))
        for item in action['nodes']:
            self.assertTrue(Path(item['prompt']).is_file(), item['prompt'])
            self.assertTrue(item['prompt_text'])
        self.assertEqual(sorted(sb.holding_ids()), sorted([NODE, NODE2]))

    def test_a_close_fills_the_slot_it_freed(self):
        """手里两格、关掉一格：填充把下一格就绪的补上 —— 线自己接着跑，不留空位。"""
        sb = self.make()
        sb.add_node(NODE, '甲', mode='auto')
        sb.add_node(NODE2, '乙', mode='auto')
        sb.add_node(NODE3, '丙', mode='auto')
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'parallel', 2)
        sb.set_current_node(NODE2)
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done')

        first = sb.close_round(NODE)

        # 关掉一格腾出空位，而图里还有就绪的活：同一次收尾把它补上并派发 —— 手里重新是满的，
        # 派发那一格带的是「现在握着的所有节点」（刚补上的那个在 `nodes` 里）。
        self.assertEqual(first['action'], 'dispatch')
        self.assertEqual(first['nodes'][0]['node_id'], NODE2,
                         'the dispatch names every node in hand, not only the fresh one')
        self.assertEqual(sorted(sb.holding_ids()), sorted([NODE2, NODE3]),
                         'the slot the close freed must be refilled')

    def test_a_close_fills_every_free_slot(self):
        """手里一格、关掉它时图里有两个就绪：三个空位该补两个 —— 两个都得落盘。

        空位是 `parallel` 减手里还握着的；名单取到几个就 pin 几个。只 pin 名单头一个的话，
        第二个格会静静地空着：动作里的 `nodes` 报了它，手里的手却没有它。
        """
        sb = self.make()
        for uid, name in ((NODE, '甲'), (NODE2, '乙'), (NODE3, '丙')):
            sb.add_node(uid, name, mode='auto')
        sb.start_cycle(tags=['coding'])           # 开局按缺省 parallel=1 只钉一个
        sb.set_policy('cyc', 'parallel', 3)
        closing = sb.current_node()
        rest = sorted({NODE, NODE2, NODE3} - {closing})
        sb.write_signal('cyc', closing, 'completed', 'done')

        action = sb.close_round(closing)

        self.assertEqual(sorted(sb.holding_ids()), rest,
                         'every freed slot must be refilled, not only the head of the fill')
        self.assertEqual(sorted(n['node_id'] for n in action['nodes']), rest,
                         'the dispatch names every node in hand')
        for item in action['nodes']:
            self.assertTrue(Path(item['prompt']).is_file(), item['prompt'])

    def test_a_close_fills_the_free_slots_and_no_more(self):
        """手里满三格、关掉一格：只补腾出来的那一格，线不会越过 `parallel`。

        补格不是无脑补三个 —— 空位跟手里还在跑的格数挂钩（`parallel` 减还握着的），
        图里给得出几个就补几个，给不出就不补。
        """
        sb = self.make()
        four = (NODE, NODE2, NODE3, 'dddddddd-4444-4444-8444-dddddddddddd')
        for i, uid in enumerate(four):
            sb.add_node(uid, f'节点 {i}', mode='auto')
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'parallel', 3)
        closing = sb.current_node()
        rest = [u for u in four if u != closing]
        sb.set_current_node(rest[0])
        sb.set_current_node(rest[1])              # 手里三格（满）
        sb.write_signal('cyc', closing, 'completed', 'done')

        action = sb.close_round(closing)

        self.assertEqual(len(sb.holding_ids()), 3, 'the hand stays at the cap')
        self.assertEqual(sorted(sb.holding_ids()), sorted(rest),
                         'only the freed slot is filled, and it is filled')
        self.assertEqual(sorted(n['node_id'] for n in action['nodes']), sorted(rest))

    def test_a_close_with_nothing_to_fill_keeps_the_line_in_service(self):
        """手里两格、图里没有能补的：收掉这一格，线留在服务中 —— 那一格的收尾自己接着跑。

        补不上有两种：图里真没活了（线停手等人），和线里还有别的节点在飞（这条线没停，接力棒在
        那些节点自己的收尾上）。后者也 park 的话，它们的收尾只做登记，腾出来的活再没人接。
        """
        sb = self.make()
        sb.add_node(NODE, '甲', mode='auto')
        sb.add_node(NODE2, '乙', mode='auto')
        sb.add_node(NODE3, '丙', mode='auto', deps=[NODE2])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'parallel', 2)
        sb.set_current_node(NODE)
        sb.set_current_node(NODE2)
        sb.write_signal('cyc', NODE, 'completed', 'done')

        first = sb.close_round(NODE)

        self.assertEqual(first['action'], 'stop_pipeline')
        self.assertEqual(sb.cycle()['status'], 'active', '手里还有别的节点在飞，这一格不停这条线')
        self.assertEqual(sb.holding_ids(), [NODE2])
        self.assertIn(NODE2[:8], first['reason'], 'reason 点名手里还有谁')

        sb.write_signal('cyc', NODE2, 'completed', 'done')
        second = sb.close_round(NODE2)

        self.assertEqual(second['action'], 'dispatch')
        self.assertEqual(sb.holding_ids(), [NODE3], '那一格的收尾把丙补上并派发')

    def stub(self, sb):
        """The stand-in spawner these cases aim the real spawn call at (never a host one)."""
        return sb.fake_spawner('ok-spawner.ps1')

    def test_a_started_node_is_never_handed_to_the_spawner_twice(self):
        """已经开出去的那一格不再交给 spawner：收尾重规划出来的整份名单里带着它，跳过它。

        `started` 是成功派发留下的记号（prompt 被会话拿走、claim 撤掉、目录留着当落点）。没有
        它，一次补格就会把在跑的兄弟节点再开一个会话 —— 同一个节点、同一个工作树，收不回来。
        """
        sb = self.make()
        for uid, name in ((NODE, '甲'), (NODE2, '乙'), (NODE3, '丙')):
            sb.add_node(uid, name, mode='auto')
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'parallel', 2)
        sb.set_current_node(NODE)
        sb.set_current_node(NODE2)
        sb.next()
        launch = sb.root / '.pf' / 'meta' / 'launch' / 'cyc'

        _opened, opened = sb.spawn_next('--cycle', 'cyc', spawner=self.stub(sb), dry_run=False)
        self.assertEqual(opened.returncode, 0, opened.stderr)
        for node in (NODE, NODE2):
            self.assertTrue((launch / node / 'started').is_file(), f'{node[:8]}: 记号没落下')
            self.assertFalse((launch / node / 'prompt.txt').exists(), f'{node[:8]}: prompt 该被拿走')

        sb.write_signal('cyc', NODE, 'completed', 'done')
        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual([n['node_id'] for n in action['nodes']], [NODE3],
                         '派发名单里只有在等开工的那一格')
        self.assertFalse((launch / NODE2 / 'prompt.txt').exists(),
                         '在跑的乙，prompt 不该被补回来 —— 补回来就等于说它还没开')

        action, _ = sb.spawn_next('--cycle', 'cyc', spawner=self.stub(sb), dry_run=False)
        self.assertEqual([n['node_id'] for n in action['nodes']], [NODE3], '在跑的乙不进名单')

    def test_with_every_session_in_hand_running_the_answer_is_a_wait(self):
        """手里每一格都开出去了：`plan` 答 `wait`，派发则响亮拒掉（并把重开那一格的路念出来）。"""
        sb = self.make()
        sb.add_node(NODE, '甲', mode='auto')
        sb.start_cycle(tags=['coding'])
        sb.next()
        self.assertEqual(
            sb.spawn_next('--cycle', 'cyc', spawner=self.stub(sb), dry_run=False)[1].returncode, 0)

        action = sb.plan()

        self.assertEqual(action['action'], 'wait', '会话都在跑，没有可派的动作')
        self.assertIn('条子', action['note'])

        proc = sb.dispatch(spawner=self.stub(sb), dry_run=False, check=False)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('cycle retry', proc.stderr, '拒绝里要给重开那一格的路')

    def test_the_limit_is_the_line_own_policy(self):
        """上限写在线自己身上：`cycle policy cyc parallel 2` 生效，0 与负数被拒。"""
        sb = self.two_ready()
        sb.set_policy('cyc', 'parallel', 2)
        self.assertEqual(sb.cycle()['policies']['parallel'], 2)
        refused = sb.set_policy('cyc', 'parallel', 0, check=False)
        self.assertNotEqual(refused.returncode, 0)

    def test_the_dispatch_sweep_opens_one_session_per_node_it_holds(self):
        """`dispatch <cycle>` 是一条扫射：手里每个节点各开一个会话，各自吃自己的 prompt。"""
        sb = self.two_ready(parallel=2)
        sb.set_current_node(NODE2)
        sb.next()
        _action, opened = sb.spawn_next(spawner=self.stub(sb), dry_run=False)
        self.assertEqual(opened.returncode, 0, opened.stderr)
        for node in (NODE, NODE2):
            self.assertFalse((sb.root / '.pf' / 'meta' / 'launch' / 'cyc' / node
                              / 'SPAWN.claim').exists(),
                             f'{node}: a started session must drop its launch dir')


if __name__ == '__main__':
    unittest.main()
