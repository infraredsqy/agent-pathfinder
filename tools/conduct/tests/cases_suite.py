"""The rules about the tests themselves, pinned where the suite can go red on them.

Five of them, each a fault the suite would otherwise carry silently: a session start spliced into a
case's command line (the drill switch would be the only thing left holding it), a `cases_*.py` that
only imports once something else has run, an entry-point header that names a class nothing defines,
`finish_node` walking a node itself instead of handing `Driver` its finish line, and a shipped `.ps1`
edited without its BOM -- the cases drive these through pwsh 7, which reads either encoding, so a
lost BOM leaves the suite green.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import HERE, SPAWN_FLAG, TOOLS_DIR, run_argv  # noqa: E402

__all__ = ['TestNoRealDispatchInTheSuite', 'TestEveryCasesFileStandsAlone',
           'TestTheHeaderNamesRealClasses', 'TestFinishNodeStaysADriverShell',
           'TestShippedPowerShellCarriesItsBom']

#: The repo root this checkout is a worktree of. The shipped tree is everything under it.
ROOT = TOOLS_DIR.parent
#: The one file allowed to bind the spawn flag -- read from `harness.SPAWN_FLAG` (which
#: `harness_sandbox.py` owns), never spelled here: `Sandbox.spawn_next` builds its argv from that
#: name, and it is the suite's only door to a call that could open a session.
SPAWN_OWNER = 'harness_sandbox.py'


def _shipped_ps1():
    """Every `.ps1` under the repo root that the tree actually ships.

    A dot-directory is never part of what ships -- `.git` holds the repository, and `.work/` /
    `.pf/` hold checkouts and ledgers made by running things. Without that cut, one checkout's scan
    would reach into every sibling worktree beside it.
    """
    return sorted(path for path in ROOT.rglob('*.ps1')
                  if not any(part.startswith('.') for part in path.relative_to(ROOT).parts))

#: One child that imports every `cases_*` module the way `python -m unittest conduct.tests.<file>`
#: does -- package-qualified, with this directory NOT on `sys.path`. Each module is imported with the
#: path and the sibling modules a previous iteration left behind cleared, so one file's bootstrap
#: cannot stand in for another's.
_LOAD_ALONE = '\n'.join([
    'import importlib, json, sys, unittest',
    'from pathlib import Path',
    'here = Path(sys.argv[1]).resolve()',
    'clean_path = [p for p in sys.path if Path(p or ".").resolve() != here]',
    'report = {}',
    'for name in sys.argv[2:]:',
    '    sys.path[:] = clean_path',
    '    for mod in [m for m, v in list(sys.modules.items())',
    '                if getattr(v, "__file__", None)',
    '                and Path(v.__file__).resolve().parent == here]:',
    '        del sys.modules[mod]',
    '    try:',
    "        loaded = importlib.import_module('conduct.tests.' + name)",
    '        report[name] = unittest.TestLoader().loadTestsFromModule(loaded).countTestCases()',
    '    except Exception as exc:',
    "        report[name] = type(exc).__name__ + ': ' + str(exc)",
    'print(json.dumps(report))',
])


class TestNoRealDispatchInTheSuite(unittest.TestCase):
    """No case puts a session start on a command line.

    The suite's one door to that flag is `Sandbox.spawn_next`, and it owns both the spelling and the
    drill switch behind it. A case that spells the flag into its own argv -- as a literal, or through
    a name bound to it -- leaves the switch as the only thing between that call and a real session,
    and the switch is one environment variable that a test asserting spawner behaviour deliberately
    takes back out.
    """

    def test_the_flag_is_bound_in_one_place_and_never_spliced_into_an_argv(self):
        trees = {path.name: ast.parse(path.read_text(encoding='utf-8'))
                 for path in sorted(HERE.glob('*.py'))}
        spelled, bound = set(), set()
        for name, tree in trees.items():
            for node in ast.walk(tree):
                if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                        and node.value.value == SPAWN_FLAG):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            spelled.add((name, target.id))
                            bound.add(target.id)
                elif (isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                        and node.module == Path(SPAWN_OWNER).stem):
                    # An aliased import (`import SPAWN_FLAG as SF`) binds the flag under a name the
                    # literal-assignment walk never sees; it is the same flag all the same.
                    for alias in node.names:
                        if alias.name == 'SPAWN_FLAG':
                            bound.add(alias.asname or alias.name)
        # A chain (`SF2 = SF`) keeps the flag under a new name: follow assignments until none is new.
        grown = True
        while grown:
            grown = False
            for name, tree in trees.items():
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
                            and node.value.id in bound):
                        for target in node.targets:
                            if isinstance(target, ast.Name) and target.id not in bound:
                                bound.add(target.id)
                                grown = True

        spliced = []
        for name, tree in trees.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for part in [*node.args, *(kw.value for kw in node.keywords)]:
                    for inner in ast.walk(part):
                        if isinstance(inner, ast.Constant) and inner.value == SPAWN_FLAG:
                            spliced.append(f'{name}:{inner.lineno}')
                        elif (isinstance(inner, ast.Name) and inner.id in bound
                              and name != SPAWN_OWNER):
                            # The flag's own home builds the one sanctioned argv out of the name it
                            # binds; anywhere else that name is the literal in disguise.
                            spliced.append(f'{name}:{inner.lineno} ({inner.id})')

        self.assertEqual({(SPAWN_OWNER, 'SPAWN_FLAG')}, spelled,
                         'a case bound the spawn flag to a name of its own: that flag has one home '
                         'and one door, Sandbox.spawn_next')
        self.assertEqual([], spliced,
                         f'a case spliced --spawn into a command line, leaving the drill switch as '
                         f'the only thing before a real session: {spliced}')


class TestEveryCasesFileStandsAlone(unittest.TestCase):
    """Every `cases_*.py` imports and runs on its own, and the entry point collects all of them."""

    def test_each_cases_file_imports_and_loads_its_cases_alone(self):
        names = sorted(path.stem for path in HERE.glob('cases_*.py'))
        self.assertTrue(names, 'no cases_*.py found in the tests directory')
        proc = run_argv(['-c', _LOAD_ALONE, str(HERE), *names], cwd=TOOLS_DIR, check=False)

        self.assertEqual(0, proc.returncode, proc.stderr)
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(names, sorted(report))
        # `cases_base` is the ground the others stand on: it defines `Base` and no cases of its own.
        broken = {name: value for name, value in report.items()
                  if not isinstance(value, int) or (value == 0 and name != 'cases_base')}
        self.assertEqual({}, broken,
                         f'these cases files do not import/load on their own: {broken}')

    def test_the_entry_point_imports_every_cases_file(self):
        entry = (HERE / 'test_conduct_pipeline.py').read_text(encoding='utf-8')
        imported = {node.module for node in ast.walk(ast.parse(entry))
                    if isinstance(node, ast.ImportFrom) and node.module}
        on_disk = {path.stem for path in HERE.glob('cases_*.py')}

        self.assertEqual(set(), on_disk - imported,
                         'a cases file on disk is not collected by the entry point, so its cases '
                         f'never run: {sorted(on_disk - imported)}')


class TestTheHeaderNamesRealClasses(unittest.TestCase):
    """The entry point's own list of what it covers names classes the suite really collects.

    That list is the suite's only statement of what is guarded; a reader who follows it to a class
    that was renamed or folded away has been told something untrue about the suite.
    """

    def test_every_test_class_the_entry_point_names_exists(self):
        entry = (HERE / 'test_conduct_pipeline.py').read_text(encoding='utf-8')
        header = ast.get_docstring(ast.parse(entry)) or ''
        named = set(re.findall(r'\bTest[A-Z][A-Za-z0-9_]*\b', header))
        self.assertTrue(named, 'the header names no class at all: the reading is wrong')

        defined = set()
        for path in HERE.glob('cases_*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.ClassDef) and node.name.startswith('Test'):
                    defined.add(node.name)
        self.assertEqual(set(), named - defined,
                         'the header names classes no cases file defines: '
                         f'{sorted(named - defined)}')


class TestFinishNodeStaysADriverShell(unittest.TestCase):
    """`finish_node` walks a node by handing `Driver` its finish line, and holds no walk of its own.

    `Driver` owns the stepping, the applied-state contract and the worker's own act; `finish_node` is
    the convenience builder that names one finish line (`_closable`). A walk re-inlined here would
    read perfectly well on its own and drift from the driver's, so the shape is pinned instead of
    left for a reader to notice.
    """

    def test_finish_node_is_one_driver_call_with_a_finish_line(self):
        tree = ast.parse((HERE / 'harness_roles.py').read_text(encoding='utf-8'))
        fn = next((node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == 'finish_node'), None)
        self.assertIsNotNone(fn, 'finish_node is gone: the helpers that call it read as unresolved')

        body = [stmt for stmt in fn.body
                if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))]
        self.assertEqual(1, len(body),
                         f'finish_node carries a walk of its own ({len(body)} statements): the '
                         f'stepping, the applied-state check and the worker act are Driver\'s')
        self.assertIsInstance(body[0], ast.Return, 'finish_node no longer returns its walk')
        call = body[0].value
        self.assertIsInstance(call, ast.Call, f'finish_node returns {ast.dump(call)}')
        self.assertIsInstance(call.func, ast.Attribute, 'finish_node no longer runs the driver')
        self.assertEqual('run', call.func.attr, 'finish_node no longer runs the driver')
        self.assertIsInstance(call.func.value, ast.Call, 'finish_node runs something else')
        self.assertEqual('Driver', getattr(call.func.value.func, 'id', None),
                         'finish_node runs something other than Driver')
        self.assertIn('until', [kw.arg for kw in call.keywords],
                      'finish_node runs Driver without a finish line of its own')


class TestShippedPowerShellCarriesItsBom(unittest.TestCase):
    """Every `.ps1` the tree ships starts with the UTF-8 BOM (the bytes `EF BB BF`).

    Windows PowerShell 5.1 reads a BOM-less file as the machine's ANSI codepage: the Chinese text in
    these scripts turns into mojibake and the parse dies before a single line runs. The cases drive
    them through pwsh 7, which reads either encoding, so dropping one leaves the suite green.
    """

    def test_every_shipped_ps1_starts_with_the_bom(self):
        scripts = _shipped_ps1()
        self.assertTrue(scripts, f'no .ps1 under {ROOT}: the scan root is wrong')

        missing = [str(path.relative_to(ROOT)) for path in scripts
                   if not path.read_bytes().startswith(b'\xef\xbb\xbf')]
        self.assertEqual([], missing,
                         f'these .ps1 files lost their UTF-8 BOM, which PS 5.1 needs to parse '
                         f'them at all: {missing}')