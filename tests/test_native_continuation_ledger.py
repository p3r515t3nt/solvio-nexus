"""Real native continuation transport composed with task/grant/cost/revision APIs.

The installed Hermes client/session, launcher and subscription dispatch run;
only Codex is replaced by the existing local JSON-RPC process fixture. This is
an isolated integration proof, not a provider-model or public-HTTP acceptance.
"""
from contextlib import contextmanager
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_native_sessions as M
import test_native_continuation_transport as T
from solvio.agent_runtime import cost_dispatch as D, store as S, task_revisions as TR, result_files as F
from solvio.agent_runtime import native_sessions as NS, specialists as SP
from solvio.agent_runtime.task_authority import VerifiedTaskReceipt
from solvio.specialists import hermes_native as N


@contextmanager
def fixture(mode='ok'):
    with T.fixture(mode) as (root, config, _unused_task, _unused_workspace), M.world() as w:
        w.args = dict(w.args, profile='researcher/hermes', policy_digest=N.continuation_policy(config))
        w.session = w.manager.bind(**w.args)
        w.continuation = N.NativeContinuation(w.manager, w.session.session_id)
        w.root, w.config = root, config
        yield w


async def call(w, operation, *, run_id=None, objective='Prüfe die erste synthetische Quelle.', quote=None):
    run_id = run_id or w.run.run_id
    request = SP.SpecialistRequest('researcher/hermes', objective, '', run_id=run_id)
    with D.task_cost_scope(w.ledger, task_id=w.task.task_id, run_id=run_id, phase='specialist',
            operation_id=operation, quote_adapter=quote or (lambda *_: M.FREE)):
        return await N.run_research(request, config=w.config, continuation=w.continuation)


def app_server_processes(w):
    return [row for row in T.B.observed(w.root) if row.get('argv', [None])[0] == 'app-server']


async def t_native_revision_two_uses_same_thread_workspace_cost_subject_and_preserves_old_file():
    with fixture() as w:
        first = await call(w, 'first')
        require(first.result.ok, first.result.reason)
        first_turn = w.manager.latest_native_turn(w.session.session_id)
        require_equal((first_turn.state, first_turn.revision), ('terminal', 1))
        require_equal(w.manager.session(w.session.session_id).native_thread_id, 'local-thread')
        old_bytes = json.dumps(first.result.findings, ensure_ascii=False).encode()
        step = w.ledger.create_step(run_id=w.run.run_id, seq=1, kind='specialist', specialist_profile='researcher/hermes')
        w.ledger.update_step(step.step_id, state='running', started=True)
        old_file = F.publish_file(w.ledger, w.run.run_id, step.step_id, old_bytes,
                                 'Erster-Befund.txt', 'text/plain', requirement='r1')
        w.ledger.update_step(step.step_id, state='succeeded', finished=True)
        w.ledger.transition(w.run.run_id, S.SUCCEEDED, result_summary='Erster nativer Befund ist erhalten.')
        w.ledger.set_task_state(w.task.task_id, S.TASK_COMPLETED)
        old_run = w.ledger.get_run(w.run.run_id)
        before = TR.revision_for_run(w.ledger, w.run.run_id)
        prepared = TR.prepare(w.ledger, dict(run_id=w.run.run_id, text='Prüfe zusätzlich die dritte Quelle.',
            expected_revision=before['revision'], expected_digest=before['digest'], input_artifact_ids=[],
            client_request_id='native-real-followup'), VerifiedTaskReceipt('dashboard_session', 'followup:real', 'owner'))
        task, revised = w.starts.admit_followup(prepared)
        # Recreate the metadata adapter; the actual second worker/peer are fresh
        # processes. No in-memory Hermes session is reused by this test.
        w.manager = NS.NativeSessions(w.ledger)
        w.continuation = N.NativeContinuation(w.manager, w.session.session_id)
        answer = dict(findings=['Die zweite native Antwort nach echter Fortsetzung.'],
            evidence=['https://example.org/second'], assumptions=[], uncertainties=[],
            recommended_path='Beide Befunde berücksichtigen.', rejected_alternatives=[], risk_notes=[], confidence='hoch')
        (w.root / 'answer.json').write_text(json.dumps(answer))
        second = await call(w, 'second', run_id=revised.run_id, objective='Prüfe die weitere synthetische Quelle.')
        require(second.result.ok, second.result.reason)
        require_equal(second.result.findings, answer['findings'])
        second_turn = w.manager.latest_native_turn(w.session.session_id)
        require_equal((second_turn.revision, second_turn.run_id, second_turn.native_turn_id),
                      (2, revised.run_id, 'turn-2'))
        require_equal(first.native_thread_id, second.native_thread_id)
        require_equal(w.ledger.get_run(w.run.run_id), old_run)
        require_equal(F.read_result(w.ledger, w.run.run_id, old_file['id'])[1], old_bytes)
        require_equal(w.costs.view(task.task_id)['counts'], {'settled': 2})
        calls = D.invocations(w.ledger, task.task_id)
        require_equal({row['task_id'] for row in calls}, {task.task_id})
        require_equal({row['run_id'] for row in calls}, {w.run.run_id, revised.run_id})
        require_equal({row['state'] for row in calls}, {'finished'})
        require_equal(len({row['pid'] for row in app_server_processes(w)}), 2)
        require_equal(len(T.B.method_rows(w.root, 'thread/start')), 1)
        require_equal(len(T.B.method_rows(w.root, 'thread/read')), 1)
        require_equal(len(T.B.method_rows(w.root, 'thread/resume')), 1)
        require_equal(len(T.B.method_rows(w.root, 'turn/start')), 2)
        require_equal(T.B.method_rows(w.root, 'thread/start')[0]['params']['cwd'], str(w.workspace))
        require_equal(T.B.method_rows(w.root, 'thread/resume')[0]['params']['cwd'], str(w.workspace))
        require(w.workspace.is_dir())


async def t_native_quota_before_turn_is_no_start_and_can_resume_without_losing_previous_turn():
    with fixture() as w:
        require((await call(w, 'first')).result.ok)
        (w.root / 'mode').write_text('quota_before')
        quota = await call(w, 'quota')
        require_equal(quota.result.reason, 'quota')
        require_equal(w.manager.latest_turn(w.session.session_id).terminal_status, 'not_started')
        require_equal(w.manager.latest_native_turn(w.session.session_id).native_turn_id, 'turn-1')
        require_equal(len(T.B.method_rows(w.root, 'turn/start')), 1)
        (w.root / 'mode').write_text('ok')
        continued = await call(w, 'owner-resumed')
        require(continued.result.ok, continued.result.reason)
        require_equal(continued.native_turn_id, 'turn-2')
        require_equal(len(T.B.method_rows(w.root, 'thread/start')), 1)
        require_equal(len(T.B.method_rows(w.root, 'thread/resume')), 1)
        require_equal(w.costs.view(w.task.task_id)['counts'], {'settled': 3})


async def t_lost_native_completion_remains_unknown_and_refuses_a_second_send():
    with fixture('disconnect') as w:
        first = await call(w, 'first')
        require_equal(first.result.reason, 'cost_recovery_required')
        original = w.manager.latest_turn(w.session.session_id)
        require_equal(original.state, 'unknown')
        require_equal(len(T.B.method_rows(w.root, 'turn/start')), 1)
        before = len(app_server_processes(w))
        (w.root / 'mode').write_text('ok')
        second = await call(w, 'second')
        require_equal(second.result.reason, 'cost_recovery_required')
        require_equal(len(app_server_processes(w)), before)
        require_equal(len(T.B.method_rows(w.root, 'turn/start')), 1)
        require_equal(w.manager.latest_turn(w.session.session_id), original)
        require_equal(w.costs.view(w.task.task_id)['counts'], {'unknown': 1})


async def t_stale_prepared_invocation_cannot_start_a_fresh_thread_after_another_turn_finished():
    with fixture() as w:
        prepared, release = asyncio.Event(), asyncio.Event()
        async def delayed_quote(*_):
            prepared.set()
            await release.wait()
            return M.FREE
        delayed = asyncio.create_task(call(w, 'prepared-before-first', quote=delayed_quote))
        await asyncio.wait_for(prepared.wait(), 3)
        try:
            first = await call(w, 'first')
            require(first.result.ok, first.result.reason)
        finally:
            release.set()
        stale = await delayed
        require(not stale.result.ok, 'stale invocation launched a new native thread')
        require_equal(len(T.B.method_rows(w.root, 'thread/start')), 1)
        require_equal(len(T.B.method_rows(w.root, 'turn/start')), 1)
        require_equal(w.manager.latest_native_turn(w.session.session_id).native_turn_id, 'turn-1')
        require_equal(w.costs.view(w.task.task_id)['counts'], {'settled': 1, 'released': 1})


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
