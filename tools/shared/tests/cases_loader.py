"""`tools/shared/loader.py`: the loader's three knobs, package self-resolution, and the
`load_shared` door."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from shared_env import SHARED_DIR, shared_loader

__all__ = ['TestTheOneLoader', 'TestTheSharedDoor']


class TestTheOneLoader(unittest.TestCase):
    """`tools/shared/loader.py`: the three knobs, and what each one is for."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-loader-case-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.loader = shared_loader()

    def _source(self, filename='pf_case_module.py', body='VALUE = 1\n'):
        path = self.root / filename
        path.write_text(body, encoding='utf-8')
        return path

    def test_the_default_is_a_copy_of_its_own_that_touches_nothing(self):
        path = self._source()

        first = self.loader.load_by_path('pf_case_private', path)
        second = self.loader.load_by_path('pf_case_private', path)

        self.assertIsNot(first, second, 'each load at the default is a module of its own')
        self.assertEqual(1, first.VALUE)
        self.assertNotIn('pf_case_private', sys.modules, 'the default load registers nothing')

    def test_fresh_drops_what_an_earlier_load_registered(self):
        path = self._source()
        first = self.loader.load_by_path('pf_case_fresh', path, register=True)
        self.assertIs(sys.modules['pf_case_fresh'], first)

        second = self.loader.load_by_path('pf_case_fresh', path, register=True, fresh=True)

        self.assertIsNot(second, first, 'fresh shares no module object with the last load')
        self.assertIs(sys.modules['pf_case_fresh'], second)

    def test_reuse_hands_back_the_copy_already_registered_for_this_file(self):
        path = self._source()

        first = self.loader.load_by_path('pf_case_reuse', path, register=True, reuse=True)
        second = self.loader.load_by_path('pf_case_reuse', path, register=True, reuse=True)

        self.assertIs(first, second, 'reuse is how one process keeps one copy of a file')

        other = self._source('pf_case_reuse_other.py', 'VALUE = 2\n')
        third = self.loader.load_by_path('pf_case_reuse', other, register=True, reuse=True)

        self.assertIsNot(third, first, 'another file under the name is a module, not a reuse')
        self.assertEqual(2, third.VALUE)

    def test_a_package_resolves_its_own_submodules(self):
        package = self.root / 'pf_case_package'
        package.mkdir()
        (package / '__init__.py').write_text('from . import leaf\nMARK = leaf.MARK\n',
                                             encoding='utf-8')
        (package / 'leaf.py').write_text("MARK = 'leaf'\n", encoding='utf-8')

        module = self.loader.load_by_path('pf_case_package', package / '__init__.py',
                                          package_dir=package)

        self.assertEqual('leaf', module.MARK)
        self.assertIs(sys.modules['pf_case_package'], module, 'a package is registered to load')


class TestTheSharedDoor(unittest.TestCase):
    """`load_shared(stem)`: the door onto the shared pieces -- where it lands, one copy per process,
    the registered name, and a typo that has to speak up."""

    def test_the_door_lands_on_the_file_that_stem_names(self):
        """`load_shared('jsonio')` hands back exactly `shared/jsonio.py`, and the copy that was
        executed.

        What this holds off: the landing place and the stem disagreeing (a misspelled filename, one
        directory too many). As long as the file handed back can be executed at all, the caller goes
        on treating it as this piece, and the mistake only surfaces far away, as a missing
        attribute.
        """
        mod = shared_loader().load_shared('jsonio')

        self.assertEqual((SHARED_DIR / 'jsonio.py').resolve(), Path(mod.__file__).resolve(),
                         'this door did not land on the file of that name in the shared directory')
        self.assertTrue(hasattr(mod, 'atomic_write_json'),
                        'the module handed back was never executed (no `atomic_write_json`)')

    def test_one_copy_per_process_however_often_you_ask(self):
        """Two calls in a row with the same stem are one module object, and it is this process's
        copy in `sys.modules`.

        What this holds off: two calls getting two module objects. `load_shared`'s copy is the one
        that owns the process state (the lock's reentrant record, the `bind_paths`-style bindings
        all hang off the module); with a second copy around, callers each believe they are talking
        about the same thing while they have already parted ways -- the inner call gets shut out by
        its own lock, and the path bindings point at a different project each.
        """
        mod = shared_loader().load_shared('jsonio')
        again = shared_loader().load_shared('jsonio')

        self.assertIs(mod, again, 'the second fetch handed back another module object')
        self.assertIs(sys.modules['pf_shared_jsonio'], mod,
                      'the name this door registered does not match the one in sys.modules')

    def test_a_hyphen_in_the_stem_stays_out_of_the_registered_name(self):
        """A stem with a hyphen lands on `session-note.py`, the registered name drops the hyphen,
        and fetching it once more is still that one copy.

        What this holds off: the hyphen being spelled into the `sys.modules` key. The first fetch
        looks perfectly normal, and the second one looks the file up under a hyphen-free key, finds
        nothing and executes it again -- so one file has two copies in one process, and the drift
        only surfaces on the next fetch.
        """
        mod = shared_loader().load_shared('session-note')

        self.assertEqual((SHARED_DIR / 'session-note.py').resolve(), Path(mod.__file__).resolve())
        self.assertIs(sys.modules['pf_shared_sessionnote'], mod,
                      'the registered name kept the hyphen: the next fetch runs a second copy')
        self.assertIs(shared_loader().load_shared('session-note'), mod,
                      'the second fetch of the hyphenated stem handed back another module object')

    def test_the_door_and_load_by_path_hand_back_one_copy(self):
        """This door and a direct `load_by_path(..., register=True, reuse=True)` hand back the same
        object.

        What this holds off: the two doors giving a copy each. A shared piece is "one copy per
        process" in this tree, and the case that monkeypatches the source's attributes (the one in
        `conduct/tests/cases_shared.py` where renaming `.pf` takes the marker along) depends on
        there being only one copy of the source -- with two, the patch lands on one of them, the
        path under test reads the other, and the case silently tests an empty shell.
        """
        via_door = shared_loader().load_shared('ledger')
        via_knobs = shared_loader().load_by_path('pf_shared_ledger', SHARED_DIR / 'ledger.py',
                                                 register=True, reuse=True)

        self.assertIs(via_door, via_knobs,
                      'the two doors gave a `shared/ledger.py` each: patch and read parted ways')

    def test_a_stem_that_names_no_file_raises(self):
        """A misspelled stem speaks up on the spot (`FileNotFoundError`) -- never a silent empty
        module.

        What this holds off: degrading "this shared piece does not exist" into an empty shell. The
        module the caller gets does not carry the name it asked for, and the missing attribute blows
        up far away from the typo -- or worse, some `getattr` default swallows it and the command
        carries on with half its abilities.
        """
        with self.assertRaises(FileNotFoundError):
            shared_loader().load_shared('no-such-shared-file')