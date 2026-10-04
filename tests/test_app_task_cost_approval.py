"""Native cost decisions through real temporary HTTPS and signed App Attest.

Reuse the existing registered-device, browser, task and cost ledgers. No real
device, biometric prompt, provider call, payment or production state is used.
"""
import asyncio
import base64
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal, require_raises
enforce_assertions()
from test_agent_task_entry import world, BODY
import mobile_attest_helper as H
from solvio.agent_runtime import task_cost_approval_proof as E, task_start_proof as T, store as S
from solvio.agent_runtime.costs import CostEvidence, CostLedger, MAX_CENTS
from solvio.security.mobile_approval import app_attest as AA, protocol as P

TRANSPORT = 'temporary-cost-approval-transport'


def path(task_id):
    return '/v1/agent/tasks/' + task_id + '/cost-approval'


def value(task_id, amount=2000, request_id='native-cost-001'):
    return {'task_id': task_id, 'max_total_cents': amount, 'client_request_id': request_id}


async def waiting(w, request_id='cost-task-001'):
    response = await w.start(dict(BODY, client_request_id=request_id))
    require_equal(response.status, 201)
    task = await response.json()
    reservation = w.orch.costs.reserve(task['task_id'], 'fixture-expensive-call', 1200,
        route='fixture', evidence=CostEvidence('enforceable_upper_bound', 'synthetic-cost-limit'))
    require_equal(reservation.status, 'approval_required')
    w.ledger.transition(task['run_id'], S.PLANNING)
    w.ledger.transition(task['run_id'], S.WAITING_USER)
    return task


async def app_client(w, principal='local-owner', device_id='dev-cost'):
    device = await H.enroll_attested(w.cp, device_id=device_id, principal=principal,
                                   transport_cred=TRANSPORT)
    return await w.new_client(), device, {'X-Device-Id': device.device_id, 'X-Transport-Cred': TRANSPORT}


async def signed(w, app, device, headers, body, hash_function=E.client_data_hash):
    response = await app.post(path(body['task_id']) + '/challenge', json={'cost_approval': body}, headers=headers)
    require_equal(response.status, 200, await response.text())
    require_equal(response.headers['Cache-Control'], 'no-store')
    challenge = await response.json()
    raw = base64.b64decode(challenge['binding_b64'])
    binding = json.loads(raw)
    require_equal(binding['type'], E.TYPE_TASK_COST_APPROVAL)
    require_equal(binding['principal_id'], 'local-owner')
    require_equal(binding['device_id'], device.device_id)
    require_equal(challenge['request_digest'], E.request_digest(body))
    assertion = AA.fake_assertion(device.aakey, hash_function(raw), 1)
    return {'cost_approval': body, 'proof': {'nonce': challenge['nonce'],
            'assertion_b64': base64.b64encode(assertion).decode()}}


def authorizations(w):
    with w.ledger._open() as db:
        return [tuple(row) for row in db.execute('SELECT subject_id,approval_ref,category,max_total_cents,created_at '
                                                 'FROM agent_cost_authorizations ORDER BY approval_ref')]


async def t_native_cap_is_same_browser_cost_truth_without_biometric_or_automatic_resume():
    async with world() as w:
        task = await waiting(w); task_id, run_id = task['task_id'], task['run_id']
        app, device, headers = await app_client(w)
        original = w.ledger.get_run(run_id)
        body = value(task_id)
        payload = await signed(w, app, device, headers, body)
        require_equal(authorizations(w), [], 'challenge itself approved costs')
        response = await app.post(path(task_id), json=payload, headers={'X-Device-Id': device.device_id})
        require_equal(response.status, 200, await response.text())
        require_equal(response.headers['Cache-Control'], 'no-store')
        result = await response.json()
        require_equal(set(result), {'task_id', 'client_request_id', 'max_total_cents', 'costs'})
        require_equal({key: result[key] for key in body}, body)
        require_equal(result['costs'], w.orch.costs.view(task_id))
        require_equal(result['costs']['approved_ai_cap_cents'], 2000)
        browser = await (await w.client.get('/v1/agent/runs/' + run_id)).json()
        native = await (await app.get('/v1/agent/runs/' + run_id, headers=headers)).json()
        require_equal(browser['kosten'], result['costs'])
        require_equal(native['kosten'], browser['kosten'])
        require_equal(w.ledger.get_run(run_id), original)
        require_equal(len(w.ledger.recent_runs()), 1)
        require_equal(await w.store.list_pending(), [])
        require_equal((await w.store.get_device(device.device_id))['app_attest_counter'], 0)
        require_equal(len(authorizations(w)), 1)
        require_equal(authorizations(w)[0][1], E.approval_reference('local-owner', body['client_request_id']))


async def t_transport_only_wrong_domain_and_proof_replay_cannot_approve():
    async with world() as w:
        task = await waiting(w); task_id = task['task_id']; body = value(task_id)
        app, device, headers = await app_client(w)
        require_equal((await app.post(path(task_id), json={'cost_approval': body}, headers=headers)).status, 401)
        require_equal((await app.post(path(task_id), json={'max_total_cents': 2000,
            'client_request_id': body['client_request_id']}, headers=headers)).status, 401)
        wrong = await signed(w, app, device, headers, body, T.client_data_hash)
        require_equal((await app.post(path(task_id), json=wrong, headers=headers)).status, 401)
        require_equal(authorizations(w), [])
        payload = await signed(w, app, device, headers, body)
        require_equal((await app.post(path(task_id), json=payload, headers=headers)).status, 200)
        require_equal((await app.post(path(task_id), json=payload, headers=headers)).status, 401)
        require_equal(len(authorizations(w)), 1)


async def t_exact_body_and_url_task_are_bound_without_consuming_honest_proof():
    async with world() as w:
        first = await waiting(w); second = await waiting(w, 'cost-task-002')
        app, device, headers = await app_client(w)
        body = value(first['task_id']); payload = await signed(w, app, device, headers, body)
        for changed in (dict(body, task_id=second['task_id']), dict(body, max_total_cents=3000),
                        dict(body, client_request_id='native-cost-002'), dict(body, category='purchase')):
            reply = await app.post(path(first['task_id']), json={**payload, 'cost_approval': changed}, headers=headers)
            require_equal(reply.status, 401)
        require_equal((await app.post(path(second['task_id']), json=payload, headers=headers)).status, 401)
        require_equal(authorizations(w), [])
        require_equal((await app.post(path(first['task_id']), json=payload, headers=headers)).status, 200)
        require_equal(w.orch.costs.view(second['task_id'])['approved_ai_cap_cents'], None)


async def t_fresh_nonce_replay_is_one_reference_and_changed_task_or_amount_conflicts():
    async with world() as w:
        first = await waiting(w); app, device, headers = await app_client(w)
        body = value(first['task_id'])
        async def submit(request):
            return await app.post(path(request['task_id']),
                json=await signed(w, app, device, headers, request), headers=headers)
        first_reply = await submit(body); require_equal(first_reply.status, 200)
        accepted = await first_reply.json(); original_rows = authorizations(w)
        w.orch.costs = CostLedger(w.ledger)  # Reopen the existing durable cost data before replay.
        again = await submit(body); require_equal(again.status, 200)
        require_equal(await again.json(), accepted)
        require_equal(authorizations(w), original_rows)
        require_equal((await submit(dict(body, max_total_cents=4000))).status, 409)
        second = await waiting(w, 'cost-task-002')
        require_equal((await submit(dict(body, task_id=second['task_id']))).status, 409)
        require_equal(authorizations(w), original_rows)
        require_equal(w.orch.costs.view(second['task_id'])['approved_ai_cap_cents'], None)
        require(all(run.state == S.WAITING_USER and run.planner_calls == 0 for run in w.ledger.recent_runs()))


async def t_replayed_request_reports_its_exact_amount_even_after_browser_raises_cap():
    async with world() as w:
        task = await waiting(w); app, device, headers = await app_client(w)
        body = value(task['task_id'])
        payload = await signed(w, app, device, headers, body)
        require_equal((await app.post(path(task['task_id']), json=payload, headers=headers)).status, 200)
        browser = await w.client.post(path(task['task_id']), headers=w.headers,
            json={'max_total_cents': 3000, 'client_request_id': 'browser-cost-001'})
        require_equal(browser.status, 200)
        browser_costs = await browser.json()
        require_equal(browser_costs, w.orch.costs.view(task['task_id']))
        require('costs' not in browser_costs, 'browser response shape changed')
        original_rows = authorizations(w)
        retry = await signed(w, app, device, headers, body)
        response = await app.post(path(task['task_id']), json=retry, headers=headers)
        require_equal(response.status, 200)
        result = await response.json()
        require_equal({key: result[key] for key in body}, body)
        require_equal(result['costs']['approved_ai_cap_cents'], 3000)
        require_equal(result['max_total_cents'], 2000)
        require_equal(authorizations(w), original_rows)


async def t_distinct_nonces_concurrently_commit_only_one_cost_authorization():
    async with world() as w:
        task = await waiting(w); app, device, headers = await app_client(w)
        body = value(task['task_id'])
        left = await signed(w, app, device, headers, body)
        right = await signed(w, app, device, headers, body)
        responses = await asyncio.gather(*(app.post(path(task['task_id']), json=p, headers=headers)
                                          for p in (left, right)))
        require_equal([r.status for r in responses], [200, 200])
        require_equal(await responses[0].json(), await responses[1].json())
        require_equal(len(authorizations(w)), 1)
        require_equal(w.ledger.get_run(task['run_id']).state, S.WAITING_USER)


async def t_foreign_owner_and_browser_app_mixtures_are_refused():
    async with world() as w:
        task = await waiting(w); task_id = task['task_id']; body = value(task_id)
        app, device, headers = await app_client(w)
        other, _, other_headers = await app_client(w, 'foreign-owner', 'dev-foreign')
        require_equal((await other.post(path(task_id) + '/challenge',
            json={'cost_approval': body}, headers=other_headers)).status, 404)
        payload = await signed(w, app, device, headers, body)
        require_equal((await w.client.post(path(task_id) + '/challenge',
            json={'cost_approval': body}, headers=headers)).status, 401)
        require_equal((await w.client.post(path(task_id), json=payload, headers=headers)).status, 401)
        require_equal((await w.client.post(path(task_id), json=payload, headers={**headers, **w.headers})).status, 409)
        require_equal(authorizations(w), [])
        require_equal((await app.post(path(task_id), json=payload, headers=headers)).status, 200)


async def t_verified_proof_is_rechecked_for_revocation_enrollment_principal_and_runtime():
    for change in ('revoke', 'enrollment', 'principal', 'core', 'task_owner', 'runtime'):
        async with world() as w:
            task = await waiting(w); app, device, headers = await app_client(w)
            payload = await signed(w, app, device, headers, value(task['task_id']))
            original = E.TaskCostApprovalProofService.verify
            async def changed(service, **kwargs):
                actor = await original(service, **kwargs)
                require(actor is not None, 'fixture did not verify a real signed assertion')
                if change == 'revoke':
                    await w.cp.revoke_device(device.device_id)
                elif change == 'enrollment':
                    await w.store._run(lambda: w.store._conn.execute(
                        'UPDATE devices SET current_enrollment_id=? WHERE device_id=?', ('replacement-enrollment', device.device_id)))
                elif change == 'principal':
                    await w.store._run(lambda: w.store._conn.execute(
                        'UPDATE devices SET principal=? WHERE device_id=?', ('foreign-owner', device.device_id)))
                elif change == 'core':
                    w.cp.core_instance_id = 'another-core'
                elif change == 'task_owner':
                    with w.ledger._open() as db:
                        db.execute('UPDATE agent_tasks SET created_principal=? WHERE task_id=?',
                                   ('foreign-owner', task['task_id']))
                else:
                    w.app['agent_runtime'] = None
                return actor
            with patch.object(E.TaskCostApprovalProofService, 'verify', changed):
                response = await app.post(path(task['task_id']), json=payload, headers=headers)
            require_equal(response.status, {'task_owner': 404, 'runtime': 409}.get(change, 401), change)
            require_equal(authorizations(w), [])
            require_equal(w.orch.costs.view(task['task_id'])['approved_ai_cap_cents'], None)
            require_equal(w.ledger.get_run(task['run_id']).state, S.WAITING_USER)


async def t_invalid_amounts_and_declining_without_submit_leave_waiting_task_unchanged():
    async with world() as w:
        task = await waiting(w); app, device, headers = await app_client(w)
        before = w.ledger.get_run(task['run_id'])
        original_costs = w.orch.costs.view(task['task_id'])
        for amount in (-1, True, 2000.0, '2000', None, MAX_CENTS + 1):
            response = await app.post(path(task['task_id']) + '/challenge',
                json={'cost_approval': value(task['task_id'], amount)}, headers=headers)
            require_equal(response.status, 400)
        # Opening the decision and declining it sends no approval request.
        await signed(w, app, device, headers, value(task['task_id']))
        require_equal(w.ledger.get_run(task['run_id']), before)
        require_equal(w.orch.costs.view(task['task_id']), original_costs)
        require_equal(authorizations(w), [])
        require_equal(await w.store.list_pending(), [])


def t_cost_proof_has_separate_golden_domain_and_stable_request_identity():
    body = value('at-' + '1' * 16, request_id='cost-golden-001')
    digest = E.request_digest(body)
    require_equal(digest, '5a4481cc18e9c17775c5eae5c5f1f76938049782065612c93a7c0183b0652de0')
    raw = P.canonical_bytes({'protocol_version': 1, 'type': E.TYPE_TASK_COST_APPROVAL,
        'core_instance_id': 'core-fixture', 'principal_id': 'owner-fixture', 'device_id': 'device-fixture',
        'nonce': 'b' * 64, 'request_digest': digest, 'enrollment_id': 'enroll-fixture',
        'app_attest_key_id': 'key-fixture', 'approval_key_sha256': 'c' * 64})
    require_equal(E.client_data_hash(raw).hex(), '528afa01cf05240f32e8bced59b075096f7976f67e3f1dfbd40cfb86e27bbf20')
    require(E.client_data_hash(raw) != T.client_data_hash(raw))
    require_equal(E.canonical_cost_approval(value(body['task_id'], MAX_CENTS))['max_total_cents'], MAX_CENTS)
    require(E.approval_reference('owner-a', body['client_request_id']) !=
            E.approval_reference('owner-b', body['client_request_id']))
    for invalid in (dict(body, task_id='../other'), dict(body, client_request_id='short'), dict(body, origin='app')):
        require_raises(ValueError, lambda: E.canonical_cost_approval(invalid))


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
