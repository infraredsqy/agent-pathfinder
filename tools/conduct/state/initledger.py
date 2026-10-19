"""`init`: the one setup act, and the only thing that ever writes these two files.

A project either commits its `.pf/` or keeps that directory to itself. Which one it is gets decided
here, once, and every other command just reads what is on disk -- a `.pf/` the project's git tracks
is the committed kind, anything else is the local kind. Nothing else records the choice, so there is
no second place for it to drift out of step with reality.

What init writes:

    .pf/.gitignore       `meta/ worktrees/ temp/` -- the tool's own machinery. Both modes want that
                          ignored: the committed kind needs it out of the project's history, the
                          own-repo kind needs it out of the ledger's own.
    .pf/.gitattributes   no EOL conversion on the record files, and the entry-wise merge for the
                          four that must not be merged line by line
    .pf/AGENTS.md        how this directory is to be used. The tool owns it, not the host: every
                          project that runs pf gets the same discipline, on any harness

and, for `--local`, one line in the repository's `info/exclude`. That file belongs to the clone and
never travels, so no teammate ever sees a line about a directory that does not exist for them.
`--commit` takes the line back out.

`--own-repo` gives `.pf/` a repository of its own, which buys local history and -- once it is given
a remote -- the trip between machines. That `git init` is the only git command this file runs, and it
is a setup act by construction: no runtime command ever touches a repository.
"""

import os
import subprocess
import sys
from pathlib import Path

from .store import LEDGER_NAME, _ledger, _loader, _resolve_project_root

#: The three pieces of machinery under `.pf/`. In the committed kind it keeps them out of the
#: project repository; with a repository of its own it keeps them out of that one.
GITIGNORE_BODY = """\
# This tool's own machinery: where dispatch lands, worktrees, the temp files a node produces.
meta/
worktrees/
temp/
"""

#: The patterns are relative to `.pf/`, so the same file is right in both modes: the committed kind
#: governs `<project>/.pf/…`, and the own-repo kind governs that repository's root.
GITATTRIBUTES_BODY = """\
# Records do not go through EOL conversion: the tool writes CRLF on Windows, and no machine's
# core.autocrlf should rewrite that.
** -text

# Records merge entry-wise, not line by line; the driver lives in tools/conduct/ledger-merge.py,
# and the command is registered in each machine's git config (pf register).
ideas.json merge=pf
state.json merge=pf
instructions.json merge=pf
signals/** merge=pf
"""

#: `.pf/`'s own usage note, written by `init` and carried with the project: everyone who runs pf
#: sees the same discipline, with no hook hung on every tool call for it. To change it, ask the
#: user first.
LEDGER_AGENTS_BODY = """\
# .pf usage note

> This file (`.pf/AGENTS.md`) must not exceed 100 lines.
> This file is written and maintained by this tool at `init` time. **To change it, ask the user first** -- once they have said "go ahead", then touch it.

This directory is the pf pipeline's ledger: `ideas.json`, `state.json`, `instructions.json` and
`signals/` are the pipeline's records, and `nodes/<uuid>/` holds the spec and the deliverable of
each slot (DAG node).

- Records are read and written only through `pf` (`pf dag …`, `pf issue …`, `pf next`); hand-editing the JSON is forbidden, and so is reading `ideas.json` raw -- use the tool's list / plan output.
- Process artifacts belonging to one slot (probes, raw output, self-check scripts) go in `.pf/nodes/<uuid>/temp/`, which is temporary and not committed: do not submit them as deliverables.
- Everything else temporary goes in `.tmp/` at the workspace root.
- `meta/`, `worktrees/` and `nodes/<id>/temp/` are kept out of the repository by `.gitignore`; do not move anything from inside them out by hand and commit it.
"""

#: The marker on those two lines written into `info/exclude`. Harvested by marker: planting one is
#: it, and pulling one is it too.
EXCLUDE_MARK = '# pf working system: this ledger stays on this machine, never in the team repo'

#: Where `pf` sits in this tool tree: three levels up from `<tools>/conduct/state/` is `<tools>`.
TOOLS = Path(__file__).resolve().parent.parent.parent


def driver_command() -> str:
    """The command registered into `merge.pf.driver`.

    `pf` on PATH is preferred: that command carries no absolute path, so moving the checkout
    elsewhere or switching machines keeps the line already registered in the project valid, with no
    per-project redo. When `pf` is not on PATH yet, fall back to the absolute form and ask a human
    to fix PATH -- after that, one more `pf register` makes the registered line
    location-independent.
    """
    for entry in os.environ.get('PATH', '').split(os.pathsep):
        if entry and (Path(entry) / 'pf').is_file():
            return 'pf merge-ledger %O %A %B %P'
    return f'"{sys.executable}" -X utf8 "{TOOLS / "bin" / "pf.py"}" merge-ledger %O %A %B %P'


def register_driver(project: Path) -> str:
    """Register one project's merge driver, and return the command that was registered."""
    command = driver_command()
    _git(project, 'config', 'merge.pf.name', 'pf ledger three-way merge')
    _git(project, 'config', 'merge.pf.driver', command)
    return command


def cmd_register(project: str | None = None) -> None:
    """`register`: register the merge driver, and print the command that was registered.

    `init` already does this along the way; the verb is here so that a project already init-ed can
    top the driver up on its own (re-running it once is enough after a new machine or a new
    checkout location), and so that `pf register` has something to forward to.
    """
    root = Path(project).expanduser().resolve() if project else _resolve_project_root(Path.cwd())
    print(register_driver(root))


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    """`tools/shared/gitcmd.py`: the tree's one way of asking git (cwd, decoding, no-raise all live there)."""
    return _loader().load_shared('gitcmd').git(project, *args)


def _exclude_file(project: Path) -> tuple[Path, str] | None:
    """This project's repository ignore list (`info/exclude`) and the pattern to write into it.

    Returns None when this is not a git project, or when git is not at hand: that kind of project
    has nowhere to be swept into, so the `--local` ignore line has no meaning either. The pattern
    carries the project's prefix relative to the repository root -- a project may be a
    subdirectory of the repository.
    """
    if _git(project, 'rev-parse', '--git-dir').returncode != 0:
        return None
    common = _git(project, 'rev-parse', '--git-common-dir').stdout.strip()
    prefix = _git(project, 'rev-parse', '--show-prefix').stdout.strip()
    if not common:
        return None
    gitdir = Path(common)
    if not gitdir.is_absolute():
        gitdir = (project / gitdir).resolve()
    return gitdir / 'info' / 'exclude', f'/{prefix}{LEDGER_NAME}/'


def _write_exclude(path: Path, pattern: str | None) -> str:
    """Plant or pull the two lines in `info/exclude`, and return one sentence saying what was done.

    Harvested by marker, not by pattern: the pattern moves with the project's place in the
    repository (a subdirectory project), while the marker does not.
    """
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines() if path.exists() else []
    kept, skipped = [], False
    for line in lines:
        if skipped:
            skipped = False
            continue
        if line.strip() == EXCLUDE_MARK:
            skipped = True
            continue
        kept.append(line)
    had = len(kept) != len(lines)
    if pattern is not None:
        while kept and not kept[-1].strip():
            kept.pop()
        if kept:
            kept.append('')
        kept += [EXCLUDE_MARK, pattern]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(kept) + '\n', encoding='utf-8')
        return f'wrote into {path}: {pattern}' + (' (replaced the old pattern)' if had else '')
    if not had:
        return f'{path} had no pf ignore line in the first place.'
    path.write_text('\n'.join(kept) + ('\n' if kept else ''), encoding='utf-8')
    return f'pulled the pf ignore line out of {path}.'


def _untrack(project: Path, ledger: Path) -> str:
    """The local kind: a `.pf/` that already got into the project repository has to come out of the
    index, with the files left on disk.

    Leaving it in would make the mode a lie -- on disk it looks local (the ignore line is there,
    and the next commit does not carry it), while the project repository still takes the whole
    directory along. An ignore line cannot govern a file already tracked; only the index decides
    this one.
    """
    if _git(project, 'ls-files', '--', LEDGER_NAME).stdout.strip() == '':
        return 'the project repository never tracked `.pf/`; the index was left alone.'
    done = _git(project, 'rm', '-r', '-q', '--cached', '--', LEDGER_NAME)
    if done.returncode != 0:
        return f'Warning: `git rm --cached` failed: {done.stderr.strip()}'
    return ('took `.pf/` out of the project repository index (every file is still on disk; commit '
            'this round as you like).')


def cmd_init(project: str | None = None, mode: str = 'local', own_repo: bool = True) -> None:
    """Lay `.pf/` out as a usable evidence directory, and settle whether it enters the project
    repository or stays on this machine alone.

    Idempotent: both files are rewritten from their constants every time, and the ignore line is
    planted or pulled according to the mode. Re-running on an already init-ed project has no side
    effect, and switching modes is just another run -- `--local` and `--commit` each settle the
    `info/exclude` line their own way.
    """
    root = Path(project).expanduser().resolve() if project else _resolve_project_root(Path.cwd())
    ledger = _ledger().ledger_dir_for(root)
    own_repo = own_repo and mode == 'local'
    ledger.mkdir(parents=True, exist_ok=True)
    (ledger / '.gitignore').write_text(GITIGNORE_BODY, encoding='utf-8')
    (ledger / '.gitattributes').write_text(GITATTRIBUTES_BODY, encoding='utf-8')
    (ledger / 'AGENTS.md').write_text(LEDGER_AGENTS_BODY, encoding='utf-8')
    # The config file: lay down an empty object, visible at a glance; an empty object reads as "all
    # factory defaults". When it already has content, not one character is touched -- init is
    # idempotent, and this is a file humans / agents write into, so a re-run must not wipe out what
    # they wrote.
    config_file = _ledger().config_path_of(ledger)
    made_config = not config_file.exists()
    if made_config:
        config_file.write_text('{}\n', encoding='utf-8')

    print(f'Ledger: {ledger}')
    print(f'Mode: {"this machine only (.pf/ does not enter the team repository)" if mode == "local" else "into the version repository with the project"}')
    print(f'Config file: {config_file}'
          f' ({"a freshly laid empty object" if made_config else "already there, left alone"}; '
          f'empty = all factory defaults, and `pf config set` is how to change one)')

    exclude = _exclude_file(root)
    if exclude is None:
        print('This project is not a git checkout: there is no ignore line to speak of, and the '
              'whole pull / push story does not apply either.')
    else:
        path, pattern = exclude
        print(_write_exclude(path, pattern if mode == 'local' else None))
        if mode == 'local':
            print(_untrack(root, ledger))

    if own_repo and not (ledger / '.git').exists():
        done = _git(ledger, 'init', '--quiet')
        if done.returncode == 0:
            print(f'Gave {ledger} a repository of its own: local history starts here, and once a '
                  f'remote is attached to it, it can travel to another machine on its own.')
        else:
            print(f'Warning: `git init` failed: {done.stderr.strip()}', file=sys.stderr)
    elif own_repo:
        print(f'{ledger} already has a repository of its own; it was left alone.')

    if mode == 'local':
        print('A reminder: `.pf/` is ignored, so `git clean -xdf` deletes it, history and all. '
              'Attaching a remote is the only way to get it back.')

    if _git(root, 'rev-parse', '--git-dir').returncode == 0:
        command = register_driver(root)
        print(f'Merge driver registered: merge.pf.driver = {command}')
        if not command.startswith('pf '):
            print('  Note: `pf` is not on PATH yet, so the absolute form was registered instead. '
                  'Put its bin directory on PATH, run `pf register` once more, and this line '
                  'becomes location-independent.')
