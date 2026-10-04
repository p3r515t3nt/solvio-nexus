"""Real HTTPS + browser sessions + Core Session/pump, only provider/device synthetic."""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import atexit
import shutil
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from aiohttp import CookieJar, TCPConnector, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer, unused_port
from solvio import browser_voice_endpoint as V, voice_endpoint as APP
from solvio.dashboard.auth import OWNER
from solvio.realtime import core_server as CS
from solvio.realtime.audio_observations import AudioObservations
from solvio.security.mobile_approval import browser_sessions as B, store as S, identity
from test_browser_sessions import _tls
from test_m0_realtime_latency import _FakeProvider, _openable_session


class Provider(_FakeProvider):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.events = asyncio.Queue()
        self.closing = asyncio.Event()
        self.release = None
        self.fail_close = False

    async def send(self, data):
        self.sent.append(json.loads(data))

    def __aiter__(self):
        async def frames():
            while True:
                yield json.dumps(await self.events.get())
        return frames()

    async def close(self):
        self.closing.set()
        if self.release is not None:
            await self.release.wait()
        if self.fail_close:
            raise OSError('synthetic provider close unavailable')
        self.closed = True


@asynccontextmanager
async def world():
    with tempfile.TemporaryDirectory(prefix='solvio-browser-voice-') as folder:
        store = S.ApprovalControlStore(str(Path(folder)/'approvals.db'))
        await store.open()
        service = B.BrowserSessionService(store, core_instance_id=identity.load_or_create_core_instance_id(folder))
        port = unused_port()
        origin = f'https://127.0.0.1:{port}'
        app = web.Application()
        B.attach(app,service,{origin})
        app[OWNER] = 'local-owner'
        server = _openable_session().server
        server._busy = False
        server._closing = False
        server.audio_observations = AudioObservations()
        V.attach(app,server)
        APP.attach(app,server)
        server_tls,client_tls = _tls(folder)
        http = TestServer(app,port=port,scheme='https')
        await http.start_server(ssl=server_tls)
        clients, sessions, providers = [], [], []
        actual_session = CS.Session
        def session(*args,**kwargs):
            value = actual_session(*args,**kwargs)
            sessions.append(value)
            return value
        async def connect(*args,**kwargs):
            provider = Provider()
            providers.append(provider)
            return provider
        async def new_client():
            client = TestClient(http,cookie_jar=CookieJar(unsafe=True),connector=TCPConnector(ssl=client_tls))
            await client.start_server()
            clients.append(client)
            return client
        async def login(client,principal='local-owner'):
            enrollment = await service.issue_enrollment(principal=principal)
            response = await client.post(B.SESSION_PATH+'/login',json={'token':enrollment.token},headers={'Origin':origin})
            require_equal(response.status,200)
            data = await response.json()
            return {'Origin':origin,B.CSRF_HEADER:data['csrf_token']},data
        client = await new_client()
        headers,auth = await login(client)
        w = SimpleNamespace(app=app,server=server,store=store,service=service,origin=origin,
            endpoint=app[V.STATE],client=client,headers=headers,auth=auth,new_client=new_client,
            login=login,sessions=sessions,providers=providers,http=http)
        with patch.object(CS,'Session',session),patch.object(CS,'ws_connect',connect), \
                patch.object(V,'RECHECK_SECONDS',0.03):
            try:
                yield w
            finally:
                for provider in providers:
                    if provider.release is not None:
                        provider.release.set()
                await asyncio.gather(*(client.close() for client in clients),return_exceptions=True)
                await http.close()
                await store.close()
                require(not w.endpoint.handlers)
                require(not server._busy)
                for sess in sessions:
                    require(not sess._draining)


async def issued(w,client=None,headers=None):
    response = await (client or w.client).post(V.SESSION_PATH,json={},headers=headers or w.headers)
    require_equal(response.status,201)
    require_equal(response.headers['Cache-Control'],'no-store')
    data = await response.json()
    require_equal(data['audio'],{'encoding':'pcm_s16le','sample_rate':16000,'channels':1})
    return data


async def receive(ws,kind):
    for _ in range(20):
        msg = await asyncio.wait_for(ws.receive(),2)
        if msg.type == WSMsgType.TEXT:
            value = json.loads(msg.data)
            if value.get('type')==kind:
                return value
        elif msg.type in {WSMsgType.CLOSE,WSMsgType.CLOSED,WSMsgType.ERROR}:
            raise AssertionError('Socket ended before '+kind+': '+str(msg.type))
    raise AssertionError('bounded receive did not find '+kind)


async def authenticated(w,nonce=None):
    nonce = nonce or (await issued(w))['nonce']
    ws = await w.client.ws_connect(V.PATH,headers={'Origin':w.origin})
    await ws.send_json({'type':'session_auth','nonce':nonce})
    auth = await receive(ws,'session_authenticated')
    require(auth['connection_id'].startswith('browser:'))
    require_equal(w.providers,[], 'authentication opened a provider')
    return ws,auth


async def started(w):
    ws,auth = await authenticated(w)
    await ws.send_json({'type':'session_start'})
    ready = await receive(ws,'session_ready')
    require_equal((ready['session_id'],ready['connection_id'],ready['generation']),
                  (auth['session_id'],auth['connection_id'],1))
    return ws,auth


async def closed(ws):
    for _ in range(20):
        msg = await asyncio.wait_for(ws.receive(),2)
        if msg.type in {WSMsgType.CLOSE,WSMsgType.CLOSED,WSMsgType.ERROR}:
            return
    raise AssertionError('socket did not close')


async def t_real_https_auth_rejects_missing_origin_csrf_foreign_owner_and_replay():
    async with world() as w:
        for headers in ({'Origin':w.origin},{B.CSRF_HEADER:w.headers[B.CSRF_HEADER]},
                        dict(w.headers,Origin='https://foreign.example')):
            require_equal((await w.client.post(V.SESSION_PATH,json={},headers=headers)).status,401)
        for origin in (None,'https://foreign.example'):
            response = await w.client.get(V.PATH,headers={'Origin':origin} if origin else {})
            require_equal(response.status,401)
        other = await w.new_client()
        headers,_ = await w.login(other,'another-owner')
        require_equal((await other.post(V.SESSION_PATH,json={},headers=headers)).status,401)
        nonce = (await issued(w))['nonce']
        ws,_ = await authenticated(w,nonce)
        await ws.send_json({'type':'session_end'})
        require_equal((await receive(ws,'session_closed'))['provider'],'not_opened')
        await closed(ws)
        replay = await w.client.ws_connect(V.PATH,headers={'Origin':w.origin})
        await replay.send_json({'type':'session_auth','nonce':nonce})
        await closed(replay)
        require_equal(len(w.sessions),1)
        require_equal(w.providers,[])
        require_equal(await w.store._run(lambda:w.store._conn.execute('SELECT COUNT(*) FROM devices').fetchone()[0]),0)


async def t_nonce_is_bound_to_browser_core_ttl_and_bounded_pending_inventory():
    async with world() as w:
        nonce = (await issued(w))['nonce']
        second = await w.new_client()
        await w.login(second)
        socket = await second.ws_connect(V.PATH,headers={'Origin':w.origin})
        await socket.send_json({'type':'session_auth','nonce':nonce})
        await closed(socket)
        expired = (await issued(w))['nonce']
        with patch.object(w.endpoint.nonces,'clock',lambda:time.time()+31):
            socket = await w.client.ws_connect(V.PATH,headers={'Origin':w.origin})
            await socket.send_json({'type':'session_auth','nonce':expired})
            await closed(socket)
        actor = await w.service.authenticate(w.client.session.cookie_jar.filter_cookies(w.client.make_url('/'))[B.COOKIE_NAME].value)
        value = w.endpoint.nonces.issue(actor,'original-core')[0]
        require(not w.endpoint.nonces.consume(value,actor,'another-core'))
        w.endpoint.nonces.pending.clear()
        for _ in range(V.MAX_NONCES):
            require(w.endpoint.nonces.issue(actor,w.service.core_instance_id) is not None)
        require_equal((await w.client.post(V.SESSION_PATH,json={},headers=w.headers)).status,429)
        require_equal(w.sessions,[])
        require_equal(w.providers,[])


async def t_actual_session_pump_pcm_and_confirmed_end_are_shared():
    async with world() as w:
        ws,auth = await started(w)
        sess = w.sessions[0]
        require_equal(sess.channel,'voice_browser')
        require_equal(sess.satellite_id,'local-owner')
        require(await sess.browser_task_session.current())
        await ws.send_bytes(b'\x01\x02'*320)
        for _ in range(100):
            if any(x.get('type')=='input_audio_buffer.append' for x in w.providers[0].sent):
                break
            await asyncio.sleep(0.01)
        require(any(x.get('type')=='input_audio_buffer.append' for x in w.providers[0].sent))
        await w.providers[0].events.put({'type':'response.created'})
        await w.providers[0].events.put({'type':'response.output_audio.delta','delta':base64.b64encode(b'\x01\x02'*480).decode()})
        while True:
            message = await asyncio.wait_for(ws.receive(),2)
            if message.type==WSMsgType.BINARY:
                require(len(message.data)>0)
                break
        await ws.send_json({'type':'barge_in','played_ms':20})
        await receive(ws,'flush')
        require(any(x.get('type')=='response.cancel' for x in w.providers[0].sent))
        await ws.send_json({'type':'heard','played_ms':20})
        await ws.send_json({'type':'session_end'})
        end = await receive(ws,'session_closed')
        require_equal((end['provider'],end['generation'],end['previous_unconfirmed']),('closed_confirmed',1,0))
        require_equal(end['session_id'],auth['session_id'])
        await closed(ws)
        require(w.providers[0].closed)
        require_equal(w.server._busy,False)
        require_equal(sess.browser_task_session,None)


async def t_ready_duplex_capability_comes_only_from_the_selected_core_model():
    for model, expected in (("gpt-live-1", "full_duplex"), ("gpt-realtime", "turn_based"),
                            ("unconfigured", "turn_based")):
        sent = []
        async def send(payload):
            sent.append(json.loads(payload))
        observer = AudioObservations()
        observer.connect("browser:owned", "owned-session", "voice_browser")
        observer.event("browser:owned", "owned-session", 1, "opening")
        observer.event("browser:owned", "owned-session", 1, "ready")
        socket = V._BrowserSocket(SimpleNamespace(closed=False,send_str=send), "browser:owned")
        socket.session = SimpleNamespace(session_id="owned-session",open_attempts=1,
            server=SimpleNamespace(model=model,audio_observations=observer))
        await socket.send(json.dumps({"type":"session_ready", "voice_mode":"full_duplex",
            "session_id":"foreign", "connection_id":"browser:foreign", "generation":999}))
        require_equal(sent, [{"type":"session_ready", "voice_mode":expected,
            "session_id":"owned-session", "connection_id":"browser:owned", "generation":1}])


async def t_revocation_and_start_timeout_release_ownership_without_reopening():
    async with world() as w:
        with patch.object(V,'START_SECONDS',0.05):
            ws,_ = await authenticated(w)
            require_equal((await receive(ws,'session_closed'))['provider'],'not_opened')
            await closed(ws)
        require_equal(w.providers,[])
        ws,_ = await started(w)
        response = await w.client.post(B.SESSION_PATH+'/logout',json={},headers=w.headers)
        require_equal(response.status,200)
        require_equal((await receive(ws,'session_closed'))['provider'],'closed_confirmed')
        await closed(ws)
        require_equal(len(w.providers),1)
        require(not w.server._busy)


async def t_close_error_and_repeated_handler_cancel_do_not_fake_provider_end():
    for failed in (True,False):
        async with world() as w:
            ws,_ = await started(w)
            provider = w.providers[0]
            provider.fail_close = failed
            provider.release = asyncio.Event()
            await ws.send_json({'type':'session_end'})
            await asyncio.wait_for(provider.closing.wait(),2)
            require_equal(w.server.audio_observations.snapshot()['devices'][0]['provider'],'closing')
            handler = next(iter(w.endpoint.handlers))
            handler.cancel()
            await asyncio.sleep(0)
            handler.cancel()
            require(not handler.done())
            peer_closed = asyncio.create_task(closed(ws))
            provider.release.set()
            await asyncio.gather(handler,return_exceptions=True)
            await peer_closed
            row = w.server.audio_observations.snapshot()['devices'][0]
            require_equal(row['provider'],'unknown' if failed else 'closed_confirmed')
            require_equal(w.server._busy,False)
            require_equal(w.sessions[0]._draining,[])


async def t_parallel_browser_and_iphone_prepare_have_one_conversation_owner():
    async with world() as w:
        prepared,release = asyncio.Event(),asyncio.Event()
        original = web.WebSocketResponse.prepare
        async def gate(ws,request):
            result = await original(ws,request)
            if request.path == APP.PATH:
                prepared.set()
                await release.wait()
            return result
        with patch.object(web.WebSocketResponse,'prepare',gate), \
                patch.object(APP,'_owner_device',AsyncMock(return_value='synthetic-iphone')), \
                patch.object(APP,'_session_proof',AsyncMock(return_value=False)):
            iphone = await w.client.ws_connect(APP.PATH)
            await asyncio.wait_for(prepared.wait(),2)
            browser,_ = await authenticated(w)
            release.set()
            await closed(iphone)
            require(w.server._busy)
            require_equal(len(w.sessions),1)
            await browser.send_json({'type':'session_end'})
            await receive(browser,'session_closed')
            await closed(browser)
        require_equal(w.providers,[])


async def t_shutdown_during_prepare_never_creates_a_late_session():
    async with world() as w:
        await issued(w)
        prepared,release = asyncio.Event(),asyncio.Event()
        original = web.WebSocketResponse.prepare
        async def gate(ws,request):
            result = await original(ws,request)
            if request.path == V.PATH:
                prepared.set()
                await release.wait()
            return result
        with patch.object(web.WebSocketResponse,'prepare',gate):
            socket = await w.client.ws_connect(V.PATH,headers={'Origin':w.origin})
            await asyncio.wait_for(prepared.wait(),2)
            cleanup = asyncio.create_task(w.http.close())
            while not w.endpoint.closing:
                await asyncio.sleep(0)
            release.set()
            peer_closed = asyncio.create_task(closed(socket))
            await asyncio.wait_for(cleanup,3)
            await peer_closed
        require_equal(w.sessions,[])
        require_equal(w.providers,[])


async def t_provider_initiated_close_finishes_before_transport_reports_closed():
    async with world() as w:
        ws,_ = await started(w)
        provider = w.providers[0]
        provider.release = asyncio.Event()
        closing = asyncio.create_task(w.sessions[0].close('timeout'))
        await asyncio.wait_for(provider.closing.wait(),2)
        await receive(ws,'session_end')
        require(w.server._busy)
        require_equal(w.server.audio_observations.snapshot()['devices'][0]['provider'],'closing')
        provider.release.set()
        end = await receive(ws,'session_closed')
        require_equal(end['provider'],'closed_confirmed')
        await closed(ws)
        await closing
        require_equal(len(w.providers),1)
        require(not w.server._busy)


async def t_optional_admission_and_close_hooks_are_inside_the_verified_lifecycle():
    for outcome in ('reject','error','logout','allow'):
        async with world() as w:
            calls = []
            async def admission(sess):
                require_equal(w.providers,[])
                calls.append(('admission',sess.session_id))
                if outcome=='error':
                    raise ValueError('synthetic admission unavailable')
                if outcome=='logout':
                    await w.service.revoke(w.auth['session_id'],principal='local-owner')
                return outcome!='reject'
            async def final(sess,observation):
                require(not sess._draining)
                if w.providers:
                    require(w.providers[0].closed)
                calls.append(('closed',observation['provider']))
            w.server.browser_voice_admission = admission
            w.server.browser_voice_closed = final
            ws,_ = await authenticated(w)
            await ws.send_json({'type':'session_start'})
            if outcome=='allow':
                await receive(ws,'session_ready')
                await ws.send_json({'type':'session_end'})
            end = await receive(ws,'session_closed')
            await closed(ws)
            expected = 'closed_confirmed' if outcome=='allow' else 'not_opened'
            require_equal(end['provider'],expected)
            require_equal([c[0] for c in calls],['admission','closed'])
            require_equal(calls[-1][1],expected)
            require_equal(len(w.providers),1 if outcome=='allow' else 0)


async def t_auth_timeout_and_invalid_audio_frames_never_escape_the_bounded_transport():
    async with world() as w:
        with patch.object(V,'AUTH_SECONDS',0.03):
            socket = await w.client.ws_connect(V.PATH,headers={'Origin':w.origin})
            await closed(socket)
        require_equal(w.sessions,[])
        for frame in (b'\x01',b'\x00'*(V.MAX_FRAME+2)):
            socket,_ = await authenticated(w)
            await socket.send_bytes(frame)
            await closed(socket)
            # aiohttp can close an oversized transport before Core cleanup.
            # WSclose itself is deliberately not treated as an end receipt.
            await asyncio.wait_for(asyncio.gather(*tuple(w.endpoint.handlers),return_exceptions=True),2)
            require(not w.server._busy)
        require_equal(w.providers,[])


# ---------------------------------------------------------------- N8/C3 §7: Sprachbindung an einen Chat

async def closed_with(ws, code):
    for _ in range(20):
        msg = await asyncio.wait_for(ws.receive(),2)
        if msg.type in {WSMsgType.CLOSE,WSMsgType.CLOSED,WSMsgType.ERROR}:
            require_equal(ws.close_code, code, f'socket closed with {ws.close_code}, not {code}')
            return
    raise AssertionError('socket did not close')


def _chat_store(w):
    from solvio.conversation import ConversationStore
    folder = tempfile.mkdtemp(prefix='solvio-browser-voice-chat-')
    atexit.register(shutil.rmtree, folder, True)  # Review Runde 11, C11-H3: kein Rest im TMPDIR
    store = ConversationStore(str(Path(folder)/'conversations.sqlite3')).open()
    w.server.conversations = store
    return store


async def t_c3_a_ticket_with_a_conversation_binds_the_session_to_that_chat_with_the_proof_principal():
    async with world() as w:
        store = _chat_store(w)
        try:
            chat,_ = store.create_conversation(owner_principal='local-owner',kind='text',client_request_id='chat-voice-0001')
            cid = chat['conversation_id']
            store.add_message(cid,'user','Merk dir das Testwort Zaunkoenig.')
            foreign,_ = store.create_conversation(owner_principal='someone-else',kind='text',client_request_id='chat-voice-0002')
            # `{}` wie bisher; ein fremder oder unbekannter Chat ist 404; Formfehler 400.
            require_equal((await w.client.post(V.SESSION_PATH,json={},headers=w.headers)).status,201)
            response = await w.client.post(V.SESSION_PATH,json={'conversation_id':foreign['conversation_id']},headers=w.headers)
            require_equal(response.status,404)
            require_equal((await response.json())['error'],'unknown_conversation')
            require_equal((await w.client.post(V.SESSION_PATH,json={'conversation_id':'c-00000000000000ff'},headers=w.headers)).status,404)
            for bad in ({'conversation_id':'nope'},{'conversation_id':7},{'conversation_id':cid,'extra':1},[cid]):
                require_equal((await w.client.post(V.SESSION_PATH,json=bad,headers=w.headers)).status,400)
            before = len(store.conn.execute('SELECT * FROM conversations').fetchall())
            # Das gebundene Ticket: die Sitzung schreibt in DIESEN Chat und spielt SEINEN Verlauf ein.
            response = await w.client.post(V.SESSION_PATH,json={'conversation_id':cid},headers=w.headers)
            require_equal(response.status,201,await response.text())
            nonce = (await response.json())['nonce']
            ws,auth = await authenticated(w,nonce)
            await ws.send_json({'type':'session_start'})
            await receive(ws,'session_ready')
            sess = w.sessions[0]
            require_equal((sess.bound_conversation_id,sess.conversation_id,sess.conversation_mode),(cid,cid,'active'))
            require_equal(sess.conversation_principal(),'local-owner')
            require(any(x.get('type')=='conversation.item.create' and 'Zaunkoenig' in json.dumps(x) for x in w.providers[0].sent),
                    'the bound chat history was not replayed into the provider session')
            require_equal(len(store.conn.execute('SELECT * FROM conversations').fetchall()),before,'a linger conversation was created')
            require_equal([row['session_id'] for row in store.sessions_of(cid)],[sess.session_id])
            await ws.send_json({'type':'session_end'})
            await receive(ws,'session_closed')
            await closed(ws)
            require_equal(store.sessions_of(cid)[0]['close_reason'] is not None,True)
        finally:
            store.close()


async def t_c3_a_chat_that_vanishes_after_the_ticket_closes_the_socket_with_4401_and_binds_nothing():
    async with world() as w:
        store = _chat_store(w)
        try:
            chat,_ = store.create_conversation(owner_principal='local-owner',kind='text',client_request_id='chat-voice-0001')
            cid = chat['conversation_id']
            # (a) geloescht zwischen Ticket und Socket: 4401 vor jeder Anbietersitzung.
            nonce = (await (await w.client.post(V.SESSION_PATH,json={'conversation_id':cid},headers=w.headers)).json())['nonce']
            store.delete_conversation(cid)
            ws = await w.client.ws_connect(V.PATH,headers={'Origin':w.origin})
            await ws.send_json({'type':'session_auth','nonce':nonce})
            await closed_with(ws,4401)
            await asyncio.wait_for(asyncio.gather(*tuple(w.endpoint.handlers),return_exceptions=True),2)
            require_equal(w.providers,[])
            require_equal(store.conn.execute('SELECT COUNT(*) FROM conversations').fetchone()[0],0,'a linger conversation replaced the requested chat')
            require(not w.server._busy)
            # (b) geloescht zwischen Socket und session_start: die Sitzung wird verweigert, nie degradiert.
            chat,_ = store.create_conversation(owner_principal='local-owner',kind='text',client_request_id='chat-voice-0002')
            cid = chat['conversation_id']
            nonce = (await (await w.client.post(V.SESSION_PATH,json={'conversation_id':cid},headers=w.headers)).json())['nonce']
            ws,_ = await authenticated(w,nonce)
            store.delete_conversation(cid)
            await ws.send_json({'type':'session_start'})
            await closed_with(ws,4401)
            await asyncio.wait_for(asyncio.gather(*tuple(w.endpoint.handlers),return_exceptions=True),2)
            sess = w.sessions[-1]
            require_equal((sess.conversation_mode,sess.conversation_id,sess.active),('refused',None,False))
            require_equal(store.conn.execute('SELECT COUNT(*) FROM conversations').fetchone()[0],0)
            require_equal(store.conn.execute('SELECT COUNT(*) FROM conversation_messages').fetchone()[0],0)
            require(not w.server._busy)
            # Ohne Chatwunsch bleibt alles wie bisher: ein Linger-Gespraech, Owner aus dem Beweis.
            nonce = (await issued(w))['nonce']
            ws = await w.client.ws_connect(V.PATH,headers={'Origin':w.origin})
            await ws.send_json({'type':'session_auth','nonce':nonce})
            await receive(ws,'session_authenticated')
            await ws.send_json({'type':'session_start'})
            await receive(ws,'session_ready')
            sess = w.sessions[-1]
            require_equal((sess.bound_conversation_id,sess.conversation_mode),('','active'))
            linger = store.conversation(sess.conversation_id)
            require_equal((linger['explicit'],linger['kind'],linger['owner_principal']),(0,'voice','local-owner'))
            await ws.send_json({'type':'session_end'})
            await receive(ws,'session_closed')
            await closed(ws)
        finally:
            store.close()


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
