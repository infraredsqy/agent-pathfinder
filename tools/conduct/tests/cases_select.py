"""Which node a line picks: where the line starts, the filter set it reads, and the weight.

The whole `cycle start` / `cycle select` area: how the start is fixed, how far the filter set
reaches, how the weight is combined -- all of it is read at the pick. The closing end (how one node
counts as done, how it is closed out) lives in `cases_node.py` in this same directory.

    python -m unittest conduct.tests.cases_select        # this file on its own
"""

from __future__ import annotations

import importlib.util as importlib_util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import NODE, NODE2, Base, added, pipeline  # noqa: E402
from harness import DAG_MANAGE, STATE_MANAGE, _fresh_module, finish_node, run_dag  # noqa: E402


def _state_cycles_module():
    """`state` is a package with relative imports, so it cannot be loaded as a single file: follow
    state-manage.py's own bootstrap and load the whole package by path, then take the loaded cycles
    submodule. Clear stale entries under the same name first, so this file's two loads stay apart.
    """
    name = 'pf_state_for_vocab'
    for stale in [n for n in sys.modules if n == name or n.startswith(name + '.')]:
        del sys.modules[stale]
    pkg_dir = STATE_MANAGE.parent / 'state'
    spec = importlib_util.spec_from_file_location(
        name, pkg_dir / '__init__.py', submodule_search_locations=[str(pkg_dir)])
    mod = importlib_util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod.cycles


# ═══════════════════════════════════════════════════════════════════════════════
#  The start, the filter set, the weight: what the pick reads
# ═══════════════════════════════════════════════════════════════════════════════

class TestStartCycle(Base):
    def test_start_cycle_pins_the_node_the_rule_picks(self):
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()

        self.assertEqual(sb.current_node(), NODE)
        self.assertEqual(sb.cycle()['status'], 'active')

    def test_start_cycle_refuses_an_empty_graph(self):
        sb = self.make()
        proc = sb.start_cycle(check=False)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('ready', proc.stderr)
        self.assertEqual((sb.state() or {'cycles': []})['cycles'], [])

    def test_a_cycle_with_no_node_in_hand_takes_the_top_ready_row(self):
        """Several ready rows do not stop the line: the ranking decides alone at every close
        (`pick_node` takes the top row), and the line's first pick takes the same row. Naming one is
        a human's move (`--node`, `cycle hold`), not a question asked here."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.add_node(NODE2, 'another line', mode='auto')
        sb.create_cycle(cycle_id='cyc')

        action = sb.plan()
        self.assertEqual(action['action'], 'open_node')
        self.assertEqual(action['node_id'], NODE)


# ── the selection layer and tag filtering (cases_tags / cases_weight were merged in) ────────────

class TestNodeTags(Base):
    """A node tag + cycle split: which nodes a cycle picks its people from.

    There is one criterion -- a node carries no tag, or its tag intersects the cycle's filter set,
    and it is in this line's range. The filter falls only on the "pick the next node" step: a node
    already running runs to the end, and the DAG's dependencies do not change one character
    (blocking only affects who is picked: it moves no status, marks nothing failed, touches no
    dependency edge). When this line cannot pick and another line has someone ready, it stops and
    hands "should the other line start" back to a human.

    With no filter set (a cycle carrying no tags) these cases must not change the pick at all -- the
    whole suite guards that, and `test_an_untagged_cycle_picks_what_it_always_picked` pins it once
    more. That shape now has only one entry left, the records already on disk: `cycle start` /
    `cycle create` always demand `--tags` for a new call.
    """

    K = 'dddddddd-4444-4444-8444-dddddddddddd'          # coding
    W = 'eeeeeeee-5555-4555-8555-eeeeeeeeeeee'          # writing
    P = 'ffffffff-6666-4666-8666-ffffffffffff'          # no tag

    def code_and_words(self, cli=False):
        """One coding job + one writing job, both `auto` (otherwise the stop rule fires first and
        the tag never comes up).

        `W` depends on `K`, so `K` is the one that matters most in the graph: who a line picks first
        follows the DAG ranking, and this fixes it here.
        """
        sb = self.make(cli=cli)
        sb.add_node(self.K, 'Write code', mode='auto', tags=['coding'])
        sb.add_node(self.W, 'Write copy', mode='auto', tags=['writing'], deps=[self.K])
        return sb

    # ── criterion table, first row: the filter set has a ready node -> take the DAG's top row ────

    def test_the_filter_set_only_drops_rows_from_the_dag_ranking(self):
        """The filter set only deletes rows; the ranking is still the one implementation in
        `dag-manage`.

        The graph's top row carries `writing`, and this line's filter set is `coding` -- **that** is
        the row deleted, and the one picked is the filter set's own top row. If conduct_pipeline grew
        a second priority rule, this assertion would pick another node (the ranking is allowed one
        implementation, the rule `ranked_ready_nodes` has always kept).
        """
        sb = self.make()
        sb.add_node(self.W, 'Write copy', mode='auto', tags=['writing'])
        sb.add_node('aaaa1111-6666-4666-8666-ffffffffffff', "the copy's follow-up", mode='auto',
                    deps=[self.W])              # make W the one that matters most (someone waits on it)
        sb.add_node(self.K, 'Write code', mode='auto', tags=['coding'])
        sb.add_node(self.P, 'the untagged job', mode='auto')

        sb.start_cycle(tags=['coding'])
        self.assertEqual(sb.current_node(), self.K)
        self.assertEqual(sb.cycle()['tags'], ['coding'])

    def test_an_untagged_node_stays_in_every_filter_set(self):
        """A node with no tag stays in range under any filter set.

        The conservative direction: letting one through runs one extra node, while blocking wrongly
        leaves a line's work with nobody to do it forever. So this line's filter set is `art` (no
        node in the graph carries `art`), and the one picked is still the untagged node.
        """
        sb = self.make()
        sb.add_node(self.K, 'Write code', mode='auto', tags=['coding'])
        plain = sb.add_node(self.P, 'the untagged job', mode='auto')
        sb.start_cycle(tags=['art'])

        self.assertEqual(sb.current_node(), plain)
        # `exclude` is the node this step's own cycle is holding: it belongs to the caller, and
        # every other row is still judged by the filter set alone.
        self.assertEqual([n['id'] for n in pipeline.ranked_ready_nodes(sb.root, tags=['art'],
                                                                   exclude=plain)],
                         [])
        self.assertEqual([n['id'] for n in pipeline.tag_blocked_ready_nodes(sb.root, ['art'])],
                         [self.K])

    # ── criterion table, second row: none for this line, some for another -> stop and say it ─────

    def test_a_cross_line_stop_names_the_nodes_and_the_command(self):
        """When another line has someone ready it stops and the reason names them: which nodes are
        ready, which tags block them, the whole command that opens the other line.

        This reason is read to the user as it stands, so one person reading it has to know what to
        type next. The reason it stops here is to hand the choice back to a human: this line's work
        is done, and whether another line starts, and when, is his to say.
        """
        sb = self.code_and_words()
        sb.start_cycle(tags=['coding'])
        node = sb.current_node()
        self.assertEqual(node, self.K)

        waiting = finish_node(sb)
        closed = sb.close_round(waiting['node_id'])

        self.assertEqual(closed['action'], 'stop_pipeline')
        reason = closed['reason']
        self.assertIn('Write copy', reason)                 # which node is ready
        self.assertIn(self.W, reason)
        self.assertIn('writing', reason)                    # blocked by which tag
        self.assertIn('cycle start', reason)                # how the other line is opened
        self.assertIn('--tags writing', reason)
        self.assertNotIn('--launch-mode', reason, 'the deleted lever is back in the suggested command')
        self.assertNotIn('the graph has no ready node left', reason)

        # Being blocked is not the same as an unmet dependency: no status, no dependency edge and
        # not one shelved node moved.
        self.assertEqual(sb.dag_status(self.W), 'todo')
        self.assertEqual(sb.node(self.W)['dependencies'], [self.K])
        # Stopping is one sentence: inactive. This line's work is done and the other line still has
        # someone waiting to start -- and "there is work left" is not written into a line's status
        # (a graph grows, and whether a stopped line should run on is a human's call), so this stop
        # is inactive too, and the way onto that line is the command the reason gives, typed for
        # real.
        self.assertEqual(sb.cycle()['status'], 'inactive')
        # The way onto that line, the command the reason gives, typed for real: with no --force it
        # has to work. `check=True` means a refusal raises right there, so the two assertions below
        # holding means this one passed.
        sb.start_cycle('other', title='another line', tags=['writing'])
        self.assertEqual(sb.cycle('other')['status'], 'active')
        self.assertEqual(sb.cycle('other')['tags'], ['writing'])
        self.assertEqual(waiting['node_id'], node)

    def test_the_second_pick_path_says_the_same_thing(self):
        """The filter only deletes rows: after the close, the same state read through the two entries
        gives the same filtered result.

        This line recognizes only `coding` from start to finish (the untagged job is in range too, so
        it is picked first); after the close one node tagged `coding` is still on disk -- in range
        and still ready, and that is the one the second entry has to pick.
        """
        sb = self.make()
        sb.add_node(self.K, 'Write code', mode='auto', tags=['coding'])
        sb.add_node(self.P, 'the untagged job', mode='auto')
        for i in range(3):
            sb.add_node('aaaa000%d-6666-4666-8666-ffffffffffff' % i, f'the {i}th waiting on it',
                        mode='auto', deps=[self.P])
        pinned = sb.start_cycle(tags=['coding'], check=False)

        self.assertEqual(pinned.returncode, 0, pinned.stderr)
        self.assertEqual(sb.current_node(), self.P, 'the untagged job is the one that matters most')
        sb.write_signal('cyc', self.P, 'completed', 'done')
        sb.close_round(self.P)

        # the second entry reads the same graph and the same filter
        self.assertEqual(sb.current_node(), self.K, 'the ready one on this line is that coding node')
        self.assertEqual([n['id'] for n in pipeline.tag_blocked_ready_nodes(sb.root, ['coding'])], [])
        self.assertEqual(sb.dag_status(self.P), 'completed')
        self.assertEqual(sb.dag_status(self.K), 'todo', 'being picked is not being run')

    def test_nothing_ready_anywhere_keeps_the_old_sentence(self):
        """With no filter set blocking anyone, the stop still says "the graph has no ready node
        left"."""
        sb = self.make()
        sb.add_node(self.K, 'Write code', mode='auto')
        sb.create_cycle(cycle_id='cyc')
        sb.set_current_node(self.K)
        sb.write_signal('cyc', self.K, 'completed', 'done')
        action = sb.close_round(self.K)

        self.assertEqual(action['action'], 'stop_pipeline')
        self.assertIn('the graph has no ready node left', action['reason'])
        self.assertNotIn('tag', action['reason'])
        self.assertEqual(sb.cycle()['status'], 'inactive')

    def test_an_untagged_cycle_picks_what_it_always_picked(self):
        """An empty filter set makes a cycle byte-for-byte what it was before this plugin: the
        whole graph is in range.

        This branch now has only one entry -- **the records already on disk**: `cycle start` /
        `cycle create` always demand `--tags` for a new call, so here the tagless line is written to
        disk in the old record's shape and then driven as usual. The empty set is not dead code: an
        old cycle pulled down on a machine looks like this, and its effect on the pick must still be
        zero.
        """
        sb = self.make()
        sb.add_node(self.W, 'Write copy', mode='auto', tags=['writing'])
        sb.add_node(self.K, 'Write code', mode='auto', tags=['coding'])
        sb.start_cycle(tags=['coding'])
        # The old record's shape: this line carries no tags (the key does not appear at all, it is
        # not an empty list) and is pinned on W right now. In an old record, whoever is pinned is
        # it, regardless of any tag it carries.
        state = sb.state()
        state['cycles'][0].pop('tags', None)
        state['cycles'][0]['holding'] = [{'id': self.W, 'since': '2026-10-01T00:00:00Z'}]
        sb.rewrite_state(state)

        self.assertNotIn('tags', sb.cycle(), 'the fixture wrote the key back')
        self.assertEqual(sb.current_node(), self.W)
        self.assertEqual([n['id'] for n in pipeline.tag_blocked_ready_nodes(sb.root, None)], [])
        # With a filter set both rows would still be in range here (neither is in the other's set),
        # and with none, the whole graph is: the only row out of reach is the one already in hand.
        self.assertEqual({n['id'] for n in pipeline.ranked_ready_nodes(sb.root, tags=None)},
                         {self.K})

    def test_a_running_node_is_never_interrupted_by_the_filter_set(self):
        """The filter is read only at the pick: this line's node already in hand runs to the end,
        whatever tag it carries."""
        sb = self.make()
        sb.add_node(self.K, 'Write code', mode='auto', tags=['coding'])
        sb.start_cycle(tags=['coding'])
        self.assertEqual(sb.current_node(), self.K)

        # widening or narrowing the filter set must not move the node already in hand
        sb.rewrite_cycle(tags=['writing'])
        self.assertEqual(sb.current_node(), self.K)
        self.assertEqual(sb.plan()['node_id'], self.K)

    def test_a_named_node_outside_the_filter_set_runs_and_says_so(self):
        """`--node` is a name the owner typed by hand, and the filter set does not reach it -- but it
        has to say so, so nobody thinks the filter set failed to apply."""
        sb = self.code_and_words()
        sb.start_cycle(tags=['coding'])
        sb.write_signal('cyc', self.K, 'completed', 'done')
        action = sb.close_round(self.K, node=self.W)

        self.assertEqual(sb.current_node(), self.W)
        self.assertIn("is not in this cycle's filter set", action.get('note', ''),
                      f'a hand-named node outside the filter must be said out loud: {action}')

    def test_a_named_node_that_is_not_ready_is_refused(self):
        """Naming a node nobody can run: it stops and writes the named id into the reason, instead of
        quietly running another node."""
        sb = self.make()
        sb.add_node(self.K, 'Write code', mode='auto', tags=['coding'])
        absent = 'absent-9999-4999-8999-999999999999'
        sb.start_cycle(tags=['coding'])
        sb.write_signal('cyc', self.K, 'completed', 'done')
        stopped = sb.close_round(self.K, node=absent)

        self.assertEqual(stopped['action'], 'stop_pipeline')
        self.assertIn(absent, stopped['reason'])
        self.assertIsNone(sb.current_node())

    def test_the_cycle_keeps_its_filter_set_for_every_later_step(self):
        """The filter set belongs to **this line** and travels with it to the last step: every later
        slot picks by it."""
        sb = self.make()
        a = sb.add_node(self.K, 'Write code A', mode='auto', tags=['coding'])
        b = sb.add_node('aaaa2222-6666-4666-8666-ffffffffffff', 'Write code B', mode='auto',
                        tags=['coding'])
        sb.add_node(self.W, 'Write copy', mode='auto', tags=['writing'])
        sb.start_cycle(tags=['coding'])
        terminal, driver = self.run_to_stop(sb)

        picked = [node for node, _ in driver.dispatched]
        self.assertEqual(picked, [a, b])
        self.assertEqual(terminal['action'], 'stop_pipeline')
        self.assertIn(self.W, terminal['reason'])
        self.assertEqual(sb.cycle()['tags'], ['coding'])

    def test_start_cycle_tags_reach_the_pick_and_the_record(self):
        """`--tags` has to reach the pick and stay in the cycle record.

        The pick happens before the cycle is built, so landing the filter set only in the record
        would be too late: the first step would pick another line's node, and every later slot would
        inherit a line whose first node came from nowhere.
        """
        sb = self.make(cli=True)
        sb.add_node(self.W, 'Write copy', mode='auto', tags=['writing'])
        sb.add_node(self.K, 'Write code', mode='auto', tags=['coding'])

        sb.start_cycle(tags=['coding'])

        self.assertEqual(sb.current_node(), self.K)
        self.assertEqual(sb.cycle()['tags'], ['coding'])

    def test_start_cycle_refuses_when_the_filter_set_matches_nothing(self):
        """When the filter set holds no ready node at all, the refusal has to say plainly "there is
        work in the graph, it is just not this line's to pick"."""
        sb = self.make(cli=True)
        sb.add_node(self.W, 'Write copy', mode='auto', tags=['writing'])
        sb.add_node(self.K, 'Write code', mode='auto', tags=['coding'])

        proc = sb._tool('cycle', 'start', 'img', '--tags', 'art', check=False, cli=True)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('art', proc.stderr)
        self.assertIn(self.W, proc.stderr)
        self.assertIn(self.K, proc.stderr)
        self.assertEqual((sb.state() or {'cycles': []})['cycles'], [])

    def test_create_cycle_keeps_a_lone_tag_whole(self):
        """`cycle create --tags art` records `['art']`.

        Splitting the string by character would give `['a','r','t']`: the filter set would then hold
        the three words `a`, `r` and `t`, no node in the graph matches, and the record would not show
        it -- this line picks nothing, for a reason written where nobody can see it. Both record
        entries (`cycle create` and `cycle start`) have to land on the same list.
        """
        sb = self.make()
        sb.add_node(self.W, 'Write copy', mode='auto', tags=['writing'])

        sb._tool('cycle', 'create', 'img', '--tags', 'art', '--force', cli=True)

        self.assertEqual(sb.cycle('img')['tags'], ['art'])

    # ── tagging nodes in the graph: dag-manage's half ─────────────────────────────────────────────

    def test_dag_manage_writes_reads_and_clears_node_tags(self):
        """`add --tags` / `update --tags` / `update --clear-tags` / `list --tags`, all through the
        real CLI.

        A tag outside the vocabulary is taken with one warning line -- the vocabulary is laid out in
        front of a human to look at, and the engine takes anything.
        """
        sb = self.make()

        def add(*args):
            return run_dag(sb.root, 'add', *args).stdout.splitlines()[0].split(': ')[1].strip()

        code = add('Write code', '--tags', 'coding')
        words = add('Write copy', '--tags', 'writing')
        plain = add('the untagged job')

        self.assertEqual(sb.node(code)['tags'], ['coding'])
        listed = {n['id'] for n in json.loads(run_dag(sb.root, 'list', '--tags', 'coding').stdout)}
        self.assertEqual(listed, {code, plain})            # the untagged one is visible everywhere

        unknown = run_dag(sb.root, 'list', '--tags', 'misc')
        self.assertIn('is not in the vocabulary', unknown.stderr)   # one warning line, on stderr
        self.assertEqual({n['id'] for n in json.loads(unknown.stdout)}, {plain})

        # A tag outside the vocabulary is taken anyway: when a real fourth kind of work turns up,
        # writing it in is enough to use it.
        fourth = run_dag(sb.root, 'add', 'the fourth kind of work', '--tags', 'misc')
        self.assertIn('is not in the vocabulary', fourth.stderr)
        fourth_id = fourth.stdout.splitlines()[0].split(': ')[1].strip()
        self.assertEqual(sb.node(fourth_id)['tags'], ['misc'])
        self.assertIn(fourth_id,
                      {n['id'] for n in json.loads(run_dag(sb.root, 'list', '--tags',
                                                           'misc').stdout)})

        run_dag(sb.root, 'update', words, '--tags', 'art')
        self.assertEqual(sb.node(words)['tags'], ['art'])
        run_dag(sb.root, 'update', words, '--clear-tags')
        self.assertNotIn('tags', sb.node(words))
        after = json.loads(run_dag(sb.root, 'list', '--tags', 'art').stdout)
        self.assertEqual({n['id'] for n in after}, {plain, words})

    def test_a_tag_filter_never_touches_the_graph(self):
        """Filtering is about "who is picked": `list --tags` moves no status, changes no dependency
        and writes no node off.

        Blocking a node = this slot does not pick it. It stays `todo`, stays ready, and its
        dependency edges are untouched.
        """
        sb = self.make()
        run_dag(sb.root, 'add', 'Write code', '--tags', 'coding')
        run_dag(sb.root, 'add', 'Write copy', '--tags', 'writing')
        before = sb.dag()
        run_dag(sb.root, 'list', '--tags', 'coding', '--ready', '-n', '0')
        self.assertEqual(sb.dag(), before)

    def test_both_tools_spell_the_tag_vocabulary_the_same_way(self):
        """The vocabulary is one agreement, laid out in two places: dag-manage (nodes) and
        state-manage (a cycle's range).

        If the two grew their own words, you would get "a node says `coding`, the filter set says
        `code`" -- a graph that looks fine, with a line's work quietly never picked. So these three
        words are compared by this one case: the constants each tool loads are compared by value, not
        anchored to how the source spells them (a rename or a line break must not raise a false
        alarm).
        """
        dag_mod = _fresh_module('dag_manage_for_vocab', DAG_MANAGE)
        cycles_mod = _state_cycles_module()

        self.assertEqual(dag_mod.VALID_TAGS, ('writing', 'coding', 'art'))
        self.assertEqual(dag_mod.VALID_TAGS, cycles_mod.VALID_TAGS)

    def test_the_borrowed_cycle_policy_tables_read_the_same_by_value(self):
        """impl borrows the one source in `state/cycles.py`, and the two packages each load the same
        file.

        What is pinned here is that boundary: the two readings are **equal by value** (`==`). Each
        package loads under its own name, so the two module objects differ, `is` does not hold across
        the packages and nobody should compare these two with `is`; to change this table, change
        `state/cycles.py` itself.
        """
        cycles_mod = _state_cycles_module()

        self.assertEqual(pipeline.POLICY_DEFAULTS, cycles_mod.POLICY_DEFAULTS)
        self.assertEqual(pipeline.POLICY_VALUES, cycles_mod.POLICY_VALUES)
        self.assertEqual(pipeline.POLICY_NUMBERS, cycles_mod.POLICY_NUMBERS)
        self.assertEqual(pipeline.POLICY_COUNTS, cycles_mod.POLICY_COUNTS)
        self.assertEqual(pipeline.POLICY_WORDS, cycles_mod.POLICY_WORDS)


# ═══════════════════════════════════════════════════════════════════════════════
#  Cycle selection
# ═══════════════════════════════════════════════════════════════════════════════

class TestCycleSelection(Base):
    def test_no_cycle_does_not_invent_one(self):
        sb = self.make()
        action = sb.plan()
        self.assertEqual(action['action'], 'no_cycle')
        self.assertFalse(action.get('commands'))
        self.assertFalse(sb.next().get('applied'))
        state = sb.state() or {'cycles': []}      # nothing was written, not even an empty state
        self.assertEqual(state['cycles'], [])

    def test_unknown_cycle_id_is_not_a_new_cycle(self):
        sb = self.make()
        sb.create_cycle('one')
        action = sb.plan_for('nope')
        self.assertEqual(action['action'], 'ask_user')
        self.assertEqual(action['requested'], 'nope')

    def test_two_active_cycles_asks(self):
        """The planner asks about two active cycles -- a state `cycle create` refuses to create
        without `--force`. Forged with `--force`, because the planner still has to answer for a
        machine that already has two, and `ask_user` is the answer: an unattended run cannot pick."""
        sb = self.make()
        sb.create_cycle('one')
        sb.create_cycle('two', force=True)
        action = sb.plan()
        self.assertEqual(action['action'], 'ask_user')
        self.assertEqual(sorted(action['candidates']), ['one', 'two'])

    def test_a_line_out_of_service_is_asked_about_not_silently_restarted(self):
        """Taking a line out of service is a human's decision to stop driving it; the planner hands
        back the one command that puts it back in, and drives nothing."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        sb.set_cycle_status('cyc', 'inactive')
        self.assertEqual(sb.cycle()['status'], 'inactive')

        action = sb.plan_for('cyc')
        self.assertEqual(action['action'], 'ask_user')
        self.assertIn('not active', action['question'])
        self.assertIn('cycle status cyc active', action['hint'])


class TestNodeWeight(Base):
    """`dag-manage --weight`'s half, and where it lands on the invocation line."""

    # ── the key in the graph ─────────────────────────────────────────────────────────────────────

    def test_only_light_lands_the_key(self):
        """`add --weight light` lands the key; leaving it out, or stating `standard` explicitly,
        leaves the record in today's shape.

        The storage shape is itself the compatibility policy: an old graph has no such key and a
        `standard` node has no such key, and the two read as the same thing.
        """
        sb = self.make()

        light = added(run_dag(sb.root, 'add', 'light work', '--weight', 'light'))
        plain = added(run_dag(sb.root, 'add', 'standard work'))
        stated = added(run_dag(sb.root, 'add', 'explicit standard', '--weight', 'standard'))

        self.assertEqual(sb.node(light)['weight'], 'light')
        self.assertNotIn('weight', sb.node(plain))
        self.assertNotIn('weight', sb.node(stated))

    def test_update_moves_the_key_both_ways(self):
        """`update --weight` covers both tiers: writing light lands the key, writing standard makes
        the key disappear from the record."""
        sb = self.make()
        node = added(run_dag(sb.root, 'add', 'work'))

        run_dag(sb.root, 'update', node, '--weight', 'light')
        self.assertEqual(sb.node(node)['weight'], 'light')

        run_dag(sb.root, 'update', node, '--weight', 'standard')
        self.assertNotIn('weight', sb.node(node))

    def test_list_and_info_echo_it_only_for_light(self):
        """Echoed the same way tags are: for a node that never wrote this key, `list` / `info` output
        is byte-for-byte what it is today."""
        sb = self.make()
        light = added(run_dag(sb.root, 'add', 'light work', '--weight', 'light'))
        plain = added(run_dag(sb.root, 'add', 'standard work'))

        listed = {n['id']: n for n in json.loads(run_dag(sb.root, 'list').stdout)}
        self.assertEqual(listed[light]['weight'], 'light')
        self.assertNotIn('weight', listed[plain])

        self.assertEqual(json.loads(run_dag(sb.root, 'info', light).stdout)['weight'], 'light')
        self.assertNotIn('weight', json.loads(run_dag(sb.root, 'info', plain).stdout))

    def test_the_word_outside_the_vocabulary_is_refused_at_the_command_line(self):
        """A word outside the vocabulary is stopped by argparse: the two tiers are a closed set, and
        a typo is reported right there instead of landing in the record to be read later."""
        sb = self.make()
        refused = run_dag(sb.root, 'add', 'work', '--weight', 'heavy', check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('invalid choice', refused.stderr)

    def test_an_unknown_word_on_disk_reads_as_standard(self):
        """The record says another word (hand-edited, added later, one an old machine does not know)
        -> the reader goes with `standard`.

        When a reading is uncertain, lean conservative: dispatch one more worker rather than have
        the conductor step in over a typo. The reading happens on the session's side (`info` output
        plus the rule in the skill), so this only checks that `info` does not echo it.
        """
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto', weight='misc')

        self.assertNotIn('weight', json.loads(run_dag(sb.root, 'info', NODE).stdout))

    # ── where it lands on the invocation line ────────────────────────────────────────────────────

    def invocation_line(self, sb) -> str:
        """The prompt's first line when this node is dispatched -- the runtime's criterion grows on
        this line."""
        sb.start_cycle()
        action = sb.next()
        self.assertEqual('dispatch', action['action'], action)
        return action['prompt_text'].splitlines()[0]

    def test_the_invocation_line_carries_no_weight_token(self):
        """The invocation line carries only the call itself and `[pipeline]`: weight and mode are the
        node's own attributes, and a session reads `info`."""
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto', weight='light')

        self.assertEqual(self.invocation_line(sb),
                         f'/pf-conduct {NODE} [pipeline]')
