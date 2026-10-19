"""`tools/shared/loader.py`：装载器的三个旋钮、包自解析，以及 `load_shared` 那道门。"""

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
    """`load_shared(stem)`：公共件的那道门 —— 落点、一个进程一份、注册名、拼错要响。"""

    def test_the_door_lands_on_the_file_that_stem_names(self):
        """`load_shared('jsonio')` 交回来的就是 `shared/jsonio.py`，而且是执行过的那一份。

        挡的是落点与 stem 对不上（拼错文件名、多算一层目录）：那份被交回来的文件只要有办法执行，
        调用方就会拿它当这一件用下去，直到某次属性缺失才在远处现形。
        """
        mod = shared_loader().load_shared('jsonio')

        self.assertEqual((SHARED_DIR / 'jsonio.py').resolve(), Path(mod.__file__).resolve(),
                         '这道门没有落在共享目录里那一份同名文件上')
        self.assertTrue(hasattr(mod, 'atomic_write_json'),
                        '交回来的模块没执行过：`atomic_write_json` 不在它身上')

    def test_one_copy_per_process_however_often_you_ask(self):
        """同一个 stem 连调两次是同一个模块对象，且它就是这个进程在 `sys.modules` 里的那一份。

        挡的是两次调用拿到两份模块对象：`load_shared` 的这一份是进程态的正主（锁的可重入记录、
        `bind_paths` 那类绑定都挂在模块上），多出第二份时调用方各自以为在说同一件事，而它们已经
        分家 —— 内层调用会被自己那把锁挡在门外，路径绑定也会各指各的项目。
        """
        mod = shared_loader().load_shared('jsonio')
        again = shared_loader().load_shared('jsonio')

        self.assertIs(mod, again, '第二次取件交回来的是另一份模块对象')
        self.assertIs(sys.modules['pf_shared_jsonio'], mod,
                      '这道门的注册名与 sys.modules 里那一份对不上')

    def test_a_hyphen_in_the_stem_stays_out_of_the_registered_name(self):
        """带连字符的 stem 落在 `session-note.py` 上，注册名去掉连字符，且再取一次还是那一份。

        挡的是连字符被拼进 `sys.modules` 的键：第一次取件看着一切正常，第二次取件照着一个不含
        连字符的钥匙去找、找不到就再执行一遍 —— 于是同一个文件在一个进程里有了两份，漂移只在
        下一次取件时才现形。
        """
        mod = shared_loader().load_shared('session-note')

        self.assertEqual((SHARED_DIR / 'session-note.py').resolve(), Path(mod.__file__).resolve())
        self.assertIs(sys.modules['pf_shared_sessionnote'], mod,
                      '注册名里的连字符没被去掉：下一次取件会另执行一份')
        self.assertIs(shared_loader().load_shared('session-note'), mod,
                      '带连字符的 stem 第二次取件交回来的是另一份模块对象')

    def test_the_door_and_load_by_path_hand_back_one_copy(self):
        """这道门与直接 `load_by_path(..., register=True, reuse=True)` 交出的是同一个对象。

        挡的是两道门各给一份：公共件在这棵树里是「一个进程一份」，而 monkeypatch 正本属性的用例
        （`conduct/tests/cases_shared.py` 里 `.pf` 一改名标记跟着走那条）依赖的就是正本只有一份 ——
        两道门给出两份时，补丁打在其中一份上，被测的那条路读另一份，用例会静默地测了个空壳。
        """
        via_door = shared_loader().load_shared('ledger')
        via_knobs = shared_loader().load_by_path('pf_shared_ledger', SHARED_DIR / 'ledger.py',
                                                 register=True, reuse=True)

        self.assertIs(via_door, via_knobs,
                      '两道门各交出一份 `shared/ledger.py`：补丁与读取分了家')

    def test_a_stem_that_names_no_file_raises(self):
        """拼错的 stem 当场响（`FileNotFoundError`），不许静默交回一个空模块。

        挡的是把「这个公共件不存在」降级成一个空壳：调用方拿到的模块没有它要的那个名字，属性
        缺失会在离拼错处很远的地方炸，或者更糟 —— 被某个 `getattr` 缺省收下，命令带着一半的
        能力继续跑。
        """
        with self.assertRaises(FileNotFoundError):
            shared_loader().load_shared('no-such-shared-file')