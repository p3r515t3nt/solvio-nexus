"""Real local sockets/HTTP/Core; no remote provider, microphone or real budget."""
import asyncio
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent/'src'))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.realtime import acceptance_ledger as L, acceptance_voice as V
from solvio.realtime import core_server as CS
from test_voice_acceptance_ledger import _book
from test_browser_voice_endpoint import world, authenticated, receive, closed
from websockets.asyncio.client import connect, ClientConnection
from websockets.asyncio.server import serve


async def t_exact_remaining_timer_includes_cleanup_inside_allowance():
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'book.json';_book(path)
        wall=[1000.0]
        previous=L.beginnen(path,uhr=lambda:wall[0]);wall[0]+=200;previous.beenden('closed')
        run=L.beginnen(path,uhr=lambda:wall[0])
        timers=[]
        class Timer:
            def __init__(self,seconds,callback):timers.append((seconds,callback))
            def cancel(self):pass
        end=asyncio.Event()
        guard=V.AcceptanceGuard(run,end=end.set,clock=lambda:10,timer_factory=Timer)
        require_equal(timers[0][0],100)
        wall[0]+=98
        await guard.finish({'provider':'closed_confirmed','previous_unconfirmed':0})
        require_equal(L.rest(path),2)
        require(L.lesen(path)['laeufe'][-1]['beendet'])


async def t_thread_deadline_cuts_socket_while_event_loop_is_blocked_and_preserves_unknown():
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'book.json';_book(path,budget=0.15)
        run=L.beginnen(path)
        end=asyncio.Event()
        guard=V.AcceptanceGuard(run,end=end.set,reserve=0.02)
        mine,peer=socket.socketpair()
        try:
            guard._remember(mine)
            # This is intentional: an asyncio-only deadline cannot run here.
            time.sleep(0.22)
            peer.settimeout(0.2)
            require_equal(peer.recv(1),b'')
            require(guard.forced)
            await guard.finish({'provider':'closed_confirmed','previous_unconfirmed':0})
            require(not L.lesen(path)['laeufe'][-1]['beendet'], 'forced cut claimed provider acknowledgement')
            try:L.beginnen(path)
            except L.BudgetErschoepft:pass
            else:raise AssertionError('unknown prior provider released allowance')
        finally:
            mine.close();peer.close()


async def t_hanging_websocket_handshake_is_physically_cut_before_it_can_open():
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'book.json';_book(path,budget=0.25)
        ended=asyncio.Event();received=asyncio.Event();disconnected=asyncio.Event()
        async def handler(reader,writer):
            try:
                await reader.read(4096);received.set()
                require_equal(await reader.read(4096),b'')
                disconnected.set()
            finally:
                writer.close();await writer.wait_closed()
        server=await asyncio.start_server(handler,'127.0.0.1',0)
        guard=V.AcceptanceGuard(L.beginnen(path),end=ended.set,reserve=0.04)
        try:
            port=server.sockets[0].getsockname()[1]
            with patch('websockets.proxy.get_proxy',return_value=None):
                task=asyncio.create_task(guard.connect(connect,f'ws://127.0.0.1:{port}',open_timeout=10))
                await asyncio.wait_for(received.wait(),1)
                await asyncio.wait_for(disconnected.wait(),1)
                result=await asyncio.gather(task,return_exceptions=True)
            require(isinstance(result[0],BaseException))
            # Timer and connector timeout may race; both cut this tracked socket.
            require(guard.remaining==0 or guard.forced)
            await guard.finish({'provider':'unknown','previous_unconfirmed':0})
            require(not L.lesen(path)['laeufe'][0]['beendet'])
        finally:
            server.close();await server.wait_closed()


async def t_existing_https_start_path_consumes_same_book_and_closes_local_provider():
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'book.json';_book(path,budget=30)
        provider_opened=asyncio.Event();provider_closed=asyncio.Event()
        async def provider(ws):
            provider_opened.set()
            await ws.send(json.dumps({'type':'session.created'}))
            try:
                async for _ in ws:pass
            finally:provider_closed.set()
        async with serve(provider,'127.0.0.1',0) as upstream:
            port=upstream.sockets[0].getsockname()[1]
            async with world() as w:
                V.attach(w.server,environment={'SOLVIO_ABNAHME':'1','SOLVIO_ABNAHME_BUCH':str(path)})
                with patch.object(CS,'ws_connect',connect),patch.object(CS,'REALTIME_URL',f'ws://127.0.0.1:{port}'),patch('websockets.proxy.get_proxy',return_value=None):
                    ws,_=await authenticated(w)
                    require_equal(L.gespraeche(path),0)
                    require(not provider_opened.is_set())
                    await ws.send_json({'type':'session_start'})
                    await receive(ws,'session_ready')
                    require_equal(L.gespraeche(path),1)
                    await ws.send_json({'type':'session_end'})
                    observation=await receive(ws,'session_closed')
                    await closed(ws)
                    require_equal(observation['provider'],'closed_confirmed')
                    await asyncio.wait_for(provider_closed.wait(),1)
                    require(L.lesen(path)['laeufe'][0]['beendet'])
                    require(0<L.verbraucht(L.lesen(path))<30)


async def t_no_implicit_book_and_no_limit_in_normal_mode():
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'missing.json'
        async with world() as w:
            V.attach(w.server,environment={})
            require(not hasattr(w.server,'browser_voice_admission'))
            V.attach(w.server,environment={'SOLVIO_ABNAHME':'1','SOLVIO_ABNAHME_BUCH':str(path)})
            ws,_=await authenticated(w)
            await ws.send_json({'type':'session_start'})
            observation=await receive(ws,'session_closed')
            await closed(ws)
            require_equal(observation['provider'],'not_opened')
            require(not path.exists())
            require_equal(len(w.providers),0)


async def t_actual_core_reconnect_keeps_one_admission_and_original_deadline():
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'book.json';_book(path,budget=30)
        disconnect=asyncio.Event();reconnected=asyncio.Event();connections=[]
        async def provider(ws):
            connections.append(ws)
            number=len(connections)
            await ws.send(json.dumps({'type':'session.created'}))
            if number==1:
                await disconnect.wait()
                await ws.close(code=1011,reason='synthetic transport loss')
            else:
                reconnected.set()
                async for _ in ws:pass
        async with serve(provider,'127.0.0.1',0) as upstream:
            port=upstream.sockets[0].getsockname()[1]
            async with world() as w:
                V.attach(w.server,environment={'SOLVIO_ABNAHME':'1','SOLVIO_ABNAHME_BUCH':str(path)})
                with patch.object(CS,'ws_connect',connect),patch.object(CS,'REALTIME_URL',f'ws://127.0.0.1:{port}'),patch.object(CS,'RECONNECT_BACKOFF',0.01),patch('websockets.proxy.get_proxy',return_value=None):
                    ws,_=await authenticated(w)
                    await ws.send_json({'type':'session_start'})
                    await receive(ws,'session_ready')
                    sess=w.sessions[0];guard=sess.acceptance_guard;deadline=guard._deadline
                    disconnect.set()
                    await asyncio.wait_for(reconnected.wait(),2)
                    for _ in range(100):
                        if sess.active and sess.open_attempts==2:break
                        await asyncio.sleep(.005)
                    require_equal(sess.open_attempts,2)
                    require(sess.acceptance_guard is guard)
                    require_equal(guard._deadline,deadline)
                    require_equal(L.gespraeche(path),1)
                    await ws.send_json({'type':'session_end'})
                    end=await receive(ws,'session_closed');await closed(ws)
                    require_equal(end['generation'],2)
                    require_equal(end['provider'],'closed_confirmed')
                    require_equal(end['previous_unconfirmed'],0)
                    require_equal(len(connections),2)
                    require(L.lesen(path)['laeufe'][0]['beendet'])


async def t_expired_guard_cannot_create_a_late_provider_or_reconnect():
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'book.json';_book(path)
        end=asyncio.Event();guard=V.AcceptanceGuard(L.beginnen(path),end=end.set)
        calls=[]
        async def connector(*args,**kwargs):calls.append(args);raise AssertionError('opened')
        guard._hard_stop()
        try:await guard.connect(connector,'ws://127.0.0.1:9')
        except L.BudgetErschoepft:pass
        else:raise AssertionError('expired generation restarted')
        require_equal(calls,[])
        await guard.finish({'provider':'unknown','previous_unconfirmed':0})


async def t_failed_native_handshake_cuts_its_duplicate_before_successful_address_fallback():
    """Actual native connector and local TCP: only first handshake fails.

    Closing the original FD alone leaves the guard's duplicate connected. The
    first peer must see EOF while the second connection remains usable.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'book.json';_book(path,budget=30)
        first_closed,second_opened,second_closed=asyncio.Event(),asyncio.Event(),asyncio.Event()
        async def first_peer(reader,writer):
            try:
                require_equal(await reader.read(1),b'')
                first_closed.set()
            finally:
                writer.close();await writer.wait_closed()
        async def second_peer(ws):
            second_opened.set()
            try:
                async for message in ws:
                    await ws.send(message)
            finally:
                second_closed.set()
        first=await asyncio.start_server(first_peer,'127.0.0.1',0)
        guard=V.AcceptanceGuard(L.beginnen(path),end=lambda:None)
        connection=None
        try:
            async with serve(second_peer,'127.0.0.1',0) as second:
                ports=[first.sockets[0].getsockname()[1],second.sockets[0].getsockname()[1]]
                loop=asyncio.get_running_loop()
                async def addresses(*args,**kwargs):
                    return [(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',('127.0.0.1',port)) for port in ports]
                original=ClientConnection.handshake
                calls=[]
                async def handshake(self,*args,**kwargs):
                    calls.append(1)
                    if len(calls)==1:
                        raise OSError('synthetic first-address native handshake failure')
                    return await original(self,*args,**kwargs)
                with patch.object(loop,'getaddrinfo',addresses), \
                        patch.object(ClientConnection,'handshake',handshake), \
                        patch('websockets.proxy.get_proxy',return_value=None):
                    connection=await guard.connect(connect,f'ws://127.0.0.1:{ports[0]}',open_timeout=2)
                await asyncio.wait_for(first_closed.wait(),1)
                require_equal(len(calls),2)
                require(second_opened.is_set())
                require(not second_closed.is_set())
                require(not guard.forced)
                # Closing the failed socket must not cut its successful sibling.
                await connection.send('second connection remains live')
                require_equal(await asyncio.wait_for(connection.recv(),1),'second connection remains live')
                await connection.close()
                await asyncio.wait_for(second_closed.wait(),1)
                await guard.finish({'provider':'closed_confirmed','previous_unconfirmed':0})
                require(L.lesen(path)['laeufe'][0]['beendet'])
        finally:
            if connection is not None:
                await connection.close()
            if not guard.finished:
                await guard.finish({'provider':'unknown','previous_unconfirmed':0})
            first.close();await first.wait_closed()


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
