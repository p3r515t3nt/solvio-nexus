"""Actual temporary HTTPS + Owner/AppAttest doors and same-task admission.

The previous completed result is a local ledger fixture, not model execution.
No providers, real devices, native service effects, or production state.
"""
import asyncio
import base64
import hashlib
import json
import os
import sys
import threading
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_agent_task_entry import world, BODY
import mobile_attest_helper as H
from solvio.agent_runtime import store as S, requirements as RQ, result_files as F, task_revisions as TR
from solvio.agent_runtime import task_followup_endpoint as E, task_start_proof as T
from solvio.security.mobile_approval import app_attest as AA, browser_sessions as B, protocol as P


async def parent(w, *, request_id='followup-parent-001', negative=False):
    initial = await w.start(dict(BODY, client_request_id=request_id, **({'objective':
        'Suche bei Amazon einen Weichschalenkoffer mit exakt 46 × 31 × 78 cm unter 70 €.'} if negative else {})))
    require_equal(initial.status, 201)
    accepted = await initial.json()
    task_id, run_id = accepted['task_id'], accepted['run_id']
    task = w.ledger.get_task(task_id)
    requirements = RQ.validate({'auskunft': [{'id': 'r1', 'text': 'Vergleiche die Werte.'}]}, objective=task.objective)
    require(w.ledger.bind_requirements(task_id, json.dumps(requirements)))
    w.ledger.transition(run_id, S.PLANNING); w.ledger.transition(run_id, S.RUNNING)
    step = w.ledger.create_step(run_id=run_id, seq=1, kind='specialist', specialist_profile='researcher/hermes')
    w.ledger.update_step(step.step_id, state='running', started=True)
    file = F.publish_file(w.ledger, run_id, step.step_id, b'Monat,Umsatz\nJuli,12\nAugust,18\n',
                          'Vergleich.csv', 'text/csv', requirement='r1')
    w.ledger.update_step(step.step_id, state='succeeded', finished=True)
    w.ledger.transition(run_id, S.FAILED if negative else S.SUCCEEDED,
        failure_category='goal_unverified' if negative else '', result_summary=(
            'Kein zugleich maßgerechter und unter 70 € liegender Treffer bestätigt.' if negative else 'Der Vergleich ist fertig.'))
    w.ledger.set_task_state(task_id, S.TASK_FAILED if negative else S.TASK_COMPLETED)
    return task_id, run_id, file


def request(w, run_id, **changes):
    revision = TR.revision_for_run(w.ledger, run_id)
    return {'run_id': run_id, 'text': 'Erkläre den Unterschied genauer.', 'expected_revision': revision['revision'],
            'expected_digest': revision['digest'], 'input_artifact_ids': [], 'client_request_id': 'followup-request-001', **changes}


async def post(w, value, *, client=None, headers=None):
    return await (client or w.client).post('/v1/agent/runs/' + value['run_id'] + '/followup',
                                          json=value, headers=headers if headers is not None else w.headers)


async def proof(w, app, device, headers, value, counter, hash_function=E.client_data_hash):
    path = '/v1/agent/runs/' + value['run_id'] + '/followup'
    response = await app.post(path + '/challenge', json={'followup': value}, headers=headers)
    require_equal(response.status, 200, await response.text())
    challenge = await response.json()
    raw = base64.b64decode(challenge['binding_b64'])
    require_equal(json.loads(raw)['type'], E.TYPE_TASK_FOLLOWUP)
    require_equal(challenge['request_digest'], E.request_digest(value))
    assertion = AA.fake_assertion(device.aakey, hash_function(raw), counter)
    return {'followup': value, 'proof': {'nonce': challenge['nonce'], 'assertion_b64': base64.b64encode(assertion).decode()}}


async def t_browser_same_task_fresh_grant_and_cumulative_costs_with_bound_files():
    async with world() as w:
        task_id, run_id, file = await parent(w)
        before = w.ledger.get_run(run_id)
        value = request(w, run_id, input_artifact_ids=[file['id']])
        response = await post(w, value); require_equal(response.status, 202, await response.text())
        accepted = await response.json()
        require_equal(accepted['task_id'], task_id); require(accepted['run_id'] != run_id)
        require_equal(accepted['parent_run_id'], run_id); require_equal(accepted['revision'], 2)
        require_equal(accepted['annahme'], 'ready')
        require_equal(w.ledger.get_run(run_id), before)
        require_equal(w.ledger.get_task(task_id).objective, BODY['objective'])
        grant = w.orch.task_authority.for_run(accepted['run_id'])
        require_equal(grant.receipt_method, 'dashboard_session')
        require_equal({entry.name for entry in grant.capabilities}, {'file_process'})
        require_equal(TR.read_inputs(w.ledger, accepted['run_id'])[0].content, F.read_result(w.ledger, run_id, file['id'])[1])
        require_equal(w.orch.costs.view(task_id)['ask_threshold_cents'], 1000)
        with w.ledger._open() as db:
            require_equal(db.execute('SELECT count(*) FROM agent_tasks').fetchone()[0], 1)
            require_equal(db.execute('SELECT count(*) FROM agent_cost_policies').fetchone()[0], 1)
        require_equal(await w.store.list_pending(), [])
        require_equal(sum(r.planner_calls for r in w.ledger.recent_runs()), 0)


async def t_negative_research_browser_amendment_keeps_result_authority_and_cost_subject():
    from solvio.agent_runtime.costs import CostEvidence
    async with world() as w:
        task_id, run_id, file = await parent(w, negative=True)
        before = w.ledger.get_run(run_id); old_grant = w.orch.task_authority.for_run(run_id)
        old_task = w.ledger.get_task(task_id)
        cost = w.orch.costs.reserve_subject(task_id, 'earlier-research-cost', 400, route='fixture',
            evidence=CostEvidence('enforceable_upper_bound', 'synthetic-cost'))
        w.orch.costs.settle(cost.reservation_id, 400, CostEvidence('actual_charge', 'synthetic-cost'))
        detail = await (await w.client.get('/v1/agent/runs/' + run_id)).json()
        require_equal(detail['zustand_code'], 'FAILED')
        require_equal(detail['followup'], {'eligible': True, 'reason': ''})
        value = request(w, run_id, text='Bis zu einem Zentimeter Maßabweichung ist erlaubt. Unter 70 € und Amazon bleiben Pflicht.')
        reply = await post(w, value); require_equal(reply.status, 202, await reply.text())
        accepted = await reply.json(); new_id = accepted['run_id']
        require_equal(accepted['task_id'], task_id); require(new_id != run_id)
        require_equal(w.ledger.get_run(run_id), before)
        require_equal(w.ledger.get_task(task_id).requirements, old_task.requirements)
        require_equal(w.ledger.get_task(task_id).objective, old_task.objective)
        grant = w.orch.task_authority.for_run(new_id)
        require(grant.reference != old_grant.reference)
        require_equal(grant.receipt_method, 'dashboard_session')
        require(not w.orch.task_authority.active(old_grant.reference, task_id=task_id, run_id=run_id).allowed)
        new = await (await w.client.get('/v1/agent/runs/' + new_id)).json()
        require_equal(new['task_revision']['text'], value['text'])
        require_equal([r['run_id'] for r in new['task_history']], [run_id, new_id])
        require_equal(new['task_history'][0]['state'], 'FAILED')
        require_equal(w.ledger.get_run(run_id).failure_category, 'goal_unverified')
        require_equal(F.read_result(w.ledger, run_id, file['id'])[0], file)
        with w.ledger._open() as db:
            require_equal(db.execute('SELECT count(*) FROM agent_tasks').fetchone()[0], 1)
            require_equal(db.execute('SELECT count(*) FROM agent_cost_policies').fetchone()[0], 1)
            require_equal(db.execute('SELECT sum(actual_cents) FROM agent_cost_reservations WHERE subject_id=?', (task_id,)).fetchone()[0], 400)
        next_cost = w.orch.costs.reserve_subject(task_id, 'followup-research-cost', 600, route='fixture',
            evidence=CostEvidence('enforceable_upper_bound', 'synthetic-cost'))
        require_equal(next_cost.status, 'approval_required')
        require_equal(await w.store.list_pending(), [])
        require_equal(sum(r.planner_calls for r in w.ledger.recent_runs()), 0)


async def t_negative_research_public_parallel_and_fresh_session_retry_are_one_revision():
    for same in (True, False):
        async with world() as w:
            task_id, run_id, _ = await parent(w, negative=True)
            value = request(w, run_id, text='Ein Zentimeter Maßabweichung ist erlaubt.')
            other_value = value if same else dict(value, text='Zwei Zentimeter sind erlaubt.', client_request_id='negative-other-001')
            replies = await asyncio.gather(post(w, value), post(w, other_value))
            require_equal(sorted(r.status for r in replies), [202, 202] if same else [202, 409])
            successful = next(i for i, response in enumerate(replies) if response.status == 202)
            accepted = await replies[successful].json()
            original_grant = w.orch.task_authority.for_run(accepted['run_id'])
            fresh = await w.new_client(); headers = await w.login(fresh)
            repeated = await post(w, (value, other_value)[successful], client=fresh, headers=headers)
            require_equal(repeated.status, 202); require_equal(await repeated.json(), accepted)
            require_equal(w.orch.task_authority.for_run(accepted['run_id']), original_grant)
            require_equal(len(w.ledger.runs_for_task(task_id)), 2)
            require_equal(w.ledger.get_run(run_id).state, S.FAILED)


async def t_negative_research_app_uses_exact_owner_answer_and_refuses_other_owner():
    async with world() as w:
        task_id, run_id, _ = await parent(w, negative=True)
        value = request(w, run_id, text='Ein Zentimeter Maßabweichung ist erlaubt.')
        other = await w.new_client(); foreign_headers = await w.login(other, 'foreign-owner')
        require_equal((await post(w, value, client=other, headers=foreign_headers)).status, 404)
        device = await H.enroll_attested(w.cp, transport_cred='temporary-negative-transport')
        app = await w.new_client(); headers = {'X-Device-Id': device.device_id, 'X-Transport-Cred': 'temporary-negative-transport'}
        payload = await proof(w, app, device, headers, value, 1)
        path = '/v1/agent/runs/' + run_id + '/followup'
        changed = dict(payload, followup=dict(value, text='Kaufe stattdessen den Koffer.'))
        require_equal((await app.post(path, json=changed, headers=headers)).status, 401)
        response = await app.post(path, json=payload, headers=headers)
        require_equal(response.status, 202, await response.text())
        accepted = await response.json()
        require_equal(w.orch.task_authority.for_run(accepted['run_id']).receipt_method, 'app_session')
        fresh = await proof(w, app, device, headers, value, 2)
        response = await app.post(path, json=fresh, headers=headers)
        require_equal(response.status, 202); require_equal(await response.json(), accepted)
        require_equal(len(w.ledger.runs_for_task(task_id)), 2)


async def t_parallel_and_fresh_login_replay_recover_one_run_exactly():
    async with world() as w:
        task_id, run_id, _ = await parent(w)
        value = request(w, run_id)
        replies = await asyncio.gather(post(w, value), post(w, value))
        require_equal([r.status for r in replies], [202, 202])
        accepted = [await r.json() for r in replies]
        require_equal(accepted[0], accepted[1]); require_equal(len(w.ledger.runs_for_task(task_id)), 2)
        original = w.orch.task_authority.for_run(accepted[0]['run_id'])
        client = await w.new_client(); headers = await w.login(client)
        again = await post(w, value, client=client, headers=headers)
        require_equal(again.status, 202); require_equal(await again.json(), accepted[0])
        require_equal(w.orch.task_authority.for_run(accepted[0]['run_id']), original)
        changed = await post(w, dict(value, text='Ein anderer Text.'))
        require_equal(changed.status, 409); require_equal(len(w.ledger.runs_for_task(task_id)), 2)


async def t_distinct_concurrent_followups_cannot_fork_one_parent():
    async with world() as w:
        task_id, run_id, _ = await parent(w)
        replies = await asyncio.gather(post(w, request(w, run_id)),
            post(w, request(w, run_id, client_request_id='followup-other-001', text='Eine andere Ergänzung.')))
        require_equal(sorted(r.status for r in replies), [202, 409])
        require_equal(len(w.ledger.runs_for_task(task_id)), 2)


async def t_read_projection_retains_original_objective_and_real_revision_history():
    async with world() as w:
        _, run_id, _ = await parent(w)
        original = await (await w.client.get('/v1/agent/runs/' + run_id)).json()
        require_equal(original['followup'], {'eligible': True, 'reason': ''})
        require_equal(original['task_revision']['revision'], 1)
        value = request(w, run_id); new = await (await post(w, value)).json()
        detail = await (await w.client.get('/v1/agent/runs/' + new['run_id'])).json()
        require_equal(detail['auftrag'], BODY['objective'])
        require_equal(detail['task_revision']['text'], value['text'])
        require_equal([r['run_id'] for r in detail['task_history']], [run_id, new['run_id']])
        require_equal(detail['task_history'][0]['result_summary'], original['ergebnis'])
        require_equal(detail['followup']['eligible'], False)
        old = await (await w.client.get('/v1/agent/runs/' + run_id)).json()
        require_equal(old['followup']['eligible'], False)
        runs = (await (await w.client.get('/v1/agent/runs')).json())['laeufe']
        require_equal({r['auftrag'] for r in runs}, {BODY['objective']})
        require_equal({r['task_revision']['revision'] for r in runs}, {1, 2})


async def t_browser_auth_scope_schema_and_parent_mismatches_cannot_admit():
    async with world() as w:
        task_id, run_id, _ = await parent(w); value = request(w, run_id)
        for headers in ({}, {'Origin': w.origin}, dict(w.headers, Origin='https://foreign.example.invalid')):
            require_equal((await post(w, value, headers=headers)).status, 401)
        other = await w.new_client(); headers = await w.login(other, 'foreign-owner')
        require_equal((await post(w, value, client=other, headers=headers)).status, 404)
        for change in ({'origin': 'trusted_dashboard'}, {'input_artifact_ids': ['aa-' + 'f'*16]},
                       {'expected_digest': '0'*64}, {'text': ' changed '}):
            response = await post(w, dict(value, **change))
            require(response.status in {400, 409}, await response.text())
        require_equal((await w.client.post('/v1/agent/runs/ar-'+'f'*16+'/followup', json=value, headers=w.headers)).status, 400)
        require_equal(len(w.ledger.runs_for_task(task_id)), 1)


async def t_revocation_during_file_preparation_prevents_admission():
    async with world() as w:
        task_id, run_id, _ = await parent(w); value = request(w, run_id)
        loop = asyncio.get_running_loop(); entered = asyncio.Event(); release = threading.Event(); original = TR.prepare
        def delayed(*args):
            result = original(*args); loop.call_soon_threadsafe(entered.set)
            require(release.wait(5), 'fixture release timed out')
            return result
        with patch.object(TR, 'prepare', delayed):
            pending = asyncio.create_task(post(w, value))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                response = await w.client.post(B.SESSION_PATH + '/logout', headers=w.headers)
                require_equal(response.status, 200)
            finally:
                release.set()
            require_equal((await pending).status, 401)
        require_equal(len(w.ledger.runs_for_task(task_id)), 1)


async def t_app_requires_fresh_exact_followup_domain_and_supports_fresh_nonce_replay():
    async with world() as w:
        task_id, run_id, _ = await parent(w); value = request(w, run_id)
        device = await H.enroll_attested(w.cp, transport_cred='temporary-followup-transport')
        app = await w.new_client(); headers = {'X-Device-Id': device.device_id, 'X-Transport-Cred': 'temporary-followup-transport'}
        path = '/v1/agent/runs/' + run_id + '/followup'
        require_equal((await app.post(path, json={'followup': value}, headers=headers)).status, 401)
        wrong = await proof(w, app, device, headers, value, 1, T.client_data_hash)
        require_equal((await app.post(path, json=wrong, headers=headers)).status, 401)
        payload = await proof(w, app, device, headers, value, 2)
        changed = dict(payload, followup=dict(value, text='Anderer Auftrag.'))
        require_equal((await app.post(path, json=changed, headers=headers)).status, 401)
        accepted_response = await app.post(path, json=payload, headers=headers)
        require_equal(accepted_response.status, 202, await accepted_response.text()); accepted = await accepted_response.json()
        require_equal((await app.post(path, json=payload, headers=headers)).status, 401)
        fresh = await proof(w, app, device, headers, value, 3)
        again = await app.post(path, json=fresh, headers=headers)
        require_equal(again.status, 202); require_equal(await again.json(), accepted)
        grant = w.orch.task_authority.for_run(accepted['run_id'])
        require_equal(grant.receipt_method, 'app_session')
        from solvio.agent_runtime.cost_subjects import _task_authority_reason
        import time
        with w.ledger._open() as db:
            require_equal(_task_authority_reason(db, source_kind='task', source_ref=grant.reference,
                task_id=task_id, run_id=accepted['run_id'], now=time.time()), '')
        require_equal(len(w.ledger.runs_for_task(task_id)), 2)


async def t_invalid_browser_cookie_never_falls_back_to_attested_device():
    async with world() as w:
        task_id, run_id, _ = await parent(w); value = request(w, run_id)
        device = await H.enroll_attested(w.cp, transport_cred='temporary-followup-transport')
        app = await w.new_client(); headers = {'X-Device-Id': device.device_id, 'X-Transport-Cred': 'temporary-followup-transport'}
        path = '/v1/agent/runs/' + run_id + '/followup'
        payload = await proof(w, app, device, headers, value, 1)
        require_equal((await w.client.post(path, json=payload, headers=headers)).status, 401)
        require_equal((await w.client.post(path + '/challenge', json={'followup': value}, headers=headers)).status, 401)
        # Rejected mixed credentials never consumed the genuine device proof.
        require_equal((await app.post(path, json=payload, headers=headers)).status, 202)
        require_equal(len(w.ledger.runs_for_task(task_id)), 2)


async def t_consumed_app_proof_is_rechecked_after_preparation_await():
    async with world() as w:
        task_id, run_id, _ = await parent(w); value = request(w, run_id)
        device = await H.enroll_attested(w.cp, transport_cred='temporary-followup-transport')
        app = await w.new_client(); headers = {'X-Device-Id': device.device_id, 'X-Transport-Cred': 'temporary-followup-transport'}
        payload = await proof(w, app, device, headers, value, 1)
        loop = asyncio.get_running_loop(); entered = asyncio.Event(); release = threading.Event(); original = TR.prepare
        def delayed(*args):
            result = original(*args); loop.call_soon_threadsafe(entered.set)
            require(release.wait(5), 'fixture release timed out')
            return result
        with patch.object(TR, 'prepare', delayed):
            pending = asyncio.create_task(app.post('/v1/agent/runs/' + run_id + '/followup', json=payload, headers=headers))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                await w.cp.revoke_device(device.device_id)
            finally:
                release.set()
            require_equal((await pending).status, 401)
        require_equal(len(w.ledger.runs_for_task(task_id)), 1)


async def t_durable_admission_survives_grant_initialization_failure_and_replay():
    async with world() as w:
        task_id, run_id, _ = await parent(w); value = request(w, run_id)
        with patch.object(w.orch.task_starts, 'finish', side_effect=RuntimeError('synthetic interrupted initialization')):
            response = await post(w, value)
        require_equal(response.status, 202); accepted = await response.json()
        require_equal(accepted['annahme'], 'preparing')
        require(w.orch.task_authority.for_run(accepted['run_id']) is None)
        again = await post(w, value); require_equal(again.status, 202)
        require_equal((await again.json())['run_id'], accepted['run_id'])
        await w.orch._advance(w.ledger.get_run(accepted['run_id']))
        require(w.orch.task_starts.ready(accepted['run_id']))
        require_equal(w.ledger.get_run(accepted['run_id']).planner_calls, 0)
        require_equal(len(w.ledger.runs_for_task(task_id)), 2)


def t_app_followup_golden_binding_uses_separate_domain_and_exact_canonical_body():
    value = {'run_id': 'ar-'+'1'*16, 'text': 'Bitte ergänze März.', 'expected_revision': 1,
             'expected_digest': 'a'*64, 'input_artifact_ids': [], 'client_request_id': 'followup-golden-001'}
    digest = E.request_digest(value)
    require_equal(digest, 'e07158911a9ddd8748efba06cba4f6e264209d2407a912ba269f0f20aaeb6d79')
    raw = P.canonical_bytes({'protocol_version': 1, 'type': E.TYPE_TASK_FOLLOWUP,
        'core_instance_id': 'core-fixture', 'principal_id': 'owner-fixture', 'device_id': 'device-fixture',
        'nonce': 'b'*64, 'request_digest': digest, 'enrollment_id': 'enroll-fixture',
        'app_attest_key_id': 'key-fixture', 'approval_key_sha256': 'c'*64})
    require(E.client_data_hash(raw) != T.client_data_hash(raw))
    require_equal(E.client_data_hash(raw).hex(), '33a47361996c86965f099ef1faa61e161c8ef5a0e3daa84684be50d71bb88619')


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
