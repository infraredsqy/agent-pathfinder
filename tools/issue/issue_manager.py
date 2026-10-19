#!/usr/bin/env python3
"""issue_manager.py — CLI entry point for the issue database manager.

Subcommand implementations live in commands.py; data access in storage.py.
Run `python issue_manager.py help` for the full usage reference.
"""

import argparse
import importlib.util
from pathlib import Path

# 引导：装载器也按路径取，所以这一段只能每个文件自己带（见 `shared/loader.py`）。
HERE = Path(__file__).resolve().parent
SHARED = HERE.parent / 'shared'

_LOADER = None


def _loader():
    """`tools/shared/loader.py`: the one loader, for the modules this tree keeps outside `shared/`."""
    global _LOADER
    if _LOADER is None:
        spec = importlib.util.spec_from_file_location('pf_shared_loader', SHARED / 'loader.py')
        _LOADER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_LOADER)
    return _LOADER


#: 这一对邻居：`commands.py` 装着动词的实现，`storage.py` 装着数据访问。两份都按路径取，名字固定、
#: 留在 `sys.modules` 里并复用 —— `commands.py` 拿到的是同一个 `storage.py` 模块对象，`main()` 在这里
#: `configure()` 钉下的项目，就是这一趟命令读写的项目。
_STORAGE = _loader().load_by_path('pf_issue_storage', HERE / 'storage.py',
                                  register=True, reuse=True)
_COMMANDS = _loader().load_by_path('pf_issue_commands', HERE / 'commands.py',
                                   register=True, reuse=True)

_resolve_project_root = _STORAGE._resolve_project_root
configure = _STORAGE.configure
VALID_DECISIONS = _STORAGE.VALID_DECISIONS
VALID_SEVERITIES = _STORAGE.VALID_SEVERITIES
VALID_SOURCES = _STORAGE.VALID_SOURCES
VALID_STATUSES = _STORAGE.VALID_STATUSES

# ═══════════════════════════════════════════════════════════════════════════════
#  CLI entry point
# ═══════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='issue_manager.py',
        description='Manage a persistent issue database for code quality tracking.',
    )
    subs = parser.add_subparsers(dest='command', required=True)

    # add
    p = subs.add_parser('add', help='Add a new issue (or batch with --from-file)')
    p.add_argument('--id', default=None, help='Semantic issue ID (kebab-case)')
    p.add_argument('--from-file', default=None,
                   help='JSON file with batch of issues (array of objects)')
    p.add_argument('--auto-id', action='store_true',
                   help='Auto-generate IDs from title for entries without id (--from-file only)')
    p.add_argument('--severity', choices=sorted(VALID_SEVERITIES), default='P3',
                   help='Severity level (default: P3)')
    p.add_argument('--decision', choices=sorted(VALID_DECISIONS),
                   help='Decision type: autofix (auto-fixable), external (needs human/agent decision), unknown')
    p.add_argument('--source', choices=sorted(VALID_SOURCES), default='scan',
                   help='Source: scan (pool scan), manual (user-reported) or conduct '
                        '(filed by a session that ran into it)')
    p.add_argument('--module', default=None, help='Module name')
    p.add_argument('--title', default=None, help='Issue title')
    p.add_argument('--description', default=None, help='Detailed description')
    p.add_argument('--suggestion', default=None, help='Suggested fix')
    p.add_argument('--reason', default=None, help='Decision note or context')
    p.add_argument('--files', nargs='*', default=None,
                   help='Affected file paths')
    p.add_argument('--functions', nargs='*', default=None,
                   help='Affected function/class names')
    p.add_argument('--plan-path', default=None,
                   help='Path to detailed design spec file (convention: .pf/issues/specs/<id>.md)')
    p.set_defaults(func=_COMMANDS.cmd_add)

    # list
    p = subs.add_parser('list', help='List issues')
    p.add_argument('--status', default=None, choices=sorted(VALID_STATUSES))
    p.add_argument('--severity', default=None, choices=sorted(VALID_SEVERITIES))
    p.add_argument('--decision', default=None, choices=sorted(VALID_DECISIONS))
    p.add_argument('--source', default=None, choices=sorted(VALID_SOURCES),
                   help='Filter by source')
    p.add_argument('--module', default=None, help='Filter by module (substring)')
    p.add_argument('--limit', '-n', type=int, default=0, help='Max results')
    p.add_argument('--brief', action='store_true',
                   help='Compact output: id / severity / status / title only')
    p.set_defaults(func=_COMMANDS.cmd_list)

    # update
    p = subs.add_parser('update', help='Update issue fields')
    p.add_argument('id', help='Issue ID')
    p.add_argument('--severity', default=None, choices=sorted(VALID_SEVERITIES))
    p.add_argument('--decision', default=None, choices=sorted(VALID_DECISIONS))
    p.add_argument('--status', default=None, choices=sorted(VALID_STATUSES))
    p.add_argument('--module', default=None)
    p.add_argument('--title', default=None)
    p.add_argument('--description', default=None)
    p.add_argument('--suggestion', default=None)
    p.add_argument('--reason', default=None, help='Decision note or context')
    p.add_argument('--files', nargs='*', default=None)
    p.add_argument('--functions', nargs='*', default=None)
    p.add_argument('--plan-path', default=None,
                   help='Path to detailed design spec (empty string to clear)')
    p.set_defaults(func=_COMMANDS.cmd_update)

    # resolve
    p = subs.add_parser('resolve', help='Mark issue as resolved')
    p.add_argument('id', help='Issue ID')
    p.add_argument('--reason', default=None,
                   help='Why resolved (audit: commit hash / review conclusion)')
    p.add_argument('--plan-path', default=None,
                   help='Path to the execution plan used')
    p.set_defaults(func=_COMMANDS.cmd_resolve)

    # reject
    p = subs.add_parser('reject', help='Mark issue as rejected')
    p.add_argument('id', help='Issue ID')
    p.add_argument('--reason', required=True, help='Why rejected')
    p.set_defaults(func=_COMMANDS.cmd_reject)

    # acknowledge
    p = subs.add_parser('acknowledge', help='Mark issue as acknowledged')
    p.add_argument('id', help='Issue ID')
    p.add_argument('--reason', default=None, help='Why deferred')
    p.set_defaults(func=_COMMANDS.cmd_acknowledge)

    # remove
    p = subs.add_parser('remove', help='Delete an issue')
    p.add_argument('id', help='Issue ID')
    p.set_defaults(func=_COMMANDS.cmd_remove)

    # open-plan
    p = subs.add_parser('open-plan', help='Print the plan file path for an issue')
    p.add_argument('id', help='Issue ID')
    p.set_defaults(func=_COMMANDS.cmd_open_plan)

    # status
    p = subs.add_parser('status', help='Summary statistics (actionable only by default)')
    p.add_argument('--verbose', '-v', action='store_true',
                   help='Include resolved/rejected and total count')
    p.set_defaults(func=_COMMANDS.cmd_status)

    # weight
    p = subs.add_parser(
        'weight',
        help="The pool's weighted debt, as one number (the pipeline's own reading)")
    p.add_argument('--verbose', '-v', action='store_true',
                   help='Show how the number was added up')
    p.set_defaults(func=_COMMANDS.cmd_weight)

    # top
    p = subs.add_parser('top', help='Show top priority issues')
    p.add_argument('--limit', '-n', type=int, default=5)
    p.add_argument('--brief', action='store_true',
                   help='Compact output: id / severity / status / title only')
    p.set_defaults(func=_COMMANDS.cmd_top)

    # by_module
    p = subs.add_parser('by_module', help='Group issues by module')
    p.set_defaults(func=_COMMANDS.cmd_by_module)

    # stale
    p = subs.add_parser('stale', help='List stale issues')
    p.add_argument('--threshold', '-t', type=int, default=3,
                   help='Minimum check_count (default: 3)')
    p.set_defaults(func=_COMMANDS.cmd_stale)

    # mark
    p = subs.add_parser(
        'mark',
        help='Record scan-aliveness confirmations (bookkeeping only; never resolves)',
    )
    p.add_argument('ids', nargs='+',
                   help='Issue IDs confirmed alive by scan agents')
    p.set_defaults(func=_COMMANDS.cmd_mark)

    # search
    p = subs.add_parser('search', help='Search issues by regex')
    p.add_argument('query', help="Regex pattern (case-insensitive). Always quote: search 'headless'")
    p.add_argument('--status', default=None, choices=sorted(VALID_STATUSES))
    p.add_argument('--severity', default=None, choices=sorted(VALID_SEVERITIES))
    p.add_argument('--source', default=None, choices=sorted(VALID_SOURCES))
    p.add_argument('--limit', '-n', type=int, default=10, help='Max results (0 = unlimited)')
    p.set_defaults(func=_COMMANDS.cmd_search)

    # export_plan
    p = subs.add_parser('export_plan', help='Generate execution plan')
    p.add_argument('ids', nargs='+', help='Issue IDs to include')
    p.set_defaults(func=_COMMANDS.cmd_export_plan)

    # help
    subs.add_parser('help', help='Show the full usage reference')

    return parser


def main():
    # 项目在这里认一次：记录目录由 `configure()` 钉住，这一趟命令都按它读写。
    configure(_resolve_project_root(Path.cwd()))
    parser = _build_parser()
    args = parser.parse_args()
    if args.command == 'help':
        # `help` and `--help` are one text: the verbs, their flags and their values are what this
        # parser prints, and nothing keeps a second copy of that list to fall out of step with it.
        parser.print_help()
        return
    # 哪些动词要拿项目那把锁：由每个命令体自己声明（改池子的那几条把它们整段读 → 写罩在里面）。
    args.func(args)


if __name__ == '__main__':
    main()
