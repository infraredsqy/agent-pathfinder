"""The writing half: `apply` runs what `plan` decided, `record` drives on to the next stable action.

Also `MAX_SETTLE_STEPS`, `_snapshot`, `spawn_session`, and `clean_launch_dir`.
"""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

from . import maintenance, seam
from .store import _holding_ids, find_cycle, load_signal, load_state, meta_dir, note_session
from .prompts import started_marker, write_prompt_file
from .planner import plan

MAX_SETTLE_STEPS = 8

#: How long a spawn claim may sit before it counts as stale: a spawn takes seconds (the spawner's
#: own ceiling is 300), and a claim left behind by a killed process must not wedge the retry
#: forever. A live claim is removed by `clean_launch_dir` together with the launch directory.
SPAWN_CLAIM_STALE_SECONDS = 30 * 60

#: The one place an argv for `dsh-spawn.ps1` is built. It sits next to the entry scripts rather than
#: inside this package so that the spawner stays reachable without loading the pipeline.
SPAWN_PY = Path(__file__).resolve().parent.parent / 'spawn' / 'conduct_spawn.py'


def _spawner():
    """`conduct_spawn.py`, loaded by path -- the way every other file in this tree is loaded."""
    spec = importlib.util.spec_from_file_location('conduct_spawn', SPAWN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the writer ───────────────────────────────────────────────────────────────

def _snapshot(project: Path, cycle_id: str) -> tuple:
    """Everything an action is allowed to be "progress" on.

    The cycle's hand (which nodes, collectively), where it points, and its status -- in a
    one-node-per-slot pipeline those are the only things a command may move.
    """
    state = load_state(project)
    cycle = find_cycle(state, cycle_id)
    if cycle is None:
        return (None, None, False, False)
    sigs = tuple((node, (load_signal(project, cycle_id, node) or {}).get('status'))
                 for node in _holding_ids(cycle))
    return (sigs, bool(cycle.get('path')), cycle.get('status'))


def _claim_pid(body: str) -> int | None:
    """The pid a claim body opens with (`<pid> started <iso>`); None when there is none to read.

    A body without a parseable leading int (an empty read, a hand-touched file) has no liveness to
    check, and the caller falls back to the staleness rule for it -- the pid check is a tightening
    of the takeover rule, never a replacement that needs the pid to exist.
    """
    try:
        pid = int(body.split()[0])
    except (IndexError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    """Is that process running right now? -- the one question a claim's pid can answer.

    A stale claim was so far judged only by its mtime, and a claim whose writer is STILL dispatching
    (the spawner's own ceiling is 300s) is not stale at any age: taking it over boots a second
    session onto the node. So the pid is asked first and the age rule only judges a dead writer. On
    Windows the ask is `OpenProcess` + `GetExitCodeProcess` (exit code STILL_ACTIVE = alive; a
    handle that will not open for anything but access-denied belongs to a process that exists); on
    POSIX it is `kill(pid, 0)`. A recycled pid can still refuse a dispatch by mistake -- that is a
    delete-the-claim-by-hand case, and the refusal says so.
    """
    if os.name == 'nt':
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_ACCESS_DENIED = 5
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        # 原型要显式：不设 restype 的句柄会被 ctypes 按 32 位 c_int 截断，64 位下传回去就坏了。
        kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel32.OpenProcess.restype = ctypes.c_void_p       # a handle, or NULL
        kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                     # exists, but it is not ours to signal
    except OSError:
        return False
    return True


def _claim_spawn(project: Path, cycle_id: str, node_id: str) -> tuple[bool, Path | None]:
    """Claim the right to start this node's session, exclusively.

    Two dispatches of the same node -- a retry racing the in-flight one, a rescue running while the
    first queue row is still alive -- must not both reach the spawner: two sessions would boot onto
    the same node. `O_CREAT | O_EXCL` is the one atomic primitive here; a losing caller gets the
    claim's path and a message. An EXISTING claim is judged by its writer before its age: a live pid
    refuses the dispatch however old the claim looks (a spawn can legitimately run for minutes, so
    mtime alone would take the claim over mid-flight), and only a dead writer falls through to the
    `SPAWN_CLAIM_STALE_SECONDS` takeover.

    Taking a dead claim over used to be stat → unlink → rebuild, and two takers in that window
    would delete each other's FRESH claim. So the unlink is followed by the create itself as the
    re-check: the replacement is built with `O_CREAT | O_EXCL` again, and if it loses -- someone
    rebuilt the claim between the unlink and the open -- the loop re-reads THAT claim's pid and
    mtime and judges it from the top. Three rounds, then give up with the refusal; a caller that
    really is alone will win one of them, and a caller that is not must not win any.
    """
    directory = meta_dir(project) / 'launch' / cycle_id / node_id
    directory.mkdir(parents=True, exist_ok=True)
    claim = directory / 'SPAWN.claim'
    for _ in range(3):
        try:
            fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(f'{os.getpid()} started {time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}')
            return True, claim
        except FileExistsError:
            try:
                body = claim.read_text(encoding='utf-8', errors='replace')
            except OSError:
                body = ''
            pid = _claim_pid(body)
            if pid is not None and _pid_alive(pid):
                return False, claim
            try:
                age = time.time() - claim.stat().st_mtime
            except OSError:
                age = 0.0
            if age <= SPAWN_CLAIM_STALE_SECONDS:
                return False, claim
            try:
                claim.unlink()
            except OSError:
                return False, claim
            # 复查与重建是同一步：下一圈直接用 O_CREAT|O_EXCL 再建，路径确实消失了才会成功；
            # 若已被另一名接管者抢先重建，open 当场 FileExistsError，那一圈会重读新 claim 的
            # pid 与 mtime、从头顶重走整段判定。三轮打满就带拒绝返回。
    return False, claim


def spawn_session(project: Path, cycle_id: str, prompt_file: str | None = None,
                  dry_run: bool = False, spawner: Path | None = None) -> str:
    """Open the next session for a node. `prompt_file` is what it will be told to do.

    This is the one place the pipeline starts a session, and `prompt_file` is REQUIRED in practice:
    the spawner on the other end refuses a call without one, because a session with nothing to load
    is not a thing the pipeline has. A `None` here is a caller bug and surfaces as the spawner's own
    loud failure, not as a silent second kind of session.

    The callers are the two spellings of one call: `state-manage.py dispatch <cycle>` (dispatch after
    the push) and `state-manage.py next --after <node_id> --spawn` (the one-shot close-and-dispatch);
    nobody hand-rolls the API calls.

    The call is guarded by a spawn claim (`_claim_spawn`), so a double dispatch -- cron rescue
    against an in-flight spawn -- is refused instead of booting two sessions onto one node. A
    successful spawn leaves the claim in place for `clean_launch_dir` to remove with the launch
    directory; a failed one releases it, because the prompt file is exactly what a human retries.
    Only a PIPELINE dispatch is claimed: the prompt must sit in the launch directory it belongs to
    (`meta/launch/<cycle>/<node_id>/prompt.txt`).

    The argv, the ceiling and the wording of a failure live in `conduct_spawn.py` -- this function is
    the pipeline's half of that seam (which spawner, and nothing else). `spawner` is how a caller
    answers "which one" outright; left out, the entry's `SPAWN_PS1` answers, and that default is the
    host's own (`conduct_pipeline.py`'s `_default_spawner`).
    """
    claim = None
    if not dry_run and prompt_file:
        prompt_path = Path(prompt_file)
        node_id = prompt_path.parent.name
        launch_dir = meta_dir(project) / 'launch' / cycle_id / node_id
        if prompt_path.parent == launch_dir:
            got, claim = _claim_spawn(project, cycle_id, node_id)
            if not got:
                raise RuntimeError(
                    f'this node already has a spawn claim ({claim}); the pid that wrote it may '
                    f'still be dispatching, and a live pid refuses every retry however old the '
                    f'claim looks (only a dead pid falls back to the '
                    f'{SPAWN_CLAIM_STALE_SECONDS // 60}-min staleness rule). A recycled pid can '
                    f'trip this by mistake: if you are sure nothing is running, delete the claim '
                    f'file and dispatch again')
    chosen = Path(spawner) if spawner else seam.spawn_ps1()
    try:
        line = _spawner().spawn_session(project, cycle_id, prompt_file,
                                        spawner=chosen, dry_run=dry_run)
    except (RuntimeError, ValueError, OSError):
        if claim is not None:
            try:
                claim.unlink()
            except OSError:
                pass
        raise
    if not dry_run:
        _note_run(project, chosen, prompt_file, line)
    return line


def _note_run(project: Path, spawner: Path, prompt_file: str | None, line: str) -> None:
    """Note the session this dispatch just opened, next to the node it will run.

    Best effort, for one reason worth stating: the session is ALREADY RUNNING by the time this
    returns, so a note that cannot be written must not turn a finished dispatch into a failure --
    what it costs is a conversation that is harder to open, not a node that never ran.

    The node id comes from the prompt file's own directory (`meta/launch/<cycle>/<node id>/prompt.txt`,
    the same reading the spawn claim makes), the session id and the host from the spawner's last line
    through `conduct_spawn.py` -- the only place either exists at all. A spawner that is not one of
    the two shipped scripts (a caller's own, a test's stand-in) states no host; the note then takes
    the id's own shape as the answer, which is what a reader would do with it anyway.
    """
    if not prompt_file:
        return
    session = _spawner().session_id_of(line)
    if not session:
        return
    try:
        note_session(project, Path(prompt_file).parent.name, 'run', session,
                     host=_spawner().host_of(spawner))
    except (OSError, ValueError):
        pass


def clean_launch_dir(project: Path, cycle_id: str, node_id: str) -> None:
    """Mark one node's launch dir as taken: its session is running.

    Called ONLY after a successful spawn. The prompt goes (the session has it), the claim goes (that
    dispatch is over), and a `started` stamp stays behind —— 一条线握着几格时，收尾会把「手里的
    每一格」重规划成同一份派发，而这枚记号就是「这一格的会话已经开过了」那件事的落点：没有它，
    同一个节点会被再交给 spawner 一次，同一个节点上开出两个会话。

    A failed spawn is the other case: the prompt file is what a human retries by hand, so it has to
    survive -- which is why this runs only on the success path. The directory itself stays as the
    stamp's home and is reaped by `_prune_launch_dirs` once the node leaves the line.
    """
    directory = meta_dir(project) / 'launch' / cycle_id / node_id
    try:
        for name in ('prompt.txt', 'SPAWN.claim'):
            f = directory / name
            if f.exists():
                f.unlink()
        marker = started_marker(project, cycle_id, node_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f'{os.getpid()} started {time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}\n',
            encoding='utf-8')
    except OSError:
        pass
    _prune_launch_dirs(project, cycle_id)


def _prune_launch_dirs(project: Path, cycle_id: str) -> None:
    """Drop the launch dirs of the nodes this cycle no longer holds.

    A successful spawn used to `rmtree` the directory; a node whose session is running needs its
    footprint kept (that is where the stamp lives), so the directory outlives the spawn and is reaped
    here instead. 一个节点离开手里就在收尾那一拍，而收尾后面紧跟的就是派发：扫一遍的时机正好。
    """
    import shutil
    try:
        cycle = find_cycle(load_state(project), cycle_id)
    except (RuntimeError, ValueError):
        return
    keep = set(_holding_ids(cycle)) if cycle else set()
    try:
        dirs = [d for d in (meta_dir(project) / 'launch' / cycle_id).iterdir()
                if d.is_dir()]
    except OSError:
        return
    for d in dirs:
        if d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)


def _capture_new_node(project: Path, add_command: list, spec_of, commands: list) -> tuple:
    """Run the one command that mints the maintenance node's id, then fill the id in everywhere.

    Returns `(node_id, rest_of_commands)`, the second already carrying the real uuid where the
    placeholder was and no longer holding `add` itself -- the command has run by then, and leaving
    it in the list would run it twice.

    `maintenance.insertion` marks where `add` belongs in the list (it is one of the pipeline's
    commands, in the pipeline's order) and hands it over separately for exactly this reason: its
    stdout is the only place the uuid ever appears, and `seam.run` throws stdout away -- so this one
    command goes through `seam.capture`, the same way `nodes._rank_ready_nodes` reads the ranking.
    Nothing written in terms of the new node can run before this has an answer.

    The node's `spec.md` is written right here, from the same step: what a session reads is that
    file, the session boots long after this call, and `plan()` is not allowed to touch a file.
    """
    node_id = maintenance.read_new_node_id(project, add_command)
    maintenance.write_spec(project, node_id, spec_of(node_id))
    return node_id, [maintenance.substitute(c, node_id) for c in commands
                     if not maintenance.is_add_slot(c)]


def apply(project, cycle_id: str | None = None, decision: str | None = None,
          node: str | None = None, note: str = '', self_verified: bool = False,
          after: str | None = None, action: dict | None = None,
          decision_given: bool = False) -> dict:
    """plan() + carry out the writing half, then enforce the contract invariant.

    The `commands` are RUN HERE and stripped from the returned action. A caller that reads
    `commands` and runs them itself is doing the v1 double-step; the field simply is not there to
    read.

    `action` is for a caller that ALREADY planned (record does): handing it in is what keeps the
    executed package the one that was planned -- re-planning here would build a second action for
    the same state, and anything the first plan carried outside `commands` (a multi-node
    dispatch's per-node prompt list, for one) would be planned twice differently.
    """
    project = Path(project).resolve()
    if action is None:
        action = plan(project, cycle_id, decision=decision, node=node, note=note,
                      self_verified=self_verified, after=after,
                      decision_given=decision_given)
    kind = action['action']
    commands = action.get('commands') or []
    # Built separately by the planner, run first and then dropped from the list (see
    # `_capture_new_node`); popped so it never reaches a caller as something to run.
    add_command = action.pop('add_command', None)
    spec_of = action.pop('spec_of', None)

    prompts = ([{'node_id': action['node_id'], 'prompt': action['prompt'],
                 'prompt_text': action['prompt_text']}] if action.get('prompt_text')
               else [n for n in (action.get('nodes') or []) if isinstance(n, dict)])
    if prompts:
        # Every dispatch carries one prompt per node in hand; writing them is not conditional on
        # there being commands to run (a multi-node dispatch has none -- the pins already ran).
        # 已经在跑的那一格跳过：prompt 是「等着开」的记号，补回来就等于说它还没开。
        for item in prompts:
            if not item.get('prompt_text'):
                continue
            if started_marker(project, action['cycle'], item['node_id']).is_file():
                continue
            write_prompt_file(project, action['cycle'], item['node_id'], item['prompt_text'])
    if not commands and add_command is None:
        action['applied'] = False
        return action

    if add_command is None and kind == maintenance.MAINTENANCE_ACTION:
        raise RuntimeError(f'action {kind!r} arrived without its `add` command: the maintenance '
                           f'node cannot be created')

    before = _snapshot(project, action['cycle'])
    # The mint runs where the planner put `add` -- the close commands ahead of it have to land
    # first (the dedup judgement reads the graph, and `add`'s uuid is what the rest of the list is
    # written in terms of), so `done` counts what has already run when it fires.
    #
    # An index loop, not `for cmd in commands`: the marker step REBINDS `commands` (placeholder
    # substituted), and a `for` would go on iterating the pre-substitution list it captured.
    ran: list = []
    done = 0
    i = 0
    while i < len(commands):
        cmd = commands[i]
        try:
            # The marker slot is where `add` belongs: run it there (its stdout carries the uuid the
            # rest of the list is written in terms of), read the uuid, and substitute it in.
            if add_command is not None and maintenance.is_add_slot(cmd):
                node_id, commands = _capture_new_node(project, add_command, spec_of, commands)
                ran.append(maintenance.substitute(list(add_command), node_id))
                action['node_id'] = node_id
                if action.get('prompt'):
                    # The prompt file's path names the node, and the node had no name until now. The
                    # text inside it does not: it is the same three blocks whatever the node turns
                    # out to be called, so it was written above without waiting.
                    action['prompt'] = str(meta_dir(project)
                                           / 'meta' / 'launch' / action['cycle']
                                           / node_id / 'prompt.txt')
                add_command = None
                done += 1
                continue                  # the rebind moved every later command: re-read this slot
            seam.run(cmd, project)
        except RuntimeError as e:
            if done:
                raise RuntimeError(f'{e}\n(commands {done}/{len(commands)} of action {kind!r} '
                                   f'already ran: the state is half-applied, look before '
                                   f'retrying)') from e
            raise
        done += 1
        i += 1
    if add_command is not None:
        raise RuntimeError(f'action {kind!r} carried an `add` command that never ran: the '
                           f'maintenance node cannot be created')
    after = _snapshot(project, action['cycle'])

    if before == after:
        raise RuntimeError(
            f'contract violation: action {kind!r} ran {len(commands)} command(s) but nothing '
            f'changed (before={before}, after={after})')
    action['applied'] = True
    action['did'] = [seam.as_text(c) for c in ran + commands]
    action['progress'] = {'before': {'node': before[0], 'signal': before[1]},
                          'after': {'node': after[0], 'signal': after[1]}}
    action.pop('commands', None)
    # An open_node is a dispatch in disguise: run the follow-up too, so callers have exactly one
    # shape to handle ("spawn a session with this prompt"). A maintenance_due is the same deal: the
    # node it inserted is now the cycle's own, and what the caller needs is its dispatch.
    if kind in ('open_node', maintenance.MAINTENANCE_ACTION):
        follow = apply(project, action['cycle'])
        follow['applied'] = True
        follow['opened_node'] = action.get('node_id')
        follow['did'] = action.get('did', []) + follow.get('did', [])
        # 开节点时捎带的那句话跟着一起走：调用方拿到的是重规划出来的那一条 dispatch，开节点动作
        # 本身到不了他手上，落在那儿的话就没人读得到（例如「点名要跑的节点不在过滤集里」）。
        # 维护节点那条路更甚：它插进来的理由只写在这个动作的 note 上，丢在这里就没人读得到了。
        spoken = (action.get('follow') or {}).get('note') or action.get('note')
        if spoken:
            follow['note'] = spoken
        return follow
    return action


def record(project, cycle_id: str | None = None) -> dict:
    """Drive the pipeline to its next *stable* action.

    This is the whole of the conductor's mechanical routine, and it is one call: consume a finished
    node's signal, do every piece of bookkeeping that follows from it, and stop at the first thing a
    caller can act on -- `dispatch` (spawn this node), `wait` (a node waiting for its own closing
    call), `ask_user`, `stop_pipeline`. A `maintenance_due` is the same shape as `open_node`: the
    tool's own intermediate step, which `apply` runs and re-plans into the dispatch behind it.

    Note the shape: it re-plans after every applied step rather than returning the action it just
    ran. Returning the close action would leave a closed-but-undispatched pipeline behind.
    """
    project = Path(project).resolve()
    action = {'action': 'wait', 'commands': []}
    for _ in range(MAX_SETTLE_STEPS):
        action = plan(project, cycle_id)
        kind = action['action']
        commands = action.get('commands') or []

        if kind in ('dispatch', 'open_node', maintenance.MAINTENANCE_ACTION):
            return apply(project, cycle_id, action=action)
        if kind == 'stop_pipeline':
            return apply(project, cycle_id, action=action) if commands else action
        if kind in ('wait', 'ask_user', 'no_cycle', 'fail_hard'):
            return action
        if commands:
            apply(project, cycle_id, action=action)
            continue
        return action
    return action
