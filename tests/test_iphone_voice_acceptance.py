"""Native HTTPS/AppAttest acceptance uses the inherited book and local sockets only."""
import asyncio
import base64
from contextlib import asynccontextmanager
from functools import partial
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from aiohttp import TCPConnector, web
from aiohttp.test_utils import TestClient, TestServer
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
import mobile_attest_helper as mobile
import test_gpt_live_session as W
from test_dashboard_task_approval import _wire
from test_voice_acceptance_ledger import _book
from solvio import voice_endpoint as E, voice_session_proof as P
from solvio.security.mobile_approval import app_attest
from solvio.realtime import acceptance_ledger as L, acceptance_voice as V


@asynccontextmanager
async def native_world(*, budget=3, enabled=True, missing=False, exhausted=False,
                       final=True, hanging=False):
    with tempfile.TemporaryDirectory(prefix='solvio-native-acceptance-') as folder:
        book = Path(folder) / 'book.json'
        if not missing:
            _book(book, budget=budget, count=2)
            if exhausted:
                old = L.beginnen(book, uhr=lambda:1000)
                old.beenden('closed')
                # Consume time, preserving the actual existing-book contract.
                data = json.loads(book.read_text())
                data['laeufe'][0]['zuletzt'] = 1000 + budget
                book.write_text(json.dumps(data))
        peers, ended = [], []
        async def provider(ws):
            peers.append(ws)
            try:
                async for raw in ws:
                    event = json.loads(raw)
                    if event['type'] == 'session.start':
                        session = dict(event['session'], id=f'local-{len(peers)}', status='active')
                        await ws.send(json.dumps({'type':'session.started', 'session':session, 'event_id':'ready'}))
                    elif event['type'] == 'session.close' and final:
                        await ws.send(json.dumps({'type':'session.closed', 'session':session,
                            'event_id':'closed', 'reason':'close_requested', 'usage':{'seconds':0.01}}))
            except Exception:
                pass  # Synthetic peer can be cut by the real acceptance deadline.
            finally:
                ended.append(ws)
        handshake, cut = asyncio.Event(), asyncio.Event()
        async def stalled(reader, writer):
            try:
                await reader.read(4096)
                handshake.set()
                if await reader.read(4096) == b'':
                    cut.set()
            finally:
                writer.close()
                await writer.wait_closed()
        if hanging:
            upstream = await asyncio.start_server(stalled, '127.0.0.1', 0)
        else:
            upstream = await serve(provider, '127.0.0.1', 0)
        try:
            url = f'ws://127.0.0.1:{upstream.sockets[0].getsockname()[1]}'
            async with W.world() as w:
                approvals, cp, _, _ = await _wire(folder)
                device = await mobile.enroll_attested(cp, transport_cred='native-acceptance-test')
                app = web.Application()
                app['control_plane'] = cp
                E.attach(app, w.server)
                V.attach(w.server, environment=({'SOLVIO_ABNAHME':'1', 'SOLVIO_ABNAHME_BUCH':str(book)} if enabled else {}))
                server_tls, client_tls = W.H._tls(folder)
                server = TestServer(app, scheme='https')
                await server.start_server(ssl=server_tls)
                client = TestClient(server, connector=TCPConnector(ssl=client_tls))
                await client.start_server()
                counter = 0
                async def authenticated():
                    nonlocal counter
                    counter += 1
                    chat, _ = w.server.conversations.create_conversation(owner_principal='local-owner',
                        kind='text', client_request_id=f'native-acceptance-{counter}')
                    cid = chat['conversation_id']
                    ws = await client.ws_connect(E.PATH, headers={'X-Device-Id':device.device_id,
                        'X-Transport-Cred':'native-acceptance-test'})
                    challenge = await W.H.receive(ws, 'session_challenge')
                    raw = P.canonical_bytes(P.build_binding(core_instance_id=cp.core_instance_id,
                        device_id=device.device_id, session_nonce=challenge['session_nonce'], conversation_id=cid))
                    assertion = app_attest.fake_assertion(device.aakey, P.client_data_hash(raw), counter)
                    await ws.send_json({'type':'session_assertion', 'session_nonce':challenge['session_nonce'],
                        'conversation_id':cid, 'assertion':base64.b64encode(assertion).decode()})
                    return ws
                guard_type = V.AcceptanceGuard
                with patch.object(W.CS, 'ws_connect', connect), \
                        patch.object(W.L.LiveSession, '_provider_url', return_value=url), \
                        patch.object(W.CS, 'RECONNECT_BACKOFF', 0.01), \
                        patch('websockets.proxy.get_proxy', return_value=None), \
                        patch.object(V, 'AcceptanceGuard', partial(guard_type, reserve=0.04, tick=0.01)):
                    try:
                        yield SimpleNamespace(w=w, book=book, authenticated=authenticated,
                            peers=peers, ended=ended, handshake=handshake, cut=cut)
                    finally:
                        await client.close()
                        await server.close()
                        await approvals.close()
        finally:
            upstream.close()
            await upstream.wait_closed()


async def start(x):
    ws = await x.authenticated()
    await ws.send_json({'type':'session_start'})
    await W.H.receive(ws, 'session_ready')
    return ws, x.w.sessions[-1]


async def wait_closed(x, ws):
    while not ws.closed:
        message = await asyncio.wait_for(ws.receive(), 2)
        if message.type in {web.WSMsgType.CLOSE, web.WSMsgType.CLOSED, web.WSMsgType.ERROR}:
            break
    await W.until(lambda:not x.w.server._busy)


def finished_guard(session):
    guard = session.acceptance_guard
    require(guard.finished)
    require(guard._monitor.done())
    require_equal(guard._sockets, [])
    require_equal(guard._providers, {})
    require_equal(session._draining, [])


async def t_default_off_keeps_two_native_sessions_and_creates_no_book():
    async with native_world(enabled=False, missing=True) as x:
        ws, session = await start(x)
        for index in range(2):
            await ws.send_json({'type':'session_end'})
            await W.H.receive(ws, 'conversation_flushed')
            if index == 0:
                await ws.send_json({'type':'session_start'})
                await W.H.receive(ws, 'session_ready')
        require_equal(len(x.peers), 2)
        require(not hasattr(session, 'acceptance_guard'))
        require(not x.book.exists())
        await ws.close()
        await W.until(lambda:not x.w.server._busy)


async def t_missing_book_refuses_native_provider_without_creating_allowance():
    async with native_world(missing=True) as x:
        ws = await x.authenticated()
        await ws.send_json({'type':'session_start'})
        await wait_closed(x, ws)
        require_equal(len(x.peers), 0)
        require(not x.book.exists())


async def t_exhausted_book_refuses_native_provider_without_replacing_history():
    async with native_world(exhausted=True) as x:
        before = x.book.read_bytes()
        ws = await x.authenticated()
        await ws.send_json({'type':'session_start'})
        await wait_closed(x, ws)
        require_equal(len(x.peers), 0)
        require_equal(x.book.read_bytes(), before)


async def t_normal_native_close_finishes_only_after_drain_and_releases_owned_work():
    async with native_world() as x:
        ws = await x.authenticated()
        require_equal(L.gespraeche(x.book), 0, 'mere authentication spent a conversation')
        await ws.send_json({'type':'session_start'})
        await W.H.receive(ws, 'session_ready')
        session = x.w.sessions[-1]
        entered, release = asyncio.Event(), asyncio.Event()
        original = session._drain
        async def drain():
            await original()
            entered.set()
            await release.wait()
        session._drain = drain
        try:
            await ws.send_json({'type':'session_end'})
            await asyncio.wait_for(entered.wait(), 1)
            require(x.w.server._busy)
            require(not L.lesen(x.book)['laeufe'][0]['beendet'])
        finally:
            release.set()
        await wait_closed(x, ws)
        require(L.lesen(x.book)['laeufe'][0]['beendet'])
        require_equal(L.gespraeche(x.book), 1)
        require(0 < L.verbraucht(L.lesen(x.book)) < 3)
        finished_guard(session)


async def t_unconfirmed_native_close_keeps_book_open_and_refuses_next_start():
    async with native_world(final=False) as x:
        ws, session = await start(x)
        await ws.send_json({'type':'session_end'})
        await wait_closed(x, ws)
        require(not L.lesen(x.book)['laeufe'][0]['beendet'])
        finished_guard(session)
        before = x.book.read_bytes()
        ws2 = await x.authenticated()
        await ws2.send_json({'type':'session_start'})
        await wait_closed(x, ws2)
        require_equal(len(x.peers), 1)
        require_equal(x.book.read_bytes(), before)


async def t_native_deadline_includes_a_hanging_provider_handshake():
    async with native_world(budget=0.3, hanging=True) as x:
        ws = await x.authenticated()
        await ws.send_json({'type':'session_start'})
        await asyncio.wait_for(x.handshake.wait(), 1)
        await asyncio.wait_for(x.cut.wait(), 1)
        await wait_closed(x, ws)
        session = x.w.sessions[-1]
        require_equal(L.gespraeche(x.book), 1)
        require(0 < L.verbraucht(L.lesen(x.book)) < 1)
        require(not L.lesen(x.book)['laeufe'][0]['beendet'])
        require(not session.active)
        finished_guard(session)


async def t_native_recovery_keeps_one_admission_and_original_deadline():
    async with native_world(budget=0.7) as x:
        ws, session = await start(x)
        guard = session.acceptance_guard
        deadline = guard._deadline
        await x.peers[0].close(code=1011, reason='synthetic disconnect')
        await W.until(lambda:len(x.peers) == 2 and session.active)
        require(session.acceptance_guard is guard)
        require_equal(guard._deadline, deadline)
        require_equal(L.gespraeche(x.book), 1)
        await wait_closed(x, ws)
        await W.until(lambda:len(x.ended) == 2)
        require_equal(L.gespraeche(x.book), 1)
        finished_guard(session)


async def t_repeated_native_start_cannot_rebook_or_reset_deadline():
    async with native_world() as x:
        ws, session = await start(x)
        await ws.send_json({'type':'session_start'})
        await wait_closed(x, ws)
        require_equal(L.gespraeche(x.book), 1)
        require_equal(len(x.peers), 1)
        finished_guard(session)


async def t_native_end_after_admission_before_open_finishes_not_opened_book():
    async with native_world() as x:
        original = x.w.server.native_voice_admission
        async def admitted_then_ended(session):
            result = await original(session)
            session.ws.ending.set()
            return result
        x.w.server.native_voice_admission = admitted_then_ended
        ws = await x.authenticated()
        await ws.send_json({'type':'session_start'})
        await wait_closed(x, ws)
        require_equal(len(x.peers), 0)
        require_equal(L.gespraeche(x.book), 1)
        require(L.lesen(x.book)['laeufe'][0]['beendet'])
        finished_guard(x.w.sessions[-1])


async def delayed_close_owner(timer):
    async with native_world(budget=0.35) as x:
        ws, session = await start(x)
        entered, release = asyncio.Event(), asyncio.Event()
        original = session._drain_persistence
        owners = []
        async def parked_first_drain(*args, **kwargs):
            if not owners:
                owners.append(asyncio.current_task())
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)
        session._drain_persistence = parked_first_drain
        if timer:
            session.timer.cancel()
            await asyncio.gather(session.timer, return_exceptions=True)
            session.timer = asyncio.create_task(session.close(reason='timeout'))
        else:
            await ws.send_json({'type':'session_end'})
        try:
            await asyncio.wait_for(entered.wait(), 1)
            session.acceptance_guard.end()
            await asyncio.sleep(0.03)
            require(not owners[0].done(), 'cleanup cancelled the actual close owner before its first session_end')
            require(x.w.server._busy, 'cleanup released busy before existing close completed')
            require(not session.acceptance_guard.finished)
            await W.until(lambda:session.acceptance_guard.forced)
        finally:
            release.set()
        await wait_closed(x, ws)
        require(not session._closing)
        require(not session.active)
        require_equal(session.oa, None)
        require_equal((session.reader, session.timer, session._tool_worker, session._persist_worker), (None,)*4)
        require(not L.lesen(x.book)['laeufe'][0]['beendet'], 'hard deadline was upgraded to a confirmed close')
        finished_guard(session)


async def t_native_deadline_preserves_endpoint_close_owner_before_session_end():
    await delayed_close_owner(False)


async def t_native_deadline_preserves_timer_close_owner_before_session_end():
    await delayed_close_owner(True)


async def t_native_cleanup_rechecks_close_owner_after_pump_cancellation_await():
    async with native_world() as x:
        cancelled, acknowledge = asyncio.Event(), asyncio.Event()
        frames = E._acceptance_frames
        async def delayed_cancellation(*args):
            try:
                async for frame in frames(*args):
                    yield frame
            except asyncio.CancelledError:
                cancelled.set()
                await acknowledge.wait()
                raise
        with patch.object(E, '_acceptance_frames', delayed_cancellation):
            ws, session = await start(x)
            entered, release = asyncio.Event(), asyncio.Event()
            drain = session._drain_persistence
            calls = []
            async def parked_first_drain(*args, **kwargs):
                if not calls:
                    calls.append(asyncio.current_task())
                    entered.set()
                    await release.wait()
                return await drain(*args, **kwargs)
            session._drain_persistence = parked_first_drain
            session.acceptance_guard.end()
            try:
                await asyncio.wait_for(cancelled.wait(), 1)
                session.timer.cancel()
                await asyncio.gather(session.timer, return_exceptions=True)
                session.timer = asyncio.create_task(session.close(reason='timeout'))
                await asyncio.wait_for(entered.wait(), 1)
                acknowledge.set()
                await asyncio.sleep(0.03)
                require(x.w.server._busy, 'cleanup released busy while a later close owner still drained')
                require(not session.acceptance_guard.finished, 'cleanup booked an unfinished later close')
                require(not calls[0].done(), 'the later close owner ended before its parked drain was released')
            finally:
                acknowledge.set()
                release.set()
            await wait_closed(x, ws)
            require(not session._closing, 'the later close owner did not finish')
            require_equal(session.oa, None)
            require(L.lesen(x.book)['laeufe'][0]['beendet'], 'completed later close was not booked as confirmed')
            finished_guard(session)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
