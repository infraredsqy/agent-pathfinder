#!/usr/bin/env python3
"""Manage the conduct pipeline's state: one directory, `.pf/`, per project.

This is the tool's one writer: the cycle book, the signals and the standing instructions all go out
through here, so a session never reads or writes raw JSON itself. (The graph is `dag-manage.py`'s own
record, with its own writer.) Two gates come with that: the ledger lock, held across each mutating
command's read-modify-write, and `require_initialized` -- nothing writes a `.pf/` this tool never
init'ed.

`--help` is the list of verbs, their flags and their values. This file owns the parser and the one
route from a parsed line to the call that answers it; the machinery behind each verb is the package
next door (`state/`, whose `__init__` names every layer).
"""

import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent      # the conduct tool directory: the entry sits at its top
CONDUCT = HERE


# ═══════════════════════════════════════════════════════════════════════════════
#  Loading a neighbouring file, and the implementation package
# ═══════════════════════════════════════════════════════════════════════════════

# Bootstrap: the loader too is taken by path, so this passage has to travel with every file
# (see `shared/loader.py`).
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


def _tool_path(env_var: str, default):
    """`tools/shared/toolpath.py`: `<env var>` when it is set, else the neighbour this tree ships.

    One rule for every part of this tree a file points at, resolved by one implementation: a review
    draft or a drill copy names the part it wants with that part's variable, and no file grows a
    second search path of its own.
    """
    toolpath = _loader().load_shared('toolpath')
    return toolpath.tool_path(env_var, default)


#: The implementation package this file reads and writes: in the shipped tree it is the `state/`
#: next door, while a draft or a drill copy points `DSH_CONDUCT_STATE_DIR` at the real tool's copy.
STATE_PKG = _tool_path('DSH_CONDUCT_STATE_DIR', CONDUCT / 'state')


def _load_impl():
    """Load `state/` by path, fresh, and hand it back.

    By PATH, not by name, and with the name dropped from `sys.modules` first: this file is loaded
    more than once in a process -- the test harness loads a fresh copy per sandbox, so that two
    throwaway projects never share one `.pf/` -- and a by-name import would hand the second copy
    the first copy's package, and its paths with it. Inside the package, `from . import store`
    resolves through the registration the load makes.

    Where the package IS comes from `STATE_PKG`: the neighbour of the shipped tree, or
    `DSH_CONDUCT_STATE_DIR` for a tree that keeps its conduct scripts somewhere else -- the same rule
    and the same variable the merge driver resolves its copy of this package with.
    """
    pkg = 'pf_state'
    return _loader().load_by_path(pkg, STATE_PKG / '__init__.py',
                                  package_dir=STATE_PKG, register=True, fresh=True)


_impl = _load_impl()

# Everything the package exports, reachable from here: `python state-manage.py <verb>` and a caller
# that loads this module in process see one flat surface -- exactly the one this file offered at
# module scope before the split. `.pf/` itself is deliberately not among them: it belongs to
# `store` and is read at call time, and `bind_paths` is how a caller aims this copy at a project.
globals().update({name: getattr(_impl, name) for name in _impl.__all__})


class _NextHelp(argparse.Action):
    """`next --help`: the contract text on the tail of the help, which is only computable by really printing.

    `_render_contract` has to load the pipeline package by path (`_load_pipeline`), so it hangs off the
    printing moment -- building the parser would otherwise make every state-manage command carry that
    load for nothing. The judgement is that the parser itself picked this action: whichever `--help` was
    picked is the one printing.
    """

    def __init__(self, option_strings, dest, default=argparse.SUPPRESS,
                 help='Print `next`\'s help (the tail carries the full set of actions)'):
        # `dest` is the one argparse computes for itself (`'help'`): a reader of the parser recognizes
        # this help action by it, and this action's whole job is to print the help.
        super().__init__(option_strings, dest, nargs=0, default=default, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        parser.epilog = _load_pipeline()._render_contract()
        parser.print_help()
        parser.exit()


def _target_layer(args) -> str:
    """Which layer a write names: `--global` / `--cycle` / `--node`, and the project layer otherwise.

    `set` and `unset` write exactly one layer, so the three flags are mutually exclusive on the
    parser: two of them at once would leave "which layer did I just write" without an answer.
    """
    if getattr(args, 'global_', False):
        return 'global'
    if getattr(args, 'cycle', None):
        return 'cycle'
    if getattr(args, 'node', None):
        return 'node'
    return 'project'


#: The one routing table: a parsed args object becomes one call, all of it here. The key is
#: `(top-level verb, subverb)` -- a leaf verb's subverb is None. Adding a verb means adding one line;
#: leaf verbs and the three groups of subverbs read the same table.
#:
#: Each line reads only the fields the parser hands over: defaults live on the parser (which flag of
#: which command is `--help` itself), and this table does not restate them -- restating is two sources
#: for one thing, and changing one while forgetting the other forks that command's behaviour from its
#: `--help`.
_ROUTES = {
    ('init', None): lambda a: cmd_init(a.project, a.mode, a.own_repo),
    ('register', None): lambda a: cmd_register(a.project),
    ('clean-temp', None): lambda a: cmd_clean_temp(a.project, a.node, a.yes, a.force),
    ('fork-worktree', None): lambda a: cmd_fork_worktree(a.node),
    ('next', None): cmd_next,
    ('dispatch', None): cmd_dispatch,

    ('cycle', 'create'): lambda a: cmd_cycle_create(a.id, a.title, a.force, a.tags),
    ('cycle', 'start'): lambda a: cmd_cycle_start(a.id, a.title, a.force, a.tags, a.parallel),
    ('cycle', 'rename'): lambda a: cmd_cycle_rename(a.id, a.title),
    ('cycle', 'status'): lambda a: cmd_cycle_status(a.id, a.status),
    ('cycle', 'policy'): lambda a: cmd_cycle_policy(a.id, a.key, a.value),
    ('cycle', 'list'): lambda a: cmd_cycle_list(),
    ('cycle', 'current'): lambda a: cmd_cycle_current(),
    ('cycle', 'show'): lambda a: cmd_cycle_show(a.id, a.node),
    ('cycle', 'hold'): lambda a: cmd_cycle_hold(a.id, a.node),
    ('cycle', 'close'): lambda a: cmd_cycle_close(a.id, a.node, a.result),
    ('cycle', 'retry'): lambda a: cmd_cycle_retry(a.id, a.node, a.summary, a.drop),

    ('instruction', 'add'): lambda a: cmd_instruction_add(a.text, a.scope, a.target, a.said_by,
                                                          a.note, a.stop_after_nodes, a.cycle,
                                                          a.node),
    ('instruction', 'list'): lambda a: cmd_instruction_list(a.json, a.live, a.project, a.scope,
                                                            a.all_entries, a.node),
    ('instruction', 'resolve'): lambda a: cmd_instruction_resolve(a.ins_id, a.state, a.note,
                                                                  a.project),
    ('instruction', 'rm'): lambda a: cmd_instruction_rm(a.ins_id, a.project),
    ('instruction', 'revive'): lambda a: cmd_instruction_revive(a.ins_id, a.project),

    ('config', 'list'): lambda a: cmd_config_list(a.project, a.cycle, a.node),
    ('config', 'get'): lambda a: cmd_config_get(a.key, a.project, a.cycle, a.node),
    ('config', 'set'): lambda a: cmd_config_set(a.key, a.value, _target_layer(a), a.project,
                                                a.cycle, a.node),
    ('config', 'unset'): lambda a: cmd_config_unset(a.key, _target_layer(a), a.project,
                                                    a.cycle, a.node),

    ('session', 'show'): lambda a: cmd_session_show(a.node, a.project, a.json),
    ('session', 'list'): lambda a: cmd_session_list(a.project, a.json),
    ('session', 'import'): lambda a: cmd_session_import(a.project, a.dry_run),
}


def route(args) -> None:
    """One command line becomes one call -- this tool's only dispatch.

    `args.command` is the top-level verb and `<verb>_command` is that group's subverb (a leaf verb has
    none). Not finding it in the table has exactly two cases: a group with no subverb, or a word the
    parser does not know at all -- both are reported from here, with one wording.
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
        print('usage: state-manage.py <verb> ... (verbs, flags and values: --help)', file=sys.stderr)
    sys.exit(1)


def main(argv=None) -> None:
    # stdout is for a caller to parse and stderr is the error text a human reads. Both streams go by
    # UTF-8: otherwise the console code page (GBK by default on this machine) writes the bytes wrong,
    # and a parent process decoding them as UTF-8 blows up on its read thread.
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
                               help="Write one policy on this line's own record (the effective set "
                                    "is echoed back; a core setting is `pf config set`'s, not this "
                                    "line's)")
    p_cpo.add_argument('id')
    p_cpo.add_argument('key', choices=list(POLICY_KEYS))
    # The value help is built from the same table the writing side judges with: what `--help` prints
    # and what is accepted cannot drift apart. That table is `state/cycles.py`'s (`POLICY_SPECS`): the
    # line's own three policies, plus every knob a declaration brings that takes the cycle layer. A
    # policy is this line's own thing, so it is not the settings table `pf config` prints.
    knob_table = POLICY_SPECS
    policy_parts = []
    for key in (*POLICY_VALUES, *POLICY_COUNTS, *POLICY_NUMBERS, *POLICY_WORDS):
        spec = knob_table[key]
        if spec['kind'] == 'word' and spec.get('values'):
            shape = ' | '.join(spec['values'])
        elif spec['kind'] == 'number':
            shape = 'a positive number'
        elif spec['kind'] == 'integer':
            shape = 'a positive integer'
        else:
            shape = 'a word'
        part = f'{key}: {shape}'
        if spec.get('summary'):
            part += f' -- {spec["summary"]}'
        if spec.get('default') is not None:
            part += f' (default: {spec["default"]})'
        policy_parts.append(part)
    p_cpo.add_argument('value',
                       help='The value, in the key\'s own kind -- ' + '; '.join(policy_parts)
                            + '. Every knob a feature declares rides on this line too. A core '
                              'setting does not: it takes the project and global layers only, and '
                              '`pf config set` is its door. `off` drops this line\'s entry for a '
                              'number / integer / word, so the next layer of the chain shows '
                              'through; on a switch it is a value -- a switch is turned off by '
                              'writing `off`, and dropping its entry is `pf config unset --cycle`. '
                              'Numbers and integers take a positive value only: a line of 0 fails '
                              'every reading')

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

    # `--scope`'s candidate set comes from the same source as its judgement: `SCOPES` lives on the
    # pipeline's side, and taking it means loading that module by path once. That load tax cannot be
    # deferred -- argparse iterates `choices` at the `add_argument` step to build the metavar, so every
    # command pays it (a dozen milliseconds). What it buys is that the value list `--help` prints cannot
    # drift from the judgement; both `--scope` flags (`instruction add` and `list`) share this one copy.
    scope_choices = list(valid_scopes())

    p_ins = sub.add_parser(
        'instruction', help='Standing user instructions (one account, two states)',
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
                    'makes an instruction reach it. The account has two states -- in force and '
                    'archived -- and only the owner moves an entry off it: `resolve` archives one '
                    '(the sentence stays on the account and stops being read), `rm` takes one away '
                    'text and all, and `revive` puts an archived one back. An entry whose scope has '
                    'run out goes out of sight on its own, with nothing to write.')
    ins_sub = p_ins.add_subparsers(dest='instruction_command')
    p_ia = ins_sub.add_parser('add', help='Append one instruction, verbatim')
    p_ia.add_argument('--text', required=True, help='The sentence, EXACTLY as the user said it')
    p_ia.add_argument('--scope', dest='scope', default='global', choices=scope_choices,
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
    p_il.add_argument('--scope', dest='scope', default=None, choices=scope_choices,
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
    p_ir = ins_sub.add_parser(
        'resolve', help='Archive one instruction (only the owner puts it out of force)')
    p_ir.add_argument('ins_id')
    p_ir.add_argument('--state', required=True, choices=['withdrawn'],
                      help='withdrawn = the entry is archived: it stays on the account and stops '
                           'being read, and the archive writes the state and the moment, nothing '
                           'else. It is the only move `resolve` has, and only the owner makes it -- '
                           'a live entry is not marked with an outcome, and a cycle/node entry goes '
                           'out of sight on its own when its node is completed or its cycle ends')
    p_ir.add_argument('--note', default=None)
    p_ir.add_argument('--project', default=None, help='Project root (default: current directory)')
    p_irm = ins_sub.add_parser(
        'rm', help='Take one instruction off the account, text and all (only the owner removes one)',
        description='The owner takes an entry away: the sentence is printed first, then the entry and '
                    'its text leave the account. What stays is the id and the moment -- the trace the '
                    'ledger merge holds the removal on, so a pull from a machine that still has the '
                    'entry does not bring it back, and so the id is never handed out again. No view '
                    'of the account shows a trace, and this is not a third state. `resolve` is the '
                    'other move: an archived entry keeps its sentence and stops being read.')
    p_irm.add_argument('ins_id')
    p_irm.add_argument('--project', default=None, help='Project root (default: current directory)')
    p_irv = ins_sub.add_parser(
        'revive', help='Put an archived instruction back in force (only the owner puts one back)',
        description='The owner undoes an archive: the entry goes back to `live`, and the moment the '
                    'archive wrote is cleared back to the shape an added entry has, so the merged '
                    'record stays one act. An entry that is already live is refused, and so is an id '
                    'the account does not have: a removed entry is off the account for good, and '
                    'putting one back is not how a removal is undone.')
    p_irv.add_argument('ins_id')
    p_irv.add_argument('--project', default=None, help='Project root (default: current directory)')

    p_cfg = sub.add_parser(
        'config',
        help="One setting on one chain: factory default, then global, project, this line and this "
             "node -- the narrowest layer that states it wins (the line's own `parallel` / "
             "`when_shelved` / `spawner` are not settings: `pf cycle policy` writes them)",
        description='Every setting is a knob with a kind, and one chain: this node -> this line -> '
                    'this project -> this machine -> factory default. Narrower layers cover wider '
                    'ones, and the same name may be stated on several layers at once: the '
                    'resolution is deterministic, and `list` / `get` always say which layer the '
                    'value in force came from. The table is the built-in settings plus every knob the '
                    'installed features declare -- `list` prints it, and a name nobody declares is '
                    'refused. Not every knob takes every layer: a core setting takes the project and '
                    'global layers, a feature\'s knob takes all four (`set` names the layers a knob '
                    'takes when it refuses one). The line\'s own policies -- `parallel`, '
                    '`when_shelved`, `spawner` -- are not settings and are not here: they live on '
                    'the line record, written by `pf cycle policy`. '
                    'The two file layers are `~/.pf/config.json` (global: travels with the '
                    'person and the machine, never generated for you) and `<project>/.pf/'
                    'config.json` (travels with the project; `init` puts an empty `{}` there); the '
                    'two narrow ones ride on the cycle record and on one node\'s own '
                    '`config.json`. `set` stores a value and `unset` drops that one layer\'s entry '
                    '-- a switch is turned off by setting `off`, while dropping its entry is '
                    '`unset` (the same split `pf cycle policy` explains). `set` / `unset` write '
                    'exactly one layer: the project layer unless --global / --cycle / --node says '
                    'otherwise.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    cfg_sub = p_cfg.add_subparsers(dest='config_command')
    knob_choices = sorted(knobs.specs())
    p_cfg_list = cfg_sub.add_parser('list',
                                    help='Every knob\'s effective value, and the layer it came from')
    p_cfg_list.add_argument('--project', default=None, help='Project root (default: current directory)')
    p_cfg_list.add_argument('--cycle', default=None,
                            help='Read this line\'s layer too (a cycle id): what that line sees')
    p_cfg_list.add_argument('--node', default=None,
                            help='Read this node\'s layer too (a node id): what that node\'s run '
                                 'sees')
    p_cfg_get = cfg_sub.add_parser('get', help='Print one effective value, and nothing else')
    p_cfg_get.add_argument('key', choices=knob_choices)
    p_cfg_get.add_argument('--project', default=None, help='Project root (default: current directory)')
    p_cfg_get.add_argument('--cycle', default=None,
                           help='Read this line\'s layer too (a cycle id)')
    p_cfg_get.add_argument('--node', default=None,
                           help='Read this node\'s layer too (a node id)')
    p_cfg_set = cfg_sub.add_parser('set',
                                   help='Write one value into exactly one layer (the project layer '
                                        'by default)')
    p_cfg_set.add_argument('key', choices=knob_choices)
    p_cfg_set.add_argument('value')
    set_layer = p_cfg_set.add_mutually_exclusive_group()
    set_layer.add_argument('--global', dest='global_', action='store_true',
                           help='Write the global layer (~/.pf/config.json) instead of the project one')
    set_layer.add_argument('--cycle', default=None,
                           help='Write this line\'s layer instead (a cycle id): the value rides on '
                                'the cycle record, whose one writer takes it')
    set_layer.add_argument('--node', default=None,
                           help='Write this node\'s layer instead (a node id): '
                                '`.pf/nodes/<id>/config.json`')
    p_cfg_set.add_argument('--project', default=None, help='Project root (default: current directory)')
    p_cfg_unset = cfg_sub.add_parser('unset',
                                     help='Drop one override, letting the next wider layer take over '
                                          'again')
    p_cfg_unset.add_argument('key', choices=knob_choices)
    unset_layer = p_cfg_unset.add_mutually_exclusive_group()
    unset_layer.add_argument('--global', dest='global_', action='store_true',
                             help='Drop it from the global layer instead of the project one')
    unset_layer.add_argument('--cycle', default=None,
                             help='Drop it from this line\'s layer instead (a cycle id)')
    unset_layer.add_argument('--node', default=None,
                             help='Drop it from this node\'s layer instead (a node id)')
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
                    '`insert_node` (a node was just put into the graph and nothing was opened: '
                    'commit and push, then run `next --spawn` again and the pick decides), '
                    '`wait` (read its note / close_with and run that one command), `ask_user`, '
                    '`stop_pipeline`, `no_cycle` (there is no cycle yet: opening one takes a '
                    "human's intent). An `open_node` never reaches you -- it is the tool pinning "
                    "the next node itself, and what you read is that slot's `dispatch`. "
                    'The returned action NEVER carries commands to run: the tool runs them.',
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
        help='Start the session for the node this cycle is holding (the prompt was written when '
             'that node was pinned)',
        description='Dispatch and nothing else: read the node this cycle holds, then start the '
                    'session whose prompt the step that pinned it already wrote at '
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
    p_mode.add_argument('--local', dest='mode', action='store_const', const='local', default='local',
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
        help='Fork one node\'s parallel-mode worktree: `.pf/worktrees/<short id>` on branch '
             '`pf/<short id>`',
        description='The parallel mode\'s fork, as one command (the SKILL\'s "parallel: worktree"): refuses '
                    'while the main tree is dirty (another line may be mid-merge-back) and while '
                    'the node\'s short id is already taken (worktree directory or branch '
                    '`pf/<short id>` exists); otherwise `git worktree add`s the checkout and writes '
                    '`.pf/meta/FORK-<short id>.json` -- the record the closing call gates on, so a '
                    'node whose merge-back never finished cannot be settled. Works from a worktree '
                    'cwd too: the main tree is resolved the same way the record commands do it.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p_fork.add_argument('node', help='The DAG node this worktree is for; its first 8 characters '
                                     'name the worktree directory and the branch')

    args = parser.parse_args(argv)
    route(args)


if __name__ == '__main__':
    main()
