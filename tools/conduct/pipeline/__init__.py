"""Everything behind `conduct_pipeline.py`, split by responsibility.

`conduct_pipeline.py` is the entry point: the module the CLI runs, the module `handoff.py`,
`state-manage.py` and the test harness load by path -- fresh every time -- and the module that owns
the child-process seam (`_cmd` / `_dag` / `_as_text` / `_run` / `CMD_TIMEOUT` / `SPAWNER_SCRIPT`),
because those are what a caller patches. The machinery is here:

    seam.py      the live link back to the entry module's globals (see its docstring)
    store.py     the read layer over one project's .pf/:
                 load_state / load_signal / load_dag / load_instructions, the cycle lookups, the
                 knob resolution and the readings a ticket is asked for, `ledger_dir` / `meta_dir`
    instruct.py  the standing-instructions account and the one scope judgement ("what can be seen
                 from the step being built")
    nodes.py     the DAG ready set, the tag filter, and "which node next"
    prompts.py   what a dispatch prompt says (the invocation line, and the node's text)
    insertion.py the core mechanism that puts a node into the graph: mint its uuid, write its spec,
                 wire its edges -- a graph edit only, never a session
    feature_host.py  the generic feature host: the Context a feature is handed, and the two questions
                 asked of the folders under `features/` (an insertion now, a gate at the dispatch)
    actions.py   state -> an action package (dispatch / wait / ask_user / stop)
    planner.py   `plan()`: the pure decision, including the node's own close
    writer.py    `apply()` / `record()`: run the commands, and enforce the apply contract

There is no stage table and no per-round file: one cycle picks one DAG node at a time, and how that
node splits its own work is the session's business. No feature is a part of this package: a feature is
a folder under `features/` -- a declaration and an implementation, with the declaration naming the
dedupe tag and the method card the outside sees -- and the names this core feeds to every reader come
from those declarations, not from here.

This package is loaded by PATH, never by name: `conduct_pipeline.py` registers it under `pf_pipeline`
on every load, so a caller that loads a fresh `conduct_pipeline.py` gets a fresh package with it. See
the bootstrap in `conduct_pipeline.py`.
"""

from __future__ import annotations

from . import seam  # noqa: F401
from . import store  # noqa: F401
from . import instruct  # noqa: F401
from . import nodes  # noqa: F401
from . import prompts  # noqa: F401
from . import insertion  # noqa: F401
from . import feature_host  # noqa: F401
from . import actions  # noqa: F401
from . import planner  # noqa: F401
from . import writer  # noqa: F401

#: Everything `conduct_pipeline.py` re-exports: one flat surface, exactly what this module had at module
#: scope before the split, minus the names the entry keeps for itself (its own paths, the seam
#: functions, and the CLI: `ACTIONS`, `_render_contract`, `main`). It is the only export list in this
#: package -- the layers keep none of their own, so there is no second list to drift from this one.
__all__ = [
    # the layers themselves, for a caller that wants to poke at one of them
    'seam', 'store', 'instruct', 'nodes', 'prompts', 'insertion', 'feature_host', 'actions',
    'planner', 'writer',

    # store: the read layer, and the door to the session notes under `.pf/meta/sessions/`
    'ledger_dir', 'meta_dir', 'load_state', 'load_signal', 'load_dag',
    'load_instructions',
    'active_cycles', 'find_cycle', 'cycle_path',
    'POLICY_DEFAULTS', 'POLICY_VALUES', 'POLICY_NUMBERS', 'POLICY_COUNTS',
    'POLICY_WORDS', 'POLICY_WORD_CHOICES', 'cycle_policy',
    '_holding_of', '_holding_ids',
    'session_note', 'session_notes', 'note_session', 'SESSION_NOTE_PY',

    # store: the settings -- one knob read through the whole chain, and the readings a ticket's
    # form asks for (both merged from the features' declarations on the way)
    'CONFIG_NAME', 'global_config_path', 'project_config_path',
    'knob_specs', 'resolve_knob', 'knob_sources', 'ticket_readings',

    # feature_host: the Context a feature is handed, and the two questions the core asks of it
    'Context', 'insertions', 'gates', 'registry', 'FEATURE_REGISTRY_PY',

    # instruct: the account, and the scope judgement
    'instruction_is_live', 'SCOPES', 'DEFAULT_SCOPE', 'entry_scope',
    'scope_binding', '_scope_facts', 'instruction_in_scope', 'live_instructions',

    # nodes: the ready set, the tag filter, the pick
    'dag_ready_nodes', 'node_in_tag_scope', 'tag_blocked_lines', 'cross_line_reason',
    '_rank_ready_nodes', 'node_holders', '_free_ready', 'ranked_ready_nodes',
    'tag_blocked_ready_nodes', 'holding_ready_nodes', 'blocked_reason', 'pick_node', 'node_spec_path',

    # prompts: what a dispatch prompt says
    'write_prompt_file', 'build_prompt_text', 'mode_for', 'started_marker', 'launch_prompt_path',

    # insertion: the core mechanism behind every inserted node (mint the id, write the spec, wire
    # the edges) -- one implementation, whatever the source
    'INSERT_ACTION', 'NEW_NODE', 'ADD_MARKER', 'insert', 'mint', 'read_new_node_id', 'write_spec',
    'is_add_slot', 'fill', 'fill_all',

    # actions: state -> an action package, and an action package as the caller reads it
    '_ask', '_dispatch_many', '_insert_node', '_insertions_due', '_pick_insertions_due',
    'attention_items', '_ticket_wait',
    '_retry_wait', '_sessions_wait', '_close_commands', '_stop_action', 'render_action',

    # planner: plan() and the node's own close
    'stop_criterion', 'plan', '_close_own_node', '_named_node_outside_scope',

    # writer: apply() / record()
    'MAX_SETTLE_STEPS', '_snapshot', 'resolve_spawner', 'spawn_session', 'clean_launch_dir',
    'apply', 'record',
]

# One list, resolved here: every name above is taken from whichever layer defines it (the layer
# modules are already in this namespace from the `from . import` lines at the top, so they are
# skipped). Writing the `from .x import y` lines out instead would be a second list to keep in step
# with `__all__`, and a name that appears in one and not the other is exactly the drift this split
# has to avoid -- so a name nobody defines is a loud ImportError at load time, not a missing
# attribute later.
_LAYERS = (store, instruct, nodes, prompts, insertion, feature_host, actions, planner, writer)

for _name in __all__:
    if _name in globals():
        continue
    for _layer in _LAYERS:
        if hasattr(_layer, _name):
            globals()[_name] = getattr(_layer, _name)
            break
    else:
        raise ImportError(f'pipeline.__all__ names {_name!r}, which no layer defines')
