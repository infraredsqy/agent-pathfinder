"""The two stubbed session roles and the driver that plays them.

`WorkerSession`/`Closer` are a node's worker and its closing call; `Driver` plays them node after
node to a terminal action; `finish_node` is the shared convenience builder.
"""

from __future__ import annotations

from harness_env import TERMINAL_ACTIONS
from harness_sandbox import Sandbox

__all__ = ['WorkerSession', 'Closer', 'stop_after', 'Driver', 'finish_node']


# ═══════════════════════════════════════════════════════════════════════════════
#  The two roles a session plays
# ═══════════════════════════════════════════════════════════════════════════════

class WorkerSession:
    """One node's session: do the work (write a real deliverable), then run its ONE command.

    A node's reports live in the node's own ledger directory by convention -- nothing validates them
    any more -- so the worker writes its deliverable under `.pf/nodes/<node>/`.
    """

    def __init__(self, sb: Sandbox, cycle='cyc', via_handoff=False, fail_nodes=(),
                 fail_status='failed'):
        self.sb = sb
        self.cycle = cycle
        self.via_handoff = via_handoff   # True: use handoff.py (the real ticket) as a process
        self.fail_nodes = set(fail_nodes)
        #: The status a failing node reports. `failed` is "I delivered nothing"; `stopped` is
        #: "the user called it off" -- both are the worker saying it did not deliver.
        self.fail_status = fail_status

    #: How many times this worker has reported, so `max_steps` can be checked in a test.
    reports = 0

    def outputs_for(self, action, deliverable, node):
        return {'deliverable': deliverable,
                'acceptance': [f'{action.get("skill") or "pf-conduct"} 的核心验收点'],
                'touched': [deliverable]}

    def work(self, action):
        """Write what the signal will point at, then report it.

        Accepts a `wait` as well as a `dispatch`: a node whose signal is already `running` is
        reported on by the worker the planner is waiting for -- the planner's word for that state
        is `wait`.
        """
        assert action['action'] in ('dispatch', 'wait'), f'not a node to work on: {action}'
        node = action.get('node_id') or self.sb.current_node(self.cycle)
        assert node, f'no node to work on: {action}'
        skill = action.get('skill') or 'pf-conduct'
        # The deliverable lives under the project so a human can go and look at it.
        rel = '.pf/nodes/%s/%s.md' % (node, skill)
        path = self.sb.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'{skill} delivered for {node}\n', encoding='utf-8')
        outs = self.outputs_for(action, rel, node)
        failed = node in self.fail_nodes
        status = self.fail_status if failed else 'completed'
        summary = f'{skill} {"failed" if failed else "done"}'
        self.reports += 1
        if self.via_handoff:
            return self.sb.handoff(self.cycle, node, status, summary, outs)
        return self.sb.write_signal(self.cycle, node, status, summary, outs)


class Closer:
    """The worker's own closing call: `next --after <node_id> [--continue|--stop] [--node ID]`.

    The outcome is NOT a lever here -- the tool reads it off the signal the worker wrote -- so what
    a test supplies is what the closing call still decides: the decision, the node it aims at, and
    the note that ends up in the stop reason. `cases` is by node id, `on_node` is a hook that gets
    the wait action the planner handed over.
    """

    def __init__(self, cases=None, default='continue', on_node=None):
        self.cases = dict(cases or {})   # {node_id: 'continue'|'stop'|(decision, node, note)}
        self.default = default
        self.on_node = on_node           # callable(sb, action) -> override

    def answer_for(self, sb, action):
        if self.on_node:
            got = self.on_node(sb, action)
            if got:
                return self._norm(got)
        return self._norm(self.cases.get(action.get('node_id'), self.default))

    @staticmethod
    def _norm(got):
        if got is None:
            return {'decision': None, 'node': None, 'note': ''}
        if isinstance(got, str):
            return {'decision': got, 'node': None, 'note': ''}
        if isinstance(got, (tuple, list)):
            return {'decision': got[0] if len(got) > 0 else None,
                    'node': got[1] if len(got) > 1 else None,
                    'note': got[2] if len(got) > 2 else ''}
        return {'decision': got.get('decision'), 'node': got.get('node'),
                'note': got.get('note', '')}

    def close(self, sb, action):
        ans = self.answer_for(sb, action)
        return sb.close_node_call(action['node_id'], **ans)


def stop_after(nodes):
    """A closing call that keeps going until `nodes` nodes have been carried, then stops.

    This is how a standing instruction like "做完这个就停" shows up: the session read it, so it
    says stop. The tool is never asked to parse the sentence.

    The count includes the node being closed, because that is the count the tool itself uses: the
    close has not run yet, so the step in hand is not in the path.
    """

    def hook(sb, action):
        cycle = action.get('cycle') or 'cyc'
        done = len(sb.path(cycle)) + (1 if sb.current_node(cycle) else 0)
        return 'stop' if done >= nodes else 'continue'

    return hook


# ═══════════════════════════════════════════════════════════════════════════════
#  The driver
# ═══════════════════════════════════════════════════════════════════════════════

class Driver:
    """Drive the real pipeline to its terminal action: worker -> its own close -> worker ...

    It never performs a pipeline step. Per step it does the two things only a session can do: run a
    worker (which writes its ticket through the real command) and run that worker's closing call
    (through `next --after`, which is what settles and closes the node).

    Every `next` that reports `applied` is checked against a before/after snapshot of the project
    taken from disk: the tool may not report success while nothing changed.
    """

    def __init__(self, sb: Sandbox, cycle='cyc', closer=None, worker=None, max_steps=80,
                 verify=True):
        self.sb = sb
        self.cycle = cycle
        self.closer = closer or Closer()
        self.worker = worker or WorkerSession(sb, cycle)
        self.max_steps = max_steps
        self.verify = verify
        self.actions = []           # every action returned by `next`
        self.applied = []           # (action, state_before, state_after) for each applied action
        self.dispatched = []        # (node_id, skill) in order
        self.nodes_closed = []      # nodes whose closing call ran

    # ── one step ─────────────────────────────────────────────────────────────
    def step(self):
        # The pre-action snapshot has to be taken BEFORE the tool runs: `apply` moves the state
        # itself, so after it returns there is nothing left to compare against.
        before = self.sb.state_sig(self.cycle)
        action = self.sb.next()
        self.actions.append(action)
        if action.get('applied'):
            after = self.sb.state_sig(self.cycle)
            self.applied.append((action, before, after))
            if self.verify and after == before:
                raise AssertionError(
                    f'action {action["action"]!r} was applied but no state on disk changed: '
                    f'{after}')
        return action

    def run(self, until=None):
        """Play both roles until `until(action)` says stop, or the pipeline answers terminally.

        `until` is how a caller picks its own finish line -- `finish_node` stops at the node's own
        close. The pipeline answering terminally before that line is reached means the walk ended
        somewhere the caller did not ask for, so it is raised rather than returned.
        """
        for _ in range(self.max_steps):
            action = self.step()
            kind = action['action']

            if until is not None and until(action):
                return action

            if kind == 'dispatch':
                self._work(action)
                # The worker's next act is its closing call; the planner answers that state with
                # `wait` + `close_with`, which is what the next step reads.
                continue

            if kind == 'wait' and action.get('close_with'):
                # Only the node's own close is left. The worker that just reported runs it.
                answer = self._close(action)
                if answer['action'] == 'dispatch':
                    self._work(answer)
                    continue
                if answer['action'] in TERMINAL_ACTIONS:
                    if until is not None:
                        raise AssertionError(
                            f'the pipeline stopped before the point asked for: {answer}')
                    return answer
                if answer['action'] == 'wait':
                    raise AssertionError(f'stalled on wait after the closing call: {answer}')
                continue

            if kind in TERMINAL_ACTIONS:
                if until is not None:
                    raise AssertionError(
                        f'the pipeline stopped before the point asked for: {action}')
                return action

            if kind == 'wait':
                # A node already in flight: the report that settles it is the worker's next act.
                self._work(action)
                continue
            raise AssertionError(f'unexpected action {kind!r}: {action}')
        raise AssertionError('the pipeline never settled; last actions: '
                             + str(self.kinds()[-8:]))

    def _work(self, action):
        """One worker act: report on the node this action names.

        Two kinds of action ask for it -- a fresh `dispatch`, and a `wait` naming a node already in
        flight, which is the planner waiting for the report that closes it. Which is which is read
        here and nowhere else, so `run` and `finish_node` cannot drift apart on it.
        """
        if action['action'] == 'dispatch':
            self.dispatched.append((action.get('node_id'), action.get('skill')))
        self.worker.work(action)

    def _close(self, action):
        """Run the closer's own call for `action` and record what it moved. Returns its answer.

        The closing call is applied by the tool: it records the path step, marks the node in the
        DAG and points the cycle at the next node (or parks it).
        """
        self.nodes_closed.append(action.get('node_id'))
        answer = self.closer.close(self.sb, action)
        self.actions.append(answer)
        if answer.get('applied'):
            self.applied.append((answer, None, self.sb.state_sig(self.cycle)))
        return answer

    # ── inspection helpers ───────────────────────────────────────────────────
    def kinds(self):
        return [a['action'] for a in self.actions]

    def no_commands_anywhere(self):
        """No returned action carries commands for the caller to RUN.

        An action that answers "nothing to do" may say so with an empty list (`commands: []` on
        `ask_user` / `no_cycle`) -- there is nothing in it to run, so it is not a
        handover. A populated list is, and that is what the action set must never contain.
        """
        return [a for a in self.actions if a.get('commands')]


# ── convenience builders used by several test classes ────────────────────────

def _closable(action):
    """`finish_node`'s stop line: only the node's own close is left."""
    return action['action'] == 'wait' and bool(action.get('close_with'))


def finish_node(sb, cycle='cyc', worker=None):
    """Walk one node to the point where only its close is left. Returns that `wait` action.

    The action is the worker's own cue: `close_with` names the command that settles the node, and
    running it (through `sandbox.close_node_call`) is what closes it. The walk itself is `Driver`'s
    -- stepping, the applied-state contract and the worker's act all live there -- with this
    predicate as its finish line.
    """
    return Driver(sb, cycle, worker=worker, max_steps=20).run(until=_closable)
