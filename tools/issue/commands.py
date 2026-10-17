"""Manage a persistent issue database for code quality tracking.

Every verb this module answers is one entry in `issue_manager.py`'s parser, and that parser is the
one place the verb list lives: `issue_manager.py --help` and `help` print that same text. This file
holds what each verb does, next to the data access in `storage.py`.
"""

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

# `tools/shared/` is the shared layer, and it has no package to import from: every file in it is
# loaded by path through `loader.py`. These two functions are that bootstrap, identical in every file
# that needs it -- a path load, never a name import, because nothing puts `tools/` on `sys.path`.
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


#: `storage.py`：这一对里的数据访问层，同样按路径取。名字固定、留在 `sys.modules` 里并复用 ——
#: `configure()` 在哪个模块对象上钉的项目，就是这一趟命令读写的项目；两份模块对象等于两个项目。
_storage = _loader().load_by_path('pf_issue_storage', HERE / 'storage.py',
                                  register=True, reuse=True)
SEVERITY_WEIGHTS = _storage.SEVERITY_WEIGHTS
VALID_DECISIONS = _storage.VALID_DECISIONS
VALID_SEVERITIES = _storage.VALID_SEVERITIES
_auto_id = _storage._auto_id
_ensure_plan_dir = _storage._ensure_plan_dir
_get = _storage._get
_ledger_lock = _storage._ledger_lock
_now = _storage._now
_read = _storage._read
_write = _storage._write

# ═══════════════════════════════════════════════════════════════════════════════
#  Commands
# ═══════════════════════════════════════════════════════════════════════════════

def _pick(source, name, default=None):
    """One key out of whichever door the values came in through.

    `cmd_add` hands in the parsed command line (`argparse.Namespace`, attributes) and
    `cmd_add_from_file` one entry of a batch file (a dict); a key that is absent, or given as `null`,
    means "not given" either way, and the default applies. That is how the two doors' text keys line
    up; severity / decision / files / functions are read through `_gate` instead.
    """
    value = source.get(name, None) if isinstance(source, dict) else getattr(source, name, None)
    return default if value is None else value


def _gate(source, name, default):
    """One of severity / decision / files / functions: a gate key, read the way both doors agree on.

    A batch row that writes `null` for one of these has said something a record cannot hold, so the
    `null` travels on to the checks in `build_issue_record` and the whole batch stops there, naming
    the row. A key that is simply absent takes the default -- and so does every one of them on the
    command line, where `argparse` hands a flag that was not passed over as `None`.
    """
    if isinstance(source, dict):
        return source.get(name, default)
    value = getattr(source, name, None)
    return default if value is None else value


def build_issue_record(source, uid: str, now: str, where: str = '') -> dict:
    """The one construction of a pool record: both doors into the pool land here.

    The keys, their defaults and the four content checks (severity / decision / files / functions)
    live in this function and nowhere else, so a new field is one line here. `where` names the entry
    in an error message -- 'row 3 (id=x) ' for a batch, empty for a one-at-a-time add.
    """
    severity = _gate(source, 'severity', 'P3')
    if severity not in VALID_SEVERITIES:
        print(f'Error: {where}invalid severity "{severity}", '
              f'valid: {", ".join(sorted(VALID_SEVERITIES))}', file=sys.stderr)
        sys.exit(1)

    decision = _gate(source, 'decision', 'unknown')
    if decision not in VALID_DECISIONS:
        print(f'Error: {where}invalid decision "{decision}", '
              f'valid: {", ".join(sorted(VALID_DECISIONS))}', file=sys.stderr)
        sys.exit(1)

    files = _gate(source, 'files', [])
    functions = _gate(source, 'functions', [])
    for field, value in (('files', files), ('functions', functions)):
        if not isinstance(value, list):
            print(f'Error: {where}"{field}" must be a list, got {type(value).__name__}',
                  file=sys.stderr)
            sys.exit(1)

    return {
        'id': uid,
        'severity': severity,
        'module': _pick(source, 'module', ''),
        'title': _pick(source, 'title', uid) or uid,
        'decision': decision,
        'source': _pick(source, 'source', 'scan'),
        'status': 'open',
        'discovered': now,
        'updated_at': now,
        'check_count': 1,
        'files': files,
        'functions': functions,
        'description': _pick(source, 'description', ''),
        'suggestion': _pick(source, 'suggestion', ''),
        'reason': _pick(source, 'reason'),
        'plan_path': _pick(source, 'plan_path'),
    }


def cmd_add(args):
    """Add a new issue (single or batch from file)."""
    with _ledger_lock():
        if args.from_file:
            return cmd_add_from_file(args)

        if not args.id:
            print('Error: --id is required (or use --from-file for batch)', file=sys.stderr)
            sys.exit(1)
        data = _read()
        if args.id in data['issues']:
            print(f'Error: issue "{args.id}" already exists', file=sys.stderr)
            sys.exit(1)

        record = build_issue_record(args, args.id, _now())
        if record['plan_path']:
            _ensure_plan_dir(record['plan_path'])
        data['issues'][args.id] = record
        _write(data)
        print(f'Issue created: {args.id}')
        if record['plan_path']:
            print(f'Plan path (nonexistent): {os.path.abspath(record["plan_path"])}')


def _free_id(base: str, named: set) -> str:
    """`base`, or `base-2` / `base-3` ... when this batch has already used it.

    Two entries of one scan whose titles share their first 40 characters are two real issues: the
    id is what has to give, so this door appends a number and the caller names it in the output.
    Colliding with an issue that is already in the pool is a different thing with its own answer
    (the skip in phase 2), so nothing here looks at the pool.
    """
    uid, n = base, 2
    while uid in named:
        uid = f'{base}-{n}'
        n += 1
    return uid


def cmd_add_from_file(args):
    """Batch import issues from a JSON file.

    The batch half of `add`: it runs inside the lock `cmd_add` takes.
    """
    try:
        with open(args.from_file, 'r', encoding='utf-8') as f:
            batch = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)

    if not isinstance(batch, list):
        print('Error: file must contain a JSON array', file=sys.stderr)
        sys.exit(1)

    # Phase 1: assign the missing IDs and build every record (each build validates its entry)
    data = _read()
    now = _now()
    records = []
    named = set()          # 这一批里已经用掉的 id：显式写下的与自动生成的都算
    for i, item in enumerate(batch):
        row = f'row {i + 1}'

        # --- id ---
        uid = item.get('id')
        if not uid:
            if args.auto_id:
                base = _auto_id(item)
                uid = _free_id(base, named)
                if uid != base:
                    print(f'Auto-id disambiguated: {row} "{base}" -> "{uid}"')
            else:
                print(f'Error: {row} missing "id" field (use --auto-id to generate)',
                      file=sys.stderr)
                sys.exit(1)
        named.add(uid)

        # --- the record: keys, defaults and the content checks, all in one place ---
        records.append(build_issue_record(item, uid, now, where=f'{row} (id={uid}) '))

    # Phase 2: write all (validation passed)
    created = 0
    skipped = 0

    for record in records:
        uid = record['id']
        if uid in data['issues']:
            print(f'Warning: issue "{uid}" already exists, skipping', file=sys.stderr)
            skipped += 1
            continue

        if record['plan_path']:
            _ensure_plan_dir(record['plan_path'])

        data['issues'][uid] = record
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
    with _ledger_lock():
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
    with _ledger_lock():
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
    with _ledger_lock():
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
    with _ledger_lock():
        data = _read()
        issue = _get(args.id, data)
        issue['status'] = 'acknowledged'
        issue['reason'] = args.reason or issue.get('reason')
        issue['updated_at'] = _now()
        _write(data)
        print(f'Issue acknowledged: {args.id}')


def cmd_remove(args):
    """Delete an issue entirely."""
    with _ledger_lock():
        data = _read()
        _get(args.id, data)
        del data['issues'][args.id]
        _write(data)
        print(f'Issue removed: {args.id}')


# ── 池子对「哪些状态算在局里」的两句口径 ──────────────────────────────────────────
# 两句都在这里，各自一个名字，别在别处再写一遍状态字面量：两句管的不是一件事。
#: 还没结的条目：`weight` 与 `status` 数它。`resolving` 在里面 —— 已经有人在动它，但没结案，
#: 这一条仍然算数。
OWED = frozenset({'open', 'acknowledged', 'resolving'})
#: 还挑得动的：`top`、`by_module`、`stale` 只列这些。`resolving` 不在里面 —— 那一条已经有人在
#: 做，再挑一次就是两个人做同一件事。它仍算在 `OWED` 里，等它结案或退回 `open`。
PICKABLE = frozenset({'open', 'acknowledged'})


def cmd_status(args):
    """Show summary statistics across all issues.

    By default only counts the issues the pool still owes (`OWED`).
    Use --verbose to include resolved/rejected and total count.
    """
    data = _read()
    all_issues = list(data['issues'].values())

    if args.verbose:
        issues = all_issues
    else:
        issues = [i for i in all_issues if i.get('status') in OWED]

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
        if i.get('status') in PICKABLE
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

    只算还没结的条目（`OWED`，跟 `status` 一个口径），再排掉
    `decision == external`：那些的决定权不在 agent 手上（改架构、动模型、改体验），
    算进来就是一块谁也挪不动的底板，阈值永远回不到线下，维护节点会一轮一轮地插。
    """
    data = _read()
    actionable = [i for i in data['issues'].values()
                  if i.get('status') in OWED]
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
    """Show the top N issues that are still pickable."""
    data = _read()
    issues = [
        i for i in data['issues'].values()
        if i.get('status') in PICKABLE
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
    """Group pickable issues by module."""
    data = _read()
    modules = {}
    for issue in data['issues'].values():
        if issue.get('status') in PICKABLE:
            mod = issue.get('module', 'unknown')
            modules.setdefault(mod, []).append(issue)
    print(json.dumps(modules, indent=2, ensure_ascii=False))


def cmd_stale(args):
    """List stale pickable issues with high check_count."""
    data = _read()
    threshold = args.threshold if args.threshold is not None else 3
    stale = [
        i for i in data['issues'].values()
        if i.get('status') in PICKABLE
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
    with _ledger_lock():
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
