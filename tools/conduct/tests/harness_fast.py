"""The in-process equivalences of the two helper programs the sandbox drives.

`FastStateManage` runs `state-manage.py`'s own `cmd_*` functions against one sandbox instead of
starting a process; `InProcessDagManage` / `_dag_hook` answer dag-manage in-process, and
`_signal_module` loads `signal.py` by path. Only the process start is saved, not the logic.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import threading
from pathlib import Path

from harness_env import DAG_MANAGE, SIGNAL_PY, STATE_MANAGE, _fresh_module, chdir, load_pipeline


__all__ = [
    '_FAST_SM_CACHE', '_PIPELINE_CALL_LOCK', 'FastStateManage', 'InProcessDagManage', '_FakeProc',
    '_dag_hook', '_SIGNAL_MODULE', '_signal_module',
]


# ═══════════════════════════════════════════════════════════════════════════════
#  In-process equivalences of the two helper programs
# ═══════════════════════════════════════════════════════════════════════════════
# Neither of these is a reimplementation: the first calls state-manage.py's own command
# functions, the second calls dag-manage.py's own list command. What is saved is the process
# start, not the logic. `test_the_fast_path_and_the_cli_agree` pins the equivalence, and
# `test_the_harness_restates_the_real_parser` pins the argv shape each command is parsed with.

_FAST_SM_CACHE = {}
_PIPELINE_CALL_LOCK = threading.RLock()

#: Destinations the real CLI declares `type=int`. argparse hands a `str` through for anything else,
#: and the maintenance thresholds are compared as numbers, so a string through here would be a
#: comparison of the wrong type rather than a visible failure.
_INT_FLAGS = ('stop_after_nodes',)


class FastStateManage:
    """state-manage.py's CLI surface, executed in-process against one sandbox.

    conduct_pipeline shells out to `state-manage.py <verb> ...` for every state change -- that is how
    the shipped tool works, and the command lists it builds are exactly what this runs. The parsed
    namespace goes to the module's own `route` (the entry's one dispatch table), so the logic, the
    files and the exit behaviour are the real ones; only the process start is gone.

    `_SPEC` and the two groups' nested tables are the one thing restated from the CLI: which flag is
    called what, which positional, and each default. `TestContract` pins that restatement against
    the real argparse tree item by item (`test_the_harness_restates_the_real_parser`), and
    `test_the_fast_path_and_the_cli_agree` drives the same round both ways and compares the projects.
    """

    #: argv[0] is the interpreter and argv[1] the script (both are skipped; conduct_pipeline builds
    #: them with sys.executable and the module's own absolute path). The two noun groups (`cycle`,
    #: `instruction`) are entries here and carry their own sub-verb tables below.
    _VERBS = ('cycle', 'instruction', 'next', 'dispatch', 'clean-temp')

    # verb -> positional names, then per-flag (name, takes_value, default).
    _SPEC = {
        'clean-temp': ((), {'--project': ('project', True, None), '--node': ('node', True, None),
                            '--yes': ('yes', False, False), '--force': ('force', False, False)}),
        'dispatch': (('id',), {'--project': ('project', True, None),
                               '--json': ('json', False, False),
                               '--spawner': ('spawner', True, None)}),
        'next': ((), {'--cycle': ('cycle', True, None), '--project': ('project', True, None),
                      '--plan': ('plan', False, False),
                      '--json': ('json', False, False),
                      '--continue': ('cont', False, False), '--stop': ('stop', False, False),
                      '--node': ('node', True, None), '--note': ('note', True, ''),
                      '--after': ('after', True, None),
                      '--spawn': ('spawn', False, False),
                      '--spawner': ('spawner', True, None)}),
    }
    #: `pf cycle <verb>`: the group's eleven verbs, in the same shape as `_SPEC`. The entry's `route`
    #: is what answers them (keyed off `ns.cycle_command`), so this table and `_CYCLE_CALLS` are one
    #: list of the group's surface: the first is what the argv is parsed with, the second names the
    #: `cmd_*` each verb reaches.
    _CYCLE_SPEC = {
        'create': (('id',), {'--title': ('title', True, None),
                             '--force': ('force', False, False),
                             '--tags': ('tags', True, None)}),
        'start': (('id',), {'--title': ('title', True, None),
                            '--force': ('force', False, False),
                            '--tags': ('tags', True, None),
                            '--parallel': ('parallel', True, None)}),
        'rename': (('id',), {'--title': ('title', True, None)}),
        'status': (('id', 'status'), {}),
        'policy': (('id', 'key', 'value'), {}),
        'list': ((), {}),
        'current': ((), {}),
        'show': (('id', 'node'), {}),
        'hold': (('id', 'node'), {}),
        'close': (('id', 'node'), {'--result': ('result', True, None)}),
        'retry': (('id', 'node'), {'--summary': ('summary', True, None),
                                   '--drop': ('drop', False, False)}),
    }
    #: `pf cycle <verb>` -> the `cmd_*` the group's own dispatch calls for it.
    _CYCLE_CALLS = {
        'create': lambda mod, ns: mod.cmd_cycle_create(ns.id, ns.title, ns.force, ns.tags),
        'start': lambda mod, ns: mod.cmd_cycle_start(ns.id, ns.title, ns.force, ns.tags,
                                                     ns.parallel),
        'rename': lambda mod, ns: mod.cmd_cycle_rename(ns.id, ns.title),
        'status': lambda mod, ns: mod.cmd_cycle_status(ns.id, ns.status),
        'policy': lambda mod, ns: mod.cmd_cycle_policy(ns.id, ns.key, ns.value),
        'list': lambda mod, ns: mod.cmd_cycle_list(),
        'current': lambda mod, ns: mod.cmd_cycle_current(),
        'show': lambda mod, ns: mod.cmd_cycle_show(ns.id, ns.node),
        'hold': lambda mod, ns: mod.cmd_cycle_hold(ns.id, ns.node),
        'close': lambda mod, ns: mod.cmd_cycle_close(ns.id, ns.node, ns.result),
        'retry': lambda mod, ns: mod.cmd_cycle_retry(ns.id, ns.node, ns.summary, ns.drop),
    }
    _INSTRUCTION_SPEC = {
        'add': ((), {'--text': ('text', True, None),
                     '--scope': ('scope', True, 'global'), '--cycle': ('cycle', True, None),
                     '--node': ('node', True, None),
                     '--target': ('target', True, 'stage'), '--said-by': ('said_by', True, 'user'),
                     '--note': ('note', True, None),
                     '--stop-after-nodes': ('stop_after_nodes', True, None),
                     }),
        'list': ((), {'--json': ('json', False, False), '--live': ('live', False, False),
                      '--scope': ('scope', True, None), '--all': ('all_entries', False, False),
                      '--project': ('project', True, None), '--node': ('node', True, None)}),
        'resolve': (('ins_id',), {'--state': ('state', True, None),
                                  '--note': ('note', True, None),
                                  '--project': ('project', True, None)}),
    }
    #: Verb -> the module function that verb runs, as `function(namespace)`.
    _CALLS = {
        'clean-temp': lambda mod, ns: mod.cmd_clean_temp(ns.project, ns.node, ns.yes, ns.force),
        'dispatch': lambda mod, ns: mod.cmd_dispatch(ns),
        'next': lambda mod, ns: mod.cmd_next(ns),
    }
    #: Positional arguments the real parser declares `type=int`.
    _INT_POSITIONALS = set()

    def __init__(self, project: Path):
        self.project = Path(project).resolve()
        key = str(self.project)
        mod = _FAST_SM_CACHE.get(key)
        if mod is None:
            # A fresh module per sandbox: it holds no state of its own, but a fresh load keeps one
            # test's imports from leaking into another's.
            mod = _fresh_module('state_manage_fast', STATE_MANAGE)
            _FAST_SM_CACHE[key] = mod
        self.mod = mod
        self.stdout = ''
        self.stderr = ''
        # state-manage.py binds its paths from Path.cwd() AT IMPORT TIME. In production the process
        # is started with cwd set to the project, so the binding is right; here the module is
        # imported once and aimed at each sandbox instead. `bind_paths` is the module's own lever
        # for that, and one directory is the whole of it.
        mod.bind_paths(self.project / '.pf')

    # ── running one command line, the way conduct_pipeline.py emits it ───────────
    def run(self, cmd, cwd=None) -> int:
        # `cwd` is conduct_pipeline._run's second argument (it runs the command with the project as
        # the working directory). The module's paths are already pinned per sandbox, so the value
        # is only checked, never used to move the process.
        if cwd is not None:
            assert Path(cwd).resolve() == self.project, \
                f'command aimed at {cwd}, sandbox is {self.project}'
        argv = [str(c) for c in cmd]
        assert len(argv) > 2 and argv[1] == str(STATE_MANAGE), \
            f'not a state-manage command: {cmd}'
        argv = argv[2:]
        verb = argv[0]
        assert verb in self._VERBS, f'unknown verb {verb!r}'
        out, err = io.StringIO(), io.StringIO()
        real_out, real_err, real_argv = sys.stdout, sys.stderr, sys.argv
        sys.stdout, sys.stderr, sys.argv = out, err, [str(STATE_MANAGE), *argv]
        try:
            with chdir(self.project):
                rc = self._dispatch(argv)
        finally:
            sys.stdout, sys.stderr, sys.argv = real_out, real_err, real_argv
        self.stdout, self.stderr = out.getvalue(), err.getvalue()
        if rc:
            raise RuntimeError(f'state-manage {" ".join(argv)} failed (rc={rc}):\n'
                               f'{self.stdout}\n{self.stderr}')
        return rc

    def _dispatch(self, argv) -> int:
        mod = self.mod
        argv = list(argv)
        if len(argv) > 1 and Path(str(argv[1])).name == 'dag-manage.py':
            argv[1] = str(DAG_MANAGE)
        if argv[0] == 'instruction':
            sub = argv[1] if len(argv) > 1 else None
            if sub not in self._INSTRUCTION_SPEC:
                self.stderr = 'usage: state-manage.py instruction {add|list|resolve} ...'
                return 1
            ns = self._namespace(argv[2:], *self._INSTRUCTION_SPEC[sub])
            ns.command = argv[0]
            ns.instruction_command = sub
            return self._call(lambda: mod.route(ns))
        if argv[0] == 'cycle':
            sub = argv[1] if len(argv) > 1 else None
            if sub not in self._CYCLE_SPEC:
                self.stderr = ('usage: state-manage.py cycle {'
                               + '|'.join(self._CYCLE_CALLS) + '} ...')
                return 1
            ns = self._namespace(argv[2:], *self._CYCLE_SPEC[sub])
            ns.command = argv[0]
            ns.cycle_command = sub
            return self._call(lambda: mod.route(ns))

        positional, flags = self._SPEC[argv[0]]
        ns = self._namespace(argv[1:], positional, flags)
        ns.command = argv[0]
        return self._call(lambda: self._CALLS[argv[0]](mod, ns))

    def _call(self, fn) -> int:
        """The real CLI reports a refusal by exiting non-zero; reproduce that, do not raise."""
        try:
            fn()
        except SystemExit as e:
            return e.code or 1
        return 0

    def _namespace(self, argv, positional_names, flag_spec):
        import argparse
        ns = argparse.Namespace()
        for name in positional_names:
            setattr(ns, name, None)
        for _, (name, takes_value, default) in flag_spec.items():
            setattr(ns, name, default)
        rest = list(argv)
        idx = 0
        for name in positional_names:                 # positionals first, in declaration order
            if idx < len(rest) and not rest[idx].startswith('--'):
                raw = rest[idx]
                setattr(ns, name, int(raw) if name in self._INT_POSITIONALS else raw)
                idx += 1
        while idx < len(rest):
            token = rest[idx]
            assert token in flag_spec, f'unknown flag {token!r} for this harness'
            name, takes_value, _ = flag_spec[token]
            if takes_value:
                idx += 1
                raw = rest[idx]
                default = flag_spec[token][2]
                if name == 'stop_after_nodes' or name in _INT_FLAGS or \
                        isinstance(default, int) and default is None:
                    raw = int(raw)
                setattr(ns, name, raw)
            else:
                setattr(ns, name, True)
            idx += 1
        return ns


class InProcessDagManage:
    """dag-manage.py's `list` and `status` in-process: a process start per plan is the biggest
    single cost in the suite.

    conduct_pipeline.py asks dag-manage for the ready set *through a subprocess* (`ranked_ready_nodes`
    shells out on purpose: the ranking must have exactly one implementation). This keeps that call
    shape -- argv in, stdout JSON out, non-zero exit -> fallback -- while running dag-manage's own
    `cmd_list` / `cmd_status` here.
    """

    _mod = None
    #: The uuid of the node the last in-process `add` minted -- a reader for a test that wants to
    #: know which node the graph just grew. `add` prints it and the pipeline parses it, so this is
    #: the same fact, kept where a test does not have to parse.
    last_added = None

    @classmethod
    def module_for(cls, project: Path):
        mod = cls._mod
        if mod is None:
            mod = _fresh_module('dag_manage_fast', DAG_MANAGE)
            cls._mod = mod
        mod.LEDGER_DIR = Path(project) / '.pf'
        mod.DATA_DIR = mod.LEDGER_DIR
        mod.NODES_DIR = mod.DATA_DIR / 'nodes'
        mod.IDEAS_JSON = mod.DATA_DIR / 'ideas.json'
        return mod

    @classmethod
    def handle(cls, project: Path, argv):
        """Answer one `dag-manage ...` argv the way the real process would.

        The pipeline needs five commands: `list [-n N]` (the tool's own node ranking), `status <node>
        completed|shelved` (the close marks the node), and -- for the maintenance channel's graph edit
        (`conduct_pipeline_impl.maintenance`) -- `add`, `depend` and `undepend`. Each one calls the
        project's own `cmd_*`, so a uuid is really minted and an edge is really written; only the
        process start is gone. Everything else falls through as a non-zero exit, which
        `ranked_ready_nodes` already knows how to read.
        """
        mod = cls.module_for(project)
        verb = argv[2] if len(argv) > 2 else None
        out, err = io.StringIO(), io.StringIO()
        real_out, real_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            with chdir(project):
                if verb == 'list':
                    limit = int(argv[argv.index('-n') + 1]) if '-n' in argv else 0
                    tags = argv[argv.index('--tags') + 1] if '--tags' in argv else None
                    ns = mod.argparse.Namespace(status=None, ready='--ready' in argv,
                                                verbose=False, limit=limit, tags=tags)
                    mod.cmd_list(ns)
                elif verb == 'status' and len(argv) > 4:
                    ns = mod.argparse.Namespace(uuid=argv[3], status=argv[4])
                    mod.cmd_status(ns)
                elif verb == 'add':
                    ns = cls._add_namespace(mod, argv[3:])
                    mod.cmd_add(ns)
                    # `apply` reads the new node's uuid off this command's stdout, so the answer
                    # here IS what the real process prints (`Node created: <uuid>`); the line below
                    # keeps the last one on the class, for a test that wants to know which node the
                    # graph just grew without parsing anything.
                    cls.last_added = out.getvalue().split(':', 1)[1].split()[0]
                elif verb == 'depend' and len(argv) > 4:
                    ns = mod.argparse.Namespace(uuid=argv[3], on=argv[argv.index('--on') + 1])
                    mod.cmd_depend(ns)
                elif verb == 'undepend' and len(argv) > 3:
                    # `undepend <uuid> <dep>` (positional) is what the pipeline emits; `--from` is
                    # the flag spelling the real parser also accepts.
                    if argv[3].startswith('--'):
                        ns = mod.argparse.Namespace(uuid=argv[4], dep=None,
                                                    from_=argv[argv.index('--from') + 1])
                    else:
                        ns = mod.argparse.Namespace(uuid=argv[3], dep=argv[4], from_=None)
                    mod.cmd_undepend(ns)
                else:
                    return _FakeProc(1, '', f'this hook does not serve: {argv}')
        except SystemExit as e:                  # the real CLI's way of reporting a refusal
            return _FakeProc(e.code or 1, out.getvalue(), err.getvalue())
        finally:
            sys.stdout, sys.stderr = real_out, real_err
        return _FakeProc(0, out.getvalue(), err.getvalue())

    @staticmethod
    def _add_namespace(mod, rest):
        """`dag-manage add` as the project's own parser declares it: title, then the description.

        `title` is the first positional and `description` swallows the rest (nargs='*'), so a flag
        has to cut the positional run short -- `cmd_add` joins whatever lands in `description`.
        """
        title, description, flags = None, [], {}
        i = 0
        while i < len(rest):
            token = rest[i]
            if token.startswith('--'):
                key = token[2:].replace('-', '_')
                if key in ('depends_on', 'mode', 'tags', 'desc', 'weight'):
                    flags[key] = rest[i + 1]
                    i += 2
                    continue
                i += 1
                continue
            if title is None:
                title = token
            else:
                description.append(token)
            i += 1
        return mod.argparse.Namespace(
            title=title, description=description, desc=flags.get('desc'),
            depends_on=flags.get('depends_on'), mode=flags.get('mode'), tags=flags.get('tags'),
            weight=flags.get('weight'))


class _FakeProc:
    """What `subprocess.run(...)` hands back, for the calls the harness answers in-process."""

    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.args = None


@contextlib.contextmanager
def _dag_hook(project: Path):
    """Make every `dag-manage` subprocess call answer in-process, for one harness call.

    conduct_pipeline reaches dag-manage through `subprocess.run` on every plan (on purpose: the
    ranking must have exactly one implementation). A process start per plan is the single biggest
    cost in this suite, so the call is answered by dag-manage's own `cmd_list` here.

    The patch is on the stdlib module and therefore process-global, which is why it is guarded by
    a lock and always undone. It has to be global rather than `pipeline.subprocess = ...`: two callers
    reach dag-manage -- the loaded conduct_pipeline module, and `state-manage.py`'s own copies -- and
    only a patch on the shared module covers both.
    """
    real_run = subprocess.run

    def hooked(argv, *a, **kw):
        # argv is [sys.executable, dag-manage.py, 'list', ...] -- the script is argv[1].
        try:
            is_dag = len(argv) > 1 and str(argv[1]) == str(DAG_MANAGE)
        except TypeError:
            is_dag = False
        if is_dag:
            return InProcessDagManage.handle(Path(kw.get('cwd') or os.getcwd()), list(argv))
        return real_run(argv, *a, **kw)

    with _PIPELINE_CALL_LOCK:
        subprocess.run = hooked
        try:
            yield load_pipeline()
        finally:
            subprocess.run = real_run


_SIGNAL_MODULE = None


def _signal_module():
    global _SIGNAL_MODULE
    if _SIGNAL_MODULE is None:
        _SIGNAL_MODULE = _fresh_module('conduct_signal', SIGNAL_PY)
    return _SIGNAL_MODULE
