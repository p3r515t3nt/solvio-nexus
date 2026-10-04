"""Public HTTPS task -> real Router/cost/Unix worker -> durable preparation.

Only browser process/page and unrelated provider partners are synthetic.
The PortalVault contains newly generated synthetic values in a temp directory.
No login executor is connected in this stage.
"""
import asyncio
import base64
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_agent_action_portal import portal_fixture
from test_agent_action_execution import world
import test_agent_task_entry as E
from solvio.agent_runtime import portal_connection as PC, action_contract as AC
from solvio.portal import service as PS, protocol as P
from solvio.portal.vault import PortalVault, USERNAME, PASSWORD
from solvio.portal import vault as PV


@asynccontextmanager
async def connection_world():
    async with portal_fixture(owner='local-owner') as p, world() as w:
        vault = PortalVault(str(w.folder / 'portal-vault'))
        vault._key = b'\x31' * 32  # Synthetic, never touches the native keychain.
        vault.store(p.binding.credential_alias, USERNAME, 'synthetic-user')
        vault.store(p.binding.credential_alias, PASSWORD, 'synthetic-password')
        p.portals.vault = vault
        w.portals = p.portals
        w.orch.action_service.portals = p.portals
        w.orch.action_service.control_plane = w.cp
        p.worker.sessions.clear()
        counters = {'processes': 0, 'navigations': [], 'probes': 0, 'submits': 0}
        async def cdp_call(method, params, **kwargs):
            require_equal(method, 'Runtime.evaluate')
            expression = params['expression']
            if 'function (formSelector' in expression:
                counters['probes'] += 1
                value = {'found': True, 'origin': p.binding.login_origin,
                    'url': p.binding.login_url, 'action': '/login', 'method': 'POST',
                    'fields': ['username', 'password']}
            elif expression == PS._VISIBLE_TEXT.replace('%LIMIT%', str(PS.MAX_TEXT)):
                value = {'url': p.binding.login_url, 'title': 'Synthetic login', 'text': 'Please sign in'}
            else:
                counters['submits'] += 1
                raise AssertionError('No field filling or submit is allowed in this stage')
            if getattr(p, 'on_probe', None) and 'function (formSelector' in expression:
                await p.on_probe()
            return {'result': {'value': value}}
        p.cdp.call = cdp_call
        def process():
            counters['processes'] += 1
            folder = w.folder / ('profile-' + str(counters['processes'])); folder.mkdir()
            async def start():
                if getattr(p, 'on_start', None):
                    await p.on_start()
            return SimpleNamespace(profile_dir=str(folder), start=start,
                new_page_socket=AsyncMock(return_value=p.cdp), stop=AsyncMock())
        def page(cdp, **kwargs):
            async def navigate(url): counters['navigations'].append(url)
            return SimpleNamespace(cdp=cdp, prepare=AsyncMock(), navigate=navigate,
                close=AsyncMock(), state=SimpleNamespace(blocked=[]))
        with patch.object(PS, 'BrowserProcess', process), patch.object(PS, 'BrowserPage', page), \
                patch.object(p.worker, '_policy', return_value=True):
            response = await w.client.get('/v1/agent/action-portals')
            data = await response.json(); require_equal(response.status, 200, str(data))
            item = data['portals'][0]
            w.body = {'scope': 'action', 'objective': 'Verbinde mich mit dem eingerichteten Testportal.',
                'target_repo': '', 'client_request_id': 'portal-connect-001', 'action_request': {'actions': [{
                    'action_id': 'connect', 'service': 'portal', 'operation': 'connect',
                    'account': item['account'], 'target': {'portal_id': item['portal_id']}, 'payload': {}}]}}
            yield p, w, counters
            require_equal(counters['submits'], 0)
            require_equal(w.native.transport.calls, [])
            require_equal(w.calls(), [])


async def admit(w):
    response = await w.start(w.body)
    body = await response.json()
    require_equal(response.status, 201, str(body))
    return body['run_id']


async def prepare(w, run):
    for _ in range(5):
        if w.ledger.get_run(run).terminal:
            break
        await w.orch.tick()
    return PC.read(w.ledger, run)


async def t_public_exact_task_admission_is_durable_and_replay_is_one_task():
    async with connection_world() as (p, w, counters):
        responses = await asyncio.gather(w.start(w.body), w.start(w.body))
        bodies = [await r.json() for r in responses]
        require(all(r.status == 201 for r in responses), str(bodies))
        require_equal(bodies[0]['run_id'], bodies[1]['run_id'])
        row = PC.read(w.ledger, bodies[0]['run_id'])
        require_equal(row['phase'], 'admitted'); require_equal(row['owner'], 'local-owner')
        require_equal(counters['processes'], 0)
        require_equal(await w.store.list_pending(), [])
        require_equal(len(w.ledger.recent_runs()), 1)


async def t_public_task_prepares_real_owned_session_and_nonexecutable_journal_only():
    async with connection_world() as (p, w, counters):
        run = await admit(w)
        row = await prepare(w, run)
        require_equal(row['phase'], 'prepared', str(row))
        require_equal(counters['processes'], 1)
        require_equal(counters['navigations'], [p.binding.login_url])
        require_equal(counters['probes'], 1)
        require_equal(p.worker.sessions[row['session_id']].owner_principal, 'local-owner')
        require_equal(p.portals._pending, {})
        require(not w.ledger.get_run(run).state == 'SUCCEEDED')
        require_equal(AC.completion_evidence(w.ledger, run), ())
        require_equal(await w.store.get_request(row['journal_ref']), None)
        require_equal((await w.co.execute_approved(row['journal_ref'], AsyncMock()))[1], 'not_approved')
        def journal():
            return dict(w.store._conn.execute('SELECT * FROM portal_task_preparations').fetchone())
        recorded = await w.store._run(journal)
        require_equal(recorded['manifest_json'], row['manifest_json'])
        require('synthetic-password' not in str(recorded) and 'synthetic-user' not in str(recorded))
        response = await w.client.get('/v1/agent/runs/' + run)
        view = await response.json()
        require_equal(view['portal_connection']['state'], 'prepared')
        require_equal(view['portal_connection']['connected'], False)
        require_equal(view['portal_connection']['login_wired'], False)
        require_equal(await w.store.list_pending(), [])


async def t_mixed_actions_extra_fields_and_changed_replay_rejected_without_open():
    async with connection_world() as (p, w, counters):
        original = deepcopy(w.body)
        for case in ('mixed', 'owner', 'url', 'session'):
            body = deepcopy(original)
            action = body['action_request']['actions'][0]
            if case == 'mixed':
                other = deepcopy(action); other['action_id'] = 'other'
                body['action_request']['actions'].append(other)
            elif case == 'owner': action['owner'] = 'forged'
            else: action['target'][case] = 'forged'
            response = await w.start(body)
            require_equal(response.status, 400, await response.text())
        require_equal(w.ledger.recent_runs(), [])
        await admit(w)
        mutated = deepcopy(original); mutated['objective'] += ' Anders.'
        response = await w.start(mutated)
        require_equal(response.status, 409)
        require_equal(counters['processes'], 0)


async def t_native_account_stale_or_worker_changed_rejected_before_task_admission():
    for mode in ('vault', 'worker', 'account'):
        async with connection_world() as (p, w, counters):
            if mode == 'vault': p.portals.vault.store(p.binding.credential_alias, PASSWORD, 'new-synthetic')
            elif mode == 'worker': p.worker._build = lambda: 'wrong-build'
            else: w.body['action_request']['actions'][0]['account'] = 'forged'
            response = await w.start(w.body)
            require_equal(response.status, 409, await response.text())
            require_equal(w.ledger.recent_runs(), [])
            require_equal(counters['processes'], 0)


async def t_browser_revoked_during_admission_does_not_create_task():
    async with connection_world() as (p, w, counters):
        original = p.worker._op_ping
        async def revoke(message):
            value = await original(message)
            await w.client.post('/v1/browser/session/logout', json={}, headers=w.headers)
            return value
        p.worker._op_ping = revoke
        response = await w.start(w.body)
        require_equal(response.status, 401)
        require_equal(w.ledger.recent_runs(), [])
        require_equal(counters['processes'], 0)


async def t_replay_finds_accepted_task_even_after_native_account_changes():
    async with connection_world() as (p, w, counters):
        run = await admit(w)
        p.portals.vault.store(p.binding.credential_alias, PASSWORD, 'changed-synthetic')
        response = await w.start(w.body)
        require_equal(response.status, 201, await response.text())
        require_equal((await response.json())['run_id'], run)
        require_equal(counters['processes'], 0)


async def t_task_revocation_during_open_handshake_stops_before_browser():
    async with connection_world() as (p, w, counters):
        run = await admit(w)
        original = p.worker._op_ping
        async def revoke(message):
            value = await original(message)
            grant = w.orch.task_authority.for_run(run)
            w.orch.task_authority.revoke(grant.reference, 'fixture:revoke')
            return value
        p.worker._op_ping = revoke
        await prepare(w, run)
        require_equal(counters['processes'], 0)
        require_equal(PC.read(w.ledger, run)['phase'], 'admitted')


async def t_worker_open_reservation_replays_and_owner_mismatch_cannot_reopen():
    async with connection_world() as (p, w, counters):
        reference = 'pc-' + '1' * 32
        first = await p.client.open_connection(p.binding, owner_principal='local-owner', connection_ref=reference)
        second = await p.client.open_connection(p.binding, owner_principal='local-owner', connection_ref=reference)
        require_equal(first, second); require_equal(counters['processes'], 1)
        response = await p.client.call({'op': P.OPEN_SESSION, 'owner_principal': 'other-owner',
            'connection_ref': reference, 'binding': p.binding.as_data()})
        require_equal(response.get('reason'), 'connection_binding_changed')
        require_equal(counters['processes'], 1)
        await p.client.close_session(first, owner_principal='local-owner')
        status = await p.client.open_status(reference, owner_principal='local-owner')
        require_equal(status['state'], 'closed')
        require_equal(counters['processes'], 1)


async def signed_start(w, client, device, headers):
    response = await client.post('/v1/agent/tasks/challenge', json={'task': w.body}, headers=headers)
    require_equal(response.status, 200)
    challenge = await response.json()
    assertion = E.AA.fake_assertion(device.aakey,
        E.T.client_data_hash(base64.b64decode(challenge['binding_b64'])), 1)
    return {'task': deepcopy(w.body), 'proof': {'nonce': challenge['nonce'],
        'assertion_b64': base64.b64encode(assertion).decode()}}


async def t_app_attest_connection_target_is_bound_and_static_transport_cannot_admit():
    async with connection_world() as (p, w, counters):
        device = await E.H.enroll_attested(w.cp, transport_cred='synthetic-portal-transport')
        client = await w.new_client()
        headers = {'X-Device-Id': device.device_id, 'X-Transport-Cred': 'synthetic-portal-transport'}
        require_equal((await client.post('/v1/agent/tasks', json={'task': w.body}, headers=headers)).status, 401)
        payload = await signed_start(w, client, device, headers)
        payload['task']['action_request']['actions'][0]['target']['portal_id'] = 'other-portal'
        require_equal((await client.post('/v1/agent/tasks', json=payload, headers=headers)).status, 401)
        require_equal(w.ledger.recent_runs(), [])
        payload = await signed_start(w, client, device, headers)
        response = await client.post('/v1/agent/tasks', json=payload, headers=headers)
        data = await response.json()
        require_equal(response.status, 201, str(data))
        require_equal(w.orch.task_authority.for_run(data['run_id']).receipt_method, 'app_session')
        require_equal(PC.read(w.ledger, data['run_id'])['owner'], 'local-owner')
        require_equal(counters['processes'], 0)
        require_equal(await w.store.list_pending(), [])


async def t_app_revocation_or_key_rotation_during_preflight_blocks_admission():
    for mode in ('revoked', 'enrollment', 'key'):
        async with connection_world() as (p, w, counters):
            device = await E.H.enroll_attested(w.cp, transport_cred='synthetic-portal-transport')
            client = await w.new_client()
            headers = {'X-Device-Id': device.device_id, 'X-Transport-Cred': 'synthetic-portal-transport'}
            payload = await signed_start(w, client, device, headers)
            original = p.worker._op_ping
            async def mutate(message):
                result = await original(message)
                if mode == 'revoked':
                    await w.cp.revoke_device(device.device_id, reason='synthetic revoke')
                else:
                    column = 'current_enrollment_id' if mode == 'enrollment' else 'app_attest_public_key'
                    await w.store._run(lambda: w.store._conn.execute(
                        'UPDATE devices SET ' + column + '=? WHERE device_id=?', ('changed', device.device_id)))
                return result
            p.worker._op_ping = mutate
            response = await client.post('/v1/agent/tasks', json=payload, headers=headers)
            require_equal(response.status, 401, await response.text())
            require_equal(w.ledger.recent_runs(), [])
            require_equal(counters['processes'], 0)


async def t_anonymous_and_foreign_owner_never_reach_worker_or_create_task():
    async with connection_world() as (p, w, counters):
        ping = AsyncMock(side_effect=AssertionError('No unauthorised native request'))
        p.worker._op_ping = ping
        client = await w.new_client()
        require_equal((await client.get('/v1/agent/action-portals')).status, 401)
        require_equal((await client.post('/v1/agent/tasks', json={'task': w.body})).status, 401)
        headers = await w.login(client, principal='foreign-owner')
        require_equal((await client.get('/v1/agent/action-portals')).status, 403)
        response = await client.post('/v1/agent/tasks', json={'task': w.body}, headers=headers)
        require(response.status in (403, 409), await response.text())
        require_equal(ping.await_count, 0)
        require_equal(counters['processes'], 0)
        require_equal(w.ledger.recent_runs(), [])


async def t_loss_before_grant_preserves_native_admission_and_same_task():
    from test_agent_action_task_entry import fresh_service
    async with connection_world() as (p, w, counters):
        with patch.object(w.orch.task_authority, 'issue', side_effect=RuntimeError('synthetic crash')):
            response = await w.start(w.body)
            data = await response.json()
            require_equal(response.status, 202, str(data))
        row = PC.read(w.ledger, data['run_id'])
        require_equal(row['phase'], 'admitted')
        require(w.orch.task_authority.for_run(data['run_id']) is None)
        fresh = fresh_service(w)
        require(fresh.finish(data['run_id']))
        require_equal(PC.read(fresh.ledger, data['run_id']), row)
        require_equal(await admit(w), data['run_id'])
        require_equal(counters['processes'], 0)


async def t_grant_insertion_rejects_changed_connection_owner_and_capability():
    from test_agent_action_task_entry import fresh_service
    for mode in ('owner', 'capabilities'):
        async with connection_world() as (p, w, counters):
            with patch.object(w.orch.task_authority, 'issue', side_effect=RuntimeError('synthetic crash')):
                response = await w.start(w.body)
                data = await response.json(); require_equal(response.status, 202)
            with w.ledger._open() as c:
                if mode == 'owner':
                    c.execute('UPDATE agent_portal_connections SET owner=?', ('foreign-owner',))
                else:
                    c.execute('UPDATE agent_task_sources SET capabilities=?', ('[]',))
            try:
                fresh_service(w).finish(data['run_id'])
            except ValueError:
                pass
            require(w.orch.task_authority.for_run(data['run_id']) is None)
            require_equal(counters['processes'], 0)


async def t_lost_open_response_never_reopens_even_after_runtime_restart():
    async with connection_world() as (p, w, counters):
        run = await admit(w)
        original = p.worker._op_open_session
        async def lose(message):
            await original(message)
            return P.failure('synthetic_lost_reply')
        p.worker._op_open_session = lose
        row = await prepare(w, run)
        require_equal(counters['processes'], 1)
        require_equal(row['phase'], 'opening')
        status = await p.client.open_status(row['connection_id'], owner_principal='local-owner')
        require_equal(status['state'], 'opened')
        require_equal(len(p.worker.sessions), 1)
        for worker_alive in (True, False):
            if not worker_alive:
                p.worker._openings.clear(); p.worker.sessions.clear()
            w.runtime(); w.orch.action_service.control_plane = w.cp
            await w.orch.reconcile(); await w.orch.tick()
            require_equal(counters['processes'], 1)
            require_equal(counters['navigations'], [])
            require_equal(PC.read(w.ledger, run)['phase'], 'opening')
            require_equal(AC.completion_evidence(w.ledger, run), ())


async def t_prepared_view_and_journal_survive_fresh_runtime_without_native_calls():
    from solvio.security.mobile_approval.portal_preparation import record
    async with connection_world() as (p, w, counters):
        run = await admit(w)
        row = await prepare(w, run)
        replay_row = dict(row, phase='manifest_ready')
        require_equal(await record(w.cp, replay_row), row['journal_ref'])
        changed = deepcopy(replay_row)
        payload = json.loads(changed['manifest_json']); payload['session_id'] = 'ps-0-0'
        changed['manifest_json'] = PC._raw(payload); changed['manifest_digest'] = PC._digest(payload)
        try:
            await record(w.cp, changed)
        except ValueError:
            pass
        else:
            raise AssertionError('Changed native session entered the preparation journal')
        with w.ledger._open() as c:
            costs = [dict(r) for r in c.execute('SELECT * FROM agent_cost_reservations')]
            invocations = [dict(r) for r in c.execute('SELECT * FROM agent_provider_invocations')]
        require_equal(len(costs), 1); require_equal(len(invocations), 1)
        require_equal(costs[0]['upper_bound_cents'], 0)
        require_equal(json.loads(costs[0]['evidence'])['reference'], 'core:configured-portal-login-only:v1')
        require_equal(invocations[0]['state'], 'not_dispatched')  # Login was deliberately not dispatched.
        w.runtime(); w.orch.action_service.control_plane = w.cp
        p.worker._op_ping = AsyncMock(side_effect=AssertionError('View is ledger-only'))
        response = await w.client.get('/v1/agent/runs/' + run)
        require_equal(response.status, 200)
        require_equal((await response.json())['portal_connection']['connected'], False)
        require_equal(PC.read(w.ledger, run), row)


async def t_catalogue_missing_native_key_never_creates_or_replaces_it():
    async with connection_world() as (p, w, counters):
        original = Path(p.portals.vault.path).read_bytes()
        p.portals.vault._key = None
        with patch.object(PV, '_read_master', return_value=None), \
                patch.object(PV, '_write_master', side_effect=AssertionError('A catalogue cannot create a key')) as write:
            response = await w.client.get('/v1/agent/action-portals')
            data = await response.json()
            require_equal(response.status, 200, str(data))
            require_equal(data['portals'][0]['credential_available'], False)
            require_equal((await w.start(w.body)).status, 409)
            require_equal(write.call_count, 0)
        require_equal(Path(p.portals.vault.path).read_bytes(), original)
        require_equal(w.ledger.recent_runs(), [])
        require_equal(counters['processes'], 0)


async def t_missing_username_is_not_a_complete_native_login():
    async with connection_world() as (p, w, counters):
        p.portals.vault._save({p.binding.credential_alias: {PASSWORD: 'synthetic-only-password'}})
        response = await w.client.get('/v1/agent/action-portals')
        data = await response.json()
        require_equal(data['portals'][0]['credential_available'], False)
        w.body['action_request']['actions'][0]['account'] = data['portals'][0]['account']
        require_equal((await w.start(w.body)).status, 409)
        require_equal(counters['processes'], 0)


async def t_concurrent_worker_open_reserves_before_first_await():
    async with connection_world() as (p, w, counters):
        entered, release = asyncio.Event(), asyncio.Event()
        async def hold():
            entered.set()
            await release.wait()
        p.on_start = hold
        reference = 'pc-' + '2' * 32
        first = asyncio.create_task(p.client.open_connection(p.binding,
            owner_principal='local-owner', connection_ref=reference))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            second = await p.client.call({'op': P.OPEN_SESSION, 'owner_principal': 'local-owner',
                'connection_ref': reference, 'binding': p.binding.as_data()})
            require_equal(second['state'], 'opening')
            require_equal(second['ok'], False)
            require_equal(counters['processes'], 1)
        finally:
            release.set()
        session = await first
        observed = await p.client.open_status(reference, owner_principal='local-owner')
        require_equal(observed['session_id'], session)
        require_equal(observed['state'], 'opened')
        require_equal(counters['processes'], 1)


async def t_persisted_manifest_cannot_substitute_grant_or_owner_or_selector():
    for field in ('grant', 'owner', 'selector'):
        async with connection_world() as (p, w, counters):
            run = await admit(w)
            phase = PC._phase
            def corrupt(ledger, row, expected, next_phase, **fields):
                phase(ledger, row, expected, next_phase, **fields)
                if next_phase == 'manifest_ready':
                    changed = json.loads(fields['manifest_json'])
                    if field == 'grant': changed['grant_reference'] = 'foreign-grant'
                    elif field == 'owner': changed['manifest']['principal'] = 'foreign-owner'
                    else: changed['manifest']['fields'][0]['selector'] = '#foreign-field'
                    with ledger._open() as c:
                        c.execute('UPDATE agent_portal_connections SET manifest_json=?,manifest_digest=? WHERE run_id=?',
                            (PC._raw(changed), PC._digest(changed), run))
            with patch.object(PC, '_phase', side_effect=corrupt):
                await prepare(w, run)
            row = PC.read(w.ledger, run)
            require_equal(row['phase'], 'manifest_ready')
            require_equal(row['journal_ref'], '')
            require_equal(counters['submits'], 0)
            require_equal(AC.completion_evidence(w.ledger, run), ())
            require_equal(await w.store.list_pending(), [])


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
