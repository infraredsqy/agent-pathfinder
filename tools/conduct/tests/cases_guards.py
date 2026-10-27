"""Five guards over the sources: the rules that only a red case keeps.

Each of them pins one sentence that otherwise lives in prose, and each is written against the
sources rather than against a snapshot, so a rename that keeps the behaviour keeps the guard green
and a behaviour put back the old way turns it red.

  1. **The approval on a checkpoint line is written by a human action, and nothing else.** A
     `checkpoint` node's delivery carries `approved_at`, and putting the line back in service is the
     human's whole answer at that gate; `pf dag approve` is the other hand that says the same thing.
     The guard walks the call chain from the one write site outwards and requires the two human doors
     -- and no third caller anywhere in the shipped tree.
  2. **The core names no installed plugin.** The six name groups a declaration INVENTS for itself --
     its knob keys, its readings, its claimed source names, its command words, its dedupe tags and its
     method names -- may not appear in any module of this tool outside `plugins/`. They are read off
     the installed declarations, never restated here. The folder's own name is deliberately not one of
     them: that is the identity a folder is GIVEN, not a word a declaration invents. A word a
     declaration DOES invent is a different matter -- one that claims a common word (a command word, a
     source name) makes that string its own, and the plain substring scan below is what keeps the core
     from going on spelling it for its own work.
  2b. **The skills name no installed plugin either.** The same six groups may not appear in a skill
     under `skills/`: a skill bound to a plugin lives in that plugin's folder, and what a skill says
     instead is "run the command that lists what is installed".
  3. **Every self-entry has a real door.** A word a tool's own parser registers is either a word `pf`
     routes to it or a door another shipped module really calls (`plan` / `apply` / `record`). An
     entry that only a case drives is red: that is the shape of an exit grown to prove itself.
  4. **Every case class that drives a shipped declaration skips once that folder is gone.** The
     classes that bind a metric (`DEBT`) in the two maintenance case modules are imported twice --
     once as the tree is, once with `PF_PLUGINS_DIR` pointed at an empty directory -- and each has to
     report itself skipped in the second. The import is where the decorator decides, so nothing is
     run: a class carrying no decorator (invisible in the ordinary run, where every folder is
     installed) and a class naming a folder this checkout does not ship are both red here.

Nothing here drives the pipeline: the subjects are the sources on disk, the parsers the tools build,
and one import of a case module, read for the marks on its classes; no case starts a session,
dispatches anything or runs a graph edit.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import inspect
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, pipeline  # noqa: E402
from harness import (CONDUCT_DIR, DAG_MANAGE, HANDOFF_PY, SIGNAL_PY, STATE_MANAGE,  # noqa: E402
                     TOOLS_DIR, _fresh_module, patched, plugins_dir, shipped_sources)

__all__ = ['TestTheApprovalHasTwoHumanDoors', 'TestTheCoreNamesNoFeature',
           'TestTheSkillsNameNoFeature', 'TestEverySelfEntryHasARealDoor',
           'TestEveryMetricPassSkipsWithoutItsPlugin']

#: The skill trees this checkout ships, one directory per language. A skill bound to a plugin is not
#: here -- it lives in the plugin's own folder -- so everything under this root is a skill that has to
#: work with no plugin installed.
SKILLS_ROOT = TOOLS_DIR.parent / 'skills'

#: The node cell a human's "continue" at a checkpoint gate lands in. Named once: everything below
#: asks about this one cell, and `dag-manage.py`'s `approve_delivery` is its one write site.
APPROVAL = 'approved_at'

CONDUCT_PIPELINE = CONDUCT_DIR / 'conduct_pipeline.py'
PF = TOOLS_DIR / 'bin' / 'pf.py'


# ── reading the shipped sources ─────────────────────────────────────────────

def sources() -> dict:
    """Every shipped module, as `{label under tools/: parsed module}`."""
    return {path.relative_to(TOOLS_DIR).as_posix():
            ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for path in shipped_sources()}


def _body_nodes(node):
    """One function's own nodes, and not the bodies of the functions nested inside it.

    A nested `def` is its own function and gets its own record; counting its body here as well would
    blame the enclosing function for everything its helpers do.
    """
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield child
        stack.extend(ast.iter_child_nodes(child))


def functions(trees: dict) -> dict:
    """`{(file, function): {'writes': set, 'calls': set}}` for every function in the tree.

    A **write** is an assignment into a record the function was handed -- the root of the target is
    one of the function's own parameters. A function that copies the cell into a dictionary of its own
    (`cmd_info` echoing it out) is not writing it, and would otherwise read as a second writer.
    """
    out = {}
    for label, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            record = out.setdefault((label, node.name), {'writes': set(), 'calls': set()})
            handed = {argument.arg for argument in (*node.args.posonlyargs, *node.args.args,
                                                    *node.args.kwonlyargs)}
            for child in _body_nodes(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    name = func.attr if isinstance(func, ast.Attribute) \
                        else getattr(func, 'id', None)
                    if name:
                        record['calls'].add(name)
                    if isinstance(func, ast.Attribute) and func.attr == 'setdefault' \
                            and child.args and _root_name(func.value) in handed:
                        record['writes'] |= _key_of(child.args[0])
                elif isinstance(child, ast.Subscript) and isinstance(child.ctx, ast.Store) \
                        and _root_name(child.value) in handed:
                    record['writes'] |= _key_of(child.slice)
    return out


def _root_name(expr):
    """The name an access chain starts at: `data['nodes'][uid]['x']` -> `'data'`."""
    while isinstance(expr, (ast.Subscript, ast.Attribute)):
        expr = expr.value
    return expr.id if isinstance(expr, ast.Name) else None


def _key_of(node) -> set:
    """The string key one subscript (or `setdefault` argument) names, when it is a literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    return set()


def callers_of(record: dict, name: str) -> set:
    """Every `(file, function)` that calls `name`, out of one `functions()` reading."""
    return {where for where, entry in record.items() if name in entry['calls']}


# ── 1. the approval: one write site, two human doors ────────────────────────

class TestTheApprovalHasTwoHumanDoors(Base):
    """`approved_at` is written by the two human actions, and by nothing else.

    The chain is the whole guard: one write site (`approve_delivery`), the two doors that reach it
    (`pf dag approve`'s own command, and `approve_node`, the door another tool's command comes
    through), and the one function that reaches that second door -- the release that puts a
    checkpoint line back in service. A third caller anywhere is the failure this rules out: a
    checkpoint gate whose signature the pipeline can write by itself is a gate that says yes.
    """

    #: The write site, and the two doors the human actions come through.
    CHAIN = (
        # the one write site, and the two callers allowed to reach it
        ('approve_delivery', {('dag/dag-manage.py', 'cmd_approve'),
                              ('dag/dag-manage.py', 'approve_node')}),
        # the other tool's door, reached from the human's release and nowhere else
        ('approve_node', {('conduct/state/cycles.py', '_approve_gate_delivery')}),
        # the release itself: the `cycle status` command, and nowhere else
        ('_approve_gate_delivery', {('conduct/state/cycles.py', 'cmd_cycle_status')}),
    )

    def test_the_approval_is_written_only_by_the_two_human_doors(self):
        record = functions(sources())

        writers = {where for where, entry in record.items() if APPROVAL in entry['writes']}
        self.assertEqual({('dag/dag-manage.py', 'approve_delivery')}, writers,
                         f'the signature has a second write site: {sorted(writers)}')

        for writer, allowed in self.CHAIN:
            found = callers_of(record, writer)
            self.assertEqual(allowed, found,
                             f'{writer}() is reached from somewhere else: '
                             f'{sorted(found - allowed)}')

        # And the two doors really are the two human actions: `pf dag approve`'s own command, and the
        # `cycle status` route a human runs to put the line back in service.
        dag = _fresh_module('dag_parser_for_the_guard', DAG_MANAGE)._build_parser()
        verbs = next(action for action in dag._actions
                     if isinstance(action, argparse._SubParsersAction)).choices
        approve = verbs['approve']._defaults.get('func')
        self.assertIsNotNone(approve, 'the `approve` word is not bound to a command any more')
        self.assertEqual('cmd_approve', approve.__name__,
                         'the `approve` word no longer reaches the command this guard pins')

        route = _route('cycle', 'status')
        self.assertEqual({'cmd_cycle_status'}, _called_names(route),
                         'the `cycle status` route no longer reaches the release')


def _route(verb: str, sub: str):
    """One `_ROUTES` entry's value, out of `state-manage.py`'s own source."""
    tree = ast.parse(STATE_MANAGE.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)):
            continue
        if not any(isinstance(target, ast.Name) and target.id == '_ROUTES'
                   for target in node.targets):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if not isinstance(key, ast.Tuple):
                continue
            words = [element.value for element in key.elts
                     if isinstance(element, ast.Constant)]
            if words == [verb, sub]:
                return value
    raise AssertionError(f'`_ROUTES` has no ({verb!r}, {sub!r}) entry')


def _called_names(expr) -> set:
    """The plain functions one expression calls (`lambda a: cmd_x(a.id)` -> {'cmd_x'})."""
    return {node.func.id for node in ast.walk(expr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}


# ── 2. the core names no plugin ────────────────────────────────────────────

class TestTheCoreNamesNoFeature(Base):
    """No module of this tool outside `plugins/` spells a name an installed declaration invents.

    The six groups are read off the declarations, so renaming a knob, a tag, a source name or a
    method inside a plugin folder keeps this green with no edit here -- and a core that starts
    branching on one of them turns it red. `plugins/` itself is where those names belong, so it is
    out of scope; the test suite is not shipped code and is out of scope the same way.

    What a declaration does NOT invent is the name of the folder it sits in (`load_all`'s `name`): a
    folder is given its name and a declaration merely agrees with it. So it is not one of the six --
    but the words a declaration invents are, and this check is a plain substring scan over the shipped
    modules: a declaration that claims a word as ordinary as a command word or a source name makes
    that string the core's to lose, wherever the core used to spell it for its own work.
    """

    def installed_names(self) -> set:
        """Every name an installed declaration invents, as one set of strings."""
        names = set()
        for plugin in pipeline.registry().load_all():
            decl = plugin.get('decl')
            if not isinstance(decl, dict):
                continue
            for group in ('knobs', 'readings', 'commands', 'sources'):
                cells = decl.get(group)
                if isinstance(cells, dict):
                    names |= set(cells)
            for cell in (decl.get('metrics') or {}).values():
                if isinstance(cell, dict):
                    names |= {cell.get('tag'), cell.get('method')}
        return {name for name in names if isinstance(name, str) and name.strip()}

    def test_the_core_names_no_installed_feature(self):
        names = self.installed_names()
        if not names:
            self.skipTest('no plugin is installed: there is no name for the core to carry')

        # every shipped module, not just the pipeline's own: a name that drifts into the graph tool
        # or the pool tool is the same failure -- that tool would then be reading a plugin's word
        shipped = shipped_sources()
        self.assertTrue(shipped, 'the scan found no module of this tool')
        hits = [f'{path.relative_to(TOOLS_DIR).as_posix()}: {name}'
                for path in shipped
                for name in sorted(names)
                if name in path.read_text(encoding='utf-8')]
        self.assertEqual([], hits, f'the core names a plugin: {hits}')


# ── 2b. the skills name no plugin ──────────────────────────────────────────

class TestTheSkillsNameNoFeature(Base):
    """No skill under `skills/` spells a name an installed declaration invents.

    The same six name groups the core guard watches -- a declaration's knob keys, its readings, its
    claimed source names, its commands, its dedupe tags and its method names -- read off the
    declarations, so a rename inside a plugin folder follows with no edit here and a skill that starts
    carrying one turns this red. A skill may say "run the command that lists what is installed"; the
    listing is the declarations' own words.

    A skill bound to a plugin is out of scope by construction: it lives in that plugin's own folder
    (`plugins/<name>/skills/`), where naming the plugin it binds is the point, and where it is
    installed and removed with that plugin.

    The folder name is not one of the six here either, and for the same reason the core guard gives --
    but the words a declaration invents are, so a skill may not carry one any more than a core module
    may: the scan here is case-insensitive, because a skill's prose is read by a human before it is
    run.
    """

    def outside_names(self) -> set:
        """Every name a declaration invents: knobs, readings, commands, sources, tags, methods."""
        names = set()
        for plugin in pipeline.registry().load_all():
            decl = plugin.get('decl')
            if not isinstance(decl, dict):
                continue
            for group in ('knobs', 'readings', 'commands', 'sources'):
                cells = decl.get(group)
                if isinstance(cells, dict):
                    names |= set(cells)
            for cell in (decl.get('metrics') or {}).values():
                if isinstance(cell, dict):
                    names |= {cell.get('tag'), cell.get('method')}
        return {name for name in names if isinstance(name, str) and name.strip()}

    def test_the_skills_name_no_installed_feature(self):
        names = self.outside_names()
        if not names:
            self.skipTest('no plugin is installed: there is no name for a skill to carry')
        skills = SKILLS_ROOT
        self.assertTrue(skills.is_dir(), f'{skills} is not there')

        hits = [f'{path.relative_to(SKILLS_ROOT).as_posix()}: {name}'
                for path in sorted(skills.rglob('*.md'))
                for name in sorted(names)
                if name.lower() in path.read_text(encoding='utf-8').lower()]

        self.assertEqual([], hits, f'a skill carries a plugin\'s name: {hits}')


# ── 3. every self-entry has a real door ─────────────────────────────────────
#: The tool entries whose own parser registers command words. `handoff.py` and `signal.py` register
#: none -- one command each, which is the tool's whole door -- and are still asked, so a word added
#: to one of them is caught the day it appears. A plugin's own tools are not on this list: their
#: words are reached through the word the folder's declaration claims for `pf`, and that routing is
#: `cases_plugin.py`'s question (a folder's command, and its removal with the folder).
ENTRIES = (CONDUCT_PIPELINE, STATE_MANAGE, DAG_MANAGE, HANDOFF_PY, SIGNAL_PY)


def entry_commands(path: Path) -> set:
    """The command words one tool's own parser registers, built without running the tool.

    Two shapes, and both are the tree's own convention: a subparser group (its `choices` are the
    words), or one positional named `command` whose `choices` are the words (`conduct_pipeline.py`).
    A positional with a value vocabulary of its own -- `signal.py`'s status -- is not an entry: it is
    an argument, and calling it a door would ask this guard about the wrong thing.
    """
    module = _fresh_module('entry_probe_' + path.stem.replace('-', '_'), path)
    if hasattr(module, '_build_parser'):
        parser = module._build_parser()
    else:
        caught = {}

        def grab(parser, *args, **kwargs):
            caught['parser'] = parser
            raise SystemExit(0)

        argv = [] if inspect.signature(module.main).parameters else None
        with patched(argparse.ArgumentParser, 'parse_args', grab), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                module.main(argv) if argv is not None else module.main()
            except SystemExit:
                pass
        if 'parser' not in caught:
            raise AssertionError(f'{path.name} built no parser this probe could see')
        parser = caught['parser']
    commands = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            commands |= set(action.choices)
        elif not action.option_strings and action.dest == 'command' and action.choices:
            commands |= set(action.choices)
    return commands


class TestEverySelfEntryHasARealDoor(Base):
    """A word a tool registers for itself is reachable from outside the suite, or it is red.

    Two legal doors, and the argument for each:

      * **`pf` routes to it** -- a verb row (`pf next` hands `next` to `state-manage.py`), a group
        row (`pf cycle list`, `pf dag add`: the group hands the rest of the line over), or a row whose
        name is not one of the tool's own words, which wraps the whole parser;
      * **a shipped module really calls it.** `plan` / `apply` / `record` are `conduct_pipeline.py`'s
        own entry, and `state/nextcmd.py` and `handoff.py` call them: the allowance is written down
        here, once, and each name in it has to keep a caller or it goes red as well.

    A third kind -- a word whose only caller is the case that tests it -- is what the deleted `contract`
    entry was, and it is exactly what this turns red.
    """

    #: The entries with no `pf` word of their own, each reached by a shipped module instead.
    INTERNAL = {CONDUCT_PIPELINE: ('plan', 'apply', 'record')}

    def routing_tables(self, commands: dict):
        """`pf.py`'s two tables: `(named, wrapped)`.

        `named` is `{(tool path, word)}` -- what a `pf` word hands to a tool, verb rows and group rows
        alike. `wrapped` is the set of tools whose whole parser a group hands over (a group word that
        is not one of the tool's own words, or a verb forwarded with `keep_name` off).
        """
        pf = _fresh_module('pf_tables_for_the_guard', PF)
        named, wrapped = set(), set()
        for name, target, keep_name, _blurb in pf.FORWARD_VERBS:
            named.add((target, name))
            if not keep_name:
                wrapped.add(target)
        for name, target, _keep_name, _blurb in pf.FORWARD_GROUPS:
            named.add((target, name))
            if name not in commands.get(target, set()):
                wrapped.add(target)
        return named, wrapped

    def pipeline_calls(self) -> dict:
        """`{entry name: [file labels]}` for every `pipeline.<name>(...)` call in a shipped module."""
        out = {}
        for label, tree in sources().items():
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                owner = node.func.value
                if isinstance(owner, ast.Name) and owner.id == 'pipeline':
                    out.setdefault(node.func.attr, []).append(label)
        return out

    def test_every_registered_entry_is_routed_or_called(self):
        commands = {path: entry_commands(path) for path in ENTRIES}
        named, wrapped = self.routing_tables(commands)
        called = self.pipeline_calls()
        problems = []

        for path in ENTRIES:
            internal = self.INTERNAL.get(path, ())
            for name in sorted(commands[path]):
                if (path, name) in named or path in wrapped or name in internal:
                    continue
                problems.append(f'{path.name}: `{name}` -- no `pf` word reaches it and no shipped '
                                f'module calls it (a door only a case drives)')
            for name in internal:
                if name not in commands[path]:
                    problems.append(f'{path.name}: `{name}` is kept as an internal entry but the '
                                    f'parser no longer registers it')
                elif not called.get(name):
                    problems.append(f'{path.name}: `{name}` is kept as an internal entry but no '
                                    f'shipped module calls it any more')

        self.assertEqual([], problems, 'a self-entry has no real door:\n' + '\n'.join(problems))


# ── 4. every metric pass skips without its plugin ───────────────────────────
#: The two case modules whose test classes bind a metric of their own (`DEBT = POOL` and the like):
#: they drive the two declarations this checkout ships.
METRIC_CASES = (CONDUCT_DIR / 'tests' / 'cases_maintenance.py',
                CONDUCT_DIR / 'tests' / 'cases_maintenance_run.py')


def debt_classes(module) -> dict:
    """`{class name: class}` for every test case in one module that binds a metric for itself.

    A class BINDS one when `DEBT` is in its own body: the mixins carrying the shared cases leave
    theirs at `None`, and the two bindings that name a metric state it on their own class.
    """
    import unittest
    return {name: obj for name, obj in vars(module).items()
            if isinstance(obj, type) and issubclass(obj, unittest.TestCase)
            and 'DEBT' in vars(obj)}


class TestEveryMetricPassSkipsWithoutItsPlugin(Base):
    """A case class that drives a shipped declaration skips itself once that plugin's folder is gone.

    `cases_maintenance.py` and `cases_maintenance_run.py` drive the two declarations this checkout
    ships, and each of their pass classes binds one metric to drive it. The skip is
    `requires_plugin(...)`, and it is decided when the module is IMPORTED: the decorator asks the
    plugin scan whether the folder is there and marks the class when it is not. So both modules are
    imported twice here -- once as the tree is, once with `PF_PLUGINS_DIR` pointed at an empty
    directory -- and the classes are asked directly, with no case run and no project built.

    Three ways one of these classes loses its door, each one red here instead of invisible in the
    ordinary run:

      * **no decorator at all.** Every folder is installed in the ordinary run, so the class runs
        there; with the folder hidden it dies inside its own fixture instead of skipping -- a declared
        knob is not a knob any more, and `cycle policy` refuses it.
      * **a decorator naming a folder this checkout does not ship.** The class skips itself even with
        everything installed, so its subject never runs anywhere.
      * **the criterion going vacuous**: a module stops binding a metric, or stops importing, and the
        halves below would otherwise pass by asking nothing.
    """

    def test_every_metric_pass_skips_without_its_plugin(self):
        for path in METRIC_CASES:
            with self.subTest(case_module=path.name):
                self.check_one_module(path)

    def check_one_module(self, path: Path):
        """One case module's classes, asked in both worlds."""
        here = debt_classes(_fresh_module(f'guard_metrics_{path.stem}', path))
        self.assertTrue(here, f'{path.name} binds no metric any more: this guard asks nothing')
        stuck = sorted(name for name, cls in here.items()
                       if getattr(cls, '__unittest_skip__', False))
        self.assertEqual([], stuck,
                         f'{path.name}: these classes skip with every plugin installed, so the '
                         f'subject they drive never runs anywhere: {stuck}')

        with tempfile.TemporaryDirectory() as empty, plugins_dir(empty):
            hidden = debt_classes(_fresh_module(f'guard_metrics_no_plugins_{path.stem}', path))

        self.assertEqual(sorted(here), sorted(hidden),
                         f'{path.name}: the two imports disagree on which classes bind a metric')
        missing = sorted(name for name, cls in hidden.items()
                         if not getattr(cls, '__unittest_skip__', False))
        self.assertEqual([], missing,
                         f'{path.name}: with no plugin installed these classes run instead of '
                         f'skipping: {missing}')
