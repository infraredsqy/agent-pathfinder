"""The knobs: one name, one chain, and where an effective value came from.

A **knob** is one named setting this tree reads. There are four kinds and no others: `number` (a
decimal), `integer` (a positive whole number), `word` (one of a small vocabulary, when the knob names
one), and `switch` (`on` / `off`). Which knobs exist is two tables merged: `BUILTIN` below -- the
settings the core itself needs -- and whatever the installed plugins declare, which `specs` takes
from `pipeline/plugin_registry.py` and merges in. No plugin's name appears in this file: a
plugin's knobs arrive as data, and the merge is what makes one name reachable from every door
(writing-side validation, `--help`'s candidates, the ticket form, the doctor).

**Narrow beats wide.** A value lives on one chain, and the first layer that states it wins:

    node      `<project>/.pf/nodes/<id>/config.json`   one node's own run
    cycle     the cycle record's `policies`            one line of work
    project   `<project>/.pf/config.json`              travels with the project
    global    `~/.pf/config.json`                      travels with this machine
    factory   `BUILTIN` / the declaration's `default`  where it lands when nobody says anything

The same name on several layers at once is legal and resolves deterministically. What every reading
has to carry is the pair -- **the effective value and the layer it came from** -- because "I set it,
why is it not in force" is the one question this chain owes an answer to: `resolve` hands back the
pair, and the labels the command line prints come from `source_label`.

**Which layers a knob takes is the knob's own cell** (`layers`), not a property of the chain. A
built-in *setting* is how this project and this machine behave -- `max_agents` is not a property of
one line or of one node's run, and neither reader asks those layers -- so it takes the two wide ones
only (`SETTING_LAYERS`). A knob a plugin declares is read through the whole chain by the plugin
host, so it takes all four unless its own declaration narrows the set (`layers`, copied in by
`plugin_registry.knob_specs`). A value hand-written into a layer the knob does not take is
*reported* when that layer file is read and never quietly ignored: a setting nobody reads is exactly
the complaint this table exists to end. The line's own three policies (`parallel` / `when_shelved` /
`spawner`) are not knobs at all -- they live on the line record, and `state/cycles.py` owns them.

`worktree_mode`'s factory value is a condition rather than a constant (`fork` in a git project, `off`
otherwise), so its cell states no default and `default_of` computes it. A knob with no default at all
(a plugin's threshold) has no factory value: "not configured" is its resting state, and it reads as
`None` until some layer states it.

The two write-side rules that are not obvious:

  * **A layer file that cannot be read stops the call.** A mis-written setting quietly falling back
    to the factory value makes that knob look "never configured" while it is in fact configured
    wrongly -- so `read_layer` prints what is wrong and exits. The one exception is the cycle layer:
    it is a *state record*, and a state record is read tolerantly (the pipeline's own rule), so a
    value nobody can parse is simply not stated.
  * **`off` is a value for a switch and a clearing word for the other kinds.** That rule belongs to
    the writing command line (`cycle policy`), not here: `parse` states what a value of each kind is,
    and clearing means "drop the layer's entry" (`write_layer(..., value=None)`), which is the same
    thing `unset` does.

This file is loaded by path from the pipeline package too (which borrows `specs`, `resolve_knob`,
`knob_sources`, `ticket_readings` and `shadowed` through `pipeline/store.py`), so it keeps its imports
inside the `state` package and takes the one foreign file (`plugin_registry.py`) by path.
"""

from __future__ import annotations

import contextlib
import math
import sys
from pathlib import Path

from .store import (
    _atomic_write,
    _ledger,
    _ledger_lock,
    _read_json,
    _read_state,
    ledger_dir_for,
    nodes_dir_for,
)

#: The override layers, narrowest first -- the order a value is looked up in. The factory default is
#: not a layer: it is what is left when none of these states the name.
LAYERS = ('node', 'cycle', 'project', 'global')

#: The layers a built-in *setting* takes. A setting is how this project or this machine behaves, and
#: the readers of these three ask no narrower layer: a value written on one node's own file, or on
#: one line's record, would be a value nothing could ever be in force from. `state/knobs.py` is where
#: that is refused out loud rather than ignored.
SETTING_LAYERS = ('project', 'global')

#: Where an effective value came from, as the command line prints it for a human. The two layers that
#: are records rather than files get their own label at printing time (`source_label`), because their
#: name is an identity (which node, which line) and not a path.
LAYER_LABELS = {
    'builtin': 'factory default',
    'global': f'global ~/{_ledger().LEDGER_NAME}/{_ledger().CONFIG_NAME}',
    'project': f'project {_ledger().LEDGER_NAME}/{_ledger().CONFIG_NAME}',
}

#: The settings the core itself reads. One cell per knob: `kind` is `parse`'s whole dispatch,
#: `default` is the factory value (`None` means "no factory value" -- see `default_of`), `values` is
#: the vocabulary a word knob names (absent means "any word"), `layers` is which override layers may
#: state it (see `SETTING_LAYERS`), and `summary` is what `--help` prints. A plugin's knobs are
#: merged on top of these by `specs`, and a built-in name always keeps its cell: a broken declaration
#: must never be able to change what the core's own settings mean (`shadowed` is where a declaration
#: that tried is reported).
BUILTIN = {
    'max_agents': {
        'kind': 'integer',
        'default': 3,
        'layers': SETTING_LAYERS,
        'summary': 'how many subagents one session may have in flight at once',
    },
    'test_tier': {
        'kind': 'word',
        'values': ('full', 'minimal'),
        'default': 'full',
        'layers': SETTING_LAYERS,
        'summary': 'how this project is verified: full runs the whole suite, minimal runs only the '
                   'few checks the project names',
    },
    'worktree_mode': {
        'kind': 'word',
        'values': ('off', 'fork', 'reuse'),
        'default': None,
        'layers': SETTING_LAYERS,
        'summary': 'whether a node gets a worktree of its own: fork by default in a git project, '
                   'off otherwise',
    },
}


# ── the one foreign file this package takes: the plugin declarations ────────

CONDUCT = Path(__file__).resolve().parent.parent


def _registry():
    """`pipeline/plugin_registry.py` loaded by path -- the tree's one scan of installed plugins.

    By path, not by import: this package and the pipeline package are each loaded by path, and
    importing one into the other would wind a cycle. `register` + `reuse` gives every caller in this
    process the same module object, and the registry caches nothing of its own.
    """
    from .store import _loader
    return _loader().load_by_path('pf_plugin_registry',
                                  CONDUCT / 'pipeline' / 'plugin_registry.py',
                                  register=True, reuse=True)


# ── the shape of a value ────────────────────────────────────────────────────

def parse(kind: str, value, values=None):
    """One value of one kind, in the knob's own domain -- or `None` when it is not that kind's value.

    Read side and write side both come through here (a JSON value off disk, a string off the command
    line), so a kind's boundary is written down once. A boolean is never a number: it is the JSON
    spelling of a decision, and reading `true` as 1 has bitten this tree before.
    """
    if kind == 'integer':
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            try:
                value = int(value.strip())
            except ValueError:
                return None
        return value if isinstance(value, int) and value >= 1 else None
    if kind == 'number':
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            try:
                value = float(value.strip())
            except ValueError:
                return None
        if not isinstance(value, (int, float)):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return int(value) if isinstance(value, float) and value.is_integer() else value
    if kind == 'word':
        if not isinstance(value, str):
            return None
        return value if values is None or value in values else None
    if kind == 'switch':
        return value if value in ('on', 'off') else None
    return None


def _parse_cell(spec: dict, value):
    """`parse` for one cell of `specs()`."""
    return parse(spec['kind'], value, spec.get('values'))


def layers_of(spec: dict) -> tuple:
    """The override layers one cell takes, narrowest first -- the cell's `layers`, `LAYERS` if absent.

    A declared cell may name a narrower set (a plugin that wants only the project layer, say); the
    default is the whole chain, because a plugin's knob is read by the host through all of it.
    """
    return tuple(spec.get('layers') or LAYERS)


# ── the tables ──────────────────────────────────────────────────────────────

def specs() -> dict:
    """Every knob this tree knows: `BUILTIN`, with the installed plugins' declarations merged in.

    The merge is the one place two sources meet, so it is where the precedence is decided: a built-in
    name keeps its own cell (a plugin may not redefine what the core's settings mean -- that would be
    a core change discussed on its own), and `shadowed` is where the doctor reports a declaration that
    tried. Plugins' knobs are added in declaration order, for a listing that does not move between
    runs, and each takes the layers its own declaration names (the registry validates that cell and
    copies it in); a declaration naming none takes the whole chain, node and line included, because
    the plugin host reads its knobs through all of it.
    """
    out = {name: dict(cell) for name, cell in BUILTIN.items()}
    for name, cell in _registry().knob_specs().items():
        if name not in out:
            declared = dict(cell)
            declared.setdefault('layers', LAYERS)
            out[name] = declared
    return out


def shadowed() -> list:
    """The declared knobs the built-in table already owns: the merge's one silence, said out loud.

    `specs()` keeps the built-in cell when a declaration claims the same name -- the safe precedence,
    because a broken declaration must never change what a core setting means -- but the declaration
    is then unreachable and its own summary misleading. Nothing else in the tree sees both tables, so
    this door does: `pf doctor` prints these as problems. One sentence per collision, naming both
    sides, and the runtime goes on as before.
    """
    out = []
    for name, cell in _registry().knob_specs().items():
        if name in BUILTIN:
            out.append(f'plugin {cell.get("plugin")!r} declares the knob {name!r}, which is already '
                       f'a built-in setting: the built-in cell stays in force, so the declared one '
                       f'can never be read -- give it a name of its own')
    return out


def default_of(key: str, project: str | None = None):
    """A knob's factory value, or `None` when it has none.

    One cell states a condition instead of a value: `worktree_mode` is `fork` in a git project and
    `off` outside one, and which of those this project is has to be asked at read time. A declared
    default that its kind refuses is treated as no default at all -- the declaration is at fault, and
    the value it states must not leak into the judgement.
    """
    return _default_of(key, specs().get(key) or {}, project)


def _default_of(key: str, spec: dict, project: str | None = None):
    """`default_of` for one cell of an already-read table (the resolver reads the table once)."""
    default = spec.get('default')
    if default is None:
        if key == 'worktree_mode':
            return 'fork' if (ledger_dir_for(project).parent / '.git').exists() else 'off'
        return None
    return _parse_cell(spec, default)


def source_label(source: str, cycle=None, node=None) -> str:
    """The layer a value came from, as one printed label.

    `cycle` may be a cycle record or its id (either way the label carries the id), `node` is a node id;
    both are shortened the way this tree shortens identities elsewhere.
    """
    if source == 'cycle':
        cycle_id = cycle.get('id') if isinstance(cycle, dict) else cycle
        return f'cycle {cycle_id or "?"}'
    if source == 'node':
        return f'node {str(node or "?")[:8]}'
    return LAYER_LABELS.get(source, source)


# ── the layers ──────────────────────────────────────────────────────────────

def global_config_path() -> Path:
    """This machine's layer: `~/.pf/config.json` (the same one `pf config set --global` writes).

    Where it lands is the ledger contract's business (`shared/ledger.py`'s `home_config_path`, where
    the `PF_HOME_DIR` sandbox switch is read too), so the reading side and the writing side resolve
    the same "home".
    """
    return _ledger().home_config_path()


def project_config_path(project: str | None = None) -> Path:
    """This project's layer: `<project>/.pf/config.json`."""
    return _ledger().config_path_of(ledger_dir_for(project))


def node_config_path(project: str | None = None, node: str | None = None) -> Path:
    """One node's layer: `<project>/.pf/nodes/<id>/config.json`."""
    return nodes_dir_for(project) / str(node) / _ledger().CONFIG_NAME


def layer_path(layer: str, project: str | None = None, node: str | None = None) -> Path:
    """Where one *file* layer lives. The cycle layer has no file of its own: it rides on the record."""
    if layer == 'node':
        return node_config_path(project, node)
    if layer == 'project':
        return project_config_path(project)
    if layer == 'global':
        return global_config_path()
    raise ValueError(f'the {layer!r} layer is not a file layer (node / project / global are)')


def cycle_record(project: str | None = None, cycle_id: str | None = None) -> dict:
    """The cycle record this id names, or `{}`. The one step from "which line" to its record."""
    if not cycle_id:
        return {}
    state = _read_state(project)
    for cycle in state.get('cycles') or []:
        if cycle.get('id') == cycle_id:
            return cycle
    return {}


def _cycle_layer(cycle) -> dict:
    """One line's own layer: the values stated on its record, parsed by kind, in record order.

    Read tolerantly, like every other state record: a value the kind does not accept is not stated
    (the closing judgement must not die on a hand-edited record). `cycle` is the record itself -- the
    step from an id to a record is `cycle_record`'s, done by whoever holds an id.
    """
    if isinstance(cycle, str):
        cycle = cycle_record(None, cycle)
    stated = (cycle or {}).get('policies') if isinstance(cycle, dict) else None
    if not isinstance(stated, dict):
        return {}
    out = {}
    for key, value in stated.items():
        spec = specs().get(key)
        if spec is None or 'cycle' not in layers_of(spec):
            continue
        parsed = _parse_cell(spec, value)
        if parsed is not None:
            out[key] = parsed
    return out


def read_layer(layer: str, project: str | None = None, cycle=None, node: str | None = None) -> dict:
    """The knobs one layer states, parsed by kind. A missing file is empty (the normal state).

    A file layer is read strictly: a value that cannot be read stops the call with one sentence, so a
    mis-written setting is never quietly reported as "never configured". The cycle layer is the
    exception (see `_cycle_layer`), because a state record is read tolerantly by this tool's own rule.

    A value in a layer its knob does not take is reported and skipped, never silently folded in: that
    is how a hand-written `max_agents` in a node's own file becomes visible as a value nothing reads.
    """
    if layer == 'cycle':
        return _cycle_layer(cycle)
    path = layer_path(layer, project, node=node)
    data = _read_json(path, str(path))
    if data is None:
        return {}
    if not isinstance(data, dict):
        print(f'Error: {path} must be a JSON object (key -> value)', file=sys.stderr)
        sys.exit(1)
    out = {}
    all_specs = specs()
    for key, value in data.items():
        spec = all_specs.get(key)
        if spec is None:
            print(f'Warning: {path} has {key!r}, which is not a configurable key; skipped '
                  f'(configurable: {", ".join(sorted(all_specs))})', file=sys.stderr)
            continue
        allowed = layers_of(spec)
        if layer not in allowed:
            print(f'Warning: {path} states {key!r} on the {layer} layer, which this knob does not '
                  f'take (it takes: {", ".join(allowed)}); skipped -- nothing could ever be in '
                  f'force from here', file=sys.stderr)
            continue
        parsed = _parse_cell(spec, value)
        if parsed is None:
            print(f'Error: {path} has an invalid {key}: {value!r}', file=sys.stderr)
            sys.exit(1)
        out[key] = parsed
    return out


def resolve(project: str | None = None, cycle=None, node: str | None = None) -> dict:
    """Every knob's effective value: `{key: (value, source)}`, `source` one of the five layer names.

    Widest first, narrowest last, so the layer that states a name last is the one that is in force;
    a knob nobody states rests on its factory value (`source` = `builtin`, which is also the answer
    for a knob that has no factory value at all -- `None` is then the effective value). `cycle` is the
    record itself, never an id (see `cycle_record` for that step), and `node` is a node id.
    """
    stated = []
    for layer in reversed(LAYERS):                 # global, project, cycle, node
        if layer == 'cycle' and not cycle:
            continue
        if layer == 'node' and not node:
            continue
        stated.append((layer, read_layer(layer, project, cycle=cycle, node=node)))
    all_specs = specs()
    out = {key: (_default_of(key, spec, project), 'builtin') for key, spec in all_specs.items()}
    for layer, values in stated:
        for key, value in values.items():
            out[key] = (value, layer)
    return out


def resolve_knob(project: str | None = None, key: str | None = None, cycle=None,
                 node: str | None = None):
    """One knob's effective value alone (`None` when nothing states it and it has no factory value)."""
    return resolve(project, cycle=cycle, node=node).get(key, (None, 'builtin'))[0]


def knob_sources(project: str | None = None, cycle=None, node: str | None = None) -> dict:
    """Which layer each knob's effective value came from: `{key: source}`."""
    return {key: source for key, (_value, source) in resolve(project, cycle=cycle, node=node).items()}


# ── the ticket readings: what a worker is asked for ─────────────────────────

def ticket_readings(project: str | None = None, cycle=None, node: str | None = None) -> list:
    """The reading keys a worker is asked for, in declaration order.

    Narrowed by two things, and both are the declaration's own data: only `source: ticket` readings
    are asked (only the session that ran the verification can measure them; a number a tool computes
    is never asked of a worker), and only those whose `when_knob` has a value in force on the chain
    -- some layer states it, or the declaration gives it a factory `default`. A threshold with
    neither is "not configured" and would be measured for nothing, which is why it is not asked. Note
    that a declared default counts as in force: the closing judgement already uses it, so the form
    that asks for the number has to ask too. `node` is the node the asking reader has in hand: the
    closing judgement resolves with it, so the form that asks a worker for the number has to resolve
    with it too, or a threshold written on one node's own layer would judge but never be asked. With
    no node named the answer is the chain's own (`cycle` and wider). This is the same reading the
    form and the closing judgement use; the words each reading is asked with are the declaration's.
    """
    effective = resolve(project, cycle=cycle, node=node)
    out = []
    for reading in _registry().readings():
        if reading.get('source') != 'ticket':
            continue
        when = reading.get('when_knob') or ''
        if when:
            value, _source = effective.get(when, (None, 'builtin'))
            if value is None:
                continue
        out.append(reading['key'])
    return out


# ── writes ─────────────────────────────────────────────────────────────────

def write_layer(layer: str, project: str | None = None, key: str | None = None, value=None,
                cycle=None, node: str | None = None) -> None:
    """Write one knob into one layer; `value is None` drops that layer's entry instead.

    A layer file is one whole read-modify-write, held under the project's ledger lock: two lines
    writing different knobs at once would otherwise lose one of the two. The lock is also the gate --
    a `.pf/` this tool never init-ed cannot be written through it. The global layer belongs to this
    machine rather than to any project, so there is no shared lock to take and `_atomic_write`'s
    atomic replace is the whole of it. A key this tool does not own is left exactly as it is.

    The cycle layer is not written here: its writer is the cycle record's own (`state/cycles.py`),
    the one writer of `state.json`, and having a second one would be two writers of one record.
    """
    if layer == 'cycle':
        raise ValueError('the cycle layer is written by the cycle record\'s own writer '
                         '(state/cycles.py), never through a layer file')
    path = layer_path(layer, project, node=node)
    lock = _ledger_lock(project) if layer in ('node', 'project') else contextlib.nullcontext()
    with lock:
        data = _read_json(path, str(path)) if path.exists() else {}
        if not isinstance(data, dict):
            data = {}
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        _atomic_write(path, data)


def write_node_layer(project: str | None = None, node: str | None = None, key: str | None = None,
                     value=None) -> None:
    """One node's own layer: `<project>/.pf/nodes/<id>/config.json` (the node layer of the chain)."""
    write_layer('node', project, key, value, node=node)
