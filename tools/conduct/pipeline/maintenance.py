"""The maintenance channel: a crossed threshold inserts a node into the graph.

Maintenance is a PROJECT-level debt -- the test suite's wall clock, the issue pool's backlog. It
belongs to no line and has no edges, so the graph's own pick pushes it to the bottom forever. This
module is the one place that gives it an entrance.

**The judgement happens at the close.** `_close_own_node` asks this module, once, at the `--after`
settle: read each metric's number, compare it with the lines this line carries in its own policies,
and, if a line is crossed, hand back the action that puts a maintenance node between the node just
closed and the node the close picked. Where a number comes from is the metric's own `source`: the
test suite's wall clock travels in the worker's ticket (`reading.test_seconds`), while the issue
pool's weighted debt is read from the tool that owns the pool (`pf issue weight`).

Every number here is OPTIONAL. A project that configures none of them gets `None` from
`crossed()` and insertion never runs: the close's return value is byte-for-byte what it was before
this channel existed. That is the regression this feature is built around.

**One switch sits above the numbers, and it belongs to the project.** The debt is project-level, so
whether the project wants to be handed a node for it now is a project-level answer: turned off, a
crossed line inserts nothing (`issue_repair_on`, the key `pf config` owns). Default `on`, so a
project that never writes the key keeps the behaviour above byte for byte. This module owns what the
words mean; the layers, the default and the words `set` accepts are `pf config`'s.

**The graph edit is commands, and never a write from here.** `insertion()` builds them in the DAG
tool's own vocabulary (`undepend` / `add` / `depend` / `cycle hold`) and `writer.apply` runs
them: `plan()` stays a pure reader, and running the commands in one place is what keeps it that way. The
new node's uuid does not exist until `dag-manage add` has run, so the commands are written against
the `NEW_NODE` placeholder and the uuid is filled in afterwards -- `writer._capture_new_node` runs
that one command through the seam's `capture`, the same read-the-child's-stdout move
`nodes._rank_ready_nodes` already makes for the ranking.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import seam
from .nodes import node_spec_path
from .store import POLICY_NUMBERS, cycle_policy, load_config, load_dag


#: The action this module's insertion is handed over as. Registered in `conduct_pipeline.ACTIONS` and
#: handled by `writer.apply` / `writer.record` exactly like `dispatch` -- the difference is only
#: WHICH node is pinned (the one just inserted, not the one the close picked first).
MAINTENANCE_ACTION = 'maintenance_due'


def _reading_key(metric: dict) -> str:
    return metric['reading']


def _line_key(metric: dict) -> str:
    return metric['line']


#: The two metrics, and where each of their numbers lives.
#:
#: `reading` is the key the number appears under inside the ticket's `outputs` (`signal.reading_of`),
#: `line` is the numeric policy key the line itself carries (`cycle policy`, the same slot `when_shelved`
#: lives in). Both are optional, and neither is ever parsed out of anybody's prose.
#:
#: `source` names who produces the number: `ticket` for the wall clock only the session that ran the
#: verification can measure, `pool` for the pool's weighted debt, which the issue tool computes from
#: its own ledger (`pf issue weight`). A ticket never carries a `pool` number, and one that
#: does is ignored.
#:
#: `where` is the line the generated spec shows a reader: it says how this number was obtained, so a
#: maintenance node knows whether to go and measure or to go and look.
#:
#: `subject` / `criterion` / `remedy` are the mechanical completion rule the generated spec carries:
#: what is being pulled back under the line, what the next close must read, and what to run to see it.
#: `method` is the one step the tool can name without guessing at the project: which skill card owns
#: this kind of debt, so a node handed a number does not have to invent a method for it. The runbook
#: itself lives in that card; the spec only points at it (and the card declares which level runs it).
#: The list order is the severity order used to pick WHICH metric a node is inserted for when both
#: cross (the test suite's line first -- it is the one that blocks every other reading).
METRICS = (
    {
        'reading': 'test_seconds',
        'source': 'ticket',
        'unit': 's',
        'line': 'test_seconds_limit',
        'subject': 'the project test suite\'s full wall clock (seconds)',
        'where': 'the note\'s `outputs.reading.test_seconds` (measured by the session that ran the '
                 'full verification)',
        'criterion': 'the next closing note\'s `reading.test_seconds` must be **below**',
        'remedy': 'run the full verification command the project docs settle on and **measure** it '
                  '(never estimate), then report the measured seconds in the note',
        'method': 'pf-test-audit',
    },
    {
        'reading': 'issue_pool_weight',
        'source': 'pool',
        'unit': '',
        'line': 'issue_pool_weight_limit',
        'subject': 'the issue pool\'s weighted sum',
        'where': '`pf issue weight` (the issue tool computes it itself; it does not go into a note)',
        'criterion': 'the pool\'s weighted sum the tool computes at the next close must be **below**',
        'remedy': 'run `pf issue weight -v` to see how this number adds up, '
                  'and confirm it is back under the line',
        'method': 'pf-issue-repair',
    },
)

#: The ticket-sourced readings, in METRICS order: the ones a worker is asked for. `asked_readings`
#: narrows this to the lines a given cycle actually carries.
READING_KEYS = tuple(metric['reading'] for metric in METRICS if metric['source'] == 'ticket')

#: `dag-manage` prints its own uuid; nothing on this side can know it before the child has run, so the
#: maintenance node starts life under this placeholder name. The chain from here to the sweep, and it
#: is this long on purpose:
#:
#:   1. `insertion` writes the graph edit in terms of it (`depend` / `undepend` / `cycle hold`), and
#:      `actions._maintenance_due` builds the action around it -- `node_id`, the prompt file's path
#:      (`meta/launch/<cycle>/%DUID%/prompt.txt`) and the invocation line all name the placeholder.
#:   2. `writer.apply` writes that placeholder prompt, then runs `add` in the slot `ADD_MARKER` marks.
#:      `_capture_new_node` reads the uuid off its stdout and rebinds `node_id` / `prompt` to it.
#:   3. The action is a maintenance_due, which `apply` re-plans into the dispatch behind it: that
#:      recursion is what writes the REAL node's prompt file (`meta/launch/<cycle>/<uuid>/prompt.txt`).
#:   4. The placeholder's launch directory stays behind. `writer._prune_launch_dirs` sweeps the launch
#:      dirs of the nodes a cycle no longer holds, on the next successful spawn.
NEW_NODE = '%DUID%'
#: The slot marker `insertion` leaves where `add` belongs in the command list.
ADD_MARKER = '%ADD%'
_UUID_PRINTED = re.compile(r'Node created:\s*([0-9a-fA-F-]{8,})')


# ── the reading: what the worker reported ────────────────────────────────────

def _number(value) -> float | None:
    """One reading, or `None`.

    A number is a number and nothing else: a bool is not one (it is the JSON spelling of a decision,
    not a measurement), and a string is not parsed -- guessing a number out of a worker's prose is
    how a threshold ends up firing on the wrong evidence.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def reading_of(sig: dict | None) -> dict:
    """The ticket-sourced readings on one ticket, as numbers. An absent/garbled reading is simply not
    there.

    Only `READING_KEYS` are recognized (the `source: ticket` ones). An old note that still carries an
    `issue_pool_weight` is ignored here -- that number is computed by the issue tool out of the pool,
    and nobody can re-check the note's copy of it.
    """
    outputs = (sig or {}).get('outputs') or {}
    reading = outputs.get('reading') if isinstance(outputs, dict) else None
    if not isinstance(reading, dict):
        return {}
    out = {}
    for key in READING_KEYS:
        value = _number(reading.get(key))
        if value is not None:
            out[key] = value
    return out


# ── the lines: what this line carries ───────────────────────────────────────

def thresholds_for(cycle: dict | None) -> dict:
    """The lines in force for this line: its own policies.

    They are the line's own rules, in the same slot `when_shelved` lives in (`state-manage.py
    cycle policy`), which is why nobody has to write a sentence to configure one -- and why a line
    that has nothing to do with code simply does not carry them.

    "A numeric policy is configured" = a key really written on this line's record in the
    `state.cycles.POLICY_NUMBERS` table (integer keys like `parallel` are not among them); with none
    configured this reading is empty, and that is what `crossed`'s empty gate rests on -- non-empty
    means "this line configured at least one metric". The table itself has one home, the writing
    side's copy.
    """
    policy = cycle_policy(cycle or {})
    return {key: policy[key] for key in POLICY_NUMBERS if key in policy}


def asked_readings(cycle: dict | None) -> list:
    """Which readings this line asks for, in `METRICS` order.

    The list is narrowed by two things: only the `source: ticket` ones are named (only the session
    that ran the verification can measure them), and only those whose line this line really carries
    -- a slot nobody reads would be measured for nothing. `handoff.py --form` prints the note
    skeleton against this list.
    """
    thresholds = thresholds_for(cycle)
    return [metric['reading'] for metric in METRICS
            if metric['source'] == 'ticket' and metric['line'] in thresholds]


def pool_weight(project) -> float | None:
    """How heavy the pool is: ask the issue tool for that number (`pf issue weight`).

    If it cannot be read, `None` is handed back and this metric is not judged this pass -- a missing
    number reads as "not known this pass"; reading it as 0 would make a real debt look paid off.
    """
    try:
        out = seam.capture(seam.issue('weight'), project)
    except (OSError, RuntimeError):
        return None
    lines = [line.strip() for line in (out or '').splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return float(lines[-1])
    except ValueError:
        return None


# ── the judgement ────────────────────────────────────────────────────────────

#: This channel's project-level switch, living in the `pf config` slot. The pipeline side states its
#: own vocabulary here: the two packages are each loaded by path, and importing one into the other
#: would loop, so the key name and the vocabulary are written once on each side -- the writing side
#: rejects values outside the vocabulary, the reading side reads an unknown word as the default (the
#: same split as `store.cycle_policy`: write rejects, read falls back). One case compares the two.
ISSUE_REPAIR_KEY = 'issue_repair'
#: The one word that turns it off.
ISSUE_REPAIR_OFF = 'off'
#: This vocabulary, and the factory default.
ISSUE_REPAIR_VALUES = ('on', ISSUE_REPAIR_OFF)
ISSUE_REPAIR_DEFAULT = 'on'


def issue_repair_on(project) -> bool:
    """Whether this project now opens "insert a debt-repaying node when a line is crossed".

    The factory default is on: a project that never wrote this key closes exactly as it does today.
    The global layer still counts on this channel (the project layer overrides the global layer),
    read the same way as the effective value `pf config` prints.
    """
    value = load_config(project).get(ISSUE_REPAIR_KEY, ISSUE_REPAIR_DEFAULT)
    if value not in ISSUE_REPAIR_VALUES:
        value = ISSUE_REPAIR_DEFAULT
    return value != ISSUE_REPAIR_OFF


def _wired(metric: dict, reading: dict, thresholds: dict):
    """(value, line) for one metric, or `None` when either half is missing.

    The comparison is `>=`: the line is the last acceptable number, so reading exactly the line
    means the debt is at its limit and the channel opens. One line per measurement: the action on
    either side of it is the same one, so the line only has to say where the debt stands.
    """
    value = reading.get(_reading_key(metric))
    if value is None:
        return None
    line = thresholds.get(_line_key(metric))
    if line is not None and value >= line:
        return value, line
    return None


def crossed(project, cycle: dict | None, sig: dict | None, dag: dict | None = None):
    """The metric to insert a maintenance node for, or `None` (the common case).

    `None` covers every way this channel stays shut: no number for the metric, no line on this
    cycle, a number under its line, a cycle that is not about code -- the two metrics are
    code-path debt, so a `writing` / `art` line reads the same numbers and does nothing about them --
    a project that turned the channel off (`issue_repair`, `issue_repair_on`),
    and a metric that ALREADY has a maintenance node open on the graph:
    one metric, one live maintenance node at a time (`maintenance_nodes_of_metric`).

    The pool slot only asks the issue tool when "this line really carries that line", so the close
    path does not spawn a process for nothing. The switch is read at a similarly late point: a line
    the channel cannot reach at all (not the code branch, or no line configured) does not even read
    the setting.
    """
    if not _is_coding(cycle):
        return None
    thresholds = thresholds_for(cycle)
    if not thresholds:
        return None
    if not issue_repair_on(project):
        return None
    values = reading_of(sig)
    if any(metric['source'] == 'pool' and metric['line'] in thresholds for metric in METRICS):
        weight = pool_weight(project)
        if weight is not None:
            values['issue_pool_weight'] = weight
    if not values:
        return None
    for metric in METRICS:
        got = _wired(metric, values, thresholds)
        if got is not None:
            if maintenance_nodes_of_metric(project, metric['reading'], dag):
                continue
            value, line = got
            hit = {**metric, 'value': value, 'at': line}
            hit['title'] = maintenance_title(hit)
            # A CALLABLE, not the text: the node's uuid decides where its spec lands, and the uuid
            # does not exist until `dag-manage add` has run (`writer._capture_new_node`).
            hit['spec_of'] = lambda node_id, _hit=hit: maintenance_spec(
                _hit, node_spec_path(project, node_id))
            return hit
    return None


def maintenance_tag(reading_key: str) -> str:
    """The tag on this metric's maintenance node: `add` writes it, the dedup reads it -- the metric
    is recognized without relying on the words of the title."""
    return f'maint:{reading_key}'


def maintenance_nodes_of_metric(project, reading_key: str, dag: dict | None = None) -> list:
    """The maintenance nodes for this metric that are not settled yet (`todo` status), sorted by id.

    "Not settled yet" = the node is still in play (`todo`): `completed` means this pass of the debt
    is settled, and `shelved` means it is out of play waiting for a human -- neither should block the
    next insertion, or a human who never revives it would leave this channel mute forever.
    The judgement happens before the close commands land, so **the maintenance node being closed
    right now is still `todo` here and still blocks its own duplicate** -- one metric runs one
    maintenance node at a time, and this holds regardless of how far the close has got.

    `dag` is the one graph read at the close entry: this judgement reads it, and the close does not
    re-read the disk for this one question.
    """
    tag = maintenance_tag(reading_key)
    nodes = (dag if dag is not None else load_dag(project)).get('nodes') or {}
    out = []
    for uid, node in (nodes or {}).items():
        if not isinstance(node, dict):
            continue
        if node.get('status') != 'todo':
            continue
        if tag in [t for t in (node.get('tags') or []) if isinstance(t, str)]:
            out.append(uid)
    return sorted(out)


def _is_coding(cycle: dict | None) -> bool:
    """Only a cycle whose tags contain `coding` gets an insertion for these two metrics -- both are
    code-branch debt."""
    tags = [t for t in ((cycle or {}).get('tags') or []) if isinstance(t, str)]
    return 'coding' in tags


# ── the node's own text ──────────────────────────────────────────────────────

def maintenance_title(hit: dict) -> str:
    """The node's title: which measurement and which line, in one line a human reads in the DAG."""
    shown = f'{hit["value"]:g}' if hit['value'] != int(hit['value']) else f'{int(hit["value"])}'
    line = f'{hit["at"]:g}' if hit['at'] != int(hit['at']) else f'{int(hit["at"])}'
    return (f'Maintenance: {hit["subject"]} = {shown}{hit["unit"]}, '
            f'over the line {line}{hit["unit"]}')


def _closing_reading(hit: dict) -> str:
    """What the next judgement of this debt reads: the two sources are written differently, because
    they are read differently.

    The pool number is computed by the issue tool itself (`pf issue weight`), and writing it in the
    note would be read by nobody; the wall clock can only be measured by the session that ran the
    verification, and `handoff.py --form <cycle>` points that slot out.
    """
    if hit['source'] == 'pool':
        return ('- the issue tool computes this metric itself (`pf issue weight`); the note does '
                'not carry it:\n'
                f'  at the next close, a number the tool computes below {hit["at"]:g}{hit["unit"]} '
                f'counts as back under the line.\n')
    return ('- in the note\'s `-o`, carry `"reading": {"' + hit['reading'] + '": <measured number>}`'
            ' (the form points this slot out), so the next close can judge whether it is back '
            'under the line.\n')


def maintenance_spec(hit: dict, spec_path: str) -> str:
    """The generated spec: why this node exists, what its scope is, which skill owns it, and how it is
    judged done.

    Written by the tool, not by the session that will run it -- the whole point of the channel is
    that the judgement was mechanical, so the node cannot be handed the judgement as well. What it
    does carry is the one thing a worker cannot derive: which measurement was read, what it read,
    and where the line is.

    No function name, file path or line number from the project appears here: which files move is
    the node's own business, and a template that guessed would be a template that lied.
    """
    return (
        '# Maintenance node (generated by the tool)\n'
        'This node\'s spec is generated by conduct\'s maintenance channel and copied verbatim; it '
        'says **why this node was inserted**, **the scope of this one pass**, **how to do it**, and '
        '**the completion rule fixed from the start**.\n'
        '\n'
        '## Why this node was inserted\n'
        '\n'
        f'- metric: {hit["subject"]}\n'
        f'- where this number comes from: {hit["where"]}\n'
        f'- read at the close: **{hit["value"]:g}{hit["unit"]}**\n'
        f'- the line written in **this line\'s policies**: **{hit["at"]:g}{hit["unit"]}** '
        f'(`policies.{hit["line"]}`)\n'
        '- this channel is project-level: maintenance debt belongs to no line and has no edge in '
        'the graph, so the ranking always pushes it to the bottom; the close therefore inserts it '
        'against the line this line carries itself.\n'
        '\n'
        '## Scope\n'
        '\n'
        '- **this pass does one thing only**: pull this one metric back under the line. No adding '
        'features on the side, no refactoring on the side, no cleanup unrelated to this metric.\n'
        '- another metric crossing its line, or other work in the graph being more urgent, is '
        'outside this node\'s scope -- whoever crosses comes in for a pass of their own.\n'
        '- the size of the change follows the criterion: keep going while it fails, stop when it '
        'passes.\n'
        '\n'
        '## How to do it\n'
        '\n'
        f'- `method: {hit["method"]}` (how to run it: `~/.agents/skills/{hit["method"]}/SKILL.md`; '
        'that card declares who runs it, and this spec only points at it)\n'
        '\n'
        '## Completion rule (mechanically judgeable)\n'
        '\n'
        f'- {hit["criterion"]} {hit["at"]:g}{hit["unit"]}.\n'
        f'- how to see this number: {hit["remedy"]}.\n'
        '- if the criterion fails, do **not** report `completed`; report `failed` and write the '
        'number you read in `-s`, leaving this debt out in the open for a human.\n'
        '\n'
        '## Closing out\n'
        '\n'
        'Follow the "closing out" section of pf-conduct: get a freshly printed note form, then '
        'write the note -> close -> commit and push -> dispatch.\n'
        + _closing_reading(hit) +
        f'\n(this spec lands at: {spec_path})\n'
    )


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


# ── the graph edit, as commands ──────────────────────────────────────────────

def insertion(project, closed: str, target: str, cycle_id: str, hit: dict,
              spec_of, close_commands: list | None = None) -> dict:
    """The whole graph edit as one package: the command list, and the `add` at the head of it.

    The list, in order, and it is the order the pipeline and the graph tool both demand rather than
    the order the picture reads:

      1. `cycle close` / `dag-manage status` -- close the node and mark it in the DAG, BEFORE the new
         node is created: the dedup judgement reads the graph and an unfinished node must never be
         what stands between a threshold and its own maintenance node.
      2. `add` -- the maintenance node, with its title and its generated spec. Its stdout is the only
         place its uuid appears, so `writer.apply` runs it through `seam.capture` -- which is why the
         command is also handed back separately, as `add_command`: the caller runs it after the close
         commands, reads the uuid, and drops it from the list it runs next.
      3. `undepend <target> <closed>` -- drop the old direct edge. It frequently does not exist
         (being ready only asks that every dependency be completed or absent), and dropping an edge
         that is not there is a no-op. It has to come before the new edges: while it is still in
         place, `closed -> maintenance` plus `maintenance -> target` closes a cycle through the old
         edge, and `dag-manage` refuses a graph with a cycle in it.
      4. `depend` -- the two new edges: the maintenance node is the closed node's direct dependent,
         and the target waits for it. (`add --depends-on` is deliberately not used: those edges go in
         after the old one is gone, for the same reason.)
      5. `cycle hold` -- the cycle is pinned on the maintenance node. This is one write into
         the cycle's hand, and it has to name the maintenance node on the first try; the line's
         the other nodes it holds are untouched.
    """
    title = maintenance_title(hit)
    spec = spec_of(NEW_NODE)
    add = seam.dag('add', title, '--tags', maintenance_tag(hit['reading']), '--desc',
                   # The spec travels as a positional argument rather than as a shell string:
                   # `seam.dag` returns an argv LIST, so subprocess quotes it. Encoding it by hand
                   # would put a second quoting convention between the tool and the child, and the
                   # child is Python.
                   json.dumps(spec, ensure_ascii=False, separators=(',', ':')))
    return {
        'title': title, 'spec': spec, 'add_command': add,
        'commands': list(close_commands or []) + [
            [ADD_MARKER],                # where `add` belongs; `writer.apply` runs the real argv here
            seam.dag('undepend', target, closed),
            seam.dag('depend', NEW_NODE, '--on', closed),
            seam.dag('depend', target, '--on', NEW_NODE),
            seam.cmd('cycle', 'hold', cycle_id, NEW_NODE),
        ],
    }


def is_add_slot(argv) -> bool:
    """Is this the marker slot where `add` belongs? (`writer.apply` runs the real argv in its place.)"""
    return len(argv) == 1 and str(argv[0]) == ADD_MARKER


def _new_node_id(stdout: str) -> str:
    """The uuid `dag-manage add` printed, or a loud failure (the close is half-applied by then)."""
    found = _UUID_PRINTED.search(stdout or '')
    if not found:
        raise RuntimeError(f'dag-manage add did not report a node id; its output was: {stdout!r}')
    return found.group(1)


def substitute(argv, node_id: str) -> list:
    """The placeholder in an argv, replaced by the uuid the child actually minted."""
    return [str(part).replace(NEW_NODE, node_id) for part in argv]


def read_new_node_id(project, argv) -> str:
    """Run `add` and read the uuid it printed on its stdout.

    Captured rather than run through the seam's `run`: `run` discards stdout, and the uuid only
    exists in that stdout. This is the same move `nodes._rank_ready_nodes` makes to read
    `dag-manage list` -- the tool prints the fact, so the fact is read off the tool.
    """
    return _new_node_id(seam.capture(argv, project))
