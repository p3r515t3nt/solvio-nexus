"""Independent process-loss cuts of the public research/file integration.

Reuse its local provider protocols and actual temporary Office/result stores.
No real provider or production state is used.
"""
from __future__ import annotations

from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_agent_research_files import world
from solvio.agent_runtime import artifact_creation as A, result_files as RF, specialists as SP, store as S


class ProcessLoss(BaseException):
    pass


async def advance(w, run_id):
    await w.orch._advance(w.ledger.get_run(run_id))
    w.orch._checkpoint_if_open(run_id)


async def cut(w, run_id, position):
    publish = A.publish
    # Orchestrator has a fresh Ledger facade over the exact same database.
    orch_update_step = w.orch.ledger.update_step
    def publication(*args, **kwargs):
        if position == 'before_publish':
            raise ProcessLoss()
        result = publish(*args, **kwargs)
        if position == 'after_publish':
            raise ProcessLoss()
        return result
    def update(step_id, **kwargs):
        result = orch_update_step(step_id, **kwargs)
        step = w.ledger.get_step(step_id)
        if (position == 'after_step' and kwargs.get('state') == 'succeeded'
                and step.specialist_profile == SP.FILES_PROFILE):
            raise ProcessLoss()
        return result
    with patch.object(A, 'publish', publication), patch.object(w.orch.ledger, 'update_step', update):
        for _ in range(12):
            try:
                await advance(w, run_id)
            except ProcessLoss:
                return
    raise AssertionError('process-loss cut not reached')


async def resume(w, run_id):
    w.ledger.transition(run_id, S.INTERRUPTED)
    w.runtime()
    await w.orch.reconcile()
    for _ in range(12):
        current = w.ledger.get_run(run_id)
        if current.terminal or current.state == S.WAITING_USER:
            return current
        await advance(w, run_id)
    raise AssertionError('restored run did not finish')


async def t_crash_before_publish_keeps_paid_builder_and_refuses_replay():
    async with world() as w:
        accepted, _ = await w.admit()
        run_id = accepted['run_id']
        await cut(w, run_id, 'before_publish')
        before = w.calls()
        require_equal([r['kind'] for r in before], ['plan', 'specialist'])
        final = await resume(w, run_id)
        require(final.state != S.SUCCEEDED)
        require_equal(w.calls(), before)
        require_equal(RF.describe_files(w.ledger, run_id)[0], [])


async def t_crash_after_publish_does_not_invent_completed_step_or_recreate_files():
    async with world() as w:
        accepted, _ = await w.admit()
        run_id = accepted['run_id']
        await cut(w, run_id, 'after_publish')
        before = w.calls()
        require_equal(len([a for a in w.ledger.artifacts_for_run(run_id)
                           if a.kind == 'result_file']), 2)
        final = await resume(w, run_id)
        require(final.state != S.SUCCEEDED)
        require_equal(w.calls(), before)
        require_equal(RF.describe_files(w.ledger, run_id)[0], [])


async def t_crash_after_durable_step_retains_bundle_and_only_assesses_once():
    async with world() as w:
        accepted, _ = await w.admit()
        run_id = accepted['run_id']
        await cut(w, run_id, 'after_step')
        require_equal([r['kind'] for r in w.calls()], ['plan', 'specialist'])
        final = await resume(w, run_id)
        require_equal(final.state, S.SUCCEEDED)
        require_equal([r['kind'] for r in w.calls()], ['plan', 'specialist', 'assessment'])
        require_equal(len(RF.describe_files(w.ledger, run_id)[0]), 2)
        require_equal({d.requirement for d in A.completion_evidence(w.ledger, run_id)}, {'h1', 'h2'})


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
