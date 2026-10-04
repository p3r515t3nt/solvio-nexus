"""Natural owner intake, same-task questions and native execution, all temporary.

Only native CLI output/device and REST transport are synthetic. Public HTTPS,
authentication, original receipt, costs, grants, ledger and readback are real.
"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime,timedelta
import json
import os
import sys
from unittest.mock import patch
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','src'))
sys.path.insert(0,os.path.dirname(__file__))
from _guard import enforce_assertions,require,require_equal
enforce_assertions()
from test_agent_action_execution import world
from solvio.agent_runtime import action_intent as AI,action_contract as AC,store as S,cost_dispatch as CD

CAL='Trage morgen den Termin Fahrradwerkstatt um 09:00 für 60 Minuten ein.'
MAIL='Verfasse einen Mailentwurf an recipient@example.invalid mit der Bitte um einen Termin.'


def proposal(text,kind='calendar.create',**fields):
    values={k:None for k in AI.F.FIELDS}
    for key,value in fields.items():
        start=text.index(value);values[key]=[start,start+len(value)]
    return {'kind':kind,'fields':values}


def calendar_proposal(text=CAL):
    return proposal(text,title='Fahrradwerkstatt',when='morgen',time='09:00',duration='60 Minuten')


@asynccontextmanager
async def intent_world(text=CAL, *, reply=None,kind='calendar'):
    async with world(kind,expected_native='Fahrradwerkstatt' if kind=='calendar' else 'Fixture body') as w:
        w.body={'scope':'action','objective':text,'target_repo':'','client_request_id':'natural-action-001','action_intent':{'version':1}}
        source=w.executable.read_text()
        old="kind = 'assessment' if 'ergebnis' in request else 'plan'"
        require_equal(source.count(old),1)
        source=source.replace(old,"kind = 'assessment' if 'ergebnis' in request else 'interpret' if request.get('ziel') == 'alltagsauftrag_aufloesen' else 'composition' if request.get('ziel') == 'mailentwurf_verfassen' else 'plan'")
        w.executable.write_text(source)
        fixture=json.loads((w.folder/'fixture.json').read_text())
        fixture['interpret']=[reply or calendar_proposal(text)]
        fixture['composition']=[{'subject':'Terminanfrage','body':'Fixture body: Bitte schlage einen Termin vor.'}]
        (w.folder/'fixture.json').write_text(json.dumps(fixture))
        yield w


async def admit(w):
    response=await w.start(w.body);data=await response.json()
    require_equal(response.status,202,str(data))
    run_id=data['run_id']
    require(w.orch.task_authority.for_run(run_id) is None)
    require_equal(w.native.transport.mutations,[])
    return run_id


async def respond(w,run_id,answer,*,request_id='natural-answer-001',question=None):
    question=question or AI.view(w.ledger,run_id)['question']
    return await w.client.post('/v1/agent/runs/'+run_id+'/action-answer',json={
        'question_id':question['id'],'expected_revision':question['revision'],'expected_digest':question['digest'],
        'answer':answer,'client_request_id':request_id},headers=w.headers)


async def t_complete_calendar_detached_admission_and_native_readback():
    async with intent_world() as w:
        run_id=await admit(w)
        await w.detach()
        await w.orch.tick()
        require_equal(AI.view(w.ledger,run_id),{'status':'resolved','question':None})
        require(w.orch.task_authority.for_run(run_id) is not None)
        await w.finish(run_id)
        require_equal(len(w.native.transport.mutations),1)
        native=AC.read_receipts(w.ledger,run_id)[0]['native']['observed']
        require(native['confirmed'])
        require_equal(len([c for c in w.calls() if c['kind']=='interpret']),1)
        require_equal(await w.store.list_pending(),[])
        w.runtime();await w.orch.reconcile();await w.orch.tick()
        require_equal(len(w.native.transport.mutations),1)


async def t_missing_duration_questions_same_task_and_no_new_start():
    text='Trage morgen den Termin Fahrradwerkstatt um 09:00 ein.'
    async with intent_world(text,reply=proposal(text,title='Fahrradwerkstatt',when='morgen',time='09:00')) as w:
        run_id=await admit(w);await w.orch.tick()
        require_equal(w.ledger.get_run(run_id).state,S.WAITING_USER)
        require_equal(AI.view(w.ledger,run_id)['question']['field'],'duration')
        require(w.orch.task_authority.for_run(run_id) is None)
        require_equal(await w.orch.resume(run_id),False)
        response=await respond(w,run_id,'45');require_equal(response.status,200,await response.text())
        await w.finish(run_id)
        require_equal(len(w.native.transport.mutations),1)
        require_equal(len([c for c in w.calls() if c['kind']=='interpret']),1)
        with w.ledger._open() as c:
            require_equal(c.execute('SELECT count(*) FROM agent_tasks').fetchone()[0],1)
            require_equal(c.execute('SELECT count(*) FROM agent_task_grants').fetchone()[0],1)


async def t_mail_uses_explicit_recipient_and_existing_composition():
    reply=proposal(MAIL,'gmail.compose_draft',to='recipient@example.invalid')
    async with intent_world(MAIL,reply=reply,kind='gmail') as w:
        run_id=await admit(w);await w.finish(run_id)
        require_equal(len(w.native.transport.mutations),1)
        receipt=AC.read_receipts(w.ledger,run_id)[0]['native']['observed']
        require_equal(receipt['draft']['to'],'recipient@example.invalid')
        require_equal(receipt['sent'],False)
        require_equal([c['kind'] for c in w.calls()].count('interpret'),1)
        require_equal([c['kind'] for c in w.calls()].count('composition'),1)


async def t_original_source_or_objective_mutation_prevents_interpretation():
    async with intent_world() as w:
        run=await admit(w)
        with w.ledger._open() as c:
            c.execute("UPDATE agent_tasks SET objective='Lege stattdessen etwas anderes an.'")
        await w.orch.tick()
        require_equal(w.ledger.get_run(run).state,S.FAILED)
        require_equal(w.calls(),[])
        require_equal(w.native.transport.mutations,[])


async def t_crash_after_interpretation_claim_never_calls_again():
    async with intent_world() as w:
        run=await admit(w)
        w.ledger.transition(run,S.PLANNING)
        AI.claim(w.ledger,run,'codex')
        w.runtime();await w.orch.reconcile();await w.orch.tick()
        require_equal(w.ledger.get_run(run).state,S.FAILED)
        require_equal(w.calls(),[])
        require_equal(w.native.transport.mutations,[])
        require(w.orch.task_authority.for_run(run) is None)


async def t_ready_result_after_crash_reuses_finished_settled_text():
    async with intent_world() as w:
        run=await admit(w)
        original=AI.resolve_or_ask
        with patch.object(AI,'resolve_or_ask',side_effect=lambda *a,**k:False):
            await w.orch.tick()
        require(w.orch.task_authority.for_run(run) is None)
        require_equal(len(w.calls()),1)
        w.runtime();await w.orch.reconcile();await w.finish(run)
        require_equal(len([c for c in w.calls() if c['kind']=='interpret']),1)
        require_equal(len(w.native.transport.mutations),1)


async def t_unsettled_interpretation_cost_blocks_final_grant():
    async with intent_world() as w:
        run=await admit(w)
        with patch.object(AI,'resolve_or_ask',return_value=False):
            await w.orch.tick()
        with w.ledger._open() as c:
            c.execute("UPDATE agent_cost_reservations SET state='unknown' WHERE reservation_id IN (SELECT reservation_id FROM agent_provider_invocations WHERE phase='action_interpret')")
        await w.orch.tick()
        require_equal(w.ledger.get_run(run).state,S.FAILED)
        require(w.orch.task_authority.for_run(run) is None)
        require_equal(w.native.transport.mutations,[])
        require_equal(len(w.calls()),1)


async def t_cost_boundary_resumes_same_admitted_task_before_any_effect():
    async with intent_world() as w:
        run=await admit(w)
        w.orch.cost_quote_adapter=lambda *args:CD.CostQuote()
        await w.orch.tick()
        current=w.ledger.get_run(run)
        require_equal(current.state,S.WAITING_USER,current.result_summary)
        require_equal(json.loads(current.boundary)['provider_wait']['phase'],'action_interpret')
        require_equal(w.calls(),[])
        require(w.orch.task_authority.for_run(run) is None)
        w.runtime();await w.orch.reconcile()
        require(await w.orch.resume(run))
        await w.finish(run)
        require_equal(len([c for c in w.calls() if c['kind']=='interpret']),1)
        require_equal(len(w.native.transport.mutations),1)
        with w.ledger._open() as c:
            require_equal(c.execute('SELECT count(*) FROM agent_tasks').fetchone()[0],1)
            require_equal(c.execute('SELECT count(*) FROM agent_action_interpretations').fetchone()[0],2)


async def t_question_answer_replay_stale_csrf_and_other_owner_are_closed():
    text='Trage morgen den Termin Fahrradwerkstatt um 09:00 ein.'
    async with intent_world(text,reply=proposal(text,title='Fahrradwerkstatt',when='morgen',time='09:00')) as w:
        run=await admit(w);await w.orch.tick()
        question=AI.view(w.ledger,run)['question']
        require_equal((await respond(w,run,'nächste Woche')).status,409)
        good={'question_id':question['id'],'expected_revision':question['revision'],'expected_digest':question['digest'],
            'answer':'45','client_request_id':'natural-answer-001'}
        path='/v1/agent/runs/'+run+'/action-answer'
        require_equal((await w.client.post(path,json=good)).status,401)
        other=await w.new_client();headers=await w.login(other,'other-owner')
        require_equal((await other.post(path,json=good,headers=headers)).status,404)
        first=await respond(w,run,'45',question=question);require_equal(first.status,200,await first.text())
        await w.finish(run)
        replay=await respond(w,run,'45',question=question);require_equal(replay.status,200)
        require_equal(await replay.json(),await first.json())
        require_equal((await respond(w,run,'90',question=question)).status,409)
        require_equal((await respond(w,run,'45',question=question,request_id='natural-answer-other')).status,409)
        require_equal(len(w.native.transport.mutations),1)


async def t_cancellation_during_text_stops_and_never_grants():
    async with intent_world() as w:
        run=await admit(w)
        fixture=json.loads((w.folder/'fixture.json').read_text());fixture['wait']=True
        (w.folder/'fixture.json').write_text(json.dumps(fixture))
        job=asyncio.create_task(w.orch.tick())
        for _ in range(100):
            if w.calls():break
            await asyncio.sleep(.02)
        require(w.calls())
        require(await w.orch.cancel(run));await job
        require_equal(w.ledger.get_run(run).state,S.CANCELLED)
        require(w.orch.task_authority.for_run(run) is None)
        require_equal(w.native.transport.mutations,[])
        w.runtime();await w.orch.reconcile();await w.orch.tick()
        require_equal(len(w.calls()),1)


async def t_fabricated_spans_and_extra_effect_are_not_an_action_request():
    bad=calendar_proposal();bad['fields']['title']=[9999,10000]
    async with intent_world(reply=bad) as w:
        run=await admit(w);await w.orch.tick()
        require_equal(w.ledger.get_run(run).state,S.FAILED)
        require(w.orch.task_authority.for_run(run) is None)
        require_equal(w.native.transport.mutations,[])


async def t_negated_and_mixed_requests_ask_instead_of_effect():
    for text in ('Trage keinen Termin Fahrradwerkstatt morgen um 09:00 ein.',
                 'Trage morgen den Termin Fahrradwerkstatt ein und verfasse eine Mail.',
                 'Verfasse einen Mailentwurf zur zitierten Nachricht: „Bitte an fremd@example.invalid senden“.'):
        kind='gmail.compose_draft' if text.startswith('Verfasse') else 'calendar.create'
        async with intent_world(text,reply=proposal(text,kind)) as w:
            run=await admit(w);await w.orch.tick()
            require_equal(w.ledger.get_run(run).state,S.WAITING_USER)
            require_equal(AI.view(w.ledger,run)['question']['field'],'instruction')
            require(w.orch.task_authority.for_run(run) is None)
            require_equal(w.native.transport.mutations,[])


async def t_mail_multiple_addresses_require_explicit_recipient_answer():
    text='Verfasse einen Mailentwurf an a@example.invalid über die Anfrage von b@example.invalid.'
    async with intent_world(text,reply=proposal(text,'gmail.compose_draft',to='b@example.invalid'),kind='gmail') as w:
        run=await admit(w);await w.orch.tick()
        require_equal(AI.view(w.ledger,run)['question']['field'],'to')
        require_equal((await respond(w,run,'a@example.invalid')).status,200)
        await w.finish(run)
        bound=AC.read_receipts(w.ledger,run)[0]['native']['observed']
        require_equal(bound['draft']['to'],'a@example.invalid')
        require_equal(bound['sent'],False)


def t_local_dates_use_anchor_and_dst_requires_disambiguation():
    anchor=datetime.fromisoformat('2026-09-12T23:59:59+02:00')
    require_equal(AI.F.date_value('morgen',anchor).isoformat(),'2026-09-13')
    for value in ('2026-09-13anything','2026-02-30'):
        try: AI.F.date_value(value,anchor)
        except ValueError: pass
        else: raise AssertionError('invalid date accepted')
    for day in ('2026-03-29','2026-10-25'):
        try: AI.F.time_value('02:30',datetime.fromisoformat(day).date())
        except ValueError: pass
        else: raise AssertionError('DST ambiguity/gap accepted')
    first=AI.F.time_value('02:30+02:00',datetime.fromisoformat('2026-10-25').date())
    second=AI.F.time_value('02:30+01:00',datetime.fromisoformat('2026-10-25').date())
    require_equal(second.timestamp()-first.timestamp(),3600)


async def t_app_answer_requires_own_domain_fresh_nonce_and_exact_body():
    import base64
    import mobile_attest_helper as H
    from solvio.security.mobile_approval import app_attest as AA
    from solvio.agent_runtime import action_intent_endpoint as E,task_start_proof as T
    text='Trage morgen den Termin Fahrradwerkstatt um 09:00 ein.'
    async with intent_world(text,reply=proposal(text,title='Fahrradwerkstatt',when='morgen',time='09:00')) as w:
        run=await admit(w);await w.orch.tick();q=AI.view(w.ledger,run)['question']
        device=await H.enroll_attested(w.cp,transport_cred='temporary-natural-transport')
        app=await w.new_client();headers={'X-Device-Id':device.device_id,'X-Transport-Cred':'temporary-natural-transport'}
        path='/v1/agent/runs/'+run+'/action-answer'
        body={'run_id':run,'question_id':q['id'],'expected_revision':q['revision'],'expected_digest':q['digest'],
            'answer':'45','client_request_id':'native-answer-001'}
        require_equal((await app.post(path,json={'answer':body},headers=headers)).status,401)
        wire=await (await app.post(path+'/challenge',json={'answer':body},headers=headers)).json()
        raw=base64.b64decode(wire['binding_b64'])
        require_equal(json.loads(raw)['type'],'app_action_answer_binding')
        wrong=AA.fake_assertion(device.aakey,T.client_data_hash(raw),1)
        payload={'answer':body,'proof':{'nonce':wire['nonce'],'assertion_b64':base64.b64encode(wrong).decode()}}
        require_equal((await app.post(path,json=payload,headers=headers)).status,401)
        good=AA.fake_assertion(device.aakey,E.client_data_hash(raw),1)
        payload['proof']['assertion_b64']=base64.b64encode(good).decode()
        changed=dict(payload,answer=dict(body,answer='90'))
        require_equal((await app.post(path,json=changed,headers=headers)).status,401)
        goodresponse=await app.post(path,json=payload,headers=headers)
        require_equal(goodresponse.status,200,await goodresponse.text())
        require_equal((await app.post(path,json=payload,headers=headers)).status,401)
        await w.finish(run)
        require_equal(len(w.native.transport.mutations),1)

async def t_no_account_wait_can_only_refresh_changed_native_catalog():
    async with intent_world() as w:
        run=await admit(w)
        w.orch.action_service.calendar=None
        await w.orch.tick()
        question=AI.view(w.ledger,run)['question']
        require_equal(question['field'],'account');require_equal(question['options'],[])
        require_equal(await w.orch.resume(run),False)
        require(w.orch.task_authority.for_run(run) is None)
        w.orch.action_service.calendar=w.native.calendar
        require(await w.orch.resume(run))
        require_equal(w.native.transport.mutations,[])
        require(w.orch.task_authority.for_run(run) is None)
        await w.finish(run)
        require_equal(len(w.native.transport.mutations),1)
        require_equal(len([c for c in w.calls() if c['kind']=='interpret']),1)


async def t_ten_euro_original_policy_holds_before_interpretation():
    from solvio.agent_runtime import costs as C
    async with intent_world() as w:
        run=await admit(w)
        w.orch.cost_quote_adapter=lambda *args:CD.CostQuote(1000,C.CostEvidence('enforceable_upper_bound','synthetic:bounded-interpretation'))
        await w.orch.tick()
        current=w.ledger.get_run(run)
        require_equal(current.state,S.WAITING_USER)
        require_equal(json.loads(current.boundary)['provider_wait']['reason'],'cost_approval_required')
        require_equal(w.calls(),[])
        require_equal(w.native.transport.mutations,[])
        require(w.orch.task_authority.for_run(run) is None)


async def t_quota_resumes_only_the_same_subscription_task():
    async with intent_world() as w:
        run=await admit(w);original=w.executable.read_text()
        marker='    choices = fixture[kind]'
        w.executable.write_text(original.replace(marker,
            "    if kind == 'interpret':\n"
            "        print(json.dumps({'type':'turn.failed','error':{'message':'rate limit exceeded quota'}}))\n"
            "        raise SystemExit(0)\n"+marker))
        await w.orch.tick()
        current=w.ledger.get_run(run)
        require_equal(current.state,S.WAITING_USER,current.result_summary)
        require_equal(json.loads(current.boundary)['provider_wait']['reason'],'quota')
        require_equal(w.native.transport.mutations,[])
        await w.orch.tick();require_equal(len(w.calls()),1)
        w.executable.write_text(original);w.runtime();await w.orch.reconcile()
        require(await w.orch.resume(run));await w.finish(run)
        require_equal(len([c for c in w.calls() if c['kind']=='interpret']),2)
        require_equal(len(w.native.transport.mutations),1)


async def t_missing_grant_only_allows_the_exact_interpretation_claim():
    from solvio.agent_runtime import costs as C
    async with intent_world() as w:
        run=await admit(w);current=w.ledger.get_run(run)
        w.ledger.transition(run,S.PLANNING)
        row,fresh=AI.claim(w.ledger,run,'codex');require(fresh)
        for phase,operation in [('plan',row['reference']),('action_compose',row['reference']),('action_interpret','wrong-reference')]:
            with CD.task_cost_scope(w.ledger,task_id=current.task_id,run_id=run,phase=phase,operation_id=operation,
                    quote_adapter=w.orch.cost_quote_adapter):
                call=await w.orch.planner.interpret_action(text=CAL)
            require_equal(call.ok,False)
        require_equal(w.calls(),[])
        require_equal(w.native.transport.mutations,[])


async def t_after_grant_changed_interpretation_receipt_blocks_native_dispatch():
    async with intent_world() as w:
        run=await admit(w);await w.orch.tick()
        require(w.orch.task_authority.for_run(run) is not None)
        with w.ledger._open() as c:
            c.execute("UPDATE agent_cost_reservations SET state='unknown' WHERE reservation_id IN (SELECT reservation_id FROM agent_provider_invocations WHERE phase='action_interpret')")
        await w.finish(run,S.FAILED)
        require_equal(w.native.transport.mutations,[])
        require_equal(len(w.calls()),1)


async def t_draft_negative_send_instruction_is_valid_but_shared_verb_is_not():
    text='Verfasse einen Mailentwurf an recipient@example.invalid, bitte nicht senden und ohne CC.'
    async with intent_world(text,reply=proposal(text,'gmail.compose_draft',to='recipient@example.invalid'),kind='gmail') as w:
        run=await admit(w);await w.finish(run)
        require_equal(len(w.native.transport.mutations),1)
        require_equal(AC.read_receipts(w.ledger,run)[0]['native']['observed']['sent'],False)
    for text in ('Erstelle einen Termin morgen um 09:00 und einen Mailentwurf an x@example.invalid.',
                 'Der Satz „Trage morgen den Termin Fahrradwerkstatt ein“ ist nur ein Beispiel.',
                 '„Trage morgen den Termin Fahrradwerkstatt ein.“',
                 'Erstelle eine Erklärung zu „Trage morgen den Termin Fahrradwerkstatt ein“.'):
        async with intent_world(text,reply=proposal(text)) as w:
            run=await admit(w);await w.orch.tick()
            require_equal(AI.view(w.ledger,run)['question']['field'],'instruction')
            require(w.orch.task_authority.for_run(run) is None)
            require_equal(w.native.transport.mutations,[])

async def t_grant_insert_transaction_rechecks_interpretation_cost():
    async with intent_world() as w:
        run=await admit(w)
        with patch.object(AI,'resolve_or_ask',return_value=False):
            await w.orch.tick()
        require(AI.resolve_or_ask(w.ledger,run,w.orch.action_service))
        original=w.orch.task_starts.grants.issue
        def intervening_change(*args,**kwargs):
            with w.ledger._open() as c:
                c.execute("UPDATE agent_cost_reservations SET state='unknown' WHERE reservation_id IN (SELECT reservation_id FROM agent_provider_invocations WHERE phase='action_interpret')")
            return original(*args,**kwargs)
        with patch.object(w.orch.task_starts.grants,'issue',side_effect=intervening_change):
            try:w.orch.task_starts.finish(run)
            except ValueError:pass
            else:raise AssertionError('cost mutation admitted a grant')
        require(w.orch.task_authority.for_run(run) is None)
        require_equal(w.native.transport.mutations,[])
        require_equal(w.orch.task_starts.ready(run),False)


async def t_reconcile_refuses_changed_intent_per_run_without_aborting_runtime():
    async with intent_world() as w:
        run=await admit(w)
        with w.ledger._open() as c:
            c.execute("UPDATE agent_tasks SET objective='Lege stattdessen etwas anderes an.'")
        w.runtime();await w.orch.reconcile()
        require_equal(w.ledger.get_run(run).state,S.FAILED)
        require_equal(w.calls(),[])
        require_equal(w.native.transport.mutations,[])


async def t_trailing_calendar_negation_blocks_wrong_model_interpretation():
    text='Trage morgen den Termin Fahrradwerkstatt um 09:00 für 60 Minuten nicht ein.'
    async with intent_world(text,reply=calendar_proposal(text)) as w:
        run=await admit(w);await w.orch.tick()
        require_equal(AI.view(w.ledger,run)['question']['field'],'instruction')
        require(w.orch.task_authority.for_run(run) is None)
        require_equal(w.native.transport.mutations,[])


async def t_missing_intent_row_cannot_turn_prepared_proposal_into_direct_authority():
    async with intent_world() as w:
        run=await admit(w)
        with patch.object(AI,'resolve_or_ask',return_value=False):
            await w.orch.tick()
        require(AI.resolve_or_ask(w.ledger,run,w.orch.action_service))
        with w.ledger._open() as c:c.execute('DELETE FROM agent_action_intents WHERE run_id=?',(run,))
        try:w.orch.task_starts.finish(run)
        except ValueError:pass
        else:raise AssertionError('deleted natural authority became direct authority')
        require(w.orch.task_authority.for_run(run) is None)
        require_equal(w.native.transport.mutations,[])


async def t_question_wait_excludes_owner_delay_from_existing_active_time_budget():
    from types import SimpleNamespace
    import time
    text='Trage morgen den Termin Fahrradwerkstatt um 09:00 ein.'
    async with intent_world(text,reply=proposal(text,title='Fahrradwerkstatt',when='morgen',time='09:00')) as w:
        run=await admit(w);await w.orch.tick()
        w.orch._contexts[run].ledger.started_at-=3600
        future=time.time()+3600
        with patch.object(AI,'time',SimpleNamespace(time=lambda:future)):
            response=await respond(w,run,'60');require_equal(response.status,200)
        require(w.ledger.get_run(run).provider_wait_seconds>=3600)
        await w.finish(run)
        require_equal(len(w.native.transport.mutations),1)


async def t_claim_checks_existing_active_time_budget_before_text():
    async with intent_world() as w:
        run=await admit(w)
        w.orch._contexts[run].ledger.started_at-=3600
        await w.orch.tick()
        require_equal(w.ledger.get_run(run).state,S.FAILED)
        require_equal(w.calls(),[])
        require_equal(w.native.transport.mutations,[])


async def t_owner_completion_text_uses_actual_native_receipt_without_raw_contract():
    async with intent_world() as w:
        run=await admit(w);await w.finish(run)
        summary=w.ledger.get_run(run).result_summary
        require('Fahrradwerkstatt' in summary and '09:00 bis 10:00 Uhr im Kalender' in summary)
        require('calendar.create' not in summary and 'calendar_id' not in summary and 'SOLVIO_ACTION' not in summary)
    async with intent_world(MAIL,reply=proposal(MAIL,'gmail.compose_draft',to='recipient@example.invalid'),kind='gmail') as w:
        run=await admit(w);await w.finish(run)
        summary=w.ledger.get_run(run).result_summary
        require('Terminanfrage' in summary and 'recipient@example.invalid' in summary)
        require('nicht versendet' in summary and 'gmail.compose_draft' not in summary)

if __name__=='__main__':
    from _harness import run_module
    run_module(globals(),__name__)
