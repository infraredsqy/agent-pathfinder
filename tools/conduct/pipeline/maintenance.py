"""The maintenance channel: a crossed threshold inserts a node into the graph.

Maintenance is a PROJECT-level debt -- the test suite's wall clock, the issue pool's backlog. It
belongs to no line and has no edges, so the graph's own pick pushes it to the bottom forever. This
module is the one place that gives it an entrance.

**The judgement happens at the close.** `_close_own_node` asks this module, once, at the `--after`
settle: read each metric's number, compare it with the lines this line carries in its own policies,
and, if a line is crossed, hand back the insertion that puts a maintenance node between the node just
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

**The graph edit is commands, and never a write from here.** What this channel inserts is declared
in `insert_into` (its title, its tags, its generated spec, and the place between the node just closed
and the node the close had picked) and built by `insertion.insert`, the core mechanism every
insertion source shares; `writer.apply` is the one thing that runs it. `plan()` stays a pure reader,
which is what running the commands in one place buys. **An insertion is a graph edit only**: no
session is opened and the line's hand is not moved (`insertion.py`) -- the pick, one step later,
decides whether this node is the next one to run.
"""

from __future__ import annotations

from . import insertion, seam
from .nodes import node_spec_path
from .store import POLICY_NUMBERS, cycle_policy, load_config, load_dag


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
            # does not exist until `dag-manage add` has run (`insertion.mint`).
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


# ── the declaration: this channel's insertion, as one call ──────────────────

def insert_into(project, cycle: dict | None, hit: dict, closed: str, target: str,
                close_commands: list | None = None) -> dict:
    """This channel's insertion, declared in one place: what to insert, and where it goes.

    Everything here is this channel's own answer -- the title, the metric's tag (the dedup reads it
    back), the line's tags, the generated spec, and the place: between the node just closed and the
    node the close had already picked. The three steps that turn a declaration into a node (mint the
    uuid, write the spec, wire the edges) are `insertion.py`'s, and every insertion source gets them
    from there -- a source never re-implements them.

    The line's own tags ride on the node because the pick is a SEPARATE step now: the inserted node
    is chosen by the usual ranking under some line's filter set, and a node carrying only the metric
    tag would be invisible to the very line that asked for it. Which lines may take it is not decided
    here: the tags say exactly what the line that crossed the threshold answers to.
    """
    return insertion.insert(
        project,
        title=maintenance_title(hit),
        tags=[maintenance_tag(hit['reading']), *_line_tags(cycle)],
        spec_of=hit['spec_of'],
        after=closed, before=target,
        close_commands=close_commands,
    )


def _line_tags(cycle: dict | None) -> list:
    """The filter set of the line that asked for this insertion, read as it is written."""
    return [t for t in ((cycle or {}).get('tags') or []) if isinstance(t, str)]
