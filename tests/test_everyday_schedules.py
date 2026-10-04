"""Daily overview and reply follow-up without any real mailbox or messages."""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from _guard import enforce_assertions,require,require_equal
enforce_assertions()
from solvio.proactive.everyday import validate,execute
from solvio.proactive.runner import TaskRunner
from solvio.proactive import store as S
from solvio.proactive.schedule import daily,in_seconds
from solvio.capabilities.proactive import classify_create,describe_create,parse_when
from solvio.capabilities.policy import ActionClass
from solvio.integrations.gmail import message_from_api


def task(kind='tagesueberblick'):
    return S.Task(task_id='bt-test',owner='test-owner',title='Test',created_at=1,
        created_from='synthetic',schedule=daily(8).as_dict(),action={'kind':kind})


async def t_new_private_schedules_require_face_id_and_bind_mail_reference():
    for action in ('tagesueberblick','mail_antwort_pruefen'):
        classification=await classify_create({'aktion':action},router=None)
        require_equal(classification.action_class,ActionClass.VERY_CRITICAL)
    args={'titel':'Prüfen','wann':'2099-10-02 17:00','aktion':'mail_antwort_pruefen',
          'argumente':{'thread_id':'thread1','message_id':'sent1'}}
    shown=await describe_create(args,clock=lambda:100)
    require_equal(shown['argumente'],args['argumente'])
    require('pruefzeitpunkt' in shown)
    require_equal(await describe_create(args,clock=lambda:1000),shown)


def t_private_schedule_arguments_are_closed():
    from solvio.capabilities.contract import CapabilityDeclined
    for action,args,plan in [('tagesueberblick',{'argumente':{'send':True}},daily(8)),
                             ('mail_antwort_pruefen',{'argumente':{'thread_id':'a','message_id':'b'}},daily(8)),
                             ('tagesueberblick',{'bedingung':'filter'},daily(8))]:
        try: validate(action,args,plan)
        except CapabilityDeclined: pass
        else: raise AssertionError('unbound action accepted')


async def t_daily_overview_reports_missing_services_and_preserves_all_sections():
    async def read(name,args,**kwargs):
        require_equal(kwargs['principal'],'test-owner')
        if name=='calendar_list_events':
            return SimpleNamespace(succeeded=True,data={'events':[{'title':'Synthetic meeting','start':'09:00'}]})
        return SimpleNamespace(succeeded=False,data=None)
    runner=TaskRunner(SimpleNamespace(capabilities=SimpleNamespace(execute=read)),SimpleNamespace(is_running_version=AsyncMock(return_value=True)))
    first=await execute(runner,task(),{'kind':'tagesueberblick'},'run1')
    require_equal(first.state,S.DONE)
    require('unvollständig' in first.item['summary'])
    require(any('Abfrage nicht bestätigt' in x for x in first.item['findings']))
    require(any('Synthetic meeting' in x for x in first.item['findings']))
    second=await execute(runner,task(),{'kind':'tagesueberblick'},'run2')
    require(first.item['fingerprint'] != second.item['fingerprint'])


async def _follow(messages, success=True):
    reader=AsyncMock(return_value=SimpleNamespace(succeeded=success,data={'messages':messages}))
    runner=TaskRunner(SimpleNamespace(capabilities=SimpleNamespace(execute=reader)),SimpleNamespace(is_running_version=AsyncMock(return_value=True)))
    action={'kind':'mail_antwort_pruefen','arguments':{'thread_id':'t','message_id':'sent'}}
    return await execute(runner,task('mail_antwort_pruefen'),action,'run1'),reader

ORIGINAL={'id':'sent','sent':True,'draft':False,'received_at_ms':1000}


async def t_no_reply_report_only_after_successful_bound_thread_read():
    result,reader=await _follow([ORIGINAL])
    require_equal(result.state,S.DONE)
    require('keine spätere' in result.item['summary'])
    require_equal(reader.await_args.args[0],'gmail_read_thread')
    require_equal(reader.await_count,1)
    failed,_=await _follow([],success=False)
    require_equal(failed.state,S.FAILED);require('nicht bestätigt' in failed.item['summary'])


async def t_reply_suppresses_reminder_but_sent_messages_and_drafts_do_not():
    for newer,expected in [({'sent':False,'draft':False},S.NO_CHANGE),
                           ({'sent':True,'draft':False},S.DONE),
                           ({'sent':False,'draft':True},S.DONE)]:
        result,_=await _follow([ORIGINAL,{'id':'later','received_at_ms':2000,**newer}])
        require_equal(result.state,expected)
    for rows in [[],[dict(ORIGINAL,sent=False)],[dict(ORIGINAL,received_at_ms=0)]]:
        result,_=await _follow(rows);require_equal(result.state,S.FAILED)


def t_provider_metadata_not_spoofable_by_mail_date_header():
    message=message_from_api({'id':'sent','threadId':'t','internalDate':'12345','labelIds':['SENT'],
        'payload':{'headers':[{'name':'Date','value':'Fri, 1 Jan 2099 00:00:00 +0000'}]}})
    data=message.as_data();require_equal(data['received_at_ms'],12345);require_equal(data['sent'],True)


async def t_native_schedule_real_approval_and_continuation_denial_final():
    import json
    import test_notiz_fortsetzung as fixture
    import test_memory_mutation_endpoint as mutation
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from solvio import everyday_endpoint as endpoint
    from solvio.capabilities.proactive import ProactiveCapabilities, register
    from solvio.security.mobile_approval import gateway
    from solvio.secret_vault.context import bound, UseContext
    from solvio.capabilities.policy import OriginClass
    w = await fixture._aufbau()
    with tempfile.TemporaryDirectory() as temp:
        store = S.ProactiveStore(os.path.join(temp,'p.sqlite3'))
        caps = ProactiveCapabilities(store)
        register(w['orch'].router, caps)
        device = await w['H'].enroll_attested(w['cp'], transport_cred='synthetic-everyday')
        # The helper uses one device ID; enrollment replaces its synthetic key.
        w['geraet'] = device
        app = gateway.build_app(control_plane=w['cp'], coordinator=w['co'])
        dispatcher = SimpleNamespace(capabilities=w['orch'].router,agent_runtime=w['orch'])
        endpoint.attach(app,SimpleNamespace(dispatcher=dispatcher))
        client=TestClient(TestServer(app)); await client.start_server()
        headers={'X-Device-Id':device.device_id,'X-Transport-Cred':'synthetic-everyday'}
        try:
            schedule={'titel':'Synthetic overview','wann':'täglich 08:00','aktion':'tagesueberblick','argumente':{}}
            response=await client.post(endpoint.API,json={});require_equal(response.status,401)
            requests=[]
            for approve in (True,False):
                ch=await (await client.get(endpoint.API+'/challenge',headers=headers)).json()
                args={'schedule_json':json.dumps(dict(schedule,titel='First' if approve else 'Denied'))}
                w['zaehler'][0] += 1
                assertion=mutation._assertion_b64(device,core_id=ch['core_instance_id'],nonce=ch['nonce'],capability='everyday_schedule',arguments=args,counter=w['zaehler'][0])
                payload={'capability':'everyday_schedule','arguments':args,'nonce':ch['nonce'],'assertion_b64':assertion}
                response=await client.post(endpoint.API,json=payload,headers=headers)
                require_equal(response.status,200)
                result=await response.json();require_equal(result['ok'],False)
                request_id=result['request_id'];require(request_id)
                requests.append(request_id)
                await w['orch']._poll_pending_starts()
                require_equal(len(await store.list_tasks()), 0 if approve else 1)
                if approve: decision = await fixture._freigeben(w,request_id)
                else: decision = await fixture._ablehnen(w,request_id)
                require_equal(decision, 'ok', 'decision failed')
                await w['orch']._poll_pending_starts(); await w['orch']._poll_pending_starts()
                record = await w['cp'].store.get_request(request_id)
                require_equal(len(await store.list_tasks()),1, 'approval row: ' + str({k:v for k,v in record.items() if k in ('state','failure_reason','last_error','result_json')}))
                response=await client.post(endpoint.API,json=payload,headers=headers);require_equal(response.status,401)
            tasks=await store.list_tasks();require_equal(tasks[0].owner,'local-owner')
            require_equal(tasks[0].action['notify'],'immer')
            require_equal(tasks[0].title,'First')
            # Repeated completion using the SAME actual approved receipt cannot
            # create a second schedule or reset the original due time.
            with bound(UseContext(principal='local-owner',approval_id=requests[0],origin=OriginClass.TRUSTED_INTERACTIVE_APP)):
                repeated=await caps.create(dict(schedule,titel='First'))
            require_equal(repeated['id'],tasks[0].task_id)
            require_equal(len(await store.list_tasks()),1)
        finally:
            await client.close(); await w['speicher'].close()


async def t_native_sent_mail_search_exposes_only_selection_metadata():
    import test_memory_mutation_endpoint as mutation
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from solvio import everyday_endpoint as endpoint
    cp=mutation._ControlPlane(mutation.H.fake_verifier()); dev=mutation._enroll(cp,'schedule-search')
    cp.store.rows[dev.device_id]['principal']='owner-test'
    reader=AsyncMock(return_value=SimpleNamespace(succeeded=True,data={'messages':[
        {'id':'s','thread_id':'t','subject':'Synthetic','date':'yesterday','body':'must not leave','sent':True,'draft':False},
        {'id':'d','thread_id':'t','subject':'Draft','sent':False,'draft':True}]}))
    app=web.Application();app['control_plane']=cp
    endpoint.attach(app,SimpleNamespace(dispatcher=SimpleNamespace(capabilities=SimpleNamespace(execute=reader))))
    client=TestClient(TestServer(app));await client.start_server()
    try:
        require_equal((await client.post(endpoint.API+'/mail/search',json={'query':'Synthetic'})).status,401)
        response=await client.post(endpoint.API+'/mail/search',json={'query':'Synthetic'},headers=dev.headers())
        require_equal(response.status,200)
        rows=(await response.json())['messages'];require_equal(len(rows),1)
        require_equal(set(rows[0]),{'id','thread_id','subject','date'})
        require_equal(reader.await_args.kwargs['principal'],'owner-test')
        require(reader.await_args.args[1]['query'].startswith('in:sent -in:drafts'))
    finally:await client.close()


async def t_pause_or_delete_during_private_read_stays_revoked_on_success_and_failure():
    from solvio.proactive.scheduler import Scheduler
    for delete in (True,False):
        for fails in (True,False):
            with tempfile.TemporaryDirectory() as temp:
                store=S.ProactiveStore(os.path.join(temp,'p.sqlite3'))
                current=task();current.next_run_at=1000;await store.put_task(current)
                calls=[]
                async def read(name,args,**kwargs):
                    calls.append(name)
                    if delete: await store.delete_task(current.task_id)
                    else:
                        live=await store.get_task(current.task_id);live.enabled=False;live.state=S.PAUSED
                        await store.put_task(live)
                    if fails:raise RuntimeError('synthetic unavailable')
                    return SimpleNamespace(succeeded=True,data={'events':[]})
                scheduler=Scheduler(SimpleNamespace(capabilities=SimpleNamespace(execute=read)),store,clock=lambda:1000)
                await scheduler.run_once(current,1000)
                require_equal(calls,['calendar_list_events'])
                require_equal(await store.unread_count(),0)
                after=await store.get_task(current.task_id)
                if delete:require(after is None)
                else:require(not after.enabled);require_equal(after.state,S.PAUSED)
                # Timeout/error completion must not revive an old snapshot either.
                await scheduler._after_failure(current,'timeout','synthetic')
                after=await store.get_task(current.task_id)
                if delete:require(after is None)
                else:require(not after.enabled)


async def t_failed_one_shot_read_retries_actual_read_and_keeps_failure_evidence():
    from solvio.proactive.scheduler import Scheduler
    with tempfile.TemporaryDirectory() as temp:
        store=S.ProactiveStore(os.path.join(temp,'p.sqlite3'));now=1000
        current=task('mail_antwort_pruefen');current.schedule=in_seconds(1,now=999).as_dict()
        current.next_run_at=1000;current.action['arguments']={'thread_id':'t','message_id':'sent'}
        await store.put_task(current)
        reader=AsyncMock(side_effect=[SimpleNamespace(succeeded=False,data=None),SimpleNamespace(succeeded=True,data={'messages':[ORIGINAL]})])
        scheduler=Scheduler(SimpleNamespace(capabilities=SimpleNamespace(execute=reader)),store,clock=lambda:now)
        await scheduler.run_once(current,1000)
        require_equal(reader.await_count,1);first=await store.get_task(current.task_id)
        require_equal(first.state,S.ACTIVE);require_equal(first.next_run_at,first.retry_after)
        require_equal(await store.unread_count(),1)
        now=first.retry_after;await scheduler.recover()
        second=await store.get_task(current.task_id);await scheduler.run_once(second,second.next_run_at)
        require_equal(reader.await_count,2)
        require_equal((await store.get_task(current.task_id)).state,S.COMPLETED)
        require_equal(len(await store.runs_for(current.task_id)),2)
        require_equal(await store.unread_count(),2)


async def t_voice_schedule_uses_actual_approving_owner_not_transport_alias():
    import test_notiz_fortsetzung as fixture
    from solvio.capabilities.proactive import ProactiveCapabilities, register
    from solvio.capabilities.contract import ArgumentSource
    from solvio.capabilities.policy import OriginClass
    from solvio.contracts.trust import TrustContext, TrustLevel
    from solvio.tools.agent_capability_tools import remember_pending_start
    w=await fixture._aufbau()
    try:
        with tempfile.TemporaryDirectory() as temp:
            store=S.ProactiveStore(os.path.join(temp,'p.sqlite3'));register(w['orch'].router,ProactiveCapabilities(store))
            args={'titel':'Voice overview','wann':'täglich 08:00','aktion':'tagesueberblick','argumente':{}}
            context=SimpleNamespace(principal='iphone-transport-alias',origin=OriginClass.ROOM_VOICE,commanded=True)
            result=await w['orch'].router.execute('background_create',args,
                trust=TrustContext(TrustLevel.USER_DIRECT,user_authorized=True),
                provenance={k:ArgumentSource.TRUSTED_CONTEXT for k in args},
                principal=context.principal,origin=context.origin,commanded=True)
            require_equal(result.outcome.value,'approval_required')
            remember_pending_start(w['ledger'],capability='background_create',result=result,arguments=args,context=context)
            require_equal(await fixture._freigeben(w,result.data['request_id']),'ok')
            await w['orch']._poll_pending_starts()
            tasks=await store.list_tasks();require_equal(len(tasks),1)
            require_equal(tasks[0].owner,'local-owner')
    finally:await w['speicher'].close()


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
