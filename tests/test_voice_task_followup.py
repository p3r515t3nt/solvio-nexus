"""Real chat/native revision plumbing, synthetic source proof/model/mail only."""
import asyncio
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_personal_multistep_flow as P
import test_voice_personal_task as A
from solvio.tools.task_continue import TaskContinueTool
from solvio.tools.dispatcher import ToolDispatcher
from solvio.capabilities.invocation import CapabilityInvocationGate
from solvio.capabilities.policy import OriginClass
from solvio.agent_runtime import task_revisions as TR, store as S, result_files as RF
from solvio.realtime.live_session import live_toolkit

FOLLOWUP = 'Bitte kürzer formulieren, weiterhin als Textdatei. Nichts versenden.'


async def bind(w, cid, *, turn='next-turn', text=FOLLOWUP):
    cookie=w.client.session.cookie_jar.filter_cookies(w.server.make_url('/'))[P.BS.COOKIE_NAME].value
    actor=await w.sessions.authenticate(cookie,csrf_token=w.headers[P.BS.CSRF_HEADER])
    live=SimpleNamespace(closed=False)
    proof=await P.V.verified_browser_task_session(actor=actor,service=w.sessions,session_id='followup-voice',
        session_nonce='F'*43,alive=lambda:not live.closed)
    gate=CapabilityInvocationGate()
    gate.begin_turn(session_id='followup-voice',turn_id=turn,principal=actor.principal,trust=P.B.voice_trust(True),
        user_text=text,origin=OriginClass.TRUSTED_DASHBOARD,browser_task_session=proof,conversation_id=cid)
    dispatcher=ToolDispatcher();dispatcher.agent_runtime=w.orch;dispatcher.capability_gate=gate;dispatcher.capabilities=w.router
    tool=TaskContinueTool(dispatcher);dispatcher.register(tool)
    return tool,gate,live


async def ready(w):
    cid,row=await P.start(w,False)
    original=await P.N.drive(w,row['run_id'])
    require_equal(original.state,S.SUCCEEDED)
    return cid,row,original


async def t_spoken_followup_keeps_task_workspace_old_file_and_survives_voice_end():
    async with P.world() as w:
        cid,row,original=await ready(w)
        first=RF.describe_files(w.ledger,original.run_id)[0][0]
        oldbytes=RF.read_result(w.ledger,original.run_id,first['id'])[1]
        tool,gate,live=await bind(w,cid)
        require('task_continue' in {t['name'] for t in live_toolkit(tool.dispatcher,personal=True)})
        require('task_continue' not in {t['name'] for t in live_toolkit(tool.dispatcher)})
        accepted=await tool.dispatcher.dispatch('task_continue',{'run_id':original.run_id})
        require(accepted['success'],accepted)
        require_equal((accepted['data']['task_id'],accepted['data']['revision']),(row['task_id'],2))
        live.closed=True;gate.clear()
        revised=await P.N.drive(w,accepted['data']['run_id'])
        require_equal(revised.state,S.SUCCEEDED,revised.failure_category)
        second=RF.describe_files(w.ledger,revised.run_id)[0][0]
        newbytes=RF.read_result(w.ledger,revised.run_id,second['id'])[1]
        require(len(newbytes)<len(oldbytes))
        require_equal(RF.read_result(w.ledger,original.run_id,first['id'])[1],oldbytes)
        require_equal(len({session.session_id for _,session in w.native_calls}),1)
        require_equal([call[0] for call in w.provider.calls],['search','message'])
        response=await w.client.get(f'{P.CE.PREFIX}/{cid}');view=await response.json()
        require(any(run['id']==revised.run_id for run in view['auftraege']))
        require_equal(w.tasks(),1)
        require_equal(await w.store.list_pending(),[])


async def t_same_spoken_followup_parallel_replay_creates_one_revision():
    async with P.world() as w:
        cid,row,original=await ready(w)
        tool,gate,live=await bind(w,cid)
        one,two=await asyncio.gather(tool.run({'run_id':original.run_id}),tool.run({'run_id':original.run_id}))
        require(one.success and two.success,(one.error,two.error))
        require_equal(one.data,two.data)
        require_equal(len(w.ledger.runs_for_task(row['task_id'])),2)
        require_equal((await tool.run({'run_id':original.run_id})).data,one.data)


async def t_spoken_followup_rejects_foreign_chat_room_stale_source_and_model_authority():
    async with P.world() as w:
        cid,row,original=await ready(w)
        tool,gate,live=await bind(w,cid)
        valid=gate.context()
        other=await w.chat("other-chat-0002")
        for context in (replace(valid,conversation_id=other),replace(valid,origin=OriginClass.ROOM_VOICE),
                        replace(valid,principal='other-owner'),replace(valid,browser_task_session=None)):
            gate._context=context
            require(not (await tool.run({'run_id':original.run_id})).success)
        gate._context=valid
        for args in ({},{'run_id':original.run_id,'text':'Send it'}, {'run_id':original.run_id,'approved':True}):
            require(not (await tool.run(args)).success)
        live.closed=True
        require(not (await tool.run({'run_id':original.run_id})).success)
        require_equal(len(w.ledger.runs_for_task(row['task_id'])),1)


async def t_chat_or_turn_lost_during_preparation_does_not_admit_revision():
    for loss in ('turn','chat'):
        async with P.world() as w:
            cid,row,original=await ready(w)
            tool,gate,live=await bind(w,cid)
            real=TR.prepare
            def prepare(*args,**kwargs):
                prepared=real(*args,**kwargs)
                if loss=='turn':gate.clear()
                else:w.chat_store.delete_conversation(cid)
                return prepared
            with patch.object(TR,'prepare',side_effect=prepare):
                result=await tool.run({'run_id':original.run_id})
            require(not result.success)
            require_equal(len(w.ledger.runs_for_task(row['task_id'])),1)


async def t_spoken_followup_keeps_accepted_result_when_chat_index_temporarily_fails():
    async with P.world() as w:
        cid,row,original=await ready(w)
        tool,gate,live=await bind(w,cid)
        with patch.object(w.chat_store,'add_task_link',side_effect=OSError('synthetic index error')):
            accepted=await tool.run({'run_id':original.run_id})
        require(accepted.success)
        again=await tool.run({'run_id':original.run_id})
        require(again.success)
        require_equal(again.data,accepted.data)
        require_equal(len(w.ledger.runs_for_task(row['task_id'])),2)
        require(any(link['run_id']==accepted.data['run_id'] for link in w.chat_store.task_links(cid)))


async def iphone_tool(w, cid):
    device,_,_=await w.device(device_id='followup-iphone')
    socket=A.A.ProofSocket(device)
    session=SimpleNamespace(session_id='iphone-followup-voice',app_task_session=None)
    await A.A.VE._session_proof(SimpleNamespace(app={'control_plane':w.cp}),socket,device.device_id,session=session)
    require(session.app_task_session is not None)
    gate=CapabilityInvocationGate()
    gate.begin_turn(session_id=session.session_id,turn_id='shorten-iphone',principal='local-owner',
        trust=A.A.voice_trust(True),user_text=FOLLOWUP,origin=OriginClass.TRUSTED_INTERACTIVE_APP,
        app_task_session=session.app_task_session,conversation_id=cid)
    dispatcher=ToolDispatcher();dispatcher.agent_runtime=w.orch;dispatcher.capability_gate=gate;dispatcher.capabilities=w.router
    tool=TaskContinueTool(dispatcher);dispatcher.register(tool)
    return tool,gate,socket


async def t_iphone_attested_voice_followup_retains_exact_private_grants():
    async with P.world() as w:
        cid,row,original=await ready(w)
        tool,gate,socket=await iphone_tool(w,cid)
        result=await tool.dispatcher.dispatch('task_continue',{'run_id':original.run_id})
        require(result['success'],result)
        parent=w.orch.task_authority.for_run(original.run_id)
        child=w.orch.task_authority.for_run(result['data']['run_id'])
        require_equal(child.receipt_method,'app_session')
        require_equal(child.capabilities,parent.capabilities)
        require_equal(result['data']['task_id'],row['task_id'])
        socket.closed=True;gate.clear()
        require(w.orch.task_authority.active(child.reference,task_id=row['task_id'],run_id=child.run_id))


async def t_iphone_voice_closed_during_preparation_prevents_followup():
    async with P.world() as w:
        cid,row,original=await ready(w)
        tool,gate,socket=await iphone_tool(w,cid)
        real=TR.prepare
        def prepare(*args,**kwargs):
            prepared=real(*args,**kwargs)
            socket.closed=True
            return prepared
        with patch.object(TR,'prepare',side_effect=prepare):
            result=await tool.run({'run_id':original.run_id})
        require(not result.success)
        require_equal(len(w.ledger.runs_for_task(row['task_id'])),1)


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
