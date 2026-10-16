"""The `session` group: which conversation produced a node, and which ones ran it.

A session id is machine-local -- a conversation lives on the machine that opened it -- so the ids do
not ride on the record: an id in a shared JSON is a value that is guaranteed dead on every other
machine, and one that cannot even be told apart from "that session was deleted here". They live in
`.pf/meta/sessions/<node id>.json`, which `.pf/.gitignore` keeps out of both histories. The shape,
and why it is two named lists rather than one list with a role field, are in
`tools/shared/session-note.py`.

    show <node> [--json]    one node's note: the discussion it came from, the sessions that ran it
    list [--json]           every note this machine holds, keyed by node id
    import [--dry-run]      fold the older `dsh_session` field, and any `> discussion:` header in the
                            node's spec, into notes -- once per machine

`import` writes notes and nothing else. `ideas.json` is deliberately NOT touched: the field it reads
is a value no other machine can use, and the note is what a reader should use from now on. Running it
twice is a no-op, because a note ignores an id it already holds.
"""

import json
import re
import sys
from pathlib import Path

from .pipeline_loader import _load_pipeline
from .store import _read_json, ledger_dir_for, require_initialized

#: The header an older routing session left in a node's `spec.md`:
#: `> discussion: dsh-session:session-…[, dsh-session:…]`. Read only in the first lines of the file,
#: and only on a line that is exactly this header -- the convention lived as prose, so anything
#: looser would pick up whatever a spec happens to say about a session.
SPEC_HEAD_LINES = 6
DISCUSSION_HEADER = re.compile(r'^\s*>\s*discussion:\s*(.+)$')
DISCUSSION_ID = re.compile(r'dsh-session:([^\s,]+)')
#: What an imported value is marked with: it came off the record, so it may name a session on the
#: machine that wrote the record rather than on this one.
LEDGER_SOURCE = 'ledger'


def _root(project) -> Path:
    """The project root: the `.pf/` every command here is resolved through is `<root>/.pf`."""
    return ledger_dir_for(project).parent


def _graph(root: Path) -> dict:
    """The graph as it is on disk: the node table, or an empty one.

    Read here rather than through the pipeline's door because these commands answer about a NOTE --
    a node whose spec, title or record is missing still has a conversation worth naming.
    """
    dag = _read_json(root / '.pf' / 'ideas.json', 'ideas.json') or {}
    return dag.get('nodes') or {}


def _title(root: Path, node_id: str) -> str:
    """The node's title, or '' -- a note whose node is gone is still worth printing."""
    node = _graph(root).get(node_id) or {}
    return node.get('title') or ''


def _resolve_node(root: Path, node_id: str) -> str:
    """The node the caller means: the exact id, or the one node a prefix names.

    Ambiguity is refused rather than guessed at: a note is a pointer into a real conversation, and
    opening the wrong one is worse than being told to type more characters.
    """
    nodes = _graph(root)
    if node_id in nodes:
        return node_id
    matches = sorted(uid for uid in nodes if uid.startswith(node_id))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        print(f'Error: no node starts with `{node_id}`', file=sys.stderr)
    else:
        print(f'Error: `{node_id}` names {len(matches)} nodes: {", ".join(m[:12] for m in matches)}',
              file=sys.stderr)
    sys.exit(1)


def _lines_of(note: dict, role: str) -> list:
    """One role of a note as printable lines: `<role>  <session>  <host>  <at>  (source)`."""
    out = []
    for entry in note.get(role) or []:
        source = f'  ({entry["source"]})' if entry.get('source') else ''
        out.append(f'  {role:<10} {entry["session"]}  {entry.get("host") or "?"}  '
                   f'{entry.get("at") or "time unknown"}{source}')
    return out


def cmd_session_show(node_id: str, project=None, as_json: bool = False) -> None:
    """One node's note. Empty is an answer, not an error: it says this machine never noted it."""
    root = _root(project)
    uid = _resolve_node(root, node_id)
    note = _load_pipeline().session_note(root, uid)
    if as_json:
        print(json.dumps({'node': uid, **note}, ensure_ascii=False, indent=2))
        return
    title = _title(root, uid)
    print(f'{uid}  {title}'.rstrip())
    lines = _lines_of(note, 'discussion') + _lines_of(note, 'run')
    if not lines:
        print('  no session note: this machine did not record a discussion or a run for it')
        return
    print('\n'.join(lines))


def cmd_session_list(project=None, as_json: bool = False) -> None:
    """Every note this machine holds, keyed by node id -- what a panel reads in one call."""
    root = _root(project)
    notes = _load_pipeline().session_notes(root)
    if as_json:
        print(json.dumps(notes, ensure_ascii=False, indent=2))
        return
    if not notes:
        print(f'no session notes under {root / ".pf" / "meta" / "sessions"}')
        return
    for uid in sorted(notes):
        note = notes[uid]
        stamps = [e.get('at') for role in ('discussion', 'run') for e in note[role] if e.get('at')]
        latest = max(stamps) if stamps else 'time unknown'
        print(f'{uid}  discussion {len(note["discussion"])}  run {len(note["run"])}  latest {latest}  '
              f'{_title(root, uid)}'.rstrip())


def _spec_discussion_ids(root: Path, node_id: str) -> list:
    """The discussion session ids an older spec header names, in file order (usually none)."""
    spec = root / '.pf' / 'nodes' / node_id / 'spec.md'
    try:
        head = spec.read_text(encoding='utf-8', errors='replace').splitlines()[:SPEC_HEAD_LINES]
    except OSError:
        return []
    ids = []
    for line in head:
        header = DISCUSSION_HEADER.match(line)
        if not header:
            continue
        for found in DISCUSSION_ID.findall(header.group(1)):
            if found not in ids:
                ids.append(found)
    return ids


def cmd_session_import(project=None, dry_run: bool = False) -> None:
    """Fold the older record's session ids into notes, once per machine.

    Two sources, both older than the note and both left where they are: the `dsh_session` field
    `ideas.json` carries (the session the node was added in -- 出生 session), and a
    `> discussion:` header an older spec may still open with. Nothing here writes the record.
    """
    require_initialized(project)
    root = _root(project)
    pipeline = _load_pipeline()
    nodes = _graph(root)
    planned, already, from_spec = [], 0, 0
    for uid in sorted(nodes):
        node = nodes[uid]
        legacy = node.get('dsh_session')
        ids = [legacy] if isinstance(legacy, str) and legacy else []
        for found in _spec_discussion_ids(root, uid):
            if found not in ids:
                ids.append(found)
                from_spec += 1
        if not ids:
            continue
        note = pipeline.session_note(root, uid)
        for found in ids:
            if found in [e['session'] for e in note['discussion']]:
                already += 1
                continue
            planned.append((uid, found))
    if dry_run:
        for uid, found in planned:
            print(f'would note  {uid}  discussion  {found}')
        print(f'dry run: {len(planned)} session id(s) from {len(nodes)} nodes would be noted; '
              f'{already} already noted')
        return
    added = 0
    for uid, found in planned:
        if pipeline.note_session(root, uid, 'discussion', found, at=None, source=LEDGER_SOURCE):
            added += 1
    print(f'noted {added} session id(s) as discussion; {already} were already noted; '
          f'{from_spec} came from a spec header; the record itself was not touched')


def _dispatch_session(args) -> None:
    """`pf session <verb>`: the group's own reading of its sub-command."""
    as_json = getattr(args, 'json', False)
    project = getattr(args, 'project', None)
    verb = getattr(args, 'session_command', None)
    if verb == 'show':
        cmd_session_show(args.node, project, as_json)
    elif verb == 'list':
        cmd_session_list(project, as_json)
    elif verb == 'import':
        cmd_session_import(project, getattr(args, 'dry_run', False))
    else:
        print('Error: `pf session` wants one of: show | list | import', file=sys.stderr)
        sys.exit(1)