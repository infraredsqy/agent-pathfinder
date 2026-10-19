"""`tools/shared/`'s own suite: this layer's cases, taking no fixture from another tool.

How to run it (either way works):

    python -m unittest discover -s shared/tests -t .      (cwd in tools/)
    python shared/tests/test_shared.py

This file is the one collection point: the cases live in groups under `cases_*.py` and are
star-imported one by one, so this is the file discover reaches. (`cases_*` also falls outside
discover's `test*.py` and pytest's `test_*.py`, so the same batch of cases is never run twice.)
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