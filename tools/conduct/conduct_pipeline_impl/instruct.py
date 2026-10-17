"""The standing-instructions account and the scope judgement.

`instruction_is_live` reads an entry's state, `entry_scope` / `scope_binding` resolve its scope,
`instruction_in_scope` judges visibility, and `live_instructions` lists what is visible.
"""

from __future__ import annotations

from pathlib import Path

from .store import active_cycles, load_dag, load_instructions, load_state


# ── instructions ─────────────────────────────────────────────────────────────

def instruction_is_live(ins: dict) -> bool:
    return ins.get('state', 'live') == 'live'


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


def _scope_facts(project: Path) -> tuple:
    """The two facts a scope question needs, read once per call: the DAG's nodes, the active cycles."""
    nodes = load_dag(project).get('nodes') or {}
    active = {c.get('id') for c in active_cycles(load_state(project))}
    return nodes, active


def instruction_in_scope(ins: dict, nodes: dict, active: set, cycle: str | None = None) -> bool:
    """Can this entry be seen from the step being built?

    `global` always can. `node` can while the node it was recorded on is not `completed`; `cycle` can
    while the cycle it was recorded in is still active. An entry with nothing to measure against
    stays visible, and so does anything the tool cannot judge -- a node the DAG does not know, a
    scope it does not recognise: the direction of the error is "noisy", never "silently dropped".

    `cycle` is the cycle the caller is working on, and passing it turns the cycle scope into an
    exact comparison. Without it -- and this is why it exists -- the judgement degrades to "is the
    bound cycle still active", which is true of EVERY cycle a project is running at once: with two
    of them side by side, one line's rhythm instruction would cap the other line's run. The caller
    is closing a node of a known cycle, so that fact is at hand and no pointer has to be guessed.
    """
    kind, bound = scope_binding(ins)
    if kind == 'node':
        return (nodes.get(bound) or {}).get('status') != 'completed'
    if kind == 'cycle':
        if cycle is not None:
            return bound == cycle
        return bound in active
    return True


def live_instructions(project: Path, target: str | None = None,
                      cycle: str | None = None) -> list:
    """Instructions visible from the step being built, newest last.

    `target` filters to one delivery channel; the scope filter drops entries whose
    scope no longer reaches here -- a `node` entry whose node is already `completed`, a `cycle` entry
    whose cycle is no longer active. Both channels are filtered the same way: a stale rhythm
    instruction must not cap a later run either.

    `cycle` names the cycle the caller is working on, which is what keeps a `cycle` entry inside
    its own line when a project runs two of them (see `instruction_in_scope`).
    """
    nodes, active = _scope_facts(project)
    out = []
    for ins in load_instructions(project).get('instructions', []):
        if not instruction_is_live(ins):
            continue
        if target and (ins.get('target') or 'stage') != target:
            continue
        if not instruction_in_scope(ins, nodes, active, cycle=cycle):
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
