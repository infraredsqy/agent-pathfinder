"""The core mechanism every insertion source shares: mint the id, write the spec, wire the edges.

Two things are pinned here, and they are the two halves of one claim:

  * **it works for a source that is not the shipped plugin** -- the fixture below is a hand-written
    plugin folder (a declaration plus an implementation) plugged in through `PF_PLUGINS_DIR`, and
    it gets the graph edit without re-implementing any of it: the uuid, the `spec.md` and the two
    edges all come out of `insertion.py`;
  * **there is only one implementation of each step** -- a second one reads perfectly well on its
    own, which is exactly why the guard exists. `insertion.py` is the one file allowed to spell the
    placeholder, read the uuid off `add`, build the edge edits or write the text; no plugin folder
    may carry a second copy either.

What an insertion deliberately does NOT do (open no session, write no prompt, move no hand) is a
property of the action, and the cases that pin it drive a close or a pick:
`cases_maintenance.py` and `cases_plugin.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE, NODE2, pipeline  # noqa: E402
from harness import CONDUCT_DIR, plugins_dir, write_plugin  # noqa: E402

__all__ = ['TestTheInsertionMechanism']

#: The second source's own implementation: an offer and nothing else. It declares what it wants
#: inserted and where -- the place, the uuid, the text file and the edges are the core's.
SECOND_SOURCE_IMPL = '''
TAG = 'second-source'


def offer(ctx):
    """One node in front of whatever this close picked, until one of ours is already open."""
    if ctx.nodes_with_tag(TAG):
        return None
    return {
        'title': 'Inserted by a second source',
        'tags': ['coding', TAG],
        'spec_of': lambda node_id: f'# Inserted by a second source\\n\\nwritten for {node_id}\\n',
        'fields': {'metric': 'second_source'},
        'note': 'a hand-written plugin folder asked for this node',
    }
'''


def second_source_decl(name='second'):
    """A close-timing insertion declaration: the smallest one that is a legal plugin."""
    return {
        'name': name,
        'summary': 'a hand-written second insertion source (the fixture)',
        'timing': 'close',
        'action': 'insert',
        'knobs': {},
        'readings': {},
        'metrics': {},
    }


class TestTheInsertionMechanism(Base):
    """A source declares a node and where it goes; the machinery is the core's, and it is one copy."""

    def a_pair(self):
        """Two nodes with an edge between them -- the place an insertion wants, and nothing else.

        No cycle: the mechanism is a graph edit, so the case that pins it does not need a line at all.
        """
        sb = self.make()
        sb.add_node(NODE, 'Write code A', tags=['coding'])
        sb.add_node(NODE2, 'Write code B', tags=['coding'], deps=[NODE])
        return sb

    def test_a_second_source_gets_the_same_mint_spec_and_edges(self):
        """The declaration is the source's; all three steps come out of the one mechanism.

        The source is a folder with a `plugin.json` and an `impl.py` and nothing else: it is plugged
        in by pointing the scan at it, so the core is untouched (the same move the shipped plugin
        makes), and what lands is the core's own graph edit.
        """
        sb = self.a_pair()
        sb.start_cycle(tags=['coding'])
        sb.set_policy('cyc', 'parallel', 1)
        sb.next()                                  # the dispatch for NODE
        sb.write_signal('cyc', NODE, 'completed', 'done', {'deliverable': 'x'})

        with plugins_dir(sb.root / 'plugins'):
            write_plugin(sb.root / 'plugins', 'second', second_source_decl(),
                          SECOND_SOURCE_IMPL)
            action = sb.close_round(NODE)

        self.assertEqual(action['action'], 'insert_node', action)
        made = action['node_id']
        nodes = sb.dag()['nodes']
        self.assertEqual(len(nodes), 3, f'nothing was inserted: {action}')
        self.assertRegex(made, r'^[0-9a-fA-F-]{36}$', 'the id is not the uuid the tool minted')
        self.assertEqual(nodes[made]['title'], 'Inserted by a second source')
        self.assertEqual(nodes[made]['tags'], ['coding', 'second-source'])
        self.assertEqual(nodes[made]['dependencies'], [NODE],
                         'the new node must be the place it was given')
        self.assertEqual(nodes[NODE2]['dependencies'], [made],
                         'the node it was put in front of must wait for it')
        landed = Path(pipeline.nodes.node_spec_path(sb.root, made))
        self.assertTrue(landed.is_file(), f'the spec did not land at {landed}')
        self.assertIn(made, landed.read_text(encoding='utf-8'))
        self.assertEqual(sb.holding_ids(), [], 'the insertion pinned a node')

    def test_the_three_steps_have_one_implementation(self):
        """Minting the uuid, writing the spec and wiring the edges exist in `insertion.py` only.

        A second copy of any of them reads perfectly well on its own -- and that is the reason this
        guard is here: two copies drift, and the drifted one is whichever the next source copies. A
        plugin folder is where such a copy would appear first, so it is scanned too: a plugin
        declares, it never re-implements.
        """
        impl = CONDUCT_DIR / 'pipeline'
        needles = {
            '%DUID%': 'the new node\'s placeholder',
            'Node created': 'the uuid `add` prints',
            "'undepend'": 'the edge edit',
            "'--on'": 'a dependency',
            'def write_spec': 'the spec writer',
        }

        def files_with(needle: str, folder: Path, base: Path = CONDUCT_DIR) -> list:
            return sorted(path.relative_to(base).as_posix()
                          for path in folder.rglob('*.py')
                          if path.parent.name != 'tests' and needle in path.read_text(encoding='utf-8'))

        plugins = CONDUCT_DIR.parents[1] / 'plugins'
        for needle, what in needles.items():
            self.assertEqual(['pipeline/insertion.py'], files_with(needle, impl),
                             f'{what} is spelled somewhere else in the package')
            self.assertEqual([], files_with(needle, plugins, CONDUCT_DIR.parents[1]),
                             f'a plugin folder carries its own copy of {what}')
