"""The core mechanism that puts one node into the graph: mint it, text it, wire it.

**An insertion is a graph edit and nothing else.** It creates the node, writes its `spec.md`, and
hangs it into the graph. No session is opened, no prompt file is written, and the line's hand is not
moved: which node runs next -- this one or another -- is decided one step later, at the dispatch, by
the same picking rules every other node goes through (`_pick_next` and the ranking it reads). Nothing
here holds a shared decision, so two sources never have to know about each other: each lands its own
node, and the ranking is what orders them.

**A source declares, this file does.** A source says what node it wants (title, tags, spec text) and
where it goes (`after` / `before`); this file is the one implementation of the three steps that turn
that into a node:

  1. **the uuid** -- `dag-manage add` mints it, and it is printed on that command's stdout and
     nowhere else, so `read_new_node_id` goes through `seam.capture` (`run` throws stdout away);
  2. **the spec** -- `write_spec` puts the generated text where `nodes.node_spec_path` says, because
     the session that will read it boots long after this edit (`plan()` must stay a pure reader);
  3. **the edges** -- `depend` / `undepend`, in the one order the graph tool accepts.

Second source: declare, and call `insert()` / `mint()`. There is nothing to re-implement.

**The uuid is not known when the package is built**, so the package is written against the
`NEW_NODE` placeholder and `mint` fills the real one in. The chain is this long on purpose:

  1. `insert` writes the graph edit in terms of `NEW_NODE`; `actions._insert_node` carries it as an
     action and `writer.apply` runs it.
  2. The `add` command is handed over separately, in the slot `ADD_MARKER` marks: `apply` runs it at
     that spot through `capture`, so the close commands ahead of it really land first (the dedup
     judgement reads the graph), and `mint` rebinds the rest of the list to the uuid it read back.
  3. Nothing else moves. The node waits in the graph for a pick, like any other node.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import seam
from .nodes import node_spec_path


#: The action kind every insertion source hands back. The caller reads the kind, and the source's own
#: words ride along beside it (`note`, and whatever fields the source chose to report).
INSERT_ACTION = 'insert_node'

#: The new node's uuid, before `dag-manage add` has run. See the module docstring for the chain.
NEW_NODE = '%DUID%'
#: The slot marker `insert` leaves where `add` belongs in the command list (`writer.apply` runs the
#: real argv in its place and reads the uuid off its stdout).
ADD_MARKER = '%ADD%'
_UUID_PRINTED = re.compile(r'Node created:\s*([0-9a-fA-F-]{8,})')


# ── the declaration: what goes in, where it goes ────────────────────────────

def insert(project, *, title: str, tags=(), spec_of, after: str | None = None,
           before: str | None = None, close_commands: list | None = None) -> dict:
    """The whole graph edit as one package: the node, its edges, and the commands that run them.

    `title` / `tags` / `spec_of` are what the SOURCE decides, and nothing here looks inside them:
    `spec_of(node_id)` is the text, called twice on purpose (once here for the `add` description,
    once more in `mint` with the real uuid) because the uuid does not exist until `add` has run.

    `after` / `before` is the place: the new node becomes `after`'s direct dependent, and `before`
    (when named) stops being `after`'s dependent and waits for the new node instead. `before`'s old
    direct edge is dropped first and only if it is there (`undepend` on an edge that is not present
    is a no-op), because while it is still in place `after -> new -> before` closes a cycle through
    it and the graph tool refuses a graph with a cycle in it. Dropping the edge is also what makes
    the insertion VOID the close's current pick: whatever `before` was going to be, it now waits.

    `close_commands` is the mechanical close this insertion rides on. It goes FIRST, and never the
    other way round: the judgement that decided to insert read the graph as it is on disk, so the
    node this close settles has to be marked before the new node is created.

    The `add` command is ALSO handed back on its own, as `add_command`: its stdout is the only place
    its uuid appears, so `writer.apply` runs it separately (`mint`). `commands` keeps the marker
    where it belongs, so the order the pipeline and the graph tool demand cannot drift.
    """
    tags = [str(t) for t in tags if str(t).strip()]
    spec = spec_of(NEW_NODE)
    add = ['add', *_add_argv(title, tags), *_description(spec)]
    commands = [*(close_commands or []), [ADD_MARKER]]
    if after and before:
        commands.append(seam.dag('undepend', before, after))
    if after:
        commands.append(seam.dag('depend', NEW_NODE, '--on', after))
    if after and before:
        commands.append(seam.dag('depend', before, '--on', NEW_NODE))
    return {
        'title': title, 'tags': tags, 'spec_path': node_spec_path(project, NEW_NODE),
        'add_command': seam.dag(*add), 'spec_of': spec_of, 'commands': commands,
    }


def _add_argv(title: str, tags) -> list:
    """`add`'s own arguments: the title, the tags when there are any, and nothing else."""
    argv = [title]
    if tags:
        argv += ['--tags', ','.join(tags)]
    return argv


def _description(spec: str) -> list:
    """The spec as `add --desc`.

    The spec travels as one argv element rather than as a shell string: `seam.dag` returns an argv
    LIST and subprocess quotes it. Encoding it by hand would put a second quoting convention between
    the tool and the child, and the child is Python.
    """
    return ['--desc', json.dumps(spec, ensure_ascii=False, separators=(',', ':'))]


# ── the mint: the uuid, the spec, the placeholders ──────────────────────────

def is_add_slot(argv) -> bool:
    """Is this the marker slot where `add` belongs? (`writer.apply` runs the real argv in its place.)"""
    return len(argv) == 1 and str(argv[0]) == ADD_MARKER


def fill(part, node_id: str) -> str:
    """The placeholder in one string, replaced by the uuid the child actually minted."""
    return str(part).replace(NEW_NODE, node_id)


def fill_all(parts, node_id: str) -> list:
    """`fill` over a list of strings: an argv, or the steps an action shows a reader."""
    return [fill(part, node_id) for part in parts]


def _new_node_id(stdout: str) -> str:
    """The uuid `dag-manage add` printed, or a loud failure (the close is half-applied by then)."""
    found = _UUID_PRINTED.search(stdout or '')
    if not found:
        raise RuntimeError(f'dag-manage add did not report a node id; its output was: {stdout!r}')
    return found.group(1)


def read_new_node_id(project, argv) -> str:
    """Run `add` and read the uuid it printed on its stdout.

    Captured rather than run through the seam's `run`: `run` discards stdout, and the uuid only
    exists in that stdout. This is the same move `nodes._rank_ready_nodes` makes to read
    `dag-manage list` -- the tool prints the fact, so the fact is read off the tool.
    """
    return _new_node_id(seam.capture(argv, project))


def write_spec(project, node_id: str, spec: str) -> Path:
    """Put the generated spec in the new node's ledger directory.

    `dag-manage add` carries the text (so the graph row's description says what the node is for) but
    creates only the empty directory: what a node's session reads is `spec.md`, and by the time the
    session boots the graph edit is long over. Writing it here -- from the same `add` step, with the
    uuid the child just minted -- is what keeps `plan()` a pure reader: the planner builds the text,
    `apply` is the only thing that touches a file.

    `node_id` comes straight from the child process's own output, so it is checked before it reaches
    a path: a uuid from anywhere is still a uuid.
    """
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', node_id or ''):
        raise RuntimeError(f'refusing to write a spec under a node id that is not a uuid: '
                           f'{node_id!r}')
    path = Path(node_spec_path(project, node_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(spec, encoding='utf-8')
    return path


def mint(project, add_command: list, spec_of, commands: list) -> tuple:
    """Run the one command that mints the node's uuid, then fill the uuid in everywhere.

    Returns `(node_id, rest_of_commands)`, the second already carrying the real uuid where the
    placeholder was and no longer holding `add` itself -- the command has run by then, and leaving
    it in the list would run it twice. The node's `spec.md` is written here too, from the same step:
    what a session reads is that file, the session boots long after this call, and `plan()` is not
    allowed to touch a file.
    """
    node_id = read_new_node_id(project, add_command)
    write_spec(project, node_id, spec_of(node_id))
    return node_id, [fill_all(c, node_id) for c in commands if not is_add_slot(c)]
