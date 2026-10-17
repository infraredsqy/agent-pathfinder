"""Everything behind `state-manage.py`, split by responsibility.

`state-manage.py` is the entry point: the CLI a launcher runs (through `pf`, which also reaches it
by path) and the module the test harness loads in process. It owns the argument
parser and nothing else; the machinery is here:

    store.py             one root -- `.pf/` -- and the four kinds of thing under it, and how they
                         are read: where it is, how it is read, how it is written
    pipeline_loader.py   how `conduct_pipeline.py` is loaded by path
    cycles.py            the `cycle` group: one line of work -- create / start / rename / status /
                         policy / list / current / show / hold / close / retry
    instruct.py          the `instruction` group: the standing-instructions account, and what is
                         visible from where
    nextcmd.py           `next` -- the one command that drives the pipeline -- and `dispatch`, the
                         standalone half of its spawn tail; also the closing call's by-node
                         resolution
    maintenance.py       the repair-and-tidy levers (clean-temp)
    initledger.py        `init` -- the one setup act: write `.pf/.gitignore`, `.pf/.gitattributes` and
                         `.pf/AGENTS.md`; and `register`, which wires the merge driver into a
                         project's git config (init does it too)
    fork.py              the parallel-mode fork: `fork-worktree`, one worktree per node, and the
                         FORK record the close gates on
    config.py            this machine's and this project's settings: the built-in defaults, the
                         global and project overrides, and how the three resolve into one value
    session.py           the `session` group: which conversation produced a node, and which ones ran
                         it -- read from `.pf/meta/sessions/`, plus the one-shot import of the ids an
                         older record left behind

This package is loaded by PATH, never by name: `state-manage.py` registers it under the name below
on every load, so a caller that loads a fresh `state-manage.py` per project -- the test harness does
exactly that -- gets its own `store` (and its own `bind_paths`) with it. See the bootstrap at the top
of `state-manage.py` for why that matters.
"""

from . import store  # noqa: F401
from . import pipeline_loader  # noqa: F401
from . import cycles  # noqa: F401
from . import instruct  # noqa: F401
from . import nextcmd  # noqa: F401
from . import maintenance  # noqa: F401
from . import initledger  # noqa: F401
from . import fork  # noqa: F401
from . import config  # noqa: F401
from . import session  # noqa: F401

#: Everything `state-manage.py` re-exports: one flat surface, exactly what this file had at module
#: scope before the split. A command that is not in this list is not reachable from the module a
#: caller loads.
#:
#: The two roots are deliberately NOT here. They are `store`'s and are read at call time;
#: re-exporting them would hand a caller a snapshot that `bind_paths` cannot move.
__all__ = [
    # the layers themselves, for a caller that wants to poke at one of them
    'store', 'pipeline_loader', 'cycles', 'instruct', 'nextcmd', 'maintenance',
    'initledger', 'fork', 'config',

    # store: the read/write layer over the ledger (`.pf/`) and the doors off it
    'VALID_NODE_RESULTS', 'ISO_FMT', 'PARALLEL_DEFAULT', 'LEDGER_NAME', 'INIT_MARKERS',
    'ledger_dir_for', 'meta_dir_for', 'nodes_dir_for', 'require_initialized',
    '_now', '_atomic_write', '_read_json', '_read_state', '_write_state', '_find_cycle',
    '_signal_path', '_read_signal', '_write_signal', 'bind_paths', '_cycles_holding_node',
    '_closed_steps_of_node', '_holding_of', '_holding_ids', '_set_holding',
    # store: the cycle-status vocabulary
    'CYCLE_VALID_STATUSES',

    # pipeline_loader: the by-path load of conduct_pipeline.py
    '_load_pipeline',

    # cycles: the `cycle` group, the tag vocabulary, and the policies a line carries
    'VALID_TAGS', '_parse_cycle_tags',
    'POLICY_DEFAULTS', 'POLICY_VALUES', 'POLICY_NUMBERS', 'POLICY_COUNTS',
    'POLICY_WORDS', 'POLICY_WORD_CHOICES', 'cycle_policy',
    'cmd_cycle_create', 'cmd_cycle_start', 'cmd_cycle_rename', 'cmd_cycle_status',
    'cmd_cycle_policy', 'cmd_cycle_list', 'cmd_cycle_current', 'cmd_cycle_show',
    'cmd_cycle_hold', 'cmd_cycle_close', 'cmd_cycle_retry', '_dispatch_cycle',

    # nextcmd: the closing call's by-node resolution
    'resolve_cycle_for_node', 'describe_holders',

    # instruct: the instruction account, the scope vocabulary, and what is visible from where
    'VALID_SCOPES', 'VALID_TARGETS', 'VALID_INSTRUCTION_STATES',
    '_scope_rules',
    '_scope_from_flag', '_instructions_path', '_read_instructions', '_write_instructions',
    '_retired', '_is_live', '_cycle_of_node', '_position', '_cycle_statuses', '_dag_nodes',
    '_visible_from_here', '_ambiguous_cycle_view', '_fold',
    '_instruct_empty_lines', '_nothing_visible_lines', 'SCOPE_WORDS', '_render_constraints',
    'cmd_instruction_add', 'cmd_instruction_list', 'cmd_instruction_resolve',
    '_dispatch_instruction',

    # nextcmd / maintenance / init
    'cmd_next', 'cmd_dispatch', 'cmd_clean_temp', 'cmd_init', 'cmd_register',

    # fork: the parallel-mode fork, its worktree, and the record the close gates on
    'node_short_id', 'worktree_path', 'worktree_branch', 'fork_record_path',
    'cmd_fork_worktree', 'gate_close_on_fork',

    # config: the built-in defaults, the two override layers, and one resolved reading
    'CONFIG_NAME', 'CONFIG_SPEC', 'LAYERS', 'LAYER_LABELS',
    'global_config_path', 'project_config_path', 'layer_path', 'default_of',
    'read_layer', 'resolve_config', 'config_sources', 'write_value',
    'cmd_config_list', 'cmd_config_get', 'cmd_config_set', 'cmd_config_unset',
    '_dispatch_config',

    # session: the node's own conversations -- the note under `.pf/meta/sessions/`, and the import
    'cmd_session_show', 'cmd_session_list', 'cmd_session_import', '_dispatch_session',
]


def bind_paths(ledger_dir) -> None:
    """Point this copy of the tool at one project's `.pf/` (in-process callers only).

    Everything the tool writes for a project lives under that one directory. The CLI's paths come
    from `Path.cwd()` at import, because it is started with the cwd set to the project. A caller that
    loads this module in process instead has to say which project it means, and this is that one
    sentence: `store` reads its bindings at call time, so one call moves the whole module.
    """
    store.bind_paths(ledger_dir)


# One list, resolved here: every name above is taken from whichever layer defines it (the layer
# modules are already in this namespace from the `from . import` lines at the top, so they are
# skipped). Writing the `from .x import y` lines out instead would be a second list to keep in step
# with `__all__`, and a name that appears in one and not the other is exactly the drift this split
# has to avoid -- so a name nobody defines is a loud ImportError at load time, not a missing
# attribute later.
_LAYERS = (store, pipeline_loader, cycles, instruct, nextcmd, maintenance, initledger,
           fork, config, session)

for _name in __all__:
    if _name in globals():
        continue
    for _layer in _LAYERS:
        if hasattr(_layer, _name):
            globals()[_name] = getattr(_layer, _name)
            break
    else:
        raise ImportError(f'conduct_state.__all__ names {_name!r}, which no layer defines')
