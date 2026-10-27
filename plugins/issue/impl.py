"""The issue plugin's own implementation: the judgement, and the node's own text.

This file is loaded **by path**, on its own, from the folder it sits in (`plugin.json` beside it is
this plugin's declaration). It therefore imports nothing from the pipeline package: everything it
may touch arrives in the `ctx` it is handed (`plugin_host.Context`) --

    ctx.knob(name)            the effective value of one knob, from the whole resolution chain
    ctx.reading(key)          the number behind one of this declaration's readings; the source it
                              draws on is the one the declaration claims, computed by the function
                              that claim names (`pool_weight` below) -- so this is the door the
                              pool's own number comes through
    ctx.nodes_with_tag(tag)   the ids of the still-open nodes carrying one tag
    ctx.capture(argv)         run one command, hand back its stdout
    ctx.spec_path(node_id)    where a node's own `spec.md` lands (this tree's layout, not ours)
    ctx.project / ctx.cycle / ctx.node_id / ctx.sig / ctx.dag / ctx.target

**This copy is this plugin's alone.** Reading the declaration, pairing it with the prose below,
comparing the number against the line, assembling the offer and writing the node's title and spec
all live here, and the sibling plugin that watches the test suite carries its own copy of the same
mechanism: nothing of this is shared between plugins or lifted into the core. The core merges the
names and looks for clashes, and it is the core's `insertion.py` that turns the offer into a node.

`offer(ctx)` is the whole of its contract with the core: it answers the insertion this plugin wants
now (a title, tags, a text builder and the sentence saying why), or `None` -- the common case.
It never writes a file, never opens a session, and never raises for a missing number: a project that
configured no line gets `None` here, which is what keeps a close without knobs byte-for-byte what it
was before this plugin existed.

**The pool's weight is not a number a session reports.** This plugin claims the source it draws on
(`plugin.json`'s `sources`) and names the function below that computes it, so the number arrives
through the reading door (`ctx.reading`) -- which is what makes the tool that owns the pool this
folder's own business and not the core's. The child process is started LAST, and only when this line
really carries a line (see `offer`): a line with no line configured does not start it, and neither
does a plugin that was turned off. A missing or garbled number reads as "not known this pass", never
as 0 -- reading it as 0 would make a real debt look paid off.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# ── the declaration beside this file ─────────────────────────────────────────
# The names (which reading, which line, which tag, which method) live in `plugin.json`, never a
# second time here; this reads it. A declaration that cannot be read leaves the plugin with no
# judgement at all -- the runtime's answer for "this plugin is broken this pass", the same as an
# exception inside `offer`.

_HERE = Path(__file__).resolve().parent
DECLARATION = _HERE / 'plugin.json'

_DECL = None
_DECL_ERROR = None


def declaration() -> dict | None:
    """This plugin's declaration, read once per process. `None` when it cannot be read."""
    global _DECL, _DECL_ERROR
    if _DECL is None and _DECL_ERROR is None:
        try:
            loaded = json.loads(DECLARATION.read_text(encoding='utf-8'))
            _DECL = loaded if isinstance(loaded, dict) else None
            if _DECL is None:
                _DECL_ERROR = 'plugin.json is not an object'
        except (OSError, ValueError) as e:
            _DECL_ERROR = str(e)
    return _DECL


#: What this plugin's reading measures, in the words the generated spec shows a reader. The
#: declaration owns the mechanical half (which line, which tag, which method, and the source this
#: reading draws on); this owns the prose only, and a metric the declaration names but this table
#: does not know is simply not judged (the doctor is where a declaration that names an unknown
#: reading is reported).
#:
#: `where` says how the number was obtained, so a node handed one knows whether to go and measure or
#: to go and look. `subject` / `criterion` / `remedy` are the mechanical completion rule the spec
#: carries: what is being pulled back under the line, what the next close must read, and what to run
#: to see it.
PROSE = {
    'issue_pool_weight': {
        'unit': '',
        'subject': 'the issue pool\'s weighted sum',
        'where': '`pf issue weight` (the issue tool computes it itself; it does not go into a note)',
        'criterion': 'the pool\'s weighted sum the tool computes at the next close must be **below**',
        'remedy': 'run `pf issue weight -v` to see how this number adds up, '
                  'and confirm it is back under the line',
    },
}


# ── the tool this plugin measures with ──────────────────────────────────────
# The pool's number is computed by the tool that owns the pool, so this plugin asks it -- a child
# process, which is why it is asked LAST and only when this line really carries that line (see
# `offer`). The tool lives in this folder's own `tools/`: the pool moved in here with the plugin, so
# nothing outside the folder knows where it is.

#: The tool that owns the pool, inside this plugin's own folder.
POOL_TOOL = _HERE / 'tools' / 'issue_manager.py'

#: The one word `pf issue weight` answers with: the verb that prints the pool's weighted sum.
WEIGHT_VERB = 'weight'


def issue_argv() -> list:
    """The argv that asks this folder's tool for the pool's weighted debt."""
    return [sys.executable, str(POOL_TOOL), WEIGHT_VERB]


# ── the reading: the number the tool prints ──────────────────────────────────

def _number(value) -> float | None:
    """One reading, or `None`.

    A number is a number and nothing else: a bool is not one (it is the JSON spelling of a decision,
    not a measurement), and a string is not parsed -- guessing a number out of a tool's prose is how
    a threshold ends up firing on the wrong evidence.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def pool_weight(ctx) -> float | None:
    """How heavy the pool is, asked of the tool that owns it. `None` when it cannot be read.

    The answer is the last non-empty line of the tool's stdout; a missing or garbled one reads as
    "not known this pass", never as 0.
    """
    try:
        out = ctx.capture(issue_argv())
    except (OSError, RuntimeError):
        return None
    lines = [line.strip() for line in (out or '').splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return float(lines[-1])
    except ValueError:
        return None


# ── the metric: the declaration's names, this file's prose ───────────────────

def metrics(decl: dict) -> list:
    """The declared metric, merged with the prose above, in the declaration's own order.

    A metric whose reading has no prose here cannot be written into a node and is left out.
    """
    readings = decl.get('readings') or {}
    declared = decl.get('metrics') or {}
    out = []
    for key, spec in declared.items():
        prose = PROSE.get(key)
        if not isinstance(spec, dict) or prose is None:
            continue
        reading = readings.get(key) if isinstance(readings.get(key), dict) else {}
        out.append({**prose, **spec,
                    'reading': key,
                    'source': reading.get('source') or 'ticket'})
    return out


def _is_coding(cycle: dict) -> bool:
    """Only a line about code is judged: this metric is code-branch debt."""
    tags = [t for t in (cycle.get('tags') or []) if isinstance(t, str)]
    return 'coding' in tags


def _line_tags(cycle: dict) -> list:
    """The filter set of the line that asked for this insertion, read as it is written."""
    return [t for t in (cycle.get('tags') or []) if isinstance(t, str)]


# ── the judgement ────────────────────────────────────────────────────────────

def _wired(metric: dict, values: dict, lines: dict):
    """(value, line) for one metric, or `None` when either half is missing.

    The comparison is `>=`: the line is the last acceptable number, so reading exactly the line means
    the debt is at its limit and the insertion opens.
    """
    value = values.get(metric['reading'])
    if value is None:
        return None
    line = lines.get(metric['line'])
    if line is not None and value >= line:
        return value, line
    return None


def offer(ctx):
    """The insertion this plugin wants now, or `None` (the common case).

    `None` covers every way the plugin stays shut: no number for the metric, no line on this line, a
    number under its line, a line that is not about code, a plugin that was turned off, and a metric
    that ALREADY has a node open on the graph -- one metric, one live node at a time.

    The child is asked late on purpose: a line that carries no line at all, or a plugin that was
    turned off, does not start the issue tool.
    """
    decl = declaration()
    if not decl:
        return None
    cycle = ctx.cycle or {}
    if not _is_coding(cycle):
        return None
    declared = metrics(decl)
    if not declared:
        return None
    lines = {}
    for metric in declared:
        value = ctx.knob(metric['line'])
        if value is not None:
            lines[metric['line']] = value
    if not lines:
        # Nothing configured: the close's answer is `None` and no child is started. This is the
        # regression floor -- a project that never configured a line closes as it always did.
        return None
    enable = decl.get('enable_knob')
    if enable and ctx.knob(enable) == 'off':
        return None
    values = {}
    for metric in declared:
        if metric['line'] not in lines:
            continue
        # The number arrives through the reading door: the source this declaration claims is the
        # one `pool_weight` below computes, and the core hands its answer on untouched.
        value = ctx.reading(metric['reading'])
        if value is not None:
            values[metric['reading']] = value
    if not values:
        return None
    for metric in declared:
        got = _wired(metric, values, lines)
        if got is None:
            continue
        if ctx.nodes_with_tag(metric['tag']):
            # One metric, one live node: while one of them is still in play, the next crossing waits.
            continue
        value, line = got
        hit = {**metric, 'value': value, 'at': line}
        hit['title'] = title(hit)
        # A CALLABLE, not the text: the node's uuid decides where its spec lands, and the uuid does
        # not exist until the node has been created (`insertion.mint`).
        hit['spec_of'] = lambda node_id, _hit=hit: spec(_hit, ctx.spec_path(node_id))
        return _offer(ctx, cycle, hit)
    return None


def _shown(value: float, unit: str) -> str:
    """One number as the reader sees it: an integer stays an integer, a fraction stays a fraction."""
    return f'{value:g}{unit}'


def _offer(ctx, cycle: dict, hit: dict) -> dict:
    """The one crossing, as the core reads it: what to insert, and the words around it."""
    target = ctx.target or {}
    target_id = target.get('id') or ''
    closed = ctx.node_id or ''
    reading = _shown(hit['value'], hit['unit'])
    line = _shown(hit['at'], hit['unit'])
    note = (f'{hit["subject"]} read {reading}, over the line {line} in force for this close '
            f'(the `{hit["line"]}` knob): a node is inserted between the just-closed {closed[:8]} '
            f'and the {target.get("title") or target_id} it had picked. No session was opened, '
            f'and the inserted node is not pinned: which node the next dispatch picks is the usual '
            f'ranking\'s call (on a line that fills several slots, this same close may have pinned '
            f'the rest of its fill). After committing and pushing: '
            f'`pf next --cycle {cycle.get("id")} --spawn --json`.')
    return {
        'title': hit['title'],
        'tags': [hit['tag'], *_line_tags(cycle)],
        'spec_of': hit['spec_of'],
        'why': f'{hit["reading"]} = {reading} (line {line})',
        'note': note,
    }


# ── the node's own text ──────────────────────────────────────────────────────

def title(hit: dict) -> str:
    """The node's title: which measurement and which line, in one line a human reads in the DAG."""
    return (f'Maintenance: {hit["subject"]} = {_shown(hit["value"], hit["unit"])}, '
            f'over the line {_shown(hit["at"], hit["unit"])}')


def _closing_reading(hit: dict) -> str:
    """What the next judgement of this debt reads: the pool number is computed by the issue tool
    itself, and writing it in the note would be read by nobody."""
    return ('- the issue tool computes this metric itself (`pf issue weight`); the note does '
            'not carry it:\n'
            f'  at the next close, a number the tool computes below'
            f' {_shown(hit["at"], hit["unit"])} counts as back under the line.\n')


def spec(hit: dict, spec_path: str) -> str:
    """The generated spec: why this node exists, what its scope is, which skill owns it, and how it is
    judged done.

    Written by the tool, not by the session that will run it -- the whole point is that the judgement
    was mechanical, so the node cannot be handed the judgement as well. What it does carry is the one
    thing a worker cannot derive: which measurement was read, what it read, and where the line is.

    No function name, file path or line number from the project appears here: which files move is the
    node's own business, and a template that guessed would be a template that lied.
    """
    return (
        '# Maintenance node (generated by the tool)\n'
        'This node\'s spec is generated by a conduct plugin and copied verbatim; it says **why this '
        'node was inserted**, **the scope of this one pass**, **how to do it**, and **the completion '
        'rule fixed from the start**.\n'
        '\n'
        '## Why this node was inserted\n'
        '\n'
        f'- metric: {hit["subject"]}\n'
        f'- where this number comes from: {hit["where"]}\n'
        f'- read at the close: **{_shown(hit["value"], hit["unit"])}**\n'
        f'- the line in force for this close: **{_shown(hit["at"], hit["unit"])}** '
        f'(the `{hit["line"]}` knob; it may be stated on the line, the project, the machine or a '
        f'node, and the value the close read is the one that counts)\n'
        '- this plugin is project-level: maintenance debt belongs to no line and has no edge in '
        'the graph, so the ranking always pushes it to the bottom; the close therefore inserts it '
        'against the line in force for that close.\n'
        '\n'
        '## Scope\n'
        '\n'
        '- **this pass does one thing only**: pull this one metric back under the line. No adding '
        'plugins on the side, no refactoring on the side, no cleanup unrelated to this metric.\n'
        '- another metric crossing its line, or other work in the graph being more urgent, is '
        'outside this node\'s scope -- whoever crosses comes in for a pass of their own.\n'
        '- the size of the change follows the criterion: keep going while it fails, stop when it '
        'passes.\n'
        '\n'
        '## How to do it\n'
        '\n'
        f'- `method: {hit["method"]}` (how to run it: `~/.agents/skills/{hit["method"]}/SKILL.md`; '
        'that skill declares who runs it, and this spec only points at it)\n'
        '\n'
        '## Completion rule (mechanically judgeable)\n'
        '\n'
        f'- {hit["criterion"]} {_shown(hit["at"], hit["unit"])}.\n'
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


# ── the second way in: run as a script ──────────────────────────────────────
# `plugin.json` reports the word `issue`, and `pf` answers that word by running THIS file as a
# script (`pf`'s plugin bootstrap: the argv, `__name__` and `sys.path` entry a plain
# `python impl.py …` would give it). The line's remaining arguments arrive unchanged, and they go
# to the tool inside this folder unchanged too: this door is the folder's own command, so what the
# tool reads and where it looks for the project are exactly what they were before the tool moved in
# here. One implementation serves both ways in, which is what "a plugin is a folder with an
# implementation" means.

def main(argv=None) -> int:
    """`pf issue …`: this folder's own tool, with the rest of the line handed over as it came."""
    rest = list(sys.argv[1:] if argv is None else argv)
    return subprocess.call([sys.executable, '-X', 'utf8', str(POOL_TOOL), *rest])


if __name__ == '__main__':
    sys.exit(main())