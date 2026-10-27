#!/usr/bin/env python3
"""`pf`: one command in front of the tools.

Every skill says `pf next`, `pf handoff`, `pf dag add`: where the tools live is resolved here, once,
from this file's own location, so the prose reads the same on every machine.

Everything else forwards to a tool, keeping the tool's own subcommand names so the docs stay
greppable. Which words those are is the table further down, with the installed plugins' own commands
merged in behind it: `--help` prints that one table and the router walks it, so the two cannot come
apart. A forwarded verb's flags are its tool's: `pf <verb> --help` is where they are. `pf init` and
`pf register` are forwards too: the registration logic lives next to the ledger it serves, in
`conduct/state/initledger.py`.
"""

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

#: The tools this command fronts. `HERE` is `<repo>/tools/bin`, so the tool tree is one level up --
#: the checkout may sit anywhere: nothing below is absolute.
HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
CONDUCT = TOOLS / 'conduct'
DAG = TOOLS / 'dag' / 'dag-manage.py'
STATE_MANAGE = CONDUCT / 'state-manage.py'
HANDOFF = CONDUCT / 'handoff.py'
CONDUCT_PIPELINE = CONDUCT / 'conduct_pipeline.py'
LEDGER_MERGE = CONDUCT / 'ledger-merge.py'
#: Shared plumbing, and every writer treats a missing session note as best effort -- which is exactly
#: why the doctor has to check it by name.
SESSION_NOTE = TOOLS / 'shared' / 'session-note.py'
#: The one scan of installed plugins: the pipeline entry never names it (each plugin's timing goes
#: through it), so it is checked here by name like the other unrouted files.
PLUGIN_REGISTRY = CONDUCT / 'pipeline' / 'plugin_registry.py'

# Bootstrap: the loader is fetched by path too, so every file has to carry this paragraph itself
# (see `shared/loader.py`).
SHARED = TOOLS / 'shared'

_LOADER = None


def _loader():
    """`tools/shared/loader.py`: the one loader, for the modules this tree keeps outside `shared/`."""
    global _LOADER
    if _LOADER is None:
        spec = importlib.util.spec_from_file_location('pf_shared_loader', SHARED / 'loader.py')
        _LOADER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_LOADER)
    return _LOADER


def _ledger():
    """`tools/shared/ledger.py`: where `.pf/` lands and the init gate -- `pf` only reads it, it borrows
    no tool's package."""
    return _loader().load_shared('ledger')

#: This tree's own version. Release tags are `v<version>`, and every build handed to someone
#: carries its own number -- one number, one content.
VERSION = '0.1.0-alpha.1'


def _verb_version(rest: list[str]) -> int:
    """`pf version`: this tree's version, and where the checkout lives."""
    print(f'pf {VERSION} at {HERE} (tools: {TOOLS})')
    return 0


def _verb_migrate(rest: list[str]) -> int:
    """`pf migrate`: the door stays shut until this system itself needs migrating."""
    print('migrate: this door is shut for now -- there is nothing to migrate; it reopens when this '
          'system itself needs migrating.')
    return 0


def _verb_doctor(rest: list[str]) -> int:
    """`pf doctor [--fix]`."""
    return cmd_doctor(fix='--fix' in rest)


#: The words `pf` itself runs. One row each: the name the router matches, the form `--help` prints,
#: the one line of sense, and the function that answers it. Their flags are printed here because
#: nothing else prints them.
SELF_VERBS = (
    ('doctor', 'doctor [--fix]',
     'read the health of this checkout and this project', _verb_doctor),
    ('version', 'version', 'print the version and where this checkout lives', _verb_version),
    ('migrate', 'migrate',
     'inactive for now -- it reopens when this system itself needs migrating', _verb_migrate),
)

#: The words `pf` hands to a tool. One row each: the name, the file that answers it, whether the
#: name goes along in the argv (`pf next` reaches `state-manage.py` as `next`, `pf dag add` reaches
#: `dag-manage.py` as `add`), and the one line of sense `--help` prints.
#:
#: A forwarded verb's flags live in the tool's own parser, and `pf <verb> --help` prints them: that
#: parser is the authority, so no flags are kept here.
#:
#: These two tables are static: the words this checkout owns. An installed plugin contributes words of
#: its own, and `_routing()` merges them in behind these -- one table, printed by `usage()` and walked
#: by `main()`, with the core's row keeping its name where the two collide.
FORWARD_VERBS = (
    ('init', STATE_MANAGE, True, 'put the ledger in place (run once per project)'),
    ('register', STATE_MANAGE, True, "write the ledger's git merge driver into this project"),
    ('next', STATE_MANAGE, True, 'show (or do) the next pipeline action'),
    ('dispatch', STATE_MANAGE, True, 'start the session for the node the cycle holds'),
    ('clean-temp', STATE_MANAGE, True, "list (or remove) every node's temp/ directory"),
    ('fork-worktree', STATE_MANAGE, True, "fork one node's parallel-mode worktree"),
    ('handoff', HANDOFF, False, "a worker's own closing call for the line it just ran"),
    ('merge-ledger', LEDGER_MERGE, False, 'the three-way merge driver git calls'),
)

#: The groups: a word whose remaining argv names one of its own verbs. Which verbs those are is the
#: parser behind the group -- `pf cycle --help`, `pf dag --help` -- never a list kept here.
FORWARD_GROUPS = (
    ('cycle', STATE_MANAGE, True, 'one line of work'),
    ('dag', DAG, False, 'the graph'),
    ('config', STATE_MANAGE, True, "this machine's and this project's settings"),
    ('instruction', STATE_MANAGE, True, 'standing user instructions'),
    ('session', STATE_MANAGE, True, "a node's own conversations, which live on this machine only"),
)

#: The files no verb names: the pipeline entry every verb file loads, the shared session note, and the
#: registry the installed plugins are read through. `TOOL_FILES` checks these alongside everything
#: the tables above point at.
UNROUTED_TOOLS = (CONDUCT_PIPELINE, SESSION_NOTE, PLUGIN_REGISTRY)

#: What `doctor` insists on finding: the tables' targets, once each, then the two above -- so a verb
#: added to a table is checked by itself.
TOOL_FILES = tuple(dict.fromkeys(
    [row[1] for row in (*FORWARD_VERBS, *FORWARD_GROUPS)] + list(UNROUTED_TOOLS)))

#: The handlers `pf` itself answers, by the name the router matches.
_SELF_HANDLERS = {row[0]: row[3] for row in SELF_VERBS}

#: The words `pf` answers before it consults any table. One home for them: the router reaches these
#: first, so a plugin's command spelling one of them could never run, and `pf doctor` reports that as
#: it reports any other word this checkout already owns.
HELP_WORDS = ('-h', '--help', 'help')


def _core_words() -> set:
    """Every word this checkout already answers: the tables above, and the help forms."""
    return set(HELP_WORDS) | {row[0] for row in (*SELF_VERBS, *FORWARD_VERBS, *FORWARD_GROUPS)}


def _plugin_commands() -> list:
    """The commands the installed plugins declare, or nothing.

    Nothing, never an exception: the help and the router must answer even when the registry itself
    cannot be read (a missing file, a half-written copy). A declaration that cannot be read, or one
    with a problem of its own, contributes nothing either -- the registry's own `commands()` is where
    that is decided -- and `pf doctor` is where the declaration, the code, or the whole registry is
    named.
    """
    try:
        return _plugin_registry().commands()
    except Exception:                          # noqa: BLE001 -- the door answers, always
        return []


def _routing() -> dict:
    """The one table `pf` routes by: the core's own rows, with the plugins' commands merged in behind.

    `usage()` prints exactly these rows and `main()` walks exactly these rows, so a word a declaration
    contributes shows up in the help and reaches its folder's implementation in the same breath --
    there is no second list for the two readers to drift apart on.

    Static priority is applied here, once: a plugin's word this checkout already owns is dropped from
    the table, so both readers see the core's row and no other. `cmd_doctor` -- the one door holding
    both tables -- names the declaration that lost; the registry cannot, since it does not know this
    checkout's words and a copy of them there would be a second home for them.
    """
    own = _core_words()
    plugins = tuple((entry['name'], entry['target'], entry['keep_name'], entry['summary'])
                    for entry in _plugin_commands() if entry['name'] not in own)
    return {'verbs': FORWARD_VERBS, 'plugins': plugins, 'groups': FORWARD_GROUPS}


def _command_clashes() -> list:
    """The plugin commands whose word this checkout already answers: one sentence per declaration.

    Static priority keeps the core's row, so such a word routes nowhere and the help does not print it
    either -- it is unreachable, and that is what the sentence says.
    """
    own = _core_words()
    return [f'plugin {entry["plugin"]!r}: command {entry["name"]!r} is a word this checkout already '
            f'answers, so the core keeps it and this one never routes'
            for entry in _plugin_commands() if entry['name'] in own]


_USAGE_SHAPE = """\
pf -- the agent pipeline's own command

  Level 2 is a verb: it acts on the project or on the pipeline itself. Level 3 is a noun -- a record
  with an identity -- plus the verb that acts on it. So `pf init` and `pf next` are verbs, and
  `pf cycle list`, `pf dag add` are a group's verbs.

"""


#: The listing's numbers: the column the word column opens at, the sense column the static tables use
#: (a table whose widest word does not fit under it opens the sense column further right -- see
#: `_usage_column`), and the width the sense wraps into: what is left of it after the word column.
#: One home for them: `_usage_rows` lays every row out by these.
USAGE_MARGIN, USAGE_COLUMN, USAGE_WIDTH = 12, 42, 100


def _usage_column(pairs) -> int:
    """Where one table's sense column opens: the word column takes the widest word in this table plus
    one blank.

    `USAGE_COLUMN` is the floor, so the tables this checkout ships are laid out as they always were. A
    word a declaration contributes has no length bound -- one token without a blank is a word `pf` can
    be handed, and a word the router takes is not refused over the listing's layout -- so the word
    column grows with the widest word of its own table, keeping one blank between that word and its
    sense: glued together, the row reads back as a single token no table carries.
    """
    widest = max((len(left) for left, _right in pairs), default=0)
    return USAGE_MARGIN + max(USAGE_COLUMN - USAGE_MARGIN, widest + 1)


def _sense_lines(text: str, column: int) -> list:
    """One sense (or the plugin section's title) as the lines it prints beside the word column.

    It wraps in what is left of `USAGE_WIDTH` after `column`. A word so wide that nothing is left
    stands with its sense unwrapped rather than with a column of one character or an error: a
    declaration's word is of no bounded length, and the listing answers for every one of them.
    """
    room = USAGE_WIDTH - column
    return textwrap.wrap(text, room) if room > 0 else [text]


def _usage_rows(pairs) -> str:
    """The word column and the sense column, one row per pair; a long sense wraps under itself.

    The sense column is the table's own (`_usage_column`) and the sense wraps in the room left of it
    (`_sense_lines`), so a word wider than the floor narrows the sense beside it instead of pushing
    the row past the listing's width.
    """
    column = _usage_column(pairs)
    out = []
    for left, right in pairs:
        body = _sense_lines(right, column) or ['']
        out.append(f'{" " * USAGE_MARGIN}{left:<{column - USAGE_MARGIN}}{body[0]}')
        out += [f'{" " * column}{line}' for line in body[1:]]
    return '\n'.join(out)


def usage() -> str:
    """`pf --help`: the shape of the command, then the words the routing table carries.

    The listing is grown from that table, so a verb added to one shows up here by itself, a plugin's
    command shows up with the declaration's own line beside it, and the router and this text cannot
    come apart. A project with no plugin installed gets the listing this checkout always printed: the
    plugin section is only there when there is a word to put in it.
    """
    table = _routing()
    own = _usage_rows([(display, blurb) for _n, display, blurb, _f in SELF_VERBS])
    verbs = _usage_rows([(name, blurb) for name, _t, _k, blurb in table['verbs']])
    groups = _usage_rows([(f'{name} <verb>', blurb) for name, _t, _k, blurb in table['groups']])
    text = (
        _USAGE_SHAPE
        + '\n  pf itself:\n' + own
        + '\n\n  verbs:    (each one hands the rest of the line to a tool)\n' + verbs
        + "\n\n  groups:   (a group's verbs are that tool's: `pf <group> --help`)\n" + groups
    )
    if table['plugins']:
        # The title's second line stands at this table's sense column and wraps like a sense, so it
        # reads as the rest of the title rather than as a word in the column under it.
        rows = [(name, blurb) for name, _t, _k, blurb in table['plugins']]
        column = _usage_column(rows)
        title = ('(a word an installed plugin contributes, answered by that plugin: '
                 '`pf <word> --help`\n'
                 + '\n'.join(' ' * column + line for line
                             in _sense_lines('is its own, and `pf doctor` lists what is installed)',
                                             column)))
        text += ('\n\n  plugins:  ' + title + '\n' + _usage_rows(rows))
    return text + (
        '\n\n  Run `pf <verb> --help` for the verb\'s own flags and values, or `pf <group> --help`\n'
        "  for a group's verbs.\n"
    )


def _run(*argv: str) -> int:
    """Hand over to a tool with the same interpreter and the same stdio, and keep its exit code."""
    env = dict(os.environ, PYTHONUTF8='1')
    return subprocess.call([sys.executable, '-X', 'utf8', *argv], env=env)


#: The bootstrap one plugin's word is run through. It hands the folder's implementation the argv, the
#: `__name__` and the `sys.path` entry a plain `python impl.py …` gives it, and turns a folder that
#: will not start -- no `impl.py`, a syntax error, a raise, a `sys.exit` in its own body -- into one
#: sentence on stderr naming the plugin and the error. Nothing of this runs in `pf`'s own process:
#: listing and routing read the declaration, and the folder's code is executed exactly once, here.
_PLUGIN_BOOTSTRAP = """\
import os
import runpy
import sys
import traceback

_path, _plugin = sys.argv[1], sys.argv[2]
_name = os.path.basename(_path)
# The argv a plain `python impl.py …` gives the folder: the path first, then the rest of the line.
sys.argv = [_path, *sys.argv[3:]]
sys.path.insert(0, os.path.dirname(os.path.abspath(_path)))
try:
    runpy.run_path(_path, run_name='__main__')
except SystemExit as stop:
    # A stop is an answer: the code that came this far keeps its exit code, named so a human reading
    # stderr knows which folder stopped.
    if stop.code not in (None, 0):
        sys.stderr.write('plugin %r: %s stopped with exit code %s\\n' % (_plugin, _name, stop.code))
    raise
except BaseException as exc:
    traceback.print_exc()
    sys.stderr.write('plugin %r: %s failed to load: %r\\n' % (_plugin, _name, exc))
    sys.exit(1)
"""


def _plugin_run(plugin: str, target, keep_name: bool, verb: str, rest) -> int:
    """Hand one plugin's word to its folder, and let a folder that will not start say so by name.

    The word reached this table from the declaration alone, so it is routed even when the code behind
    it is broken or gone: this is where that is found out. `target` is the folder's implementation and
    the folder is the plugin's identity, so the sentence a failure prints names the plugin and the
    error, with a non-zero exit. A folder that starts and exits with a code of its own keeps that code
    -- `pf <word>`'s exit is the plugin's answer -- and the word travels in the argv unless the
    declaration's cell says otherwise.
    """
    return _run('-c', _PLUGIN_BOOTSTRAP, str(target), plugin,
                *((verb, *rest) if keep_name else rest))


def _project_root(start: Path) -> Path:
    """This project's root, read the way every tool `pf` fronts reads it: `start` itself,
    except inside a linked worktree, where it is the main tree the worktree was forked from."""
    return _ledger().resolve_project_root(start)


def _ledger_name() -> str:
    """`.pf/`, named once, in the ledger contract."""
    return _ledger().LEDGER_NAME


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    """`tools/shared/gitcmd.py`: the tree's one way of asking git (cwd, decoding, no-raise all live there)."""
    return _loader().load_shared('gitcmd').git(project, *args)


def _is_tracked(root: Path) -> bool:
    return bool(_git(root, 'ls-files', '--', _ledger_name()).stdout.strip())


def _registered_driver(root: Path) -> str:
    return _git(root, 'config', '--get', f'merge{_ledger_name()}.driver').stdout.strip()


def _plugin_registry():
    """`conduct/pipeline/plugin_registry.py` loaded by path: the tree's one scan of installed plugins.

    By path, like every other part of this tree -- `pf` borrows no tool's package. The doctor is where
    "what is installed" is answered without running the pipeline at all, which is exactly why a
    plugin's declaration is data: a broken plugin must not take this door down with it.
    """
    return _loader().load_by_path('pf_plugin_registry', PLUGIN_REGISTRY,
                                  register=True, reuse=True)


def _knob_shadows() -> list:
    """The declarations that claim a built-in setting's name, through the knob table's own door.

    The two tables meet in `state/knobs.py`, and the merge keeps the built-in cell: the declaration
    is then unreachable while its summary still reads as if it were in force. Nothing else sees both
    tables, so the doctor asks this one -- loaded through the pipeline's door onto it
    (`pipeline/store.py`), by path like every other neighbour.
    """
    store = _loader().load_by_path('pf_pipeline_store', CONDUCT / 'pipeline' / 'store.py',
                                   register=True, reuse=True)
    return store.knob_shadows()


def _knob_line(name: str, spec: dict) -> str:
    """One declared knob as the listing prints it: its name, its kind, its starting value and its own
    one-sentence summary.

    The sentence is the declaration's, read through the registry -- the doctor restates none of it.
    Everything here is data a declaration owns, which is why a listing can answer "how does this
    plugin work" without running it.
    """
    kind = spec.get('kind') or '?'
    details = []
    values = spec.get('values')
    if values:
        details.append('one of ' + ' / '.join(values))
    default = spec.get('default')
    if default is not None and default != '':
        details.append(f'default {default}')
    cell = f' ({kind}' + (f': {", ".join(details)}' if details else '') + ')'
    summary = spec.get('summary') or ''
    return f'{name}{cell}' + (f' — {summary}' if summary else '')


def _report_features() -> int:
    """Say which plugins are installed and how each one works -- its own summary, the knobs it
    declares, the tags and methods the outside writes down, the reading sources it computes itself,
    the commands it contributes -- print every problem the validator found, and count those problems
    for the doctor's exit code.

    Zero plugin folders is a legal project and is reported as such: the core is whole without any
    plugin, and that sentence is what makes it visible.

    This is the door a skill points at when it has to say "go look at what is installed": the listing
    is the declarations' own words, so no skill has to carry a plugin's name, its tags or its method.

    The questions are the validator's (`plugin_registry.verify()`), which is also what the test
    suite asks: the declarations against the core's closed sets, the method skills each one names, and
    one real call of every implementation against a stand-in project -- a plugin whose code cannot
    even compile, or whose door raises, has to be named on this list, not discovered at the next
    close. The one road a project's pipeline takes never asks any of this.

    Two questions are this door's own, because it is the only one holding both tables: a declared knob
    claiming a built-in setting's name, and a declared command claiming a word `pf` already answers.
    """
    registry = _plugin_registry()
    plugins = registry.load_all()
    if not plugins:
        print(f'plugins: 0 installed (the scan reads {registry.plugins_dir()}; one folder per '
              f'plugin is where one would come from)')
    else:
        print(f'plugins: {len(plugins)} installed')
        knobs = registry.knob_specs()
        for plugin in plugins:
            decl = plugin.get('decl')
            decl = decl if isinstance(decl, dict) else {}
            summary = decl.get('summary')
            summary = summary.strip() if isinstance(summary, str) else ''
            print(f'  · {plugin["name"]}' + (f' — {summary}' if summary else ''))
            mine = [(name, spec) for name, spec in knobs.items()
                    if spec.get('plugin') == plugin['name']]
            for index, (name, spec) in enumerate(mine):
                lead = '      knobs: ' if index == 0 else '             '
                print(lead + _knob_line(name, spec))
            tags = registry.tags_of(plugin)
            methods = registry.methods_of(plugin)
            claimed = registry.sources_of(plugin)
            words = registry.commands_of(plugin)
            if tags:
                print('      tags: ' + ', '.join(tags))
            if methods:
                print('      methods: ' + ', '.join(methods))
            if claimed:
                print('      sources: ' + ', '.join(
                    f'{cell["name"]} (computed by {cell["by"]}())' for cell in claimed))
            if words:
                print('      commands: ' + ', '.join(words))
    problems = registry.verify()
    try:
        shadows = _knob_shadows()
    except Exception as e:                     # noqa: BLE001 -- a health check must answer, not die
        shadows = [f'the knob table could not be read: {e}']
    clashes = _command_clashes()
    for problem in (*problems, *shadows, *clashes):
        print(f'  ✗ {problem}')
    return len(problems) + len(shadows) + len(clashes)


def cmd_doctor(fix: bool = False) -> int:
    """Read the health of the checkout and the project; with --fix, run the setup act once."""
    problems = 0
    print(f'pf command: {HERE / "pf.py"}')
    print(f'tool tree: {TOOLS}')
    for path in TOOL_FILES:
        if not path.is_file():
            print(f'  ✗ missing {path}')
            problems += 1
    if sys.version_info < (3, 10):
        print(f'  ✗ Python {sys.version.split()[0]} is too old (this tool tree needs 3.10+)')
        problems += 1
    try:
        problems += _report_features()
    except Exception as e:                     # noqa: BLE001 -- a health check must answer, not die
        print(f'  ✗ the plugin registry could not be read: {e}')
        problems += 1

    root = _project_root(Path.cwd())
    ledger = _ledger().ledger_dir_for(str(root))
    print(f'project root: {root}')
    if not ledger.is_dir():
        print(f'  · there is no {_ledger_name()}/ here yet (a new project runs `pf init`)')
        return 1 if problems else 0

    gaps = []
    markers = _ledger().INIT_MARKERS
    missing = [m for m in markers if not (ledger / m).is_file()]
    if missing:
        gaps.append(f'{_ledger_name()}/ is missing {" and ".join(missing)}')
    if not (ledger / 'AGENTS.md').is_file():
        gaps.append(f'{_ledger_name()}/AGENTS.md is not there')

    git_here = (root / '.git').exists()
    driver = _registered_driver(root) if git_here else ''
    if git_here:
        if not driver:
            gaps.append('the merge driver is not registered')
        elif not driver.startswith('pf '):
            gaps.append('the merge driver is registered in its absolute form '
                        '(it needs re-registering after the checkout moves)')

    for gap in gaps:
        print(f'  · {gap}')
    if not gaps:
        print('  · the ledger, the usage note and the merge driver are all in place')
    elif fix:
        print('  → run `init` once to fill them in (it recognizes what is already there, and '
              're-running has no side effect)')
        code = _run(str(STATE_MANAGE), 'init', '--project', str(root),
                    '--commit' if _is_tracked(root) else '--local')
        if code != 0:
            problems += 1
        else:
            for gap in gaps:
                print(f'  ✓ {gap} -- filled in')
    else:
        print('  (`pf doctor --fix` can fill them in)')

    done = not problems and (not gaps or fix)
    print('every health check passed.' if done else f'{problems + len(gaps)} item(s) left to do.')
    return 0 if done else 1


def main(argv: list[str]) -> int:
    # doctor's readings go out as UTF-8: otherwise the console code page (GBK on this machine)
    # writes the bytes wrong, and a caller decoding them as UTF-8 blows up.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    if not argv or argv[0] in HELP_WORDS:
        print(usage())
        return 0
    verb, rest = argv[0], argv[1:]

    own = _SELF_HANDLERS.get(verb)
    if own is not None:
        return own(rest)
    table = _routing()
    for name, target, keep_name, _blurb in (*table['verbs'], *table['groups']):
        if name == verb:
            return _run(str(target), *((verb, *rest) if keep_name else rest))
    # The plugin rows come last: static priority already dropped every word this checkout owns, so a
    # word reaching here is the folder's own. It is run through the bootstrap rather than handed to the
    # file directly, so a folder whose code is gone or broken answers with one sentence naming it.
    for name, target, keep_name, _blurb in table['plugins']:
        if name == verb:
            return _plugin_run(Path(target).parent.name, target, keep_name, verb, rest)

    print(f'pf: does not recognize `{verb}`.\n', file=sys.stderr)
    print(usage(), file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))