"""Real public document need: a crash cannot consume its still-unplanned continuation."""
from __future__ import annotations

import os
from pathlib import Path
import json
import sqlite3
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_agent_document_execution as T
import test_agent_capability_need as N
from solvio.agent_runtime import budget as BU, checkpoint as CP, capability_need as CN


class ProcessLoss(BaseException):
    pass


async def lose_process_after_need_resolution(w, run_id):
    """Same boundary before/after the fix: resolved step, before any next plan."""
    original_plan, original_replan = w.orch._do_plan, w.orch._replan

    async def crash_or_call(original, run, *args):
        steps = w.ledger.steps_for_run(run.run_id)
        if any(s.kind == 'capability_need' and s.outcome_reason == 'implementation_ready'
               for s in steps):
            raise ProcessLoss()
        return await original(run, *args)

    async def plan(run, *args):
        return await crash_or_call(original_plan, run, *args)

    async def replan(run, *args):
        return await crash_or_call(original_replan, run, *args)

    with patch.object(w.orch, '_do_plan', plan), patch.object(w.orch, '_replan', replan):
        try:
            await T.advance(w.orch, run_id)
        except ProcessLoss:
            return
    raise AssertionError('The real post-resolution crash point was not reached')


async def t_public_need_resolution_crash_replans_same_task_once_and_preserves_budgets():
    async with T.world() as w:
        response = await w.start(T.body(T.DOCUMENT))
        accepted = await response.json()
        require_equal(response.status, 201, str(accepted))
        run_id, task_id = accepted['run_id'], accepted['task_id']
        waiting = await T.advance(w.orch, run_id, until=lambda r: r.state == T.S.WAITING_CAPABILITY)
        context = w.orch._contexts[run_id]
        context.ledger.attempts['a' * 20] = 1
        context.ledger.low_value['b' * 20] = 1
        w.orch._checkpoint(run_id, context)
        grant = w.orch.task_authority.for_run(run_id)
        await lose_process_after_need_resolution(w, run_id)
        interrupted = w.ledger.get_run(run_id)
        require_equal(interrupted.state, T.S.RUNNING)
        require_equal(interrupted.plan_revision, waiting.plan_revision + 1)
        require_equal(interrupted.planner_calls, 1)
        snapshot = CP.decode(interrupted.plan_checkpoint)
        require_equal(snapshot['revision'], interrupted.plan_revision)
        require_equal(snapshot['versuche'], {'a' * 20: 1})
        require_equal(snapshot['geringwertig'], {'b' * 20: 1})
        claims = T.D.invocations(w.ledger, task_id)
        require_equal([c['phase'] for c in w.calls()], ['plan', 'extension_build', 'extension_review'])
        require_equal([a for a in w.ledger.artifacts_for_run(run_id) if a.kind == 'document_result'], [])
        fresh = T.fresh_runtime(w)
        w.orch = fresh
        await fresh.reconcile()
        require_equal(fresh.ledger.get_run(run_id).state, T.S.INTERRUPTED)
        completed = await T.advance(fresh, run_id)
        require_equal(completed.state, T.S.SUCCEEDED, completed.result_summary)
        require_equal((completed.task_id, completed.plan_revision, completed.planner_calls),
                      (task_id, waiting.plan_revision + 1, 2))
        require_equal(fresh.task_authority.for_run(run_id), grant)
        require_equal([c['phase'] for c in w.calls()],
                      ['plan', 'extension_build', 'extension_review', 'plan', 'assessment'])
        after = T.D.invocations(fresh.ledger, task_id)
        require(all(c in after for c in claims), 'prior cost claims were reset or replaced')
        require(all(c['run_id'] == run_id and c['task_id'] == task_id for c in after))
        outputs = [a for a in fresh.ledger.artifacts_for_run(run_id) if a.kind == 'document_result']
        require_equal(len(outputs), 1)
        require_equal(Path(outputs[0].path).read_text().strip(), 'Actual original input.')
        require(CN.replan_pending(fresh.ledger, run_id) is None)
        count = len(w.calls())
        await fresh.tick()
        require_equal(len(w.calls()), count)


def t_both_need_transitions_roll_back_together_and_never_refill_revision_budget():
    with N.world() as w:
        step_id, _ = N.park(w)
        before = w.ledger.get_run(w.run.run_id)
        with w.ledger._open() as connection:
            connection.execute("CREATE TRIGGER fail_resolution BEFORE UPDATE OF plan_revision ON agent_runs "
                "BEGIN SELECT RAISE(ABORT, 'synthetic resolution crash'); END")
        try:
            CN.resolve(w.ledger, run_id=w.run.run_id, step_id=step_id, checkpoint=before.plan_checkpoint)
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError('Resolution never reached the SQLite transaction boundary')
        require_equal(w.ledger.get_run(w.run.run_id), before)
        require_equal(CN.pending(w.ledger, w.run.run_id).step_id, step_id)
        with w.ledger._open() as connection:
            connection.execute('DROP TRIGGER fail_resolution')
            connection.execute('UPDATE agent_tasks SET budget=? WHERE task_id=?',
                (json.dumps({**w.task.budget, 'max_plan_revisions': 0}), w.task.task_id))
        try:
            CN.resolve(w.ledger, run_id=w.run.run_id, step_id=step_id, checkpoint=before.plan_checkpoint)
        except BU.BudgetExhausted as exc:
            require_equal(exc.detail, 'max_plan_revisions')
        else:
            raise AssertionError('Need resolution ignored the original revision budget')
        require_equal(w.ledger.get_run(w.run.run_id), before)
        require_equal(CN.pending(w.ledger, w.run.run_id).step_id, step_id)
        with w.ledger._open() as connection:
            connection.execute('UPDATE agent_tasks SET budget=? WHERE task_id=?',
                               (json.dumps(w.task.budget), w.task.task_id))
        CN.resolve(w.ledger, run_id=w.run.run_id, step_id=step_id, checkpoint=before.plan_checkpoint)
        resolved = w.ledger.get_run(w.run.run_id)
        require_equal((resolved.state, resolved.plan_revision), (T.S.RUNNING, 1))
        plan = T.PL.validate({'schritte': [{'art': 'verify'}]}, scope='research',
                            goal=w.task.objective, allowed_profiles=set(), known_capabilities=set())
        checkpoint = N.checkpoint(plan=plan, revision=1)
        with w.ledger._open() as connection:
            connection.execute("CREATE TRIGGER fail_plan_commit BEFORE UPDATE OF outcome_reason ON agent_steps "
                "WHEN NEW.outcome_reason='implementation_replanned' "
                "BEGIN SELECT RAISE(ABORT, 'synthetic plan crash'); END")
        try:
            CN.finish_replan(w.ledger, run_id=w.run.run_id, checkpoint=checkpoint)
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError('Plan completion never reached the SQLite transaction boundary')
        require_equal(w.ledger.get_run(w.run.run_id), resolved)
        require_equal(CN.replan_pending(w.ledger, w.run.run_id).step_id, step_id)
        with w.ledger._open() as connection:
            connection.execute('DROP TRIGGER fail_plan_commit')
        CN.finish_replan(w.ledger, run_id=w.run.run_id, checkpoint=checkpoint)
        require(CN.replan_pending(w.ledger, w.run.run_id) is None)
        require_equal(w.ledger.get_run(w.run.run_id).plan_checkpoint, checkpoint)
        N.rejects(lambda: CN.resolve(w.ledger, run_id=w.run.run_id, step_id=step_id,
                                     checkpoint=before.plan_checkpoint), 'resolution replay spent a second revision')
        require_equal(w.ledger.get_run(w.run.run_id).plan_revision, 1)
        require_equal(T.D.invocations(w.ledger, w.task.task_id), [])


async def t_crash_before_context_refresh_retains_marker_and_exhausted_planner_budget():
    async with T.world() as w:
        accepted = await (await w.start(T.body(T.DOCUMENT))).json()
        run_id, task_id = accepted['run_id'], accepted['task_id']
        await T.advance(w.orch, run_id, until=lambda r: r.state == T.S.WAITING_CAPABILITY)
        original = CN.resolve

        def crash_after_commit(*args, **kwargs):
            original(*args, **kwargs)
            raise ProcessLoss()

        with patch.object(CN, 'resolve', crash_after_commit):
            try:
                await T.advance(w.orch, run_id)
            except ProcessLoss:
                pass
            else:
                raise AssertionError('The post-commit/pre-context crash point was missed')
        current = w.ledger.get_run(run_id)
        require_equal(CP.decode(current.plan_checkpoint)['revision'], current.plan_revision)
        require_equal(current.plan_revision, 1)
        require_equal(w.orch._contexts[run_id].ledger.plan_revisions, 0,
                      'This probe must leave the old finally context in place')
        w.ledger.set_run_fields(run_id, planner_calls=BU.MAX_PLANNER_CALLS_PER_RUN)
        before, costs = w.calls(), T.D.invocations(w.ledger, task_id)
        fresh = T.fresh_runtime(w)
        w.orch = fresh
        await fresh.reconcile()
        completed = await T.advance(fresh, run_id)
        require_equal((completed.state, completed.failure_category), (T.S.FAILED, 'budget_exhausted'))
        require_equal(completed.plan_revision, 1)
        require_equal(w.calls(), before, 'Restart refilled the exhausted planner budget')
        require_equal(T.D.invocations(fresh.ledger, task_id), costs)
        require_equal([a for a in fresh.ledger.artifacts_for_run(run_id) if a.kind == 'document_result'], [])


async def t_need_followup_quota_wait_and_owner_resume_keep_the_same_revision_and_adapter():
    async with T.world() as w:
        accepted = await (await w.start(T.body(T.DOCUMENT))).json()
        run_id, task_id = accepted['run_id'], accepted['task_id']
        await T.advance(w.orch, run_id, until=lambda r: r.state == T.S.WAITING_CAPABILITY)
        original = w.orch.planner.plan

        async def quota_on_followup(**kwargs):
            if CN.replan_pending(w.ledger, run_id) is not None:
                kwargs['ledger'].check_planner()
                kwargs['ledger'].note_planner_call()
                raise T.PL.ProviderUnavailable(T.PL.PlannerCall(False, reason='quota', provider='codex',
                    billing_mode='subscription', auth='subscription', dispatch_started=False))
            return await original(**kwargs)

        with patch.object(w.orch.planner, 'plan', quota_on_followup):
            waiting = await T.advance(w.orch, run_id, until=lambda r: r.state == T.S.WAITING_USER)
        require_equal(waiting.state, T.S.WAITING_USER, waiting.result_summary)
        require_equal(waiting.plan_revision, 1)
        require_equal(json.loads(waiting.boundary)['provider_wait']['phase'], 'plan')
        calls, costs = w.calls(), T.D.invocations(w.ledger, task_id)
        adapter = w.orch.extension_activation.selected(run_id)
        fresh = T.fresh_runtime(w)
        w.orch = fresh
        await fresh.reconcile()
        await fresh.tick()
        require_equal(w.calls(), calls, 'A waiting quota resumed itself')
        require(await fresh.resume(run_id), 'The existing explicit Owner-resume must accept this boundary')
        require_equal(fresh.ledger.get_run(run_id).state, T.S.PLANNING)
        completed = await T.advance(fresh, run_id)
        require_equal(completed.state, T.S.SUCCEEDED, completed.result_summary)
        require_equal((completed.task_id, completed.plan_revision), (task_id, 1))
        require_equal([c['phase'] for c in w.calls()],
                      ['plan', 'extension_build', 'extension_review', 'plan', 'assessment'])
        require(all(c in T.D.invocations(fresh.ledger, task_id) for c in costs))
        # Terminal grants no longer activate candidates; read the retained
        # selection and the effect receipt to identify the actual implementation.
        with fresh.ledger._open() as connection:
            selection = connection.execute('SELECT active_artifact,pending_artifact '
                'FROM agent_extension_selection WHERE run_id=?', (run_id,)).fetchone()
        require_equal(tuple(selection), (adapter[0], ''))
        receipt = next(a for a in fresh.ledger.artifacts_for_run(run_id) if a.kind == 'document_receipt')
        require_equal(json.loads(Path(receipt.path).read_bytes())['implementation_ref'], adapter[0])
        require(CN.replan_pending(fresh.ledger, run_id) is None)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
