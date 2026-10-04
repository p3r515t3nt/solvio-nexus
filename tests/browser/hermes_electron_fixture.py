"""Real temporary Core Unix/HTTPS auth; previously stored synthetic run evidence.

No specialist transport is invoked. The native application observes the same
public projection as the browser. Test control is a local fixture file, never an
application HTTP route, renderer API or production socket.
"""
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from unittest.mock import patch
from aiohttp import web

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from _guard import enforce_assertions
enforce_assertions()
from test_hermes_observer_sessions import observer_world
from test_agent_task_entry import BODY
from solvio.agent_runtime import store as S
from solvio.security.mobile_approval import browser_sessions as B, observer_contract as O
from solvio.dashboard import window_share as W


async def main(output):
    requests = []
    delays = set()
    delay_waits = {}
    release = asyncio.Event()
    original_login, original_current, original_logout = B._login, B._current, B._logout
    async def response_boundary(kind, original, request):
        response = await original(request)
        if kind not in delays or response.status != 200:
            return response
        stream = web.StreamResponse(status=response.status, headers=response.headers)
        for morsel in response.cookies.values():
            stream.headers.add('Set-Cookie', morsel.OutputString())
        await stream.prepare(request)  # Real accepted cookies arrive before the held JSON body.
        await stream.write(response.body[:1])  # Flush accepted headers; the JSON remains incomplete.
        delay_waits[kind] = delay_waits.get(kind, 0) + 1
        await release.wait()
        try: await stream.write(response.body[1:]); await stream.write_eof()
        except (ConnectionResetError, RuntimeError): pass
        return stream
    attach = B.attach
    def observed_attach(app, *args, **kwargs):
        async def response(request, _response):
            requests.append({'method': request.method, 'path': request.path, 'status': _response.status,
                             'observer_cookie_present': O.COOKIE_NAME in request.cookies,
                             'set_cookie_names': [v.split('=', 1)[0] for v in _response.headers.getall('Set-Cookie', [])]})
        app.on_response_prepare.append(response)
        return attach(app, *args, **kwargs)
    with ExitStack() as overrides:
        overrides.enter_context(patch.object(B, 'attach', observed_attach))
        for kind, original, name in [('login', original_login, '_login'), ('probe', original_current, '_current'), ('logout', original_logout, '_logout')]:
            async def held(request, kind=kind, original=original):
                return await response_boundary(kind, original, request)
            overrides.enter_context(patch.object(B, name, held))
        async with observer_world() as w:
            control_operations = []
            original_handle = w.control.handle
            async def observed_handle(message):
                control_operations.append({'op': message.get('op'), 'purpose': message.get('purpose'),
                                           'fields': sorted(message)})
                result = await original_handle(message)
                if 'handoff' in delays:
                    delay_waits['handoff'] = delay_waits.get('handoff', 0) + 1
                    await release.wait()
                return result
            w.control.handle = observed_handle
            other = await (await w.start(dict(BODY, objective='Zweiter lokaler Auftrag ohne Hermes-Beleg.',
                client_request_id='electron-empty-fixture'))).json()
            second = await (await w.start(dict(BODY, objective='Eigener Hermes-Arbeitsstand für den Museumsbesuch.',
                client_request_id='electron-second-native-fixture'))).json()
            second_id = second['run_id']
            for state in (S.PLANNING, S.RUNNING, S.WAITING_SPECIALIST): w.ledger.transition(second_id, state)
            second_step = w.ledger.create_step(run_id=second_id, seq=1, kind='specialist', specialist_profile='researcher/hermes')
            w.ledger.record_event(second_id, 'native_progress', 'Der eigene Museumsaufruf wurde beobachtet.', step_id=second_step.step_id,
                ref=json.dumps({'runtime': 'hermes-codex-app-server', 'invocation_id': 'museum-invocation',
                    'operation_id': 'museum-operation', 'native_thread_id': 'museum-thread', 'native_turn_id': 'museum-turn',
                    'item_id': '', 'seq': 1, 'event': 'started', 'status': ''}))
            w.ledger.update_step(second_step.step_id, state='succeeded', finished=True)
            w.ledger.transition(second_id, S.WAITING_USER, summary='Eigener Museumszwischenstand.',
                result_summary='## Museumszwischenstand\n\nDieser zweite Auftrag hat **seine eigenen Nachrichten**.\n\n'
                    '[Museumsquelle](https://example.invalid/museum-fixture)')
            accepted = await (await w.start(dict(BODY, objective='Lokaler Hermes-Arbeitsstand für einen Hamburg-Ausflug.',
                client_request_id='electron-native-fixture'))).json()
            rid = accepted['run_id']
            for state in (S.PLANNING, S.RUNNING, S.WAITING_SPECIALIST): w.ledger.transition(rid, state)
            for index in (1, 2):
                step = w.ledger.create_step(run_id=rid, seq=index, kind='specialist', specialist_profile='researcher/hermes')
                for seq, event, status in ((1, 'started', ''), (2, 'web_search', 'started'), (3, 'web_search', 'completed')):
                    w.ledger.record_event(rid, 'native_progress', 'Lokal gestelltes, dauerhaftes Rechercheereignis.', step_id=step.step_id,
                        ref=json.dumps({'runtime': 'hermes-codex-app-server', 'invocation_id': f'fixture-invocation-{index}',
                            'operation_id': f'fixture-operation-{index}', 'native_thread_id': f'fixture-thread-{index}',
                            'native_turn_id': f'fixture-turn-{index}', 'item_id': '' if event == 'started' else f'fixture-search-{index}',
                            'seq': seq, 'event': event, 'status': status}))
                w.ledger.update_step(step.step_id, state='succeeded' if index == 1 else 'unknown', finished=True)
            w.ledger.transition(rid, S.WAITING_USER, summary='Zweiter Ausgang ungewiss. Kein automatischer Neuversuch.',
                result_summary='## Belegter Zwischenstand\n\nDie erste Recherche ist dokumentiert. '
                    'Der zweite Ausgang ist **ungewiss**; der Auftrag ist noch offen.\n\n'
                    '[Quelle der lokalen Testdaten](https://example.invalid/observer-fixture)')
            output.mkdir(parents=True, exist_ok=True)
            config = {'test_only': True, 'control_socket': w.local.socket_path,
                'origin': w.origin, 'native_run': rid, 'other_run': other['run_id'], 'second_native_run': second_id}
            (output / 'fixture.json').write_text(json.dumps(config))
            baseline = len(requests)
            stop = asyncio.Event()
            for signum in (signal.SIGTERM, signal.SIGINT):
                asyncio.get_running_loop().add_signal_handler(signum, stop.set)
            command_seq = 0
            print('TEMP_CORE_READY', flush=True)
            while not stop.is_set():
                command = output / 'command.json'
                if command.exists():
                    value = json.loads(command.read_text())
                    if value['seq'] > command_seq:
                        if value['action'] == 'revoke':
                            rows = await w.store._run(lambda: list(w.store._conn.execute(
                                'SELECT session_id FROM browser_sessions WHERE purpose=? AND revoked_at IS NULL', (O.PURPOSE,))))
                            for row in rows: await w.sessions.revoke(row['session_id'], principal='local-owner')
                        elif value['action'] in {'delay_handoff', 'delay_login', 'delay_probe_and_logout'}:
                            delays.clear(); release.clear(); delay_waits.clear()
                            delays.update({'delay_handoff': {'handoff'}, 'delay_login': {'login'},
                                           'delay_probe_and_logout': {'probe', 'logout'}}[value['action']])
                        elif value['action'] == 'release_delays':
                            delays.clear(); release.set()
                        elif value['action'] == 'offline': await w.server.close()
                        elif value['action'] == 'stop': stop.set()
                        else: raise AssertionError('unknown fixture command')
                        command_seq = value['seq']
                sessions = await w.store._run(lambda: [dict(row) for row in w.store._conn.execute(
                    'SELECT purpose,revoked_at FROM browser_sessions')])
                enrollment_count = await w.store._run(lambda: w.store._conn.execute(
                    'SELECT COUNT(*) FROM browser_enrollment_tokens WHERE purpose=?', (O.PURPOSE,)).fetchone()[0])
                data = {'runs': len(w.ledger.recent_runs()), 'real_provider_turns': 0, 'native_rpc_turns': 0,
                    'control_operations': control_operations,
                    'native_events': sum(event.kind == 'native_progress' for event in w.ledger.events_for_run(rid)),
                    'native_run_state': w.ledger.get_run(rid).state,
                    'second_native_events': sum(event.kind == 'native_progress' for event in w.ledger.events_for_run(second_id)),
                    'delay_waits': delay_waits,
                    'window_peers': len(w.app[W.HUB].peers), 'command_seq': command_seq,
                    'observer_enrollments': enrollment_count,
                    'active_observers': sum(row['purpose'] == O.PURPOSE and row['revoked_at'] is None for row in sessions),
                    'revoked_observers': sum(row['purpose'] == O.PURPOSE and row['revoked_at'] is not None for row in sessions),
                    'requests_after_preparation': requests[baseline:]}
                temporary = output / 'audit-next.json'; temporary.write_text(json.dumps(data)); temporary.replace(output / 'audit.json')
                try: await asyncio.wait_for(stop.wait(), .1)
                except asyncio.TimeoutError: pass


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='solvio-electron-state-') as folder:
        with patch.dict(os.environ, {'SOLVIO_STATE_DIR': folder}): asyncio.run(main(Path(sys.argv[1]).resolve()))
