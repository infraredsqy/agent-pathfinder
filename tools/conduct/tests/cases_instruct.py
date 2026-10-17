"""The standing-instruction account: one account file, three scopes, two targets, two states.

Nothing is injected into a dispatch prompt any more, so `instruction list` IS the instruction channel
-- a session runs it among its first commands and reads where it stands. Everything here is about
that account: what it keeps verbatim, which entries a reader can see from where, and what the owner
can take back.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE, NODE2  # noqa: E402
from harness import run_state_manage  # noqa: E402

__all__ = ['TestInstructionScope', 'TestInstructionTargets', 'TestInstructionAuthority',
           'TestInstructionRemoval', 'TestInstructionRevival']


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

    def test_a_finished_nodes_entry_leaves_the_view_once_the_line_lets_go(self):
        """这个节点做完、线把它放下之后，位置钉不下来（没有 active 线握着它）：判定退回按条目
        绑定的那个节点的现状读 —— `completed`，于是不念。

        点名这个节点读到的是同一个答案：没有一条 active 线握着它，位置同样钉不下来。
        """
        sb = self.with_cycle()
        sb.instruct('做这个节点时的约束', scope='node')
        sb.write_signal('cyc', NODE, 'completed', 'done')
        sb.close_round(NODE)

        self.assertEqual(sb.instruct_visible_cli(), [])
        self.assertEqual(sb.instruct_visible_cli(node=NODE), [])

    def test_a_named_node_is_not_swapped_for_the_one_in_hand(self):
        """`--node X` 说出的位置就是 X：线上正握着别的节点也不换。

        手握着的那一格说的是**线**在哪儿，`--node` 说的是**读者**在哪一格 —— 换掉它，等于让
        读者站在线上的别处读约束。
        """
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '另一条支线', mode='auto')
        sb.start_cycle()                       # 线握着 NODE
        sb.instruct('只属于 NODE 的', scope='node', node=NODE)
        sb.instruct('只属于 NODE2 的', scope='node', node=NODE2)

        # 没点名：读手里那一格。
        self.assertEqual([i['text'] for i in sb.instruct_visible_cli()], ['只属于 NODE 的'])
        # 点名 NODE2：原样生效，不换成手里的 NODE。
        self.assertEqual([i['text'] for i in sb.instruct_visible_cli(node=NODE2)],
                         ['只属于 NODE2 的'])

    def test_a_node_entry_is_not_read_at_another_nodes_close(self):
        """收尾与读侧站的是同一个位置 —— 这是位置制唯一的判据。

        一条绑在 NODE2 上的收尾指令（含「跑够 N 个就停」）收 NODE 时不能念：站在 NODE 的会话
        看不见它，收 NODE 的那一拍也就不该被它叫停。收尾自己按「绑定节点做没做完」判一次，就是
        同一个位置读出两个答案。
        """
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '另一条支线', mode='auto')
        sb.start_cycle()
        sb.instruct('只属于另一个节点的', scope='node', target='boundary', node=NODE2,
                    stop_after_nodes=1)

        self.assertEqual(sb.instruct_visible_cli(node=NODE), [])
        self.assertEqual([i['text'] for i in sb.instruct_list()], ['只属于另一个节点的'])

        sb.write_signal('cyc', NODE, 'completed', 'done')
        action = sb.close_round(NODE, decision='continue')

        self.assertNotEqual(action['action'], 'stop_pipeline',
                            "another node's rhythm instruction capped this close")
        self.assertEqual(action['action'], 'dispatch')
        self.assertEqual(action['node_id'], NODE2)

    def test_the_place_is_the_node_in_hand_even_within_one_line(self):
        """一条线同时握着两个节点时（`parallel`），条目跟着手上那个节点走，不跟着线走。"""
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '另一条支线', mode='auto')
        sb.start_cycle()
        sb.set_policy('cyc', 'parallel', 2)
        sb.set_current_node(NODE2)
        sb.instruct('只属于另一个节点的', scope='node', node=NODE2)

        self.assertEqual(sb.instruct_visible_cli(node=NODE), [])
        self.assertEqual([i['text'] for i in sb.instruct_visible_cli(node=NODE2)],
                         ['只属于另一个节点的'])

    def test_two_lines_side_by_side_read_the_wide_way_when_nothing_names_your_place(self):
        """位置钉不下来的时候往「吵」的一边倒。

        两条 active cycle 并排、又没说手上是哪个节点：指针指不出哪条线在问，于是 `cycle` 作用域的
        条目照念 —— 漏挡一条真约束比多念一条不相干的贵得多。说了手上是哪个节点，就按那条线读。
        """
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.add_node(NODE2, '另一条支线', mode='auto')
        sb.start_cycle('one')
        sb.start_cycle('two', force=True)
        sb.instruct('one 的约束', scope='cycle', cycle='one')

        self.assertEqual([i['text'] for i in sb.instruct_visible_cli()], ['one 的约束'])
        self.assertEqual([i['text'] for i in sb.instruct_visible_cli(node=NODE)], ['one 的约束'])
        self.assertEqual(sb.instruct_visible_cli(node=NODE2), [])

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
        # Naming the other line's node pins the position to that node (and the line holding it), so the
        # entry -- bound to `two` -- is not read here: the comparison is exact.
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

    def test_the_archive_is_the_state_and_the_moment_and_no_hand(self):
        """Only the owner archives an entry, so the record names no hand for it: the archive writes
        the state and the moment, and a recorded entry never carries the field at all."""
        sb, ins_id = self.seeded()
        self.assertNotIn('resolved_by', sb.instruct_list()[0])

        sb.instruct_resolve(ins_id, 'withdrawn')

        entry = sb.instruct_list()[0]
        self.assertEqual('withdrawn', entry['state'])
        self.assertTrue(entry['resolved_at'])
        self.assertNotIn('resolved_by', entry)

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


class TestInstructionRemoval(Base):
    """Taking an entry off the account: gone from every reading, and a trace the merge holds on."""

    def seeded(self, count=2):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        for i in range(count):
            sb.instruct(f'第 {i + 1} 条约束')
        return sb, [e['id'] for e in sb.instruct_list()]

    def account_bytes(self, sb) -> str:
        return (sb.root / '.pf' / 'instructions.json').read_text(encoding='utf-8')

    def test_the_entry_and_its_text_are_gone_from_every_reading(self):
        """Archiving hides an entry from the default view; removal has to leave nothing at all --
        not in the account view, not in its machine twin, not on disk."""
        sb, ids = self.seeded()
        sb.instruct_rm(ids[0])

        self.assertEqual([e['id'] for e in sb.instruct_list()], [ids[1]])
        self.assertEqual([e['id'] for e in sb.instruct_list_cli()], [ids[1]])       # --all --json
        self.assertEqual([e['id'] for e in sb.instruct_visible_cli()], [ids[1]])    # the default view
        human_all = sb._tool('instruction', 'list', '--all', cli=True).stdout
        self.assertNotIn(ids[0], human_all)
        self.assertNotIn('第 1 条约束', human_all)
        self.assertNotIn('第 1 条约束', self.account_bytes(sb))

    def test_what_stays_is_the_id_and_the_moment_and_no_state_word(self):
        """A removal is not a third state: the trace is bookkeeping, and the account's own
        vocabulary is untouched by it."""
        sb, ids = self.seeded()
        sb.instruct_rm(ids[0])

        traces = sb.read_instructions()['removed']
        self.assertEqual([{'id': ids[0], 'at': traces[0]['at']}], traces)
        self.assertTrue(traces[0]['at'])
        self.assertEqual('live', [e for e in sb.instruct_list() if e['id'] == ids[1]][0]['state'])

    def test_the_id_of_a_removed_entry_is_not_minted_again(self):
        """The id is what tells one machine's account from another's across a merge. Counting the
        list would hand the number that just went away to the next entry added."""
        sb, ids = self.seeded()
        sb.instruct_rm(ids[1])

        sb.instruct('接着记的')

        self.assertEqual([e['id'] for e in sb.instruct_list()], [ids[0], 'i-0003'])

    def test_the_sentence_is_printed_before_it_goes(self):
        """After the write there is nothing left to compare against: the one reading that proves
        the caller and the tool meant the same entry has to come first."""
        sb, ids = self.seeded()

        proc = sb.instruct_rm(ids[0], cli=True)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(ids[0], proc.stdout)
        self.assertIn('第 1 条约束', proc.stdout)

    def test_an_archived_entry_can_still_be_taken_off(self):
        """The two owner moves are independent: an archive that turned out to be the wrong call is
        not a reason to keep the sentence on the account."""
        sb, ids = self.seeded()
        sb.instruct_resolve(ids[0], 'withdrawn')

        sb.instruct_rm(ids[0], cli=True)

        self.assertEqual([e['id'] for e in sb.instruct_list()], [ids[1]])
        self.assertNotIn('第 1 条约束', self.account_bytes(sb))

    def test_an_id_the_account_never_had_is_refused(self):
        sb, ids = self.seeded()

        proc = sb.instruct_rm('i-9999', check=False, cli=True)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('no instruction', proc.stderr)
        self.assertIn('i-9999', proc.stderr)
        self.assertEqual([e['id'] for e in sb.instruct_list()], ids)

    def test_removing_one_that_is_already_off_names_the_trace(self):
        """A second removal is not "no such entry": the id is known, and the answer says which
        move already happened to it."""
        sb, ids = self.seeded()
        sb.instruct_rm(ids[0], cli=True)
        before = sb.read_instructions()

        proc = sb.instruct_rm(ids[0], check=False, cli=True)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('was removed', proc.stderr)
        self.assertIn(ids[0], proc.stderr)
        self.assertEqual(before, sb.read_instructions())


class TestInstructionRevival(Base):
    """Putting an archived entry back in force: the state moves, the record's shape does not."""

    def archived(self, text='全局的约束'):
        sb = self.make()
        sb.add_node(NODE, '界面设计', mode='auto')
        sb.start_cycle()
        sb.instruct(text)
        ins_id = sb.instruct_list()[0]['id']
        sb.instruct_resolve(ins_id, 'withdrawn', note='当时先收起来')
        return sb, ins_id

    def test_reviving_puts_an_archived_entry_back_in_force(self):
        sb, ins_id = self.archived()

        proc = sb.instruct_revive(ins_id, cli=True)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = sb.instruct_list()[0]
        self.assertEqual('live', entry['state'])
        self.assertIsNone(entry['resolved_at'])
        self.assertNotIn('resolved_by', entry)
        self.assertEqual([ins_id], [i['id'] for i in sb.instruct_visible_cli()])
        self.assertIn('全局的约束', sb._tool('instruction', 'list', cli=True).stdout)

    def test_an_entry_that_is_already_live_is_refused(self):
        sb, ins_id = self.archived()
        sb.instruct_revive(ins_id, cli=True)
        before = sb.read_instructions()

        proc = sb.instruct_revive(ins_id, check=False, cli=True)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('already live', proc.stderr)
        self.assertEqual(before, sb.read_instructions())

    def test_an_id_the_account_never_had_is_refused(self):
        sb, ins_id = self.archived()

        proc = sb.instruct_revive('i-9999', check=False, cli=True)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('no instruction', proc.stderr)
        self.assertEqual('withdrawn', sb.instruct_list()[0]['state'])

    def test_a_removed_entry_is_not_brought_back_by_putting_it_back(self):
        """Removal is off the account, not archived on it: the trace has no sentence left, and
        reviving must not invent one."""
        sb, ins_id = self.archived()
        sb.instruct_rm(ins_id, cli=True)

        proc = sb.instruct_revive(ins_id, check=False, cli=True)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('was removed', proc.stderr)
        self.assertEqual(sb.instruct_list(), [])
        self.assertNotIn('全局的约束',
                         (sb.root / '.pf' / 'instructions.json').read_text(encoding='utf-8'))
