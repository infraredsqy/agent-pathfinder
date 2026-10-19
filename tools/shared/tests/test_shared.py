"""`tools/shared/` 自己的套件：这一层的用例，不从别的工具的夹具里取件。

跑法（两种都行）：

    python -m unittest discover -s shared/tests -t .      （cwd 在 tools/）
    python shared/tests/test_shared.py

这个文件是唯一的收集点：用例按组住在 `cases_*.py` 里，逐个 star-import 进来，所以 discover
走到的就是这一份。（`cases_*` 这个名字同时落在 discover 的 `test*.py` 与 pytest 的 `test_*.py`
之外，同一批用例不会被跑两遍。）
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_loader import *      # noqa: E402,F401,F403
from cases_primitives import *  # noqa: E402,F401,F403
from cases_records import *     # noqa: E402,F401,F403


if __name__ == '__main__':
    unittest.main(verbosity=2)