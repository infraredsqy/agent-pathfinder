#!/usr/bin/env python3
"""Queue one node's session into ZCode's own cron scheduler, then wait for it to exist.

The seam this file fills
------------------------
`dsh-spawn.ps1` opens a DSH session over the DSH web API. This file opens a ZCode session by
writing one row into the desktop's task index (`~/.zcode/v2/tasks-index.sqlite`, table
`automations`). The desktop forks a `zcode-cron-scheduler` utility process that polls that table
every 20 seconds, and a row it claims becomes a session created by the GUI itself.

The indirection is the point. A session started from outside (`zcode.cjs -p`) never reaches the
GUI: the task list is fed by a per-workspace projection seeded once inside the agent-host process,
with no re-read path for a local workspace and no protocol method that asks for one. A session the
GUI creates lands in the list, can be watched and stopped there, and its `createTask` broadcasts
`workspace_task_list_changed`, so it shows up without a restart.

What the row has to say
-----------------------
One node is one session: `enabled=1`, `running=0`, `recurring=0`, `max_runs=1`.
`next_run_at` sits at (or a little before) now -- `claimDue` skips a row more than five minutes
overdue as a misfire, which is why this file waits for the session instead of trusting the row (see
below).
`mode` is `yolo`, full access: a node session that has to ask before it may edit a file stops at its
first edit, and nobody is sitting there to answer it.
`model_selection` decides which model runs, and it is named rather than left to the workspace: see
`DEFAULT_MODEL`. The other shape the column accepts is the JSON literal `null`, meaning "follow the
workspace's preferred model" -- whatever the GUI's picker last pointed at, which is not an answer a
caller can budget. The desktop reads the column strictly: a value it cannot parse is refused
outright rather than quietly defaulted to something else.
Every other column takes its neutral default.
`workspace_key` and `workspace_path` are the project root: that is what groups the session in the
GUI. `cron_expr` is display-only here (a non-recurring row is never rescheduled from it), so it
quotes the moment the row is meant to fire rather than a repeating minute.

Waiting, and what it buys
-------------------------
Writing the row proves nothing: the desktop claims it on its next poll, and if no window is open
there is no host to dispatch to, so the row lands in `failed_to_dispatch` and retires without a
session ever existing. So the default is to poll the same database for the `tasks` row whose
`cron_automation_id` is ours, and to exit non-zero if it does not appear inside
`--verify-seconds` (default 30, one poll interval plus room). That is what makes this spawner mean
the same thing `dsh-spawn.ps1` means: a session exists now. `--no-verify` writes the row and leaves.

Output and failure
------------------
On success: the automation id on the first line, and `session: <sess_id>` on the second when the
session was seen. On failure: a message on stderr and a non-zero exit.

Usage:
    python zcode-dispatch.py --prompt-file <path> --workspace <dir>
                             [--at <when>] [--verify-seconds <n>|--no-verify] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

#: How far in the past a row may sit and still be claimed. `claimDue` skips anything older, so a
#: caller that names a time past this window is refused here instead of being quietly dropped.
MISFIRE_GRACE_MS = 5 * 60 * 1000

#: A session younger than this blocks a second queue for the same node: the one real double-session
#: is a verify race (the row was claimed and the session exists, but the waiter gave up just before
#: it appeared) and that window is one poll interval wide. Anything older is fair game for a
#: documented retry (`cycle retry` + `cycle hold` + `next --spawn`).
SESSION_FRESH_MS = 10 * 60 * 1000

#: The desktop's data root; `ZCODE_DATA_BASE_DIR` moves the whole tree, the same knob the CLI's own
#: provider resolution reads -- and the one a test points at a sandbox to keep this file off a real
#: install.
DATA_BASE_DIR = Path(os.environ.get('ZCODE_DATA_BASE_DIR') or Path.home()) / '.zcode'
INDEX_PATH = DATA_BASE_DIR / 'v2' / 'tasks-index.sqlite'

#: One poll interval of the desktop scheduler is 20s; the default wait is that plus room to spare.
DEFAULT_VERIFY_SECONDS = 30.0

#: The permission mode the queued session boots in. `yolo` is full access, and it is not a
#: preference: a node session that has to ask before it may edit a file stops at the first edit and
#: waits for an answer nobody is there to give. A caller that wants the leash back names another
#: value from the desktop's own set (`build` / `edit` / `plan`) with `--mode`.
DEFAULT_MODE = 'yolo'

#: The two models a node session may run on -- this host's two answers to "whose quota does an
#: unattended, bulk-run node spend". Both are spelled in the desktop's own picker form,
#: `provider/model$level`:
#:
#:   glm      the built-in Zhipu coding-plan account this host is signed in to, `GLM-5.3-Flash`,
#:            which is what that account serves in the off-peak window.
#:   opencode `OpenCode Go (Chat)`'s `deepseek-v4.1-flash` (`opencode-go-chat`), which is what the
#:            GUI sessions on this host run.
#:
#: The `$level` is part of the value, not decoration: the desktop refuses an automation selection
#: that carries no reasoning level, three steps downstream ("Automation 模型选择不可用").
MODEL_CHOICES = {
    'glm': 'account:bigmodel-individual-coding-plan/GLM-5.3-Flash$high',
    'opencode': 'opencode-go-chat/deepseek-v4.1-flash$high',
}

#: The switch: which of `MODEL_CHOICES` a queued node runs on when the caller names none. Flip this
#: one word to move the whole pipeline between the two models. Named rather than left to the
#: workspace on purpose -- node sessions run unattended and in bulk, and "whatever the GUI's picker
#: last pointed at" is not something a caller can budget. `--model` / `DSH_CONDUCT_SPAWN_MODEL` still
#: win for a single call; `workspace` writes the JSON literal `null` instead, meaning "follow the
#: workspace's preferred model".
DEFAULT_MODEL = MODEL_CHOICES['glm']

#: The desktop's separator between a model and its reasoning level in a picker value
#: (`packages/shared/src/model-selection.ts`).
LEVEL_SEPARATOR = '$'

#: Every column `AutomationRepo.create` fills, written explicitly so a schema that grows a default
#: cannot silently change what this row means.
COLUMNS = (
    'automation_id', 'title', 'cron_expr', 'prompt', 'model', 'provider', 'model_selection',
    'workspace_key', 'workspace_path', 'workspace_identity', 'target_task_id',
    'bot_delivery_target', 'location_kind', 'recurring', 'max_runs', 'end_at', 'schedule_rule',
    'schedule_edited_by_user', 'run_count', 'scheduled_run_count', 'enabled', 'lifecycle_status',
    'next_run_at', 'last_run_at', 'running', 'claimed_at', 'dispatch_status', 'dispatch_attempts',
    'retry_at', 'last_error', 'mode', 'thought_level', 'created_at', 'updated_at',
)


def parse_at(when: str | None, now_ms: int) -> int:
    """`--at` as epoch milliseconds: `+30m` / `+2h` from now, or a local ISO-8601 timestamp.

    Local time on purpose: the desktop compares `next_run_at` against its own clock, and a human
    saying "tonight at 23:00" means the clock on the wall.
    """
    if not when:
        return now_ms
    text = when.strip()
    if text.startswith('+'):
        body = text[1:].strip().lower()
        unit = body[-1:] if body[-1:] in ('m', 'h') else 'm'
        number = body[:-1] if body[-1:] in ('m', 'h') else body
        try:
            amount = float(number)
        except ValueError:
            raise SystemExit(f'--at: cannot read {when!r} as +<minutes>m or +<hours>h')
        ms = int(amount * (3600_000 if unit == 'h' else 60_000))
        return now_ms + ms
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise SystemExit(f'--at: cannot read {when!r} as a local timestamp like 2026-10-07T23:00')
    return int(moment.timestamp() * 1000)


def cron_for(epoch_ms: int) -> str:
    """The 5-field cron line the desktop's automation list shows for this row."""
    moment = datetime.fromtimestamp(epoch_ms / 1000)
    return f'{moment.minute} {moment.hour} * * *'


def selection_json(model: str | None) -> str:
    """The `model_selection` column: a serialized `ModelSelection`, or the literal `null`.

    The desktop parses this column with a strict schema and refuses anything it cannot read
    (`readSerializedModelSelection`), and a selection that carries no reasoning level is refused a
    step later (`automationModelSelection.ts`), so exactly two shapes are worth writing:

        null                                                        follow the workspace's pick
        {"providerId":…,"modelId":…,"options":{"reasoningLevel":…}}  this model, this level

    The value a caller types is the desktop's own picker form -- `provider/model$level`, the same
    string the GUI's model picker shows (`formatModelPickerValue`) -- or `workspace`, or the JSON
    pasted verbatim.
    """
    text = (model or DEFAULT_MODEL).strip()
    if text.lower() in ('', 'workspace', 'null', 'default'):
        return 'null'
    if text.startswith('{'):
        try:
            payload = json.loads(text)
        except ValueError as e:
            raise SystemExit(f'--model: that is not JSON ({e})')
    else:
        provider, slash, rest = text.partition('/')
        if not slash or not provider.strip() or not rest.strip():
            raise SystemExit(f'--model: cannot read {model!r}; give `provider/model$level` '
                             f'(`$level` optional but usually required), or `workspace`')
        model_id, separator, level = rest.partition(LEVEL_SEPARATOR)
        payload = {'providerId': provider.strip(), 'modelId': model_id.strip()}
        if separator and level.strip():
            payload['options'] = {'reasoningLevel': level.strip()}
    # The same strictness the desktop applies on read: unknown keys are how a plausible-looking
    # value turns into "Automation 模型选择不可用" three steps later.
    allowed = {'providerId', 'modelId', 'options'}
    extra = set(payload) - allowed
    if extra:
        raise SystemExit(f'--model: unknown key(s) {sorted(extra)}; the desktop accepts only '
                         f'{sorted(allowed)}')
    for key in ('providerId', 'modelId'):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise SystemExit(f'--model: {key} must be a non-empty string')
    options = payload.get('options')
    if options is not None:
        if not isinstance(options, dict) or set(options) - {'reasoningLevel'}:
            raise SystemExit('--model: options accepts only {"reasoningLevel": "…"}')
        if not isinstance(options.get('reasoningLevel'), str) or not options['reasoningLevel'].strip():
            raise SystemExit('--model: options.reasoningLevel must be a non-empty string')
    return json.dumps(payload, ensure_ascii=False)


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(INDEX_PATH), timeout=15)
    con.execute('pragma busy_timeout=15000')
    return con


def queue(workspace: Path, prompt_file: Path, next_run_at: int, title: str,
          mode: str = DEFAULT_MODE, model_selection: str = 'null',
          allow_existing: bool = False) -> str:
    """Write the one row and hand back its `automation_id`.

    Idempotent at the node level: `(workspace, title)` names the dispatch (the default title IS the
    node id), and one of three states means this node is already being dispatched -- a row that is
    still unclaimed, a row claimed within the last `SESSION_FRESH_MS` whose session has not been
    written yet (the claim-to-task seam: claimed rows miss the pending check, and until the host
    writes its tasks row there is no session for the freshness check to see), or a session created
    within the last `SESSION_FRESH_MS`. Queueing again is how a node gets two live sessions, so it
    is refused unless `allow_existing`; a row the scheduler already retired
    (`failed_to_dispatch`) does not count as pending or in-flight, which is what makes the
    documented rescue re-queue legal.
    """
    if not INDEX_PATH.is_file():
        raise SystemExit(f'no ZCode task index at {INDEX_PATH}: this host has no ZCode desktop '
                         f'data, or the desktop never ran')
    prompt = prompt_file.read_text(encoding='utf-8')
    now = int(time.time() * 1000)
    automation_id = 'automation-' + str(uuid.uuid4())
    values = (
        automation_id, title, cron_for(next_run_at), prompt, None, None, model_selection,
        str(workspace), str(workspace), None, None,
        None, 'local', 0, 1, None, None,
        0, 0, 0, 1, 'active',
        next_run_at, None, 0, None, 'idle', 0,
        None, None, mode, None, now, now,
    )
    placeholders = ','.join('?' * len(COLUMNS))
    statement = f"insert into automations ({','.join(COLUMNS)}) values ({placeholders})"
    con = connect()
    try:
        # `begin immediate` takes the write lock up front, so the dedup read and the insert are one
        # atomic step: two concurrent queue() calls serialize, and the loser sees the winner's row.
        con.execute('begin immediate')
        pending = con.execute(
            'select automation_id from automations where workspace_key=? and title=? '
            "and enabled=1 and running=0 and lifecycle_status='active' and run_count=0 "
            "and claimed_at is null and dispatch_status<>'failed_to_dispatch' "
            'order by created_at desc limit 1', (str(workspace), title)).fetchone()
        if pending and not allow_existing:
            con.rollback()
            raise SystemExit(
                f'this node is already queued, not yet claimed: {pending[0]}. A second row would '
                f'boot two sessions onto one node; if that row is stale, delete it in the desktop '
                f'and queue again (or pass --allow-existing).')
        claimed = con.execute(
            'select a.automation_id from automations a '
            'where a.workspace_key=? and a.title=? '
            'and a.claimed_at is not null and a.claimed_at > ? '
            "and a.dispatch_status<>'failed_to_dispatch' "
            'and not exists (select 1 from tasks t where t.cron_automation_id = a.automation_id) '
            'order by a.claimed_at desc limit 1',
            (str(workspace), title, now - SESSION_FRESH_MS)).fetchone()
        if claimed and not allow_existing:
            con.rollback()
            raise SystemExit(
                f'a row for this node was just claimed: {claimed[0]} (claimed within the last '
                f'{SESSION_FRESH_MS // 60000} min, its session not written yet). A second row '
                f'would boot two sessions onto one node; wait for the session to appear, or pass '
                f'--allow-existing if that claim is dead.')
        fresh = con.execute(
            'select t.task_id from tasks t join automations a '
            'on t.cron_automation_id = a.automation_id '
            'where a.workspace_key=? and a.title=? and t.created_at > ? '
            'order by t.created_at desc limit 1',
            (str(workspace), title, now - SESSION_FRESH_MS)).fetchone()
        if fresh and not allow_existing:
            con.rollback()
            raise SystemExit(
                f'a session for this node already exists and is fresh: {fresh[0]} (created within '
                f'the last {SESSION_FRESH_MS // 60000} min). Queueing another would put two '
                f'sessions on one node; pass --allow-existing only when you are sure the first '
                f'session is dead.')
        con.execute(statement, values)
        con.commit()
    except sqlite3.OperationalError as e:
        raise SystemExit(f'could not queue the session in {INDEX_PATH}: {e}')
    finally:
        con.close()
    return automation_id


def session_for(automation_id: str) -> str | None:
    """The session the desktop created for this row, if it has got that far."""
    con = connect()
    try:
        row = con.execute('select task_id from tasks where cron_automation_id=? '
                          'order by created_at desc limit 1', (automation_id,)).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def automation_state(automation_id: str) -> dict:
    """What the row itself says: the only readable account when no session appears."""
    con = connect()
    con.row_factory = sqlite3.Row
    try:
        row = con.execute('select dispatch_status, dispatch_attempts, lifecycle_status, enabled, '
                          'running, retry_at, last_error from automations where automation_id=?',
                          (automation_id,)).fetchone()
        return dict(row) if row else {}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


def wait_for_session(automation_id: str, seconds: float) -> tuple[str | None, str]:
    """Poll for the session. Returns `(session_id_or_None, why)`.

    The wait is the desktop's own poll interval plus room, and there is no event to hang it on: the
    scheduler lives in another process and says nothing until it has written the row.
    """
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        session_id = session_for(automation_id)
        if session_id:
            return session_id, 'created'
        if time.monotonic() >= deadline:
            state = automation_state(automation_id)
            return None, (f'no session appeared within {seconds:g}s (row: '
                          f'dispatch_status={state.get("dispatch_status")!r}, '
                          f'attempts={state.get("dispatch_attempts")}, '
                          f'last_error={state.get("last_error")!r}); the desktop dispatches a '
                          f'claimed row only while a window is open')
        # One second, twice over: SQLite hands out no change notification, and the row is written
        # by an Electron utility process on the far side of a file. A bounded poll is the only
        # mechanism there is; the deadline above is what keeps it honest.
        time.sleep(1.0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Queue one node session into ZCode's cron scheduler (see the module docstring)")
    parser.add_argument('--prompt-file', required=True,
                        help='The node prompt, as the session will receive it')
    parser.add_argument('--workspace', default=None,
                        help='Project root (default: current directory); also the GUI grouping key')
    parser.add_argument('--title', default=None,
                        help="The automation list entry (default: the prompt file's parent dir)")
    parser.add_argument('--mode', default=DEFAULT_MODE, choices=('yolo', 'build', 'edit', 'plan'),
                        help=f'Permission mode of the queued session (default {DEFAULT_MODE}, full '
                             f'access: a node that has to ask before editing stops at its first '
                             f'edit)')
    parser.add_argument('--model', default=DEFAULT_MODEL,
                        help="Which model the session runs on: `provider/model$level` (the GUI "
                             "picker's own form), a ModelSelection object as JSON, or `workspace` "
                             f"to follow the workspace's preferred model (default: {DEFAULT_MODEL})")
    parser.add_argument('--at', default=None,
                        help='When it should fire: +30m, +2h, or a local timestamp (default: now)')
    parser.add_argument('--verify-seconds', type=float, default=DEFAULT_VERIFY_SECONDS,
                        help=f'Wait this long for the session to exist (default '
                             f'{DEFAULT_VERIFY_SECONDS:g}); 0 gives up on the first look')
    parser.add_argument('--no-verify', dest='verify', action='store_false',
                        help='Write the row and return without waiting for the session')
    parser.add_argument('--allow-existing', action='store_true',
                        help='Skip the pending-row / fresh-session dedup and queue anyway (the '
                             'documented rescue is legal without it: a failed_to_dispatch row '
                             'does not count as pending)')
    parser.add_argument('--json', action='store_true', help='Print a JSON record instead of lines')
    args = parser.parse_args()

    workspace = Path(args.workspace or Path.cwd()).expanduser().resolve()
    prompt_file = Path(args.prompt_file).expanduser().resolve()
    if not prompt_file.is_file():
        raise SystemExit(f'prompt file not found: {prompt_file} '
                         f'(a session with nothing to load is not a thing the pipeline has)')
    if not workspace.is_dir():
        raise SystemExit(f'workspace not found: {workspace}')

    now = int(time.time() * 1000)
    next_run_at = parse_at(args.at, now)
    if next_run_at < now - MISFIRE_GRACE_MS:
        raise SystemExit(f'--at {args.at!r} is more than five minutes in the past; the desktop '
                         f'would skip it as a misfire. Name a later time, or leave --at out.')
    title = args.title or prompt_file.parent.name or 'pf node'
    model_selection = selection_json(args.model)
    if model_selection != 'null' and 'reasoningLevel' not in model_selection:
        print('warning: --model carries no reasoning level, and the desktop refuses an automation '
              'selection without one ("Automation 模型选择不可用"). Add it: '
              '`--model provider/model$level`, e.g. deepseek/deepseek-flash$max.', file=sys.stderr)
    automation_id = queue(workspace, prompt_file, next_run_at, title, mode=args.mode,
                          model_selection=model_selection, allow_existing=args.allow_existing)

    session_id = None
    if args.verify:
        # A schedule for later cannot be verified now; only a row that is due can be waited on.
        if next_run_at <= now + 1000:
            session_id, why = wait_for_session(automation_id, args.verify_seconds)
            if not session_id:
                raise SystemExit(f'queued {automation_id} but {why}')
        else:
            why = 'scheduled, not waited for'
    else:
        why = 'not waited for'

    if args.json:
        print(json.dumps({'automationId': automation_id, 'sessionId': session_id,
                          'workspace': str(workspace), 'promptFile': str(prompt_file),
                          'nextRunAt': next_run_at, 'title': title, 'mode': args.mode,
                          'modelSelection': json.loads(model_selection), 'note': why},
                         ensure_ascii=False, indent=2))
    else:
        print(automation_id)
        if session_id:
            print(f'session: {session_id}')


if __name__ == '__main__':
    main()
