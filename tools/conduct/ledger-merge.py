#!/usr/bin/env python3
"""The git merge driver for the pf ledger: the argv surface git invokes.

    python ledger-merge.py <base> <ours> <theirs>            # git's %O %A %B
    python ledger-merge.py <base> <ours> <theirs> <path>     # ... and %P, the ledger's own path
    python ledger-merge.py --dry-run <base> <ours> <theirs> [<path>]

Wired up as `merge.pf.driver`; the result is written into <ours> and git carries on. This file is the
command line and nothing else -- which ledger a file is, the four engines, the conflict flags and the
bytes each record is written with are the package next door (`ledger_merge/`, whose `__init__` carries
the rules this driver runs on).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 引导：装载器也按路径取，所以这一段只能每个文件自己带（见 `shared/loader.py`）。
SHARED = HERE.parent / 'shared'
PACKAGE = HERE / 'ledger_merge'

_LOADER = None


def _loader():
    """`tools/shared/loader.py`: the one loader, for the modules this tree keeps outside `shared/`."""
    global _LOADER
    if _LOADER is None:
        spec = importlib.util.spec_from_file_location('pf_shared_loader', SHARED / 'loader.py')
        _LOADER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_LOADER)
    return _LOADER


_PACKAGE = None


def _package():
    """`ledger_merge/`: the engines and the rules, loaded by path through the tree's one loader.

    Registered and reused under one name: the package holds the store binding an in-process caller
    aims at its own project (`ledger_merge.store().bind_paths(...)`), so the copy a caller binds has to
    be the copy this file forwards to.
    """
    global _PACKAGE
    if _PACKAGE is None:
        _PACKAGE = _loader().load_by_path('pf_conduct_ledger_merge', PACKAGE / '__init__.py',
                                          package_dir=PACKAGE, register=True, reuse=True)
    return _PACKAGE


def _parse(argv):
    # `help=` is %-formatted by argparse (`_expand_help` always substitutes), so every literal `%`
    # in one is doubled; the description only substitutes when it names `%(prog)s`, so it carries the
    # driver's own spelling unescaped.
    parser = argparse.ArgumentParser(
        prog='ledger-merge.py',
        description='Three-way merge of one pf ledger file (a git merge driver: %O %A %B [%P]).')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print the decisions and write nothing')
    parser.add_argument('base', help="git's %%O -- the common ancestor")
    parser.add_argument('ours', help="git's %%A -- the merged result is written HERE")
    parser.add_argument('theirs', help="git's %%B -- the other branch")
    parser.add_argument('path', nargs='?', default=None,
                        help="git's %%P -- the ledger's path in the repo, which is what names it "
                             "(under git, %%O/%%A/%%B are temp files)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    return _package().run(_parse(argv))


if __name__ == '__main__':
    sys.exit(main())