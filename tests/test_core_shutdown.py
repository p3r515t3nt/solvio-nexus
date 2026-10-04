"""Real SIGTERM + live HTTP/Unix sockets + existing workers; all state is temporary."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parents[1]/'src'))
sys.path.insert(0, str(Path(__file__).parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def _child(root, mode):
    from aiohttp import web
    from solvio.realtime import core_server as CS
    from solvio.capabilities.approver_runtime import ApproverRuntime
    from solvio import config, voice_endpoint as VE
    from solvio.agent_runtime import orchestrator as O, planner as P, extension_runtime as E
    from solvio.agent_runtime.store import AgentRunLedger
    from solvio.agent_runtime.workspace import WorkspaceManager
    from solvio.memory.adaptive.pipeline import AdaptiveMemory
    from solvio.memory.adaptive.candidates import CandidateStore
    from solvio.conversation import ConversationStore
    from solvio.security.mobile_approval.store import ApprovalControlStore
    from solvio.specialists import launcher as L
    from solvio.tools import registry as R
    from solvio.bots import team as BT
    from solvio.doctor import supervisor as SU
    from test_m0_realtime_latency import _FakeProvider
    root=Path(root)
    os.environ.update(SOLVIO_STATE_DIR=str(root), SOLVIO_CONTROL_SOCKET_PATH=str(root/'control.sock'),
        SOLVIO_SCHEDULER='0', SOLVIO_AUTOPILOT='0', SOLVIO_AGENT_RUNTIME='1', SOLVIO_DOCTOR='1')
    def event(name):
        with (root/'events').open('a') as f: f.write(name+'\n')
    # Launcher owns each real child group; ignored TERM exercises escalation.
    worker=root/'worker.py'
    worker.write_text("import os,signal,time,sys\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n")
    def invocation(name):
        return L.Invocation(sys.executable,(str(worker),str(root/(name+'.pid'))),
            timeout=45,cwd=str(root),cleanup_group=True,shutdown_grace=0.15)
    candidates=CandidateStore(str(root/'memory'))
    adaptive=AdaptiveMemory(SimpleNamespace(),candidates)
    async def process(*args, **kwargs):
        await L.run(invocation('learning'),'')
    adaptive.process=process
    adaptive.start()
    adaptive._queue.put_nowait((None,'',None))
    ledger=AgentRunLedger(str(root/'agent_runs.sqlite3'))
    planner=SimpleNamespace(route={'provider':'codex','billing_mode':'subscription'})
    orch=O.Orchestrator(ledger=ledger,planner=planner,workspaces=WorkspaceManager(allowed=(str(root),)))
    original_start=orch.start
    async def start():
        await original_start()
        orch._drivers['owned-test-worker']=asyncio.create_task(L.run(invocation('agent'),''))
    orch.start=start
    conversations=ConversationStore(path=str(root/'conversations.sqlite3')).open()
    class Doctor:
        async def start(self): pass
        async def stop(self):
            event('supervisor_stop')
            await asyncio.sleep(.3)  # actual signal is repeated while inside cleanup
            if mode=='failure': raise RuntimeError('synthetic cleanup error')
    dispatcher=SimpleNamespace(openai_tools=lambda:[], adaptive_memory=adaptive,doctor=object(),capabilities=SimpleNamespace(names=lambda:[]),
        memory=None,conversations=conversations)
    server=CS.CoreServer('', 'no-provider', '127.0.0.1',0,dispatcher=dispatcher,conversations=conversations)
    class Gateway(ApproverRuntime):
        def __init__(self):
            self._stopping=False; self._runner=None; self._store=None; self.approvals=SimpleNamespace(pending=AsyncMock(return_value=[]))
            self.port=0
        async def start(self):
            self._store=ApprovalControlStore(str(root/'approval.sqlite3'))
            await self._store.open()
            app=web.Application(middlewares=[self._shutdown_guard()])
            async def touch(request):
                event('http_admitted')
                return web.json_response({'ok':True})
            app.router.add_get('/test',touch)
            VE.attach(app,server)
            self._runner=web.AppRunner(app,shutdown_timeout=1)
            await self._runner.setup()
            site=web.TCPSite(self._runner,'127.0.0.1',0)
            await site.start()
            self.port=site._server.sockets[0].getsockname()[1]
            if mode=='early':
                while not (root/'learning.pid').exists(): await asyncio.sleep(.01)
                (root/'ready.json').write_text(json.dumps({'port':self.port,'early':True}))
                await asyncio.Future()  # SIGTERM in actual asynchronous bootstrap
    gateway=Gateway()
    class Provider(_FakeProvider):
        async def close(self, *a, **kw):
            await super().close(*a, **kw)
            event('provider_closed')
    provider=Provider()
    native_ws=CS.ws_serve
    async def listen(*args,**kwargs):
        result=await native_ws(*args,**kwargs)
        while not (root/'agent.pid').exists() or not (root/'learning.pid').exists(): await asyncio.sleep(.01)
        (root/'ready.json').write_text(json.dumps({'port':gateway.port,'early':False}))
        return result
    with patch.object(CS,'approver_from_env',return_value=gateway), \
         patch.object(CS,'deep_from_env',return_value=None), \
         patch.object(CS,'_scrub_jail_credentials',return_value=0), \
         patch.object(CS,'ws_serve',listen), \
         patch.object(CS,'ws_connect',AsyncMock(return_value=provider)), \
         patch.object(config,'load_settings',return_value=SimpleNamespace(openai_api_key='',cognitive_router_mode='off')), \
         patch.object(BT,'from_environment',return_value=None), \
         patch.object(O,'Orchestrator',return_value=orch), \
         patch.object(P,'planner_from_settings',return_value=planner), \
         patch.object(E,'attach_document_runtime'), \
         patch.object(R,'attach_agent_runtime'), patch.object(R,'attach_cognition'), \
         patch.object(SU,'Supervisor',return_value=Doctor()), \
         patch.object(VE,'_owner_device',AsyncMock(return_value='synthetic-device')), \
         patch.object(VE,'_session_proof',AsyncMock(return_value=False)):
        await CS.run_until_stopped(server)
    require(provider.closed, 'real Session closed its provider')
    require(candidates._closed,'candidate store closed')
    require_equal(conversations._conn,None)
    require_equal(gateway._runner,None)
    require(adaptive._worker is None)
    require_equal(orch._drivers,{})
    event('verified_end')


async def _scenario(mode):
    from aiohttp import ClientSession, ClientError
    from solvio.portal import protocol as wire
    with tempfile.TemporaryDirectory(prefix='solvio-signal-') as directory:
        root=Path(directory)
        env=os.environ.copy()
        proc=await asyncio.create_subprocess_exec(sys.executable,__file__,'--child',directory,mode,
            env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True)
        unix=None
        try:
            async def ready():
                while not (root/'ready.json').exists():
                    if proc.returncode is not None: raise RuntimeError((await proc.communicate())[1].decode())
                    await asyncio.sleep(.02)
                return json.loads((root/'ready.json').read_text())
            state=await asyncio.wait_for(ready(),12)
            async with ClientSession() as client:
                async with client.get(f"http://127.0.0.1:{state['port']}/test") as response:
                    require_equal(response.status,200)
                ws=await client.ws_connect(f"http://127.0.0.1:{state['port']}/v1/voice")
                await ws.send_json({'type':'session_start'})
                async def voice_ready():
                    while True:
                        reply=await ws.receive_json()
                        if reply.get('type')=='session_ready': return
                await asyncio.wait_for(voice_ready(),3)
                if not state['early']:
                    unix=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
                    unix.connect(str(root/'control.sock'))
                    # An accepted idle connection blocks P.decode in an executor.
                    await asyncio.sleep(.1)
                began=time.monotonic()
                proc.send_signal(signal.SIGTERM)
                await asyncio.sleep(.08)
                # The doctor is still draining: both existing HTTP/Unix clients
                # already lose admission, before the listeners finish cleanup.
                try:
                    async with client.get(f"http://127.0.0.1:{state['port']}/test") as refused:
                        require_equal(refused.status,503)
                except ClientError:
                    pass  # closed listener/connection is also a confirmed stop
                if unix is not None:
                    unix.settimeout(1)
                    try:
                        unix.sendall(wire.encode({'op':'health'}))
                        denied=await asyncio.to_thread(wire.decode,unix)
                        require_equal(denied.get('reason'),'core_stopping')
                    except (OSError,wire.ProtocolError):
                        pass
                proc.send_signal(signal.SIGTERM)
                stdout,stderr=await asyncio.wait_for(proc.communicate(),8)
                require_equal(proc.returncode,0,stderr.decode()[-1600:])
                require(time.monotonic()-began<8)
                require('core.shutdown_finished' in stdout.decode())
                require('verified_end' in (root/'events').read_text())
                require_equal((root/'events').read_text().count('http_admitted'),1)
                require('provider_closed' in (root/'events').read_text())
                if mode=='failure':
                    require('core.shutdown_component_failed' in stdout.decode())
                    require('clean=False' in stdout.decode())
                else:
                    require('clean=True' in stdout.decode())
                await ws.close()
            pids=[int(p.read_text()) for p in root.glob('*.pid')]
            require_equal(len(pids),1 if mode=='early' else 2)
            for pid in pids: require(not _alive(pid),f'worker still alive: {pid}')
            require(not (root/'control.sock').exists())
            print(f'cleanup {mode}: {time.monotonic()-began:.2f}s, {len(pids)} worker processes ended')
        finally:
            if unix is not None: unix.close()
            if proc.returncode is None:
                os.killpg(proc.pid,signal.SIGKILL)
                await proc.wait()
            for file in root.glob('*.pid'):
                pid=int(file.read_text())
                if _alive(pid):
                    try: os.killpg(pid,signal.SIGKILL)
                    except ProcessLookupError: pass


def t_real_sigterm_drains_normal_bootstrap_workers_http_voice_and_idle_control():
    asyncio.run(_scenario('normal'))


def t_real_sigterm_during_early_gateway_bootstrap_drains_partial_resources():
    asyncio.run(_scenario('early'))


def t_repeated_sigterm_and_component_failure_do_not_skip_other_cleanup():
    asyncio.run(_scenario('failure'))


def t_satellite_handshake_cannot_create_a_session_after_shutdown_begins():
    from solvio.realtime import core_server as CS
    async def go():
        server=CS.CoreServer('', 'no-provider', '127.0.0.1',0)
        ws=SimpleNamespace(close=AsyncMock(),remote_address='temporary')
        entered,release=asyncio.Event(),asyncio.Event()
        async def authenticate(ws):
            entered.set(); await release.wait(); return 'synthetic-pi'
        with patch.object(server,'_authenticate',authenticate),patch.object(CS,'Session') as session:
            job=asyncio.create_task(server._handle_pi(ws))
            await entered.wait()
            server._closing=True
            release.set(); await job
            session.assert_not_called()
            ws.close.assert_awaited_once()
            ws.close.reset_mock()
            await server._handle_pi(ws)
            session.assert_not_called(); ws.close.assert_awaited_once()
    asyncio.run(go())


def t_cli_signal_helper_restores_previous_handlers():
    from solvio.realtime import core_server as CS
    async def go():
        before={s:signal.getsignal(s) for s in (signal.SIGTERM,signal.SIGINT)}
        await CS.run_until_stopped(SimpleNamespace(serve=AsyncMock()))
        require_equal({s:signal.getsignal(s) for s in before},before)
    asyncio.run(go())


def t_app_handshake_resuming_after_gateway_shutdown_never_starts_a_session():
    from aiohttp import web, ClientSession
    from solvio import voice_endpoint as V
    from solvio.realtime import core_server as C
    async def go():
        entered, release, shutdown = asyncio.Event(), asyncio.Event(), asyncio.Event()
        server=SimpleNamespace(_closing=False,_busy=False)
        app=web.Application(); V.attach(app,server)
        async def on_shutdown(app):
            require_equal(len(app['voice_websockets']),0)
            shutdown.set()
        app.on_shutdown.append(on_shutdown)
        real_prepare=web.WebSocketResponse.prepare
        first=True
        async def prepare(ws,request):
            nonlocal first
            if first:
                first=False; entered.set(); await release.wait()
            return await real_prepare(ws,request)
        runner=web.AppRunner(app,shutdown_timeout=1)
        await runner.setup(); site=web.TCPSite(runner,'127.0.0.1',0)
        await site.start()
        port=site._server.sockets[0].getsockname()[1]
        try:
            with patch.object(V,'_owner_device',AsyncMock(return_value='synthetic-device')), \
                 patch.object(web.WebSocketResponse,'prepare',prepare), \
                 patch.object(C,'Session') as factory, \
                 patch.object(C,'pump_endpoint',AsyncMock()) as pump:
                async with ClientSession() as client:
                    connect=asyncio.create_task(client.ws_connect(f'http://127.0.0.1:{port}/v1/voice'))
                    await asyncio.wait_for(entered.wait(),2)
                    server._closing=True
                    draining=asyncio.create_task(runner.cleanup())
                    await asyncio.wait_for(shutdown.wait(),2)
                    release.set()
                    ws=await asyncio.wait_for(connect,2)
                    await ws.close()
                    await asyncio.wait_for(draining,3)
                    factory.assert_not_called(); pump.assert_not_awaited()
        finally:
            release.set(); await runner.cleanup()
    asyncio.run(go())


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--child':
        asyncio.run(_child(sys.argv[2],sys.argv[3]))
    else:
        from _harness import run_module
        raise SystemExit(run_module(globals(),__name__))
