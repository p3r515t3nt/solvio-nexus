"""Durable native references through real task authority and cost dispatch.

No provider process/model is started. Only the native response is simulated;
admission, revision, costs, transactions and reopened SQLite are real.
"""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import native_sessions as N, store as S, cost_dispatch as D, costs as C
from solvio.agent_runtime import requirements as RQ, task_revisions as TR
from solvio.agent_runtime.task_authority import TaskAuthority, VerifiedTaskReceipt
from solvio.agent_runtime.task_start_service import TaskStartService
from solvio.specialists import launcher as L

POLICY = 'a' * 64
PROFILE = 'worker/codex'
FREE = D.CostQuote(0, C.CostEvidence('free_local', 'test:native-metadata'))
INVOCATION = L.Invocation('/synthetic/codex', (), cwd='/', timeout=1)


def rejected(action, reason):
    try:
        action()
    except ValueError as exc:
        require_equal(str(exc), reason)
    else:
        raise AssertionError('accepted: ' + reason)


@contextmanager
def world():
    with tempfile.TemporaryDirectory(prefix='solvio-native-sessions-') as folder, \
            patch.dict(os.environ, {'SOLVIO_STATE_DIR': folder}):
        ledger = S.AgentRunLedger(str(Path(folder) / 'agent.sqlite3'))
        authority, costs = TaskAuthority(ledger), C.CostLedger(ledger)
        starts = TaskStartService(ledger, grants=authority, costs=costs)
        task, run = starts.create(objective='Vergleiche zwei öffentliche Quellen.', scope='research',
            origin='trusted_dashboard', principal='owner',
            receipt=VerifiedTaskReceipt('dashboard_session', 'start:one', 'owner'), request_id='native-start-one')
        payload = json.dumps(RQ.validate({'auskunft': [{'id': 'r1', 'text': 'Vergleiche die Quellen.'}]},
                                        objective=task.objective))
        ledger.bind_requirements(task.task_id, payload)
        workspace = Path(folder).resolve() / 'workspace'
        workspace.mkdir(mode=0o700)
        manager = N.NativeSessions(ledger, authority=authority)
        args = dict(task_id=task.task_id, run_id=run.run_id, provider='codex', profile=PROFILE,
                    policy_digest=POLICY, workspace=str(workspace))
        session = manager.bind(**args)
        ledger.transition(run.run_id, S.PLANNING)
        ledger.transition(run.run_id, S.RUNNING)
        yield SimpleNamespace(ledger=ledger, authority=authority, costs=costs, starts=starts,
            task=task, run=run, folder=Path(folder), workspace=workspace, manager=manager,
            session=session, args=args)


async def dispatch(w, operation, native, *, run_id=None, quote=FREE, provider='codex'):
    with D.task_cost_scope(w.ledger, task_id=w.task.task_id, run_id=run_id or w.run.run_id,
            phase='specialist', operation_id=operation, quote_adapter=lambda *_: quote):
        async def runner(*_):
            await native()
            return L.Outcome(True, exit_code=0, process_started=True)
        return await D.dispatch(provider, INVOCATION, operation, runner)


def request(w, *, run_id=None, revision=1, manager=None, session=None):
    manager = manager or w.manager
    run_id = run_id or w.run.run_id
    claim = manager.active_claim(run_id)
    require(claim is not None, 'physical cost claim missing')
    return manager.request_turn(session_id=(session or w.session).session_id, run_id=run_id,
        revision=revision, invocation_id=claim['invocation_id'])


def finish(w, turn, *, turn_id='native-turn-1', manager=None):
    manager = manager or w.manager
    manager.bind_thread(turn.invocation_id, 'native-thread-1')
    manager.started(turn.invocation_id, native_thread_id='native-thread-1', native_turn_id=turn_id)
    manager.terminal(turn.invocation_id, native_thread_id='native-thread-1', native_turn_id=turn_id,
                     status='completed')


def t_two_turns_reopen_preserve_workspace_thread_and_cumulative_costs():
    async def go():
        with world() as w:
            async def first():
                turn, fresh = request(w)
                require(fresh)
                finish(w, turn)
                require_equal(request(w), (w.manager.turn(turn.invocation_id), False))
            first_result = await dispatch(w, 'first', first)
            require_equal(first_result.cost_status, 'settled')
            reopened = N.NativeSessions(S.AgentRunLedger(w.ledger.path))
            require_equal(reopened.session(w.session.session_id).native_thread_id, 'native-thread-1')
            require_equal(reopened.session(w.session.session_id).workspace, str(w.workspace))
            require_equal(reopened.latest_turn(w.session.session_id).state, 'terminal')
            # Reopened metadata alone cannot manufacture live cost ownership.
            require_equal(reopened.active_claim(w.run.run_id), None)
            async def second():
                identity = dict(session_id=w.session.session_id, run_id=w.run.run_id, revision=1,
                    invocation_id=w.manager.active_claim(w.run.run_id)['invocation_id'])
                rejected(lambda: w.manager.request_turn(**identity, expected_native_thread_id='native-thread-1',
                    expected_previous_turn_id='different-turn'), 'native_prepared_binding_changed')
                rejected(lambda: w.manager.request_turn(**identity, expected_native_thread_id='',
                    expected_previous_turn_id='native-turn-1'), 'native_prepared_binding_changed')
                # SQLite admission order remains canonical if wall time moves
                # backwards; timestamps are observations, not turn ordering.
                with patch.object(N.time, 'time', return_value=1.0):
                    turn, fresh = w.manager.request_turn(**identity, expected_native_thread_id='native-thread-1',
                        expected_previous_turn_id='native-turn-1')
                require(fresh)
                finish(w, turn, turn_id='native-turn-2')
            require_equal((await dispatch(w, 'second', second)).cost_status, 'settled')
            require_equal(w.costs.view(w.task.task_id)['counts'], {'settled': 2})
            require_equal(w.manager.latest_turn(w.session.session_id).native_turn_id, 'native-turn-2')
    asyncio.run(go())


def t_requested_started_and_unknown_cannot_blindly_replay_even_with_settled_costs():
    async def go():
        for state in ('requested', 'started', 'unknown'):
            with world() as w:
                captured = []
                async def first():
                    turn, _ = request(w)
                    captured.append(turn)
                    if state != 'requested':
                        w.manager.bind_thread(turn.invocation_id, 'native-thread-1')
                        w.manager.started(turn.invocation_id, native_thread_id='native-thread-1', native_turn_id='turn-1')
                    if state == 'unknown':
                        w.manager.unknown(turn.invocation_id)
                await dispatch(w, 'first', first)
                fresh_manager = N.NativeSessions(w.ledger)
                recorded, fresh = fresh_manager.request_turn(session_id=w.session.session_id,
                    run_id=w.run.run_id, revision=1, invocation_id=captured[0].invocation_id)
                require_equal((recorded.state, fresh), (state, False))
                async def second():
                    rejected(lambda: request(w), 'native_turn_recovery_required')
                await dispatch(w, 'second', second)
                require_equal(fresh_manager.latest_turn(w.session.session_id).invocation_id, recorded.invocation_id)
    asyncio.run(go())


def t_unknown_costs_block_new_dispatch_even_after_native_terminal():
    async def go():
        with world() as w:
            with D.task_cost_scope(w.ledger, task_id=w.task.task_id, run_id=w.run.run_id,
                    phase='specialist', operation_id='uncertain', quote_adapter=lambda *_: FREE):
                async def runner(*_):
                    turn, _ = request(w)
                    finish(w, turn)
                    return L.Outcome(False, reason='timeout', exit_code=None, process_started=True)
                result = await D.dispatch('codex', INVOCATION, 'first', runner)
            require_equal(result.cost_status, 'unknown')
            async def forbidden():
                raise AssertionError('cost uncertainty dispatched another turn')
            require_equal((await dispatch(w, 'second', forbidden)).outcome.reason, 'cost_recovery_required')
            require_equal(w.manager.latest_turn(w.session.session_id).state, 'terminal')
    asyncio.run(go())


def t_binding_changes_foreign_task_workspace_and_no_claim_are_refused():
    async def go():
        with world() as w:
            rejected(lambda: w.manager.bind(**dict(w.args, policy_digest='b' * 64)), 'native_session_binding_changed')
            alternate = w.folder.resolve() / 'alternate'
            alternate.mkdir(mode=0o700)
            rejected(lambda: w.manager.bind(**dict(w.args, workspace=str(alternate))), 'native_session_binding_changed')
            rejected(lambda: w.manager.request_turn(session_id=w.session.session_id, run_id=w.run.run_id,
                revision=1, invocation_id='pc-missing'), 'native_active_cost_claim_required')
            other_task, other_run = w.starts.create(objective='Anderer Auftrag.', scope='research',
                origin='trusted_dashboard', principal='owner',
                receipt=VerifiedTaskReceipt('dashboard_session', 'start:other', 'owner'), request_id='native-start-other')
            rejected(lambda: w.manager.bind(**dict(w.args, task_id=other_task.task_id, run_id=other_run.run_id)),
                     'native_workspace_other_task')
            async def native():
                claim = w.manager.active_claim(w.run.run_id)
                rejected(lambda: w.manager.request_turn(session_id=w.session.session_id, run_id=w.run.run_id,
                    revision=2, invocation_id=claim['invocation_id']), 'native_revision_changed')
                rejected(lambda: w.manager.request_turn(session_id=w.session.session_id, run_id=w.run.run_id,
                    revision=1, invocation_id=claim['invocation_id'], request_digest='b' * 64), 'native_cost_binding_changed')
                turn, _ = request(w)
                w.manager.bind_thread(turn.invocation_id, 'native-thread-1')
                rejected(lambda: w.manager.bind_thread(turn.invocation_id, 'native-thread-other'),
                         'native_thread_binding_changed')
                w.manager.started(turn.invocation_id, native_thread_id='native-thread-1', native_turn_id='turn-1')
                rejected(lambda: w.manager.terminal(turn.invocation_id, native_thread_id='native-thread-1',
                    native_turn_id='turn-other', status='completed'), 'native_turn_binding_changed')
                w.manager.unknown(turn.invocation_id)
                rejected(lambda: w.manager.terminal(turn.invocation_id, native_thread_id='native-thread-1',
                    native_turn_id='turn-1', status='completed'), 'native_terminal_recovery_required')
            await dispatch(w, 'bound', native)
    asyncio.run(go())


def t_cancellation_after_native_start_keeps_actual_terminal_without_new_authority():
    async def go():
        with world() as w:
            async def native():
                turn, _ = request(w)
                w.manager.bind_thread(turn.invocation_id, 'native-thread-1')
                w.ledger.transition(w.run.run_id, S.CANCELLED)
                w.manager.started(turn.invocation_id, native_thread_id='native-thread-1', native_turn_id='turn-1')
                w.manager.terminal(turn.invocation_id, native_thread_id='native-thread-1', native_turn_id='turn-1', status='interrupted')
                rejected(lambda: w.manager.bind(**w.args), 'native_task_authority_required')
            await dispatch(w, 'cancelled', native)
            require_equal(w.manager.latest_turn(w.session.session_id).terminal_status, 'interrupted')
    asyncio.run(go())


def t_explicit_owner_revision_retains_thread_workspace_and_task_cost_subject():
    async def go():
        with world() as w:
            async def first():
                turn, _ = request(w)
                finish(w, turn)
            await dispatch(w, 'first', first)
            w.ledger.transition(w.run.run_id, S.SUCCEEDED, result_summary='Quellen verglichen.')
            w.ledger.set_task_state(w.task.task_id, S.TASK_COMPLETED)
            current = TR.revision_for_run(w.ledger, w.run.run_id)
            prepared = TR.prepare(w.ledger, dict(run_id=w.run.run_id, text='Prüfe zusätzlich Quelle drei.',
                expected_revision=current['revision'], expected_digest=current['digest'], input_artifact_ids=[],
                client_request_id='native-followup-one'), VerifiedTaskReceipt('dashboard_session', 'followup:one', 'owner'))
            task, run = w.starts.admit_followup(prepared)
            retained = w.manager.bind(**dict(w.args, run_id=run.run_id))
            require_equal((retained.task_id, retained.workspace, retained.native_thread_id),
                          (task.task_id, str(w.workspace), 'native-thread-1'))
            async def second():
                turn, fresh = request(w, run_id=run.run_id, revision=2)
                require(fresh)
                require_equal(turn.revision, 2)
                finish(w, turn, turn_id='native-turn-2')
            result = await dispatch(w, 'second', second, run_id=run.run_id)
            require_equal(result.cost_status, 'settled')
            require_equal(w.costs.view(task.task_id)['counts'], {'settled': 2})
            require_equal({row['task_id'] for row in D.invocations(w.ledger, task.task_id)}, {task.task_id})
            require_equal(w.ledger.get_run(w.run.run_id).state, S.SUCCEEDED)
    asyncio.run(go())


def t_verified_no_start_allows_later_attempt_and_retains_previous_native_turn():
    async def go():
        with world() as w:
            async def first():
                turn, _ = request(w)
                finish(w, turn)
            await dispatch(w, 'first', first)
            async def no_start():
                turn, _ = request(w)
                w.manager.not_started(turn.invocation_id)
                require_equal(w.manager.not_started(turn.invocation_id).terminal_status, 'not_started')
            await dispatch(w, 'quota-before-turn', no_start)
            require_equal(w.manager.latest_turn(w.session.session_id).terminal_status, 'not_started')
            require_equal(w.manager.latest_native_turn(w.session.session_id).native_turn_id, 'native-turn-1')
            async def later():
                turn, fresh = request(w)
                require(fresh)
                w.manager.bind_thread(turn.invocation_id, 'native-thread-1')
                w.manager.started(turn.invocation_id, native_thread_id='native-thread-1', native_turn_id='native-turn-2')
                rejected(lambda: w.manager.not_started(turn.invocation_id), 'native_no_start_unproven')
                w.manager.terminal(turn.invocation_id, native_thread_id='native-thread-1',
                    native_turn_id='native-turn-2', status='completed')
            await dispatch(w, 'later', later)
            require_equal(w.costs.view(w.task.task_id)['counts'], {'settled': 3})
    asyncio.run(go())


def t_parallel_attempt_cannot_start_another_native_turn_in_the_same_task():
    async def go():
        with world() as w:
            requested, release = asyncio.Event(), asyncio.Event()
            async def first():
                turn, _ = request(w)
                requested.set()
                await release.wait()
                finish(w, turn)
            active = asyncio.create_task(dispatch(w, 'parallel-one', first))
            await asyncio.wait_for(requested.wait(), 2)
            async def second():
                rejected(lambda: request(w), 'native_turn_recovery_required')
                release.set()
            await dispatch(w, 'parallel-two', second)
            await active
            require_equal(w.manager.latest_turn(w.session.session_id).native_turn_id, 'native-turn-1')
    asyncio.run(go())


# --------------------------------------------------------------------------
# N8/C4 — the closed provider set: Codex and Claude Code as task workers on
# the SAME workspace; anything else stays refused. The Claude turn cases run
# against the product's claim lookup (cost_dispatch.active_task_invocation
# names both native worker providers); no test seam patches it.
# --------------------------------------------------------------------------
CLAUDE_PROFILE = 'worker/claude'


def t_claude_session_binds_on_the_same_workspace_and_foreign_providers_stay_refused():
    with world() as w:
        claude = w.manager.bind(**dict(w.args, provider='claude-code', profile=CLAUDE_PROFILE, policy_digest='c' * 64))
        require(claude.session_id != w.session.session_id)
        require_equal((claude.task_id, claude.provider, claude.profile, claude.workspace),
                      (w.task.task_id, 'claude-code', CLAUDE_PROFILE, str(w.workspace)))
        require_equal(w.manager.bind(**dict(w.args, provider='claude-code', profile=CLAUDE_PROFILE,
                                            policy_digest='c' * 64)), claude)
        for provider in ('hermes', 'anthropic-api', 'openai-api', 'claude', ''):
            rejected(lambda: w.manager.bind(**dict(w.args, provider=provider, profile=CLAUDE_PROFILE,
                                                   policy_digest='c' * 64)), 'native_provider_unsupported')
        with w.ledger._open() as db:
            rows = db.execute('SELECT provider, workspace FROM agent_native_sessions WHERE task_id=? ORDER BY provider',
                              (w.task.task_id,)).fetchall()
        require_equal([(r['provider'], r['workspace']) for r in rows],
                      [('claude-code', str(w.workspace)), ('codex', str(w.workspace))])


def t_claude_turn_waits_until_the_codex_turn_of_the_task_is_terminal_and_settled():
    async def go():
        with world() as w:
            claude = w.manager.bind(**dict(w.args, provider='claude-code', profile=CLAUDE_PROFILE, policy_digest='c' * 64))
            requested, release = asyncio.Event(), asyncio.Event()
            async def codex_turn():
                turn, _ = request(w)
                requested.set()
                await release.wait()
                finish(w, turn)
            active = asyncio.create_task(dispatch(w, 'codex-one', codex_turn))
            await asyncio.wait_for(requested.wait(), 2)
            async def claude_turn_blocked():
                rejected(lambda: request(w, session=claude), 'native_turn_recovery_required')
                release.set()
            await dispatch(w, 'claude-blocked', claude_turn_blocked, provider='claude-code')
            await active
            async def codex_claim_for_claude():
                rejected(lambda: request(w, session=claude), 'native_cost_binding_changed')
            await dispatch(w, 'claude-under-codex-claim', codex_claim_for_claude)
            async def claude_turn():
                turn, fresh = request(w, session=claude)
                require(fresh)
                require_equal(turn.session_id, claude.session_id)
                w.manager.bind_thread(turn.invocation_id, '11111111-2222-4333-8444-555555555555')
                w.manager.started(turn.invocation_id, native_thread_id='11111111-2222-4333-8444-555555555555',
                                  native_turn_id='11111111-2222-4333-8444-555555555555/' + turn.invocation_id)
                w.manager.terminal(turn.invocation_id, native_thread_id='11111111-2222-4333-8444-555555555555',
                                   native_turn_id='11111111-2222-4333-8444-555555555555/' + turn.invocation_id,
                                   status='completed')
            result = await dispatch(w, 'claude-one', claude_turn, provider='claude-code')
            require_equal(result.cost_status, 'settled')
            require_equal(w.costs.view(w.task.task_id)['counts'], {'settled': 4})
            require_equal({row['task_id'] for row in D.invocations(w.ledger, w.task.task_id)}, {w.task.task_id})
            require_equal(w.manager.latest_turn(claude.session_id).state, 'terminal')
            require_equal(w.manager.latest_turn(w.session.session_id).native_turn_id, 'native-turn-1')
    asyncio.run(go())


def t_claude_no_start_turn_does_not_block_the_codex_session():
    async def go():
        with world() as w:
            claude = w.manager.bind(**dict(w.args, provider='claude-code', profile=CLAUDE_PROFILE, policy_digest='c' * 64))
            async def no_start():
                turn, _ = request(w, session=claude)
                w.manager.not_started(turn.invocation_id)
            await dispatch(w, 'claude-quota', no_start, provider='claude-code')
            require_equal(w.manager.latest_turn(claude.session_id).terminal_status, 'not_started')
            async def codex_turn():
                turn, fresh = request(w)
                require(fresh)
                finish(w, turn)
            require_equal((await dispatch(w, 'codex-after', codex_turn)).cost_status, 'settled')
    asyncio.run(go())


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
