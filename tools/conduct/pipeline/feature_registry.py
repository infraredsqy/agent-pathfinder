#!/usr/bin/env python3
"""The registry of installed features: one folder each, and what its declaration says.

A feature is a folder under the features directory, and the folder is its identity: remove it and
the feature is gone, with no second place remembering it. The folder carries two files -- a
declaration (`feature.json`: the data, which timing it takes, which names it claims, which action it
produces, and per metric the dedupe tag and the method whose card a session reads) and an
implementation (`impl.py`: the code, called once at the timing it claimed). The names the outside
writes down to name one of its debts live in that declaration, never here.

This module is the core's whole knowledge of features: the four knob kinds, the three timings, the
two actions, and the rules a declaration has to satisfy. No installed feature's name appears here:
names arrive from the declarations and are merged here for every reader (the writing side's
validation, the candidates `--help` shows, the ticket form, the doctor).

The declaration is a data file of its own because listing a feature must not mean executing it: a
broken declaration that took the help, the listing and the doctor down with it would take exactly the
three doors a human reaches for when something is already wrong.

Runtime is tolerant and the doctor is strict. An unreadable or malformed declaration is a *problem*
reported by `validate()` and never an exception: a project in the middle of its pipeline must not be
stopped by a feature that is broken. The scan is not cached -- the folder on disk is the one answer,
so a feature put back or taken away shows up at the next call, and a features directory with nothing
in it returns nothing from every door here (that is what "the folder was deleted" looks like).

Standalone on purpose: this file imports the standard library and fetches `tools/shared/loader.py`
by path, and it carries no package-relative import, because it is loaded by path from two packages.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

#: The knob kinds the core knows. A feature's declaration may only use these -- a fifth kind is a
#: change to the core, discussed on its own, never smuggled in with a feature.
KINDS = ('number', 'integer', 'word', 'switch')

#: When a feature may act: closing a node out, picking which node runs next, and dispatching (opening
#: the session). Two axes: inserting a node is possible at close and at pick, blocking a gate only at
#: dispatch.
TIMINGS = ('close', 'pick', 'dispatch')

#: What a feature may do at its timing: put a node into the graph, or stop a gate from opening.
ACTIONS = ('insert', 'block')

#: The two axes as one table: which (timing, action) cells the core actually serves. Inserting is
#: meaningful at a close (the node the close picked is in hand) and at a pick (a node is about to
#: run); blocking is a dispatch's question and nothing else. A declaration naming another cell would
#: pass a plain membership check and then never run -- a closed set that lies -- so it is a problem.
CELLS = (('close', 'insert'), ('pick', 'insert'), ('dispatch', 'block'))

#: Where a reading's number comes from: the worker's ticket, or the pool a tool computes itself.
SOURCES = ('ticket', 'pool')

#: The layers a knob's resolution chain walks, narrowest first. `state/knobs.py` owns the chain, and
#: this file restates the names because it is loaded by path from two packages and imports neither:
#: they are the closed set a declaration's `layers` cell may narrow its own knob to.
LAYERS = ('node', 'cycle', 'project', 'global')

#: The environment variable that points the scan somewhere else (a sandbox, a drill copy): the
#: features directory is data, so it gets the same kind of switch every other part of the tree has.
FEATURES_ENV = 'PF_FEATURES_DIR'

#: The two files a feature folder holds that this module reads: the declaration, and the implementation
#: the timing calls. There is no third file in the folder -- the skill card a metric names by its
#: `method` lives in the `skills/` tree, and the declaration only carries its name.
DECLARATION = 'feature.json'
IMPLEMENTATION = 'impl.py'

# ── the bootstrap every file outside `tools/shared/` carries (see `shared/loader.py`) ────────────
SHARED = Path(__file__).resolve().parents[2] / 'shared'

_LOADER = None


def _loader():
    """`tools/shared/loader.py`: the one loader, for the modules this tree keeps outside `shared/`."""
    global _LOADER
    if _LOADER is None:
        spec = importlib.util.spec_from_file_location('pf_shared_loader', SHARED / 'loader.py')
        _LOADER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_LOADER)
    return _LOADER


#: The implementation files that would not load, by feature name. `impl_of` writes one entry here when
#: the load fails (and clears it when a later load works), so `validate()` can report it: the failure
#: belongs to the runtime, and the doctor is where it becomes visible. One entry per feature keeps a
#: repeated failure from growing a list.
_RUNTIME_PROBLEMS: dict[str, str] = {}


# ── where the folders are ───────────────────────────────────────────────────

def features_dir() -> Path:
    """The directory the feature folders live under: `<conduct>/features`, or `PF_FEATURES_DIR`.

    The shipped location is computed from this file's own place -- the conduct directory is one level
    up from the package this file sits in -- so a checkout that moves carries its features with it.
    """
    override = (os.environ.get(FEATURES_ENV) or '').strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / 'features'


def _feature_folders() -> list:
    """Every folder a feature could be, sorted by folder name; the scan errors are left to the caller.

    `__pycache__` and dotted directories are not features: a compiled artefact of an implementation
    is not a declaration, and calling it one would make the doctor shout about the tree's own litter.
    """
    root = features_dir()
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir()
                  if path.is_dir() and not path.name.startswith(('.', '__')))


def _read_declaration(path: Path):
    """(declaration, problem): the parsed `feature.json`, or `None` and one sentence saying why.

    A declaration is data and its failures are data too. This never raises: a file that is not UTF-8,
    not JSON, or not a JSON object comes back as a problem string a doctor prints.
    """
    if not path.is_file():
        return None, f'no {DECLARATION} (a feature is a folder with a declaration in it)'
    try:
        decl = json.loads(path.read_text(encoding='utf-8'))
    except OSError as e:
        return None, f'{path} cannot be read: {e}'
    except UnicodeDecodeError as e:
        return None, f'{path} is not valid UTF-8: {e}'
    except json.JSONDecodeError as e:
        return None, f'{path} parse failure: {e}'
    if not isinstance(decl, dict):
        return None, f'{path} must be a JSON object (one declaration per feature)'
    return decl, None


# ── the scan ─────────────────────────────────────────────────────────────────

def load_all() -> list:
    """Every feature folder, in folder-name order: `{name, folder, decl, problems}`.

    `name` is the folder's name -- the folder IS the identity -- and `decl` is the parsed declaration
    (or `None` when it could not be read, in which case `problems` says so). Nothing is cached and
    nothing raises: an empty features directory answers an empty list, and one broken folder does not
    hide its neighbours.
    """
    out = []
    for folder in _feature_folders():
        decl, problem = _read_declaration(folder / DECLARATION)
        out.append({
            'name': folder.name,
            'folder': folder,
            'decl': decl,
            'problems': [] if problem is None else [problem],
        })
    return out


def scan_problems() -> list:
    """What is wrong with the features directory itself (nothing, for the normal cases).

    A missing directory is not a problem: zero feature folders is a legal project, and every door
    here answers empty for it. A path that exists but cannot be listed IS one, because it would
    otherwise read as "no features installed" while features are sitting there.
    """
    root = features_dir()
    if not root.exists():
        return []
    if not root.is_dir():
        return [f'{root} is not a directory (the features directory holds one folder per feature)']
    try:
        list(root.iterdir())
    except OSError as e:
        return [f'{root} cannot be scanned: {e}']
    return []


# ── the rules a declaration has to satisfy ──────────────────────────────────

def _decl_of(feature: dict):
    decl = feature.get('decl')
    return decl if isinstance(decl, dict) else None


def _names(decl: dict) -> dict:
    """The three name groups of one declaration, each as a mapping (`{}` when absent or malformed)."""
    def group(key):
        value = decl.get(key)
        return value if isinstance(value, dict) else {}
    return {'knobs': group('knobs'), 'readings': group('readings'), 'metrics': group('metrics')}


def _problems_of(feature: dict) -> list:
    """One declaration's own problems: the shape of its cells, and the references between them."""
    name = feature['name']
    out = list(feature.get('problems') or [])
    decl = _decl_of(feature)
    if decl is None:
        return out
    declared = decl.get('name')
    if declared is not None and declared != name:
        out.append(f'feature {name!r} declares the name {declared!r}: the folder is its identity, '
                   f'so the two have to agree')
    timing = decl.get('timing')
    if timing not in TIMINGS:
        out.append(f'feature {name!r}: timing {timing!r} is not one of {list(TIMINGS)}')
    action = decl.get('action')
    if action not in ACTIONS:
        out.append(f'feature {name!r}: action {action!r} is not one of {list(ACTIONS)}')
    if timing in TIMINGS and action in ACTIONS and (timing, action) not in CELLS:
        served = ', '.join(f'{act} at {when}' for when, act in CELLS)
        out.append(f'feature {name!r}: {action!r} at {timing!r} is not a cell this core serves '
                   f'({served})')

    groups = _names(decl)
    knobs, readings, metrics = groups['knobs'], groups['readings'], groups['metrics']
    for key, cell in knobs.items():
        if not isinstance(cell, dict):
            out.append(f'feature {name!r}: knob {key!r} must be an object '
                       f'(kind, default, values, summary)')
            continue
        kind = cell.get('kind')
        if kind not in KINDS:
            out.append(f'feature {name!r}: knob {key!r} has kind {kind!r}, which is not one of '
                       f'{list(KINDS)}')
        if kind == 'word' and cell.get('values') is not None:
            values = cell.get('values')
            if (not isinstance(values, list) or not values
                    or not all(isinstance(word, str) for word in values)):
                out.append(f'feature {name!r}: knob {key!r} takes `values` as a non-empty list of '
                           f'words')
        layers = cell.get('layers')
        if layers is not None:
            if (not isinstance(layers, list) or not layers
                    or not all(isinstance(layer, str) for layer in layers)):
                out.append(f'feature {name!r}: knob {key!r} takes `layers` as a non-empty list of '
                           f'layer names ({", ".join(LAYERS)})')
            else:
                unknown = [layer for layer in layers if layer not in LAYERS]
                if unknown:
                    out.append(f'feature {name!r}: knob {key!r} names the layer(s) '
                               f'{", ".join(repr(layer) for layer in unknown)}, which are not on the '
                               f'chain ({", ".join(LAYERS)})')

    for key, cell in readings.items():
        if not isinstance(cell, dict):
            out.append(f'feature {name!r}: reading {key!r} must be an object (source, ask, ...)')
            continue
        source = cell.get('source')
        if source not in SOURCES:
            out.append(f'feature {name!r}: reading {key!r} has source {source!r}, which is not one '
                       f'of {list(SOURCES)}')
        when = cell.get('when_knob')
        if when and when not in knobs:
            out.append(f'feature {name!r}: reading {key!r} is narrowed by {when!r}, which the '
                       f'feature does not declare as a knob')

    for key, cell in metrics.items():
        if not isinstance(cell, dict):
            out.append(f'feature {name!r}: metric {key!r} must be an object (line, tag, method)')
            continue
        if key not in readings:
            out.append(f'feature {name!r}: metric {key!r} names a reading the feature does not '
                       f'declare')
        line = cell.get('line')
        if line not in knobs:
            out.append(f'feature {name!r}: metric {key!r} is judged against {line!r}, which the '
                       f'feature does not declare as a knob')
        for field in ('tag', 'method'):
            value = cell.get(field)
            if not isinstance(value, str) or not value.strip():
                out.append(f'feature {name!r}: metric {key!r} needs a {field} (the string the '
                           f'outside writes down to name this debt)')

    enable = decl.get('enable_knob')
    if enable:
        cell = knobs.get(enable)
        if not isinstance(cell, dict):
            out.append(f'feature {name!r}: switches on {enable!r}, which the feature does not '
                       f'declare as a knob')
        elif cell.get('kind') != 'switch':
            out.append(f'feature {name!r}: switches on {enable!r}, whose kind is '
                       f'{cell.get("kind")!r}; only a switch names on/off')

    if not (feature['folder'] / IMPLEMENTATION).is_file():
        out.append(f'feature {name!r}: no {IMPLEMENTATION} in {feature["folder"]}')
    return out


def _conflicts(features: list) -> list:
    """The names two features may not share: feature names, knobs, readings, and dedupe tags."""
    out = []
    for label, values in (
        ('feature name', [(feature['name'], feature['name']) for feature in features]),
        ('knob', _declared(features, 'knobs')),
        ('reading', _declared(features, 'readings')),
        ('dedupe tag', _declared(features, 'metrics', 'tag')),
    ):
        out.extend(_first_claim(label, values))
    return out


def _declared(features: list, group: str, field: str | None = None) -> list:
    """(name, feature name) pairs for one declaration group: its keys, or one string field of them."""
    out = []
    for feature in features:
        decl = _decl_of(feature)
        if decl is None:
            continue
        cells = decl.get(group)
        if not isinstance(cells, dict):
            continue
        for key, cell in cells.items():
            value = key if field is None else (cell.get(field) if isinstance(cell, dict) else None)
            if isinstance(value, str) and value.strip():
                out.append((value, feature['name']))
    return out


def _first_claim(label: str, values: list):
    """One sentence per name a second feature claims (`values` is ordered by folder)."""
    seen = {}
    for value, owner in values:
        if value in seen:
            yield (f'{label} {value!r} is claimed by both {seen[value]!r} and {owner!r}: '
                   f'two features may not share it')
        else:
            seen[value] = owner


def validate() -> list:
    """Every problem the installed features have: the doctor's list, and nothing that raises.

    Covered: the features directory itself, each declaration's own shape and references, the names two
    features may not share, an implementation file that is missing, and an implementation that failed
    to load at runtime. An unknown timing or action is stated here rather than silently dropped: the
    runtime skips that cell, and this is where a human reads why.
    """
    features = load_all()
    problems = list(scan_problems())
    for feature in features:
        problems.extend(_problems_of(feature))
    problems.extend(_conflicts(features))
    installed = {feature['name'] for feature in features}
    problems.extend(_RUNTIME_PROBLEMS[name] for name in sorted(_RUNTIME_PROBLEMS)
                    if name in installed)
    return problems


# ── what the declarations say, merged for the readers ───────────────────────

def knob_specs() -> dict:
    """Every knob the installed features declare: `{name: {kind, default, values?, layers, summary,
    feature}}`.

    Only well-formed cells come out (a kind outside the four kinds is not a knob, and a `layers` cell
    that is not a non-empty subset of the chain is dropped: `validate()` says what is wrong with
    both). A name two features both claim is reported by `validate()` and the first folder keeps it --
    but whether a feature may shadow a name the core already owns is answered by the merger, not here:
    this file does not know the built-in table, and copying it here would be a second place for those
    names.
    """
    out = {}
    for feature in load_all():
        decl = _decl_of(feature)
        if decl is None:
            continue
        for key, cell in _names(decl)['knobs'].items():
            if not isinstance(cell, dict) or cell.get('kind') not in KINDS:
                continue
            spec = {
                'kind': cell['kind'],
                'default': cell.get('default'),
                'summary': cell.get('summary') or '',
                'feature': feature['name'],
            }
            values = cell.get('values')
            if isinstance(values, list) and values and all(isinstance(w, str) for w in values):
                spec['values'] = tuple(values)
            declared_layers = _layer_cell(cell.get('layers'))
            if declared_layers is not None:
                spec['layers'] = declared_layers
            out.setdefault(key, spec)
    return out


def _layer_cell(value):
    """A declared `layers` cell as the chain's own order, or `None` when it is not a usable one.

    Narrowing is the declaration's own business (a knob that is a project's answer and nothing about
    one node's run); an empty cell, a name that is not on the chain, or anything that is not a list of
    names is a declaration problem, and the knob then takes the whole chain -- the default the
    merger's readers already have -- while `validate()` says why.
    """
    if not isinstance(value, list) or not value or not all(isinstance(layer, str) for layer in value):
        return None
    if any(layer not in LAYERS for layer in value):
        return None
    return tuple(layer for layer in LAYERS if layer in value)


def readings() -> list:
    """Every reading the installed features declare: `{feature, key, source, ask, when_knob}`.

    Declaration order inside one feature, features in folder order -- the order the declarations were
    written in, which is the order a form or a judgement should walk them in. A reading whose source
    is not one of the two sources is dropped (the doctor reports it).
    """
    out = []
    for feature in load_all():
        decl = _decl_of(feature)
        if decl is None:
            continue
        for key, cell in _names(decl)['readings'].items():
            if not isinstance(cell, dict) or cell.get('source') not in SOURCES:
                continue
            out.append({
                'feature': feature['name'],
                'key': key,
                'source': cell['source'],
                'ask': cell.get('ask') if isinstance(cell.get('ask'), str) else '',
                'when_knob': cell.get('when_knob') if isinstance(cell.get('when_knob'), str) else '',
            })
    return out


def tags_of(feature: dict) -> list:
    """One feature's dedupe tags, in declaration order (what a human writes to name the same debt)."""
    decl = _decl_of(feature) or {}
    out = []
    for cell in _names(decl)['metrics'].values():
        tag = cell.get('tag') if isinstance(cell, dict) else None
        if isinstance(tag, str) and tag.strip() and tag not in out:
            out.append(tag)
    return out


def methods_of(feature: dict) -> list:
    """One feature's method names, in declaration order (the skill cards it points a node at)."""
    decl = _decl_of(feature) or {}
    out = []
    for cell in _names(decl)['metrics'].values():
        method = cell.get('method') if isinstance(cell, dict) else None
        if isinstance(method, str) and method.strip() and method not in out:
            out.append(method)
    return out


def dedupe_tags() -> list:
    """Every dedupe tag the installed features declare, in folder/declaration order."""
    out = []
    for feature in load_all():
        for tag in tags_of(feature):
            if tag not in out:
                out.append(tag)
    return out


def methods() -> list:
    """Every method name the installed features declare, in folder/declaration order."""
    out = []
    for feature in load_all():
        for method in methods_of(feature):
            if method not in out:
                out.append(method)
    return out


def _usable(feature: dict) -> bool:
    """Whether this feature may act at runtime: a readable declaration with no problem of its own."""
    return _decl_of(feature) is not None and not _problems_of(feature)


def by_timing(timing: str) -> list:
    """The features that act at this timing, in folder order.

    A feature whose declaration is broken, or whose timing nobody recognizes, is skipped: the runtime
    is tolerant, and the doctor is where it is pointed out.
    """
    return [feature for feature in load_all()
            if _usable(feature) and (_decl_of(feature) or {}).get('timing') == timing]


def by_action(action: str) -> list:
    """The features that produce this action, in folder order (see `by_timing` for the skipping)."""
    return [feature for feature in load_all()
            if _usable(feature) and (_decl_of(feature) or {}).get('action') == action]


# ── the implementation ──────────────────────────────────────────────────────

def impl_of(name: str):
    """One feature's implementation module, loaded by path, or `None` when it will not load.

    Loaded once per process (`register` + `reuse`, under a name of its own): the module is stateless
    by contract -- it is called at its timing with the context it is handed and keeps nothing between
    calls -- so every caller shares the one copy. A load failure is recorded and reported by
    `validate()` instead of raised: a broken feature must not stop the project that is running it.
    """
    path = features_dir() / name / IMPLEMENTATION
    module_name = 'pf_feature_' + re.sub(r'\W', '_', name) + '_impl'
    try:
        module = _loader().load_by_path(module_name, path, register=True, reuse=True)
    except (Exception, SystemExit) as e:      # noqa: BLE001 -- a broken feature must not raise here
        _RUNTIME_PROBLEMS[name] = f'feature {name!r}: {IMPLEMENTATION} failed to load: {e!r}'
        return None
    _RUNTIME_PROBLEMS.pop(name, None)
    return module
