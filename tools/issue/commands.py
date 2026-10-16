"""Manage a persistent issue database for code quality tracking.

Usage:
    issue_manager.py <command> [args]

Commands:
    add            Add a new issue
    list           List issues (with optional filters)
    update         Update issue fields
    resolve        Mark issue as resolved
    reject         Mark issue as rejected (requires --reason)
    acknowledge    Mark issue as acknowledged
    remove         Delete an issue entirely
    status         Show summary statistics
    weight         The pool's weighted debt, as one number
    top            Show top N open issues by priority
    by_module      List open issues grouped by module
    stale          List stale open issues (high check_count)
    mark           Record scan-aliveness confirmations (never resolves)
    export_plan    Generate execution plan for given issue IDs
"""

import json
import os
import re
import sys
from pathlib import Path

from storage import (
    SEVERITY_WEIGHTS,
    VALID_DECISIONS,
    VALID_SEVERITIES,
    _auto_id,
    _ensure_plan_dir,
    _get,
    _now,
    _read,
    _write,
)

# ═══════════════════════════════════════════════════════════════════════════════
#  Commands
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_add(args):
    """Add a new issue (single or batch from file)."""
    if args.from_file:
        return cmd_add_from_file(args)

    if not args.id:
        print('Error: --id is required (or use --from-file for batch)', file=sys.stderr)
        sys.exit(1)
    data = _read()
    if args.id in data['issues']:
        print(f'Error: issue "{args.id}" already exists', file=sys.stderr)
        sys.exit(1)
    if args.severity not in VALID_SEVERITIES:
        print(f'Error: invalid severity "{args.severity}". '
              f'Valid: {", ".join(sorted(VALID_SEVERITIES))}',
              file=sys.stderr)
        sys.exit(1)

    plan_path = args.plan_path
    if plan_path:
        _ensure_plan_dir(plan_path)

    now = _now()
    data['issues'][args.id] = {
        'id': args.id,
        'severity': args.severity,
        'module': args.module or '',
        'title': args.title or args.id,
        'decision': args.decision or 'unknown',
        'source': args.source or 'scan',
        'status': 'open',
        'discovered': now,
        'updated_at': now,
        'check_count': 1,
        'files': args.files or [],
        'functions': args.functions or [],
        'description': args.description or '',
        'suggestion': args.suggestion or '',
        'reason': args.reason,
        'plan_path': plan_path,
    }
    _write(data)
    if plan_path:
        print(f'Issue created: {args.id}')
        print(f'Plan path (nonexistent): {os.path.abspath(plan_path)}')
    else:
        print(f'Issue created: {args.id}')


def cmd_add_from_file(args):
    """Batch import issues from a JSON file."""
    try:
        with open(args.from_file, 'r', encoding='utf-8') as f:
            batch = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)

    if not isinstance(batch, list):
        print('Error: file must contain a JSON array', file=sys.stderr)
        sys.exit(1)

    # Phase 1: validate all entries, assign IDs
    data = _read()
    now = _now()
    for i, item in enumerate(batch):
        row = f'row {i + 1}'

        # --- id ---
        uid = item.get('id')
        if not uid:
            if args.auto_id:
                uid = _auto_id(item)
                item['id'] = uid
            else:
                print(f'Error: {row} missing "id" field (use --auto-id to generate)',
                      file=sys.stderr)
                sys.exit(1)

        # --- severity ---
        severity = item.get('severity', 'P3')
        if severity not in VALID_SEVERITIES:
            print(f'Error: {row} (id={uid}) invalid severity "{severity}", '
                  f'valid: {", ".join(sorted(VALID_SEVERITIES))}',
                  file=sys.stderr)
            sys.exit(1)

        # --- decision ---
        decision = item.get('decision', 'unknown')
        if decision not in VALID_DECISIONS:
            print(f'Error: {row} (id={uid}) invalid decision "{decision}", '
                  f'valid: {", ".join(sorted(VALID_DECISIONS))}',
                  file=sys.stderr)
            sys.exit(1)

        # --- files / functions must be lists ---
        for field in ('files', 'functions'):
            val = item.get(field, [])
            if not isinstance(val, list):
                print(f'Error: {row} (id={uid}) "{field}" must be a list, '
                      f'got {type(val).__name__}',
                      file=sys.stderr)
                sys.exit(1)

    # Phase 2: write all (validation passed)
    created = 0
    skipped = 0

    for item in batch:
        uid = item['id']
        if uid in data['issues']:
            print(f'Warning: issue "{uid}" already exists, skipping', file=sys.stderr)
            skipped += 1
            continue

        plan_path = item.get('plan_path')
        if plan_path:
            _ensure_plan_dir(plan_path)

        data['issues'][uid] = {
            'id': uid,
            'severity': item.get('severity', 'P3'),
            'module': item.get('module', ''),
            'title': item.get('title', uid),
            'decision': item.get('decision', 'unknown'),
            'source': item.get('source', 'scan'),
            'status': 'open',
            'discovered': now,
            'updated_at': now,
            'check_count': 1,
            'files': item.get('files', []),
            'functions': item.get('functions', []),
            'description': item.get('description', ''),
            'suggestion': item.get('suggestion', ''),
            'reason': item.get('reason'),
            'plan_path': plan_path,
        }
        created += 1

    _write(data)
    print(f'Batch import: {created} created, {skipped} skipped')


def _brief_issue(issue: dict) -> dict:
    """Compact projection used by --brief output."""
    return {
        'id': issue.get('id'),
        'severity': issue.get('severity', '?'),
        'status': issue.get('status', '?'),
        'title': issue.get('title', ''),
    }


def cmd_list(args):
    """List issues, optionally filtered."""
    data = _read()
    issues = list(data['issues'].values())

    if args.status:
        issues = [i for i in issues if i['status'] == args.status]
    if args.severity:
        issues = [i for i in issues if i['severity'] == args.severity]
    if args.module:
        issues = [i for i in issues if args.module in i.get('module', '')]
    if args.decision:
        issues = [i for i in issues if i.get('decision', 'unknown') == args.decision]
    if args.source:
        issues = [i for i in issues if i.get('source', 'scan') == args.source]

    sev_order = {s: i for i, s in enumerate(sorted(VALID_SEVERITIES))}
    # Stable sort: newest first, then by severity
    issues.sort(key=lambda i: i.get('discovered', ''), reverse=True)
    issues.sort(key=lambda i: sev_order.get(i.get('severity', ''), 99))

    if args.limit > 0:
        issues = issues[:args.limit]

    if args.brief:
        print(json.dumps([_brief_issue(i) for i in issues], ensure_ascii=False))
    else:
        print(json.dumps(issues, indent=2, ensure_ascii=False))


def cmd_update(args):
    """Update fields on an existing issue."""
    data = _read()
    issue = _get(args.id, data)
    now = _now()
    changed = set()

    field_map = {
        'severity': args.severity,
        'decision': args.decision,
        'status': args.status,
        'module': args.module,
        'title': args.title,
        'description': args.description,
        'suggestion': args.suggestion,
        'files': args.files,
        'functions': args.functions,
        'reason': args.reason,
    }

    for field, value in field_map.items():
        if value is not None:
            old_val = issue.get(field)
            issue[field] = value
            if old_val != issue.get(field):
                changed.add(field)

    if args.plan_path is not None:
        old_path = issue.get('plan_path')
        issue['plan_path'] = args.plan_path or None
        if args.plan_path:
            _ensure_plan_dir(args.plan_path)
        if old_path != issue.get('plan_path'):
            changed.add('plan_path')

    if changed:
        issue['updated_at'] = now
        _write(data)
        print(f'Issue updated: {args.id}')
        for f in sorted(changed):
            print(f'  ~ {f}')
    else:
        print('No changes specified', file=sys.stderr)


def cmd_resolve(args):
    """Mark issue as resolved."""
    data = _read()
    issue = _get(args.id, data)
    issue['status'] = 'resolved'
    issue['reason'] = args.reason or issue.get('reason')
    issue['updated_at'] = _now()
    if args.plan_path:
        issue['plan_path'] = args.plan_path
    _write(data)
    print(f'Issue resolved: {args.id}')


def cmd_reject(args):
    """Mark issue as rejected with a reason."""
    if not args.reason:
        print('Error: --reason is required for reject', file=sys.stderr)
        sys.exit(1)
    data = _read()
    issue = _get(args.id, data)
    issue['status'] = 'rejected'
    issue['reason'] = args.reason
    issue['updated_at'] = _now()
    _write(data)
    print(f'Issue rejected: {args.id}')


def cmd_acknowledge(args):
    """Mark issue as acknowledged (seen, deferred)."""
    data = _read()
    issue = _get(args.id, data)
    issue['status'] = 'acknowledged'
    issue['reason'] = args.reason or issue.get('reason')
    issue['updated_at'] = _now()
    _write(data)
    print(f'Issue acknowledged: {args.id}')


def cmd_remove(args):
    """Delete an issue entirely."""
    data = _read()
    _get(args.id, data)
    del data['issues'][args.id]
    _write(data)
    print(f'Issue removed: {args.id}')


ACTIONABLE_STATUSES = frozenset({'open', 'acknowledged', 'resolving'})


def cmd_status(args):
    """Show summary statistics across all issues.

    By default only counts open/acknowledged/resolving issues (actionable).
    Use --verbose to include resolved/rejected and total count.
    """
    data = _read()
    all_issues = list(data['issues'].values())

    if args.verbose:
        issues = all_issues
    else:
        issues = [i for i in all_issues if i.get('status') in ACTIONABLE_STATUSES]

    by_severity = {}
    by_status = {}
    by_module = {}
    by_source = {}
    for i in issues:
        sev = i.get('severity', '?')
        by_severity[sev] = by_severity.get(sev, 0) + 1
        st = i.get('status', '?')
        by_status[st] = by_status.get(st, 0) + 1
        mod = i.get('module', 'unknown')
        by_module[mod] = by_module.get(mod, 0) + 1
        src = i.get('source', 'scan')
        by_source[src] = by_source.get(src, 0) + 1

    stale_count = sum(
        1 for i in all_issues
        if i.get('status') in ('open', 'acknowledged')
        and i.get('check_count', 0) >= 3
    )

    result = {
        'by_severity': dict(sorted(by_severity.items())),
        'by_status': dict(sorted(by_status.items())),
        'by_module': dict(sorted(by_module.items())),
        'by_source': dict(sorted(by_source.items())),
        'stale': stale_count,
    }

    if args.verbose:
        result['total'] = len(all_issues)

    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_weight(args):
    """The pool's weighted debt, as ONE number (default) or its breakdown (`-v`).

    这是管线自己要的那一格读数：pf-conduct 在节点收尾时拿这个数跟这条线自己的
    `issue_pool_weight_limit` 比。数由这个工具算出来 —— 池子是它的正本；会话报上来的
    数没人复核得了。

    只算 actionable 的条目（`ACTIONABLE_STATUSES`，跟 `status` 一个口径），再排掉
    `decision == external`：那些的决定权不在 agent 手上（改架构、动模型、改体验），
    算进来就是一块谁也挪不动的底板，阈值永远回不到线下，维护节点会一轮一轮地插。
    """
    data = _read()
    actionable = [i for i in data['issues'].values()
                  if i.get('status') in ACTIONABLE_STATUSES]
    payable = [i for i in actionable if (i.get('decision') or 'unknown') != 'external']
    weight = sum(SEVERITY_WEIGHTS.get(i.get('severity'), 0) for i in payable)

    if not args.verbose:
        print(weight)
        return

    by_severity = {}
    for issue in payable:
        severity = issue.get('severity', '?')
        by_severity[severity] = by_severity.get(severity, 0) + 1
    held_back = [i for i in actionable if (i.get('decision') or 'unknown') == 'external']
    print(json.dumps({
        'weight': weight,
        'counted': len(payable),
        'by_severity': dict(sorted(by_severity.items())),
        'not_counted_external': len(held_back),
        'not_counted_external_weight': sum(SEVERITY_WEIGHTS.get(i.get('severity'), 0)
                                           for i in held_back),
    }, indent=2, ensure_ascii=False))


def cmd_top(args):
    """Show top N open/acknowledged issues by priority."""
    data = _read()
    issues = [
        i for i in data['issues'].values()
        if i.get('status') in ('open', 'acknowledged')
    ]

    sev_order = {s: i for i, s in enumerate(sorted(VALID_SEVERITIES))}
    # Stable sort: newest first, then by severity × status
    issues.sort(key=lambda i: i.get('discovered', ''), reverse=True)
    issues.sort(key=lambda i: (
        sev_order.get(i.get('severity', ''), 99),
        0 if i.get('status') == 'open' else 1,
    ))

    if args.limit > 0:
        issues = issues[:args.limit]

    if args.brief:
        print(json.dumps([_brief_issue(i) for i in issues], ensure_ascii=False))
    else:
        print(json.dumps(issues, indent=2, ensure_ascii=False))


def cmd_by_module(args):
    """Group open/acknowledged issues by module."""
    data = _read()
    modules = {}
    for issue in data['issues'].values():
        if issue.get('status') in ('open', 'acknowledged'):
            mod = issue.get('module', 'unknown')
            modules.setdefault(mod, []).append(issue)
    print(json.dumps(modules, indent=2, ensure_ascii=False))


def cmd_stale(args):
    """List stale open issues with high check_count."""
    data = _read()
    threshold = args.threshold if args.threshold is not None else 3
    stale = [
        i for i in data['issues'].values()
        if i.get('status') in ('open', 'acknowledged')
        and i.get('check_count', 0) >= threshold
    ]
    stale.sort(key=lambda i: i.get('check_count', 0), reverse=True)
    print(json.dumps(stale, indent=2, ensure_ascii=False))


def cmd_mark(args):
    """Record scan-aliveness confirmations. Never changes any status.

    Mark-sweep separation (Java GC metaphor):
      - mark (this command): confirmed-alive ids get check_count + 1; open
        issues NOT in the marked set are reported back as *candidates*.
      - sweep (explicit, separate): resolving a candidate is always a
        per-issue `resolve <id> --reason` backed by concrete evidence
        (e.g. a scan agent's three-layer "not found anywhere" report).

    "Not marked" is NOT evidence of death: issues added to the library
    after the scan started are unmarked by definition. This command only
    bookkeeps and reports — it resolves nothing, by design.
    """
    data = _read()

    # Filter out empty strings from shell quoting pitfalls
    wanted = {a for a in args.ids if a and a.strip()}

    now = _now()
    marked, unknown = [], []
    for uid in sorted(wanted):
        issue = data['issues'].get(uid)
        if issue is None:
            unknown.append(uid)
            continue
        issue['check_count'] = issue.get('check_count', 0) + 1
        issue['updated_at'] = now
        marked.append(uid)

    marked_set = set(marked)
    candidates = sorted(
        (i for i in data['issues'].values()
         if i.get('status') == 'open' and i.get('id') not in marked_set),
        key=lambda i: i.get('id', ''),
    )

    _write(data)
    print(json.dumps({
        'marked': marked,
        'unknown_ids': unknown,
        'candidates': [
            {'id': i.get('id'), 'severity': i.get('severity'),
             'title': i.get('title'), 'discovered': i.get('discovered')}
            for i in candidates
        ],
        'note': ('candidates are advisory only: issues this scan did not '
                 'confirm. Freshly added ones are unmarked by definition. '
                 'Resolve individually and only with concrete evidence.'),
    }, indent=2, ensure_ascii=False))


def cmd_open_plan(args):
    """Print the plan file path for an issue (if plan_path is set)."""
    data = _read()
    issue = _get(args.id, data)
    plan_path = issue.get('plan_path')
    if not plan_path:
        print(f'Issue "{args.id}" has no plan_path set', file=sys.stderr)
        sys.exit(1)
    resolved = Path(plan_path)
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    print(resolved.resolve())


def cmd_help(args):
    """Show full usage reference."""
    print(__doc__)


def cmd_search(args):
    """Search issues by regex across all text fields.

    Three-tier priority:
      Tier 1: id/title matches (most relevant, full description shown)
      Tier 2: description/suggestion matches (snippet shown, full if 2+ hits)
      Tier 3: files/functions/module matches (matched field+value shown)
    """
    data = _read()
    issues = list(data['issues'].values())

    if args.status:
        issues = [i for i in issues if i['status'] == args.status]
    if args.severity:
        issues = [i for i in issues if i['severity'] == args.severity]
    if args.source:
        issues = [i for i in issues if i.get('source', 'scan') == args.source]

    try:
        pattern = re.compile(args.query, re.IGNORECASE)
    except re.error as e:
        print(f'Error: invalid regex: {e}', file=sys.stderr)
        sys.exit(1)

    def _snippet(text: str) -> str:
        """Return ±20 char window around the first match."""
        m = pattern.search(text)
        if not m:
            return text
        pos = m.start()
        PAD = 20
        start = max(0, pos - PAD)
        end = min(len(text), pos + PAD)
        out = text[start:end]
        if start > 0:
            out = '...' + out
        if end < len(text):
            out = out + '...'
        return out

    tier1 = []  # id/title
    tier2 = []  # description/suggestion
    tier3 = []  # files/functions/module

    for issue in issues:
        uid = issue.get('id', '')
        title = issue.get('title', '')
        desc = issue.get('description', '')
        sugg = issue.get('suggestion', '')
        mod = issue.get('module', '')
        files = issue.get('files', [])
        funcs = issue.get('functions', [])

        id_hit = bool(pattern.search(uid))
        title_hit = bool(pattern.search(title))
        desc_hit = bool(pattern.search(desc))
        sugg_hit = bool(pattern.search(sugg))
        mod_hit = bool(pattern.search(mod))
        files_hit = any(pattern.search(f) for f in files)
        funcs_hit = any(pattern.search(f) for f in funcs)

        base = {
            'id': uid,
            'title': title,
            'severity': issue.get('severity', '?'),
            'status': issue.get('status', '?'),
        }

        if id_hit or title_hit:
            base['match'] = 'id' if id_hit else 'title'
            if desc:
                base['description'] = desc
            tier1.append(base)
        elif desc_hit or sugg_hit:
            field = 'description' if desc_hit else 'suggestion'
            text = desc if desc_hit else sugg
            base['match'] = field
            count = len(list(pattern.finditer(text))) if text else 0
            base['snippet'] = text if count >= 2 else _snippet(text)
            tier2.append(base)
        elif mod_hit or files_hit or funcs_hit:
            if mod_hit:
                base['match'] = 'module'
                base['value'] = mod
            elif files_hit:
                matched = next(f for f in files if pattern.search(f))
                base['match'] = 'files'
                base['value'] = matched
            else:
                matched = next(f for f in funcs if pattern.search(f))
                base['match'] = 'functions'
                base['value'] = matched
            tier3.append(base)

    result = tier1 + tier2 + tier3
    limit = args.limit if args.limit > 0 else len(result)
    print(json.dumps(result[:limit], indent=2, ensure_ascii=False))


def cmd_export_plan(args):
    """Generate a subagent-executable plan for given issue IDs."""
    data = _read()
    ids = args.ids or []
    if not ids:
        print('Error: specify issue IDs to export', file=sys.stderr)
        sys.exit(1)

    issues = []
    for uid in ids:
        issues.append(_get(uid, data))

    sev_order = {s.upper(): i for i, s in enumerate(sorted(VALID_SEVERITIES))}
    issues.sort(key=lambda i: (
        sev_order.get(i.get('severity', '').upper(), 99),
        i.get('discovered', ''),
    ))

    lines = ['# Execution Plan\n']
    for issue in issues:
        sev = issue.get("severity", "?")
        mod = issue.get("module", "") or "?"
        fid = issue["id"]
        lines.append(f'## {fid}  ({sev})')
        if mod:
            lines.append(f'Module: {mod}')
        if issue.get('functions'):
            lines.append(f'Functions: {", ".join(issue["functions"])}')
        if issue.get('files'):
            lines.append(f'Files: {", ".join(issue["files"])}')
        if issue.get('description'):
            lines.append(f'Desc: {issue["description"]}')
        if issue.get('suggestion'):
            lines.append(f'Suggested: {issue["suggestion"]}')
        plan_path = issue.get('plan_path')
        if plan_path:
            resolved = Path(plan_path)
            if not resolved.is_absolute():
                resolved = Path.cwd() / resolved
            lines.append(f'Plan: {resolved.resolve()}')
            if resolved.exists():
                try:
                    content = resolved.read_text(encoding='utf-8')
                    lines.append('')
                    lines.append(content)
                except Exception as e:
                    lines.append(f'  (error reading plan: {e})')
        lines.append('')

    lines.append('---')
    lines.append(f'Total: {len(issues)} issue(s)')
    print('\n'.join(lines))
