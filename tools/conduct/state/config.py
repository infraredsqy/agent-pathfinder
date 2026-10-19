"""This machine's and this project's settings: factory default -> global override -> project
override, the later one wins.

A value lives in exactly three places, and no other file may write a second copy of it:

    factory default   `CONFIG_SPEC` -- the one definition (to change a default, change this one table)
    global override   `~/.pf/config.json` (travels with the person/machine, never enters a project's repository)
    project override  `<project>/.pf/config.json` (travels with the project; `init` puts an empty `{}` there)

**Either override layer may be absent**: a missing file reads as "the default was never changed", and
everything goes by the factory default. Nobody generates the global layer for you: `pf config set
--global` writes it only when a human really means to change the global default. The project layer is
written by `init` as an empty object, at a path you can see at a glance, and an empty object reads as
"no override". `list` prints each key's effective value and the layer it came from; `get` prints the
value alone (that is what a skill reads).

`worktree_mode`'s default is not a value but a condition -- `fork` for a git project, `off` otherwise --
so `default_of()` computes it and that cell of the table is left `None`. The four keys are also the
shape of `CONFIG_SPEC`: the candidate table for `set` and `get` comes straight from it, a value read off
disk is checked against that same table, and a value that cannot be read is reported on the spot rather
than quietly falling back to the default.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

from .store import (
    LEDGER_NAME,
    _atomic_write,
    _ledger,
    _ledger_lock,
    _read_json,
    ledger_dir_for,
)

#: The override layer's file name -- both layers share it. The one source is `tools/shared/ledger.py`,
#: and this file only takes that value over: where the project layer lands is computed by `ledger.py`'s
#: `config_path_of`, and this file never composes a `config.json` of its own.
CONFIG_NAME = _ledger().CONFIG_NAME

#: The one definition of the four keys: factory default, shape, and a one-line summary. `kind` is
#: `parse_value`'s only dispatch -- adding a key means adding this one cell.
CONFIG_SPEC = {
    'max_agents': {
        'kind': 'count',
        'default': 3,
        'summary': 'how many subagents one session may have in flight at once',
    },
    'test_tier': {
        'kind': 'choice',
        'values': ('full', 'minimal'),
        'default': 'full',
        'summary': 'how this project is verified: full runs the whole suite, minimal runs only the '
                   'few checks the project names',
    },
    'worktree_mode': {
        'kind': 'choice',
        'values': ('off', 'fork', 'reuse'),
        'default': None,
        'summary': 'whether a node gets a worktree of its own: fork by default in a git project, '
                   'off otherwise',
    },
    'issue_repair': {
        'kind': 'choice',
        'values': ('on', 'off'),
        'default': 'on',
        'summary': 'whether a line crossing its own maintenance threshold gets a repair node '
                   'inserted for it',
    },
}

#: The override layers, lowest first: factory default, then global, then project.
LAYERS = ('global', 'project')

#: Where an effective value came from, as it is written for a human to read.
LAYER_LABELS = {
    'builtin': 'factory default',
    'global': f'global ~/{LEDGER_NAME}/{CONFIG_NAME}',
    'project': f'project {LEDGER_NAME}/{CONFIG_NAME}',
}


# ────────────────────────────────────────────────────────────────────────────
# The project layer goes through `ledger_dir_for`; no other place composes a path under `.pf/`
# (that discipline lives in store). The global layer is the same shape in the home directory:
# `~/.pf/config.json`.

def global_config_path() -> Path:
    """Where the global override lands: `~/.pf/config.json`.

    "The home directory counts as a project" is this layer's own way of putting it; the landing itself
    is the one source's business (`shared/ledger.py`'s `home_config_path`, along with the `PF_HOME_DIR`
    sandbox switch): both layers share one name and one shape, and the composition lives in one place.
    """
    return _ledger().home_config_path()


def project_config_path(project: str | None = None) -> Path:
    """Where the project override lands: `<project>/.pf/config.json`.

    `project` may be omitted (omitted, the root this copy is bound to is used), so this wants the ledger
    directory first and then the file under it: `ledger_dir_for(None)`'s omit semantics stay in store,
    and `config_path_of` wants nothing but a ledger directory.
    """
    return _ledger().config_path_of(ledger_dir_for(project))


def layer_path(layer: str, project: str | None = None) -> Path:
    return global_config_path() if layer == 'global' else project_config_path(project)


def project_root_of(project: str | None = None) -> Path:
    """This project's root: the parent of the ledger directory. `worktree_mode`'s default has to ask it whether this is a git checkout."""
    return ledger_dir_for(project).parent


# ───────────────────────────────────────────────────────────────────────────

#: One rule per kind: hand it a value (the JSON value read off disk, or the string the command line
#: gave) and, if it is valid, it hands back a value in the config's domain -- None if it is not. The
#: read side and the write side both come through here: adding a kind, or moving a kind's boundary,
#: touches this section; each cell of `CONFIG_SPEC` has one `kind`, and that is the only dispatch.
def _parse_count(spec: dict, value):
    """`count`: a positive integer. A boolean does not count; the command line's `"3"` and the disk's 3 both yield 3."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    return value if isinstance(value, int) and value >= 1 else None


def _parse_choice(spec: dict, value):
    """`choice`: a word from this cell's own candidate table."""
    return value if value in spec['values'] else None


_KINDS = {'count': _parse_count, 'choice': _parse_choice}


def parse_value(spec: dict, value):
    """One value for one key: parsed and checked by `spec['kind']`'s rule, None if it is not valid.

    Both sides share this one copy: `read_layer` uses it to check a value read off disk, and
    `cmd_config_set` uses it to check a value from the command line. One copy per side ends in "it
    writes but does not read back" (or the other way round) -- a kind's boundary is defined here once.
    """
    return _KINDS[spec['kind']](spec, value)


def _shape_error(key: str, value, where: Path) -> None:
    print(f'Error: {where} has an invalid {key}: {value!r}', file=sys.stderr)
    sys.exit(1)


def _checked(key: str, value, where: Path):
    """One value read off disk, checked against `CONFIG_SPEC` and handed back.

    A value that cannot be read stops the call right there: a mis-written setting quietly falling back
    to the default makes that key look "never configured" -- while it is in fact configured wrongly.
    """
    checked = parse_value(CONFIG_SPEC[key], value)
    if checked is None:
        _shape_error(key, value, where)
    return checked


def read_layer(layer: str, project: str | None = None) -> dict:
    """The keys written in one override layer. A missing file is empty (the normal state); a broken one gets a shout."""
    path = layer_path(layer, project)
    data = _read_json(path, str(path))
    if data is None:
        return {}
    if not isinstance(data, dict):
        print(f'Error: {path} must be a JSON object (key -> value)', file=sys.stderr)
        sys.exit(1)
    out = {}
    for key, value in data.items():
        if key not in CONFIG_SPEC:
            print(f'Warning: {path} has {key!r}, which is not a configurable key; skipped '
                  f'(configurable: {", ".join(sorted(CONFIG_SPEC))})', file=sys.stderr)
            continue
        out[key] = _checked(key, value, path)
    return out


def default_of(key: str, project: str | None = None):
    spec = CONFIG_SPEC[key]
    if spec['default'] is not None:
        return spec['default']
    return 'fork' if (project_root_of(project) / '.git').exists() else 'off'


def resolve_config(project: str | None = None) -> dict:
    """Effective values: factory default -> global -> project, each layer covering the one before."""
    out = {key: default_of(key, project) for key in CONFIG_SPEC}
    for layer in LAYERS:
        out.update(read_layer(layer, project))
    return out


def config_sources(project: str | None = None) -> dict:
    """Which layer each key's effective value came from: `builtin` / `global` / `project`."""
    out = {key: 'builtin' for key in CONFIG_SPEC}
    for layer in LAYERS:
        for key in read_layer(layer, project):
            out[key] = layer
    return out


# ───────────────────────────────────────────────────────────────────────────
# The project layer is locked together with the ledger directory: like state.json it is one whole
# read-modify-write, and two lines running `config set` at once would lose one write. The global layer
# has no shared lock to speak of (it belongs to no project), so it goes by `_atomic_write`'s atomic replace.

def _parse_for_write(key: str, value: str):
    """One value from the command line's side: the same kind's rule, and this side speaks the refusal."""
    spec = CONFIG_SPEC[key]
    stored = parse_value(spec, value)
    if stored is None:
        if spec['kind'] == 'count':
            print(f'Error: {key} wants a positive integer, but read {value!r}', file=sys.stderr)
        else:
            print(f'Error: {key} wants one of {list(spec["values"])}, but read {value!r}',
                  file=sys.stderr)
        sys.exit(1)
    return stored


def write_value(key: str, value, layer: str, project: str | None = None) -> None:
    """Write one key of one layer (`value is None` = drop that layer's override).

    The project layer goes through `_ledger_lock`, which is itself the gate: a `.pf/` that was never
    init-ed may not be written at all. The global layer belongs to this machine rather than to any
    project, so there is no shared lock to speak of -- `nullcontext` makes both paths write one shape.
    """
    path = layer_path(layer, project)
    with (_ledger_lock(project) if layer == 'project' else contextlib.nullcontext()):
        data = _read_json(path, str(path)) if path.exists() else {}
        if not isinstance(data, dict):
            data = {}
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        _atomic_write(path, data)


# ────────────────────────────────────────────────────────────────────────────

def cmd_config_list(project: str | None = None) -> None:
    effective = resolve_config(project)
    sources = config_sources(project)
    width = max(len(key) for key in CONFIG_SPEC)
    for key in CONFIG_SPEC:
        print(f'{key.ljust(width)} = {effective[key]} ({LAYER_LABELS[sources[key]]})')


def cmd_config_get(key: str, project: str | None = None) -> None:
    """It prints the value alone: this one line is what a caller (a skill, a script) gets."""
    print(resolve_config(project)[key])


def cmd_config_set(key: str, value: str, layer: str = 'project',
                   project: str | None = None) -> None:
    stored = _parse_for_write(key, value)
    write_value(key, stored, layer, project)
    print(f'CONFIG: {key} = {stored} (written to {LAYER_LABELS[layer]})')
    _report_effective(key, project)


def cmd_config_unset(key: str, layer: str = 'project', project: str | None = None) -> None:
    write_value(key, None, layer, project)
    print(f'CONFIG: {key} override dropped from {LAYER_LABELS[layer]}')
    _report_effective(key, project)


def _report_effective(key: str, project: str | None) -> None:
    """One more line on the effective value after the write: writing the global layer while the project has an override of its own makes that line the whole difference."""
    effective = resolve_config(project)[key]
    source = config_sources(project)[key]
    print(f'CONFIG: effective {key} = {effective} (from {LAYER_LABELS[source]})')
