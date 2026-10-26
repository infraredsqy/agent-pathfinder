"""The core mechanism every insertion source shares: mint the id, write the spec, wire the edges.

Two things are pinned here, and they are the two halves of one claim:

  * **it works for a source that is not the maintenance channel** -- the fixture below declares its
    own node (title, tags, spec, place) and gets the three steps without re-implementing any of them;
  * **there is only one implementation of each step** -- a second one reads perfectly well on its
    own, which is exactly why the guard exists.

What an insertion deliberately does NOT do (open no session, write no prompt, move no hand) is a
property of the action, and the case that pins it is the one that drives a close:
`cases_maintenance.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE, NODE2, pipeline  # noqa: E402
from harness import CONDUCT_DIR, _dag_hook  # noqa: E402

__all__ = ['TestTheInsertionMechanism']


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

    def second_source(self, sb) -> str:
        """A second insertion source, standing in for any future insertion action.

        It states what it wants inserted and where -- and that is the whole of its code: the uuid,
        the `spec.md` and the two edges come from `insertion.py`, through the same `mint` the
        maintenance channel's action goes through in `writer.apply`.
        """
        with _dag_hook(sb.root):
            edit = pipeline.insertion.insert(
                sb.root,
                title='Inserted by a second source',
                tags=['coding', 'second-source'],
                spec_of=lambda node_id: f'# Inserted by a second source\n\nwritten for {node_id}\n',
                after=NODE, before=NODE2,
            )
            node_id, commands = pipeline.insertion.mint(
                sb.root, edit['add_command'], edit['spec_of'], edit['commands'])
            for command in commands:
                pipeline.seam.run(command, sb.root)
        return node_id

    def test_a_second_source_gets_the_same_mint_spec_and_edges(self):
        """The declaration is the source's; all three steps come out of the one mechanism."""
        sb = self.a_pair()

        made = self.second_source(sb)

        nodes = sb.dag()['nodes']
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

    def test_the_three_steps_have_one_implementation(self):
        """Minting the uuid, writing the spec and wiring the edges exist in `insertion.py` only.

        A second copy of any of them reads perfectly well on its own -- and that is the reason this
        guard is here: two copies drift, and the drifted one is whichever the next source copies.
        """
        impl = CONDUCT_DIR / 'pipeline'

        def files_with(needle: str) -> list:
            return sorted(path.name for path in impl.glob('*.py')
                          if needle in path.read_text(encoding='utf-8'))

        self.assertEqual(['insertion.py'], files_with('%DUID%'),
                         'the new node\'s placeholder is spelled somewhere else')
        self.assertEqual(['insertion.py'], files_with('Node created'),
                         'something else reads the uuid off `dag-manage add` as well')
        self.assertEqual(['insertion.py'], files_with("'undepend'"),
                         'the edge edit is built somewhere else')
        self.assertEqual(['insertion.py'], files_with("'--on'"),
                         'a dependency is wired somewhere else')
        self.assertEqual(['insertion.py'], files_with('def write_spec'),
                         'the spec is written somewhere else')
