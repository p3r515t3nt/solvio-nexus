"""Missing research input: actual planner, grants, answer binding and recovery.

Only provider answers are local fixtures. No public lookup or device needed.
"""
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_research_refinement import world, drive, advance, REQUIREMENTS
from solvio.agent_runtime import requirements as RQ, action_intent as AI, research_question as Q, store as S, planner as PL, orchestrator as O
from solvio.agent_runtime.task_authority import VerifiedTaskReceipt

QUESTION = 'Von welcher Stadt oder welchem Flughafen möchtest du abfliegen?'
PROVISIONAL = dict(REQUIREMENTS, unklar=[{'id': 'u1', 'text': 'Der Abflugort fehlt.'}])
RECEIPT = VerifiedTaskReceipt('dashboard_session', 'browser:research-answer-001', 'owner')


def questions(w, count=1):
    call = w.planner._call
    async def wrapped(**kw):
        result = await call(**kw)
        if len(w.planner.plans) <= count:
            result.text = json.dumps({'schritte': [], 'rueckfrage': QUESTION,
                'anforderungen': kw.get('bound_requirements') or PROVISIONAL})
        return result
    w.planner._call = wrapped


def body(w, answer='Frankfurt am Main', request='research-answer-001'):
    q = AI.view(w.ledger, w.run.run_id)['question']
    return {'run_id': w.run.run_id, 'question_id': q['id'], 'expected_revision': q['revision'],
        'expected_digest': q['digest'], 'answer': answer, 'client_request_id': request}


def rejects(call):
    try: call()
    except (ValueError, TypeError): return
    raise AssertionError('expected rejection')


def t_missing_departure_is_answerable_before_research_and_continues_same_task():
    with world(first_good=True) as w:
        questions(w)
        async def case():
            run = await drive(w)
            require_equal(run.state, S.WAITING_USER)
            require_equal(w.requests, [])
            require_equal(len(w.planner.plans), 1)
            require_equal(AI.view(w.ledger, run.run_id)['question']['prompt'], QUESTION)
            require_equal(await w.orch.resume(run.run_id), False)
            original = w.ledger.get_task(w.task.task_id)
            require_equal(original.requirements, '')
            grant = w.orch.task_authority.for_run(run.run_id)
            request = body(w)
            require_equal(AI.answer(w.ledger, request, RECEIPT)['status'], 'interpreting')
            require_equal(AI.answer(w.ledger, request, RECEIPT)['status'], 'interpreting')
            final = await drive(w)
            require_equal(final.state, S.SUCCEEDED)
            require_equal((final.plan_revision, len(w.planner.plans), len(w.requests)), (1, 2, 1))
            require('Frankfurt am Main' in w.planner.plans[1]['context'])
            require('Frankfurt am Main' in w.requests[0].research_briefing)
            assessed = json.loads(w.planner.assessments[0]['snapshot_body'])
            require('Frankfurt am Main' in assessed['nutzerangaben'])
            require(assessed['nutzerangaben'] not in RQ.snapshot_references(assessed))
            verdict = json.loads(final.completion_verdict)
            saved = RQ.read_snapshot(w.ledger, final.run_id, verdict['snapshot'])
            require_equal(saved['nutzerangaben'], assessed['nutzerangaben'])
            resolved = w.ledger.get_task(w.task.task_id)
            require_equal(resolved.objective, original.objective)
            require_equal(RQ.load(resolved.requirements, objective=resolved.objective)[RQ.UNCLEAR], [])
            require_equal(w.orch.task_authority.for_run(run.run_id), grant)
            require_equal(len(w.ledger.runs_for_task(w.task.task_id)), 1)
        asyncio.run(case())


def t_wait_and_answer_survive_restart_without_new_task_or_repeated_call():
    with world(first_good=True) as w:
        # A contract bound before this run is immutable, including across a
        # clarification. Only a previously unbound proposal can be deferred.
        fixed = json.dumps(RQ.validate(REQUIREMENTS, objective=w.task.objective))
        require(w.ledger.bind_requirements(w.task.task_id, fixed))
        questions(w)
        async def case():
            await drive(w); request = body(w)
            w.orch = O.Orchestrator(ledger=w.ledger, planner=w.planner, researcher=object())
            await w.orch.reconcile()
            require_equal(AI.view(w.ledger, w.run.run_id)['question']['id'], request['question_id'])
            AI.answer(w.ledger, request, RECEIPT)
            w.orch = O.Orchestrator(ledger=w.ledger, planner=w.planner, researcher=object())
            await w.orch.reconcile()
            require_equal((await drive(w)).state, S.SUCCEEDED)
            require_equal((len(w.planner.plans), len(w.requests)), (2, 1))
            require_equal(w.ledger.get_task(w.task.task_id).requirements, fixed)
        asyncio.run(case())


def t_answer_rejects_wrong_owner_stale_binding_conflicting_replay_and_cancel():
    with world() as w:
        questions(w); asyncio.run(drive(w)); request = body(w)
        rejects(lambda: AI.answer(w.ledger, request, VerifiedTaskReceipt('dashboard_session', 'other-answer', 'other')))
        rejects(lambda: AI.answer(w.ledger, request | {'expected_digest': 'f'*64}, RECEIPT))
        require_equal(w.ledger.get_run(w.run.run_id).state, S.WAITING_USER)
        AI.answer(w.ledger, request, RECEIPT)
        rejects(lambda: AI.answer(w.ledger, request | {'answer': 'Berlin'}, RECEIPT))
        rejects(lambda: AI.answer(w.ledger, request | {'client_request_id': 'different-answer-001'}, RECEIPT))
        require_equal(w.ledger.get_run(w.run.run_id).plan_revision, 1)
    with world() as w:
        questions(w); asyncio.run(drive(w)); request = body(w)
        asyncio.run(w.orch.cancel(w.run.run_id))
        rejects(lambda: AI.answer(w.ledger, request, RECEIPT))
        require_equal(w.ledger.get_run(w.run.run_id).state, S.CANCELLED)


def t_changed_answer_receipt_or_question_cannot_become_research_context():
    for field in ('answer', 'receipt', 'question'):
        with world() as w:
            questions(w); asyncio.run(drive(w)); request = body(w)
            if field == 'question':
                with w.ledger._open() as db:
                    raw = json.loads(w.ledger.get_run(w.run.run_id).boundary)
                    raw['research_question']['question']['prompt'] = 'Andere Frage'
                    db.execute('UPDATE agent_runs SET boundary=? WHERE run_id=?', (json.dumps(raw), w.run.run_id))
                rejects(lambda: AI.answer(w.ledger, request, RECEIPT))
            else:
                AI.answer(w.ledger, request, RECEIPT)
                with w.ledger._open() as db:
                    column = 'answer_json' if field == 'answer' else 'receipt_json'
                    raw = json.loads(db.execute('SELECT '+column+' FROM agent_action_intent_answers').fetchone()[0])
                    if field == 'answer': raw['body']['answer'] = 'Andere Angaben'
                    else: raw['authorizer'] = 'other'
                    db.execute('UPDATE agent_action_intent_answers SET '+column+'=?', (json.dumps(raw),))
                rejects(lambda: Q.context(w.ledger, w.run.run_id))


def t_questions_consume_existing_revisions_and_do_not_create_unbounded_work():
    with world() as w:
        questions(w, count=10)
        async def case():
            for index in range(2):
                require_equal((await drive(w)).state, S.WAITING_USER)
                AI.answer(w.ledger, body(w, request='research-answer-'+str(index)), RECEIPT)
            final = await drive(w)
            require_equal(final.state, S.FAILED)
            require_equal(final.failure_category, 'budget_exhausted')
            require_equal((len(w.planner.plans), final.plan_revision, len(w.requests)), (3, 2, 0))
        asyncio.run(case())


def t_question_schema_never_authorizes_work_or_asks_alongside_execution():
    for scope, raw in [('action', {'schritte': [], 'rueckfrage': QUESTION}),
                       ('research', {'schritte': [{'art': 'verify'}], 'rueckfrage': QUESTION}),
                       ('research', {'schritte': [], 'rueckfrage': 'x'*401}),
                       ('research', {'schritte': []})]:
        rejects(lambda: PL.validate(raw, scope=scope, allowed_profiles=set(), known_capabilities=set(), goal='test'))


async def t_https_owner_answer_uses_existing_endpoint_and_same_cost_subject():
    from test_agent_public_research import world as native_world, PLAN
    from solvio.agent_runtime import cost_dispatch as CD
    question_plan = dict(PLAN, schritte=[], rueckfrage=QUESTION,
        anforderungen=dict(PLAN['anforderungen'], unklar=PROVISIONAL['unklar']))
    async with native_world(plan=question_plan, followup_plan=PLAN) as w:
        accepted, grant = await w.admit()
        run_id = accepted['run_id']
        waiting = await w.tick_until(run_id, S.WAITING_USER)
        require_equal(waiting.specialist_count, 0)
        response = await w.client.get('/v1/agent/runs/' + run_id)
        require_equal(response.status, 200)
        projection = await response.json()
        question = projection['action_intent']['question']
        require_equal((projection['zustand_code'], projection['offen']), (S.WAITING_USER, True))
        answer = {'question_id': question['id'], 'expected_revision': question['revision'],
            'expected_digest': question['digest'], 'answer': 'Frankfurt', 'client_request_id': 'http-research-answer-001'}
        denied = await w.client.post('/v1/agent/runs/' + run_id + '/action-answer', json=answer)
        require_equal(denied.status, 401)
        require_equal(w.ledger.get_run(run_id).state, S.WAITING_USER)
        result = await w.client.post('/v1/agent/runs/' + run_id + '/action-answer', json=answer, headers=w.headers)
        require_equal(result.status, 200, await result.text())
        require_equal((await result.json())['action_intent']['status'], 'interpreting')
        replay = await w.client.post('/v1/agent/runs/' + run_id + '/action-answer', json=answer, headers=w.headers)
        require_equal(replay.status, 200)
        final = await w.tick_until(run_id, S.SUCCEEDED)
        require_equal(final.task_id, accepted['task_id'])
        require_equal(final.specialist_count, 1)
        require_equal([c['kind'] for c in w.calls()], ['plan', 'plan', 'assessment'])
        require('Frankfurt' in w.calls()[1]['request']['kontext'])
        require('Frankfurt' in json.loads(w.calls()[-1]['request']['ergebnis'])['nutzerangaben'])
        require_equal(len(w.ledger.runs_for_task(final.task_id)), 1)
        require_equal(w.orch.task_authority.for_run(run_id).reference, grant.reference)
        require_equal([c['phase'] for c in CD.invocations(w.ledger, final.task_id)],
                      ['plan', 'plan', 'specialist', 'assessment'])


def t_lost_claimed_answer_plan_is_not_silently_repeated_after_restart():
    with world() as w:
        questions(w)
        async def case():
            await drive(w)
            AI.answer(w.ledger, body(w), RECEIPT)
            require(Q.pending_plan(w.ledger, w.run.run_id, claim=True))
            w.orch = O.Orchestrator(ledger=w.ledger, planner=w.planner, researcher=object())
            await w.orch.reconcile()
            final = await drive(w)
            require_equal(final.state, S.FAILED)
            require_equal((len(w.planner.plans), len(w.requests)), (1, 0))
        asyncio.run(case())


def t_question_and_wait_state_commit_together_before_process_loss():
    from unittest.mock import patch
    class ProcessLost(BaseException): pass
    with world() as w:
        questions(w)
        open_question = Q.open_question
        def crash_after_commit(*args):
            open_question(*args)
            raise ProcessLost()
        async def case():
            try:
                with patch.object(Q, 'open_question', crash_after_commit):
                    await drive(w)
            except ProcessLost: pass
            else: raise AssertionError('crash hook was not reached')
            require_equal(w.ledger.get_run(w.run.run_id).state, S.WAITING_USER)
            require_equal(AI.view(w.ledger, w.run.run_id)['question']['prompt'], QUESTION)
            w.orch = O.Orchestrator(ledger=w.ledger, planner=w.planner, researcher=object())
            await w.orch.reconcile()
            require_equal((await drive(w)).state, S.WAITING_USER)
            require_equal((len(w.planner.plans), len(w.requests)), (1, 0))
        asyncio.run(case())


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
