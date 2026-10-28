#!/usr/bin/env python3
"""The registry of installed plugins: one folder each, and what its declaration says.

A plugin is a folder under the plugins directory, and the folder is its identity: remove it and
the plugin is gone, with no second place remembering it. The folder carries two files -- a
declaration (`plugin.json`: the data, which timing it takes, which names it claims, which action it
produces, per metric the dedupe tag and the method whose skill a session reads, the reading sources it
claims for itself, and the commands `pf` itself hands to it) and an implementation (`impl.py`: the
code, called once at the timing it claimed, or run as a script by one of those commands) -- and, when
a method it names has a skill, the skill itself under `skills/<lang>/<method>/`. The names the outside
writes down to name one of its debts, and the words `pf` routes to it, live in that declaration, never
here.

This module is the core's whole knowledge of plugins: the four knob kinds, the three timings, the
two actions, the shape of a declared command, the one reading source the core serves itself plus the
ones a declaration claims for its own, and the rules a declaration has to satisfy. No installed
plugin's name appears here: names arrive from the declarations and are merged here for every reader
(the writing side's validation, the candidates `--help` shows, the words `pf` routes, the ticket form,
the doctor).

The declaration is a data file of its own because listing a plugin must not mean executing it: a
broken declaration that took the help, the listing and the doctor down with it would take exactly the
three doors a human reaches for when something is already wrong.

Runtime is tolerant and the doctor is strict. An unreadable or malformed declaration is a *problem*
reported by `validate()` and never an exception: a project in the middle of its pipeline must not be
stopped by a plugin that is broken. The scan is not cached -- the folder on disk is the one answer,
so a plugin put back or taken away shows up at the next call, and a plugins directory with nothing
in it returns nothing from every door here (that is what "the folder was deleted" looks like).

`validate()` asks the declaration alone; `verify()` is the validator and asks both sides: the same
declaration problems, plus the method skills the declarations name, plus the implementation function
each claimed source says computes its number, plus one real call of every implementation against a
stand-in project (`_StandIn`). The suite and `pf doctor` are its two callers, and nothing on the
pipeline's route is: a project in the middle of its pipeline must not be stopped by a plugin that is
broken.

Standalone on purpose: this file imports the standard library and fetches `tools/shared/loader.py`
by path, and it carries no package-relative import, because it is loaded by path from two packages.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

#: The knob kinds the core knows. A plugin's declaration may only use these -- a fifth kind is a
#: change to the core, discussed on its own, never smuggled in with a plugin.
KINDS = ('number', 'integer', 'word', 'switch')

#: When a plugin may act: closing a node out, picking which node runs next, and dispatching (opening
#: the session). Two axes: inserting a node is possible at close and at pick, blocking a gate only at
#: dispatch.
TIMINGS = ('close', 'pick', 'dispatch')

#: What a plugin may do at its timing: put a node into the graph, or stop a gate from opening.
ACTIONS = ('insert', 'block')

#: The two axes as one table: which (timing, action) cells the core actually serves. Inserting is
#: meaningful at a close (the node the close picked is in hand) and at a pick (a node is about to
#: run); blocking is a dispatch's question and nothing else. A declaration naming another cell would
#: pass a plain membership check and then never run -- a closed set that lies -- so it is a problem.
CELLS = (('close', 'insert'), ('pick', 'insert'), ('dispatch', 'block'))

#: The one reading source the core serves itself: the number the worker reports in its ticket. Every
#: other number a reading may draw on is a name some declaration claims for itself (`SOURCES_GROUP`):
#: the set of sources is this one plus the names the installed plugins claim, merged here for every
#: reader.
SOURCES = ('ticket',)

#: The group a declaration claims reading sources of its own in: `{name: {by}}`. A claimed
#: source is the claimant's: the number is computed by the implementation function `by` names, and the
#: core only hands it on to the plugin that asked for it -- it does not read what the number means.
#: The names in `SOURCES` are the core's own and cannot be claimed (that would be a second home
#: for the meaning the core already fixed); a name two plugins claim is `_conflicts`' problem.
SOURCES_GROUP = 'sources'

#: The cell a claimed source must state: the implementation function that computes the number. "Who
#: computes it" is the whole of what a claim adds over a bare name, and it is data -- answerable
#: without executing anything, which is why it lives in the declaration and not in the implementation.
SOURCE_BY = 'by'

#: The name group a declaration claims when it brings words of its own: `pf <word>` hands the rest of
#: the line to the folder's implementation, run as a script. A command is not a timing -- it happens
#: when a human types it, not at one of the three moments the pipeline serves -- so a plugin that
#: carries commands and nothing else occupies no cell of the two-axis table at all, which is why
#: `timing` and `action` are stated together or neither is (see `_problems_of`).
COMMANDS = 'commands'

#: The two cells one command states beside its word: the line `pf --help` prints, and whether the word
#: goes along in the argv handed to the implementation. Both are defaults when the cell is absent --
#: the word does go along, so a folder answering several commands can tell which one it was -- and a
#: cell that is there and not of its type is a declaration problem (`_problems_of`).
COMMAND_SUMMARY = 'summary'
COMMAND_KEEP_NAME = 'keep_name'

#: The group a declaration lists what else it ships in its own folder as: `["tools/x.py"]`, relative
#: to that folder. What a plugin's commands reach for is inside it, and the core cannot spell those
#: paths without knowing one plugin's layout -- so the plugin says them and the doctor checks each is
#: there. A tool that never shipped is then one line in the doctor instead of a reading that quietly
#: stops arriving.
FILES = 'files'

#: The layers a knob's resolution chain walks, narrowest first. `state/knobs.py` owns the chain, and
#: this file restates the names because it is loaded by path from two packages and imports neither:
#: they are the closed set a declaration's `layers` cell may narrow its own knob to.
LAYERS = ('node', 'cycle', 'project', 'global')

#: Where a method's skill lives: in the folder of the plugin that binds it, as
#: `<plugin>/skills/<lang>/<method>/SKILL.md`. One language is enough: the install links the folder
#: of the language the chosen one lacks and says which language it borrowed, so a skill in another
#: language still reads. The skill moves with the folder -- removing a plugin removes the skill, which
#: is what "installed and removed with it" means for a skill. The install is what turns one of those
#: folders into the `~/.agents/skills/<method>` path a session reads; nothing here depends on that
#: layout.
SKILLS = 'skills'
SKILL_FILE = 'SKILL.md'

#: The environment variable that points the scan somewhere else (a sandbox, a drill copy): the
#: plugins directory is data, so it gets the same kind of switch every other part of the tree has.
PLUGINS_ENV = 'PF_PLUGINS_DIR'

#: The two files a plugin folder holds that this module reads: the declaration, and the implementation
#: the timing calls. A skill a metric names by its `method` is the folder's third kind of thing, and it
#: lives under `SKILLS` in the same folder: the declaration carries the name, the folder carries the
#: skill, and the two move together.
DECLARATION = 'plugin.json'
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


#: The implementation files that would not load, by plugin name. `impl_of` writes one entry here when
#: the load fails (and clears it when a later load works), so `validate()` can report it: the failure
#: belongs to the runtime, and the doctor is where it becomes visible. One entry per plugin keeps a
#: repeated failure from growing a list.
_RUNTIME_PROBLEMS: dict[str, str] = {}


# ── where the folders are ───────────────────────────────────────────────────

def plugins_dir() -> Path:
    """The directory the plugin folders live under: `<repo>/plugins`, or `PF_PLUGINS_DIR`.

    The shipped location is computed from this file's own place -- the conduct directory is one level
    up from the package this file sits in -- so a checkout that moves carries its plugins with it.
    """
    override = (os.environ.get(PLUGINS_ENV) or '').strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[3] / 'plugins'


def _plugin_folders() -> list:
    """Every folder a plugin could be, sorted by folder name; the scan errors are left to the caller.

    `__pycache__` and dotted directories are not plugins: a compiled artefact of an implementation
    is not a declaration, and calling it one would make the doctor shout about the tree's own litter.
    """
    root = plugins_dir()
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir()
                  if path.is_dir() and not path.name.startswith(('.', '__')))


def _read_declaration(path: Path):
    """(declaration, problem): the parsed `plugin.json`, or `None` and one sentence saying why.

    A declaration is data and its failures are data too. This never raises: a file that is not UTF-8,
    not JSON, or not a JSON object comes back as a problem string a doctor prints.
    """
    if not path.is_file():
        return None, f'no {DECLARATION} (a plugin is a folder with a declaration in it)'
    try:
        decl = json.loads(path.read_text(encoding='utf-8'))
    except OSError as e:
        return None, f'{path} cannot be read: {e}'
    except UnicodeDecodeError as e:
        return None, f'{path} is not valid UTF-8: {e}'
    except json.JSONDecodeError as e:
        return None, f'{path} parse failure: {e}'
    if not isinstance(decl, dict):
        return None, f'{path} must be a JSON object (one declaration per plugin)'
    return decl, None


# ── the scan ─────────────────────────────────────────────────────────────────

def load_all() -> list:
    """Every plugin folder, in folder-name order: `{name, folder, decl, problems}`.

    `name` is the folder's name -- the folder IS the identity -- and `decl` is the parsed declaration
    (or `None` when it could not be read, in which case `problems` says so). Nothing is cached and
    nothing raises: an empty plugins directory answers an empty list, and one broken folder does not
    hide its neighbours.
    """
    out = []
    for folder in _plugin_folders():
        decl, problem = _read_declaration(folder / DECLARATION)
        out.append({
            'name': folder.name,
            'folder': folder,
            'decl': decl,
            'problems': [] if problem is None else [problem],
        })
    return out


def scan_problems() -> list:
    """What is wrong with the plugins directory itself (nothing, for the normal cases).

    A missing directory is not a problem: zero plugin folders is a legal project, and every door
    here answers empty for it. A path that exists but cannot be listed IS one, because it would
    otherwise read as "no plugins installed" while plugins are sitting there.
    """
    root = plugins_dir()
    if not root.exists():
        return []
    if not root.is_dir():
        return [f'{root} is not a directory (the plugins directory holds one folder per plugin)']
    try:
        list(root.iterdir())
    except OSError as e:
        return [f'{root} cannot be scanned: {e}']
    return []


# ── the rules a declaration has to satisfy ──────────────────────────────────

def _decl_of(plugin: dict):
    decl = plugin.get('decl')
    return decl if isinstance(decl, dict) else None


def _names(decl: dict) -> dict:
    """The name groups of one declaration, each as a mapping (`{}` when absent or malformed)."""
    def group(key):
        value = decl.get(key)
        return value if isinstance(value, dict) else {}
    return {'knobs': group('knobs'), 'readings': group('readings'), 'metrics': group('metrics'),
            'commands': group(COMMANDS), SOURCES_GROUP: group(SOURCES_GROUP)}


def _one_word(value) -> bool:
    """Whether one declared value is a single token -- a command's word, or a source's name.

    One token, no blanks: `pf` matches a command word against its first argv as it stands, and a
    source's name is a word of the same kind (a reading names it back verbatim), so anything a shell
    would split is not one. It also does not open with a dash: that spelling is an option, and neither
    table says anything about options.
    """
    return (isinstance(value, str) and bool(value) and not any(ch.isspace() for ch in value)
            and not value.startswith('-'))


def _source_cells(decl: dict) -> dict:
    """One declaration's usable source claims: `{name: {by}}`, in declaration order.

    A cell is usable when its name is one token, the name is not one of the core's own, and it
    states `by` as one word. Everything else is a declaration problem (`_declaration_problems`) and is
    not a source any reading may draw on -- a broken claim is named by the doctor and skipped by the
    merge, the same way a broken knob cell is.
    """
    out = {}
    for key, cell in _names(decl)[SOURCES_GROUP].items():
        if not isinstance(cell, dict) or not _one_word(key) or key in SOURCES:
            continue
        by = cell.get(SOURCE_BY)
        if not _one_word(by):
            continue
        out[key] = {'by': by}
    return out


def _served(decl: dict) -> tuple:
    """What one declaration's readings may draw on: the core's own, then the names it claims.

    One declaration only. A source another plugin claims is not in this tuple: a claimed name is the
    claimant's, not a well every reading may reach into, so B's reading on A's source stays a
    declaration problem (the same answer as a name nobody claims).
    """
    return (*SOURCES, *_source_cells(decl))


def _declaration_problems(plugin: dict) -> list:
    """One declaration's own problems: the shape of its cells, and the references between them.

    The implementation file's presence is not one of them. A word belongs to the declaration, so a
    folder whose code is gone still lists and routes the word it claims -- typing that word is where
    the missing file is found out. `_problems_of` adds that check back for the doctor.
    """
    name = plugin['name']
    out = list(plugin.get('problems') or [])
    decl = _decl_of(plugin)
    if decl is None:
        return out
    declared = decl.get('name')
    if declared is not None and declared != name:
        out.append(f'plugin {name!r} declares the name {declared!r}: the folder is its identity, '
                   f'so the two have to agree')
    timing = decl.get('timing')
    action = decl.get('action')
    # The pair is one cell: stated whole, or not at all. The one declaration that may state neither is
    # the one that brings `pf` a word of its own -- it is asked at no moment of the pipeline -- and a
    # half-stated pair there would name a thing nothing ever asks for. Every other declaration occupies
    # the cell it was written for, and an absent half is named the way it always was.
    spared = bool(commands_of(plugin))
    if spared and (timing is None) != (action is None):
        out.append(f'plugin {name!r}: timing and action travel together -- {timing!r} and '
                   f'{action!r} state only one of the two (state both, or neither)')
    if timing not in TIMINGS and not (spared and timing is None):
        out.append(f'plugin {name!r}: timing {timing!r} is not one of {list(TIMINGS)}')
    if action not in ACTIONS and not (spared and action is None):
        out.append(f'plugin {name!r}: action {action!r} is not one of {list(ACTIONS)}')
    if timing in TIMINGS and action in ACTIONS and (timing, action) not in CELLS:
        served = ', '.join(f'{act} at {when}' for when, act in CELLS)
        out.append(f'plugin {name!r}: {action!r} at {timing!r} is not a cell this core serves '
                   f'({served})')

    out.extend(_file_problems(name, decl.get(FILES)))

    groups = _names(decl)
    knobs, readings, metrics = groups['knobs'], groups['readings'], groups['metrics']
    commands, sources = groups['commands'], groups[SOURCES_GROUP]
    served = _served(decl)                        # what this declaration's readings may draw on
    for key, cell in knobs.items():
        if not isinstance(cell, dict):
            out.append(f'plugin {name!r}: knob {key!r} must be an object '
                       f'(kind, default, values, summary)')
            continue
        kind = cell.get('kind')
        if kind not in KINDS:
            out.append(f'plugin {name!r}: knob {key!r} has kind {kind!r}, which is not one of '
                       f'{list(KINDS)}')
        if kind == 'word' and cell.get('values') is not None:
            values = cell.get('values')
            if (not isinstance(values, list) or not values
                    or not all(isinstance(word, str) for word in values)):
                out.append(f'plugin {name!r}: knob {key!r} takes `values` as a non-empty list of '
                           f'words')
        layers = cell.get('layers')
        if layers is not None:
            if (not isinstance(layers, list) or not layers
                    or not all(isinstance(layer, str) for layer in layers)):
                out.append(f'plugin {name!r}: knob {key!r} takes `layers` as a non-empty list of '
                           f'layer names ({", ".join(LAYERS)})')
            else:
                unknown = [layer for layer in layers if layer not in LAYERS]
                if unknown:
                    out.append(f'plugin {name!r}: knob {key!r} names the layer(s) '
                               f'{", ".join(repr(layer) for layer in unknown)}, which are not on the '
                               f'chain ({", ".join(LAYERS)})')

    for key, cell in readings.items():
        if not isinstance(cell, dict):
            out.append(f'plugin {name!r}: reading {key!r} must be an object (source, ask, ...)')
            continue
        source = cell.get('source')
        if source not in served:
            out.append(f'plugin {name!r}: reading {key!r} has source {source!r}, which is neither '
                       f'one of the core\'s ({list(SOURCES)}) nor a source this plugin claims '
                       f'(its {SOURCES_GROUP}: {list(_source_cells(decl))})')
        when = cell.get('when_knob')
        if when and when not in knobs:
            out.append(f'plugin {name!r}: reading {key!r} is narrowed by {when!r}, which the '
                       f'plugin does not declare as a knob')

    for key, cell in metrics.items():
        if not isinstance(cell, dict):
            out.append(f'plugin {name!r}: metric {key!r} must be an object (line, tag, method)')
            continue
        if key not in readings:
            out.append(f'plugin {name!r}: metric {key!r} names a reading the plugin does not '
                       f'declare')
        line = cell.get('line')
        if line not in knobs:
            out.append(f'plugin {name!r}: metric {key!r} is judged against {line!r}, which the '
                       f'plugin does not declare as a knob')
        for field in ('tag', 'method'):
            value = cell.get(field)
            if not isinstance(value, str) or not value.strip():
                out.append(f'plugin {name!r}: metric {key!r} needs a {field} (the string the '
                           f'outside writes down to name this debt)')

    for key, cell in sources.items():
        if not isinstance(cell, dict):
            out.append(f'plugin {name!r}: source {key!r} must be an object ({SOURCE_BY})')
            continue
        if not _one_word(key):
            out.append(f'plugin {name!r}: source {key!r} is not a name (one word, no blanks, not '
                       f'opening with a dash)')
        if key in SOURCES:
            out.append(f'plugin {name!r}: source {key!r} is one of the core\'s own ({list(SOURCES)}) '
                       f'and cannot be claimed: a second home for that meaning is what this cell '
                       f'exists to avoid')
        by = cell.get(SOURCE_BY)
        if not _one_word(by):
            out.append(f'plugin {name!r}: source {key!r} needs a {SOURCE_BY} (the implementation '
                       f'function that computes the number)')

    for key, cell in commands.items():
        if not isinstance(cell, dict):
            out.append(f'plugin {name!r}: command {key!r} must be an object '
                       f'({COMMAND_SUMMARY}, {COMMAND_KEEP_NAME})')
            continue
        if not _one_word(key):
            out.append(f'plugin {name!r}: command {key!r} is not a word `pf` could be given (one '
                       f'word, no blanks, not opening with a dash)')
        summary = cell.get(COMMAND_SUMMARY)
        if not isinstance(summary, str) or not summary.strip():
            out.append(f'plugin {name!r}: command {key!r} needs a {COMMAND_SUMMARY} (the one line '
                       f'`pf --help` prints beside the word)')
        keep = cell.get(COMMAND_KEEP_NAME)
        if keep is not None and not isinstance(keep, bool):
            out.append(f'plugin {name!r}: command {key!r} takes {COMMAND_KEEP_NAME} as true or '
                       f'false (whether the word goes along in the argv)')

    enable = decl.get('enable_knob')
    if enable:
        cell = knobs.get(enable)
        if not isinstance(cell, dict):
            out.append(f'plugin {name!r}: switches on {enable!r}, which the plugin does not '
                       f'declare as a knob')
        elif cell.get('kind') != 'switch':
            out.append(f'plugin {name!r}: switches on {enable!r}, whose kind is '
                       f'{cell.get("kind")!r}; only a switch names on/off')

    return out


def _usable_files(files) -> list:
    """The entries of a `files` list that name a path inside the plugin's own folder.

    A declaration says what it ships; it does not get to point outside the folder that is its
    identity, so an absolute path, a rooted or drive-relative one (`/etc`, `C:etc`), or a `..` is
    not one of these (it is `_file_problems`' finding).
    """
    if not isinstance(files, list):
        return []
    out = []
    for entry in files:
        if not isinstance(entry, str) or not entry.strip():
            continue
        path = Path(entry)
        # a rooted path without a drive (`/etc`) is not `is_absolute()` on Windows, and `folder / it`
        # would land beside the checkout: the root and the drive are the other two tells
        if path.is_absolute() or path.root or path.drive or '..' in path.parts:
            continue
        out.append(entry)
    return out


def _file_problems(name: str, files) -> list:
    """The shape of a declaration's `files` list -- never the disk.

    Whether each path is there is `_problems_of`'s question: the shape is answerable from the
    declaration alone, and keeping the two apart is what lets a declaration be read in a tree other
    than the one it describes.
    """
    if files is None:
        return []
    if not isinstance(files, list):
        return [f'plugin {name!r}: {FILES} must be a list of paths relative to its own folder, '
                f'got {type(files).__name__}']
    out = []
    for entry in files:
        if not isinstance(entry, str) or not entry.strip():
            out.append(f'plugin {name!r}: {FILES} holds {entry!r}, which is not a path')
        elif entry not in _usable_files([entry]):
            out.append(f'plugin {name!r}: {FILES} holds {entry!r}, which is not inside its own '
                       f'folder')
    return out


def _problems_of(plugin: dict) -> list:
    """`_declaration_problems`, plus whether the implementation file is there.

    This is the doctor's whole reading of one folder (`validate()` asks it) and the runtime's own gate
    (`_usable`, which the timing doors read): a folder with no code is one the timing doors skip, and
    the doctor names the missing file. The command face is the one reader that comes through
    `_declaration_problems` instead -- a word is the declaration's, and it stays on the list and on the
    route while the folder's code is missing.
    """
    out = _declaration_problems(plugin)
    if not (plugin['folder'] / IMPLEMENTATION).is_file():
        out.append(f'plugin {plugin["name"]!r}: no {IMPLEMENTATION} in {plugin["folder"]}')
    for entry in _usable_files((_decl_of(plugin) or {}).get(FILES)):
        if not (plugin['folder'] / entry).is_file():
            out.append(f'plugin {plugin["name"]!r}: {entry} is not in its folder, and its '
                       f'declaration lists it under {FILES!r}')
    return out


def _conflicts(plugins: list) -> list:
    """The names two plugins may not share: plugin names, knobs, readings, sources, dedupe tags,
    commands."""
    out = []
    for label, values in (
        ('plugin name', [(plugin['name'], plugin['name']) for plugin in plugins]),
        ('knob', _declared(plugins, 'knobs')),
        ('reading', _declared(plugins, 'readings')),
        ('source', _declared(plugins, SOURCES_GROUP)),
        ('dedupe tag', _declared(plugins, 'metrics', 'tag')),
        ('command', _declared(plugins, COMMANDS)),
    ):
        out.extend(_first_claim(label, values))
    return out


def _declared(plugins: list, group: str, field: str | None = None) -> list:
    """(name, plugin name) pairs for one declaration group: its keys, or one string field of them."""
    out = []
    for plugin in plugins:
        decl = _decl_of(plugin)
        if decl is None:
            continue
        cells = decl.get(group)
        if not isinstance(cells, dict):
            continue
        for key, cell in cells.items():
            value = key if field is None else (cell.get(field) if isinstance(cell, dict) else None)
            if isinstance(value, str) and value.strip():
                out.append((value, plugin['name']))
    return out


def _first_claim(label: str, values: list):
    """One sentence per name a second plugin claims (`values` is ordered by folder)."""
    seen = {}
    for value, owner in values:
        if value in seen:
            yield (f'{label} {value!r} is claimed by both {seen[value]!r} and {owner!r}: '
                   f'two plugins may not share it')
        else:
            seen[value] = owner


def validate() -> list:
    """Every problem the installed plugins have: the doctor's list, and nothing that raises.

    Covered: the plugins directory itself, each declaration's own shape and references (a reading's
    source is one of the core's own or one its own declaration claims; a claim names the function that
    computes the number), the names two plugins may not share, an implementation file that is missing,
    and an implementation that failed to load at runtime. An unknown timing or action is stated here
    rather than silently dropped: the runtime skips that cell, and this is where a human reads why.
    """
    plugins = load_all()
    problems = list(scan_problems())
    for plugin in plugins:
        problems.extend(_problems_of(plugin))
    problems.extend(_conflicts(plugins))
    installed = {plugin['name'] for plugin in plugins}
    problems.extend(_RUNTIME_PROBLEMS[name] for name in sorted(_RUNTIME_PROBLEMS)
                    if name in installed)
    return problems


# ── the validator: the declaration, against the implementation beside it ────

class _StandIn:
    """The context the validator calls an implementation with: a temporary project, its own names.

    The doors are the core `Context`'s (`plugin_host.Context`), answered the way a project that
    configured its knobs but holds nothing yet would: a declared knob reads the first value its cell
    allows (a `number` / `integer` as 1, a `word` as its first declared word, a `switch` as `on`), a
    declared reading the core hands a number for reads 1 -- the ticket's, and one drawn on a source
    this declaration claims -- the line is in service with no tags, and the graph is empty. A reading
    on a source this declaration neither is served nor claims reads nothing here, exactly as the real
    context would answer it: the stand-in says what the real context would say.

    The line carries no tags on purpose: which line a plugin judges is its own business, read off
    the cycle it is handed, and the core guessing a tag on its behalf would be the core naming a
    plugin's concern. A plugin that needs one answers "not known this pass", which is legal.

    **No child process starts and nothing is written.** `capture` answers the empty text without
    running anything, `run` does nothing, and `spec_path` only spells a landing spot: the validator
    must not perform a graph edit or open a session to find out whether an implementation lies.
    """

    def __init__(self, decl: dict, project):
        self.project = Path(project)
        self.cycle = {'id': 'validator', 'title': 'the validator\'s stand-in line', 'tags': [],
                      'status': 'active'}
        self.node_id = None
        self.sig = {}
        self.dag = {'nodes': {}}
        self.target = {}
        self._knobs = {key: _cell_value(cell) for key, cell in _names(decl)['knobs'].items()
                       if isinstance(cell, dict)}
        served = ('ticket', *_source_cells(decl))    # what a real context hands a number for
        self._readings = {key: 1.0 for key, cell in _names(decl)['readings'].items()
                          if isinstance(cell, dict) and cell.get('source') in served}

    def knob(self, name: str):
        return self._knobs.get(name)

    def reading(self, key: str):
        return self._readings.get(key)

    def nodes_with_tag(self, tag: str) -> list:
        return []

    def capture(self, argv) -> str:
        return ''

    def run(self, argv) -> None:
        return None

    def spec_path(self, node_id: str) -> str:
        """Where a node's own text would land -- a placeholder, because that layout is not this
        file's to spell (`nodes.py` owns it, and nothing here writes a file)."""
        return str(self.project / 'stand-in' / f'{node_id}.md')


def _cell_value(cell: dict):
    """One declared knob's stand-in value: the first value the cell's kind allows."""
    kind = cell.get('kind')
    if kind in ('number', 'integer'):
        return 1
    words = cell.get('values')
    if kind == 'word' and isinstance(words, list) and words:
        return words[0]
    return 'on'


def skill_folders(plugin: dict) -> list:
    """The skill folders one plugin ships: `(language, method, folder)`, in folder order.

    The layout is the plugin's own: `<plugin>/skills/<lang>/<method>/`. A skill is the third kind of
    thing a plugin folder carries beside the declaration and the implementation, and it is the thing
    a `method` name points a session at -- so it lives where the plugin lives, and removing the
    folder removes both. A dotted or underscored directory is not a skill (the same cut the plugin
    scan makes); a folder no declaration names is simply another skill the plugin ships.
    """
    root = Path(plugin['folder']) / SKILLS
    out = []
    if not root.is_dir():
        return out
    for lang in sorted(path for path in root.iterdir()
                       if path.is_dir() and not path.name.startswith(('.', '__'))):
        for method in sorted(path for path in lang.iterdir()
                             if path.is_dir() and not path.name.startswith(('.', '__'))):
            out.append((lang.name, method.name, method))
    return out


def skills() -> dict:
    """Every skill the installed plugins ship, by method name: `{method: [(plugin, folder), ...]}`.

    One entry per folder, so a name a plugin ships in two languages appears twice. A name two plugins
    ship is one of the two answers `_skill_problems` reports.
    """
    out = {}
    for plugin in load_all():
        for _lang, method, folder in skill_folders(plugin):
            out.setdefault(method, []).append((plugin['name'], folder))
    return out


def _skill_problems(plugins: list) -> list:
    """The two ways a skill and a declaration can disagree, both read off the folders.

    A plugin's own folders are its inventory: every folder under `SKILLS/<lang>/` is a skill it ships,
    and the names under `metrics` are its own skills. So:

    * a `method` a declaration names must have `SKILL.md` in THAT plugin's own folder -- a name that
      lands on another plugin's skill is a loan, and a skill does not move between plugins: the two
      would be one plugin;
    * a skill name belongs to one plugin. Shipping the same name from two plugin folders is the same
      loan seen from the other side, and it is what makes `~/.agents/skills/<name>` ambiguous.

    A skill no declaration names is not a problem either way: it is a skill the user invokes, and
    `pf doctor` lists it with the plugin that carries it.

    The skill belongs to the plugin, so a project with no plugin has no skill and nothing to report.
    """
    out = []
    owner = {}
    for plugin in plugins:
        name = plugin['name']
        backed = set()
        for lang, method, folder in skill_folders(plugin):
            if (folder / SKILL_FILE).is_file():
                backed.add(method)
            if method not in owner:
                owner[method] = (name, lang)
            elif owner[method][0] != name:
                out.append(f'skill {method!r}: {owner[method][0]!r} ships it under '
                           f'{SKILLS}/{owner[method][1]} and {name!r} under {SKILLS}/{lang} -- '
                           f'a skill belongs to one plugin')
        for method in methods_of(plugin):
            if method not in backed:
                out.append(f'plugin {name!r}: method {method!r} has no skill of its own (no '
                           f'{SKILLS}/<lang>/{method}/{SKILL_FILE} in its folder)')
    return out


def _command_verbs(answer, out: list) -> None:
    """Every command-shaped list in one answer, collected by its verb.

    A command is an argv of strings, and an answer spelling an edit of its own would spell it as a
    list of them; so a list whose elements are all lists/tuples is command-shaped, and anything else
    (a tag list, a paragraph, a number) is not.
    """
    if isinstance(answer, dict):
        for part in answer.values():
            _command_verbs(part, out)
    elif isinstance(answer, (list, tuple)):
        if answer and all(isinstance(part, (list, tuple)) for part in answer):
            out.extend(str(argv[0]) for argv in answer if argv)
        else:
            for part in answer:
                _command_verbs(part, out)


#: The answer keys this check reads: the two list-shaped ones `plugin_host._usable_offer` reads and
#: types as lists of strings (`tags`, `steps`). `fields` is left out on purpose -- its value is the
#: free-shape dict a reader is shown, so a two-dimensional list there is a legitimate field and the
#: core accepts it. A key the core never reads is left out for the same reason: an argv list in it
#: edits nothing, and a flat one is no command list anyway. The rest (`title`, `note`, `spec_of`)
#: carries a string or a callable, where no command list can live.
EDIT_KEYS = ('tags', 'steps')


def _edit_problems(name: str, answer) -> list:
    """The command-shaped lists one answer carries in the keys the core reads -- and there may be none.

    A plugin **declares a node**; the edit that puts it into the graph is the core's, and it has one
    implementation (`insertion.py`), which is also the one home of the verbs it uses. So this check
    does not restate that vocabulary: it reports a command-shaped list in a key the core reads
    (`EDIT_KEYS`), and the sentence stays on that fact -- a list shaped like a command, and which key
    it turned up in -- because what the answer holds is all this side can see.
    """
    out = []
    for key in EDIT_KEYS:
        verbs = []
        _command_verbs(answer.get(key), verbs)
        if verbs:
            out.append(f'plugin {name!r}: its answer carries a command-shaped list in {key!r} '
                       f'({", ".join(repr(verb) for verb in verbs)}); a plugin declares the node '
                       f'and the core performs the edit')
    return out


def _live_call_problems(plugin: dict, decl: dict) -> list:
    """One real call of one implementation, against a stand-in project.

    The door is the one its action has: `offer` for an `insert`, `check` for a `block`. A missing
    door is a plugin that declares a timing nothing will ever serve. An action outside the two is
    skipped -- `validate()` names it, and there is no door to call.

    **The two doors answer different shapes, and they are read as such.** An `insert` door answers an
    offer object; a `block` door answers a reason string or nothing, and that string is prose for a
    human. Only the first kind is walked for its keys (`_edit_problems`), because only it has any.

    A load failure is skipped here for the same reason: it is `_RUNTIME_PROBLEMS`' entry, printed by
    `validate()`, and calling a module that did not load is not a second finding.
    """
    name = plugin['name']
    door = {'insert': 'offer', 'block': 'check'}.get(decl.get('action'))
    if door is None:
        return []
    impl = impl_of(name)
    if impl is None:
        return []
    call = getattr(impl, door, None)
    if not callable(call):
        return [f'plugin {name!r}: {IMPLEMENTATION} has no {door}(), so the {decl.get("action")!r} '
                f'timing it declares can never be served']
    try:
        with tempfile.TemporaryDirectory(prefix='pf-plugin-check-') as tmp:
            answer = call(_StandIn(decl, tmp))
    except (Exception, SystemExit) as e:      # noqa: BLE001 -- a plugin's door may not raise
        return [f'plugin {name!r}: {door}() raised on a stand-in project: {e!r}']
    if not answer:
        # "Nothing from me this pass" is always a legal answer: it is what a project with no debt
        # left, no line stated and nothing to say gets, and no plugin has to answer every time.
        return []
    if door != 'offer':
        if not isinstance(answer, str):
            return [f'plugin {name!r}: check() answered {type(answer).__name__}, and that timing '
                    f'reads a reason string or nothing']
        return []
    if not isinstance(answer, dict):
        return [f'plugin {name!r}: offer() answered {type(answer).__name__}, and that timing reads '
                f'an offer object or nothing']
    return _edit_problems(name, answer)


def _source_problems(plugins: list) -> list:
    """Every claimed source whose computation the implementation cannot show.

    A claim names, in `by`, the implementation function that computes the number; the doctor is the
    one door that may load the implementation and find out whether that function is there. Both faces
    are answered without running it: the function is looked up, never called -- a number a tool prints
    is the runtime's to fetch, on the pass that needs it. A load failure and a broken declaration are
    skipped here: `validate()` already names both, and a second sentence for one fault is noise.
    """
    out = []
    for plugin in plugins:
        decl = _decl_of(plugin)
        if decl is None:
            continue
        name = plugin['name']
        for source, cell in _source_cells(decl).items():
            if not (plugin['folder'] / IMPLEMENTATION).is_file():
                continue
            impl = impl_of(name)
            if impl is None:
                continue
            if not callable(getattr(impl, cell['by'], None)):
                out.append(f'plugin {name!r}: source {source!r} says its number is computed by '
                           f'{cell["by"]}() in {IMPLEMENTATION}, and there is no such function')
    return out


def verify() -> list:
    """The validator: everything `validate()` answers, plus the half only the implementation can show.

    Four questions, and every sentence that comes back names the plugin:

      * **is the declaration inside the core's closed sets, and do two plugins keep off each other's
        names?** -- `validate()`'s own list, unchanged;
      * **does the method its metrics name have a skill?** -- `skills/<lang>/<method>/SKILL.md` in an
        installed plugin's own folder, and no skill folder whose method nothing names
        (`_skill_problems`);
      * **does every claimed source name a function the implementation has?** -- the `by` cell, looked
        up and not called (`_source_problems`): a claim whose computation is missing would answer
        nothing at every close, silently;
      * **does the implementation keep its side of the bargain?** -- one real call against a stand-in
        project: it must answer (an offer, a reason) or "not known this pass", it must never raise,
        and it may not carry a command-shaped list in a key the core reads (`_live_call_problems`).

    The commands a declaration claims have no door of their own here: their shape is `validate()`'s
    (a word, a line, whether the word goes along) and their routing is `pf`'s. Running one to see
    whether it answers would mean starting a child process, which this file never does -- and so is a
    claimed source's `by`, for the same reason.

    Nothing here raises on a broken plugin -- a broken plugin is a sentence, not an exception. The
    two callers are the test suite and `pf doctor`; the pipeline's route never asks: somebody's
    project must not be stopped mid-run by a plugin that is broken, which is failure isolation and
    nothing about speed.
    """
    plugins = load_all()
    # The loads happen FIRST: `validate()` reads `_RUNTIME_PROBLEMS`, and an implementation that
    # cannot be loaded has to be on this list rather than discovered at some later close.
    for plugin in plugins:
        if (plugin['folder'] / IMPLEMENTATION).is_file():
            impl_of(plugin['name'])
    problems = validate()
    problems.extend(_skill_problems(plugins))
    problems.extend(_source_problems(plugins))
    for plugin in plugins:
        decl = _decl_of(plugin)
        if decl is not None:
            problems.extend(_live_call_problems(plugin, decl))
    return problems


# ── what the declarations say, merged for the readers ───────────────────────

def knob_specs() -> dict:
    """Every knob the installed plugins declare: `{name: {kind, default, values?, layers, summary,
    plugin}}`.

    Only well-formed cells come out (a kind outside the four kinds is not a knob, and a `layers` cell
    that is not a non-empty subset of the chain is dropped: `validate()` says what is wrong with
    both). A name two plugins both claim is reported by `validate()` and the first folder keeps it --
    but whether a plugin may shadow a name the core already owns is answered by the merger, not here:
    this file does not know the built-in table, and copying it here would be a second place for those
    names.
    """
    out = {}
    for plugin in load_all():
        decl = _decl_of(plugin)
        if decl is None:
            continue
        for key, cell in _names(decl)['knobs'].items():
            if not isinstance(cell, dict) or cell.get('kind') not in KINDS:
                continue
            spec = {
                'kind': cell['kind'],
                'default': cell.get('default'),
                'summary': cell.get('summary') or '',
                'plugin': plugin['name'],
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
    """Every reading the installed plugins declare: `{plugin, key, source, by, ask, when_knob}`.

    Declaration order inside one plugin, plugins in folder order -- the order the declarations were
    written in, which is the order a form or a judgement should walk them in. A reading whose source
    is neither one of the core's own nor a source its own declaration claims is dropped (the doctor
    reports it). `by` is the implementation function that computes the number for a claimed source,
    and `''` for the core's own -- the door that hands the number over reads it from here.
    """
    out = []
    for plugin in load_all():
        decl = _decl_of(plugin)
        if decl is None:
            continue
        claims = _source_cells(decl)
        served = _served(decl)
        for key, cell in _names(decl)['readings'].items():
            if not isinstance(cell, dict) or cell.get('source') not in served:
                continue
            out.append({
                'plugin': plugin['name'],
                'key': key,
                'source': cell['source'],
                'by': claims.get(cell['source'], {}).get('by', ''),
                'ask': cell.get('ask') if isinstance(cell.get('ask'), str) else '',
                'when_knob': cell.get('when_knob') if isinstance(cell.get('when_knob'), str) else '',
            })
    return out


def sources_of(plugin: dict) -> list:
    """The source names one plugin's declaration claims, as `{name, by}`, in declaration order.

    The usable claims only (`_source_cells`): a cell the doctor reports is not a source here either.
    This is the declaration's own words, which is what `pf doctor` lists and what a reader goes
    looking for when it wants to know where one number comes from.
    """
    return [{'name': name, **cell} for name, cell in _source_cells(_decl_of(plugin) or {}).items()]


def sources() -> list:
    """Every source the installed plugins claim: `{plugin, name, by}`, merged for the readers.

    Folder order then declaration order, the same order every other merged table has. A name two
    plugins claim is `_conflicts`' problem and the first folder keeps it, exactly like a knob's or a
    command's first claim. The core's own are not here: `SOURCES` is their one home, and a claim
    that named one of them is a declaration problem.
    """
    out = []
    seen = set()
    for plugin in load_all():
        decl = _decl_of(plugin)
        if decl is None or _declaration_problems(plugin):
            continue
        for name, cell in _source_cells(decl).items():
            if name in seen:
                continue
            seen.add(name)
            out.append({'plugin': plugin['name'], 'name': name, **cell})
    return out


def source_names() -> list:
    """Every reading source in play right now: the core's own, then the names the plugins claim.

    The whole set, for whoever asserts that the core names no other one -- not the set a reading is
    judged against: that one is per declaration (`_served`, the core's own plus the names that
    declaration itself claims), so a name one plugin claims is never handed to another plugin's
    reading.
    """
    return [*SOURCES, *[claim['name'] for claim in sources()]]


def tags_of(plugin: dict) -> list:
    """One plugin's dedupe tags, in declaration order (what a human writes to name the same debt)."""
    decl = _decl_of(plugin) or {}
    out = []
    for cell in _names(decl)['metrics'].values():
        tag = cell.get('tag') if isinstance(cell, dict) else None
        if isinstance(tag, str) and tag.strip() and tag not in out:
            out.append(tag)
    return out


def _command_summary(cell: dict) -> str:
    """One command cell's line for `--help`, or `''` when it does not carry a usable one."""
    summary = cell.get(COMMAND_SUMMARY)
    return summary.strip() if isinstance(summary, str) else ''


def _command_keep_name(cell: dict) -> bool:
    """Whether the word goes along in the argv: the declaration's cell, defaulting to yes.

    Yes is the default because one folder answers all of its commands with the one implementation:
    without the word, a folder carrying two of them could not tell which one was asked for.
    """
    keep = cell.get(COMMAND_KEEP_NAME)
    return True if keep is None else keep is True


def commands_of(plugin: dict) -> list:
    """The words one plugin claims for `pf`, in declaration order.

    What the declaration says, not what the router would take: a word the core already owns, or one a
    second plugin claims too, is still listed here -- both are somebody else's to report (the doctor
    holds the core's table, `_conflicts` the other declarations). A cell that could not become a
    command at all (not an object, a key that is not a word, no line for the help to print) is not a
    word here either: `_problems_of` says why, and the runtime skips it.
    """
    decl = _decl_of(plugin) or {}
    out = []
    for key, cell in _names(decl)['commands'].items():
        if isinstance(cell, dict) and _one_word(key) and _command_summary(cell) and key not in out:
            out.append(key)
    return out


def methods_of(plugin: dict) -> list:
    """One plugin's method names, in declaration order (the skill skills it points a node at)."""
    decl = _decl_of(plugin) or {}
    out = []
    for cell in _names(decl)['metrics'].values():
        method = cell.get('method') if isinstance(cell, dict) else None
        if isinstance(method, str) and method.strip() and method not in out:
            out.append(method)
    return out


def dedupe_tags() -> list:
    """Every dedupe tag the installed plugins declare, in folder/declaration order."""
    out = []
    for plugin in load_all():
        for tag in tags_of(plugin):
            if tag not in out:
                out.append(tag)
    return out


def methods() -> list:
    """Every method name the installed plugins declare, in folder/declaration order."""
    out = []
    for plugin in load_all():
        for method in methods_of(plugin):
            if method not in out:
                out.append(method)
    return out


def commands() -> list:
    """Every command the installed plugins contribute, as the words `pf` hands to a folder.

    One entry per word, of a plugin whose declaration is sound (`_declaration_problems`): the word is
    the declaration's, and nothing here loads an implementation to ask whether the code behind it
    starts. Listing must not mean executing -- an import that prints, writes, reaches the network or
    sleeps would otherwise land on every `pf` call -- so a word stays on this list even when the code
    behind it is broken or gone, and typing that word is where it is found out: `pf` runs the folder's
    script and names the plugin if it will not start. A declaration that cannot be read, or one with a
    problem of its own, contributes nothing here -- there is no data to list -- and `pf doctor` is where
    it is named. In folder order then declaration order: `{name, plugin, target, keep_name, summary}`.

    `target` is the folder's implementation, run as a script -- a plugin is a folder with one
    implementation, and having it run by a command is the second way in it has beside being called at
    its timing. The word two plugins both claim is `_conflicts`' problem, and the first folder keeps
    it, the same way a knob's first claim does. A word the core's own table already carries is NOT
    judged here: this file does not know that table, and copying it in would be a second home for those
    words -- the doctor, which holds both, reports it.
    """
    out = []
    seen = set()
    for plugin in load_all():
        if _decl_of(plugin) is None or _declaration_problems(plugin):
            continue
        decl = _decl_of(plugin)
        for key in commands_of(plugin):
            if key in seen:
                continue
            seen.add(key)
            cell = _names(decl)['commands'][key]
            out.append({
                'name': key,
                'plugin': plugin['name'],
                'target': Path(plugin['folder']) / IMPLEMENTATION,
                'keep_name': _command_keep_name(cell),
                'summary': _command_summary(cell),
            })
    return out


def _usable(plugin: dict) -> bool:
    """Whether this plugin may act at runtime: a readable declaration with no problem of its own."""
    return _decl_of(plugin) is not None and not _problems_of(plugin)


def by_timing(timing: str) -> list:
    """The plugins that act at this timing, in folder order.

    A plugin whose declaration is broken, or whose timing nobody recognizes, is skipped: the runtime
    is tolerant, and the doctor is where it is pointed out.
    """
    return [plugin for plugin in load_all()
            if _usable(plugin) and (_decl_of(plugin) or {}).get('timing') == timing]


def by_action(action: str) -> list:
    """The plugins that produce this action, in folder order (see `by_timing` for the skipping)."""
    return [plugin for plugin in load_all()
            if _usable(plugin) and (_decl_of(plugin) or {}).get('action') == action]


# ── the implementation ──────────────────────────────────────────────────────

def impl_of(name: str):
    """One plugin's implementation module, loaded by path, or `None` when it will not load.

    Loaded once per process (`register` + `reuse`, under a name of its own): the module is stateless
    by contract -- it is called at its timing with the context it is handed and keeps nothing between
    calls -- so every caller shares the one copy. A load failure is recorded and reported by
    `validate()` instead of raised: a broken plugin must not stop the project that is running it.

    A failure leaves nothing registered behind. The loader registers a module before it executes it --
    which a package needs and a plain module does not -- so a run that raised would leave a half-built
    module under the name, and the next `reuse` call would hand it back as a success: within one
    process, a broken plugin's second scan would look healthy. Dropping it makes the next call really
    try the file again, which is what "a later load works" means.
    """
    path = plugins_dir() / name / IMPLEMENTATION
    module_name = 'pf_plugin_' + re.sub(r'\W', '_', name) + '_impl'
    try:
        module = _loader().load_by_path(module_name, path, register=True, reuse=True)
    except (Exception, SystemExit) as e:      # noqa: BLE001 -- a broken plugin must not raise here
        for stale in [key for key in sys.modules
                      if key == module_name or key.startswith(module_name + '.')]:
            del sys.modules[stale]
        _RUNTIME_PROBLEMS[name] = f'plugin {name!r}: {IMPLEMENTATION} failed to load: {e!r}'
        return None
    _RUNTIME_PROBLEMS.pop(name, None)
    return module
