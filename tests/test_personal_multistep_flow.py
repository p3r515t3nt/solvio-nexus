"""Synthetic chat/voice -> real private worker bridge -> file -> same-task revision.

Only model answers and Gmail are fixtures. No production mailbox, provider or
real sending. This proves transport and binding, not model writing quality.
"""
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_native_task_entry as N
import test_conversation_processing as T
import test_native_mail_calendar_tools as M
import test_browser_voice_task_authority as B
from test_agent_cost_runtime import _cli
from solvio.conversation import mail, endpoint as CE
from solvio.capabilities import gmail as G
from solvio.capabilities.invocation import CapabilityInvocationGate
from solvio import browser_voice_session as V
from solvio.security.mobile_approval import browser_sessions as BS
from solvio.tools.dispatcher import ToolDispatcher
from solvio.tools.personal_task import PersonalTaskTool
from solvio.agent_runtime import store as S, task_revisions as TR, cost_dispatch as D, result_files as RF
from solvio.specialists.result import SpecialistResult

GOAL = 'Lies die letzte Rechnung und formuliere eine freundliche Rückfrage zur Zahlungsfrist als Textdatei. Sende nichts.'
CRITERIA = {'anforderungen': {'auskunft':[{'id':'a1','text':'Formuliere die Rückfrage anhand der gelesenen Rechnung.'}],
    'handlungen':[{'id':'h1','text':'Speichere den Antwortentwurf als Textdatei.','effect':'file'}],
    'unklar':[], 'belege':{'mindestens':0}}}


@asynccontextmanager
async def world():
    with patch.object(N.E, 'World', T.ProcWorld):
        async with N.world(objective=GOAL, criteria=CRITERIA) as w:
            w.orch.conversations=w.chat_store
            w.provider=M.RecordingGmail([M.mail('m1','Rechnung September','Betrag 120 Euro. Zahlungsfrist bitte abstimmen.')])
            G.register(w.router,G.GmailCapabilities(w.provider))
            executable=_cli(w.folder,w.ledger.path,plans=[CRITERIA],assessments=[N.VERDICT])
            script=executable.read_text()
            marker='    text = reply if isinstance(reply, str) else json.dumps(reply)'
            script=script.replace(marker,'''    if kind == 'assessment':
        effects=request.get('gepruefte_handlungsbelege',{}).get('eintraege',[])
        snapshot=json.loads(request['ergebnis'])
        actual=next((snapshot[e['feld']][e['index']] for e in effects if e['id']=='h1'),'')
        answered='120 Euro' in request['ergebnis'] and 'Zahlungsfrist' in request['ergebnis'] and bool(actual)
        reply={'beantwortet':[{'id':'a1','belege':[actual]},{'id':'h1','belege':[actual]}] if answered else [],
               'offen':[] if answered else ['a1','h1'],'fehlend':[] if answered else ['Mailbezug oder Datei fehlt'],
               'unsicher':[],'weiterarbeit_noetig':not answered}
''' + marker)
            executable.write_text(script)
            w.native_calls=[]
            async def native(request, *, config, continuation, bridge, on_event=None):
                session=continuation.sessions.session(continuation.session_id)
                revision=TR.revision_for_run(w.ledger,request.run_id)['revision']
                w.native_calls.append((request,session))
                receipts=[]; thread='private-native-thread';turn_id='turn-'+str(revision)
                async def perform(*_):
                    claim=continuation.sessions.active_claim(request.run_id)
                    turn,fresh=continuation.sessions.request_turn(session_id=session.session_id,
                        run_id=request.run_id,revision=revision,invocation_id=claim['invocation_id'])
                    require(fresh)
                    continuation.sessions.bind_thread(turn.invocation_id,thread)
                    continuation.sessions.started(turn.invocation_id,native_thread_id=thread,native_turn_id=turn_id)
                    names={t['name'] for t in bridge.tools}
                    require({'gmail_search','gmail_read_message'} <= names)
                    require(not {'gmail_create_draft','gmail_send_draft','calendar_create_event'} & names)
                    from solvio.specialists.native_task_profile import web_search_mode
                    require_equal(web_search_mode(names),'disabled')
                    async def read(name,args,call):
                        response=await bridge.adapter.call({'threadId':thread,'turnId':turn_id,'callId':call,
                            'tool':name,'arguments':args})
                        require(response['success'],response)
                        receipts.append({'kind':'dynamicToolCall','item_id':call,'status':'completed','tool':name,'success':True})
                        return json.loads(response['contentItems'][0]['text'])['data']
                    target=Path(session.workspace)/'antwort.txt'
                    if revision==1:
                        found=await read('gmail_search',{'query':'rechnung','limit':3},'find-mail')
                        source=await read('gmail_read_message',{'message_id':found['messages'][0]['id']},'read-mail')
                        require('120 Euro' in json.dumps(source))
                        text='Vielen Dank für die Rechnung über 120 Euro. Welche Zahlungsfrist ist vorgesehen?'
                    else:
                        require('kürzer' in request.context or 'kürzer' in request.objective)
                        require('120 Euro' in target.read_text(),'previous native result was lost')
                        text='Welche Zahlungsfrist gilt für die 120 Euro?'
                    target.write_text(text)
                    w.draft=text
                    continuation.sessions.terminal(turn.invocation_id,native_thread_id=thread,native_turn_id=turn_id,status='completed')
                    return N.L.Outcome(True,exit_code=0,process_started=True)
                result=await D.dispatch('codex',N.L.Invocation('/synthetic/native',(),timeout=1),request.objective,perform)
                require(result.dispatch_started,result.outcome.reason)
                return N.SP.SpecialistRun(result=SpecialistResult(role='worker',provider='codex',question=request.objective,
                    ok=True,findings=[w.draft],evidence=[],recommended_path='Antwortentwurf als lokale Datei gespeichert; nichts versendet.'),
                    provider='codex',billing_mode='subscription',dispatch_started=True,cost_status=result.cost_status,
                    cost_invocation_id=result.invocation_id,cost_reservation_id=result.reservation_id,
                    native_thread_id=thread,native_turn_id=turn_id,native_files=('antwort.txt',),
                    native_file_requirements=(('antwort.txt','h1'),),native_tool_receipts=tuple(receipts))
            with patch.object(N.NT,'run_task',side_effect=native):
                try:yield w
                finally:await w.processor.shutdown()


async def start(w, voice):
    cid=await w.chat()
    if voice:
        cookie=w.client.session.cookie_jar.filter_cookies(w.server.make_url('/'))[BS.COOKIE_NAME].value
        actor=await w.sessions.authenticate(cookie,csrf_token=w.headers[BS.CSRF_HEADER])
        live=SimpleNamespace(closed=False)
        proof=await V.verified_browser_task_session(actor=actor,service=w.sessions,session_id='private-voice',
            session_nonce='N'*43,alive=lambda:not live.closed)
        gate=CapabilityInvocationGate()
        gate.begin_turn(session_id='private-voice',turn_id='one',principal=actor.principal,trust=B.voice_trust(True),
            user_text=GOAL,origin=B.OriginClass.TRUSTED_DASHBOARD,browser_task_session=proof,conversation_id=cid)
        dispatcher=ToolDispatcher();dispatcher.agent_runtime=w.orch;dispatcher.capability_gate=gate;dispatcher.capabilities=w.router
        dispatcher.register(PersonalTaskTool(dispatcher))
        result=await dispatcher.dispatch('personal_task',{})
        require(result['success'],result)
        live.closed=True;gate.clear()
        return cid,result['data']
    w.cli.assessments.append(T.assessment('auftrag_recherche',GOAL,auftragsprofil='persoenlich'))
    def reply(kind,record):
        if mail.MARKER in record['prompt']:
            return N.L.Outcome(True,text=T.codex_lines(json.dumps({'calls':[{'name':'private_task','arguments':{}}],'clarification':''})),exit_code=0,process_started=True)
    w.cli.override=reply
    with patch.object(T.U,'text_invocation',return_value=N.L.Invocation(sys.executable,('-c','pass'),cwd=str(w.folder),timeout=10)), \
            patch.object(T.P,'codex_status',AsyncMock(return_value=T.AUTH)):
        row=await w.ask(cid,GOAL)
    require_equal(row['status'],'completed',row['error_code'])
    return cid,row


async def flow(voice):
    async with world() as w:
        cid,accepted=await start(w,voice)
        original=await N.drive(w,accepted['run_id'])
        require_equal(original.state,S.SUCCEEDED,original.failure_category+': '+original.result_summary)
        require_equal([c[0] for c in w.provider.calls],['search','message'])
        first=RF.describe_files(w.ledger,original.run_id)[0][0]
        oldbytes=RF.read_result(w.ledger,original.run_id,first['id'])[1]
        require('120 Euro' in oldbytes.decode())
        response=await w.client.get(f'{CE.PREFIX}/{cid}')
        view=await response.json();require_equal(response.status,200)
        require(any(r['id']==original.run_id for r in view['auftraege']),view)
        followup='Bitte kürzer formulieren, weiterhin als Textdatei. Nichts versenden.'
        w.cli.assessments.append(T.assessment('auftrag_recherche',followup,
            fortsetzung_von=accepted['task_id'],auftragsprofil='persoenlich'))
        with patch.object(T.U,'text_invocation',return_value=N.L.Invocation(sys.executable,('-c','pass'),cwd=str(w.folder),timeout=10)), \
                patch.object(T.P,'codex_status',AsyncMock(return_value=T.AUTH)):
            revised=await w.ask(cid,followup,client_message_id='shorten-draft-002')
        require_equal(revised['status'],'completed',revised['error_code'])
        require_equal((revised['task_id'],revised['revision']),(accepted['task_id'],2))
        run=await N.drive(w,revised['run_id'])
        require_equal(run.state,S.SUCCEEDED,run.failure_category+': '+run.result_summary)
        new=RF.describe_files(w.ledger,run.run_id)[0][0]
        newbytes=RF.read_result(w.ledger,run.run_id,new['id'])[1]
        require(newbytes!=oldbytes and len(newbytes)<len(oldbytes))
        require_equal(RF.read_result(w.ledger,original.run_id,first['id'])[1],oldbytes)
        require_equal(len({s.session_id for _,s in w.native_calls}),1)
        require_equal([c[0] for c in w.provider.calls],['search','message'],'revision reread unrelated mailbox')
        require_equal(await w.store.list_pending(),[],'a text draft is not a send approval')
        require_equal(w.tasks(),1)


async def t_typed_private_mail_to_draft_file_then_revision_keeps_original():
    await flow(False)


async def t_voice_private_mail_to_draft_file_continues_after_voice_close():
    await flow(True)


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
