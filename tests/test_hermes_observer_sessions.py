"""Own observer enrollment through actual Core Unix and HTTPS seams, temp state only."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import os
import secrets
import sqlite3
import socket as unix_socket
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal, require_raises
enforce_assertions()
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from solvio.realtime.control import CoreControl, ControlClient, BROWSER_ENROLL
from solvio.security.mobile_approval import browser_sessions as B, observer_contract as O
from test_nexus_dashboard import world
from test_agent_task_entry import BODY
from test_browser_sessions import _harness


@asynccontextmanager
async def observer_world():
    async with world() as w:
        cert = x509.load_pem_x509_certificate((Path(w.folder) / 'test-cert.pem').read_bytes())
        w.sessions.observer_origin = w.origin
        w.sessions.observer_tls_fingerprint = cert.fingerprint(hashes.SHA256()).hex()
        w.binding = O.Binding(w.cp.core_instance_id, 'local-owner', w.origin,
                              w.sessions.observer_tls_fingerprint)
        w.runtime = SimpleNamespace(browser_sessions=w.sessions, control_plane=w.cp,
            approvals=SimpleNamespace(owner_principal='local-owner'),
            tls_fingerprint=w.binding.tls_fingerprint, bound_hosts=['127.0.0.1'],
            port=w.server.port)
        with tempfile.TemporaryDirectory(prefix='solvio-observer-', dir='/tmp') as folder:
            socket = str(Path(folder).resolve() / 'control.sock')
            w.control = CoreControl(SimpleNamespace(approver_runtime=w.runtime), socket_path=socket)
            await w.control.start()
            try:
                w.local = ControlClient(socket)
                yield w
            finally:
                await w.control.stop()


async def login(w):
    enrollment = await w.local.observer_enrollment(expected=w.binding)
    client = await w.new_client()
    response = await client.post(B.SESSION_PATH + '/login', json={'token': enrollment.token},
                                 headers={'Origin': w.origin})
    require_equal(response.status, 200, await response.text())
    data = await response.json()
    return client, enrollment, response, data


async def t_actual_local_handoff_owns_separate_https_session_and_never_starts_work():
    async with observer_world() as w:
        client, enrollment, response, data = await login(w)
        require(O.valid_secret(enrollment.token, 'enrollment'))
        require(O.valid_secret(response.cookies[O.COOKIE_NAME].value, 'session'))
        require(B.COOKIE_NAME not in response.cookies)
        cookie = response.cookies[O.COOKIE_NAME]
        require(cookie['secure'] and cookie['httponly'])
        require_equal(cookie['samesite'], 'Strict'); require_equal(cookie['path'], '/')
        require_equal(int(cookie['max-age']), O.SESSION_LIFETIME_S)
        for field in ('core_instance_id', 'origin', 'tls_fingerprint'):
            require_equal(data[field], getattr(w.binding, field))
        require_equal(data['purpose'], O.PURPOSE); require_equal(data['principal'], 'local-owner')
        require_equal(await (await client.get(B.SESSION_PATH)).json(), data)
        dump = await w.store._run(lambda: '\n'.join(w.store._conn.iterdump()))
        for secret in (enrollment.token, cookie.value, data['csrf_token']):
            require(secret not in dump)
        require(enrollment.token not in repr(enrollment))
        require_equal(w.ledger.recent_runs(), [])
        # Existing owner remains authenticated in its original, unrelated cookie jar.
        require_equal((await w.client.get(B.SESSION_PATH)).status, 200)


async def t_reader_gets_own_actual_run_events_but_no_other_private_surface_or_effect():
    async with observer_world() as w:
        accepted = await (await w.start()).json(); run = accepted['run_id']
        w.orch.create_task(objective='foreign private task', scope='research',
                           origin='local_owner', principal='someone-else')
        foreign = next(r.run_id for r in w.ledger.recent_runs() if r.run_id != run)
        client, _, _, data = await login(w)
        for path in (B.SESSION_PATH, '/v1/agent/runs', f'/v1/agent/runs/{run}',
                     f'/v1/agent/runs/{run}/events', '/dashboard/hermes/solvio-view.html',
                     '/dashboard/assets/window-share.js'):
            response = await client.get(path)
            require_equal(response.status, 200, path + await response.text())
        require_equal((await client.get(f'/v1/agent/runs/{foreign}')).status, 404)
        require('foreign private task' not in await (await client.get('/v1/agent/runs')).text())
        before = [(r.run_id, r.state) for r in w.ledger.recent_runs()]
        headers = {'Origin': w.origin, B.CSRF_HEADER: data['csrf_token']}
        requests = [('POST', '/v1/agent/tasks', {'task': BODY}),
            ('POST', f'/v1/agent/runs/{run}/cancel', {}),
            ('POST', f'/v1/agent/runs/{run}/resume', {}),
            ('POST', '/v1/browser/approvals/fake/decision', {}),
            ('PUT', '/v1/agent/cost-policy', {'approval_threshold_cents': 1}),
            ('POST', '/v1/dashboard/voice/ticket', {}),
            ('POST', '/v1/dashboard/window-share/ticket', {}),
            ('GET', '/v1/dashboard/state', None), ('GET', '/v1/memory/memories', None),
            ('GET', '/v1/agent/action-services', None),
            ('GET', f'/v1/agent/runs/{run}/artifacts/x/download', None),
            ('HEAD', f'/v1/agent/runs/{run}', None)]
        for method, path, body in requests:
            response = await client.request(method, path, json=body, headers=headers)
            require_equal(response.status, 401, method + ' ' + path)
        require_equal([(r.run_id, r.state) for r in w.ledger.recent_runs()], before)
        require_equal(await w.store._run(lambda: w.store._conn.execute(
            'SELECT count(*) FROM approval_requests').fetchone()[0]), 0)


async def t_observer_cannot_supply_owner_origin_fingerprint_or_wrong_runtime():
    async with observer_world() as w:
        for extra in ({'principal': 'foreign'}, {'origin': 'https://127.0.0.2:443'},
                      {'tls_fingerprint': 'a' * 64}, {'purpose': 'owner'}):
            result = await w.local.call({'op': BROWSER_ENROLL, 'purpose': O.PURPOSE, **extra})
            require_equal(result['ok'], False)
        for field, wrong in [('tls_fingerprint', 'a' * 64), ('bound_hosts', []), ('port', 1)]:
            with patch.object(w.runtime, field, wrong):
                result = await w.local.call({'op': BROWSER_ENROLL, 'purpose': O.PURPOSE})
            require_equal(result['reason'], 'observer_unavailable')
        require_equal(await w.store._run(lambda: w.store._conn.execute(
            'SELECT count(*) FROM browser_enrollment_tokens WHERE purpose=?',
            (O.PURPOSE,)).fetchone()[0]), 0)


async def t_new_client_never_accepts_old_full_owner_or_partial_reply():
    async with observer_world() as w:
        actual = (await w.local.observer_enrollment()).as_response()
        old = await w.local.call({'op': BROWSER_ENROLL})
        bad = [old, dict(actual, token=old['token']), dict(actual, purpose='owner'),
               dict(actual, origin='https://example.com:443'), dict(actual, expires_at=float('inf')),
               dict(actual, core_instance_id=''), dict(actual, owner_principal=''),
               dict(actual, tls_fingerprint='')]
        bad += [{k: v for k, v in actual.items() if k != field} for field in actual]
        for reply in bad:
            async def old_reply(message, **kwargs): return reply
            with patch.object(w.local, 'call', old_reply):
                try: await w.local.observer_enrollment(expected=w.binding)
                except ValueError: pass
                else: raise AssertionError('unbound old response accepted')
        for field, value in [('core_instance_id', 'other-core'), ('owner_principal', 'other-owner'),
                             ('origin', 'https://127.0.0.2:443'), ('tls_fingerprint', 'b' * 64)]:
            require_raises(ValueError, O.validate_enrollment, actual,
                            expected=replace(w.binding, **{field: value}))


async def t_enrollment_replay_rotation_and_other_core_never_create_a_second_session():
    async with observer_world() as w:
        enrollment = await w.local.observer_enrollment()
        clients = [await w.new_client(), await w.new_client()]
        responses = await asyncio.gather(*[c.post(B.SESSION_PATH + '/login',
            json={'token': enrollment.token}, headers={'Origin': w.origin}) for c in clients])
        require_equal(sorted(r.status for r in responses), [200, 401])
        another = await w.local.observer_enrollment()
        with patch.object(w.sessions, 'observer_tls_fingerprint', 'b' * 64):
            require(await w.sessions.redeem_observer(another.token, origin=w.origin) is None)
        with patch.object(w.sessions, 'core_instance_id', 'other-core'):
            require(await w.sessions.redeem_observer(another.token, origin=w.origin) is None)
        require(await w.sessions.redeem_observer(another.token, origin=w.origin) is not None)


async def t_two_allowed_origins_do_not_loosen_observer_audience():
    async with observer_world() as w:
        other = 'https://127.0.0.2:' + str(w.server.port)
        w.app[B._ORIGINS] = frozenset({w.origin, other})
        enrollment = await w.local.observer_enrollment()
        client = await w.new_client()
        response = await client.post(B.SESSION_PATH + '/login', json={'token': enrollment.token},
            headers={'Origin': w.origin, 'Host': '127.0.0.2:' + str(w.server.port)})
        require_equal(response.status, 403)
        response = await client.post(B.SESSION_PATH + '/login', json={'token': enrollment.token},
            headers={'Origin': other})
        require_equal(response.status, 403)
        response = await client.post(B.SESSION_PATH + '/login', json={'token': enrollment.token},
            headers={'Origin': w.origin})
        require_equal(response.status, 200)
        data = await response.json()
        require_equal((await client.get(B.SESSION_PATH, headers={'Origin': other})).status, 401)
        require_equal((await client.post(B.SESSION_PATH + '/logout', json={},
            headers={'Origin': other, B.CSRF_HEADER: data['csrf_token']})).status, 401)
        require_equal((await client.get(B.SESSION_PATH)).status, 200)


async def t_mixed_cookies_cannot_upgrade_and_lower_store_authority_refuses_observer():
    async with observer_world() as w:
        client, _, response, data = await login(w)
        secret = response.cookies[O.COOKIE_NAME].value
        require(await w.sessions.authenticate(secret) is None)
        row, reason = await w.store._run(w.store._browser_session, data['session_id'],
                                         'local-owner', w.cp.core_instance_id)
        require(row is None); require_equal(reason, 'browser_session_not_authorizing')
        current = await w.client.get(B.SESSION_PATH)
        owner_cookie = w.client.session.cookie_jar.filter_cookies(w.server.make_url('/'))[B.COOKIE_NAME].value
        mixed = f'{O.COOKIE_NAME}={secret}; {B.COOKIE_NAME}={owner_cookie}'
        require_equal((await client.get(B.SESSION_PATH, headers={'Cookie': mixed})).status, 401)
        guest = await w.new_client()
        require_equal((await guest.get(B.SESSION_PATH,
            headers={'Cookie': f'{B.COOKIE_NAME}={secret}'})).status, 401)
        require_equal(current.status, 200)


async def t_revocation_during_real_run_read_discards_response_and_logout_is_self_only():
    async with observer_world() as w:
        run = (await (await w.start()).json())['run_id']
        initial_state = w.ledger.get_run(run).state
        client, _, _, data = await login(w)
        original = w.orch.ledger.get_run
        revoke = []
        def during(key):
            if not revoke:
                revoke.append(True)
                with sqlite3.connect(w.store.path) as connection:
                    connection.execute('UPDATE browser_sessions SET revoked_at=? WHERE session_id=?',
                                       (time.time(), data['session_id']))
            return original(key)
        with patch.object(w.orch.ledger, 'get_run', during):
            response = await client.get(f'/v1/agent/runs/{run}')
        require_equal(response.status, 401); require_equal(await response.json(), {'error': 'unauthorized'})
        second, _, _, fresh = await login(w)
        require_equal((await second.post(B.SESSION_PATH + '/logout', json={},
                                         headers={'Origin': w.origin})).status, 401)
        response = await second.post(B.SESSION_PATH + '/logout', json={'session_id': 'some-other-session'},
            headers={'Origin': w.origin, B.CSRF_HEADER: fresh['csrf_token']})
        require_equal(response.status, 200)
        require_equal((await second.get(B.SESSION_PATH)).status, 401)
        require_equal((await w.client.get(B.SESSION_PATH)).status, 200)
        require_equal(w.ledger.get_run(run).state, initial_state)


async def t_expired_observer_and_wrong_csrf_domain_are_rejected():
    async with _harness() as h:
        h.service.observer_origin = h.origin; h.service.observer_tls_fingerprint = 'a' * 64
        token = await h.service.issue_observer_enrollment(principal='local-owner')
        session = await h.service.redeem_observer(token.token, origin=h.origin)
        require(session is not None)
        require(await h.service.authenticate_observer(session.token, origin=h.origin,
                                                     csrf_token=B._csrf_for(session.token)) is None)
        require(await h.service.authenticate_observer(session.token, origin=h.origin,
                                                     csrf_token=session.csrf_token) is not None)
        h.clock.now += O.SESSION_LIFETIME_S + 1
        require(await h.service.authenticate_observer(session.token, origin=h.origin) is None)


async def t_actual_runtime_config_has_no_arbitrary_origin_fallback():
    from solvio.capabilities.approver_runtime import ApproverRuntime
    from aiohttp import web
    async with _harness() as h:
        runtime = ApproverRuntime(host='127.0.0.1', port=8770,
            state_dir=str(Path(h.path).parent), runtime_mode='test')
        runtime.control_plane = SimpleNamespace(store=h.store, core_instance_id=h.core_id)
        runtime.tls_fingerprint = 'a' * 64
        for origin, expected in [('https://127.0.0.1:8770', 'https://127.0.0.1:8770'),
                                 ('https://127.0.0.2:8770', '')]:
            with patch.dict(os.environ, {'SOLVIO_DASHBOARD_ORIGINS': origin}):
                runtime._attach_browser_sessions(web.Application())
            require(runtime.browser_sessions is not None)
            require_equal(runtime.browser_sessions.observer_origin, expected)
        require_equal(h.store.path, runtime.browser_sessions.store.path)


async def t_local_socket_identity_and_runtime_rotation_fail_closed():
    async with observer_world() as w:
        path = w.local.socket_path
        os.chmod(path, 0o666)
        try:
            try: await w.local.observer_enrollment()
            except ValueError: pass
            else: raise AssertionError('world-accessible socket accepted')
        finally: os.chmod(path, 0o600)
        issue = w.sessions.issue_observer_enrollment
        async def rotated(**kwargs):
            value = await issue(**kwargs)
            w.runtime.approvals.owner_principal = 'changed-owner'
            return value
        with patch.object(w.sessions, 'issue_observer_enrollment', rotated):
            try: await w.local.observer_enrollment()
            except ValueError: pass
            else: raise AssertionError('changed runtime owner accepted')
        w.runtime.approvals.owner_principal = 'local-owner'
        call = w.local.call
        replacement = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
        async def swapped(message, **kwargs):
            value = await call(message, **kwargs)
            os.unlink(path); replacement.bind(path); os.chmod(path, 0o600)
            return value
        try:
            with patch.object(w.local, 'call', swapped):
                try: await w.local.observer_enrollment()
                except ValueError: pass
                else: raise AssertionError('replaced control socket accepted')
        finally: replacement.close()


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
