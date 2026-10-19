#!/usr/bin/env python3
"""Load a file from elsewhere in this tree, by path -- the tree's one loader.

`tools/` is not a package and nothing puts it on `sys.path`, so a file that needs a neighbour loads
it from that neighbour's path. That is the convention, and this module is its only implementation:

    load_by_path(name, path, *, package_dir=None, register=False, fresh=False, reuse=False)
    load_shared(stem)

The name is bookkeeping, not an identity. It decides what the loaded file's relative imports
resolve under (a package carries its own name, and `from . import x` finds it in `sys.modules`),
what `sys.modules` holds for it when a caller asks for that, and how the file reads in a traceback.
Nothing looks a module up by a name, so two callers may load one file under two names and neither
disturbs the other.

Who decides how many module objects one file has in a process: the caller, with three knobs --

    fresh     drop every `sys.modules` entry under the name (and under `name.*`) before loading, so
              this copy shares no module object with an earlier one. A caller that wants a module of
              its own says `fresh`: the test harness keeps one state package per throwaway project,
              each with its own path bindings.
    reuse     hand back the module already registered under the name when it came out of this same
              file, instead of executing the file a second time. A caller that needs one instance
              shared by every loader of the file -- the ledger lock's reentrancy record is one per
              process -- says `reuse`.
    register  leave the loaded module in `sys.modules` under the name. A package is always
              registered: it has to be in place before it is executed, or its own submodules cannot
              find it.

Left at their defaults the three make a private copy that touches nothing: the file is executed
under `name`, handed back, and `sys.modules` is left exactly as it was.

This module imports the standard library and nothing else. It is the entry to everything shared, so
it must never depend on anything it is asked to load.
"""

import importlib.util
import sys
from pathlib import Path

#: What this copy of the loader has already handed out, by stem: see `load_shared`.
_LOADED = {}


def _registered_under(name: str, path: Path):
    """The module already in `sys.modules` for this name when it came from this file, else None."""
    module = sys.modules.get(name)
    if module is None:
        return None
    origin = getattr(module, '__file__', None)
    if not origin:
        return None
    try:
        return module if Path(origin).resolve() == path.resolve() else None
    except OSError:
        return None


def load_by_path(name: str, path, *, package_dir=None, register: bool = False,
                 fresh: bool = False, reuse: bool = False):
    """One module (or package) loaded from `path` under `name`. See this module's docstring.

    `package_dir` is the package's own directory; giving it makes `from . import x` resolve, and it
    registers the package, which is the one thing that cannot be left to the caller.
    """
    path = Path(path)
    if fresh:
        for stale in [n for n in sys.modules if n == name or n.startswith(name + '.')]:
            del sys.modules[stale]
    elif reuse:
        already = _registered_under(name, path)
        if already is not None:
            return already
    kwargs = {'submodule_search_locations': [str(package_dir)]} if package_dir else {}
    spec = importlib.util.spec_from_file_location(name, path, **kwargs)
    module = importlib.util.module_from_spec(spec)
    if register or package_dir:
        sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_shared(stem: str):
    """One file out of this directory -- `shared/<stem>.py` -- as the process's one copy of it.

    The door every file outside `tools/shared/` uses for the shared primitives: the folder's layout
    (`<stem>.py`, with a hyphen in the stem where the name reads better: `session-note`) and the
    registration name one process shares it under (`pf_shared_` + the stem without its hyphens) are
    stated here once, and no caller spells either of them again.

    `register` + `reuse` by construction: a shared primitive is one module per process, and the lock's
    reentrancy record is the case that proves it -- a second module object would sit out its own
    caller's lock. It is `load_by_path` underneath, so the count of path-load call sites in this file
    does not grow with it.

    The answer is kept here after the first ask. `reuse` costs a `sys.modules` lookup and two resolved
    paths to prove the name still belongs to this file, and this door sits on the read path of every
    command that wants a ledger path or a record's bytes -- the one copy it hands back never changes,
    so asking twice is work nobody needs.
    """
    module = _LOADED.get(stem)
    if module is None:
        module = load_by_path('pf_shared_' + stem.replace('-', ''),
                              Path(__file__).resolve().parent / f'{stem}.py',
                              register=True, reuse=True)
        _LOADED[stem] = module
    return module