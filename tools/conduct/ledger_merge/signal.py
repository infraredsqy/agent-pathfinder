"""A node's ticket under `.pf/signals/<cycle>/<node>.json` -- the file IS one record.

A ticket is ONE report: recency decides it, field by field, with a terminal status privileged.

  * `status`: a terminal word (`completed` / `failed` / `stopped`) beats a non-terminal one
    (`pending` is the mark a retry leaves), whichever file is newer
  * any other field both sides changed: the newer `%A`/`%B` file wins (a tie, or a file that is not
    there, goes to ours) -- two reports of one node are two attempts at one moment, and the later
    attempt is the one that happened
  * two equal tickets are equal: fields only one side touched are not a question at all
"""

from pathlib import Path

from .generic import _UNDECIDED, _as_dict, _both_changed, _merge_mapping, _three_way

#: 终态：这一格的活有结论了 —— `completed` 交付了，`failed` / `stopped` 没交付。
#: `pending` 是还没结论的记号（重试留下的），永远进不了这张表。
TERMINAL_SIGNAL_STATUSES = ('completed', 'failed', 'stopped')


def merge_signal(base, ours, theirs, rep, ours_path=None, theirs_path=None) -> dict:
    """One ticket: recency decides it, with a terminal status privileged."""
    def resolve(key, b, o, t):
        if key == 'status':
            return _signal_status(b, o, t, rep, ours_path, theirs_path)
        if _both_changed(b, o, t):
            return _by_mtime(o, t, ours_path, theirs_path, rep, key)
        return _three_way(b, o, t)
    return _merge_mapping(_as_dict(base), _as_dict(ours), _as_dict(theirs), rep, '条子', resolve)


def _signal_status(base_value, ours_value, theirs_value, rep, ours_path, theirs_path):
    got = _three_way(base_value, ours_value, theirs_value)
    if got is not _UNDECIDED:
        return got
    o_terminal = ours_value in TERMINAL_SIGNAL_STATUSES
    t_terminal = theirs_value in TERMINAL_SIGNAL_STATUSES
    if o_terminal != t_terminal:
        winner = ours_value if o_terminal else theirs_value
        rep.decide(f'条子: status 终态压过非终态 → {winner!r}'
                   f'（ours={ours_value!r} theirs={theirs_value!r}）')
        return winner
    return _by_mtime(ours_value, theirs_value, ours_path, theirs_path, rep, 'status 同档')


def _by_mtime(ours_value, theirs_value, ours_path, theirs_path, rep, what):
    winner, side = _newer_side(ours_value, theirs_value, ours_path, theirs_path)
    rep.decide(f'条子: {what} 两侧都改 → 取 mtime 较新的一侧（{side}；'
               f'{_mtime_note(ours_path, theirs_path)}）')
    return winner


def _newer_side(ours, theirs, ours_path, theirs_path):
    """The side whose %A/%B file is newer; a tie (or a missing file) goes to ours."""
    try:
        ours_mtime = Path(ours_path).stat().st_mtime
        theirs_mtime = Path(theirs_path).stat().st_mtime
    except (OSError, TypeError):
        return ours, 'ours'
    if theirs_mtime > ours_mtime:
        return theirs, 'theirs'
    return ours, 'ours'


def _mtime_note(ours_path, theirs_path) -> str:
    try:
        return (f'%A mtime={Path(ours_path).stat().st_mtime:.3f} / '
                f'%B mtime={Path(theirs_path).stat().st_mtime:.3f}')
    except (OSError, TypeError):
        return '有一侧没有文件'