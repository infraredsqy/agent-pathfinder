"""`tools/shared/loader.py`：装载器的三个旋钮，以及包自解析。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from shared_env import shared_loader

__all__ = ['TestTheOneLoader']


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