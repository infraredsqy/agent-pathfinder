"""Everything behind `conduct_pipeline.py`, split by responsibility.

`conduct_pipeline.py` is the entry point: the module the CLI runs, the module `handoff.py`,
`state-manage.py` and the test harness load by path -- fresh every time -- and the module that owns
the child-process seam (`_cmd` / `_dag` / `_as_text` / `_run` / `CMD_TIMEOUT` / `SPAWN_PS1`),
because those are what a caller patches. The machinery is here:

    seam.py      the live link back to the entry module's globals (see its docstring)
    store.py     the read layer over one project's .pf/:
                 load_state / load_signal / load_dag / load_instructions, the cycle lookups,
                 `ledger_dir` / `meta_dir`
    instruct.py  the standing-instructions account and the one scope judgement ("what can be seen
                 from the step being built")
    nodes.py     the DAG ready set, the tag filter, and "which node next"
    prompts.py   what a dispatch prompt says (the invocation line, and the node's text)
    maintenance.py the maintenance channel: the reading, the lines it is measured
                 against, and the graph edit that puts a maintenance node in between
    actions.py   state -> an action package (dispatch / wait / ask_user / stop / fail_hard)
    planner.py   `plan()`: the pure decision, including the node's own close
    writer.py    `apply()` / `record()`: run the commands, and enforce the apply contract

There is no stage table and no per-round file: one cycle picks one DAG node at a time, and how that
node splits its own work is the session's business.

This package is loaded by PATH, never by name: `conduct_pipeline.py` registers it under the name below
on every load, so a caller that loads a fresh `conduct_pipeline.py` gets a fresh package with it. See
the bootstrap in `conduct_pipeline.py`.
"""

from __future__ import annotations

from . import seam  # noqa: F401
from . import store  # noqa: F401
from . import instruct  # noqa: F401
from . import nodes  # noqa: F401
from . import prompts  # noqa: F401
from . import maintenance  # noqa: F401
from . import actions  # noqa: F401
from . import planner  # noqa: F401
from . import writer  # noqa: F401

#: Everything `conduct_pipeline.py` re-exports: one flat surface, exactly what this module had at module
#: scope before the split, minus the names the entry keeps for itself (its own paths, the seam
#: functions, and the CLI: `ACTIONS`, `_render_contract`, `_render`, `main`).
__all__ = [
    # the layers themselves, for a caller that wants to poke at one of them
    'seam', 'store', 'instruct', 'nodes', 'prompts', 'maintenance', 'actions', 'planner', 'writer',

    # store: the read layer, and the door to the session notes under `.pf/meta/sessions/`
    'ledger_dir', 'meta_dir', 'load_state', 'load_signal', 'load_dag',
    'load_instructions',
    'active_cycles', 'find_cycle', 'cycle_path',
    'POLICY_DEFAULTS', 'POLICY_VALUES', 'POLICY_NUMBERS', 'POLICY_COUNTS',
    'POLICY_WORDS', 'POLICY_WORD_CHOICES', 'cycle_policy',
    '_holding_of', '_holding_ids',
    'session_note', 'session_notes', 'note_session', 'SESSION_NOTE_PY',

    # instruct: the account, and the scope judgement
    'instruction_is_live', 'SCOPES', 'DEFAULT_SCOPE', 'entry_scope',
    'scope_binding', '_scope_facts', 'instruction_in_scope', 'live_instructions',

    # nodes: the ready set, the tag filter, the pick
    'dag_ready_nodes', 'node_in_tag_scope', 'tag_blocked_lines', 'cross_line_reason',
    '_rank_ready_nodes', 'node_holders', '_free_ready', 'ranked_ready_nodes',
    'tag_blocked_ready_nodes', 'holding_ready_nodes', 'blocked_reason', 'pick_node', 'node_spec_path',

    # prompts: what a dispatch prompt says
    'prompt_for', 'write_prompt_file', 'build_prompt_text', 'mode_for', 'started_marker',

    # maintenance: the maintenance channel
    'POLICY_KEYS', 'READING_KEYS', 'MAINTENANCE_ACTION', 'METRICS', 'crossed', 'reading_of',
    'thresholds_for', 'asked_readings', 'pool_weight', 'insertion', 'read_new_node_id',
    'maintenance_spec', 'maintenance_title', 'substitute',

    # actions: state -> an action package
    '_ask', '_dispatch', '_dispatch_many', '_maintenance_due', 'attention_items', '_ticket_wait',
    '_retry_wait', '_sessions_wait', '_close_commands', '_stop_action',

    # planner: plan() and the node's own close
    'stop_criterion', 'plan', 'node_mode', '_close_own_node', '_named_node_outside_scope',

    # writer: apply() / record()
    'MAX_SETTLE_STEPS', '_snapshot', 'spawn_session', 'clean_launch_dir', 'apply', 'record',
]

# One list, resolved here: every name above is taken from whichever layer defines it (the layer
# modules are already in this namespace from the `from . import` lines at the top, so they are
# skipped). Writing the `from .x import y` lines out instead would be a second list to keep in step
# with `__all__`, and a name that appears in one and not the other is exactly the drift this split
# has to avoid -- so a name nobody defines is a loud ImportError at load time, not a missing
# attribute later.
_LAYERS = (store, instruct, nodes, prompts, maintenance, actions, planner, writer)

for _name in __all__:
    if _name in globals():
        continue
    for _layer in _LAYERS:
        if hasattr(_layer, _name):
            globals()[_name] = getattr(_layer, _name)
            break
    else:
        raise ImportError(f'conduct_pipeline_impl.__all__ names {_name!r}, which no layer defines')
