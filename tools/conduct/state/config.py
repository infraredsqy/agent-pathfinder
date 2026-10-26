"""This machine's and this project's settings, over the knob chain.

A setting lives on one chain and nowhere else (`state/knobs.py` owns the table and the resolution):

    node -> this line -> this project -> this machine -> factory default

Narrow beats wide, and the same name may be stated on several layers at once: which one is in force
is determined, not guessed. What every reading carries is therefore a pair -- **the effective value
and the layer it came from** -- because "I set it, why is it not in force" is the one question this
chain owes an answer to. `list` prints the pair; `get` prints the value alone, which is what a skill
reads.

Not every knob takes every layer: a core setting is how this project or this machine behaves (the
project and global layers), while a knob a feature declares walks the whole chain. A write into a
layer the knob does not take is refused here, naming the layers it does take. And three names are
not settings at all -- `parallel`, `when_shelved` and `spawner` live on the line record, whose writer
is `pf cycle policy`: this command does not know them, and saying so here is what keeps a value
nothing reads out of a config file.

The two *file* override layers are written here: the project layer (`<project>/.pf/config.json`,
written by `init` as an empty object) and the global layer (`~/.pf/config.json`, which nobody
generates for you -- `set --global` is a human really meaning to change the machine's default). The
node layer (`<project>/.pf/nodes/<id>/config.json`) is written here too, under the same ledger lock.
The cycle layer is **not** written here: it rides on the cycle record, whose one writer is
`state/cycles.py`, and this file delegates to that writer rather than opening a second one.

`set` and `unset` write exactly one layer. `set` stores a value (a switch's `off` is a value: a knob
is turned off by writing `off`); `unset` drops the layer's entry, letting the next wider layer show
through again. The two are different moves and are spelled differently on purpose.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import knobs
from .store import _ledger

#: The override layers' file name -- both file layers share it. The one source is
#: `tools/shared/ledger.py`, and this file only takes that value over.
CONFIG_NAME = _ledger().CONFIG_NAME


# ── the layers, and the shape of one value ─────────────────────────────────

def global_config_path() -> Path:
    """Where the global layer lands: `~/.pf/config.json` (see `knobs.global_config_path`)."""
    return knobs.global_config_path()


def project_config_path(project: str | None = None) -> Path:
    """Where the project layer lands: `<project>/.pf/config.json`."""
    return knobs.project_config_path(project)


def layer_path(layer: str, project: str | None = None, node: str | None = None) -> Path:
    """Where one file layer lands (the cycle layer has no file: it is on the cycle record)."""
    return knobs.layer_path(layer, project, node=node)


def read_layer(layer: str, project: str | None = None, cycle=None, node: str | None = None) -> dict:
    """The knobs one layer states -- `cycle` is the record itself, `node` a node id."""
    return knobs.read_layer(layer, project, cycle=cycle, node=node)


def _spec(key: str) -> dict:
    """The one cell of the knob table this key names, or a refusal naming the keys that exist."""
    spec = knobs.specs().get(key)
    if spec is None:
        print(f'Error: unknown key {key!r}; configurable: {", ".join(sorted(knobs.specs()))}',
              file=sys.stderr)
        sys.exit(1)
    return spec


def _check_layer(key: str, spec: dict, layer: str) -> None:
    """Refuse a write into a layer this knob does not take, naming the layers it does.

    Which layers a knob takes is its own cell (`layers`), not a property of the chain: a core setting
    is how this project or this machine behaves and takes the two wide layers only, while a knob a
    feature declares is read through the whole chain and takes all four. Writing into a layer the
    knob does not take would land a value nothing could ever read, so it is refused here -- the read
    side reports a value hand-written into such a layer, and the two sides say the same thing.
    """
    allowed = knobs.layers_of(spec)
    if layer in allowed:
        return
    print(f'Error: {key} does not take the {layer} layer (it takes: {", ".join(allowed)}); '
          f'nothing was written', file=sys.stderr)
    sys.exit(1)


def _cycle_of(project: str | None, cycle_id: str | None) -> dict | None:
    """The record one `--cycle <id>` names; an id this project does not have is refused."""
    if not cycle_id:
        return None
    record = knobs.cycle_record(project, cycle_id)
    if not record:
        print(f'Error: this project\'s record has no cycle "{cycle_id}"', file=sys.stderr)
        sys.exit(1)
    return record


# ── the readings ───────────────────────────────────────────────────────────

def resolve_config(project: str | None = None, cycle=None, node: str | None = None) -> dict:
    """Effective values alone: `{key: value}`. `cycle` is an id (``None`` = this project's layers only)."""
    record = _cycle_of(project, cycle)
    return {key: value for key, (value, _source)
            in knobs.resolve(project, cycle=record, node=node).items()}


def config_sources(project: str | None = None, cycle=None, node: str | None = None) -> dict:
    """Which layer each knob's effective value came from: `{key: source}`."""
    record = _cycle_of(project, cycle)
    return knobs.knob_sources(project, cycle=record, node=node)


def _shown(value) -> str:
    """One value as it is printed: `-` when no layer states it and it has no factory value."""
    return '-' if value is None else str(value)


# ── the write side ─────────────────────────────────────────────────────────

def _parse_for_write(key: str, spec: dict, value: str):
    """One value from the command line: the kind's own rule, and this side speaks the refusal.

    `set` stores a value, so `off` is refused for every kind that has no `off` in its vocabulary --
    `unset` is the move that drops an entry, and the two must not be spelled the same way. `number`
    and `integer` take a positive value only: both are "the last acceptable reading" shaped, and a
    line of 0 would judge every reading as over it.
    """
    kind = spec['kind']
    parsed = knobs.parse(kind, value, spec.get('values'))
    if parsed is not None and kind in ('number', 'integer') and parsed <= 0:
        parsed = None
    if parsed is None:
        if kind == 'number':
            print(f'Error: {key} wants a positive number, but read {value!r}', file=sys.stderr)
        elif kind == 'integer':
            print(f'Error: {key} wants a positive integer, but read {value!r}', file=sys.stderr)
        elif kind == 'switch':
            print(f'Error: {key} wants on or off, but read {value!r}', file=sys.stderr)
        elif spec.get('values'):
            print(f'Error: {key} wants one of {list(spec["values"])}, but read {value!r}',
                  file=sys.stderr)
        else:
            print(f'Error: {key} wants a word, but read {value!r}', file=sys.stderr)
        sys.exit(1)
    return parsed


def _layer_label(layer: str, cycle=None, node: str | None = None) -> str:
    """One layer's own label: the two record layers carry the identity they hang on."""
    if layer in ('cycle', 'node'):
        return knobs.source_label(layer, cycle=cycle, node=node)
    return knobs.LAYER_LABELS[layer]


def write_value(key: str, value, layer: str = 'project', project: str | None = None,
                cycle=None, node: str | None = None) -> None:
    """Write one knob into exactly one layer (`value is None` = drop that layer's entry).

    `cycle` is the cycle **id** for the cycle layer (that layer's writer works from the record's
    identity), and the node id for the node layer. The cycle layer goes through `state/cycles.py`,
    the one writer of the cycle record -- this file does not write `state.json`.
    """
    if layer == 'cycle':
        _cycle_of(project, cycle)                 # the identity is checked before anything is written
        from . import cycles                      # the cycle record's writer lives with its readers
        cycles.write_cycle_policy(cycle, key, value)
        return
    if layer == 'node' and not node:
        print('Error: the node layer needs a node id (--node)', file=sys.stderr)
        sys.exit(1)
    knobs.write_layer(layer, project, key, value, node=node)


# ── the command line ───────────────────────────────────────────────────────

def cmd_config_list(project: str | None = None, cycle=None, node: str | None = None) -> None:
    """Every knob's effective value and the layer it came from -- the whole chain, one line each."""
    record = _cycle_of(project, cycle)
    effective = knobs.resolve(project, cycle=record, node=node)
    width = max(len(key) for key in effective)
    for key, (value, source) in effective.items():
        print(f'{key.ljust(width)} = {_shown(value)} '
              f'({knobs.source_label(source, cycle=record, node=node)})')


def cmd_config_get(key: str, project: str | None = None, cycle=None, node: str | None = None) -> None:
    """It prints the value alone: this one line is what a caller (a skill, a script) gets."""
    print(_shown(resolve_config(project, cycle, node)[key]))


def cmd_config_set(key: str, value: str, layer: str = 'project', project: str | None = None,
                   cycle=None, node: str | None = None) -> None:
    spec = _spec(key)
    _check_layer(key, spec, layer)
    stored = _parse_for_write(key, spec, value)
    write_value(key, stored, layer, project, cycle, node)
    print(f'CONFIG: {key} = {stored} (written to {_layer_label(layer, cycle, node)})')
    _report_effective(key, project, cycle, node)


def cmd_config_unset(key: str, layer: str = 'project', project: str | None = None,
                     cycle=None, node: str | None = None) -> None:
    _check_layer(key, _spec(key), layer)
    write_value(key, None, layer, project, cycle, node)
    print(f'CONFIG: {key} override dropped from {_layer_label(layer, cycle, node)}')
    _report_effective(key, project, cycle, node)


def _report_effective(key: str, project: str | None, cycle=None, node: str | None = None) -> None:
    """One more line on the effective value after the write: writing the global layer while a narrower
    layer states the same name makes that line the whole difference."""
    record = _cycle_of(project, cycle)
    value, source = knobs.resolve(project, cycle=record, node=node)[key]
    print(f'CONFIG: effective {key} = {_shown(value)} '
          f'(from {knobs.source_label(source, cycle=record, node=node)})')
