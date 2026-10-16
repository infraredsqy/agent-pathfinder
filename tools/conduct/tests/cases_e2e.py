"""The whole walk, through the shipped processes.

`TestEndToEnd` drives `state-manage.py next --after <node> --spawn` end to end with the dry-run
spawn switch: no session is ever started and the network is never touched, but every command is the
real one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases_base import Base, NODE, skill_of  # noqa: E402
from harness import Closer, Driver, WorkerSession, stop_after  # noqa: E402

__all__ = ['TestEndToEnd']


class TestEndToEnd(Base):
    """The shipped path: every command a real process, the spawn a dry run."""

    def project(self, count=2):
        sb = self.make(cli=True)
        ids = sb.add_chain(count)
        sb.start_cycle()
        return sb, ids

    def deliver(self, sb, node_id, outputs=None, status='completed'):
        """One worker's report, as the real process. The close is `next --after <node>`."""
        return sb.handoff('cyc', node_id, status, 'done',
                          outputs if outputs is not None else {'deliverable': 'x',
                                                               'acceptance': ['a'],
                                                               'touched': []})

    def test_the_walk_from_start_cycle_to_stop_pipeline(self):
        """Two nodes, three commands each (report, close, spawn), and the run parks itself.

        The loop mirrors what the pipeline asks a caller to do: read the action, and when it is a
        dispatch, let that node's worker report. The first dispatch is the one `cycle start` pinned.
        """
        sb, ids = self.project(2)

        opening, proc = sb.next_cli('--plan')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(opening['action'], 'dispatch')
        self.assertEqual(opening['node_id'], ids[0])
        self.assertEqual(skill_of(opening), 'pf-conduct')

        seen = []
        finished = None
        for _ in range(8):
            action = sb.next_cli()[0]
            # The walk is over when the line's last close takes it out of service: the next bare
            # `next` no longer drives it, it names the command that puts it back in.
            if action['action'] in ('stop_pipeline', 'ask_user', 'no_cycle'):
                finished = action['action']
                break
            if action['action'] == 'dispatch':
                # the worker for this node does its work and writes its ticket, through the same
                # command its prompt carries
                self.deliver(sb, action['node_id'])
                continue
            self.assertEqual(action['action'], 'wait', action)
            node = action['node_id']
            seen.append(node)
            settled, proc = sb.next_cli('--after', node, '--spawn')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            if settled['action'] == 'dispatch':
                self.assertEqual(settled['node_id'], ids[len(seen)])

        self.assertIsNotNone(finished, 'the run never settled')
        self.assertEqual(seen, ids)
        self.assertEqual(sb.path_nodes(), ids)
        self.assertEqual(sb.cycle()['status'], 'inactive')
        self.assertEqual([sb.dag_status(uid) for uid in ids], ['completed'] * 2)

    def test_no_session_is_ever_started(self):
        """`DSH_CONDUCT_SPAWN_DRY_RUN=1` is what the harness sets; every `spawned` line it produces
        has to be the dry-run banner and never a session id."""
        sb, ids = self.project(2)
        seen = []
        for _ in range(6):
            action = sb.next_cli()[0]
            if action['action'] in ('stop_pipeline', 'ask_user', 'no_cycle'):
                break
            if action['action'] == 'dispatch':
                self.deliver(sb, action['node_id'])
                continue
            settled, _ = sb.next_cli('--after', action['node_id'], '--spawn')
            if settled.get('spawned'):
                seen.append(settled['spawned'])

        self.assertTrue(seen, 'no spawn was ever reported')
        for line in seen:
            self.assertIn('dry-run', line, f'a real session was started: {line}')

