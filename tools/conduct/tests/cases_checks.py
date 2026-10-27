"""The validator: one declaration against the implementation beside it.

`pipeline/plugin_registry.py` answers two kinds of question. `validate()` reads the declaration
alone -- is the timing one the core knows, the action, the knob kinds, do two plugins keep off each
other's names. `verify()` is the validator and asks the other side as well: is there a skill for every
method the metrics name (a skill lives in the plugin folder that binds it, under `skills/<lang>/`),
and does the implementation answer the way its declaration says it will -- one real call against a
stand-in project, which may answer (an offer, a reason) or "not known this pass", may never raise, and
may not carry a command-shaped list in a key the core reads (the edit has one implementation, and it
is the core's).

What this file pins, in the order the criteria are stated:

  * the installed tree validates, and the shipped folders are inside the closed sets;
  * a declaration naming a timing or a knob kind the core does not know is a *problem*, and the
    sentence names the plugin (which is the half a human acts on);
  * two plugins may not claim one name (a knob, a reading, a dedupe tag) -- and they may share a
    method, because one skill can serve two debts;
  * a method needs its `SKILL.md` in a skill folder some installed plugin ships, one that has it is
    silent, and a skill folder whose method nothing names is a problem in the other direction;
  * an implementation that raises at its door, or that answers with a command-shaped list in a key
    the core reads, is named -- and the stand-in project starts no child process;
  * and the pipeline's route never asks any of it: the two callers are this suite and `pf doctor`.

Every case builds its own plugins directory (`PF_PLUGINS_DIR`), so nothing here depends on the
folders this checkout ships; a case whose subject IS a shipped folder skips itself when it is gone.
"""

from __future__ import annotations

import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, pipeline  # noqa: E402
from cases_maintenance import POOL, TICKET  # noqa: E402
from harness import (CONDUCT_DIR, plugins_dir, requires_plugin, shipped_sources,  # noqa: E402
                     write_skill, write_plugin)

__all__ = ['TestTheValidator']

#: The method name every fixture's skill is written under. A case that needs a method with a skill asks
#: the fixture for one instead of reading a name off the shipped tree: the skill lives in the plugin
#: folder beside the declaration, so the fixture writes both.
CARDED = 'pf-skill-fixture'

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
    """`DECL` with the one method skill it names, and any cell a case replaces."""
    decl = {key: (dict(value) if isinstance(value, dict) else value) for key, value in DECL.items()}
    decl['metrics'] = {key: dict(cell) for key, cell in DECL['metrics'].items()}
    for cell in decl['metrics'].values():
        cell['method'] = method
    decl.update(over)
    return decl


class TestTheValidator(Base):
    """One temp plugins directory per case, and the validator asked about it in this process."""

    def plugins(self):
        """A throwaway plugins directory, entered for the case's whole life.

        One per case: a second call hands the same directory back, so a case that writes a second
        fixture (or a second skill) after calling `one()` is still writing into the tree the scan is
        pointed at.
        """
        root = getattr(self, '_features_root', None)
        if root is None:
            tmp = tempfile.TemporaryDirectory(prefix='conduct-checks-')
            self.addCleanup(tmp.cleanup)
            stack = ExitStack()
            self.addCleanup(stack.close)
            stack.enter_context(plugins_dir(tmp.name))
            root = self._features_root = Path(tmp.name)
        return root

    def one(self, method=None, impl=QUIET_IMPL, skill=True, **over):
        """One plugin folder named after the declaration's own name, and the validator's answer.

        `skill` writes the skill the method names into the same folder: the plugin folder is a skill's
        one home, so "the method is backed" is the fixture writing both halves.
        """
        root = self.plugins()
        name = over.pop('name', 'probe')
        method = method or CARDED
        folder = write_plugin(root, name, declaration(method, name=name, **over), impl)
        if skill:
            write_skill(folder, method)
        return pipeline.registry().verify()

    # ── the installed tree ──────────────────────────────────────────────────
    def test_the_installed_tree_validates(self):
        """Whatever this checkout ships passes the validator, with no plugin at all included.

        A plugins directory with nothing in it is a legal project: the core is whole without any
        plugin, and the validator then has nothing to ask.
        """
        self.assertEqual([], pipeline.registry().verify())

    @requires_plugin(TICKET.plugin, POOL.plugin)
    def test_the_shipped_declaration_sits_in_the_closed_sets(self):
        """The shipped declarations are inside the tables the core serves, and their methods have
        skills.

        Read off the registry itself, not restated here: a declaration that drifted out of the three
        cells would be a plugin the runtime never asks, and the validator is what says so. Both
        shipped folders are asked for, because this case's subject is the tree as it ships -- one
        folder gone is a different (supported) state, and the cases that produce it say so themselves.
        """
        registry = pipeline.registry()
        declared = {(plugin['decl']['timing'], plugin['decl']['action'])
                    for plugin in registry.load_all() if isinstance(plugin.get('decl'), dict)}
        self.assertTrue(declared <= set(registry.CELLS), declared - set(registry.CELLS))
        skills = registry.skills()
        for method in registry.methods():
            self.assertIn(method, skills, f'{method} has no skill in any installed plugin folder')
            self.assertTrue(any((folder / registry.SKILL_FILE).is_file()
                                for _owner, folder in skills[method]),
                            f'{method} has no {registry.SKILL_FILE}')

    # ── (a) the closed sets, and the name in the sentence ───────────────────
    def test_a_timing_or_a_kind_outside_the_closed_sets_names_the_feature(self):
        """A timing the core does not serve and a knob kind it does not know are problems, and the
        sentence names the plugin.
        """
        problems = self.one(timing='whenever',
                            knobs={'probe_limit': {'kind': 'fifth'}})

        text = '\n'.join(problems)
        self.assertIn("plugin 'probe'", text, 'the problem has to name the plugin')
        self.assertIn("timing 'whenever' is not one of", text)
        self.assertIn("kind 'fifth'", text)

    def test_an_action_pair_outside_the_two_axes_is_a_problem(self):
        """The three served cells are the whole table: `insert` at a dispatch is a declaration the
        runtime would skip forever, so it is reported rather than accepted.
        """
        problems = self.one(timing='dispatch', action='insert')

        self.assertIn("plugin 'probe'", '\n'.join(problems))
        self.assertIn('is not a cell this core serves', '\n'.join(problems))

    def test_a_half_stated_cell_names_the_feature(self):
        """The timing and the action are one cell: stated whole, or not at all.

        Half of it is not a smaller declaration -- it names a thing nothing ever serves -- so the
        validator names the half that is missing rather than taking it for "no timing at all". This is
        the reading of a declaration that brings `pf` a word of its own: it is asked at no moment of the
        pipeline, so stating neither half is legal, and stating one half alone is what this names. A
        declaration that brings no word keeps occupying the cell it was written for (the case below).
        """
        word = {'probe-cmd': {'summary': 'the fixture line'}}
        missing_action = self.one(action=None, commands=word)
        missing_timing = self.one(timing=None, commands=word)

        self.assertIn('timing and action travel together', '\n'.join(missing_action))
        self.assertIn('timing and action travel together', '\n'.join(missing_timing))

    def test_a_declaration_that_brings_no_word_still_occupies_a_cell(self):
        """Only a declaration that brings `pf` a word of its own is spared the cell.

        With no word to bring it is asked at a moment of the pipeline, so stating neither half is the
        two problems it always was: the three shapes a declaration can have without a word -- knobs
        only, a name and nothing else, a metric -- are each named, and what is named is the half that
        is absent.
        """
        for problems in (
            self.one(name='knob-only', timing=None, action=None, readings={}, metrics={}),
            self.one(name='name-only', timing=None, action=None, knobs={}, readings={}, metrics={}),
            self.one(name='metric-only', timing=None, action=None, knobs={}, readings={},
                     metrics={'probe_minutes': {'line': 'probe_limit', 'tag': 'probe:minutes',
                                                'method': None, 'order': 0}}),
        ):
            text = '\n'.join(problems)
            self.assertIn("timing None is not one of ['close', 'pick', 'dispatch']", text)
            self.assertIn("action None is not one of ['insert', 'block']", text)

    # ── (a3) the command face: a word, a line, whether it travels ───────────
    def test_a_command_only_feature_occupies_no_cell_and_is_legal(self):
        """A declaration may carry words for `pf` instead of a timing: it is asked at no moment of the
        pipeline, so there is no cell to serve, and the validator has nothing to say about it.
        """
        problems = self.one(timing=None, action=None,
                            commands={'probe-cmd': {'summary': 'the fixture line'}})

        self.assertEqual([], problems)

    def test_a_command_cell_that_cannot_become_one_names_the_feature(self):
        """Four ways a declared command is not a command, each one named: the cell is not an object,
        the key is not a word `pf` could be handed, there is no line for the help to print, and the
        `keep_name` cell is not the true/false it has to be.
        """
        problems = self.one(timing=None, action=None, commands={
            'probe-cmd': 'go',
            'two words': {'summary': 'the fixture line'},
            'silent': {'summary': '   '},
            'loud': {'summary': 'the fixture line', 'keep_name': 'yes'},
        })

        text = '\n'.join(problems)
        self.assertIn("plugin 'probe'", text)
        self.assertIn("command 'probe-cmd' must be an object", text)
        self.assertIn("command 'two words' is not a word", text)
        self.assertIn("command 'silent' needs a summary", text)
        self.assertIn("command 'loud' takes keep_name as true or false", text)

    def test_a_word_pf_could_never_be_handed_is_not_a_command(self):
        """The word's shape is the one `pf` matches its first argv against: one token, and not an
        option. A well-shaped cell passes either way -- whether the word travels is the declaration's
        own cell -- while a blank inside a word and a word opening with a dash are each named.
        """
        self.assertEqual([], self.one(timing=None, action=None,
                                      commands={'probe-cmd': {'summary': 'the fixture line'}}))
        self.assertEqual([], self.one(timing=None, action=None,
                                      commands={'probe-cmd': {'summary': 'the fixture line',
                                                              'keep_name': False}}))

        text = '\n'.join(self.one(timing=None, action=None, commands={
            '-probe': {'summary': 'the fixture line'},
            'probe cmd': {'summary': 'the fixture line'}}))
        self.assertIn("command '-probe' is not a word", text)
        self.assertIn("command 'probe cmd' is not a word", text)

    # ── (b) two declarations, one name ──────────────────────────────────────
    def test_two_plugins_may_not_claim_one_name(self):
        """A knob, a reading, a dedupe tag and a skill belong to one plugin each; every sentence names
        both claimants.

        A skill name is the plugin's own: two declarations naming one method, with the skill sitting in
        one of the two folders, leaves the other naming something it does not carry.
        """
        root = self.plugins()
        method = CARDED
        write_plugin(root, 'one', declaration(method, name='one'), QUIET_IMPL)
        write_plugin(root, 'two', declaration(method, name='two'), QUIET_IMPL)
        write_skill(root / 'one', method)

        problems = pipeline.registry().verify()
        text = '\n'.join(problems)

        self.assertIn("knob 'probe_limit' is claimed by both 'one' and 'two'", text)
        self.assertIn("reading 'probe_minutes' is claimed by both 'one' and 'two'", text)
        self.assertIn("dedupe tag 'probe:minutes' is claimed by both 'one' and 'two'", text)
        self.assertIn("plugin 'two': method", text)
        self.assertIn('has no skill of its own', text)
        self.assertEqual(4, len(problems), text)

    def test_two_plugins_may_not_ship_one_skill(self):
        """The other face of the same rule: both folders carrying the name is a loan too, and it is
        what would make `~/.agents/skills/<name>` ambiguous. The sentence names both folders.
        """
        root = self.plugins()
        method = CARDED
        write_plugin(root, 'one', declaration('pf-one', name='one'), QUIET_IMPL)
        write_plugin(root, 'two', declaration('pf-two', name='two'), QUIET_IMPL)
        write_skill(root / 'one', method)
        write_skill(root / 'two', method)

        text = '\n'.join(pipeline.registry().verify())

        self.assertIn(f"skill {method!r}", text)
        self.assertIn("'one' ships it under skills/en", text)
        self.assertIn("'two' under skills/en", text)
        self.assertIn('a skill belongs to one plugin', text)

    # ── (c) the method skill ─────────────────────────────────────────────────
    def test_a_method_without_a_card_names_the_feature(self):
        """A method whose skill no plugin folder carries: the generated node would point a session at
        nothing, so the validator says which method and which plugin.
        """
        problems = self.one(method='pf-no-such-skill', skill=False)

        text = '\n'.join(problems)
        self.assertIn("plugin 'probe'", text)
        self.assertIn("method 'pf-no-such-skill' has no skill", text)

    def test_a_method_that_has_a_card_is_silent(self):
        """The other half: a method the folder beside it carries a skill for is no problem at all."""
        self.assertEqual([], self.one())

    def test_a_skill_no_declaration_names_is_the_plugins_own(self):
        """The inventory direction: a plugin's folders are what it ships, so a skill no metric names is
        a skill the user invokes -- `pf doctor` lists it with the plugin that carries it, and the
        validator says nothing about it.
        """
        root = self.plugins()
        self.assertEqual([], self.one())
        write_skill(root / 'probe', 'pf-extra-skill')

        self.assertEqual([], pipeline.registry().verify())

    # ── (d) the implementation, really called ───────────────────────────────
    def test_an_implementation_that_raises_names_the_feature(self):
        """One real call against a stand-in project: the implementation may answer or say "not
        known", and it may not raise -- a raise at the timing is what the runtime would swallow.
        """
        problems = self.one(impl='def offer(ctx):\n    raise RuntimeError("boom")\n')

        text = '\n'.join(problems)
        self.assertIn("plugin 'probe'", text)
        self.assertIn('offer() raised on a stand-in project', text)
        self.assertIn('boom', text)

    def test_an_answer_carrying_a_command_shaped_list_names_the_feature(self):
        """A plugin declares a node; the graph edit is the core's, so a command-shaped list in a key
        the core reads (`steps`, here) is a second spelling of the edit -- and the validator refuses
        it, whatever verb it names, because the vocabulary has one home.
        """
        problems = self.one(impl=(
            'def offer(ctx):\n'
            '    return {"title": "a node", "tags": ["probe:minutes"],\n'
            '            "spec_of": lambda node_id: "# a node\\n",\n'
            '            "steps": [["rm", "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"]]}\n'))

        text = '\n'.join(problems)
        self.assertIn("plugin 'probe'", text)
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
        root = self.plugins()
        marker = root / 'a-child-process-ran'
        folder = write_plugin(root, 'probe', declaration(CARDED), (
            'import sys\n\n\n'
            'def offer(ctx):\n'
            f'    ctx.run([sys.executable, "-c", "open({str(marker)!r}, \'w\').write(\'x\')"])\n'
            f'    ctx.capture([sys.executable, "-c", "open({str(marker)!r}, \'w\').write(\'x\')"])\n'
            '    return None\n'))
        write_skill(folder, CARDED)

        self.assertEqual([], pipeline.registry().verify())
        self.assertFalse(marker.exists(), 'the validator ran a command through the context')

    # ── the route never asks ────────────────────────────────────────────────
    def test_the_route_never_asks_the_validator(self):
        """Nothing the pipeline's route runs calls the validator: the failure isolation rule is that
        somebody's project must not be stopped mid-run by a plugin that is broken.

        Read off the sources rather than trusted: every module of this tool except the registry's own
        definition -- and the doctor, which lives in `tools/bin/` -- has to be silent about it.
        """
        callers = sorted(path.relative_to(CONDUCT_DIR).as_posix()
                         for path in shipped_sources()
                         if CONDUCT_DIR in path.parents and 'verify(' in path.read_text(encoding='utf-8'))
        self.assertEqual(['pipeline/plugin_registry.py'], callers,
                         'the validator is asked somewhere on the route')
