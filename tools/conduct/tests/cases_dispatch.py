"""`state-manage.py dispatch <cycle>`: start the session for the node the cycle is holding.

Why the verb exists (and what it must NOT do): the closing call
(`next --after <node> --json`) settles a node, picks the next one and writes its prompt, but the
worker that closed its node still has to commit and push its own work -- and the next session boots
in the SAME worktree and pulls on its first line. So starting it is a separate command, run after the
push:

    next --after <node> --json     settle it, write .pf/meta/launch/<cycle>/<node>/prompt.txt
    git commit … && git push       the worker's own work
    dispatch <cycle>               start that prompt's session

The cases here pin the whole contract of that verb: it dispatches, it refuses (five shapes, each with
a line on stderr), it resolves `--project`, and it touches nothing but the session it starts.

`DSH_CONDUCT_SPAWN_DRY_RUN=1` is held by the harness around every dispatch (`Sandbox.dispatch`): the
spawn call is made and reported, and no session (and no network) is involved. The drill switch is
honoured inside `spawn_session`, which is the seam the closing call's spawn uses too.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import NODE, Base  # noqa: E402
from harness import run_state_manage  # noqa: E402

__all__ = ['TestDispatchVerb']


def same_path(left: str, right: str) -> bool:
    """Is `left` in `right`, as a path? `Path.cwd()` and the sandbox's own root can differ in case.

    The sandbox hands the tools an absolute temp path; the tools that resolve from `os.getcwd()`
    report whatever the OS calls that directory back. Same file, occasionally different spelling.
    """
    return os.path.normcase(left) in os.path.normcase(right)


class TestDispatchVerb(Base):
    """`dispatch <cycle>` -- one call, and the only thing it does is start that session."""

    def pinned(self, cli=False):
        """A cycle holding NODE, with the prompt file already on disk (what `next` writes)."""
        sb = self.ready_sandbox(cli=cli)
        action = sb.next()
        self.assertEqual('dispatch', action['action'])
        return sb, action

    # ── it dispatches ────────────────────────────────────────────────────────
    def test_it_starts_the_session_for_the_node_the_cycle_holds(self):
        for cli in (False, True):
            with self.subTest(cli=cli):
                sb, action = self.pinned(cli=cli)

                proc = sb.dispatch(cli=cli)

                self.assertEqual(0, proc.returncode, proc.stderr)
                self.assertTrue(same_path(action['prompt'], proc.stdout),
                                'the spawner line must name the prompt it used')
                self.assertTrue(os.path.exists(action['prompt']),
                                'a dry run must keep the launch dir: that file is what a human retries')

    def test_json_reports_the_cycle_the_node_and_the_spawn_line(self):
        sb, action = self.pinned()

        proc = sb.dispatch(as_json=True)

        self.assertEqual(0, proc.returncode, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual({'action', 'cycle', 'node_id', 'nodes', 'prompt', 'spawned'},
                         set(payload))
        self.assertEqual('dispatch', payload['action'])
        self.assertEqual('cyc', payload['cycle'])
        self.assertEqual(NODE, payload['node_id'])
        self.assertTrue(os.path.samefile(payload['prompt'], action['prompt']),
                        'the json must name the prompt the dispatch read')
        self.assertTrue(same_path(action['prompt'], payload['spawned']))

    def test_it_touches_nothing_but_the_spawn(self):
        """It reads the ledger to find the node, and starts a session. No status, no path, no
        `holding`, no prompt rewrite -- a dispatch that wrote state would be a second close."""
        sb, _action = self.pinned()
        before = sb.tree_hashes()

        proc = sb.dispatch()

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(before, sb.tree_hashes(), 'dispatch changed the project')
        self.assertEqual(NODE, sb.current_node(), 'dispatch moved the pointer')
        self.assertEqual([], sb.path(), 'dispatch wrote a path step')

    # ── it refuses, loudly ───────────────────────────────────────────────────
    def test_a_cycle_with_no_node_in_hand_refuses(self):
        sb = self.make()
        sb.add_node(NODE, 'UI design', mode='auto')
        sb.create_cycle()                                  # created, but nothing is pinned

        proc = sb.dispatch(check=False)

        self.assertNotEqual(0, proc.returncode)
        self.assertIn('holding no node', proc.stderr)
        self.assertIn('next --cycle cyc --json', proc.stderr)   # names the call that fixes it

    def test_a_missing_prompt_names_the_path_and_the_call_that_writes_it(self):
        sb = self.ready_sandbox()                          # pinned, but `next` never ran
        proc = sb.dispatch(check=False)

        self.assertNotEqual(0, proc.returncode)
        self.assertIn('prompt.txt', proc.stderr)
        self.assertIn(NODE, proc.stderr)
        self.assertIn('next --after %s --json' % NODE, proc.stderr)

    def test_a_cycle_out_of_service_refuses(self):
        sb, action = self.pinned()
        sb.set_cycle_status('cyc', 'inactive')

        proc = sb.dispatch(check=False)

        self.assertNotEqual(0, proc.returncode)
        self.assertIn('not active', proc.stderr)
        self.assertTrue(os.path.exists(action['prompt']), 'the refusal must not consume the prompt')

    def test_a_status_word_outside_the_vocabulary_refuses(self):
        """A status word outside the vocabulary is read as written: it is not `active`, so the door
        holds here too (forged on disk -- the CLI writes canonical)."""
        sb, action = self.pinned()
        sb.rewrite_cycle(status='paused')

        proc = sb.dispatch(check=False)

        self.assertNotEqual(0, proc.returncode)
        self.assertIn('not active', proc.stderr)
        self.assertEqual('paused', sb.cycle()['status'], 'the file keeps its own word')

    def test_an_unknown_cycle_refuses_and_names_the_project(self):
        sb = self.ready_sandbox()

        proc = sb.dispatch(cycle_id='nope', check=False)

        self.assertNotEqual(0, proc.returncode)
        self.assertIn("no cycle named 'nope'", proc.stderr)
        self.assertIn('pf', proc.stderr)                  # which ledger was consulted

    # ── it resolves the project like every other verb ────────────────────────
    def test_project_names_the_ledger_when_the_cwd_is_somewhere_else(self):
        """`--project` beats the cwd: the child process runs in an unrelated project."""
        sb, action = self.pinned()
        elsewhere = self.make()

        proc = run_state_manage(elsewhere.root, 'dispatch', 'cyc', '--project', str(sb.root))

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertTrue(same_path(action['prompt'], proc.stdout))
        self.assertFalse((elsewhere.root / '.pf' / 'meta').exists(),
                         'the other project was touched')
        self.assertIsNone(elsewhere.state(), 'the cwd project grew a ledger of its own')

    def test_the_refusal_of_a_project_that_has_no_such_cycle_names_it(self):
        sb, _action = self.pinned()
        elsewhere = self.make()

        proc = run_state_manage(elsewhere.root, 'dispatch', 'cyc', '--project', str(sb.root),
                                check=False)
        other = run_state_manage(elsewhere.root, 'dispatch', 'cyc', check=False)

        self.assertEqual(0, proc.returncode, proc.stderr)      # `--project` finds sb's cycle
        self.assertNotEqual(0, other.returncode)               # the cwd project has no such cycle
        self.assertIn("no cycle named 'cyc'", other.stderr)

    # ── the line policy names a spawner ──────────────────────────────────────
    # `cycle policy <cycle> spawner dsh|zcode` is a decision recorded on the line: dispatch reads
    # it, over this machine's environment variable and the host's own guess, yielding to a one-off
    # `--spawner`. The machine-independent assertions lean on "who alone can produce this name":
    # with the environment saying zcode, a `dsh` in argv can only come from the line policy, and a
    # path can only come from the flag.

    def test_a_line_policy_beats_the_machine_setting(self):
        for cli in (False, True):
            with self.subTest(cli=cli):
                sb, _action = self.pinned(cli=cli)
                sb.set_policy('cyc', 'spawner', 'dsh', cli=cli)

                with mock.patch.dict(os.environ, {'DSH_CONDUCT_SPAWNER': 'zcode'}):
                    proc = sb.dispatch(cli=cli)

                self.assertEqual(0, proc.returncode, proc.stderr)
                self.assertIn('dsh-spawn.ps1', proc.stdout,
                              'the line policy must name the spawner the argv uses')

    def test_without_a_policy_the_machine_setting_answers(self):
        for cli in (False, True):
            with self.subTest(cli=cli):
                sb, _action = self.pinned(cli=cli)

                with mock.patch.dict(os.environ, {'DSH_CONDUCT_SPAWNER': 'zcode'}):
                    proc = sb.dispatch(cli=cli)

                self.assertEqual(0, proc.returncode, proc.stderr)
                self.assertIn('zcode-spawn.ps1', proc.stdout,
                              'a line that named no policy changes nothing')

    def test_off_returns_the_answer_to_the_machine_setting(self):
        sb, _action = self.pinned()
        sb.set_policy('cyc', 'spawner', 'dsh')
        sb.set_policy('cyc', 'spawner', 'off')

        with mock.patch.dict(os.environ, {'DSH_CONDUCT_SPAWNER': 'zcode'}):
            proc = sb.dispatch()

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn('zcode-spawn.ps1', proc.stdout,
                      '`off` must clear the policy, not park it at its last word')

    def test_a_per_call_flag_beats_the_line_policy(self):
        sb, _action = self.pinned()
        sb.set_policy('cyc', 'spawner', 'dsh')
        flag_spawner = sb.root / 'fake-spawner.ps1'

        with mock.patch.dict(os.environ, {'DSH_CONDUCT_SPAWNER': 'zcode'}):
            proc = run_state_manage(sb.root, 'dispatch', 'cyc', '--spawner', str(flag_spawner))

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn('fake-spawner.ps1', proc.stdout,
                      'only the flag can put a path in that argv')

    def test_an_out_of_table_word_refuses(self):
        sb, _action = self.pinned()

        refused = sb.set_policy('cyc', 'spawner', 'claude', check=False)

        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("'dsh', 'zcode'", refused.stderr,
                      'the refusal names the words the policy takes')

    def test_the_closing_call_names_the_line_policy_spawner_too(self):
        """The closing call's own spawn is the same spawn call as `dispatch`: the line policy
        reaches it too."""
        sb = self.ready_sandbox()
        sb.set_policy('cyc', 'spawner', 'dsh')
        sb.next()                                # pin the node and write its prompt

        with mock.patch.dict(os.environ, {'DSH_CONDUCT_SPAWNER': 'zcode'}):
            _action, proc = sb.spawn_next()

        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn('dsh-spawn.ps1', proc.stdout,
                      'the machine setting said zcode; only the line policy can answer dsh')


if __name__ == '__main__':
    unittest.main(verbosity=2)
