"""The standing-instruction account: one append-only file, three scopes, two targets.

Nothing is injected into a dispatch prompt any more, so `instruction list` IS the instruction channel
-- a session runs it among its first commands and reads where it stands. Everything here is about
that account: what it keeps verbatim, which entries a reader can see from where, and what a human
can take back.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE, NODE2  # noqa: E402
from harness import run_state_manage  # noqa: E402

__all__ = ['TestInstructionScope', 'TestInstructionTargets', 'TestInstructionAuthority']


class TestInstructionScope(Base):
    """Three scopes, and the one judgement they carry: where an entry was recorded decides who can
    read it."""

    def with_cycle(self, count=1):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        if count > 1:
            sb.add_node(NODE2, '另一条支线', mode='auto')
        return sb

    def test_a_global_entry_is_readable_anywhere(self):
        sb = self.with_cycle()
        sb.instruct('全局的约束')

        visible = sb.instruct_visible_cli()
        self.assertEqual([i['text'] for i in visible], ['全局的约束'])

    def test_a_cycle_entry_is_readable_while_its_cycle_is_active(self):
        sb = self.with_cycle()
        sb.instruct('这条线里的约束', scope='cycle')

        entry = sb.instruct_list()[0]
        self.assertEqual(entry['scope'], 'cycle')
        self.assertEqual(entry['cycle'], 'cyc')
        self.assertEqual([i['text'] for i in sb.instruct_visible_cli()], ['这条线里的约束'])

        sb._tool('cycle', 'status', 'cyc', 'inactive')
        self.assertEqual(sb.instruct_visible_cli(), [])

    def test_a_node_entry_is_readable_while_its_node_is_unfinished(self):
        sb = self.with_cycle()
        sb.instruct('做这个节点时的约束', scope='node')

        entry = sb.instruct_list()[0]
        self.assertEqual(entry['scope'], 'node')
        self.assertEqual(entry['node'], NODE)
        self.assertEqual([i['text'] for i in sb.instruct_visible_cli()], ['做这个节点时的约束'])

        sb.write_signal('cyc', NODE, 'completed', 'done')
        sb.close_round(NODE)
        self.assertEqual(sb.instruct_visible_cli(), [],
                         'a node entry outlived the node it was recorded in')

    def test_a_node_entry_for_another_node_is_not_read_from_here(self):
        """An entry belongs to the node it was recorded in. Once that node is finished, the entry
        goes out of sight there too -- nothing to compare it against, nothing to read."""
        sb = self.with_cycle()
        sb.instruct('做这个节点时的约束', scope='node')
        sb.write_signal('cyc', NODE, 'completed', 'done')
        sb.close_round(NODE)

        self.assertEqual(sb.instruct_visible_cli(node=NODE), [])

    def test_a_node_entry_does_not_leak_to_the_node_that_comes_after(self):
        """The next node of the same line is a different place: the entry recorded in the previous
        node is not read there."""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '接下来', mode='auto', deps=[NODE])
        sb.start_cycle()
        sb.instruct('只属于第一个节点的', scope='node')

        self.assertEqual([i['text'] for i in sb.instruct_visible_cli()], ['只属于第一个节点的'])

        sb.write_signal('cyc', NODE, 'completed', 'done')
        sb.close_round(NODE)

        self.assertEqual(sb.current_node(), NODE2)
        self.assertEqual(sb.instruct_visible_cli(), [],
                         'the entry followed the line into a node it was never recorded in')

    def test_the_default_view_is_computed_from_where_the_pipeline_stands(self):
        """All three scopes at once, and the answer is the union of what is live from here."""
        sb = self.with_cycle()
        sb.instruct('全局的')
        sb.instruct('这条线的', scope='cycle')
        sb.instruct('这个节点的', scope='node')

        self.assertEqual({i['text'] for i in sb.instruct_visible_cli()},
                         {'全局的', '这条线的', '这个节点的'})

    def test_an_entry_with_nothing_to_bind_to_is_refused(self):
        """An unbound cycle/node entry would be read as visible everywhere, forever -- the one
        failure the scopes exist to prevent."""
        sb = self.make()
        for scope in ('cycle', 'node'):
            proc = sb._tool('instruction', 'add', '--text', 'x', '--scope', scope, check=False,
                            cli=True)
            self.assertNotEqual(proc.returncode, 0, scope)
            self.assertIn('bind', proc.stderr, scope)
        self.assertEqual(sb.instruct_list(), [])

    def test_a_line_can_be_named_by_hand_when_two_are_active(self):
        """Two lines side by side: the state cannot say which one is asking, so the caller names
        the line. The entry is then read from there and from nowhere else."""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '另一条支线', mode='auto')
        sb.start_cycle('one')
        sb.start_cycle('two', force=True)
        sb.instruct('只属于 two 的', scope='cycle', cycle='two')

        entry = sb.instruct_list()[0]
        self.assertEqual(entry['cycle'], 'two')
        # Naming the other line's node leaves both halves of the position unknown, and a cycle entry
        # is read out to nobody there -- the whole point of asking for the line.
        self.assertEqual(sb.instruct_visible_cli(node=NODE), [])
        # `two` is pinned on NODE2 (the second pick), so reading from there lands on `two`.
        self.assertEqual([i['text'] for i in sb.instruct_visible_cli(node=NODE2)],
                         ['只属于 two 的'])

    def test_a_cycle_entry_reaches_its_own_line_only(self):
        """A cycle entry goes to the line it was recorded in. A reader on the other line sees the
        globals and its own cycle's entries, and this one is neither."""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '另一条支线', mode='auto')
        sb.start_cycle('one')
        sb.start_cycle('two', force=True)
        sb.instruct('one 的约束', scope='cycle', cycle='one')
        sb.instruct('谁都要看的')

        seen_by_two = {i['text'] for i in sb.instruct_visible_cli(node=NODE2)}
        self.assertEqual(seen_by_two, {'谁都要看的'})

    def test_the_account_view_shows_the_rows_and_the_default_view_folds_them(self):
        sb = self.with_cycle()
        sb.instruct('全局的约束')
        rows = sb.instruct_list_cli()
        folded = sb.instruct_visible_cli()

        self.assertEqual(rows[0]['text'], '全局的约束')
        self.assertIn('id', rows[0])
        self.assertEqual(folded[0]['text'], '全局的约束')
        self.assertIn('scope', folded[0])

class TestInstructionTargets(Base):
    """Two targets: a constraint every worker must see, and the rhythm read when a step closes."""

    def test_the_two_targets_reach_their_two_readers(self):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.instruct('每条都要看的', target='stage')
        sb.instruct('收尾时才读的', target='boundary')

        targets = {i['text']: i['target'] for i in sb.instruct_list()}
        self.assertEqual(targets, {'每条都要看的': 'stage', '收尾时才读的': 'boundary'})
        # Both are part of the same read: the target says WHO cares, not whether it is live.
        self.assertEqual({i['text'] for i in sb.instruct_visible_cli()},
                         {'每条都要看的', '收尾时才读的'})

    def test_a_target_outside_the_two_is_refused(self):
        sb = self.make()
        proc = sb._tool('instruction', 'add', '--text', 'x', '--target', 'round', check=False,
                        cli=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('invalid choice', proc.stderr)


class TestInstructionAuthority(Base):
    """Who may retire an entry, and what a retired one leaves behind."""

    def seeded(self, scope='global', text='全局的约束'):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.instruct(text, scope=scope)
        return sb, sb.instruct_list()[0]['id']

    def test_resolving_retires_an_entry_without_deleting_it(self):
        sb, ins_id = self.seeded()
        sb.instruct_resolve(ins_id, 'withdrawn')

        self.assertEqual(sb.instruct_visible_cli(), [])
        rows = sb.instruct_list_cli()
        self.assertEqual(rows[0]['state'], 'withdrawn')
        self.assertEqual(rows[0]['text'], '全局的约束')

    def test_a_withdrawn_entry_is_still_readable_and_out_of_force(self):
        """Archiving is not deleting: the row stays on the account and stops being read.

        Withdrawing is the owner's move; a scoped entry goes out of sight on its own once its line
        is gone, so the archive keeps the sentence and no verdict about the work is written.
        """
        sb, ins_id = self.seeded(scope='cycle', text='这条线里的约束')
        sb.instruct('新的约束', scope='cycle', cycle='cyc')
        sb.instruct_resolve(ins_id, 'withdrawn')

        rows = {r['id']: r for r in sb.instruct_list_cli()}
        self.assertEqual(rows[ins_id]['state'], 'withdrawn')
        self.assertEqual(rows[ins_id]['text'], '这条线里的约束')
        self.assertEqual([i['text'] for i in sb.instruct_visible_cli()], ['新的约束'])

    def test_the_account_has_only_one_move_left(self):
        """Two states, one of them written by the owner: anything else `resolve` is asked for is
        refused, and the refusal names the word that is left."""
        sb, ins_id = self.seeded()

        proc = sb._tool('instruction', 'resolve', ins_id, '--state', 'done', check=False, cli=True)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('withdrawn', proc.stderr)
        self.assertEqual(sb.instruct_list()[0]['state'], 'live')

    def test_a_resolved_instruction_leaves_the_next_read(self):
        sb, ins_id = self.seeded()
        sb.instruct('留下的')
        sb.instruct_resolve(ins_id, 'withdrawn')

        self.assertEqual([i['text'] for i in sb.instruct_visible_cli()], ['留下的'])

    def test_a_prompt_carries_no_instruction_text(self):
        """The channel is `instruction list`, run by the session itself; a box assembled into the
        prompt would only ever be a staler copy of that answer, and a place where a constraint can
        look present while nobody reads it."""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.instruct('一条绝不能进 prompt 的约束')

        action = sb.plan()

        self.assertNotIn('一条绝不能进 prompt 的约束', action['prompt_text'])
        self.assertNotIn('约束', action['prompt_text'])

    def test_the_closing_call_leaves_the_account_alone(self):
        sb, ins_id = self.seeded()
        before = sb.read_instructions()
        sb.next()
        sb.write_signal('cyc', NODE, 'completed', 'done')
        sb.close_round(NODE)

        self.assertEqual(sb.read_instructions(), before)

    def test_the_command_reads_the_project_it_is_told_to(self):
        """`--project` is the whole point of the flag: a session that cd'd away must still read the
        account it means, because an empty wrong-project read looks like "nothing was asked"."""
        sb, ins_id = self.seeded()
        other = self.make()

        proc = run_state_manage(other.root, 'instruction', 'list', '--project', str(sb.root))

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('全局的约束', proc.stdout)
        self.assertTrue((sb.root / '.pf' / 'instructions.json').exists())
