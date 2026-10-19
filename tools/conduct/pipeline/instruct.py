"""The standing-instructions account and the scope judgement.

`instruction_is_live` reads an entry's state, `entry_scope` / `scope_binding` resolve its scope,
`instruction_in_scope` judges visibility, and `live_instructions` lists what is visible.

That judgement is deliberately THE one: `state/instruct.py` -- what `pf instruction list`
runs -- loads this module by path and asks it the same question with the reader's own place, rather
than keeping a second reading of the rule. Two judgements are how one instruction comes to be read
two ways.
"""

from __future__ import annotations

from pathlib import Path

from .store import _state_module, active_cycles, load_dag, load_instructions, load_state


# ── instructions ─────────────────────────────────────────────────────────────

#: The word `state` is read as when it is left out. The vocabulary's one source is the writing side
#: (`state/instruct.py`'s `LIVE_STATE`), and this side borrows it through the loader -- spelling the
#: same word in two places is how one side changes and the other stops understanding it.
LIVE_STATE = _state_module('instruct').LIVE_STATE


def instruction_is_live(ins: dict) -> bool:
    return ins.get('state', LIVE_STATE) == LIVE_STATE


# ── scope: where an instruction can be seen from ───────────────
# `scope` is the entry's main attribute, the way a declaration's scope is: it says WHERE the entry
# is visible from, and nothing about whether it is still in force (`state` owns that). `global` is
# visible anywhere; `cycle` only while the cycle it was recorded in is active;
# `node` only while the node it was recorded in is not `completed`.
SCOPES = ('global', 'cycle', 'node')
#: No field at all. The noisy direction on purpose: a constraint read as narrow evaporates
#: silently, while one read as global only costs a line in a prompt.
DEFAULT_SCOPE = 'global'


def entry_scope(entry: dict) -> str:
    """The scope of one account entry.

    An entry carrying no `scope` is `global`: an unreadable entry must never come back as "nothing to
    obey here".
    """
    scope = entry.get('scope')
    return scope if scope in SCOPES else DEFAULT_SCOPE


def scope_binding(entry: dict) -> tuple:
    """What the entry's scope is measured against: `('node', <id>)`, `('cycle', <id>)`, `(None, None)`.

    Both bindings are filled in by `instruct add` from the pipeline state at the moment it recorded
    the sentence -- never by the caller, who is not supposed to know where it stands. An entry whose
    binding is empty (an old account, or one added outside a cycle) is measured against nothing and
    stays visible.
    """
    scope = entry_scope(entry)
    if scope == 'node' and entry.get('node'):
        return ('node', entry['node'])
    if scope == 'cycle' and entry.get('cycle'):
        return ('cycle', entry['cycle'])
    return (None, None)


def _scope_facts(project: Path, dag: dict | None = None) -> tuple:
    """The two facts a scope question needs, read once per call: the DAG's nodes, the active cycles.

    `dag` is the graph the caller has already read: the close reads the whole graph once, and the
    scope judgement reads the copy handed down instead of reading the disk again for itself. Left
    out, it reads its own (the other entries have no ready copy).
    """
    nodes = (dag if dag is not None else load_dag(project)).get('nodes') or {}
    active = {c.get('id') for c in active_cycles(load_state(project))}
    return nodes, active


def instruction_in_scope(ins: dict, nodes: dict, active: set, cycle: str | None = None,
                         node: str | None = None) -> bool:
    """Can this entry be seen from the step being built?

    `global` always can -- an entry with nothing to measure against, or a scope this reader does not
    know, stays visible too: the direction of the error is "noisy", never "silently dropped".

    The other two scopes are read against **where the reader stands**, and that is what the last two
    arguments are: `cycle` is the line the reader works on, `node` is the node in its hand. Given
    them, the judgement is an exact comparison -- the entry's own binding against the reader's place
    -- which is the whole rule: a session running
    `instruction list` and the close settling a node stand in the same place, so they read the same
    constraints. A constraint bound to another node is not read at this one.

    Left out, each half falls back to the status of what the entry is bound to: a `node` entry while
    its node is not `completed`, a `cycle` entry while its cycle is still active. That is the wide
    reading, and it exists for a reader whose place nothing can pin down -- with two active cycles
    side by side and no node named, "its cycle is still active" is true of every line the project
    runs, so the entry is read rather than dropped. Missing a real constraint is the expensive
    mistake; carrying an extra line is not.
    """
    kind, bound = scope_binding(ins)
    if kind == 'node':
        if node is not None:
            return bound == node
        return (nodes.get(bound) or {}).get('status') != 'completed'
    if kind == 'cycle':
        if cycle is not None:
            return bound == cycle
        return bound in active
    return True


def live_instructions(project: Path, target: str | None = None, cycle: str | None = None,
                      node: str | None = None, dag: dict | None = None) -> list:
    """Instructions visible from the step being built, newest last.

    `target` filters to one delivery channel; the scope filter drops entries whose scope no longer
    reaches here. Both channels are filtered the same way: a stale rhythm instruction must not cap a
    later run either.

    `cycle` / `node` are the reader's own place (see `instruction_in_scope`): the close passes the
    line it is settling and the node it is settling, which is how a boundary instruction bound to
    some other node stops capping this one.

    `dag` is the graph the caller already has: `stop_criterion` hands it in on the close, so the
    scope judgement does not read the disk a second time for the same run.
    """
    nodes, active = _scope_facts(project, dag)
    out = []
    for ins in load_instructions(project).get('instructions', []):
        if not instruction_is_live(ins):
            continue
        if target and (ins.get('target') or 'stage') != target:
            continue
        if not instruction_in_scope(ins, nodes, active, cycle=cycle, node=node):
            continue
        out.append(ins)
    return out


# How an instruction reaches the session that has to obey it: the session runs
# `state-manage.py instruct list` and reads what is visible from where the pipeline stands. Nothing
# is injected into a prompt any more.
#
# There is deliberately no "write `done` when the scope ends" step either. Visibility is a pure
# derivation -- `scope` + the bound node/cycle + the DAG as it stands -- so an entry that has gone
# out of sight is simply not read out, and `state: live` keeps its one meaning: declared, never
# withdrawn. Writing an ending at the close would put "somebody has to remember to run this at that
# moment" back into the guarantee, and it would leak the first time a node was closed by hand, a
# cycle was abandoned, or the closing path was never reached -- while buying nothing.
