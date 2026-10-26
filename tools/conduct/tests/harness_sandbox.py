"""The sandbox: the `Sandbox` throwaway temp project every test drives, and the spawn flag.

Owns the fixture builders for the on-disk world the pipeline reads, and the two ways of
running the tools against it: in-process by default, `cli=True` through the real processes.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import harness_proc as hproc
from harness_env import (DAG_MANAGE, SPAWN_DRY_RUN, STATE_MANAGE, _child_env, _decode,
                         chdir, init_markers, patched, spawn_dry_run)
from harness_fast import FastStateManage, InProcessDagManage, _dag_hook, _signal_module
from harness_proc import _cycle_of, _kw, _json_out

__all__ = ['SPAWN_FLAG', 'Sandbox']

_SANDBOX_SEQ = 0

#: The closing call's own flag to ALSO open the next session. It is spelled here and nowhere else in
#: the suite: a case that wants that behaviour names `Sandbox.spawn_next`, which owns both the flag
#: and the drill switch behind it.
SPAWN_FLAG = '--spawn'

#: The line a stand-in spawner prints: the shape `conduct_spawn.session_id_of` reads an id out of.
_STUB_LINE = 'spawned: session session-stub (node session)'


# ═══════════════════════════════════════════════════════════════════════════════
#  The sandbox
# ═══════════════════════════════════════════════════════════════════════════════

class Sandbox:
    """A temp project with the wiring the pipeline reads: one `.pf/`, and a DAG.

    That one root carries both the account (`state.json`, `signals/`, `instructions.json`,
    `ideas.json`, each node's own directory) and what may be thrown away (`meta/launch/`, each
    node's `temp/`). It also holds the machine's settings layer (`self.home`) for as long as it
    lives, so a case never reads the one outside the sandbox.

    `cli=True` routes every `state-manage.py` verb through the real process. The default
    (in-process) is the same modules and the same files without the process start;
    `test_the_fast_path_and_the_cli_agree` is what keeps that honest.
    """

    def __init__(self, nodes=(), cycle=None, cli=False):
        global _SANDBOX_SEQ
        _SANDBOX_SEQ += 1
        # The prefix must differ between sandboxes in the same process: the in-process
        # state-manage cache is keyed by project path, and a path reused after a cleanup would
        # hand a new sandbox an old module.
        self._tmp = tempfile.TemporaryDirectory(prefix=f'conduct-test-{_SANDBOX_SEQ:03d}-')
        self.root = Path(self._tmp.name)
        self.cli = cli
        # The machine's own settings layer (`~/.pf/config.json`) is pointed inside the sandbox: it is
        # the one thing a run reads from outside the project (a knob's widest layer), and what a case
        # reads must not change with the machine it happens to run on. `PF_HOME_DIR` is the same lever
        # `cases_config` pulls; this file's cases have no setUp of their own. `close()` puts it back.
        self.home = self.root / '.home'
        self._env_home = os.environ.get('PF_HOME_DIR')
        os.environ['PF_HOME_DIR'] = str(self.home)
        # `.pf/` holds the account: state.json + signals/ + instructions.json + ideas.json, and
        # every node's own directory. There is no `loops/`: a cycle carries its own path now.
        ledger = init_markers(self.root)
        (ledger / 'nodes').mkdir()
        self.write_dag({})
        self._fast = FastStateManage(self.root)
        for node in nodes:
            self.add_node(**node) if isinstance(node, dict) else self.add_node(*node)
        if cycle:
            self.create_cycle(**cycle) if isinstance(cycle, dict) else self.create_cycle(*cycle)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def close(self):
        """Drop the throwaway project, and never fail the case for a directory that would not go.

        The one case where a Windows temp directory survives `rmtree` is a child process that has
        not quite let go of a file (the CLI cases start real processes). That is the machine's
        timing talking, so it must not turn a green case red: what a case asserts is the project's
        contents, and the directory is about to be reaped by the OS anyway.
        """
        # The settings home this sandbox claimed (`__init__`) goes back either way: the next case in
        # this worker process reads its own project, not this one's leftovers.
        if self._env_home is None:
            os.environ.pop('PF_HOME_DIR', None)
        else:
            os.environ['PF_HOME_DIR'] = self._env_home
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    # ── test setup only: building the world the pipeline will CONSUME ────────
    # Fixture data: the graph, a node's directory, a forge for hand-written state. Nothing here
    # advances the pipeline -- the cycle pointer and the path only ever move through the tools.
    def write_dag(self, nodes: dict):
        path = self.root / '.pf' / 'ideas.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'nodes': nodes}, ensure_ascii=False, indent=2),
                        encoding='utf-8')

    def dag(self) -> dict:
        return json.loads((self.root / '.pf' / 'ideas.json').read_text(encoding='utf-8'))

    def add_node(self, uid, title, status='todo', mode='auto', deps=(), tags=(), weight=None):
        """One DAG node on disk. `status` is the account's own word -- `todo` by default, which is
        what `dag-manage add` writes."""
        dag = self.dag()
        dag['nodes'][uid] = {'title': title, 'status': status, 'mode': mode,
                             'dependencies': list(deps)}
        if tags:
            # A node with no tag looks like this in the ledger today: the `tags` key does not
            # appear at all.
            dag['nodes'][uid]['tags'] = list(tags)
        if weight:
            # `weight` is the same as `tags`: the key appears only when it was really written.
            # Passing a value outside the vocabulary is what tests the "an unknown word reads as
            # standard" fallback.
            dag['nodes'][uid]['weight'] = weight
        self.write_dag(dag['nodes'])
        node_dir = self.root / '.pf' / 'nodes' / uid
        node_dir.mkdir(parents=True, exist_ok=True)
        (node_dir / 'spec.md').write_text(f'# {title}\n\nspec body\n', encoding='utf-8')
        return uid

    def add_temp(self, uid, name='probe.txt', body='raw output\n'):
        """One file in a node's own `temp/` -- what `clean-temp` sweeps.

        Scratch, so it is NOT in the node's ledger directory: `.pf/nodes/<uid>/temp/`.
        """
        temp = self.root / '.pf' / 'nodes' / uid / 'temp'
        temp.mkdir(parents=True, exist_ok=True)
        (temp / name).write_text(body, encoding='utf-8')
        return temp

    def add_chain(self, count, prefix='node', mode='auto'):
        """`count` nodes in a line: each depends on the one before it."""
        ids, prev = [], None
        for i in range(count):
            uid = f'{prefix}-{i:02d}'
            self.add_node(uid, f'Node {i}', mode=mode, deps=[prev] if prev else ())
            ids.append(uid)
            prev = uid
        return ids

    def dag_status(self, uid):
        return (self.dag()['nodes'].get(uid) or {}).get('status')

    def node(self, uid):
        return self.dag()['nodes'].get(uid)

    def temp_dir(self, uid):
        """Where a node's generated history goes: scratch, and NOT the node's ledger directory."""
        return self.root / '.pf' / 'nodes' / uid / 'temp'

    # ── the real tools ───────────────────────────────────────────────────────
    def _tool(self, *args, check=True, cli=None):
        """Run one state-manage verb.

        `cli=True/False` forces one path; the default is the sandbox's own mode. Tests that are
        ABOUT a verb's output (its refusal message, its stdout) use `cli=True` regardless: the
        in-process path reports a refusal the same way, but only the process is the shipped thing.
        """
        use_cli = self.cli if cli is None else cli
        if use_cli:
            return hproc.run_state_manage(self.root, *args, check=check)
        try:
            # Some verbs reach back into conduct_pipeline (`cycle start` picks the node with the same
            # rule the close uses), which calls dag-manage; hook that too.
            with _dag_hook(self.root):
                self._fast.run([sys.executable, str(STATE_MANAGE), *[str(a) for a in args]])
        except RuntimeError as e:
            if check:
                raise
            return subprocess.CompletedProcess(args, 1, self._fast.stdout, str(e))
        return subprocess.CompletedProcess(args, 0, self._fast.stdout, self._fast.stderr)

    def create_cycle(self, cycle_id='cyc', title='test cycle', force=False, tags='coding'):
        """`cycle create`: a new line, with the filter set every new line must carry.

        `tags` defaults to `coding` here because `cycle create` / `cycle start` now REFUSE a call
        without `--tags` (a line's range is also the set the pick filters by). A case whose subject is
        that refusal calls `_tool('cycle', 'create', …)` itself, with no `--tags`, so the helper's
        default can never hide it.
        """
        args = ['cycle', 'create', cycle_id, '--title', title]
        if tags:
            args += ['--tags', ','.join(tags) if not isinstance(tags, str) else tags]
        if force:
            args += ['--force']
        self._tool(*args)
        return cycle_id

    def start_cycle(self, cycle_id='cyc', title='test cycle', force=False, tags='coding',
                    parallel=None, check=True):
        """`cycle start`: create the cycle AND point it at the node the rule picks.

        `tags` is the cycle's filter set, and it reaches the pick as well as the cycle record. It
        is required by the tool now (see `create_cycle`), so the helper supplies it unless a case
        names its own. The launcher refuses when nothing is ready, so `check=False` returns a
        `CompletedProcess` with the refusal instead of raising.

        `parallel` is how many slots this line starts with: it lands in the cycle's policies and
        decides how many nodes the start pins, exactly like `cycle policy <cycle> parallel <n>`
        does for every close after it.
        """
        if tags and not isinstance(tags, str):
            tags = ','.join(tags)
        return self._tool('cycle', 'start', cycle_id, '--title', title,
                          *(['--tags', tags] if tags else []),
                          *(['--parallel', str(parallel)] if parallel is not None else []),
                          *(['--force'] if force else []), check=check)

    def set_cycle_status(self, cycle_id, status, cli=None, check=True):
        """`cycle status <cycle> <status>`: put a line in or out of service."""
        return self._tool('cycle', 'status', cycle_id, status, cli=cli, check=check)

    def set_policy(self, cycle_id, key, value, cli=None, check=True):
        """`cycle policy <cycle> <key> <value>`: one of the line's own policies."""
        return self._tool('cycle', 'policy', cycle_id, key, value, cli=cli, check=check)

    def set_current_node(self, node_id, cycle_id='cyc', check=True):
        """`cycle hold <cycle> <node>`: what `conduct_pipeline` runs to pin a cycle on a node."""
        return self._tool('cycle', 'hold', cycle_id, node_id, check=check)

    def clean_temp(self, node=None, yes=False, project=None, check=True):
        args = ['clean-temp', '--project', str(project or self.root)]
        if node:
            args += ['--node', node]
        if yes:
            args += ['--yes']
        return hproc.run_state_manage(self.root, *args, check=check)

    def dispatch(self, cycle_id='cyc', project=None, as_json=False, cli=None, check=True,
                 spawner=None, dry_run=True):
        """`dispatch <cycle> [--project <root>] [--json]`: start the session for the node the cycle
        holds.

        A DRILL unless the caller names its own `spawner` stub and says `dry_run=False`: the drill
        switch is set here -- in this process for the in-process rail, in the child's environment for
        the CLI rail -- so no case can reach this host's spawner by forgetting to hold it. A case that
        only wants to check WHICH node would be dispatched reads the action's `prompt` /
        `prompt_text` (`conduct_pipeline.plan`) and never comes through here at all: what this hands
        down is the argv a real `dsh-spawn.ps1` would run, and that opens a session.
        """
        args = ['dispatch', cycle_id]
        if project:
            args += ['--project', str(project)]
        if as_json:
            args += ['--json']
        if spawner is not None:
            args += ['--spawner', str(spawner)]
        if dry_run:
            with spawn_dry_run():
                return self._tool(*args, cli=cli, check=check)
        return self._spawn_proc(args, spawner=spawner, dry_run=dry_run, check=check)

    def instruct(self, text, scope=None, target=None, **extra):
        """One `instruction add`. The three scopes are global / cycle / node.

        `stop_after_nodes` and `note` come through `extra`; `cycle` / `node` name the binding by
        hand, which is what a session does while two lines run side by side.
        """
        args = ['instruction', 'add', '--text', text]
        if scope:
            args += ['--scope', scope]
        if target:
            args += ['--target', target]
        for key, value in extra.items():
            args += ['--' + key.replace('_', '-'), str(value)]
        return self._tool(*args)

    def instruct_list(self, live_only=False):
        """The account, read straight off disk: the file IS the account (one entry per id)."""
        items = _decode(self.root / '.pf' / 'instructions.json') or {}
        items = items.get('instructions', [])
        if live_only:
            items = [i for i in items if i.get('state', 'live') == 'live']
        return items

    def instruct_list_cli(self, live_only=False):
        """The raw account as the CLI sees it: `--all` asks for the rows, not the reading."""
        args = ['instruction', 'list', '--json', '--all'] + (['--live'] if live_only else [])
        proc = hproc.run_state_manage(self.root, *args)
        return json.loads(proc.stdout)

    def instruct_visible_cli(self, node=None, cycle=None):
        """`instruction list --json`: what a session standing here now would read."""
        args = ['instruction', 'list', '--json']
        if node:
            args += ['--node', str(node)]
        if cycle:
            args += ['--cycle', str(cycle)]
        proc = hproc.run_state_manage(self.root, *args)
        return json.loads(proc.stdout)

    def instruct_resolve(self, ins_id, state, check=True, **extra):
        args = ['instruction', 'resolve', ins_id, '--state', state]
        for key, value in extra.items():
            args += ['--' + key.replace('_', '-'), str(value)]
        return self._tool(*args, check=check)

    def instruct_rm(self, ins_id, check=True, cli=None, **extra):
        """`instruction rm`: the owner takes one entry off the account, text and all."""
        args = ['instruction', 'rm', ins_id]
        for key, value in extra.items():
            args += ['--' + key.replace('_', '-'), str(value)]
        return self._tool(*args, check=check, cli=cli)

    def instruct_revive(self, ins_id, check=True, cli=None, **extra):
        """`instruction revive`: the owner puts an archived entry back in force."""
        args = ['instruction', 'revive', ins_id]
        for key, value in extra.items():
            args += ['--' + key.replace('_', '-'), str(value)]
        return self._tool(*args, check=check, cli=cli)

    # ── a worker's report ────────────────────────────────────────────────────
    def signal_path(self, cycle_id, node_id):
        return self.root / '.pf' / 'signals' / cycle_id / f'{node_id}.json'

    def write_signal(self, cycle_id, node_id, status, summary='', outputs=None):
        """A worker's report, through the real signal.py in-process: it is a pure file writer
        whose contract is the file it leaves behind, and handoff.py calls this very function."""
        mod = _signal_module()
        return mod.write_signal(cycle_id, node_id, status, summary, outputs or {},
                                base_dir=self.root / '.pf' / 'signals')

    def handoff(self, cycle_id, node_id, status='completed', summary='', outputs=None,
                dry_run=True, check=True):
        """The worker's ONE command, as a real process: write the ticket, spawn nothing."""
        return hproc.run_handoff(self.root, cycle_id, node_id, status, summary, outputs,
                                 dry_run, check)

    def read_signal(self, cycle_id, node_id):
        return _decode(self.signal_path(cycle_id, node_id))

    def write_raw_signal(self, cycle_id, node_id, status='completed', summary='forged',
                         payload=None):
        """One signal file written straight to disk, for forging what the tools will not produce
        (a malformed payload, a hand-edited file). Fixture data, never a pipeline step."""
        path = self.signal_path(cycle_id, node_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if payload is None:
            payload = {'cycle': cycle_id, 'node': node_id, 'status': status, 'summary': summary,
                       'outputs': {}, 'timestamp': '2026-01-01T00:00:00Z'}
        path.write_text(payload if isinstance(payload, str)
                        else json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return path

    # ── the planner / writer ─────────────────────────────────────────────────
    def next(self, *extra, plan=False):
        """The tool's planner+writer. Returns the action (never carrying commands)."""
        return self._pipeline_call(self.root, 'plan' if plan else 'record', *extra)

    def next_cli(self, *args, plan=False, check=True):
        """`next` through the real CLI process. Returns (action, completed_process).

        For the closing call that ALSO opens the next session, use `spawn_next` -- the flag that asks
        for a spawn is spelled there and nowhere else.
        """
        argv = ['next', '--json'] + (['--plan'] if plan else []) + [str(a) for a in args]
        proc = hproc.run_state_manage(self.root, *argv, check=False, env=_child_env())
        if check and proc.returncode != 0:
            raise AssertionError(f'next {argv} failed (rc={proc.returncode}):\n'
                                 f'{proc.stdout}\n{proc.stderr}')
        return (_json_out(proc, argv) if proc.stdout.strip() else None), proc

    # ── asking for a session ─────────────────────────────────────────────────
    def fake_spawner(self, name='spawner.ps1', line=None, body=None):
        """A stand-in spawner script: by default it prints the contract line and opens nothing.

        `line` states a different contract line (a case that reads a session id back out of it);
        `body` replaces the script outright, for the stand-ins whose subject is what the spawner does
        (a failing one). Never a host spawner: what a case does with this path is hand it to
        `--spawner`, on the rail that has taken the drill switch back out.
        """
        script = body if body is not None else 'Write-Output "%s"\n' % (line or _STUB_LINE)
        path = self.root / name
        path.write_text(script, encoding='utf-8')
        return path

    def spawn_next(self, *args, spawner=None, dry_run=True, check=True):
        """`next ... --spawn`: the closing call that also opens the next session.

        The suite's ONE spelling of that flag, and of "does this call really spawn". Returns
        (action, completed_process) like `next_cli`. The spawn is a drill unless the caller names its
        own `spawner` stub and says `dry_run=False`; `dry_run=False` without a stub is refused -- a
        spawn aimed at the host's real script is the one thing these cases must never do.
        """
        argv, env = self._spawn_argv(['next', '--json', *[str(a) for a in args], SPAWN_FLAG],
                                    spawner, dry_run)
        proc = hproc.run_state_manage(self.root, *argv, check=False, env=env)
        if check and proc.returncode != 0:
            raise AssertionError(f'next {argv} failed (rc={proc.returncode}):\n'
                                 f'{proc.stdout}\n{proc.stderr}')
        return (_json_out(proc, argv) if proc.stdout.strip() else None), proc

    def _spawn_proc(self, argv, spawner=None, dry_run=True, check=True):
        """One CLI command line that may start a session, on the caller's own rail."""
        argv, env = self._spawn_argv(argv, spawner, dry_run)
        proc = hproc.run_state_manage(self.root, *argv, check=False, env=env)
        if check and proc.returncode != 0:
            raise AssertionError(f'{" ".join(argv)} failed (rc={proc.returncode}):\n'
                                 f'{proc.stdout}\n{proc.stderr}')
        return proc

    def _spawn_argv(self, argv, spawner, dry_run):
        """One spawn call's argv and child environment -- the suite's only decision about either.

        `--spawner` goes in when a caller named a stub. The drill switch comes out only for a run
        aimed at that stub: `dry_run=False` with no stub would reach this host's real spawner, so it
        is refused outright rather than trusted.
        """
        if spawner is not None:
            argv = [*argv, '--spawner', str(spawner)]
        env = _child_env()
        if not dry_run:
            assert spawner is not None, (
                'a spawn with no drill and no stand-in spawner would reach this host\'s real one')
            env.pop(SPAWN_DRY_RUN, None)
        return argv, env

    def plan_for(self, cycle_id, *extra):
        """`plan` aimed at a named cycle (the default path uses the single active one)."""
        return self._pipeline_call(self.root, 'plan', '_cycle', cycle_id, *extra)

    def plan(self, *extra):
        return self.next(*extra, plan=True)

    def close_round(self, node_id, decision=None, node=None, note='', cycle_id=None):
        """The worker's own closing call: `next --after <node_id> [--continue|--stop] ...`.

        This is what CLOSES a node, and it needs no verdict: the outcome is read off the signal the
        worker already wrote. What the closing call still decides is whether to go on, which node
        to aim at, and why.
        """
        return self.close_node_call(node_id, decision=decision, node=node, note=note,
                                    cycle_id=cycle_id)

    def close_node_call(self, node_id, decision=None, node=None, note='', cycle_id=None):
        args = []
        if decision:
            args.append('--' + decision)
        if node:
            args += ['--node', node]
        if note:
            args += ['--note', note]
        if cycle_id:
            args += ['--cycle', cycle_id]
        if self.cli:
            proc = hproc.run_state_manage(self.root, 'next', '--json', '--after', node_id, *args)
            return json.loads(proc.stdout)
        # The CLI resolves the line from the NODE (`resolve_cycle_for_node`) and checks the name
        # against what that line carries, all BEFORE anything is written. There is no argv on this
        # path, so both rules are applied here: a test that names the wrong node has to fail, never
        # close another node's work.
        holder = cycle_id or next((c['id'] for c in (self.state() or {'cycles': []})['cycles']
                                   if node_id in self._holding_ids_of(c)), None)
        here = self._holding_ids_of(self.cycle(holder)) if holder else []
        assert node_id in here, (
            f'--after {node_id} does not name a node the cycle holds ({here})')
        extra = list(args) + (['_cycle', holder] if holder else []) + ['_after', node_id]
        return self._pipeline_call(self.root, 'after', *extra)

    def _run_tool_command(self, cmd, cwd=None):
        """Run one of conduct_pipeline's command lists.

        Those lists mix the two tools: `dag-manage status <node> completed` (the close marks the
        node) and `state-manage.py <verb> ...` (everything else the pipeline records). Both are
        dispatched here, through the tools' own functions -- the commands and their order are the
        real ones, only the two process starts are gone.
        """
        argv = [str(c) for c in cmd]
        if len(argv) > 1 and Path(argv[1]).name == 'dag-manage.py':
            assert Path(argv[1]).resolve() == Path(DAG_MANAGE).resolve(), \
                f'the tool aimed dag-manage at {argv[1]}, harness resolved {DAG_MANAGE}'
            proc = InProcessDagManage.handle(self.root, argv)
            if proc.returncode:
                raise RuntimeError(f'dag-manage {" ".join(argv[2:])} failed: {proc.stderr}')
            return proc.returncode
        return self._fast.run(argv, cwd)

    # ── the in-process call into conduct_pipeline.py ─────────────────────────────
    def _pipeline_call(self, project, mode, *extra):
        """Call the real module's plan/apply/record with the sandbox's state-manage hooked in.

        `after` is the worker's closing call, in process: the same
        `apply(..., self_verified=True)` the CLI reaches through `next --after`.
        """
        with _dag_hook(self.root) as pipeline:
            pipeline.DAG_MANAGE = DAG_MANAGE
            # Installed only for this call and put back afterwards: `load_pipeline()` is shared with
            # the tests that drive `pipeline._run` on purpose, and a leftover substitution would aim
            # those at the wrong sandbox.
            with patched(pipeline, '_run', self._run_tool_command), chdir(self.root):
                if mode == 'plan':
                    return pipeline.plan(self.root, _cycle_of(extra), **_kw(extra))
                if mode == 'record':
                    return pipeline.record(self.root, _cycle_of(extra))
                if mode == 'after':
                    return pipeline.apply(self.root, _cycle_of(extra), self_verified=True,
                                          **_kw(extra))
        raise AssertionError(mode)

    # ── readers ──────────────────────────────────────────────────────────────
    def state(self):
        return _decode(self.root / '.pf' / 'state.json')

    def cycle(self, cycle_id='cyc'):
        return [c for c in self.state()['cycles'] if c['id'] == cycle_id][0]

    @staticmethod
    def _holding_ids_of(cycle):
        """The node ids this cycle holds, in claim order (the `holding` list's one reading)."""
        holding = cycle.get('holding')
        if isinstance(holding, list):
            return [item['id'] if isinstance(item, dict) else item for item in holding]
        return []

    def holding_ids(self, cycle_id='cyc'):
        """Every node the cycle holds right now, in claim order."""
        return self._holding_ids_of(self.cycle(cycle_id))

    def current_node(self, cycle_id='cyc'):
        """The first node the cycle holds, or None (the singular view of `holding`)."""
        holding = self.holding_ids(cycle_id)
        return holding[0] if holding else None

    def path(self, cycle_id='cyc'):
        """The nodes this cycle has finished, in order."""
        return self.cycle(cycle_id).get('path') or []

    def path_nodes(self, cycle_id='cyc'):
        return [step.get('node') for step in self.path(cycle_id)]

    def read_instructions(self):
        return _decode(self.root / '.pf' / 'instructions.json') or {'instructions': []}

    def prompt_of(self, action):
        return Path(action['prompt']).read_text(encoding='utf-8')

    def state_sig(self, cycle_id='cyc'):
        """The real project state an action is allowed to move.

        Read straight off disk so a test can check movement without trusting the `progress` block
        the module reports about itself. Call it BEFORE the action too: the module's own contract
        check compares its pre-command snapshot with the post-command one, and that pre-command
        state is not reachable any more once the command has run.
        """
        cycle = self.cycle(cycle_id)
        node = self.current_node(cycle_id)
        return (node, len(cycle.get('path') or []),
                (self.read_signal(cycle_id, node) or {}).get('status') if node else None,
                cycle.get('status'))

    def files(self):
        """Every file under the project, keyed by relative path."""
        return {str(p.relative_to(self.root)).replace('\\', '/'): p
                for p in sorted(self.root.rglob('*')) if p.is_file()}

    def tree_hashes(self):
        """sha256 of every file under the project.

        The honest way to check "plan writes nothing": compare content, not just the file list.
        """
        return {rel: hashlib.sha256(p.read_bytes()).hexdigest()
                for rel, p in self.files().items()}

    def tree_contents(self):
        """Every file's bytes, for "the two paths produced the same project" comparisons."""
        return {rel: p.read_bytes() for rel, p in self.files().items()}

    # ── forge on-disk state the tools refuse to produce ──────────────────────
    def rewrite_cycle(self, **fields):
        """Write fields onto a cycle record by hand.

        One raw writer, for forging a cycle the tools will not write (a hand-edited path, a status
        the levers do not reach). Never used to advance the pipeline.
        """
        state = self.state()
        for cycle in state['cycles']:
            if cycle['id'] == fields.pop('id', 'cyc'):
                cycle.update(fields)
                break
        self.rewrite_state(state)

    def rewrite_state(self, state):
        (self.root / '.pf' / 'state.json').write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
