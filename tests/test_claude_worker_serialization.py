"""DEBT-0302: the single Core loop owns complete Claude worker turns.

Real HTTPS task admission, Orchestrator.start/loop/tick, native task adapter,
cost claims, session ledger and broker Registry. Only provider observations,
CLI transport and canary results are local fixtures; no real Claude process.
Direct concurrent run_task callers are deliberately outside this proof.
"""
from contextlib import ExitStack
import asyncio
import json
from pathlib import Path
import sys
import time
from unittest.mock import AsyncMock, patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
               str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal, require_raises
enforce_assertions()
import test_native_task_entry as E
import test_claude_native_task as C
from _native_worker_seams import claude_module_double
from solvio.agent_runtime import orchestrator as O, cost_dispatch as D, store as S
from solvio.provider_broker.session import Registry
from solvio.specialists import claude_native_task as CNT, launcher as L, providers as P


class ObservedBroker(C.FakeBroker):
    """Existing fixture accounting plus the product's token/lease registry."""
    def __init__(self, ledger):
        super().__init__(C.BROKER_PORT)
        self.registry = Registry()
        self.agent_ledger = ledger
        self.order = []
        self.overlap = False

    def register_principal(self, name):
        active = len(self.leases) - len(self.closed)
        self.overlap |= active != 0
        if self.leases:
            previous = D.invocations(self.agent_ledger, self.leases[-1]['ref'][5:])
            previous = [row for row in previous if row['phase'] == 'specialist']
            self.overlap |= len(previous) != 1 or previous[0]['state'] != 'finished'
        token = self.registry.register(name)
        self.tokens.append(token)
        self.order.append('register')
        return token

    def open_lease(self, principal, ref, *, deadline, **_):
        lease = self.registry.open_lease(principal, ref, deadline=deadline, now=time.time())
        self.leases.append({'ref': ref, 'id': lease})
        self.order.append('open')
        return lease

    def close_lease(self, lease_id):
        self.registry.close_lease(lease_id, now=time.time())
        self.closed.append(lease_id)
        self.order.append('close')


async def t_core_loop_finishes_first_claude_turn_and_lease_before_starting_second():
    async with E.world(worker='claude-code') as w:
        w.orch.require_task_authority = True  # the actual Core setting
        # Strengthen the proof: the production admission cap is currently one,
        # but it must not be what prevents overlapping broker registrations.
        w.orch.max_concurrent = 2
        broker = ObservedBroker(w.ledger)
        entered, release, completed_tick = asyncio.Event(), asyncio.Event(), asyncio.Event()
        launched = []
        real_turn = CNT.run_task

        async def launch(invocation, prompt, *, on_stdout_line, **_):
            index = len(launched)
            launched.append(invocation)
            token = broker.tokens[-1]
            require(broker.registry.resolve(token) is not None)
            if index == 0:
                entered.set()
                await release.wait()
                require(broker.registry.resolve(token) is not None,
                        'a second worker invalidated the first active broker token')
            argv = list(invocation.argv)
            sid = argv[argv.index('--session-id') + 1]
            on_stdout_line(C._init(sid))
            on_stdout_line(C._assistant([{'type': 'text', 'text': 'Local fixture result.'}], sid))
            body = {'status': 'completed', 'text': {
                'findings': ['The isolated fixture completed its bounded turn.'],
                'evidence': [], 'assumptions': [], 'uncertainties': [],
                'rejected_alternatives': [], 'risk_notes': [],
                'recommended_path': 'The turn is complete.', 'confidence': 'hoch', 'files': []},
                'sources': []}
            on_stdout_line(C._result(json.dumps(body), sid=sid))
            return L.Outcome(True, exit_code=0, process_started=True)

        async def turn(*args, **kwargs):
            continuation = kwargs['continuation']
            session = continuation.sessions.session(continuation.session_id)
            w.claude_calls.append((args[0], session, kwargs['config']))
            return await real_turn(*args, **kwargs, broker=broker, runner=launch)

        real_tick = w.orch.tick

        async def observed_tick():
            await real_tick()
            steps = [next((s for s in w.ledger.steps_for_run(row['run_id'])
                           if s.kind == 'specialist'), None) for row in accepted]
            if all(step is not None and step.state == 'succeeded' for step in steps):
                completed_tick.set()

        with ExitStack() as stack:
            # Restore the REAL adapter inside the existing native-entry fixture.
            stack.enter_context(claude_module_double(CNT))
            stack.enter_context(patch.object(CNT, 'run_task', side_effect=turn))
            stack.enter_context(patch.object(CNT, 'resolve_claude', return_value=sys.executable))
            stack.enter_context(patch.object(CNT, 'session_mode', return_value='resume'))
            stack.enter_context(patch.object(CNT, 'mcp_mode', return_value='none'))
            stack.enter_context(patch.object(E.N, '_broker', return_value=broker))
            stack.enter_context(patch.object(P, 'claude_status', AsyncMock(return_value=P.ProviderStatus(
                'claude-code', True, auth='claude.ai', billing_mode=P.SUBSCRIPTION))))
            stack.enter_context(patch.object(O, 'TICK_SECONDS', .02))
            stack.enter_context(patch.object(w.orch, 'tick', side_effect=observed_tick))
            accepted = [await E.start(w, client_request_id='serialization-first'),
                        await E.start(w, client_request_id='serialization-second')]
            require(accepted[0]['task_id'] != accepted[1]['task_id'])
            for row in accepted:
                require(w.orch.task_starts.ready(row['run_id']))
                grant = w.orch.task_authority.for_run(row['run_id'])
                require(w.orch.task_authority.active(grant.reference,
                    task_id=row['task_id'], run_id=row['run_id']).allowed)
            async def wait_for(event, phase):
                try:
                    await asyncio.wait_for(event.wait(), timeout=15)
                except TimeoutError:
                    states = [(w.ledger.get_run(row['run_id']).state,
                               w.ledger.get_run(row['run_id']).failure_category,
                               [(s.kind, s.state, s.outcome_reason, s.summary)
                                for s in w.ledger.steps_for_run(row['run_id'])]) for row in accepted]
                    raise AssertionError(f'{phase}: {states}; registrations={len(broker.tokens)}') from None
            try:
                await w.orch.start()
                loop = w.orch._task
                await w.orch.start()
                require(w.orch._task is loop, 'sequential Core start created a second loop')
                await wait_for(entered, 'first launcher')
                # A live event loop must remain responsive while the first turn is held.
                await asyncio.sleep(.05)
                require_equal(len(broker.tokens), 1, 'second token registered during first launcher')
                require_equal(len(launched), 1)
                require_equal(broker.closed, [])
                second = D.invocations(w.ledger, accepted[1]['task_id'])
                require(not any(row['phase'] == 'specialist' for row in second),
                        'second worker claimed costs while the first was held')
                release.set()
                await wait_for(completed_tick, 'two completed worker steps')
            finally:
                release.set()
                await w.orch.stop()
            require_equal(len(launched), 2, 'both authorized workers must actually run')
            require_equal(broker.overlap, False, 'worker token/lease or cost settlement overlapped')
            require_equal(broker.order, ['register', 'open', 'close'] * 2)
            require_equal(broker.closed, [entry['id'] for entry in broker.leases])
            require_equal([entry['ref'] for entry in broker.leases],
                          ['task:' + row['task_id'] for row in accepted])
            require(broker.registry.resolve(broker.tokens[0]) is None,
                    'real Registry must invalidate the old token once the next turn begins')
            require(broker.registry.resolve(broker.tokens[1]) is not None)
            turns = E._turns(w)
            require_equal([(row['run_id'], row['state'], row['terminal_status']) for row in turns],
                          [(row['run_id'], 'terminal', 'completed') for row in accepted])
            for row in accepted:
                calls = [r for r in D.invocations(w.ledger, row['task_id']) if r['phase'] == 'specialist']
                require_equal([(r['provider'], r['state']) for r in calls], [('claude-code', 'finished')])
                with w.ledger._open() as db:
                    events = db.execute("SELECT ref FROM agent_events WHERE run_id=? AND kind='native_progress'",
                                        (row['run_id'],)).fetchall()
                require_equal(len(events), 1)
                event = json.loads(events[0]['ref'])
                require_equal((event['runtime'], event['event'], event['seq']),
                              ('claude-bare-broker', 'started', 1))
                require_equal(event['invocation_id'], calls[0]['invocation_id'])
            require(w.orch._task is None and not w.orch._advances)


def t_claude_progress_accepts_only_bound_turn_shape_and_started_event():
    progress = dict(runtime='claude-bare-broker', invocation_id='pc-local-fixture',
        operation_id='step-local', native_thread_id=C.SID,
        native_turn_id=C.SID + '/pc-local-fixture', item_id='', seq=1, event='started', status='')
    require_equal(json.loads(S._native_progress_reference(json.dumps(progress))), progress)
    # Handover retains the Core thread but may have a new CLI-session UUID.
    handover = dict(progress, native_turn_id='11111111-2222-4333-8444-555555555555/pc-local-fixture')
    require_equal(json.loads(S._native_progress_reference(json.dumps(handover))), handover)
    for changes in ({'runtime': 'unknown-worker'}, {'event': 'web_search', 'status': 'started'},
                    {'seq': 2}, {'status': 'completed'},
                    {'native_turn_id': C.SID + '/pc-foreign'}, {'native_turn_id': '../pc-local-fixture'},
                    {'native_turn_id': C.SID + '/nested/pc-local-fixture'},
                    {'native_thread_id': 'foreign/path'}, {'operation_id': 'step/foreign'},
                    {'item_id': 'unrelated-item'}, {'runtime': 'hermes-codex-app-server'}):
        require_raises(ValueError, S._native_progress_reference, json.dumps(dict(progress, **changes)))


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
