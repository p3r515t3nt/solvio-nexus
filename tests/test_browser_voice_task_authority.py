"""Authenticated temporary browser identity -> voice gate -> real task/grant.

No real provider, audio, production state or device imitation. The final probe
starts at the actual HTTPS/nonce/WebSocket door and uses the Core tool loop.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_agent_task_entry as E
from solvio import browser_voice_session as V
from solvio.capabilities.invocation import CapabilityInvocationGate, voice_trust
from solvio.capabilities.policy import OriginClass, origin_for_session
from solvio.security.mobile_approval import browser_sessions as B
from solvio.tools.agent_capability_tools import AgentCapabilityTool

TASK = {"objective": "Vergleiche drei Hotels in Hamburg mit belastbaren Quellen."}


@asynccontextmanager
async def world():
    async with E.world() as w:
        cookie = w.client.session.cookie_jar.filter_cookies(w.server.make_url('/'))[B.COOKIE_NAME].value
        w.actor = await w.sessions.authenticate(cookie, csrf_token=w.headers[B.CSRF_HEADER])
        require(w.actor is not None)
        w.voice = SimpleNamespace(session_id='voice-browser-test', closed=False)
        w.proof = await V.verified_browser_task_session(actor=w.actor, service=w.sessions,
            session_id=w.voice.session_id, session_nonce='N'*43, alive=lambda:not w.voice.closed)
        require(w.proof is not None)
        w.gate = CapabilityInvocationGate()
        yield w


def begin(w, *, proof=None, turn='turn-one', origin=None, text=None):
    proof = w.proof if proof is None else proof
    w.gate.begin_turn(session_id=w.voice.session_id, turn_id=turn,
        principal='not-the-authorizer', trust=voice_trust(True),
        user_text=text or TASK['objective'],
        origin=origin or origin_for_session('voice_browser', interactive_proof=False,
            browser_task_session=proof, session_id=w.voice.session_id),
        browser_task_session=proof)


def tool(w):
    return AgentCapabilityTool('agent_task_research',w.router,w.gate,w.ledger)


async def t_browser_voice_task_is_one_owner_grant_without_extra_approval_and_survives_close():
    async with world() as w:
        begin(w)
        results = await asyncio.gather(tool(w).run(TASK),tool(w).run(TASK))
        require(all(result.success for result in results))
        require_equal(results[0].data['task_id'],results[1].data['task_id'])
        grant=w.orch.task_authority.for_run(results[0].data['run_id'])
        task=w.ledger.get_task(grant.task_id)
        require_equal((task.created_principal,task.created_origin),('local-owner','trusted_dashboard'))
        require_equal(grant.receipt_method,'dashboard_session')
        require(grant.receipt_reference.startswith('browser-voice:'))
        require_equal(await w.store.list_pending(),[])
        w.voice.closed=True
        w.gate.clear()
        require(w.orch.task_authority.active(grant.reference,task_id=task.task_id,run_id=grant.run_id).allowed)
        # Existing typed dashboard entrance is still independently usable.
        response=await w.start(dict(E.BODY,client_request_id='text-after-voice'))
        require_equal(response.status,201)
        require_equal(len(w.ledger.recent_runs()),2)


async def t_fake_origin_bool_foreign_session_and_unsealed_type_are_not_authority():
    async with world() as w:
        for proof in (True,{'principal':'local-owner'},replace(w.proof,_seal=None),
                      replace(w.proof,session_id='foreign-session')):
            require_equal(origin_for_session('voice_browser',interactive_proof=True,
                browser_task_session=proof,session_id=w.voice.session_id),OriginClass.UNSPECIFIED)
            begin(w,proof=proof,origin=OriginClass.TRUSTED_DASHBOARD)
            require(not (await tool(w).run(TASK)).success)
        require_equal(await w.store.list_pending(),[])
        require_equal(w.ledger.recent_runs(),[])
        require_equal(origin_for_session('voice_satellite',interactive_proof=True,
            browser_task_session=w.proof,session_id=w.voice.session_id),OriginClass.ROOM_VOICE)
        foreign=replace(w.actor,principal='foreign-owner')
        require_equal(await V.verified_browser_task_session(actor=foreign,service=w.sessions,
            session_id=w.voice.session_id,session_nonce='N'*43,alive=lambda:True),None)


async def t_revoke_expiry_core_change_and_end_never_fall_back_to_phone_approval():
    for change in ('revoke','expiry','core','end'):
        async with world() as w:
            begin(w)
            if change=='revoke':
                await w.sessions.revoke(w.actor.session_id,principal=w.actor.principal)
            elif change=='expiry':
                await w.store._run(lambda:w.store._conn.execute(
                    'UPDATE browser_sessions SET created_at=?,expires_at=? WHERE session_id=?',
                    (time.time()-20,time.time()-1,w.actor.session_id)))
            elif change=='core':
                w.sessions.core_instance_id='different-core'
            else:
                w.voice.closed=True
            # Without this early return a missing proof could become a new
            # mobile approval. Count the actual router entrance as well, so a
            # missing synthetic approver cannot accidentally hide that fallback.
            with patch.object(w.router,'execute',wraps=w.router.execute) as execute:
                result=await tool(w).run(TASK)
            require(not result.success,change)
            require_equal(result.error,'task_start_authorization_missing',change)
            require_equal(execute.await_count,0,change)
            require_equal(w.ledger.recent_runs(),[],change)
            require_equal(await w.store.list_pending(),[],change)


async def t_turn_and_argument_binding_cannot_be_reused_after_authority_await():
    async with world() as w:
        begin(w)
        start=await w.gate.authorize_task_start('agent_task_research',TASK)
        other=await w.gate.authorize_task_start('agent_task_research',dict(TASK,objective='Untersuche ein anderes konkretes Thema.'))
        require(start.request_id!=other.request_id)
        require(await start.dispatch_current())
        require_equal(await w.gate.authorize_task_start('note_write',{'text':'no'}),None)
        begin(w,turn='turn-two')
        require(not await start.dispatch_current())
        original=V.browser_generation
        async def next_turn(*args):
            value=await original(*args)
            begin(w,turn='turn-three')
            return value
        with patch.object(V,'browser_generation',next_turn):
            require_equal(await w.gate.authorize_task_start('agent_task_research',TASK),None)
        begin(w,text='Was ist aus meinem alten Auftrag geworden?')
        require_equal(await w.gate.authorize_task_start('agent_task_research',TASK),None)


async def t_logout_during_router_await_is_rechecked_before_synchronous_task_commit():
    async with world() as w:
        begin(w)
        original=w.router._run
        async def revoked_before_handler(*args,**kwargs):
            await w.sessions.revoke(w.actor.session_id,principal=w.actor.principal)
            return await original(*args,**kwargs)
        with patch.object(w.router,'_run',revoked_before_handler):
            result=await tool(w).run(TASK)
        require(not result.success)
        require_equal(w.ledger.recent_runs(),[])
        require_equal(await w.store.list_pending(),[])


async def t_cognitive_dispatch_rejects_revoked_voice_without_requesting_approval():
    from solvio.cognition.router import CognitiveRouter
    from solvio.cognition.types import RoutingDecision,TaskAssessment,Route
    from solvio.cognition.continuity import ContinuityView
    from solvio.cognition.ledger import CognitionLedger
    async with world() as w:
        begin(w)
        dispatcher=SimpleNamespace(capabilities=w.router,capability_gate=w.gate,agent_runtime=w.orch)
        router=CognitiveRouter(dispatcher,mode='active',ledger=CognitionLedger(str(Path(E.S.state_dir())/'cognition.db')))
        await w.sessions.revoke(w.actor.session_id,principal=w.actor.principal)
        assessment=TaskAssessment(route=Route.AGENT_RESEARCH,objective=TASK['objective'])
        result=await router._dispatch(RoutingDecision('rd-browser-test',time.time()),time.monotonic(),
            assessment,ContinuityView(conversation_ref='c-browser-test'),w.gate.context())
        require(not result.ok)
        require_equal(result.error,'refused_policy')
        require_equal(w.ledger.recent_runs(),[])
        require_equal(await w.store.list_pending(),[])


async def t_public_websocket_proof_core_tool_loop_and_real_task_grant():
    import test_browser_voice_endpoint as H
    from solvio.agent_runtime.orchestrator import Orchestrator
    from solvio.capabilities.agent import AgentCapabilities,SPECS
    from solvio.capabilities.router import CapabilityRouter
    with tempfile.TemporaryDirectory(prefix='solvio-browser-task-door-') as folder, \
            patch.dict(os.environ,{'SOLVIO_STATE_DIR':folder}):
        async with H.world() as w:
            ledger=E.S.AgentRunLedger(str(Path(folder)/'agent.sqlite3'))
            router=CapabilityRouter()
            orch=Orchestrator(ledger=ledger,router=router,require_task_authority=True)
            router.register(SPECS['agent_task_research'],AgentCapabilities(orch).research)
            gate=CapabilityInvocationGate()
            task_tool=AgentCapabilityTool('agent_task_research',router,gate,ledger)
            results=[]
            async def dispatch(name,args):
                require_equal(name,'agent_task_research')
                result=await task_tool.run(args)
                results.append(result)
                return {'success':result.success,'data':result.data,'error':result.error}
            w.server.dispatcher=SimpleNamespace(capability_gate=gate,
                parse_args=json.loads,dispatch=dispatch,openai_tools=lambda:[task_tool.schema()])
            ws,auth=await H.started(w)
            sess=w.sessions[0]
            require(await sess.browser_task_session.current())
            require_equal(sess.session_id,auth['session_id'])
            # Only speech/model output is synthetic. Session identity, origin,
            # invocation context, tool adapter, Router and task books are real.
            sess.turn_user_text=TASK['objective']
            sess.turn_text_ready.set()
            await sess._handle_tool_calls([
                {'name':'agent_task_research','call_id':f'call-{n}',
                 'arguments':json.dumps(TASK)} for n in range(2)],'spoken-turn-one')
            require_equal(len(results),2)
            require(all(result.success for result in results))
            require_equal(results[0].data['task_id'],results[1].data['task_id'])
            require_equal(len(ledger.recent_runs()),1)
            grant=orch.task_authority.for_run(results[0].data['run_id'])
            task=ledger.get_task(grant.task_id)
            require_equal((task.created_principal,task.created_origin),('local-owner','trusted_dashboard'))
            require_equal(grant.receipt_method,'dashboard_session')
            require(grant.receipt_reference.startswith('browser-voice:'))
            require_equal(await w.store.list_pending(),[])
            await ws.send_json({'type':'session_end'})
            require_equal((await H.receive(ws,'session_closed'))['provider'],'closed_confirmed')
            await H.closed(ws)
            require_equal(sess.browser_task_session,None)
            require(orch.task_authority.active(grant.reference,task_id=task.task_id,run_id=grant.run_id).allowed)
            require_equal(len(w.providers),1)
            require(w.providers[0].closed)


async def t_browser_end_before_core_begin_turn_is_not_a_new_phone_request():
    from test_m0_realtime_latency import _openable_session
    from test_browser_voice_endpoint import Provider
    for removed in (False,True):
        async with world() as w:
            sess=_openable_session()
            sess.session_id=w.voice.session_id
            sess.satellite_id=w.actor.principal
            sess.channel='voice_browser'
            w.voice.closed=True
            sess.browser_task_session=None if removed else w.proof
            sess.turn_user_text=TASK['objective']
            sess.turn_text_ready.set()
            sess.oa=Provider()
            results=[]
            async def dispatch(name,args):
                result=await tool(w).run(args)
                results.append(result)
                return {'success':result.success,'error':result.error}
            sess.server.dispatcher=SimpleNamespace(capability_gate=w.gate,
                parse_args=json.loads,dispatch=dispatch)
            with patch.object(w.router,'execute',wraps=w.router.execute) as execute:
                await sess._handle_tool_calls([{'name':'agent_task_research',
                    'call_id':'stale-model-call','arguments':json.dumps(TASK)}],'late-turn')
            require_equal(execute.await_count,0)
            require_equal(results[0].error,'no_trusted_context')
            require_equal(w.ledger.recent_runs(),[])
            require_equal(await w.store.list_pending(),[])


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
