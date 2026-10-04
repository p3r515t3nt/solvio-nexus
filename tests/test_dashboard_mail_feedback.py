"""A dashboard mail request must not become an unfulfillable native job."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_chat_mail import world, TEXT, CALL
from test_conversation_processing import assessment
from solvio.conversation import mail
from solvio.capabilities.agent import AgentCapabilities, SPECS


async def t_dashboard_mail_explains_the_app_path_without_job_draft_or_approval():
    for call,text in [(CALL,TEXT),
        ({'name':'mail_forward','arguments':{'query':'from:elevenlabs receipt','to':'empfang@example.test'}},
         'Leite die letzte Rechnung von ElevenLabs an empfang@example.test weiter.'),
        ({'name':'mail_reply','arguments':{'query':'from:elevenlabs receipt','body':'Vielen Dank.'}},
         'Antworte auf die letzte Rechnung von ElevenLabs mit Vielen Dank.')]:
        async with world() as w:
            w.router.register(SPECS["agent_task_task"],AgentCapabilities(w.orch).task)
            cid=await w.chat()
            w.selection={'calls':[call],'clarification':''}
            w.cli.assessments.append(assessment('auftrag_recherche',text,auftragsprofil='persoenlich'))
            row=await w.ask(cid,text)
            require_equal(row['status'],'completed',row['error_code'])
            require_equal(w.tasks(),0,'unsupported dashboard mail must not become a native task')
            require_equal(w.select_count,1)
            require_equal(w.api.drafts,{})
            require_equal(w.api.sent,[])
            require_equal(w.ledger.waiting_starts(),[])
            require('SOLVIO-App' in w.chat_store.messages(cid)[-1]['text'])
            require_equal(json.loads(row['dispatch'])['action_class'],'rueckfrage')
            require(not json.loads(row['dispatch']).get('mail_call'))
            require_equal(len(w.invocations(row['activity_id'])),2)
            again=await w.ask(cid,text)  # same durable message ID
            require_equal(again['delivery_id'],row['delivery_id'])
            require_equal(w.select_count,1,'replay must not ask a model again')


async def t_dashboard_private_read_still_uses_the_existing_personal_task():
    async with world() as w:
        w.router.register(SPECS["agent_task_task"],AgentCapabilities(w.orch).task)
        cid=await w.chat();text='Prüfe meine Termine für morgen und fasse sie zusammen.'
        w.selection={'calls':[{'name':'private_task','arguments':{}}],'clarification':''}
        w.cli.assessments.append(assessment('auftrag_recherche',text,auftragsprofil='persoenlich'))
        row=await w.ask(cid,text)
        require_equal(row['status'],'completed',row['error_code'])
        require_equal(w.tasks(),1)
        require_equal(w.api.drafts,{})
        require_equal(w.api.sent,[])
        require_equal(w.ledger.waiting_starts(),[])


async def t_mail_execution_never_relabels_a_dashboard_delivery_as_an_app():
    processor=SimpleNamespace(_complete=AsyncMock(),current=AsyncMock(return_value=True),worker_generation='test-worker')
    written=[]
    runtime=SimpleNamespace(store=SimpleNamespace(record_dispatch=lambda *a:written.append(a)),
        orchestrator=SimpleNamespace(router=None,ledger=None))
    row={'source_kind':'dashboard','conversation_id':'test-chat','delivery_id':'test-delivery','principal':'local-owner'}
    with patch.object(mail,'MailActionTool',side_effect=AssertionError('app-only tool reached')):
        await mail.execute(processor,row,
            {'mail_call':CALL,'objective':TEXT},runtime, 'activity-test')
    require_equal(written,[])
    require_equal(processor._complete.await_count,1)
    require('SOLVIO-App' in processor._complete.await_args.kwargs['assistant_text'])


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
