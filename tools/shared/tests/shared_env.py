"""一个一次性项目，加上把 `tools/shared/` 里那些件按路径取回来的取件方式。

自带：`tools/shared/` 是别的工具从外面驱动的那一层，它的套件就不从别的工具的夹具里取件 —— 夹具
住在消费者隔壁，这一层就再也无法单独自证。这里只有三件事：定位这棵树、按路径取一份模块、造一个
临时项目。
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

__all__ = ['HERE', 'NODE', 'SHARED_DIR', 'TOOLS_DIR', 'SharedCase', '_child_env', '_fresh_module',
           '_shared', 'shared_loader']

HERE = Path(__file__).resolve().parent
SHARED_DIR = HERE.parent
TOOLS_DIR = SHARED_DIR.parent

#: 这些用例只需要一个节点 id，便条自己的形状不认它：写死一处，省得每条用例各拼一个。
NODE = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa'

_SHARED_LOADER = None


def _fresh_module(name, path):
    """一个文件按路径加载出来的一份新拷贝，别人手上没有。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def shared_loader():
    """`tools/shared/loader.py`，按路径加载并留在本进程里。

    留在进程里的理由与工具自己的一样：`reuse` 只在每个调用方都走同一个装载器时才还回同一个模块
    对象，而项目那把跨进程锁的可重入记录是一个进程一份 —— 第二个装载器会悄悄地把第二个模块交给
    同一个文件。
    """
    global _SHARED_LOADER
    if _SHARED_LOADER is None:
        _SHARED_LOADER = _fresh_module('pf_shared_loader', SHARED_DIR / 'loader.py')
    return _SHARED_LOADER


def _shared(name, filename):
    """`tools/shared/` 里的一个文件，作为本进程唯一的那一份。"""
    return shared_loader().load_by_path(name, SHARED_DIR / filename, register=True, reuse=True)


def _child_env():
    """本套件起的每个子进程都拿到 UTF-8 的流，以及那个「不真开会话」的开关。"""
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['DSH_CONDUCT_SPAWN_DRY_RUN'] = '1'
    return env


class _Sandbox:
    """一个一次性项目根：`<tmp>/`，里面只有用例自己写的东西。"""

    def __init__(self, root):
        self.root = root


class SharedCase(unittest.TestCase):
    """一条用例一个临时目录，要几个再要几个。

    `make()` 交回来的对象带 `root` —— 和别的套件交出来的那个把手一样，所以从别处搬过来的用例，
    自己的正文（`sb = self.make()` / `sb.root`）一个字都不用动。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-shared-case-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def make(self):
        tmp = tempfile.TemporaryDirectory(prefix='pf-shared-project-')
        self.addCleanup(tmp.cleanup)
        return _Sandbox(Path(tmp.name))