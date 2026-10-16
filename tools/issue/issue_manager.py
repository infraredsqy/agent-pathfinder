#!/usr/bin/env python3
"""issue_manager.py — CLI entry point for the issue database manager.

Subcommand implementations live in commands.py; data access in storage.py.
Run `python issue_manager.py help` for the full usage reference.
"""

import argparse

from commands import (
    cmd_acknowledge,
    cmd_add,
    cmd_add_from_file,
    cmd_by_module,
    cmd_export_plan,
    cmd_mark,
    cmd_help,
    cmd_list,
    cmd_open_plan,
    cmd_remove,
    cmd_reject,
    cmd_resolve,
    cmd_search,
    cmd_stale,
    cmd_status,
    cmd_top,
    cmd_update,
    cmd_weight,
)
from storage import (
    _ledger_lock,
    VALID_DECISIONS,
    VALID_SEVERITIES,
    VALID_SOURCES,
    VALID_STATUSES,
)

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
    p.add_argument('--source', choices=sorted(VALID_SOURCES), default='optimize',
                   help='Source: optimize (scan), manual (user-reported) or conduct '
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
    p.set_defaults(func=cmd_add)

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
    p.set_defaults(func=cmd_list)

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
    p.set_defaults(func=cmd_update)

    # resolve
    p = subs.add_parser('resolve', help='Mark issue as resolved')
    p.add_argument('id', help='Issue ID')
    p.add_argument('--reason', default=None,
                   help='Why resolved (audit: commit hash / review conclusion)')
    p.add_argument('--plan-path', default=None,
                   help='Path to the execution plan used')
    p.set_defaults(func=cmd_resolve)

    # reject
    p = subs.add_parser('reject', help='Mark issue as rejected')
    p.add_argument('id', help='Issue ID')
    p.add_argument('--reason', required=True, help='Why rejected')
    p.set_defaults(func=cmd_reject)

    # acknowledge
    p = subs.add_parser('acknowledge', help='Mark issue as acknowledged')
    p.add_argument('id', help='Issue ID')
    p.add_argument('--reason', default=None, help='Why deferred')
    p.set_defaults(func=cmd_acknowledge)

    # remove
    p = subs.add_parser('remove', help='Delete an issue')
    p.add_argument('id', help='Issue ID')
    p.set_defaults(func=cmd_remove)

    # open-plan
    p = subs.add_parser('open-plan', help='Print the plan file path for an issue')
    p.add_argument('id', help='Issue ID')
    p.set_defaults(func=cmd_open_plan)

    # status
    p = subs.add_parser('status', help='Summary statistics (actionable only by default)')
    p.add_argument('--verbose', '-v', action='store_true',
                   help='Include resolved/rejected and total count')
    p.set_defaults(func=cmd_status)

    # weight
    p = subs.add_parser(
        'weight',
        help="The pool's weighted debt, as one number (the pipeline's own reading)")
    p.add_argument('--verbose', '-v', action='store_true',
                   help='Show how the number was added up')
    p.set_defaults(func=cmd_weight)

    # top
    p = subs.add_parser('top', help='Show top priority issues')
    p.add_argument('--limit', '-n', type=int, default=5)
    p.add_argument('--brief', action='store_true',
                   help='Compact output: id / severity / status / title only')
    p.set_defaults(func=cmd_top)

    # by_module
    p = subs.add_parser('by_module', help='Group issues by module')
    p.set_defaults(func=cmd_by_module)

    # stale
    p = subs.add_parser('stale', help='List stale issues')
    p.add_argument('--threshold', '-t', type=int, default=3,
                   help='Minimum check_count (default: 3)')
    p.set_defaults(func=cmd_stale)

    # mark
    p = subs.add_parser(
        'mark',
        help='Record scan-aliveness confirmations (bookkeeping only; never resolves)',
    )
    p.add_argument('ids', nargs='+',
                   help='Issue IDs confirmed alive by scan agents')
    p.set_defaults(func=cmd_mark)

    # search
    p = subs.add_parser('search', help='Search issues by regex')
    p.add_argument('query', help="Regex pattern (case-insensitive). Always quote: search 'headless'")
    p.add_argument('--status', default=None, choices=sorted(VALID_STATUSES))
    p.add_argument('--severity', default=None, choices=sorted(VALID_SEVERITIES))
    p.add_argument('--source', default=None, choices=sorted(VALID_SOURCES))
    p.add_argument('--limit', '-n', type=int, default=10, help='Max results (0 = unlimited)')
    p.set_defaults(func=cmd_search)

    # export_plan
    p = subs.add_parser('export_plan', help='Generate execution plan')
    p.add_argument('ids', nargs='+', help='Issue IDs to include')
    p.set_defaults(func=cmd_export_plan)

    # help
    p = subs.add_parser('help', help='Show full usage reference')
    p.set_defaults(func=cmd_help)

    return parser


#: The verbs that turn a read of `issues.json` into a write of it. They run under the project's
#: one ledger lock (`.pf/meta/ledger.lock`, shared with conduct and dag-manage) so two sessions
#: recording issues at the same time cannot erase each other's entry.
MUTATING_VERBS = {'add', 'update', 'resolve', 'reject', 'acknowledge', 'remove', 'mark'}


def main():
    parser = _build_parser()
    args = parser.parse_args()
    if args.command in MUTATING_VERBS:
        with _ledger_lock():
            args.func(args)
    else:
        args.func(args)


if __name__ == '__main__':
    main()
