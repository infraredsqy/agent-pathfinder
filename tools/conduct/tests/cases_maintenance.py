"""The two debt-line plugins: a crossed metric inserts a node at the close.

One file, two metrics, one mechanism. `plugins/test-audit/` watches the project test suite's wall
clock -- the number is the one a session measured and wrote into its own ticket -- and
`plugins/issue/` watches the issue pool's weighted sum -- the number is computed by the tool that
owns the pool, and no session reports it. Each folder carries its own declaration, implementation and
skill, and this file drives each of them the way the core does: through the declaration, never
through a name the core holds.

`TicketDebt` and `PoolDebt` are those two metrics as a case needs them: the names their declarations
own, the line this suite writes, and the half no declaration can state -- how a crossing is PRODUCED,
and where the number comes from. The shared cases are written once (`ChannelCases`, `SwitchCases`,
`ThresholdCases`, `ScopeCases`, `DedupCases`) and bound twice, so each metric gets a pass of its own:
`TestTicketChannel` and `TestPoolChannel`, and the same down the file.

The rule the file is built around is unchanged: the judgement happens at the CLOSE, each metric's
number comes from wherever its declared `source` says (`test_seconds` from the ticket's `reading`
key, `issue_pool_weight` from the issue tool that owns the pool), the lines arrive as knobs from the
whole resolution chain (node -> line -> project -> machine -> factory), and a crossing line puts a
node between the node just closed and the node the close had already picked. **The insertion is a
graph edit and nothing else**: no session is opened, no prompt is written and the line's hand is not
moved -- the next dispatch picks the new node up by the usual ranking. A project that configures no
line gets the close it always got -- that is the regression these plugins live or die by, and it is
the first case of each pass.

Every case that needs a shipped declaration carries `requires_plugin(...)`: deleting that folder is a
supported state of this tree (the core keeps closing and dispatching), so the behaviour cases skip
rather than fail there. A guard asks the pass classes -- the ones binding a metric -- for that mark,
because the ordinary run has every folder installed and a decorator lost from one of them is invisible
there (`cases_guards.py`); the doors written per method keep it by hand. What the two plugins do to
EACH OTHER -- a close that crosses both lines, and a tree that has lost one of the two folders -- is
`TestTheTwoLinesInOneClose`. The mechanism cases that must run with no plugin installed point
`PF_PLUGINS_DIR` at their own folder -- `cases_plugin.py`.

The side where a line really runs (the graph edit through real processes, a whole line walked to the
end, the form the note's receiver sees) lives in `cases_maintenance_run.py`.
"""
from __future__ import annotations

import contextlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE, NODE2, NODE3, pipeline, normalise
from harness import (CONDUCT_DIR, DAG_MANAGE, PLUGINS_DIR, InProcessDagManage, STATE_MANAGE,
                     TOOLS_DIR, WorkerSession, _fresh_module, _run_proc, patched, plugins_dir,
                     requires_plugin, run_state_manage)

__all__ = [
    'Debt', 'DebtWorker', 'POOL', 'PoolDebt', 'TICKET', 'TicketDebt',
    'clear_pool', 'close_offers', 'coding_line', 'layout', 'pool_ids', 'seed_pool', 'set_config',
    'write_ticket',
    'TestPoolChannel', 'TestPoolDedup', 'TestPoolScope', 'TestPoolSwitch', 'TestPoolThresholds',
    'TestTheTwoLinesInOneClose', 'TestTicketChannel', 'TestTicketDedup', 'TestTicketScope',
    'TestTicketSwitch', 'TestTicketThresholds',
]

#: The tool that owns the issue pool. The pool moved into the plugin's own folder with everything
#: else that is its business, so this is a path into `plugins/`: the plugin's implementation asks the
#: tool for the pool's number through this same CLI, out of a child process of its own.
ISSUE_MANAGE = PLUGINS_DIR / 'issue' / 'tools' / 'issue_manager.py'

#: The weights the issue tool gives a severity (`plugins/issue/tools/storage.py`). Stated here as the
#: EXPECTATION a fixture lays out, not as a copy of the tool: the pool's number is the sum over its
#: entries, so a case can only produce a number by putting entries in.
WEIGHTS = (('P0', 8), ('P1', 4), ('P2', 2), ('P3', 1))


def layout(value) -> tuple:
    """The severities whose weights add up to `value`, heaviest first (none for `None` or 0)."""
    left, out = int(value or 0), []
    for severity, weight in WEIGHTS:
        while left >= weight:
            out.append(severity)
            left -= weight
    return tuple(out)


def seed_pool(sb, *issues) -> None:
    """Put items into the pool through its own command (the same source the plugin reads)."""
    for item in issues:
        _run_proc(sb.root, [ISSUE_MANAGE, 'add', '--id', item['id'], '--title', item['id'],
                            '--severity', item['severity'],
                            '--decision', item.get('decision', 'autofix')])


def pool_ids(sb) -> list:
    """The ids the pool holds right now, read off the tool's own listing."""
    out = _run_proc(sb.root, [ISSUE_MANAGE, 'list', '--brief']).stdout
    return [item['id'] for item in json.loads(out or '[]')]


def clear_pool(sb) -> None:
    """Take out every entry the pool holds: "the debt is paid" has to be visible in the store.

    The pool is a store and its number is derived from it, so a fixture that wants a smaller number
    has to remove entries -- there is nowhere to write the number down. What it removes is what the
    tool says is there, which for these sandboxes is only ever what this fixture put in.
    """
    for issue_id in pool_ids(sb):
        _run_proc(sb.root, [ISSUE_MANAGE, 'remove', issue_id])


def write_ticket(sb, node, outputs, *, via_handoff=False):
    """A worker's report, through one of the two roads the suite has.

    `handoff.py` as a real process is what a session's last act really is (`via_handoff`); the
    in-process `signal.py` writer is the same file without the process start. A case whose subject is
    a real process takes the first; every other case takes the second.
    """
    if via_handoff:
        return sb.handoff('cyc', node, 'completed', 'done', outputs)
    return sb.write_signal('cyc', node, 'completed', 'done', outputs)


# ═══════════════════════════════════════════════════════════════════════════════
#  The two metrics, as a case needs them: the names, the line, and the crossing
# ═══════════════════════════════════════════════════════════════════════════════

class Debt:
    """One plugin's metric, as this suite drives it: the names, the line, and how it crosses.

    The names are the declaration's (`plugin.json`), read here so that a rename inside a plugin folder
    stays a folder-local edit; nothing in this file may restate them for the core's benefit. What a
    profile adds is what no declaration can state: the line this suite writes, the numbers on each
    side of it, and the ONE thing that really separates the two metrics -- whether the number arrives
    in the worker's own note (`TicketDebt`) or is computed by a tool out of a store (`PoolDebt`).
    """

    #: The plugin folder, and the five names its declaration owns: the switch that turns it on and
    #: off, the line it compares against, the reading it draws its number from, the dedupe tag the
    #: outside writes down to name this debt, and the method its generated node points a session at.
    plugin = ''
    switch = ''
    line = ''
    reading = ''
    tag = ''
    method = ''
    #: Which source the number comes from -- the core's own `ticket` (a number a session reports in
    #: its note), or a name the plugin's own declaration claims for a number its own tool computes
    #: (`pool` for the issue plugin) -- and the knob after which the note's form asks for it (`''`
    #: when the form never asks, because no session reports it).
    source = ''
    when_knob = ''
    #: The metric in the words the generated spec shows a reader, and the words that spec must carry
    #: for THIS metric -- where its number comes from, and how to see it again.
    subject = ''
    shows = ()
    #: What one unit of this number is spelled as in the spec and the title (`s`, or nothing).
    unit = ''
    #: The line this suite writes onto a cycle, and the numbers it writes against it: `under` below
    #: it, `exact` landed on it, `over` past it.
    at = 0
    under = 0
    exact = 0
    over = 0

    def shown(self, value) -> str:
        """One number as the generated spec shows it to a reader."""
        return f'{value:g}{self.unit}'

    def note(self, value) -> dict:
        """What a worker's `-o` must carry for this metric to read `value` -- `{}` when it is the
        note's metric at all: no session reports the pool."""
        return {}

    def report(self, sb, node, value, *, via_handoff=False) -> None:
        """The debt as it stands at the close of `node`: write that node's ticket, and put the number
        where this metric really keeps it. `value=None` means no number is in hand this pass."""
        raise NotImplementedError

    def repay(self, sb, value) -> None:
        """The debt is back at `value` from now on, as the session sent for it would leave it.

        Nothing for the ticket's metric: its number IS the note the session writes, so the worker
        repays it by reporting the fixed one. The pool's is a store somebody has to change.
        """
        return None


class TicketDebt(Debt):
    """`plugins/test-audit/`: the project test suite's full wall clock, measured by a session.

    The number is the worker's own: it runs the verification, writes the seconds into its ticket's
    `reading` slot, and the close reads them back. `where` in the generated spec says exactly that,
    which is why `shows` carries it -- and why this profile can produce a crossing by writing one
    number into one note.
    """

    plugin = 'test-audit'
    switch = 'test_audit'
    line = 'test_seconds_limit'
    reading = 'test_seconds'
    tag = 'test-audit:test_seconds'
    method = 'pf-test-audit'
    source = 'ticket'
    when_knob = line                       # the form asks once this line is in force
    subject = "the project test suite's full wall clock (seconds)"
    unit = 's'
    shows = ("the note's `outputs.reading.", '**measure**')
    at = 120
    under = 119
    exact = 120
    over = 160

    def note(self, value) -> dict:
        return {} if value is None else {'reading': {self.reading: value}}

    def report(self, sb, node, value, *, via_handoff=False) -> None:
        write_ticket(sb, node, {'deliverable': 'x', **self.note(value)}, via_handoff=via_handoff)


class PoolDebt(Debt):
    """`plugins/issue/`: the issue pool's weighted sum, computed by the tool that owns the pool.

    No session reports this number: the tool computes it out of the store at the close, so the ticket
    carries nothing and the fixture can only move the number by moving the STORE. `shows` is that
    difference in the generated spec's own words.
    """

    plugin = 'issue'
    switch = 'issue_pool'
    line = 'issue_pool_weight_limit'
    reading = 'issue_pool_weight'
    tag = 'issue:issue_pool_weight'
    method = 'pf-issue-repair'
    source = 'pool'                        # nobody reports it: the tool computes it, so no form asks
    subject = "the issue pool's weighted sum"
    shows = ('`pf issue weight`', 'the note does not carry it')
    at = 8
    under = 2
    exact = 8
    over = 10

    def report(self, sb, node, value, *, via_handoff=False) -> None:
        write_ticket(sb, node, {'deliverable': 'x'}, via_handoff=via_handoff)
        self.repay(sb, value)

    def repay(self, sb, value) -> None:
        """Lay the pool out at exactly `value`: clear what this fixture put there, then add the
        entries whose severities add up to it."""
        clear_pool(sb)
        for index, severity in enumerate(layout(value)):
            seed_pool(sb, {'id': f'debt-{index:02d}', 'severity': severity})


#: The two metrics, named once for every case below: `plugins/test-audit`, and `plugins/issue`.
TICKET = TicketDebt()
POOL = PoolDebt()


def set_config(sb, key, value, *extra):
    """Write a knob into the project through its own command (the same chain these plugins read).

    `--global` goes through the sandbox's own home directory (`Sandbox.home`), and `--node <id>` /
    `--cycle <id>` at the layer each names: the layer a case writes is the layer `pf config` writes.
    """
    return run_state_manage(sb.root, 'config', 'set', key, value, *extra)


def coding_line(test, debt, *, thresholds=True, value=None, dispatch=True, pair=True):
    """A coding line, started and pinned on its head node, with a ticket ready for it.

    The DAG is a real one -- `NODE` the head, `NODE2` waiting for it -- because the insertion
    rewrites the edge BETWEEN the node that was closed and the node the close had already picked: a
    pair with no edge between them would exercise only the no-op half of that.

    `thresholds` writes this metric's line onto the cycle (without one the plugin never judges at all);
    `value` is what the debt reads at the head's close (`None`: nothing is in hand); `dispatch` walks
    the line one step and leaves the head's ticket behind; `pair` adds the second node -- a case whose
    subject is the line's own settings takes just the head.
    """
    sb = test.make()
    sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
    if pair:
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
    sb.start_cycle(tags=['coding'])
    if thresholds:
        sb.set_policy('cyc', debt.line, debt.at)
    if dispatch:
        sb.next()                                  # the dispatch for NODE
        debt.report(sb, NODE, value)
    return sb


def close_offers(sb, *, target=None, ticket=None):
    """What the plugins offer at this close, through the core's own door.

    The judgement is asked exactly the way `_close_own_node` asks it: one `Context` carrying the line,
    the node being closed, that node's ticket and the graph. No graph edit is made here -- the offer
    list is the whole answer this reads.
    """
    return pipeline.insertions('close', pipeline.Context(
        sb.root, cycle=sb.cycle(), node_id=NODE,
        sig=ticket if ticket is not None else sb.read_signal('cyc', NODE),
        dag=sb.dag(), target=target or {'id': NODE2, 'title': 'Write code B'}))


class DebtWorker(WorkerSession):
    """A session that measures this debt, and repays it when sent for one of its own nodes.

    Every note it writes carries the number this metric reads -- for the ticket's metric that is the
    note's `reading` slot; for the pool's it is nothing at all, because no session reports the pool
    (`Debt.note`). The note for a node this plugin INSERTED reports the repaired number instead, and
    the pool is laid back under the line when that node runs (`Debt.repay`): a worker that reported the
    same over-the-line number after doing the work would be saying it delivered while the debt stood,
    and "one node per crossing" is only readable when the debt really moves.
    """

    def __init__(self, sb, debt, *, crosses=None, settled=None, cycle='cyc', **kw):
        super().__init__(sb, cycle=cycle, **kw)
        self.debt = debt
        self.crosses = debt.over if crosses is None else crosses
        self.settled = debt.under if settled is None else settled
        # Each instance starts from the real graph: `last_added` is whatever the last `add` in this
        # process minted, which belongs to another sandbox's case as often as not.
        self.own = set()

    def work(self, action):
        """Note what this pass inserted, repay the debt when that node's turn comes, then report.

        Every `add` the tool runs mints exactly one node, and a plugin's insertion is the only caller:
        the uuid the graph just grew is the node this plugin asked for. The harness answers
        `dag-manage` in-process here, so it is readable without parsing anything.
        """
        if InProcessDagManage.last_added:
            self.own.add(InProcessDagManage.last_added)
        node = action.get('node_id') or self.sb.current_node(self.cycle)
        if node in self.own:
            self.debt.repay(self.sb, self.settled)
        return super().work(action)

    def outputs_for(self, action, deliverable, node):
        value = self.settled if node in self.own else self.crosses
        return {**super().outputs_for(action, deliverable, node), **self.debt.note(value)}


# ═══════════════════════════════════════════════════════════════════════════════
#  The shared cases, bound to one metric each
# ═══════════════════════════════════════════════════════════════════════════════

class ChannelCases:
    """The plugin's own channel: nothing configured, under the line, on it, and past it.

    The subject is the mechanism the two plugins share, read off the declaration: which line makes it
    judge, what it does with a number, and what the graph looks like after. `DEBT` is the metric this
    binding drives.
    """

    DEBT = None

    # ── the regression floor: with no number configured, the close carries not one word more ─────

    def test_without_a_threshold_the_close_and_the_graph_are_untouched(self):
        """With not one number configured in the project, this plugin is wholly inert: the return
        value, the graph and the ledger are byte-for-byte what they are without it.

        Two projects walk the same line, the one difference being that one of them wrote one more line
        onto its cycle. The configured one still inserts **nothing** (the number is under the line),
        so the two ledgers must match word for word -- the policies slot aside (it is supposed to
        differ). The rest follows
        `TestNodeTags.test_an_untagged_cycle_picks_what_it_always_picked`: a timestamp is a legitimate
        difference, and nothing else may change by a word.
        """
        def walked(data):
            """Drop the policies slot from the ledger: what the close wrote is all in what is left."""
            data = normalise(data)
            for cycle in data.get('cycles') or []:
                cycle.pop('policies', None)
            return data

        control = coding_line(self, self.DEBT, thresholds=False, value=self.DEBT.under)
        configured = coding_line(self, self.DEBT, thresholds=True, value=self.DEBT.under)

        control_action = control.close_round(NODE)
        configured_action = configured.close_round(NODE)

        self.assertEqual(control_action['action'], 'dispatch')
        self.assertEqual(normalise(control_action), normalise(configured_action),
                         'a line that is not crossed must change nothing at all')
        self.assertEqual(configured.current_node(), NODE2, 'the close still pins its own pick')
        self.assertEqual(len(configured.dag()['nodes']), 2, 'no node was inserted')

        for rel in ('.pf/ideas.json', '.pf/state.json'):
            left = walked(json.loads(control.tree_contents()[rel]))
            right = walked(json.loads(configured.tree_contents()[rel]))
            self.assertEqual(left, right, f'{rel} must be what it always was')

    def test_a_close_with_no_number_in_hand_never_inserts_anything(self):
        """The number is not there this pass: with a line configured and no debt in hand, the clearest
        line does nothing.

        For the ticket's metric that is a note carrying no `reading`; for the pool's it is a store
        holding nothing, which the tool computes as 0.
        """
        sb = coding_line(self, self.DEBT, value=None)

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 2)

    def test_a_number_under_the_line_inserts_nothing(self):
        """The number is under the line: this is the control -- same project, same line, only the
        number pressed below it."""
        sb = coding_line(self, self.DEBT, value=self.DEBT.under)

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(sb.current_node(), NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 2, 'nothing crossed, nothing inserted')
        self.assertEqual(sb.path_nodes(), [NODE])

    # ── crossing: the graph really grows a node, the edges are right, the new one is not pinned ────

    def test_a_crossed_line_inserts_a_node_between_the_close_and_its_pick(self):
        """The number reaches the line: the close's settle inserts a new node between the node just
        closed and the node it had picked -- **the graph and nothing else**.

        One assertion pins five things: the graph grew one node, the two new edges are wired right,
        the old **direct** edge is gone, the answer is an insertion rather than a dispatch, and the
        line put down only the node it closed -- its hand is empty, because an insertion pins nothing.
        Which node runs next is the next dispatch's question
        (`test_the_next_dispatch_picks_the_inserted_node`).
        """
        sb = coding_line(self, self.DEBT, value=self.DEBT.over)

        action = sb.close_round(NODE)

        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 3, 'the debt node was not inserted')
        made = action['node_id']
        self.assertNotIn(made, (NODE, NODE2))

        # the edges: old node -> debt node -> the original pick, and the old direct edge is gone
        self.assertEqual(nodes[made]['dependencies'], [NODE],
                         'the debt node must be the closed node\'s direct dependent')
        self.assertEqual(nodes[NODE2]['dependencies'], [made],
                         'the picked node must now wait for the debt node')
        self.assertNotIn(NODE2, nodes[NODE]['dependencies'])
        self.assertEqual(nodes[NODE]['status'], 'completed')
        self.assertEqual(nodes[NODE2]['status'], 'todo', 'being picked is not being run')
        self.assertEqual(nodes[made]['status'], 'todo')
        self.assertEqual(sb.path_nodes(), [NODE])

        # the declaration's dedupe tag is what the outside writes to name this debt, and it rides
        # with the line's own filter set
        self.assertIn(self.DEBT.tag, nodes[made]['tags'])
        self.assertIn('coding', nodes[made]['tags'])

        # an insertion is not a dispatch: no prompt, and the hand stays where the close left it
        self.assertEqual(action['action'], 'insert_node')
        self.assertNotIn('prompt', action)
        self.assertNotIn('prompt_text', action)
        self.assertEqual(sb.holding_ids(), [], 'an insertion must not pin a node')
        self.assertEqual(sb.cycle()['status'], 'active', 'an insertion must not park the line')

    def test_an_insertion_opens_no_session(self):
        """The whole difference from a dispatch, read off the disk: no prompt file, no started stamp.

        The inserted node's launch directory does not exist at all. The prompt is written by the
        dispatch that picks the node, one step later -- which is also why `next --after … --spawn`
        cannot open a session here: the answer is not a dispatch.
        """
        sb = coding_line(self, self.DEBT, value=self.DEBT.over)

        action = sb.close_round(NODE)

        made = action['node_id']
        launch = sb.root / '.pf' / 'meta' / 'launch' / 'cyc'
        self.assertFalse((launch / made).exists(),
                         f'an insertion wrote a prompt file for {made}: {launch / made}')
        self.assertFalse(pipeline.started_marker(sb.root, 'cyc', made).is_file(),
                         'an insertion stamped a session as started')
        # what it DID write: the node's own text, where the one path says it lands
        self.assertEqual(Path(action['spec']),
                         Path(pipeline.nodes.node_spec_path(sb.root, made)))
        self.assertTrue(Path(action['spec']).is_file())

    def test_the_generated_spec_lands_where_the_one_path_says(self):
        """Where the generated spec lands is settled in one place, `nodes.node_spec_path`.

        The copy that was written, the sentence the spec reports at its own end, and the one the
        writer computed are one and the same file.
        """
        sb = coding_line(self, self.DEBT, value=self.DEBT.over)

        action = sb.close_round(NODE)

        made = action['node_id']
        landed = Path(pipeline.nodes.node_spec_path(sb.root, made))
        self.assertTrue(landed.is_file(), f'{landed} is not where the writer said it would be')
        self.assertIn(f'(this spec lands at: {landed.as_posix()})',
                      landed.read_text(encoding='utf-8'))

    def test_the_spec_path_is_built_in_one_file(self):
        """In the implementation package, `nodes.py` is the only file that builds the `spec.md`
        segment."""
        impl = CONDUCT_DIR / 'pipeline'
        sites = sorted(path.name for path in sorted(impl.glob('*.py'))
                       if "'spec.md'" in path.read_text(encoding='utf-8'))

        self.assertEqual(['nodes.py'], sites)

    def test_a_crossed_line_only_defers_the_node_it_picked(self):
        """A multi-slot line meets a crossing: only the picked slot is deferred, the rest of the fill
        that came up in the same pass is pinned as usual, and the insertion takes no slot at all.

        The fill list is handed over whole (`parallel` minus what is still held), and the new node
        stands in only for the picked slot; if nobody pins the rest of the list it just sits empty --
        those nodes are ready, yet neither held nor waiting in the graph for anyone. The inserted node
        itself is NOT held: it waits in the graph for the next pick, which is what keeps "insert" and
        "start running" apart.
        """
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.add_node(NODE3, 'Write code C', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])            # default parallel=1: the start pins one slot only
        sb.set_policy('cyc', 'parallel', 3)
        sb.set_policy('cyc', self.DEBT.line, self.DEBT.at)
        sb.next()                                  # the dispatch for NODE
        self.DEBT.report(sb, NODE, self.DEBT.over)

        action = sb.close_round(NODE)

        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 4, 'the debt node was not inserted')
        made = action['node_id']
        self.assertNotIn(made, (NODE, NODE2, NODE3))
        deferred = [u for u in (NODE2, NODE3) if nodes[u]['dependencies'] == [made]]
        self.assertEqual(len(deferred), 1, 'exactly one fill node waits behind the debt node')
        keep = [u for u in (NODE2, NODE3) if u != deferred[0]][0]
        self.assertEqual(nodes[keep]['dependencies'], [NODE], 'the rest of the fill is untouched')
        self.assertEqual(nodes[keep]['status'], 'todo')
        self.assertEqual(sb.holding_ids(), [keep],
                         'the rest of the fill keeps its slot, and the inserted node takes none')

    def test_the_inserted_node_carries_why_what_scope_and_a_mechanical_verdict(self):
        """The debt node's spec is generated by the plugin: why it was inserted, the scope of this
        pass, and a mechanically judgeable completion rule.

        `shows` is this metric's own half of "why": where its number came from, and how to see it
        again -- the one paragraph the two plugins cannot share, because their numbers arrive in two
        different places.
        """
        sb = coding_line(self, self.DEBT, value=self.DEBT.over)

        action = sb.close_round(NODE)
        made = action['node_id']
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')
        node = sb.dag()['nodes'][made]

        # why: which metric, what it read, where the line is
        self.assertIn(self.DEBT.subject, spec)
        self.assertIn(self.DEBT.reading, spec)
        self.assertIn(self.DEBT.shown(self.DEBT.over), spec)
        self.assertIn(self.DEBT.shown(self.DEBT.at), spec)
        self.assertIn(self.DEBT.line, spec)
        self.assertIn('Why this node was inserted', spec)
        # ... and where that number comes from, in this metric's own words
        for shown in self.DEBT.shows:
            self.assertIn(shown, spec)
        # scope: this pass does one thing only
        self.assertIn('this pass does one thing only', spec)
        # how: name which method skill owns this kind of debt -- the declaration's own method
        self.assertIn('How to do it', spec)
        self.assertIn(f'method: {self.DEBT.method}', spec)
        self.assertIn(f'~/.agents/skills/{self.DEBT.method}/SKILL.md', spec)
        # completion rule: pull it back under the line, and give a command that can be run
        self.assertIn('Completion rule (mechanically judgeable)', spec)
        self.assertIn('**below**', spec)
        self.assertIn('how to see this number', spec)
        # the spec carries no implementation detail: the template guesses no function name / file path /
        # line number
        self.assertNotIn('.py', spec)
        # the node on the graph says what it is in one line
        self.assertIn('Maintenance: ', node['title'])
        self.assertIn(self.DEBT.shown(self.DEBT.over), node['title'])

    def test_the_spec_and_the_note_name_the_line_and_the_knob_it_came_from(self):
        """How much the line is and which knob it was read from: both the spec and the close's note
        name them, and that is where a human looks them up.

        The threshold may be stated on the line, the project, the machine or a node, and the value the
        close read is the one that counts -- so the node has to say which knob that was.
        """
        sb = coding_line(self, self.DEBT, value=self.DEBT.over)
        action = sb.close_round(NODE)
        made = action['node_id']
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')

        self.assertEqual(len(sb.dag()['nodes']), 3)
        self.assertIn('the line in force for this close', spec)
        self.assertIn(f'**{self.DEBT.shown(self.DEBT.at)}**', spec)
        self.assertIn(f'(the `{self.DEBT.line}` knob', spec)
        self.assertIn(f'over the line {self.DEBT.shown(self.DEBT.at)} in force for this close',
                      action['note'])
        self.assertIn(f'(the `{self.DEBT.line}` knob)', action['note'])

    def test_the_number_exactly_on_the_line_is_over_it(self):
        """The line is the last acceptable number: the moment the number lands exactly on it, the
        debt is already at its limit."""
        sb = coding_line(self, self.DEBT, value=self.DEBT.exact)

        sb.close_round(NODE)

        self.assertEqual(len(sb.dag()['nodes']), 3)

    def test_the_next_dispatch_picks_the_inserted_node(self):
        """Keep walking after the insert: the next dispatch picks the debt node by the usual ranking
        and writes its prompt, while the deferred node still waits.

        This is the other half of the split the insertion made: the graph grew a node, and WHICH node
        runs next is a decision of the picking rules -- not of whoever inserted it.
        """
        sb = coding_line(self, self.DEBT, value=self.DEBT.over)
        inserted = sb.close_round(NODE)
        made = inserted['node_id']

        again = sb.next()

        self.assertEqual(again['action'], 'dispatch')
        self.assertEqual(again['node_id'], made)
        self.assertEqual(sb.holding_ids(), [made], 'the pick is what pins it')
        self.assertIn(made, sb.prompt_of(again))
        self.assertTrue(Path(again['prompt']).is_file())
        self.assertEqual(sb.dag()['nodes'][NODE2]['status'], 'todo')


@requires_plugin(TICKET.plugin)
class TestTicketChannel(ChannelCases, Base):
    """`plugins/test-audit`'s line, walked the way the core walks it."""
    DEBT = TICKET


@requires_plugin(POOL.plugin)
class TestPoolChannel(ChannelCases, Base):
    """`plugins/issue`'s line, walked the way the core walks it."""
    DEBT = POOL


class SwitchCases:
    """The plugin's switch: an ordinary word knob on the resolution chain, and never set = on.

    The switch is the declaration's own `enable_knob`, merged into the one knob table and enforced by
    `plugin_host` for every plugin alike -- so this class drives the same close through each layer of
    the chain, differing only in which layer states `off`.
    """

    DEBT = None

    def crossed_line(self, *, layer=None, value=None):
        """A two-node coding line: the line is configured, the debt is over it, and the close can walk
        at any time.

        `layer` names where the switch is written: `project` (the default), `global`, `cycle` or
        `node`; `value=None` writes nothing at all (the never-set case).
        """
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', self.DEBT.line, self.DEBT.at)
        if value is not None:
            if layer == 'global':
                set_config(sb, self.DEBT.switch, value, '--global')
            elif layer == 'cycle':
                sb.set_policy('cyc', self.DEBT.switch, value)
            elif layer == 'node':
                set_config(sb, self.DEBT.switch, value, '--node', NODE)
            else:
                set_config(sb, self.DEBT.switch, value)
        sb.next()                                  # the dispatch for NODE
        self.DEBT.report(sb, NODE, self.DEBT.over)
        return sb

    def assert_inserted(self, sb, action):
        """It was inserted: the graph grew a node, the two edges are wired right, no hand moved."""
        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 3, 'the debt node was not inserted')
        made = action['node_id']
        self.assertNotIn(made, (NODE, NODE2))
        self.assertEqual(nodes[made]['dependencies'], [NODE])
        self.assertEqual(nodes[NODE2]['dependencies'], [made])
        self.assertEqual(action['action'], 'insert_node')
        self.assertEqual(sb.holding_ids(), [], 'an insertion pins nothing')

    def assert_not_inserted(self, sb, action):
        """Not inserted: the close walks its original pick as usual, and the graph grows nothing."""
        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(sb.current_node(), NODE2)
        self.assertEqual(sb.path_nodes(), [NODE])
        self.assertEqual(len(sb.dag()['nodes']), 2, 'nothing may be inserted')
        self.assertEqual(sb.dag()['nodes'][NODE2]['dependencies'], [NODE],
                         'the edge the plugin would have rewritten is untouched')

    def test_a_project_that_never_set_it_inserts_the_node(self):
        """A project that never set this knob: a crossed line still inserts -- that is the factory
        default in the declaration, and the behaviour since this plugin appeared."""
        sb = self.crossed_line()

        self.assertEqual(pipeline.resolve_knob(sb.root, self.DEBT.switch, cycle=sb.cycle(), node=NODE),
                         'on')
        self.assert_inserted(sb, sb.close_round(NODE))

    def test_the_knob_is_the_one_the_declaration_names(self):
        """The name, the kind and the factory value all come out of the declaration: there is no
        second copy of them on the reading side.

        `enable_knob` is what the host enforces, and the merged table is where the CLI, the form and
        the doctor read the same cell from -- so one assertion on the merged table plus one on the
        declaration covers every door.
        """
        spec = pipeline.knob_specs()[self.DEBT.switch]
        self.assertEqual(spec['kind'], 'switch')
        self.assertEqual(spec['default'], 'on')
        decl = next(plugin['decl'] for plugin in pipeline.registry().load_all()
                    if plugin['name'] == self.DEBT.plugin)
        self.assertEqual(decl['enable_knob'], self.DEBT.switch)
        self.assertEqual(decl['timing'], 'close')
        self.assertEqual(decl['action'], 'insert')

    def test_the_switch_off_keeps_a_crossed_line_from_inserting(self):
        """A project that turned it off: the number still crosses the line (it is in hand), but the
        close inserts no new node."""
        sb = self.crossed_line(value='off')

        # the crossing is on the record: the number is in hand, the line is in force, and both
        # are still in hand -- the switch shuts the judgement, not the measurement
        self.assertEqual(pipeline.resolve_knob(sb.root, self.DEBT.switch, cycle=sb.cycle(), node=NODE),
                         'off')
        self.assertEqual(close_offers(sb), [], 'the switch must shut the judgement')
        action = sb.close_round(NODE)

        self.assert_not_inserted(sb, action)

    def test_the_global_layer_can_turn_it_off_too(self):
        """The global layer counts too: it says `off`, the project says not a word, and this project
        inserts nothing."""
        sb = self.crossed_line(layer='global', value='off')

        self.assertEqual(pipeline.knob_sources(sb.root, cycle=sb.cycle())[self.DEBT.switch], 'global')
        self.assert_not_inserted(sb, sb.close_round(NODE))

    def test_the_cycle_layer_can_turn_it_off(self):
        """The line is a layer of its own: `cycle policy cyc <knob> off` shuts this line only, and the
        close reads it before the project layer."""
        sb = self.crossed_line(layer='cycle', value='off')

        sources = pipeline.knob_sources(sb.root, cycle=sb.cycle(), node=NODE)
        self.assertEqual(sources[self.DEBT.switch], 'cycle')
        self.assert_not_inserted(sb, sb.close_round(NODE))

    def test_the_node_layer_can_turn_it_off(self):
        """The narrowest layer wins: one node's own `config.json` says `off`, and that node's close
        does not insert even though every wider layer says on."""
        sb = self.crossed_line(layer='node', value='off')

        node_layer = sb.root / '.pf' / 'nodes' / NODE / 'config.json'
        self.assertEqual(json.loads(node_layer.read_text(encoding='utf-8')),
                         {self.DEBT.switch: 'off'})
        sources = pipeline.knob_sources(sb.root, cycle=sb.cycle(), node=NODE)
        self.assertEqual(sources[self.DEBT.switch], 'node')
        self.assert_not_inserted(sb, sb.close_round(NODE))

    def test_the_project_layer_beats_the_global_one(self):
        """The project layer beats the global one: the global layer is off, the project writes `on`,
        and the project wins."""
        sb = self.crossed_line(layer='global', value='off')
        set_config(sb, self.DEBT.switch, 'on')

        self.assertEqual(pipeline.resolve_knob(sb.root, self.DEBT.switch, cycle=sb.cycle(), node=NODE),
                         'on')
        self.assert_inserted(sb, sb.close_round(NODE))


@requires_plugin(TICKET.plugin)
class TestTicketSwitch(SwitchCases, Base):
    """`plugins/test-audit`'s switch, on every layer of the chain it may be written at."""
    DEBT = TICKET


@requires_plugin(POOL.plugin)
class TestPoolSwitch(SwitchCases, Base):
    """`plugins/issue`'s switch, on every layer of the chain it may be written at."""
    DEBT = POOL


class ThresholdCases:
    """The line behind the metric is a knob, and it resolves on the whole chain -- not just in the
    cycle's own `policies` slot."""

    DEBT = None

    def test_the_line_is_the_lines_own_policy(self):
        """The line is written onto the cycle's `policies`, and the instructions list gains not a
        word -- the ledger is for things somebody said."""
        sb = self.make()
        sb.add_node(NODE, 'Write code', mode='auto', tags=['coding'])
        sb.start_cycle(tags=['coding'])

        sb.set_policy('cyc', self.DEBT.line, self.DEBT.at)

        self.assertEqual(pipeline.resolve_knob(sb.root, self.DEBT.line, cycle=sb.cycle()),
                         self.DEBT.at)
        self.assertEqual(pipeline.knob_sources(sb.root, cycle=sb.cycle())[self.DEBT.line], 'cycle')
        self.assertEqual(sb.instruct_list(), [], 'the account is for things somebody said')

    def test_a_line_without_it_carries_none(self):
        """A line that never configured it does not carry this key at all: it resolves to nothing,
        and the plugin is simply inert for it."""
        sb = self.make()
        sb.add_node(NODE, 'Write code', mode='auto', tags=['coding'])
        sb.start_cycle(tags=['coding'])

        self.assertIsNone(pipeline.resolve_knob(sb.root, self.DEBT.line, cycle=sb.cycle()))
        self.assertIsNone(sb.cycle().get('policies'))

    def test_a_threshold_stated_on_the_project_counts_too(self):
        """The chain is the point: a threshold written on the project layer (or the machine's) is the
        one the close reads, with no line policy at all -- "in force for this close", not "in this
        line's policies"."""
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        set_config(sb, self.DEBT.line, self.DEBT.at)     # the project layer, not `cycle policy`
        sb.next()
        self.DEBT.report(sb, NODE, self.DEBT.over)

        sources = pipeline.knob_sources(sb.root, cycle=sb.cycle())
        self.assertEqual(sources[self.DEBT.line], 'project')
        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'insert_node')
        self.assertEqual(len(sb.dag()['nodes']), 3)
        self.assertIn(f'(the `{self.DEBT.line}` knob', action['note'])

    def test_a_wider_layer_is_overridden_by_the_line(self):
        """Narrow beats wide: the project states a much wider line, the line states its own, and the
        line's own is what judges."""
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        set_config(sb, self.DEBT.line, self.DEBT.at * 3)
        sb.set_policy('cyc', self.DEBT.line, self.DEBT.at)
        sb.next()
        self.DEBT.report(sb, NODE, self.DEBT.over)

        self.assertEqual(pipeline.resolve_knob(sb.root, self.DEBT.line, cycle=sb.cycle()),
                         self.DEBT.at)
        action = sb.close_round(NODE)              # over the line the close read

        self.assertEqual(action['action'], 'insert_node')
        self.assertIn(f'over the line {self.DEBT.shown(self.DEBT.at)}', action['note'])


@requires_plugin(TICKET.plugin)
class TestTicketThresholds(ThresholdCases, Base):
    """`plugins/test-audit`'s line, resolved on the whole chain."""
    DEBT = TICKET


@requires_plugin(POOL.plugin)
class TestPoolThresholds(ThresholdCases, Base):
    """`plugins/issue`'s line, resolved on the whole chain."""
    DEBT = POOL


class ScopeCases:
    """What the plugin reads, and from where: the ticket, the pool tool, and the code branch only."""

    DEBT = None

    def test_a_line_that_is_not_about_code_reads_the_number_and_does_nothing(self):
        """Another line reads the same number (both the line and the number are in hand) but does
        nothing -- these metrics belong to the code branch."""
        sb = self.make()
        sb.add_node(NODE, 'Write copy', mode='auto', tags=['writing'])
        sb.add_node(NODE2, 'Write copy 2', mode='auto', tags=['writing'], deps=[NODE])
        sb.start_cycle(tags=['writing'])
        sb.set_policy('cyc', self.DEBT.line, 1)
        sb.next()
        self.DEBT.report(sb, NODE, self.DEBT.over)

        # the number and the line are both in hand and the crossing counts: the reason for doing
        # nothing is that this line is not the code branch
        self.assertEqual(pipeline.resolve_knob(sb.root, self.DEBT.line, cycle=sb.cycle()), 1)
        self.assertEqual(close_offers(sb), [])

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 2)


@requires_plugin(TICKET.plugin)
class TestTicketScope(ScopeCases, Base):
    """What `plugins/test-audit` reads, and from where: the ticket, and nothing else."""

    DEBT = TICKET

    def test_the_test_lines_number_is_never_asked_of_a_child_process(self):
        """The test line's number is the one the session measured and wrote into its own note, so a
        crossing close starts no process of its own to compute it.

        The other leg of `TestPoolScope.test_the_pool_number_is_asked_of_the_tool_that_owns_the_pool`:
        `ctx.capture` is the one road to a child process, so what was asked says which number this
        plugin computes itself. The graph tool is the one child a crossing close may start -- the
        insertion reads back the node it just created -- and the pool's tool is never among them.
        """
        sb = coding_line(self, TICKET, value=TICKET.over)
        calls = []
        real = pipeline._capture

        def spy(argv, cwd):
            calls.append([str(a) for a in argv])
            return real(argv, cwd)

        with patched(pipeline, '_capture', spy):
            action = sb.close_round(NODE)

        asked = [' '.join(call) for call in calls]
        self.assertEqual(action['action'], 'insert_node', action)
        self.assertEqual(len(sb.dag()['nodes']), 3, 'the crossing did not insert')
        self.assertEqual([text for text in asked if ISSUE_MANAGE.name in text], [],
                         'the test line asked the pool tool for its number')
        self.assertEqual([text for text in asked if DAG_MANAGE.name not in text], [],
                         f'a child process was started for a number the note already carries: {asked}')


@requires_plugin(POOL.plugin)
class TestPoolScope(ScopeCases, Base):
    """What `plugins/issue` reads, and from where: the pool tool, out of a child process."""

    DEBT = POOL

    def test_the_pool_number_is_asked_of_the_tool_that_owns_the_pool(self):
        """The pool's number is asked of the tool that owns the pool, and it is not asked when the
        line is under its line: `ctx.capture` is the one road, so the child process count says which
        half ran."""
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.next()
        self.DEBT.report(sb, NODE, None)           # the ticket carries nothing, as always
        self.DEBT.repay(sb, self.DEBT.under)       # the pool is in hand, but no line is configured

        # nothing configured: the plugin contributes nothing and starts no child
        ticket = sb.read_signal('cyc', NODE)
        calls = []
        real = pipeline._capture

        def spy(argv, cwd):
            calls.append([str(a) for a in argv])
            return real(argv, cwd)

        with patched(pipeline, '_capture', spy):
            self.assertEqual(close_offers(sb, ticket=ticket), [])
        self.assertEqual([], [c for c in calls if 'issue_manager.py' in ' '.join(c)],
                         'the pool tool was asked with nothing configured')

        # configure the pool line, over the pool's own weight: now the tool is asked
        sb.set_policy('cyc', self.DEBT.line, 1)
        with patched(pipeline, '_capture', spy):
            offers = close_offers(sb, ticket=ticket)
        self.assertEqual(1, len(offers), offers)
        self.assertTrue([c for c in calls if 'issue_manager.py' in ' '.join(c)],
                        f'the pool number did not come from the pool tool: {calls}')

    def test_the_pool_number_is_never_asked_of_a_note(self):
        """Where the pool's number comes from is the tool, and the generated spec says so outright:
        the closing hint that asks a session to carry a `reading` slot names the ticket's metrics
        only, so the pool's spec carries no such slot."""
        sb = coding_line(self, POOL, value=POOL.over)

        action = sb.close_round(NODE)
        made = action['node_id']
        spec = (sb.root / '.pf' / 'nodes' / made / 'spec.md').read_text(encoding='utf-8')

        self.assertIn('`pf issue weight`', spec)
        self.assertIn('the note does not carry it', spec)
        self.assertNotIn('"reading": {"issue_pool_weight', spec)


class DedupCases:
    """One metric may hold only one unsettled node at a time: once somebody is already repaying the
    debt, no second one is inserted."""

    DEBT = None

    def test_a_metric_already_in_hand_is_not_inserted_twice(self):
        """While a debt node is still in play (`todo`), the same metric crossing again inserts nothing
        more -- the fixer is one."""
        sb = coding_line(self, self.DEBT, value=self.DEBT.over)
        first = sb.close_round(NODE)
        self.assertEqual(first['action'], 'insert_node')
        made = first['node_id']
        self.assertNotIn(made, (NODE, NODE2))
        self.assertEqual(sb.node(made)['status'], 'todo')

        # the next dispatch is what pins it; then its own close reports another over-the-line number:
        # no second insert, and the close walks on.
        sb.next()
        self.assertEqual(sb.holding_ids(), [made])
        self.DEBT.report(sb, made, self.DEBT.over)
        second = sb.close_round(made)
        self.assertEqual(second['action'], 'dispatch')
        self.assertEqual(len(sb.dag()['nodes']), 3, 'a second debt node was inserted')
        self.assertEqual(sb.current_node(), NODE2, 'the deferred pick still came next')

    def test_a_finished_debt_node_frees_the_metric_again(self):
        """The debt node is delivered (`completed`) with the number back under the line, and the debt
        crosses again: the next over-the-line number inserts a fresh node as usual."""
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.add_node(NODE3, 'Write code C', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', self.DEBT.line, self.DEBT.at)
        sb.next()
        self.DEBT.report(sb, NODE, self.DEBT.over)
        # walk one slot at a time (parallel pressed to 1 first): each close advances only its own node,
        # so the judgements after it are legible.
        sb.set_policy('cyc', 'parallel', 1)
        first = sb.close_round(NODE)
        made = first['node_id']
        self.assertNotIn(made, (NODE, NODE2))

        # the debt node ran and repaid the debt -> its work is delivered and this metric is
        # insertable again
        sb.next()                                  # the dispatch that pins what the insertion created
        self.assertEqual(sb.holding_ids(), [made])
        self.DEBT.report(sb, made, self.DEBT.under)
        sb.close_round(made)
        sb.set_current_node(NODE2)
        # further on, another node reports another over-the-line number: the debt stands, so it must
        # come in for one more pass
        self.DEBT.report(sb, NODE2, self.DEBT.over)
        sb.close_round(NODE2)
        self.assertEqual(len(sb.dag()['nodes']), 4,
                         'a repaid metric that crosses again must insert a fresh node')

    def test_the_insert_lands_after_the_node_is_closed(self):
        """The order of the graph edit: the old node is marked first, only then is the debt node
        created -- the dedup graph read must not count the node just delivered as still in play."""
        sb = coding_line(self, self.DEBT, value=self.DEBT.over)
        sb.close_round(NODE)
        self.assertEqual(sb.node(NODE)['status'], 'completed')
        self.assertEqual(sb.path_nodes(), [NODE])

    def test_only_a_todo_node_holds_the_metric(self):
        """The dedup read is "still open", and `shelved` is not: a human who never revives one would
        leave the plugin mute forever if it counted."""
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.add_node('dddddddd-4444-4444-8444-dddddddddddd', 'an old debt pass',
                    status='shelved', tags=[self.DEBT.tag])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', self.DEBT.line, self.DEBT.at)
        sb.next()
        self.DEBT.report(sb, NODE, self.DEBT.over)

        action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'insert_node',
                         'a shelved debt node must not hold the metric')
        self.assertEqual(len(sb.dag()['nodes']), 4)

    def test_the_switch_has_no_second_table_of_its_own(self):
        """The writing side keeps no copy of this plugin's switch: the name reaches every door through
        the one merged table.

        A missing key used to be a `KeyError` in a copied spec table; the door that used to copy it
        (`CONFIG_SPEC`) is gone with the old config module, and `knob_specs()` is the one source.
        """
        written = _fresh_module('state_manage_no_config_spec', STATE_MANAGE)
        self.assertFalse(hasattr(written, 'CONFIG_SPEC'),
                         'the state package has grown a second settings table again')
        self.assertEqual(pipeline.knob_specs()[self.DEBT.switch]['kind'], 'switch')


@requires_plugin(TICKET.plugin)
class TestTicketDedup(DedupCases, Base):
    """`plugins/test-audit`: one live node per metric."""
    DEBT = TICKET


@requires_plugin(POOL.plugin)
class TestPoolDedup(DedupCases, Base):
    """`plugins/issue`: one live node per metric."""
    DEBT = POOL


# ═══════════════════════════════════════════════════════════════════════════════
#  The two plugins in one close, and one of the two folders gone
# ═══════════════════════════════════════════════════════════════════════════════

@contextlib.contextmanager
def plugins_only(name):
    """The shipped plugins tree with only `name`'s folder in it: one plugin installed, one gone.

    The folder is COPIED into a throwaway directory and the scan is pointed there
    (`PF_PLUGINS_DIR`), because "the folder was deleted" is a state of THIS tree and the suite must
    not delete the tree's own folder to produce it. What the copy lands in is shaped like the
    checkout it came from: `<tmp>/plugins/<name>/` is the folder, and the checkout's own
    `tools/shared/` is copied beside it, because a plugin's tool walks up from its own place for the
    shared libraries (`shared/loader.py`) -- a plugin folder is a part of a checkout, and a fixture
    that handed it those libraries some other way would no longer be running the folder this
    checkout ships.
    """
    source = PLUGINS_DIR / name
    assert source.is_dir(), f'{source} is not the shipped plugin folder'
    with tempfile.TemporaryDirectory(prefix='conduct-plugins-one-') as tmp:
        base = Path(tmp)
        shutil.copytree(source, base / 'plugins' / name,
                        ignore=shutil.ignore_patterns('__pycache__'))
        shared = base / 'tools' / 'shared'
        shared.mkdir(parents=True)
        for path in sorted((TOOLS_DIR / 'shared').glob('*.py')):
            shutil.copyfile(path, shared / path.name)
        with plugins_dir(base / 'plugins'):
            yield base / 'plugins' / name


class TestTheTwoLinesInOneClose(Base):
    """Both plugins at one close: each metric's crossing lands its own node, and neither needs the
    other.

    The rule that changed when the two debts moved into two folders: a close asks every installed
    plugin, so a close that crosses two lines puts TWO nodes into the graph -- and one that crosses a
    single line puts one. The two insertions are siblings, not a chain: each is placed against the
    same pair (the node just closed, and the node the close had picked), so both are ready together
    and the ranking decides who runs first.

    What this class needs is the shipped pair, and only this class needs both folders. `one_folder`
    is the other state that matters -- one plugin gone, the other still answering -- and it is the
    completion criterion the other way round: with a plugin deleted, the line still closes, still
    dispatches, and what is left still inserts.
    """

    def both_lines(self, *, ticket=None, pool=None):
        """A coding line with both lines configured (or one of them), and both debts laid out.

        Each number is produced where that metric really keeps it: the ticket's in the head's own
        note, the pool's in the store the issue tool reads. `None` leaves that debt under its line.
        """
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', TICKET.line, TICKET.at)
        sb.set_policy('cyc', POOL.line, POOL.at)
        sb.next()                                  # the dispatch for NODE
        POOL.repay(sb, pool)
        TICKET.report(sb, NODE, ticket)
        return sb

    def landed(self, sb, action):
        """The nodes this close put into the graph, in the order the action reports them."""
        made = [uid for uid in sb.dag()['nodes'] if uid not in (NODE, NODE2)]
        self.assertEqual(len(made), len(action.get('insertions') or []),
                         f'the action and the graph disagree about what landed: {action}')
        return made

    @requires_plugin(TICKET.plugin, POOL.plugin)
    def test_a_close_that_crosses_both_lines_lands_two_nodes(self):
        """Both lines are crossed: this one close puts one node in for each -- the behaviour that
        changed when the two debts left the one folder they used to share."""
        sb = self.both_lines(ticket=TICKET.over, pool=POOL.over)

        action = sb.close_round(NODE)

        nodes = sb.dag()['nodes']
        self.assertEqual(action['action'], 'insert_node')
        made = self.landed(sb, action)
        self.assertEqual(2, len(action['insertions']),
                         f'one plugin per crossing: {action.get("insertions")}')
        self.assertEqual(2, len(made), f'two crossings must land two nodes: {sorted(nodes)}')
        # each is a direct dependent of the node that was closed, so both are ready together
        self.assertEqual([[NODE], [NODE]], sorted(nodes[uid]['dependencies'] for uid in made))
        # each node carries its OWN debt's tag and the other one's tag is not on it: two tags means
        # two different nodes, not one node wearing both and one wearing none
        debts = (TICKET, POOL)
        by_metric = {debt.reading: debt for debt in debts}
        owner = {}                                 # metric -> the one node carrying its tag
        for uid in made:
            own = [debt for debt in debts if debt.tag in nodes[uid]['tags']]
            self.assertEqual(1, len(own),
                             f'{uid} must carry exactly one debt tag, has {nodes[uid]["tags"]}')
            self.assertNotIn(own[0].reading, owner,
                             f'two nodes carry the same debt tag: {uid} and '
                             f'{owner.get(own[0].reading)}')
            owner[own[0].reading] = uid
        self.assertEqual(set(by_metric), set(owner),
                         f'one node per debt tag, both debts here: {owner}')
        # the action's two insertions pair with those two nodes BY METRIC: the reading a plugin
        # judged is the reading whose node it made, and its tags are that reading's tag alone
        named = {}
        for item in action['insertions']:
            metric = item['fields']['metric']
            self.assertNotIn(metric, named, f'one insertion per metric: {action["insertions"]}')
            named[metric] = item
        self.assertEqual(set(by_metric), set(named),
                         f'the insertions name other metrics than the nodes carry: {named}')
        for metric, item in named.items():
            debt = by_metric[metric]
            self.assertEqual([debt.tag], [tag for tag in item['tags'] if tag in (TICKET.tag, POOL.tag)],
                             f'{metric}: the insertion wears {item["tags"]}')
            self.assertEqual(owner[metric], item['node_id'],
                             f'{metric}: the insertion names a node its tag does not sit on')
        # and each node's own text points at its own method only: a session for one debt is never
        # handed the sibling's skill, and the two nodes do not read as the same debt
        for debt in debts:
            uid = owner[debt.reading]
            spec = (sb.root / '.pf' / 'nodes' / uid / 'spec.md').read_text(encoding='utf-8')
            other = POOL if debt is TICKET else TICKET
            self.assertIn(f'method: {debt.method}', spec)
            self.assertNotIn(other.method, spec,
                             f'{debt.reading}: the spec names the sibling method')
            self.assertIn(f'Maintenance: {debt.subject}', nodes[uid]['title'])
            self.assertNotIn(other.subject, nodes[uid]['title'])
        # the close's own pick was deferred behind the new ones: it no longer sits on the node just
        # closed, and every dependency it has is one of them (the edit that placed the second node
        # read the graph before the first landed, so the old direct edge is what it finds to drop)
        self.assertNotIn(NODE, nodes[NODE2]['dependencies'])
        self.assertEqual(sorted(made), sorted(nodes[NODE2]['dependencies']))
        # an insertion is a graph edit and nothing else, twice over
        self.assertEqual([], sb.holding_ids(), 'an insertion pins nothing')
        self.assertEqual([NODE], sb.path_nodes())
        self.assertEqual('completed', nodes[NODE]['status'])
        self.assertEqual('todo', nodes[NODE2]['status'])
        self.assertEqual({'todo'}, {nodes[uid]['status'] for uid in made})

    @requires_plugin(TICKET.plugin, POOL.plugin)
    def test_only_the_test_lines_crossing_lands_one_node(self):
        """One crossed line, one node: the ticket's debt is over and the pool's is not, so the close
        lands exactly what crossed."""
        sb = self.both_lines(ticket=TICKET.over, pool=POOL.under)

        action = sb.close_round(NODE)

        nodes = sb.dag()['nodes']
        made = [uid for uid in nodes if uid not in (NODE, NODE2)]
        self.assertEqual(action['action'], 'insert_node')
        self.assertEqual(1, len(made), f'only one line crossed: {action}')
        self.assertIn(TICKET.tag, nodes[made[0]]['tags'])
        self.assertNotIn(POOL.tag, nodes[made[0]]['tags'])

    @requires_plugin(TICKET.plugin, POOL.plugin)
    def test_only_the_pools_crossing_lands_one_node(self):
        """The same the other way round: the pool's debt is over the line the tool computes against,
        the ticket's is under its own, and one node lands."""
        sb = self.both_lines(ticket=TICKET.under, pool=POOL.over)

        action = sb.close_round(NODE)

        nodes = sb.dag()['nodes']
        made = [uid for uid in nodes if uid not in (NODE, NODE2)]
        self.assertEqual(action['action'], 'insert_node')
        self.assertEqual(1, len(made), f'only one line crossed: {action}')
        self.assertIn(POOL.tag, nodes[made[0]]['tags'])
        self.assertNotIn(TICKET.tag, nodes[made[0]]['tags'])

    def one_folder(self, kept):
        """One plugin installed and the other gone, with BOTH debts over their lines.

        Both numbers are really in hand: the kept metric's where its own declaration reads it, and the
        gone one's in ITS place (a store has no policy key, and the declaration that would have
        validated one is not installed). What the gone plugin would have said is the point of the
        case, so its debt is left standing.
        """
        sb = self.make()
        sb.add_node(NODE, 'Write code A', mode='auto', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', mode='auto', tags=['coding'], deps=[NODE])
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', kept.line, kept.at)
        sb.next()                                  # the dispatch for NODE
        POOL.repay(sb, POOL.over)
        TICKET.report(sb, NODE, TICKET.over)
        return sb

    def walked_on(self, kept, gone):
        """Start with one of the two folders gone: the surviving plugin inserts, and the line goes on.

        The close settles its node and the next dispatch picks what the insertion put there, exactly
        as it does with both installed -- that is what "deleting a folder is a supported state"
        means. The gone plugin contributes nothing at all.
        """
        sb = self.one_folder(kept)

        action = sb.close_round(NODE)

        nodes = sb.dag()['nodes']
        self.assertEqual(action['action'], 'insert_node')
        made = [uid for uid in nodes if uid not in (NODE, NODE2)]
        self.assertEqual(1, len(made),
                         f'only the installed plugin may insert: {action}')
        self.assertIn(kept.tag, nodes[made[0]]['tags'])
        self.assertNotIn(gone.tag, nodes[made[0]]['tags'])
        self.assertEqual(nodes[made[0]]['dependencies'], [NODE])
        self.assertEqual(nodes[NODE2]['dependencies'], [made[0]])
        self.assertEqual([NODE], sb.path_nodes(), 'the close still settled the node it closed')
        self.assertEqual('completed', nodes[NODE]['status'])
        self.assertEqual([], sb.holding_ids())

        again = sb.next()

        self.assertEqual(again['action'], 'dispatch', 'the line stopped where it always went on')
        self.assertEqual(again['node_id'], made[0])
        self.assertTrue(Path(again['prompt']).is_file())

    @requires_plugin(TICKET.plugin)
    def test_with_the_test_line_installed_and_its_partner_gone(self):
        """`plugins/issue` deleted: the test line still inserts, the close still settles, and the
        dispatch still picks the inserted node."""
        with plugins_only(TICKET.plugin):
            self.walked_on(TICKET, POOL)

    @requires_plugin(POOL.plugin)
    def test_with_the_pool_line_installed_and_its_partner_gone(self):
        """`plugins/test-audit` deleted: the pool line still inserts, and the line goes on."""
        with plugins_only(POOL.plugin):
            self.walked_on(POOL, TICKET)