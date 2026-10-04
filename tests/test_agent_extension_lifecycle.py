"""Original-task resume and shutdown through real local extension processes."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_agent_extension_development import world, S, A, D, P, L
from solvio.agent_runtime.orchestrator import Orchestrator


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def owned_driver(w):
    runtime = Orchestrator(ledger=w.ledger, development=w.development,
                           require_task_authority=True)
    job = asyncio.create_task(w.service.drive(w.run.run_id))
    runtime._drivers[w.milestone] = job
    # The normal Core loop is idle while the separately owned driver works.
    runtime._task = asyncio.create_task(asyncio.sleep(60))
    try:
        async with asyncio.timeout(3):
            while not w.calls():
                await asyncio.sleep(0.01)
        pid = w.calls()[0]['pid']
        require(alive(pid))
        return runtime, job, pid
    except BaseException:
        await runtime.stop()
        raise


async def t_development_resume_uses_the_worker_route_with_a_different_planner():
    with world(quote=False) as w:
        blocked = await w.service.drive(w.run.run_id)
        require_equal(blocked.reason, 'cost_unbounded')
        wait = {'phase': 'development', 'provider': 'codex', 'billing_mode': 'subscription',
                'requested_billing_mode': 'subscription', 'reason': blocked.reason,
                'resume_allowed': True, 'resume_state': S.WAITING_CAPABILITY}
        require(w.ledger.park_provider_boundary(w.run.run_id, {'provider_wait': wait},
                                               'Owner cost decision'))
        planner = SimpleNamespace(route={'provider': 'claude-code', 'billing_mode': 'subscription'})
        runtime = Orchestrator(ledger=w.ledger, planner=planner, require_task_authority=True)
        before = w.ledger.get_run(w.run.run_id)
        grant = runtime.task_authority.for_run(w.run.run_id)
        require_equal(await runtime.resume(w.run.run_id), True)
        resumed = w.ledger.get_run(w.run.run_id)
        require_equal(resumed.state, S.WAITING_CAPABILITY)
        require_equal(json.loads(resumed.boundary)['provider_wait']['provider'], 'codex')
        require_equal(resumed.started_at, before.started_at)
        require_equal(resumed.planner_calls, before.planner_calls)
        require_equal(runtime.task_authority.for_run(w.run.run_id), grant)
        require_equal(planner.route['provider'], 'claude-code')
        require_equal(await runtime.resume(w.run.run_id), False)
        require_equal(w.calls(), [])
        # A newly verified quote unlocks that same Owner-authorized development.
        w.quote = lambda *args: D.CostQuote(0, w.evidence)
        with patch.object(P, 'claude_status', AsyncMock(side_effect=AssertionError('provider switch'))):
            ready = await w.fresh().drive(w.run.run_id)
            require_equal(ready.state, 'ready', ready.reason)
            require_equal([call['kind'] for call in w.calls()], ['build', 'review'])
            require_equal((await w.fresh().drive(w.run.run_id)).state, 'ready')
            require_equal(len(w.calls()), 2, 'repeated resume dispatched a second build')


async def t_stop_waits_for_actual_driver_process_and_durable_unknown_hold():
    with world(mode='timeout') as w:
        runtime, job, pid = await owned_driver(w)
        try:
            await asyncio.wait_for(runtime.stop(), timeout=5)
            require(job.done(), 'stop returned before the development coroutine drained')
            require(not alive(pid), 'stop returned while the actual CLI was alive')
            require_equal(runtime._drivers, {})
            require(runtime._task is None)
            require_equal(w.development.open_phases(w.milestone), [])
            require_equal(w.development.milestone(w.milestone).state, A.HUMAN_REQUIRED)
            require_equal(D.invocations(w.ledger, w.task.task_id)[0]['state'], 'unknown')
            held = await w.fresh().drive(w.run.run_id)
            require_equal(held.reason, 'cost_recovery_required')
            require(not held.resume_allowed)
            require_equal(len(w.calls()), 1)
        finally:
            if not job.done():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            await runtime.stop()
            require(not alive(pid), 'local process survived test cleanup')


async def t_stop_survives_repeated_caller_cancel_and_drains_an_owner_owned_driver():
    with world(mode='timeout') as w:
        runtime, job, pid = await owned_driver(w)
        entered, release = asyncio.Event(), asyncio.Event()
        original = L._stop_process
        owner = stopping = second = None

        async def held(process, grace=0):
            entered.set()
            await release.wait()
            await original(process, grace)

        with patch.object(L, '_stop_process', held):
            try:
                owner = asyncio.create_task(runtime.cancel(w.run.run_id))
                await asyncio.wait_for(entered.wait(), timeout=3)
                require_equal(runtime._drivers, {}, 'Owner cancel should now own the driver')
                stopping = asyncio.create_task(runtime.stop())
                await asyncio.sleep(0)
                stopping.cancel()
                await asyncio.sleep(0)
                stopping.cancel()
                second = asyncio.create_task(runtime.stop())
                await asyncio.sleep(0)
                require(not stopping.done(), 'caller cancellation abandoned shutdown')
                require(not second.done(), 'second stop bypassed the ongoing drain')
                require(not owner.done())
                require(alive(pid))
                await runtime.tick()
                require_equal(runtime._advances, {})
                require_equal(len(w.calls()), 1)
                release.set()
                require_equal(await asyncio.wait_for(owner, timeout=5), True)
                result = await asyncio.gather(stopping, return_exceptions=True)
                require(isinstance(result[0], asyncio.CancelledError))
                await asyncio.wait_for(second, timeout=5)
                require(job.done())
                require(not alive(pid))
                require_equal(w.ledger.get_run(w.run.run_id).state, S.CANCELLED)
                require_equal(w.development.open_phases(w.milestone), [])
                require_equal(D.invocations(w.ledger, w.task.task_id)[0]['state'], 'unknown')
            finally:
                release.set()
                if not job.done():
                    job.cancel()
                await asyncio.gather(job, *(t for t in (owner, stopping, second) if t),
                                     return_exceptions=True)
                await runtime.stop()
                require(not alive(pid), 'local process survived repeated cancellation')


async def t_stop_also_waits_for_an_owner_cancel_arriving_during_driver_cleanup():
    with world(mode='timeout') as w:
        runtime, job, pid = await owned_driver(w)
        process_closing, release_process = asyncio.Event(), asyncio.Event()
        finalizing, release_final = asyncio.Event(), asyncio.Event()
        original_stop, original_finish = L._stop_process, runtime._finish
        shutdown = owner = None

        async def held_stop(process, grace=0):
            process_closing.set()
            await release_process.wait()
            await original_stop(process, grace)

        async def held_finish(*args, **kwargs):
            finalizing.set()
            await release_final.wait()
            return await original_finish(*args, **kwargs)

        with patch.object(L, '_stop_process', held_stop), patch.object(runtime, '_finish', held_finish):
            try:
                shutdown = asyncio.create_task(runtime.stop())
                await asyncio.wait_for(process_closing.wait(), timeout=3)
                owner = asyncio.create_task(runtime.cancel(w.run.run_id))
                await asyncio.sleep(0)
                release_process.set()
                await asyncio.wait_for(finalizing.wait(), timeout=3)
                require(not alive(pid))
                require(not shutdown.done(), 'stop abandoned the late Owner cancellation')
                require(not owner.done())
                release_final.set()
                require_equal(await asyncio.wait_for(owner, timeout=3), True)
                await asyncio.wait_for(shutdown, timeout=3)
                require_equal(w.ledger.get_run(w.run.run_id).state, S.CANCELLED)
            finally:
                release_process.set()
                release_final.set()
                await asyncio.gather(job, *(t for t in (owner, shutdown) if t),
                                     return_exceptions=True)
                await runtime.stop()
                require(not alive(pid), 'local process survived shutdown race cleanup')


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
