#!/usr/bin/env python3
"""Manage the conduct pipeline's state: one directory, `.pf/`, per project.

This is the tool's one writer: the cycle book, the signals and the standing instructions all go out
through here, so a session never reads or writes raw JSON itself. (The graph is `dag-manage.py`'s own
record, with its own writer.) Two gates come with that: the ledger lock, held across each mutating
command's read-modify-write, and `require_initialized` -- nothing writes a `.pf/` this tool never
init'ed.

`--help` is the list of verbs, their flags and their values. This file owns the parser and the one
route from a parsed line to the call that answers it; the machinery behind each verb is the package
next door (`conduct_state/`, whose `__init__` names every layer).
"""

import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONDUCT = HERE.parent                      # the conduct tool directory


# ═══════════════════════════════════════════════════════════════════════════════
#  Loading a neighbouring file, and the implementation package
# ═══════════════════════════════════════════════════════════════════════════════

# `tools/shared/` is the shared layer, and it has no package to import from: every file in it is
# loaded by path through `loader.py`. This function is that bootstrap, the same shape in every file
# that needs it -- a path load, never a name import, because nothing puts `tools/` on `sys.path`.
SHARED = CONDUCT.parent / 'shared'

_LOADER = None


def _loader():
    """`tools/shared/loader.py`: the one loader, for the modules this tree keeps outside `shared/`."""
    global _LOADER
    if _LOADER is None:
        spec = importlib.util.spec_from_file_location('pf_shared_loader', SHARED / 'loader.py')
        _LOADER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_LOADER)
    return _LOADER


def _load_impl():
    """Load `conduct_state/` by path, fresh, and hand it back.

    By PATH, not by name, and with the name dropped from `sys.modules` first: this file is loaded
    more than once in a process -- the test harness loads a fresh copy per sandbox, so that two
    throwaway projects never share one `.pf/` -- and a by-name import would hand the second copy
    the first copy's package, and its paths with it. Inside the package, `from . import store`
    resolves through the registration the load makes.
    """
    pkg = 'conduct_state'
    return _loader().load_by_path(pkg, CONDUCT / pkg / '__init__.py',
                                  package_dir=CONDUCT / pkg, register=True, fresh=True)


_impl = _load_impl()

# Everything the package exports, reachable from here: `python state-manage.py <verb>` and a caller
# that loads this module in process see one flat surface -- exactly the one this file offered at
# module scope before the split. `.pf/` itself is deliberately not among them: it belongs to
# `store` and is read at call time, and `bind_paths` is how a caller aims this copy at a project.
globals().update({name: getattr(_impl, name) for name in _impl.__all__})


class _NextHelp(argparse.Action):
    """`next --help`：help 尾巴上那段合同正文，只有真打印这一次才算得出来。

    `_render_contract` 要按路径加载管线包（`_load_pipeline`），所以它挂在打印那一刻 —— 解析器建
    起来就给每条 state-manage 命令白背一次那份加载，不值当。判据是解析器自己选中了这个动作：哪
    一条 `--help` 被选中，就是那一条在打印。
    """

    def __init__(self, option_strings, dest, default=argparse.SUPPRESS,
                 help='打印 `next` 的 help（尾巴上是 action 全集）'):
        # `dest` 用 argparse 自己算的那个（`'help'`）：读解析器的人靠它认出这条 help 动作，
        # 而这条动作的全部作用就是打印 help。
        super().__init__(option_strings, dest, nargs=0, default=default, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        parser.epilog = _load_pipeline()._render_contract()
        parser.print_help()
        parser.exit()


def _layer(args) -> str:
    """`--global` 说的是哪一层：没有它就是项目那一层。"""
    return 'global' if getattr(args, 'global_', False) else 'project'


#: 唯一的路由表：解析好的 args 变成一次调用，全在这里。键是 `(顶层动词, 子动词)` —— 叶子动词的
#: 子动词是 None。加一个动词就是加一行，叶子动词与三组子动词看的是同一张表。
_ROUTES = {
    ('init', None): lambda a: cmd_init(getattr(a, 'project', None),
                                       getattr(a, 'mode', None) or 'local',
                                       getattr(a, 'own_repo', True)),
    ('register', None): lambda a: cmd_register(getattr(a, 'project', None)),
    ('clean-temp', None): lambda a: cmd_clean_temp(getattr(a, 'project', None),
                                                   getattr(a, 'node', None),
                                                   getattr(a, 'yes', False),
                                                   getattr(a, 'force', False)),
    ('fork-worktree', None): lambda a: cmd_fork_worktree(a.node),
    ('next', None): cmd_next,
    ('dispatch', None): cmd_dispatch,

    ('cycle', 'create'): lambda a: cmd_cycle_create(a.id, a.title,
                                                    getattr(a, 'force', False),
                                                    getattr(a, 'tags', None)),
    ('cycle', 'start'): lambda a: cmd_cycle_start(a.id, a.title,
                                                  getattr(a, 'force', False),
                                                  getattr(a, 'tags', None),
                                                  getattr(a, 'parallel', None)),
    ('cycle', 'rename'): lambda a: cmd_cycle_rename(a.id, a.title),
    ('cycle', 'status'): lambda a: cmd_cycle_status(a.id, a.status),
    ('cycle', 'policy'): lambda a: cmd_cycle_policy(a.id, a.key, a.value),
    ('cycle', 'list'): lambda a: cmd_cycle_list(),
    ('cycle', 'current'): lambda a: cmd_cycle_current(),
    ('cycle', 'show'): lambda a: cmd_cycle_show(a.id, a.node),
    ('cycle', 'hold'): lambda a: cmd_cycle_hold(a.id, a.node),
    ('cycle', 'close'): lambda a: cmd_cycle_close(a.id, a.node, a.result),
    ('cycle', 'retry'): lambda a: cmd_cycle_retry(a.id, a.node, getattr(a, 'summary', None),
                                                  getattr(a, 'drop', False)),

    ('instruction', 'add'): lambda a: cmd_instruction_add(a.text, a.scope, a.target,
                                                          getattr(a, 'said_by', 'user'),
                                                          getattr(a, 'note', None),
                                                          getattr(a, 'stop_after_nodes', None),
                                                          getattr(a, 'cycle', None),
                                                          getattr(a, 'node', None)),
    ('instruction', 'list'): lambda a: cmd_instruction_list(getattr(a, 'json', False),
                                                            getattr(a, 'live', False),
                                                            getattr(a, 'project', None),
                                                            getattr(a, 'scope', None),
                                                            getattr(a, 'all_entries', False),
                                                            getattr(a, 'node', None)),
    ('instruction', 'resolve'): lambda a: cmd_instruction_resolve(a.ins_id, a.state,
                                                                  getattr(a, 'note', None),
                                                                  getattr(a, 'superseded_by', None),
                                                                  getattr(a, 'project', None)),

    ('config', 'list'): lambda a: cmd_config_list(getattr(a, 'project', None)),
    ('config', 'get'): lambda a: cmd_config_get(a.key, getattr(a, 'project', None)),
    ('config', 'set'): lambda a: cmd_config_set(a.key, a.value, _layer(a),
                                                getattr(a, 'project', None)),
    ('config', 'unset'): lambda a: cmd_config_unset(a.key, _layer(a),
                                                    getattr(a, 'project', None)),

    ('session', 'show'): lambda a: cmd_session_show(a.node, getattr(a, 'project', None),
                                                    getattr(a, 'json', False)),
    ('session', 'list'): lambda a: cmd_session_list(getattr(a, 'project', None),
                                                    getattr(a, 'json', False)),
    ('session', 'import'): lambda a: cmd_session_import(getattr(a, 'project', None),
                                                        getattr(a, 'dry_run', False)),
}


def route(args) -> None:
    """一行命令变成一次调用 —— 这个工具唯一的分派处。

    `args.command` 是顶层动词，`<动词>_command` 是那个组的子动词（叶子动词没有）。表里查不到只有
    两种情形：一个组没带子动词，或者解析器根本不认识这个词 —— 两种都由这里报出去，一种口径。
    """
    verb = getattr(args, 'command', None)
    sub = getattr(args, f'{verb}_command', None) if verb else None
    fn = _ROUTES.get((verb, sub))
    if fn is not None:
        fn(args)
        return
    subs = [s for (v, s) in _ROUTES if v == verb and s]
    if subs:
        print(f'usage: state-manage.py {verb} {{{"|".join(subs)}}} ...', file=sys.stderr)
    else:
        print('usage: state-manage.py <verb> ...（动词、旗标与取值见 --help）', file=sys.stderr)
    sys.exit(1)


def main(argv=None) -> None:
    # 中文 everywhere：stdout 是给调用方解析的，stderr 是给人看的报错。两个流都按 UTF-8 走，
    # 否则控制台代码页（这台机器默认 GBK）会把字节写歪，按 UTF-8 解码的父进程直接炸在读线程上。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description='Manage conduct pipeline state')
    sub = parser.add_subparsers(dest='command')

    p_cyc = sub.add_parser(
        'cycle',
        help='One line of work: create / start / rename / status / policy / list / current / show / '
             'hold / close / retry',
        description='A cycle is one line of work: it picks ONE DAG node at a time, hands it to a '
                    'session, and records the step when that session reports back. This group owns '
                    'the line itself -- its identity, whether it is in service, its policies, which '
                    'nodes it is holding, and the path it has walked.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    cyc_sub = p_cyc.add_subparsers(dest='cycle_command')

    p_cc = cyc_sub.add_parser('create', help='Create a new cycle')
    p_cc.add_argument('id')
    p_cc.add_argument('--title', help='Cycle title (auto-generated if omitted)')
    p_cc.add_argument('--force', action='store_true',
                      help='Create it while another cycle is still active')
    p_cc.add_argument('--tags', default=None,
                      help='Comma-separated filter set for what this cycle picks from: '
                           'writing / coding / art. Required -- a '
                           'call without it is refused: an empty scope leaves this line '
                           'nothing to pick from')

    p_cst_ = cyc_sub.add_parser('start',
                                help='Create a cycle AND pin it on the node the rule picks '
                                     '(one command for the launcher). Refuses on an empty graph, and '
                                     'refuses while another cycle is still active unless --force')
    p_cst_.add_argument('id')
    p_cst_.add_argument('--title', default=None, help='The user\'s words, verbatim')
    p_cst_.add_argument('--tags', default=None,
                        help='Comma-separated filter set for this cycle: it decides which nodes '
                             'the first pick and every later pick may take '
                             '(writing / coding / art). Required -- a '
                             'call without it is refused: an empty scope leaves this line '
                             'nothing to pick from')
    p_cst_.add_argument('--parallel', default=None,
                        help='How many nodes this line holds and runs at once: pin that many of '
                             'the ready set right here, and write it down as the line\'s '
                             '`parallel` policy (same slot `cycle policy <id> parallel <n>` '
                             'writes). A positive integer, or `off` for the default '
                             + str(POLICY_DEFAULTS['parallel']))
    p_cst_.add_argument('--force', action='store_true',
                        help='Start anyway while another cycle is active (a bare `next` will then '
                             'have to be told which cycle)')

    p_crn = cyc_sub.add_parser('rename', help='Rename an existing cycle')
    p_crn.add_argument('id')
    p_crn.add_argument('--title', required=True, help='New title')

    p_cstt = cyc_sub.add_parser('status',
                                help='Put the line in or out of service: active | inactive')
    p_cstt.add_argument('id')
    p_cstt.add_argument('status', choices=sorted(CYCLE_VALID_STATUSES))

    p_cpo = cyc_sub.add_parser('policy',
                               help="Write one of a line's own policies (the effective set is echoed "
                                    "back)")
    p_cpo.add_argument('id')
    p_cpo.add_argument('key', choices=list(POLICY_KEYS))
    p_cpo.add_argument('value',
                       help='when_shelved: ' + ' | '.join(POLICY_VALUES['when_shelved'])
                            + ' (stop = a node that came back not delivered stops this line and '
                              'waits for a human; continue = mark it shelved and carry on with the '
                              'next node). Default: ' + POLICY_DEFAULTS['when_shelved']
                            + '. ' + ' | '.join(POLICY_COUNTS)
                            + ': a positive integer (how many nodes this line holds and runs at '
                              'once; default ' + str(POLICY_DEFAULTS['parallel']) + '), or `off`'
                            + '. ' + ' | '.join(POLICY_NUMBERS)
                            + ': a positive number (the last acceptable reading; the close of a '
                              'coding-tagged cycle inserts a maintenance node once a ticket '
                              'reaches it), or `off` to clear it'
                            + '. ' + ' | '.join(POLICY_WORDS)
                            + ': ' + ' | '.join(POLICY_WORD_CHOICES['spawner'])
                            + ' (which spawner opens this line\'s sessions; overrides the host '
                              'probe and DSH_CONDUCT_SPAWNER, loses to a per-call --spawner), '
                              'or `off` to clear it')

    cyc_sub.add_parser('list', help='Show all cycles (state.json)')
    cyc_sub.add_parser('current', help='Show the active cycle and the node it holds')

    p_csh = cyc_sub.add_parser('show', help="One node's standing in a cycle: signal + path step")
    p_csh.add_argument('id')
    p_csh.add_argument('node')

    p_chd = cyc_sub.add_parser('hold', help='Pin the cycle on a node: this is how work is claimed')
    p_chd.add_argument('id')
    p_chd.add_argument('node')

    p_ccl = cyc_sub.add_parser('close',
                               help="Record the step on the cycle's path and let the node go")
    p_ccl.add_argument('id')
    p_ccl.add_argument('node')
    p_ccl.add_argument('--result', required=True, choices=sorted(VALID_NODE_RESULTS),
                       help='completed | failed | stopped (what the worker reported)')

    p_crt = cyc_sub.add_parser('retry',
                               help='Take a node signal out of the way (retries): rewrite it as '
                                    'pending, or --drop to leave no ticket at all')
    p_crt.add_argument('id')
    p_crt.add_argument('node')
    p_crt.add_argument('--summary', help='Why the node was restarted')
    p_crt.add_argument('--drop', action='store_true',
                       help='Leave no ticket at all. A `pending` ticket is a red light: `next` '
                            'waits on it forever, so dropping it is what lets the retry be '
                            'dispatched')

    p_ins = sub.add_parser(
        'instruction', help='Standing user instructions (append-only account)',
        description='Three scopes. Whether an entry can be read is decided by WHERE it was recorded, '
                    'not by anyone remembering to resolve it: global = readable anywhere, by every '
                    'session and in every cycle; cycle = readable while the cycle it was recorded in '
                    'is active, and unreadable once that cycle ends; node = readable while the node '
                    'it was recorded in is unfinished. `add` binds the cycle/node from the state, or '
                    'takes the binding from `--cycle` / `--node` when the state cannot name one (two '
                    'active cycles side by side); a cycle/node entry with nothing to bind to is '
                    'refused, because an unbound entry is read as visible everywhere and never goes '
                    'out of sight. Nothing is injected into a dispatch prompt: `list` is how a '
                    'session reads the account, and running it among its first commands is what '
                    'makes an instruction reach it. What `resolve` is still for: a human taking an '
                    'instruction off (withdrawn), and recording that a global entry was honoured '
                    '(applied).')
    ins_sub = p_ins.add_subparsers(dest='instruction_command')
    p_ia = ins_sub.add_parser('add', help='Append one instruction, verbatim')
    p_ia.add_argument('--text', required=True, help='The sentence, EXACTLY as the user said it')
    p_ia.add_argument('--scope', dest='scope', default='global', choices=list(valid_scopes()),
                      help='Where this instruction is visible from (`instruction --help`): global = '
                           'everywhere; cycle = the cycle it is recorded in, out of sight once that '
                           'cycle ends; node = the node it is recorded in, out of sight once that '
                           'node is completed. The cycle/node it binds to come from the state, or '
                           'from --cycle / --node when the state cannot name one; a cycle/node entry '
                           'that nothing binds is refused. (default: global -- the noisy side of a '
                           'coin flip is the safe one)')
    p_ia.add_argument('--cycle', default=None,
                      help='The line this entry belongs to. Needed while two active cycles run side '
                           'by side, where the state has no single current one; a cycle-scoped entry '
                           'with nothing to bind to is refused (it would be read as visible '
                           'everywhere, forever). Default: the single active cycle')
    p_ia.add_argument('--node', default=None,
                      help='The node this entry belongs to, for --scope node. Default: the node the '
                           'cycle is holding')
    p_ia.add_argument('--target', choices=list(VALID_TARGETS), default='stage',
                      help='stage = a constraint every worker must see; boundary = rhythm, read '
                           'only when a node closes (default: stage)')
    p_ia.add_argument('--said-by', default='user')
    p_ia.add_argument('--note', default=None)
    p_ia.add_argument('--stop-after-nodes', type=int, default=None,
                      help='Optional machine-checkable cap: stop once this many nodes have run')
    p_il = ins_sub.add_parser(
        'list', help='What the user asked for, as seen from where you stand (the instruction '
                     'channel: nothing is injected into a prompt)',
        description='This is how an instruction reaches a session: run it among your first commands '
                    'and read the answer. The default output is computed from WHERE the pipeline '
                    'stands -- global entries always, a cycle entry while its cycle is the current '
                    'one, a node entry while its node is the one in hand, and anything it cannot '
                    'judge -- as plain sentences, with ids and bookkeeping folded away. '
                    '`--json` is the same answer for a machine (an empty array when there is '
                    'nothing). `--all`, `--live` and `--scope` ask for the ACCOUNT instead: the raw '
                    'rows, retired entries included when `--all` says so.')
    p_il.add_argument('--json', action='store_true')
    p_il.add_argument('--live', action='store_true',
                      help='Account view: only the entries whose state is live')
    p_il.add_argument('--all', dest='all_entries', action='store_true',
                      help='Account view: the whole account, retired entries included')
    p_il.add_argument('--scope', dest='scope', default=None, choices=list(valid_scopes()),
                      help='Account view narrowed to one scope. `--scope global` reads out the '
                           'global entries')
    p_il.add_argument('--project', default=None,
                      help='Project root. Default: the current directory. Pass it explicitly when '
                           'your cwd may not be the project root -- the ledger root is resolved at '
                           'import time, so a wrong cwd silently reads an empty account')
    p_il.add_argument('--node', default=None,
                      help='The DAG node you are working on: with two active cycles side by side it '
                           'is how this read knows which line you stand on (`cycle` entries go to '
                           'their own line only). Default: not asked')
    p_ir = ins_sub.add_parser('resolve', help='Record what became of one instruction')
    p_ir.add_argument('ins_id')
    p_ir.add_argument('--state', required=True,
                      choices=list(RESOLVE_STATES),
                      help='withdrawn = the owner takes an instruction off; applied|n/a = this '
                           'global entry was honoured here / nothing here falls under it, and it '
                           'stays in force. A cycle/node entry needs no resolve at all: it goes out '
                           'of sight on its own, so done|superseded are only for tidying an account '
                           'by hand')
    p_ir.add_argument('--note', default=None)
    p_ir.add_argument('--superseded-by', dest='superseded_by', default=None)
    p_ir.add_argument('--project', default=None, help='Project root (default: current directory)')

    p_cfg = sub.add_parser(
        'config',
        help="This machine's and this project's settings: built-in defaults, then the global and "
             "project overrides, the last one written winning",
        description='Three keys. max_agents: how many subagents one session may have in flight at '
                    'once. test_tier: full (the whole suite) or minimal (the project\'s own few '
                    'checks). worktree_mode: off / fork / reuse (whether a node\'s work gets its '
                    'own worktree). A value lives in one of two override layers: '
                    '`~/.pf/config.json` (global, travels with the person and the machine, never '
                    'generated for you) and `<project>/.pf/config.json` (travels with the project; '
                    '`init` puts an empty `{}` there). A layer with no file reads as "never '
                    'changed the default". `list` prints every effective value and the layer it '
                    'came from; `get` prints the value alone, which is what a skill reads. '
                    '`set` / `unset` write the project layer unless --global says otherwise.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    cfg_sub = p_cfg.add_subparsers(dest='config_command')
    cfg_sub.add_parser('list', help='Every effective value, and the layer it came from')
    p_cfg_get = cfg_sub.add_parser('get', help='Print one effective value, and nothing else')
    p_cfg_get.add_argument('key', choices=sorted(CONFIG_SPEC))
    p_cfg_set = cfg_sub.add_parser('set', help='Write one override (the project layer by default)')
    p_cfg_set.add_argument('key', choices=sorted(CONFIG_SPEC))
    p_cfg_set.add_argument('value')
    p_cfg_set.add_argument('--global', dest='global_', action='store_true',
                           help='Write the global layer (~/.pf/config.json) instead of the project one')
    p_cfg_set.add_argument('--project', default=None, help='Project root (default: current directory)')
    p_cfg_unset = cfg_sub.add_parser('unset',
                                     help='Drop one override, letting the default take over again')
    p_cfg_unset.add_argument('key', choices=sorted(CONFIG_SPEC))
    p_cfg_unset.add_argument('--global', dest='global_', action='store_true',
                             help='Drop it from the global layer instead of the project one')
    p_cfg_unset.add_argument('--project', default=None, help='Project root (default: current directory)')

    p_sess = sub.add_parser(
        'session',
        help='Which conversation produced a node, and which ones ran it: show / list / import',
        description='A session id is machine-local -- a conversation lives on the machine that '
                    'opened it -- so the ids live in `.pf/meta/sessions/<node id>.json` instead of '
                    'on the record: guaranteed dead on another machine, and impossible to tell apart '
                    'from "that session was deleted here". `show` reads one node\'s note, `list` '
                    'reads every note this machine holds (keyed by node id, for a panel), and '
                    '`import` folds the older `dsh_session` field into notes once per machine -- '
                    'writing notes and nothing else.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sess_sub = p_sess.add_subparsers(dest='session_command')

    p_ss = sess_sub.add_parser('show', help='One node\'s note: its discussion, and the sessions that ran it')
    p_ss.add_argument('node', help='Node id, or as much of its start as names one node')
    p_ss.add_argument('--json', action='store_true', help='Print raw JSON')
    p_ss.add_argument('--project', default=None, help='Project root (default: current directory)')

    p_sl = sess_sub.add_parser('list', help='Every note this machine holds, keyed by node id')
    p_sl.add_argument('--json', action='store_true', help='Print raw JSON')
    p_sl.add_argument('--project', default=None, help='Project root (default: current directory)')

    p_si = sess_sub.add_parser('import',
                               help='Fold the older `dsh_session` field into notes (once per machine)')
    p_si.add_argument('--dry-run', dest='dry_run', action='store_true',
                      help='Say what would be noted; write nothing')
    p_si.add_argument('--project', default=None, help='Project root (default: current directory)')

    p_next = sub.add_parser(
        'next', help='Show (or do) the next pipeline action',
        description='The one entry point. plan: read-only, writes nothing. apply (no --plan): '
                    "drive the pipeline to its next stable action -- consume a finished node's "
                    'signal, do the bookkeeping, stop at the first thing a caller can act on. '
                    'The actions a caller may have to act on are `dispatch` (spawn this node), '
                    '`wait` (read its note / close_with and run that one command), `ask_user`, '
                    '`stop_pipeline`. The returned action NEVER carries commands to run: the '
                    'tool runs them.',
        # `-h` is this parser's own action because its help carries the action vocabulary
        # (`conduct_pipeline.ACTIONS`, the single source the contract is computed from) as an
        # epilog, computed at print time.
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p_next.add_argument('-h', '--help', action=_NextHelp)
    p_next.add_argument('--cycle', help='Cycle id (default: the single active cycle)')
    p_next.add_argument('--project', default=None, help='Project root (default: current directory)')
    p_next.add_argument('--plan', action='store_true',
                        help='Read-only: never write prompt files or touch state')
    p_next.add_argument('--json', action='store_true', help='Print raw JSON')
    dec = p_next.add_mutually_exclusive_group()
    dec.add_argument('--continue', dest='cont', action='store_true',
                     help='Closing call: keep going (a stop rule that says stop still wins)')
    dec.add_argument('--stop', dest='stop', action='store_true',
                     help='Closing call: stop here (parks the cycle)')
    p_next.add_argument('--node', help='Closing call: which ready node to run next')
    p_next.add_argument('--note', default='', help='Why the closing call decided that')
    p_next.add_argument('--after', dest='after', default=None, metavar='NODE_ID',
                        help='The worker closing its own node: settle the node whose work just '
                             'finished against the signal it already wrote, then dispatch or stop. '
                             'A node still in flight answers `wait` instead of being read as '
                             'delivered')
    p_next.add_argument('--spawn', action='store_true',
                        help='If the resulting action is a dispatch, start that session too '
                             '(makes the closing call one-and-done: settle, then spawn)')
    p_next.add_argument('--spawner', default=None, metavar='KIND|PATH',
                        help='Which spawner opens that session: `dsh`, `zcode`, or a path. '
                             'Default: whichever host this shell runs under.')

    p_disp = sub.add_parser(
        'dispatch',
        help='Start the session for the node this cycle is holding (what `next --after` wrote)',
        description='Dispatch and nothing else: read the node this cycle holds, then start the '
                    'session whose prompt the closing call already wrote at '
                    '.pf/meta/launch/<cycle>/<node>/prompt.txt. No bookkeeping and no state change '
                    '-- that is what makes "commit and push first, then dispatch" expressible: the '
                    'next session boots in the same worktree and pulls. '
                    'DSH_CONDUCT_SPAWN_DRY_RUN=1 makes the call and reports it without opening a '
                    'session.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p_disp.add_argument('id', help='Cycle id')
    p_disp.add_argument('--project', default=None, help='Project root (default: current directory)')
    p_disp.add_argument('--json', action='store_true', help='Print raw JSON')
    p_disp.add_argument('--spawner', default=None, metavar='KIND|PATH',
                        help='Which spawner opens that session: `dsh`, `zcode`, or a path. '
                             'Default: whichever host this shell runs under.')

    p_init = sub.add_parser(
        'init',
        help='Put `.pf/` in place: the one setup act, run once per project',
        description='Write `.pf/.gitignore` and `.pf/.gitattributes`, and decide whether this '
                    'project commits its `.pf/` or keeps that directory to itself. `--local` (the '
                    'default) writes one line into the repository\'s `info/exclude` -- a per-clone '
                    'file, so no teammate ever sees a line about a directory that does not exist '
                    'for them -- and gives `.pf/` its own repository unless `--no-own-repo`. '
                    '`--commit` takes the exclude line back out and lets `.pf/` travel with the '
                    'project. Idempotent: re-running it never does anything twice, and switching '
                    'modes is just running it again with the other flag.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p_init.add_argument('--project', default=None, help='Project root (default: current directory)')
    p_mode = p_init.add_mutually_exclusive_group()
    p_mode.add_argument('--local', dest='mode', action='store_const', const='local',
                        help='Keep `.pf/` out of the project\'s repository (the default)')
    p_mode.add_argument('--commit', dest='mode', action='store_const', const='commit',
                        help='Let `.pf/` go into the project\'s repository')
    p_init.add_argument('--own-repo', dest='own_repo', action='store_true', default=True,
                        help='Local mode only: give `.pf/` a repository of its own (the default)')
    p_init.add_argument('--no-own-repo', dest='own_repo', action='store_false',
                        help='Local mode only: leave `.pf/` as plain ignored files')

    p_reg = sub.add_parser(
        'register',
        help='Write the ledger\'s git merge driver into this project\'s git config',
        description='Set `merge.pf.name` and `merge.pf.driver` in the project\'s git config. `init` '
                    'already does this as part of its one setup act; the verb is here so a project '
                    'can be brought forward, or a moved checkout re-pointed, without re-running '
                    'setup. `pf register` calls exactly this.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p_reg.add_argument('--project', default=None, help='Project root (default: current directory)')

    p_ct = sub.add_parser('clean-temp',
                          help='List (or with --yes, remove) every node\'s temp/ directory')
    p_ct.add_argument('--project', default=None, help='Project root (default: current directory)')
    p_ct.add_argument('--node', default=None, help='Only this node id (prefix is enough)')
    p_ct.add_argument('--yes', action='store_true',
                      help='Actually delete; without it, only list')
    p_ct.add_argument('--force', action='store_true',
                      help='Also clear a node a cycle is holding right now (its session is gone '
                           'and the claim was never released)')

    p_fork = sub.add_parser(
        'fork-worktree',
        help='Fork one node\'s parallel-mode worktree: `.pf/worktrees/<短id>` on branch '
             '`pf/<短id>`',
        description='The parallel mode\'s fork, as one command (SKILL「并行：worktree」): refuses '
                    'while the main tree is dirty (another line may be mid-merge-back) and while '
                    'the node\'s short id is already taken (worktree directory or branch '
                    '`pf/<短id>` exists); otherwise `git worktree add`s the checkout and writes '
                    '`.pf/meta/FORK-<短id>.json` -- the record the closing call gates on, so a '
                    'node whose merge-back never finished cannot be settled. Works from a worktree '
                    'cwd too: the main tree is resolved the same way the record commands do it.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p_fork.add_argument('node', help='The DAG node this worktree is for; its first 8 characters '
                                     'name the worktree directory and the branch')

    args = parser.parse_args(argv)
    route(args)


if __name__ == '__main__':
    main()
