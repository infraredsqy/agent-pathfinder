"""`tools/dag/graph.py`: 图那一半，进程内直接调用 —— 一个子进程都不用起。

跑法（两种都行）：

    python -m unittest discover -s dag/tests -t .      （cwd 在 tools/）
    python dag/tests/test_graph.py
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

GRAPH_PY = Path(__file__).resolve().parent.parent / 'graph.py'


def _load(name: str, path: Path):
    """按路径取一份模块：这一份不引用邻居，整份 import 起来就行。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


graph = _load('dag_graph_case', GRAPH_PY)


def node(status='todo', deps=()):
    return {'status': status, 'dependencies': list(deps)}


class TestAdjacency(unittest.TestCase):

    def test_it_reads_the_dependency_list_of_every_node(self):
        nodes = {'a': node(deps=['b']), 'b': node()}
        self.assertEqual({'a': ['b'], 'b': []}, graph._adjacency(nodes))

    def test_a_node_that_never_wrote_the_key_comes_out_empty(self):
        self.assertEqual({'a': []}, graph._adjacency({'a': {'status': 'todo'}}))

    def test_reverse_adjacency_lists_dependents(self):
        adj = {'a': ['b'], 'b': []}
        self.assertEqual({'a': [], 'b': ['a']}, graph._reverse_adj(adj))

    def test_a_dangling_edge_points_at_nobody(self):
        """记录里点名了图上没有的节点：反查表里它不占一格。"""
        self.assertEqual({'a': []}, graph._reverse_adj({'a': ['gone']}))


class TestOrder(unittest.TestCase):

    def test_a_dependency_comes_before_the_node_that_needs_it(self):
        order = graph._topological_sort({'a': ['b'], 'b': []})
        self.assertLess(order.index('b'), order.index('a'))

    def test_a_cycle_still_hands_every_node_back_once(self):
        order = graph._topological_sort({'a': ['b'], 'b': ['a']})
        self.assertEqual(sorted(['a', 'b']), sorted(order))


class TestScores(unittest.TestCase):

    def test_the_node_with_no_blockers_is_the_most_urgent(self):
        scores = graph._urgency_scores({'root': [], 'leaf': ['root']})
        self.assertEqual(1, scores['root'])
        self.assertEqual(0, scores['leaf'])

    def test_the_least_urgent_node_sits_at_zero(self):
        self.assertEqual(0, min(graph._urgency_scores({'a': []}).values()))

    def test_importance_counts_who_depends_on_you_transitively(self):
        scores = graph._importance_scores({'root': [], 'mid': ['root'], 'leaf': ['mid']})
        self.assertEqual(2, scores['root'])
        self.assertEqual(1, scores['mid'])
        self.assertEqual(0, scores['leaf'])

    def test_an_empty_graph_has_no_scores(self):
        self.assertEqual({}, graph._urgency_scores({}))
        self.assertEqual({}, graph._importance_scores({}))


class TestReach(unittest.TestCase):

    def test_it_hands_back_the_path_it_walked(self):
        adj = {'a': ['b'], 'b': ['c'], 'c': []}
        self.assertEqual((True, ['a', 'b', 'c']), graph._can_reach(adj, 'a', 'c'))

    def test_a_target_nobody_points_at_is_unreachable(self):
        self.assertEqual((False, []), graph._can_reach({'a': []}, 'a', 'b'))

    def test_a_dangling_reference_is_skipped_on_the_way(self):
        found, path = graph._can_reach({'a': ['gone'], 'b': []}, 'a', 'b')
        self.assertEqual((False, []), (found, path))

    def test_a_node_reaches_itself(self):
        self.assertEqual((True, ['a']), graph._can_reach({'a': []}, 'a', 'a'))


class TestCycles(unittest.TestCase):

    def test_a_back_edge_is_reported_with_the_ring_it_closed(self):
        has_cycle, path = graph._detect_cycle({'a': ['b'], 'b': ['a']})
        self.assertTrue(has_cycle)
        self.assertEqual(['a', 'b', 'a'], path)

    def test_a_line_has_no_cycle(self):
        self.assertEqual((False, []), graph._detect_cycle({'a': [], 'b': ['a']}))

    def test_a_self_loop_is_a_cycle(self):
        self.assertTrue(graph._detect_cycle({'a': ['a']})[0])


class TestReadiness(unittest.TestCase):

    def test_completed_and_absent_dependencies_are_both_satisfied(self):
        self.assertTrue(graph._is_ready(['done', 'gone'], {'done': node('completed')}))

    def test_a_pending_dependency_leaves_the_node_waiting(self):
        self.assertFalse(graph._is_ready(['later'], {'later': node('todo')}))

    def test_only_a_todo_node_is_ready(self):
        all_nodes = {'a': node('todo'), 'b': node('shelved'), 'c': node('completed')}
        self.assertTrue(graph._node_ready(all_nodes['a'], all_nodes))
        self.assertFalse(graph._node_ready(all_nodes['b'], all_nodes))
        self.assertFalse(graph._node_ready(all_nodes['c'], all_nodes))

    def test_a_todo_node_with_an_unfinished_dependency_waits(self):
        all_nodes = {'a': node('todo', ['b']), 'b': node('todo')}
        self.assertFalse(graph._node_ready(all_nodes['a'], all_nodes))


if __name__ == '__main__':
    unittest.main(verbosity=2)