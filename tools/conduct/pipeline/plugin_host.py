"""The generic plugin host: a folder's declaration, asked at one of the three timings.

A plugin is a folder under `plugins/` with two files in it: a declaration (`plugin.json`) and an
implementation (`impl.py`). The core knows the **kinds** only -- four knob kinds, three timings
(`close` / `pick` / `dispatch`), two actions (`insert` / `block`) -- and every name comes from a
declaration, merged and de-duplicated by the registry (`plugin_registry.py`, loaded here by path);
the method a metric names points at a skill in the plugin's own folder, which this module never reads.

Two questions are asked of a folder, and this module is the one place they are asked:

    insertions(timing, ctx)   "is there anything you want put into the graph now?" -> offer list
    gates(timing, ctx)        "may the next step run?"                            -> reason list

**Runtime tolerance, doctor strictness.** A plugin that raises is not allowed to stop somebody's
project mid-pipeline: one `WARNING:` line goes to stderr, that plugin contributes nothing this
pass, and the pipeline carries on. `sys.exit()` is the same failure written differently -- a door
may not end the process -- so `SystemExit` is caught here too, which is what the validator's live
call (`plugin_registry._live_call_problems`) already asks of every door; `KeyboardInterrupt` is
not caught, because Ctrl-C stays the human's. Most of what breaks a plugin is deterministic, and
the doctor (`pf doctor`) is where it is reported -- the same split as everywhere else in this tree:
the write side is loud about what it can prove at the moment it happens, and the runtime never
guesses.

**What a plugin may look at is this module's `Context`, and nothing else.** It is handed the cycle,
the node, the ticket and the graph the caller already read -- never a path to the ledger and never
`load_dag`, so a plugin cannot read the project behind the pipeline's back. The doors it is given are
the ones a judgement needs: a knob's effective value (`knob`), a number one of its declared readings
answers (`reading`: the ticket's, or the one a source it claimed computes for it), the open nodes
carrying a tag (`nodes_with_tag`), and the two ways to a child process (`capture` / `run`).
`spec_path` is the sixth, and it is the tree's own layout (which only `nodes.py` spells), handed over
so a plugin's generated text can name where it will land.

`enable_knob` is enforced here, for every plugin alike: a switch that resolves to `off` skips that
plugin at that timing, whatever its implementation would have said. Turning a plugin off is a word
knob on the resolution chain (node -> line -> project -> global -> factory default), so it needs no
second mechanism and the plugin itself does not have to know it can be turned off.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import seam, store
from .nodes import node_spec_path

#: `plugin_registry.py`, beside this file: loaded by path like every other neighbour, one copy per
#: process (`register` + `reuse`) -- the doctor, the CLI and a close all ask the same registry.
PLUGIN_REGISTRY_PY = Path(__file__).resolve().parent / 'plugin_registry.py'

#: What the one tolerant path prints, once per plugin per pass. One line, on stderr: stdout belongs
#: to the action.
WARNING = 'WARNING: plugin {name} {what}: {error}'


# ── the registry, as this module asks it ────────────────────────────────────

def registry():
    """`plugin_registry.py`, loaded by path -- the one reader of the `plugins/` folders."""
    return store._loader().load_by_path('pf_plugin_registry', PLUGIN_REGISTRY_PY,
                                        register=True, reuse=True)


def _registry_or_none():
    """The registry, or `None` when it cannot be loaded at all (a degraded tree: no plugins run)."""
    try:
        return registry()
    except (OSError, ImportError) as e:
        warn('<registry>', 'unavailable', e)
        return None


def _by_timing(timing: str) -> list:
    """The plugins declaring this timing; the caller narrows by the action (the other axis)."""
    found = _registry_or_none()
    if found is None:
        return []
    try:
        return found.by_timing(timing)
    except Exception as e:                       # noqa: BLE001 -- a broken registry is not fatal
        warn('<registry>', 'could not be read', e)
        return []


def _impl_of(plugin: dict):
    """One plugin's `impl.py`, or `None` when it cannot be loaded (skipped, with a warning)."""
    name = (plugin or {}).get('name') or '?'
    found = _registry_or_none()
    if found is None:
        return None
    try:
        impl = found.impl_of(name)
    except Exception as e:                       # noqa: BLE001
        warn(name, 'could not be loaded', e)
        return None
    if impl is None:
        warn(name, 'has no usable implementation', 'impl_of() answered None')
    return impl


def _decl(plugin: dict) -> dict:
    decl = (plugin or {}).get('decl')
    return decl if isinstance(decl, dict) else {}


def warn(name: str, what: str, error) -> None:
    """One tolerance line, on stderr (`actions` prints the same line when an offer explodes later).

    The format lives here, once: the two places that skip a plugin both speak it, so a reader sees
    one spelling of "this plugin contributed nothing this pass".
    """
    print(WARNING.format(name=name, what=what, error=error), file=sys.stderr)


# ── what a plugin is allowed to see ────────────────────────────────────────

def _number(value):
    """One reading as a number, or `None`: a number is a number and nothing else.

    A bool is the JSON spelling of a decision, never 1, and a string is not parsed -- guessing a
    number out of a worker's or a tool's prose is how a threshold fires on the wrong evidence. The one
    rule both roads to a number come through (`reading`'s note lookup and a source's own function).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _from_note(sig, key: str):
    """The number one ticket reported under `outputs.reading.<key>`, or `None` (see `_number`)."""
    outputs = (sig or {}).get('outputs') or {}
    reading = outputs.get('reading') if isinstance(outputs, dict) else None
    if not isinstance(reading, dict):
        return None
    return _number(reading.get(key))


class Context:
    """Everything a plugin is handed at one timing: the state, and the few doors to it.

    The caller passes what it already read -- the cycle, the node it is judging, that node's ticket,
    the graph and the node the close had picked. Nothing here reads the project: the pipeline hands
    the graph down so a close reads it once, and a plugin cannot read it a second time behind its
    back. The one thing this class goes and asks for is the declaration behind a reading key: where a
    number comes from is the declaration's statement, and the door reads it rather than restating it.
    """

    def __init__(self, project, cycle: dict | None = None, node_id: str | None = None,
                 sig: dict | None = None, dag: dict | None = None, target: dict | None = None):
        self.project = Path(project)
        self.cycle = cycle if isinstance(cycle, dict) else {}
        self.node_id = node_id
        self.sig = sig
        self.dag = dag
        self.target = target
        # `reading`'s two memories: which declaration cell stands behind a key, and the number a
        # source's own function answered this pass -- so a plugin that asks twice is computed once.
        self._cells: dict = {}
        self._numbers: dict = {}
        self._computing: set = set()

    def knob(self, name: str):
        """One knob's effective value, from the whole chain (node -> line -> project -> global ->
        default), or `None` when nothing states it."""
        if not name:
            return None
        return store.resolve_knob(self.project, name, cycle=self.cycle, node=self.node_id)

    def reading(self, key: str) -> float | None:
        """One declared reading's number, or `None` when there is none this pass.

        Two kinds of source, one door. A `ticket` reading -- and any key no declaration names, which
        the note may carry all the same -- is the number the worker wrote under
        `outputs.reading.<key>`. A reading drawn on a source its own declaration claims is the number
        that source computes: the declaration names the implementation function (`by`), this calls it
        with this context, and the answer is handed on untouched. The core reads neither number's
        meaning -- a number is a number and nothing else: a bool is a decision, not a measurement, and
        a string is not parsed, because guessing a number out of a tool's prose is how a threshold
        fires on the wrong evidence.

        A core source the core does not compute (`pool`: a tool's own number the plugin fetches with
        `capture`) has no answer here -- it is not the ticket's, and no function was named for it.
        """
        if not key:
            return None
        cell = self._cell(key)
        if cell is None or cell.get('source') == 'ticket':
            return _from_note(self.sig, key)
        return self._computed(key, cell)

    def _cell(self, key: str):
        """The declaration cell behind one reading key, or `None` (undeclared, or no registry)."""
        if key in self._cells:
            return self._cells[key]
        found = _registry_or_none()
        cell = None
        if found is not None:
            try:
                cell = next((entry for entry in found.readings() if entry.get('key') == key), None)
            except Exception as e:               # noqa: BLE001 -- a broken registry is not fatal
                warn('<registry>', 'could not be read', e)
        self._cells[key] = cell
        return cell

    def _computed(self, key: str, cell: dict):
        """The number the source's own implementation computes, or `None` (one `WARNING:` at most).

        The function the claim names is called with this context -- it needs the project and the two
        ways to a child process, exactly like the timing's own door -- once per key per pass. A
        function that is missing, one that raises, and one that answers something that is not a number
        all cost this number only, the same containment every other door of a plugin gets; a function
        that asks for the number it is computing is refused the same way rather than recursing.
        """
        if key in self._numbers:
            return self._numbers[key]
        by = cell.get('by') or ''
        name = cell.get('plugin') or '?'
        value = None
        if not by:
            self._numbers[key] = None
            return None
        if key in self._computing:
            warn(name, f'asked for {key!r} while computing it', 'the number stays unknown this pass')
        else:
            self._computing.add(key)
            try:
                impl = _impl_of({'name': name})
                call = getattr(impl, by, None) if impl is not None else None
                if impl is not None and not callable(call):
                    warn(name, f'has no {by}()', f'its source for {key!r} cannot be computed')
                elif callable(call):
                    value = _number(call(self))
            except (Exception, SystemExit) as e:     # noqa: BLE001 -- tolerant here, strict in doctor
                warn(name, f'failed to compute {key!r}', e)
                value = None
            finally:
                self._computing.discard(key)
        self._numbers[key] = value
        return value

    def nodes_with_tag(self, tag: str) -> list:
        """The ids of the still-open (`todo`) nodes carrying one tag, sorted.

        "Not settled yet" is `todo`: `completed` means the pass is delivered, and `shelved` is out of
        play waiting for a human -- neither may block the next insertion, or a human who never
        revives one would leave the plugin mute forever.
        """
        nodes = (self.dag or {}).get('nodes') or {}
        out = []
        for uid, node in nodes.items():
            if not isinstance(node, dict) or node.get('status') != 'todo':
                continue
            if tag in [t for t in (node.get('tags') or []) if isinstance(t, str)]:
                out.append(uid)
        return sorted(out)

    def capture(self, argv) -> str:
        """Run one command and hand back its stdout (the only road to a number a tool prints)."""
        return seam.capture(list(argv), self.project)

    def run(self, argv) -> None:
        """Run one command, its effect being the point."""
        return seam.run(list(argv), self.project)

    def spec_path(self, node_id: str) -> str:
        """Where a node's own text lands -- this tree's layout, which only `nodes.py` spells."""
        return node_spec_path(self.project, node_id)


# ── the two questions ───────────────────────────────────────────────────────

def _enabled(plugin: dict, ctx: Context) -> bool:
    """Is this plugin switched on? A plugin with no `enable_knob` always is.

    The switch is an ordinary word knob on the resolution chain, so "off" can be stated at any layer
    and the widest thing it can be stated at is the project. A switch that cannot be worked out at
    all is read as off for this pass: a plugin whose own switch is broken is not one to act on.
    """
    key = _decl(plugin).get('enable_knob')
    if not key:
        return True
    try:
        return ctx.knob(key) != 'off'
    except Exception as e:                       # noqa: BLE001
        warn(plugin.get('name') or '?', f'could not read its switch {key!r}', e)
        return False


def _usable_offer(name: str, offer):
    """One answer, normalised to the fields `actions._insertions_due` reads -- or `None`.

    The shape is checked HERE, before the graph edit exists: half an offer would fail inside
    `insertion.insert` after the close has already landed, which is the one moment the pipeline
    cannot take back. Each field is checked against the type that edit will use it as, and a wrong
    one costs the plugin this pass (one warning naming the field), exactly like a raise.

    `tags` is a list of strings and nothing else: a bare string would be read as one tag per
    character by the graph tool's argv, so it is refused rather than wrapped -- a plugin that says
    `"tags": "coding"` has a bug, and "coding" is not what it meant to write.

    The steps an insertion action shows are the CORE's words (`actions._insertion_steps`: they carry
    the placeholder only `insertion.py` may spell, so an offer never has to state them). An offer that
    states `steps` anyway is shape-checked here and then not read.
    """
    if not isinstance(offer, dict):
        warn(name, 'returned an unusable offer',
             f'an offer is an object, got {type(offer).__name__}')
        return None
    title = offer.get('title')
    if not isinstance(title, str) or not title.strip():
        warn(name, 'returned an unusable offer', f'`title` must be a non-empty string, got {title!r}')
        return None
    spec_of = offer.get('spec_of')
    if not callable(spec_of):
        warn(name, 'returned an unusable offer',
             '`spec_of` must be a callable that builds the node\'s text')
        return None
    tags = offer.get('tags')
    if tags is None:
        tags = ()
    if isinstance(tags, str) or not isinstance(tags, (list, tuple)) \
            or not all(isinstance(tag, str) for tag in tags):
        warn(name, 'returned an unusable offer',
             f'`tags` must be a list of strings, got {tags!r}')
        return None
    fields = offer.get('fields')
    if fields is None:
        fields = {}
    if not isinstance(fields, dict):
        warn(name, 'returned an unusable offer', f'`fields` must be an object, got {fields!r}')
        return None
    note = offer.get('note')
    if note is None:
        note = ''
    if not isinstance(note, str):
        warn(name, 'returned an unusable offer', f'`note` must be a string, got {note!r}')
        return None
    steps = offer.get('steps')
    if steps is None:
        steps = []
    if isinstance(steps, str) or not isinstance(steps, (list, tuple)) \
            or not all(isinstance(step, str) for step in steps):
        warn(name, 'returned an unusable offer', f'`steps` must be a list of strings, got {steps!r}')
        return None
    return {'plugin': name, 'title': title, 'tags': list(tags), 'spec_of': spec_of,
            'fields': dict(fields), 'note': note, 'steps': list(steps)}


def insertions(timing: str, ctx: Context) -> list:
    """Every insertion the plugins at this timing want now, in registry order.

    Each answer is the plugin's own: a title, tags, a callable that builds the node's text, and the
    fields a reader is shown -- checked here (`_usable_offer`) and handed on as the caller's fields.
    Nothing is inserted here -- the graph edit is `insertion.py`'s, and the caller places it
    (`actions._insertions_due`, which also contains the text builder's eager run).
    """
    offers = []
    for plugin in _by_timing(timing):
        if _decl(plugin).get('action') != 'insert':
            continue
        name = plugin.get('name') or '?'
        if not _enabled(plugin, ctx):
            continue
        impl = _impl_of(plugin)
        if impl is None or not hasattr(impl, 'offer'):
            if impl is not None:
                warn(name, 'has no offer()', 'the timing it declares cannot be served')
            continue
        try:
            offer = impl.offer(ctx)
        except (Exception, SystemExit) as e:     # noqa: BLE001 -- tolerant at runtime, strict in doctor
            warn(name, 'failed', e)
            continue
        if not offer:                            # None / {} / False: "nothing from me this pass"
            continue
        usable = _usable_offer(name, offer)
        if usable is not None:
            offers.append(usable)
    return offers


def gates(timing: str, ctx: Context) -> list:
    """One reason string per plugin that says "not now"; an empty list is a green light.

    Gates combine with **and**: any one saying no is no. A gate that raises is skipped (its own
    failure is not a reason to stop somebody's line), and the reason it carries is prose for a human.
    """
    reasons = []
    for plugin in _by_timing(timing):
        if _decl(plugin).get('action') != 'block':
            continue
        name = plugin.get('name') or '?'
        if not _enabled(plugin, ctx):
            continue
        impl = _impl_of(plugin)
        if impl is None or not hasattr(impl, 'check'):
            if impl is not None:
                warn(name, 'has no check()', 'the timing it declares cannot be served')
            continue
        try:
            reason = impl.check(ctx)
        except (Exception, SystemExit) as e:     # noqa: BLE001
            warn(name, 'failed', e)
            continue
        if reason:
            reasons.append(str(reason))
    return reasons
