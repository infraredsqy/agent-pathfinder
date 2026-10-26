"""The ground every `cases_*` module stands on: the loaded pipeline module, the node ids the cases
reuse, a few small helpers, and `Base`.

`cases_*` modules import `Base` and the constants from here and the harness pieces they need from
`harness` directly. `pipeline` is deliberately shared: `load_pipeline()` caches one module object, and the
cases patch attributes on it (`pipeline._run`, `pipeline.CMD_TIMEOUT`), so a second copy would silently
stop being the module under test.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Driver, PIPELINE_PATH, STATE_MANAGE, Sandbox, load_pipeline, run_argv

__all__ = ['ACTION_VOCABULARY', 'Base', 'NODE', 'NODE2', 'NODE3', 'pipeline', 'normalise', 'skill_of']

pipeline = load_pipeline()

NODE = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa'
NODE2 = 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb'
NODE3 = 'cccccccc-3333-4333-8333-cccccccccccc'

# No `pause`: nothing produces one. The vocabulary lives in `conduct_pipeline.ACTIONS`; these names are
# the ones a caller must be ready to handle, and `test_actions_are_the_whole_vocabulary` keeps the
# two in step. `open_node` is the planner's own step (it carries the `cycle hold` command), and
# `insert_node` is an insertion source's step (the graph grew a node; nothing was opened and the new
# node is not pinned). No caller ever receives `open_node`, and `insert_node` reaches one only
# as the answer to its own closing call -- what follows it is the next dispatch's pick.
ACTION_VOCABULARY = {'dispatch', 'open_node', 'insert_node', 'stop_pipeline', 'wait', 'ask_user',
                     'no_cycle'}


def skill_of(action):
    """The skill a dispatch asks for, off the invocation line of its prompt.

    A dispatch action names the NODE it is for, not the skill: with one node per step there is
    nothing left for a skill index to disambiguate (the old per-stage table is gone). The skill is
    still a hard contract -- the child is invoked as `/pf-conduct <node> [pipeline]` -- so it is
    read off the first line of `prompt_text`, which is where it is promised.
    """
    first = next((line for line in (action.get('prompt_text') or '').splitlines() if line.strip()),
                 '')
    return first.split()[0].lstrip('/') if first.startswith('/') else ''


def normalise(value):
    """What legitimately differs between two runs of the same thing.

    Timestamps, and absolute paths -- two sandboxes live in two temp directories, so a recorded
    absolute path differs by construction. Nothing else may.
    """
    if isinstance(value, dict):
        return {k: ('<ts>' if k.endswith('_at') or k in ('at', 'timestamp', 'since')
                    else normalise(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [normalise(v) for v in value]
    if isinstance(value, str) and ('\\Temp\\' in value or '/Temp/' in value):
        return '<tmp>'
    return value


def added(proc) -> str:
    """`dag-manage add` prints the new uuid on its first line."""
    return proc.stdout.splitlines()[0].split(': ')[1].strip()


class Base(unittest.TestCase):
    def make(self, **kw):
        sb = Sandbox(**kw)
        self.addCleanup(sb.close)
        return sb

    def cli(self, sb, *args):
        """The shipped `state-manage.py`, as a real process against the sandbox."""
        return run_argv([STATE_MANAGE, *args], cwd=sb.root, check=False)

    def pipeline_cli(self, sb, *args):
        """`conduct_pipeline.py` as a real process: the module's own entry, driven the shipped way."""
        return run_argv([PIPELINE_PATH, *args], check=False)

    def run_to_stop(self, sb, cycle='cyc', closer=None, worker=None, max_steps=80):
        driver = Driver(sb, cycle, closer=closer, worker=worker, max_steps=max_steps)
        return driver.run(), driver

    def ready_sandbox(self, extra_nodes=0, cli=False):
        """One auto node, pinned by a started cycle; `extra_nodes` more, added afterwards.

        The extras land after the pin, so the cycle is mid-line from the fixture's first view.
        """
        sb = self.make(cli=cli)
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.start_cycle()
        for i in range(extra_nodes):
            sb.add_node((NODE2, NODE3)[i], f'another line {i}', mode='auto')
        return sb
