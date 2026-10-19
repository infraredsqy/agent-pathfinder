"""What a dispatch prompt says.

`write_prompt_file` / `build_prompt_text` assemble a dispatch prompt. A dispatch prompt holds one
line -- the invocation -- and the session reads the rest (the node's text, the closing commands, the
ticket form) from the node and the skill it invokes.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .store import _jsonio, meta_dir


# ── prompt layer ─────────────────────────────────────────────────────────────

def launch_dir(project: Path, cycle_id: str) -> Path:
    """`.pf/meta/launch/<cycle>/`: the dispatch plumbing of one line, one directory per node.

    A node's prompt file, its spawn claim and its `started` stamp all hang off here, so this is the
    one place that path is built; moving the plumbing moves all three with it.
    """
    return meta_dir(project) / 'launch' / cycle_id


def launch_prompt_path(project: Path, cycle_id: str, node_id: str) -> Path:
    """This node's `prompt.txt` -- what a dispatch hands its session, and what a caller reports."""
    return launch_dir(project, cycle_id) / node_id / 'prompt.txt'


def started_marker(project: Path, cycle_id: str, node_id: str) -> Path:
    """The stamp a successful spawn leaves: this node's session is running.

    `.pf/meta/launch/<cycle>/<node>/started` -- the stamp left behind once the prompt has been taken
    by the session. Dispatch and `--spawn` stop handing this slot to the spawner when they see it (a
    second session on the same node cannot be collected), and planning no longer counts it in the
    dispatch list. Reopening that slot is `cycle retry --drop`: it wipes this stamp from the state
    side along the same path.
    """
    return launch_dir(project, cycle_id) / node_id / 'started'


def write_prompt_file(project: Path, cycle_id: str, node_id: str, text: str) -> Path:
    """Write one dispatch prompt, atomically: a temp file in place, then the tree's retried replace.

    The prompt file is read by whoever dispatches the node, and under parallel=2 the same node's
    prompt can be rewritten while a reader has the old one open -- an in-place `write_text` would
    let that reader see a half-written file. The replace, and its tolerance for a reader holding the
    target open on Windows, is `tools/shared/jsonio.py`'s: this writes text, which is why the JSON
    writer next to it does not fit, and the replace under it is shared.
    """
    path = launch_prompt_path(project, cycle_id, node_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _jsonio().replace_with_retry(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def build_prompt_text(node_id: str) -> str:
    """One dispatch prompt: the invocation line, and nothing after it.

    `/pf-conduct <node_id> [pipeline]` -- the FIRST non-empty line of prompt.txt (a hard
    constraint). `[pipeline]`
    ALWAYS there: it is how pf-conduct knows it is running inside the pipeline (rather than having been
    invoked by hand), which is what makes it end with the one closing command instead of reporting to a
    human. The node's mode is not on this line: it lives in the node's own record, which the session
    reads with `dag-manage.py info`.

    The node is the work; naming it is the one thing this text does, and the id is all it asks for.
    Nothing else belongs here, and nothing may be prepended as a paragraph above this line: the node's
    weight, title and spec path live in `dag-manage.py info` / `open`, the closing commands and the
    ticket form live in the skill the line invokes, and standing instructions stay out because a
    session reads them itself with `pf instruction list`.
    """
    if not node_id:
        raise ValueError('a dispatch prompt needs a node id: '
                         'it is the line the session is told to run')
    return f'/pf-conduct {node_id} [pipeline]\n'
