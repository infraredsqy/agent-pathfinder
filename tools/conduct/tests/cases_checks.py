"""The validator: one declaration against the implementation beside it.

`pipeline/feature_registry.py` answers two kinds of question. `validate()` reads the declaration
alone -- is the timing one the core knows, the action, the knob kinds, do two features keep off each
other's names. `verify()` is the validator and asks the other side as well: is there a card for every
method the metrics name (a card lives in the feature folder that binds it, under `cards/<lang>/`),
and does the implementation answer the way its declaration says it will -- one real call against a
stand-in project, which may answer (an offer, a reason) or "not known this pass", may never raise, and
may not carry a command-shaped list in a key the core reads (the edit has one implementation, and it
is the core's).

What this file pins, in the order the criteria are stated:

  * the installed tree validates, and the shipped folder is inside the closed sets;
  * a declaration naming a timing or a knob kind the core does not know is a *problem*, and the
    sentence names the feature (which is the half a human acts on);
  * two features may not claim one name (a knob, a reading, a dedupe tag) -- and they may share a
    method, because one card can serve two debts;
  * a method needs its `SKILL.md` in a card folder some installed feature ships, one that has it is
    silent, and a card folder whose method nothing names is a problem in the other direction;
  * an implementation that raises at its door, or that answers with a command-shaped list in a key
    the core reads, is named -- and the stand-in project starts no child process;
  * and the pipeline's route never asks any of it: the two callers are this suite and `pf doctor`.

Every case builds its own features directory (`PF_FEATURES_DIR`), so nothing here depends on the
folders this checkout ships; a case whose subject IS the shipped folder skips itself when it is gone.
"""

from __future__ import annotations

import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, pipeline  # noqa: E402
from harness import (CONDUCT_DIR, features_dir, requires_feature, shipped_sources,  # noqa: E402
                     write_card, write_feature)

__all__ = ['TestTheValidator']

#: The method name every fixture's card is written under. A case that needs a method with a card asks
#: the fixture for one instead of reading a name off the shipped tree: the card lives in the feature
#: folder beside the declaration, so the fixture writes both.
CARDED = 'pf-card-fixture'

#: A minimal, valid declaration: one metric, one line, one tag. A case changes the one cell it is
#: about, so the case's subject is the only thing that differs between a green and a red run.
DECL = {
    'name': 'probe', 'summary': 'a fixture', 'timing': 'close', 'action': 'insert',
    'knobs': {'probe_limit': {'kind': 'number'}},
    'readings': {'probe_minutes': {'source': 'ticket', 'when_knob': 'probe_limit'}},
    'metrics': {'probe_minutes': {'line': 'probe_limit', 'tag': 'probe:minutes',
                                  'method': None, 'order': 0}},
}

#: The smallest implementation: nothing to say this pass.
QUIET_IMPL = 'def offer(ctx):\n    return None\n'


def declaration(method: str, **over) -> dict:
    """`DECL` with the one method card it names, and any cell a case replaces."""
    decl = {key: (dict(value) if isinstance(value, dict) else value) for key, value in DECL.items()}
    decl['metrics'] = {key: dict(cell) for key, cell in DECL['metrics'].items()}
    for cell in decl['metrics'].values():
        cell['method'] = method
    decl.update(over)
    return decl


class TestTheValidator(Base):
    """One temp features directory per case, and the validator asked about it in this process."""

    def features(self):
        """A throwaway features directory, entered for the case's whole life.

        One per case: a second call hands the same directory back, so a case that writes a second
        fixture (or a second card) after calling `one()` is still writing into the tree the scan is
        pointed at.
        """
        root = getattr(self, '_features_root', None)
        if root is None:
            tmp = tempfile.TemporaryDirectory(prefix='conduct-checks-')
            self.addCleanup(tmp.cleanup)
            stack = ExitStack()
            self.addCleanup(stack.close)
            stack.enter_context(features_dir(tmp.name))
            root = self._features_root = Path(tmp.name)
        return root

    def one(self, method=None, impl=QUIET_IMPL, card=True, **over):
        """One feature folder named after the declaration's own name, and the validator's answer.

        `card` writes the card the method names into the same folder: the feature folder is a card's
        one home, so "the method is backed" is the fixture writing both halves.
        """
        root = self.features()
        name = over.pop('name', 'probe')
        method = method or CARDED
        folder = write_feature(root, name, declaration(method, name=name, **over), impl)
        if card:
            write_card(folder, method)
        return pipeline.registry().verify()

    # ── the installed tree ──────────────────────────────────────────────────
    def test_the_installed_tree_validates(self):
        """Whatever this checkout ships passes the validator, with no feature at all included.

        A features directory with nothing in it is a legal project: the core is whole without any
        feature, and the validator then has nothing to ask.
        """
        self.assertEqual([], pipeline.registry().verify())

    @requires_feature('maintenance')
    def test_the_shipped_declaration_sits_in_the_closed_sets(self):
        """The shipped declaration is inside the tables the core serves, and its methods have cards.

        Read off the registry itself, not restated here: a declaration that drifted out of the three
        cells would be a feature the runtime never asks, and the validator is what says so.
        """
        registry = pipeline.registry()
        declared = {(feature['decl']['timing'], feature['decl']['action'])
                    for feature in registry.load_all() if isinstance(feature.get('decl'), dict)}
        self.assertTrue(declared <= set(registry.CELLS), declared - set(registry.CELLS))
        cards = registry.cards()
        for method in registry.methods():
            self.assertIn(method, cards, f'{method} has no card in any installed feature folder')
            self.assertTrue(any((folder / registry.CARD).is_file()
                                for _owner, folder in cards[method]),
                            f'{method} has no {registry.CARD}')

    # ── (a) the closed sets, and the name in the sentence ───────────────────
    def test_a_timing_or_a_kind_outside_the_closed_sets_names_the_feature(self):
        """A timing the core does not serve and a knob kind it does not know are problems, and the
        sentence names the feature.
        """
        problems = self.one(timing='whenever',
                            knobs={'probe_limit': {'kind': 'fifth'}})

        text = '\n'.join(problems)
        self.assertIn("feature 'probe'", text, 'the problem has to name the feature')
        self.assertIn("timing 'whenever' is not one of", text)
        self.assertIn("kind 'fifth'", text)

    def test_an_action_pair_outside_the_two_axes_is_a_problem(self):
        """The three served cells are the whole table: `insert` at a dispatch is a declaration the
        runtime would skip forever, so it is reported rather than accepted.
        """
        problems = self.one(timing='dispatch', action='insert')

        self.assertIn("feature 'probe'", '\n'.join(problems))
        self.assertIn('is not a cell this core serves', '\n'.join(problems))

    # ── (b) two declarations, one name ──────────────────────────────────────
    def test_two_features_may_not_claim_one_name(self):
        """A knob, a reading and a dedupe tag belong to one feature each; the sentence names both.

        A method name may repeat -- one card can serve two debts -- so the two declarations here share
        their method on purpose and the only complaints are the three real collisions.
        """
        root = self.features()
        method = CARDED
        write_feature(root, 'one', declaration(method, name='one'), QUIET_IMPL)
        write_feature(root, 'two', declaration(method, name='two'), QUIET_IMPL)
        write_card(root / 'one', method)

        problems = pipeline.registry().verify()
        text = '\n'.join(problems)

        self.assertIn("knob 'probe_limit' is claimed by both 'one' and 'two'", text)
        self.assertIn("reading 'probe_minutes' is claimed by both 'one' and 'two'", text)
        self.assertIn("dedupe tag 'probe:minutes' is claimed by both 'one' and 'two'", text)
        # the three collisions and nothing else: the shared method is not one of the names two
        # features may not share
        self.assertEqual(3, len(problems), text)

    # ── (c) the method card ─────────────────────────────────────────────────
    def test_a_method_without_a_card_names_the_feature(self):
        """A method whose card no feature folder carries: the generated node would point a session at
        nothing, so the validator says which method and which feature.
        """
        problems = self.one(method='pf-no-such-card', card=False)

        text = '\n'.join(problems)
        self.assertIn("feature 'probe'", text)
        self.assertIn("method 'pf-no-such-card' has no card", text)

    def test_a_method_that_has_a_card_is_silent(self):
        """The other half: a method the folder beside it carries a card for is no problem at all."""
        self.assertEqual([], self.one())

    def test_a_card_no_declaration_names_names_the_feature(self):
        """The other direction: a card folder under `cards/<lang>/` whose method nothing names is a
        card no session can be pointed at, so it is a problem, and the sentence names the feature
        whose folder carries it.
        """
        root = self.features()
        self.assertEqual([], self.one())
        write_card(root / 'probe', 'pf-orphan-card')

        text = '\n'.join(pipeline.registry().verify())

        self.assertIn("feature 'probe'", text)
        self.assertIn("the card 'pf-orphan-card'", text)
        self.assertIn('no installed declaration names', text)

    # ── (d) the implementation, really called ───────────────────────────────
    def test_an_implementation_that_raises_names_the_feature(self):
        """One real call against a stand-in project: the implementation may answer or say "not
        known", and it may not raise -- a raise at the timing is what the runtime would swallow.
        """
        problems = self.one(impl='def offer(ctx):\n    raise RuntimeError("boom")\n')

        text = '\n'.join(problems)
        self.assertIn("feature 'probe'", text)
        self.assertIn('offer() raised on a stand-in project', text)
        self.assertIn('boom', text)

    def test_an_answer_carrying_a_command_shaped_list_names_the_feature(self):
        """A feature declares a node; the graph edit is the core's, so a command-shaped list in a key
        the core reads (`steps`, here) is a second spelling of the edit -- and the validator refuses
        it, whatever verb it names, because the vocabulary has one home.
        """
        problems = self.one(impl=(
            'def offer(ctx):\n'
            '    return {"title": "a node", "tags": ["probe:minutes"],\n'
            '            "spec_of": lambda node_id: "# a node\\n",\n'
            '            "steps": [["rm", "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"]]}\n'))

        text = '\n'.join(problems)
        self.assertIn("feature 'probe'", text)
        self.assertIn("carries a command-shaped list in 'steps'", text)
        self.assertIn("'rm'", text, 'the answer that carried it is quoted back')

    def test_a_two_dimensional_list_in_fields_is_no_problem(self):
        """`fields` is the free-shape dict a reader is shown, and the core only asks it to be a dict:
        a list of pairs in it is a legitimate field value, so the validator leaves it alone.
        """
        self.assertEqual([], self.one(impl=(
            'def offer(ctx):\n'
            '    return {"title": "a node", "tags": ["probe:minutes"],\n'
            '            "spec_of": lambda node_id: "# a node\\n",\n'
            '            "fields": {"pairs": [["a", "b"], ["c", "d"]]}}\n')))

    def test_a_well_shaped_answer_is_silent(self):
        """An offer whose shape the core reads is no problem: the validator is not a second shape
        check, and an offer it does not touch is the one the runtime places.
        """
        self.assertEqual([], self.one(impl=(
            'def offer(ctx):\n'
            '    return {"title": "a node", "tags": ["probe:minutes"],\n'
            '            "spec_of": lambda node_id: "# a node\\n"}\n')))

    def test_the_live_call_starts_no_child_process(self):
        """The stand-in context answers the two roads to a child process without taking either.

        The implementation here would create a marker file through either road if one of them really
        ran; the marker's absence is what says the validator performs no graph edit and opens no
        session to find out whether an implementation lies.
        """
        root = self.features()
        marker = root / 'a-child-process-ran'
        folder = write_feature(root, 'probe', declaration(CARDED), (
            'import sys\n\n\n'
            'def offer(ctx):\n'
            f'    ctx.run([sys.executable, "-c", "open({str(marker)!r}, \'w\').write(\'x\')"])\n'
            f'    ctx.capture([sys.executable, "-c", "open({str(marker)!r}, \'w\').write(\'x\')"])\n'
            '    return None\n'))
        write_card(folder, CARDED)

        self.assertEqual([], pipeline.registry().verify())
        self.assertFalse(marker.exists(), 'the validator ran a command through the context')

    # ── the route never asks ────────────────────────────────────────────────
    def test_the_route_never_asks_the_validator(self):
        """Nothing the pipeline's route runs calls the validator: the failure isolation rule is that
        somebody's project must not be stopped mid-run by a feature that is broken.

        Read off the sources rather than trusted: every module of this tool except the registry's own
        definition -- and the doctor, which lives in `tools/bin/` -- has to be silent about it.
        """
        callers = sorted(path.relative_to(CONDUCT_DIR).as_posix()
                         for path in shipped_sources()
                         if CONDUCT_DIR in path.parents and 'verify(' in path.read_text(encoding='utf-8'))
        self.assertEqual(['pipeline/feature_registry.py'], callers,
                         'the validator is asked somewhere on the route')
