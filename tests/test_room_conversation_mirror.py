"""Room transcripts share the canonical Owner view, never Owner speaker authority."""
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.conversation.store import ConversationStore, ConversationStoreError, ROLE_USER

@contextmanager
def world():
    with tempfile.TemporaryDirectory() as folder:
        store=ConversationStore(str(Path(folder)/'conversations.sqlite3'),now_fn=lambda:1000.0).open()
        try: yield store
        finally: store.close()


def room(store,sid,device='synthetic-pi',owner='alice'):
    return store.begin_room_session(sid,device_id=device,owner_principal=owner)


def t_room_chat_appears_only_for_owner_and_resumes_its_own_speech():
    with world() as store:
        cid,resumed,context=room(store,'room-1')
        require(not resumed);require_equal(context,[])
        store.add_message(cid,ROLE_USER,'Synthetisches Raumgespräch',source_session_id='room-1')
        require_equal([r['conversation_id'] for r in store.list_conversations('alice')],[cid])
        require_equal(store.list_conversations('bob'),[])
        same,resumed,context=room(store,'room-2')
        require_equal(same,cid);require(resumed);require_equal(context[0]['text'],'Synthetisches Raumgespräch')


def t_legacy_private_other_owner_and_other_device_never_enter_room_context():
    with world() as store:
        legacy,_=store.begin_session('legacy')
        store.add_message(legacy,ROLE_USER,'Synthetischer alter unzugeordneter Text')
        private,_=store.create_conversation(owner_principal='alice',client_request_id='private')
        store.add_message(private['conversation_id'],ROLE_USER,'Synthetischer privater Text')
        a,_,_=room(store,'room-a')
        store.add_message(a,ROLE_USER,'Raum A',source_session_id='room-a')
        b,_,context=room(store,'room-b',device='other-pi')
        require(b!=a);require_equal(context,[])
        other,_,context=room(store,'room-c',owner='bob')
        require(other not in (a,b));require_equal(context,[])
        require(not store.conversation_owned(legacy,'alice'))


def expect_read_only(call):
    try: call()
    except ConversationStoreError as exc:
        require(str(exc) in {"room_conversation_read_only", "conversation_not_bindable"})
    else: raise AssertionError("private write entered room conversation")


def t_private_voice_text_and_task_writes_cannot_enter_a_live_room_chat():
    with world() as store:
        cid,_,_=room(store,'room-1')
        store.add_message(cid,ROLE_USER,'Raumtext',source_session_id='room-1')
        require(store.conversation_owned(cid,'alice'))
        require(not store.conversation_owned(cid,'alice',for_write=True))
        expect_read_only(lambda:store.begin_session('owner-phone',conversation_id=cid,principal='alice'))
        expect_read_only(lambda:store.add_message(cid,ROLE_USER,'Privat',source_session_id='owner-phone'))
        expect_read_only(lambda:store.add_message(cid,ROLE_USER,'Privat ohne Sitzung'))
        expect_read_only(lambda:store.accept_delivery(conversation_id=cid,principal='alice',
            client_message_id='synthetic-message',text='Privat',digest='a'*64,source_kind='dashboard',
            source_ref='synthetic-session',source_generation='1',core_id='synthetic-core'))
        expect_read_only(lambda:store.add_task_link(cid,'private-task','private-run',source='task:private'))
        require_equal([m['text'] for m in store.messages(cid)],['Raumtext'])
        require_equal(store.task_links(cid),[])
        # The same proved room device can still continue its own work.
        store.add_task_link(cid,'room-task','room-run',source='voice:room-1')
        same,resumed,context=room(store,'room-2')
        require_equal(same,cid);require(resumed);require_equal(context[0]['text'],'Raumtext')


def t_room_transcripts_keep_existing_retention_private_chats_do_not_expire():
    with world() as store:
        cid,_,_=room(store,'room-1')
        store.add_message(cid,ROLE_USER,'Raumtext',source_session_id='room-1')
        private,_=store.create_conversation(owner_principal='alice',client_request_id='private')
        store.add_message(private['conversation_id'],ROLE_USER,'Privater Chat')
        result=store.purge_expired(now=1000+91*86400)
        require_equal(result['conversations'],1)
        require_equal(store.conversation(cid),None)
        require_equal(len(store.messages(private['conversation_id'])),1)


async def t_iphone_and_dashboard_can_read_room_but_private_message_is_refused_before_work():
    import test_conversation_endpoint as T
    async with T.world() as w:
        cid,_,_=room(w.chat_store,'room-active',owner='local-owner')
        w.chat_store.add_message(cid,ROLE_USER,'Synthetischer Raumtext',source_session_id='room-active')
        for response in (await w.client.get(T.CE.PREFIX),await w.client.get(T.CE.PREFIX+'/'+cid)):
            require_equal(response.status,200)
            data=await response.json()
            row=data['conversation'] if 'conversation' in data else data['conversations'][0]
            require_equal(row['read_only'],True)
        response=await w.send(cid,'Privater Browsertext')
        require_equal(response.status,409)
        ctx,client,headers=await w.device()
        response=await client.get(T.CE.PREFIX+'/'+cid,headers=headers)
        require_equal(response.status,200)
        require_equal((await response.json())['conversation']['read_only'],True)
        message={'conversation_id':cid,'client_message_id':'room-private','text':'Privater iPhone-Text'}
        payload=await w.app_proof(ctx,client,headers,message)
        response=await client.post(T.CE.PREFIX+'/'+cid+'/messages',json=payload,headers=headers)
        require_equal(response.status,409)
        require_equal(w.wakes,[])
        require_equal([m['text'] for m in w.chat_store.messages(cid)],['Synthetischer Raumtext'])
        # All later cognition readers receive only the existing public room data.
        from solvio.cognition import continuity as C
        view=await C.build(SimpleNamespace(conversations=w.chat_store),
                          SimpleNamespace(recent=lambda *a,**kw:[]),conversation_ref=cid)
        require('Raumtext' in view.recent_context)
        require('Privater' not in view.recent_context)
        require_equal(view.entries,[])


def t_room_visibility_does_not_promote_session_to_owner_principal():
    from solvio.realtime.core_server import Session
    with world() as store:
        session=object.__new__(Session)
        session.session_id='room-1';session.satellite_id='synthetic-pi'
        session.authenticated_room_device='synthetic-pi'
        session.server=SimpleNamespace(room_history_owner='alice')
        require_equal(session.conversation_principal(),'')
        cid,_,_=session._open_conversation_context(store,'','')
        require(store.conversation_owned(cid,'alice'))
        require_equal(session.conversation_principal(),'')
        require_equal(session.satellite_id,'synthetic-pi')
        # A claimed satellite label alone cannot activate the new history path.
        session.authenticated_room_device=''
        session.session_id='unproven'
        other,_,_=session._open_conversation_context(store,'','')
        require(not store.conversation_owned(other,'alice'))

async def t_later_private_task_revision_never_enters_the_running_room_register():
    from solvio.cognition import continuity as C
    with world() as store:
        cid,_,_=room(store,'room-active')
        task='at-0123456789abcdef'
        store.add_message(cid,ROLE_USER,'Raumtext',source_session_id='room-active')
        store.add_task_link(cid,task,'ar-room',source='voice:room-active')
        room_run=SimpleNamespace(task_id=task,state='SUCCEEDED',result_summary='Raumergebnis',finished_at=1)
        private_run=SimpleNamespace(task_id=task,state='SUCCEEDED',result_summary='PRIVATE_FOLLOWUP_RESULT',finished_at=2)
        seen=[]
        def latest(_):
            seen.append('latest');return [room_run,private_run]
        def exact(run_id):
            seen.append(run_id);return room_run if run_id=='ar-room' else private_run
        dispatcher=SimpleNamespace(conversations=store,
            agent_runtime=SimpleNamespace(ledger=SimpleNamespace(runs_for_task=latest,get_run=exact)))
        ledger=SimpleNamespace(recent=lambda *a,**kw:[{'produced_ref':task,'route_final':'auftrag_text'}])
        view=await C.build(dispatcher,ledger,conversation_ref=cid)
        require_equal([entry.summary for entry in view.entries],['Raumergebnis'])
        require_equal(seen,['ar-room'])
        require('PRIVATE' not in view.register_block())
        store.delete_conversation(cid)
        seen.clear()
        deleted=await C.build(dispatcher,ledger,conversation_ref=cid)
        require_equal(seen,[])
        require('PRIVATE' not in deleted.register_block())


async def t_real_room_commission_after_refused_private_append_only_sees_room_context():
    import _cognition_fixtures as F
    with world() as store:
        F.redirect_state(str(Path(store.path).parent/'routing'))
        cid,_,_=room(store,'room-active')
        store.add_message(cid,ROLE_USER,'Raumtext zur Frage',source_session_id='room-active')
        expect_read_only(lambda:store.add_message(cid,ROLE_USER,'PRIVATE_PHONE_TEXT',source_session_id='phone'))
        text='Wie war die Frage im Raum?'
        transport=F.FakeTransport([F.text_reply(F.assessment_body(weg='kein_auftrag',ziel=text))])
        bag=F.build(transport=transport,with_agent=False,with_deep=False,with_doctor=False)
        context=F.turn(bag,text,conversation_ref=cid,channel='voice_satellite')
        bag.conversations=store
        await bag.cognition.commission(context)
        import json
        payload=json.dumps(transport.calls,ensure_ascii=False)
        require(bool(transport.calls))
        require('Raumtext' in payload)
        require('PRIVATE_PHONE_TEXT' not in payload)


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
