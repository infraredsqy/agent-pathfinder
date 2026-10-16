"""What a dispatch prompt says.

`prompt_for` / `write_prompt_file` / `build_prompt_text` assemble a dispatch prompt. A dispatch
prompt holds one line -- the invocation -- and the session reads the rest (the node's text, the
closing commands, the ticket form) from the node and the skill it invokes.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .store import meta_dir


# ── prompt layer ─────────────────────────────────────────────────────────────

def prompt_for(node_id: str | None = None) -> str:
    """The invocation line -- the FIRST non-empty line of prompt.txt (hard constraint).

    `/pf-conduct <node_id> [pipeline]`. `[pipeline]` is ALWAYS there: it is how pf-conduct knows it
    is running inside the pipeline (rather than having been invoked by hand), which is what makes it
    end with the one closing command instead of reporting to a human. The node's mode is not on this
    line: it lives in the node's own record, which the session reads with `dag-manage.py info`.

    The node is the work; naming it is the one thing this line does. Nothing else belongs here, and
    nothing may be prepended as a paragraph above this line. The node's weight, title and spec path
    live in `dag-manage.py info` / `open`, which the session runs itself.
    """
    if not node_id:
        raise ValueError('a dispatch prompt needs a node id: it is the line the session is told to run')
    return f'/pf-conduct {node_id} [pipeline]'


def started_marker(project: Path, cycle_id: str, node_id: str) -> Path:
    """The stamp a successful spawn leaves: this node's session is running.

    `.pf/meta/launch/<cycle>/<node>/started` —— prompt 被会话拿走之后剩下的那枚记号。派发与
    `--spawn` 见到它就不再把这一格交给 spawner（同一个节点上第二个会话收不回来），规划也不再
    把它算进派发名单。重开那一格是 `cycle retry --drop`：它从 conduct_state 侧按同一个路径把
    这枚记号抹掉。
    """
    return meta_dir(project) / 'launch' / cycle_id / node_id / 'started'


def write_prompt_file(project: Path, cycle_id: str, node_id: str, text: str) -> Path:
    """Write one dispatch prompt, atomically: tempfile in place, then `os.replace` over the target.

    The prompt file is read by whoever dispatches the node, and under parallel=2 the same node's
    prompt can be rewritten while a reader has the old one open -- an in-place `write_text` would
    let that reader see a half-written file. The replace is atomic, so a reader sees the old bytes
    or the new ones, never a mixture.
    """
    path = meta_dir(project) / 'launch' / cycle_id / node_id / 'prompt.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def build_prompt_text(project: Path, cycle: dict, node: dict) -> str:
    """One dispatch prompt: the invocation line, and nothing after it.

    Everything past this line the session reads for itself -- the node (title, weight, spec path,
    dependencies) with `dag-manage.py info` / `open`, and the closing commands and ticket form with
    the skill the line invokes. Standing instructions stay out for the same reason they always did:
    a session reads them itself with `pf instruction list`.

    `node` is the DAG row with its `id` handed in (the id is the storage key, so it is not a field on
    the row itself -- see `actions._dispatch`).
    """
    node_id = node.get('id')
    if not node_id:
        raise ValueError('build_prompt_text needs the node id; the row does not carry one')
    return prompt_for(node_id).rstrip('\n') + '\n'
