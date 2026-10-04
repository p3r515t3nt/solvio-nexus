"""Authenticated, bounded preparation before a native action grant exists.

The original task owns all questions and provider costs. The subscription
planner extracts literal spans only. A final exact action is frozen with the
original entrance before TaskStartService may issue its ordinary grant.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import json
import re
import time
import uuid
from solvio.agent_runtime import action_contract as AC, store as S, planner as PL, budget as BU
from solvio.agent_runtime import action_intent_fields as F
from solvio.agent_runtime.task_authority import _task_fingerprint, VerifiedTaskReceipt

SCHEMA='''
CREATE TABLE IF NOT EXISTS agent_action_intents (
 run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, binding_json TEXT NOT NULL,
 binding_digest TEXT NOT NULL, current_json TEXT NOT NULL, current_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_action_interpretations (
 reference TEXT PRIMARY KEY, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
 binding_json TEXT NOT NULL, binding_digest TEXT NOT NULL,
 state TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '', result_digest TEXT NOT NULL DEFAULT '',
 UNIQUE(run_id,revision)
);
CREATE TABLE IF NOT EXISTS agent_action_intent_answers (
 run_id TEXT NOT NULL, request_id TEXT NOT NULL, request_digest TEXT NOT NULL,
 answer_json TEXT NOT NULL, receipt_json TEXT NOT NULL, receipt_digest TEXT NOT NULL,
 response_json TEXT NOT NULL, PRIMARY KEY(run_id,request_id)
);
'''


def _json(value): return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
def _hash(value): return AC._hash('SOLVIO_ACTION_INTENT_V1',value)


@dataclass(frozen=True)
class IntentRequest:
    version: int=1
    @property
    def descriptor(self): return {'version':self.version}


def validate_request(value):
    if type(value) is IntentRequest: value=value.descriptor
    if type(value) is not dict or set(value)!={'version'} or type(value['version']) is not int or value['version']!=1:
        raise ValueError('invalid_action_intent')
    return IntentRequest()


def _exists(c):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_action_intents'").fetchone() is not None


def admit(c, *, task_id, run_id, now):
    task=c.execute('SELECT * FROM agent_tasks WHERE task_id=?',(task_id,)).fetchone()
    source=c.execute('SELECT * FROM agent_task_sources WHERE run_id=?',(run_id,)).fetchone()
    binding={'task_id':task_id,'run_id':run_id,'fingerprint':_task_fingerprint(task),
        'objective':task['objective'],'anchor':datetime.fromtimestamp(now,F.USER_TIMEZONE).isoformat(),
        'timezone':'Europe/Berlin','source':{k:source[k] for k in ('principal','request_id','request_digest',
            'receipt_method','receipt_reference','authorizer','cost_threshold_cents')}}
    current={'revision':1,'state':'pending','values':None,'answers':{},'answer_refs':[],
        'question':None,'waiting_since':0.0,'interpretation':'','resolved_request':None,'catalog':None}
    c.execute('INSERT INTO agent_action_intents VALUES (?,?,?,?,?,?)',
        (run_id,task_id,_json(binding),_hash(binding),_json(current),_hash(current)))
    c.execute('UPDATE agent_task_sources SET action_intent_digest=? WHERE run_id=?',(_hash(binding),run_id))


def _read(c,run_id,*,active=False):
    source_columns={r[1] for r in c.execute('PRAGMA table_info(agent_task_sources)')}
    marker=(c.execute('SELECT action_intent_digest FROM agent_task_sources WHERE run_id=?',(run_id,)).fetchone()
            if 'action_intent_digest' in source_columns else None)
    if not _exists(c):
        if marker and marker[0]: raise ValueError('action_intent_missing')
        return None
    row=c.execute('SELECT * FROM agent_action_intents WHERE run_id=?',(run_id,)).fetchone()
    if row is None:
        if marker and marker[0]: raise ValueError('action_intent_missing')
        return None
    if not marker or marker[0]!=row['binding_digest']:
        raise ValueError('action_intent_source_changed')
    binding,current=json.loads(row['binding_json']),json.loads(row['current_json'])
    task=c.execute('SELECT * FROM agent_tasks WHERE task_id=?',(row['task_id'],)).fetchone()
    source=c.execute('SELECT * FROM agent_task_sources WHERE run_id=?',(run_id,)).fetchone()
    run=c.execute('SELECT * FROM agent_runs WHERE run_id=?',(run_id,)).fetchone()
    if (not task or not source or not run or _hash(binding)!=row['binding_digest']
            or _hash(current)!=row['current_digest'] or binding['run_id']!=run_id
            or binding['task_id']!=row['task_id'] or run['task_id']!=row['task_id']
            or binding['fingerprint']!=_task_fingerprint(task) or task['scope']!=S.SCOPE_ACTION
            or task['objective']!=binding['objective']
            or binding['source']!={k:source[k] for k in binding['source']}
            or source['principal']!=task['created_principal'] or source['authorizer']!=source['principal']
            or (source['receipt_method'],task['created_origin']) not in {
                ('dashboard_session','trusted_dashboard'),('app_session','trusted_interactive_app')}):
        raise ValueError('action_intent_binding_changed')
    if active and (task['state']!=S.TASK_ACTIVE or run['state'] in S.TERMINAL_STATES or run['finished_at'] is not None):
        raise ValueError('action_intent_inactive')
    answers={}
    for reference in current['answer_refs']:
        answer=c.execute('SELECT * FROM agent_action_intent_answers WHERE run_id=? AND request_id=?',(run_id,reference)).fetchone()
        if answer is None: raise ValueError('action_intent_answer_missing')
        body,receipt=json.loads(answer['answer_json']),json.loads(answer['receipt_json'])
        if (answer['receipt_digest']!=_hash({'answer':body,'receipt':receipt})
                or answer['request_digest']!=_hash({k:v for k,v in body.items() if k!='field'})
                or receipt['authorizer']!=source['principal']
                or receipt['method'] not in {'dashboard_session','app_session'}
                or body['field'] not in F.FIELDS|{'instruction','account'}):
            raise ValueError('action_intent_answer_changed')
        answers[body['field']]=body['answer']
    if current['answers']!=answers:
        raise ValueError('action_intent_answers_changed')
    return dict(row),binding,current


def _save(c,row,current):
    changed=c.execute('UPDATE agent_action_intents SET current_json=?,current_digest=? WHERE run_id=? AND current_digest=?',
        (_json(current),_hash(current),row['run_id'],row['current_digest'])).rowcount
    if changed!=1: raise ValueError('stale_question')


def pending(ledger,run_id):
    with ledger._open() as c:
        result=_read(c,run_id)
        return result is not None and result[2]['state']!='resolved'


def view(ledger,run_id):
    from solvio.agent_runtime import research_question as RQST
    question = RQST.view(ledger, run_id)
    if question is not None: return question
    with ledger._open() as c:
        result=_read(c,run_id)
        if result is None: return None
        _,_,current=result
        run=c.execute('SELECT state FROM agent_runs WHERE run_id=?',(run_id,)).fetchone()
        state=('failed' if run['state'] in (S.FAILED,S.CANCELLED) else
               'waiting_user' if current['question'] is not None else
               'resolved' if current['state']=='resolved' else 'interpreting')
        return {'status':state,'question':current['question'] if state=='waiting_user' else None}


def _call_read(c,row,binding,current):
    if not current['interpretation']: return None
    call=c.execute('SELECT * FROM agent_action_interpretations WHERE reference=?',(current['interpretation'],)).fetchone()
    if call is None: raise ValueError('action_intent_call_missing')
    bound=json.loads(call['binding_json'])
    if (call['run_id']!=row['run_id'] or call['binding_digest']!=_hash(bound)
            or bound['original_digest']!=row['binding_digest'] or bound['reference']!=call['reference']
            or bound['revision']!=call['revision']
            or bound['text']!=current['answers'].get('instruction',binding['objective'])
            or bound['answer_refs']!=current['answer_refs'][:len(bound['answer_refs'])]):
        raise ValueError('action_intent_call_changed')
    if call['state']!='claimed':
        result=json.loads(call['result_json'])
        if call['result_digest']!=_hash({'binding':call['binding_digest'],'result':result}):
            raise ValueError('action_intent_result_changed')
        if call['state']=='ready':
            _cost_proof(c,binding,bound,result['call'])
            extracted=F.extract(bound['text'],result['proposal'])
            if extracted!=result['values']: raise ValueError('action_intent_values_changed')
            if current['values']!=extracted: raise ValueError('action_intent_values_changed')
    elif call['result_json'] or call['result_digest']:
        raise ValueError('action_intent_call_changed')
    return dict(call)


def _cost_proof(c,binding,bound,call):
    invocation=c.execute('SELECT * FROM agent_provider_invocations WHERE invocation_id=?',(call.get('cost_invocation_id',''),)).fetchone()
    reservation=c.execute('SELECT * FROM agent_cost_reservations WHERE reservation_id=?',(call.get('cost_reservation_id',''),)).fetchone()
    if (call.get('ok') is not True or call.get('provider')!=bound['provider'] or call.get('billing_mode')!='subscription'
            or not invocation or not reservation or invocation['state']!='finished'
            or invocation['task_id']!=binding['task_id'] or invocation['run_id']!=binding['run_id']
            or invocation['phase']!='action_interpret' or invocation['operation_id']!=bound['reference']
            or invocation['provider']!=bound['provider'] or invocation['reservation_id']!=reservation['reservation_id']
            or reservation['state']!='settled' or reservation['subject_id']!=binding['task_id']
            or reservation['invocation_id']!=invocation['invocation_id']):
        raise ValueError('action_intent_cost_unverified')


def cost_reason(c,scope):
    try:
        result=_read(c,scope.run_id,active=True)
        if result is None: return 'cost_recovery_required'
        row,binding,current=result
        source=c.execute('SELECT state FROM agent_task_sources WHERE run_id=?',(scope.run_id,)).fetchone()
        call=_call_read(c,row,binding,current)
        if (binding['task_id']!=scope.task_id or source['state']!='preparing'
                or current['state']!='claimed' or current['question'] is not None
                or call is None or call['state']!='claimed' or call['reference']!=scope.operation_id
                or call['revision']!=current['revision']
                or c.execute('SELECT 1 FROM agent_task_grants WHERE run_id=?',(scope.run_id,)).fetchone()):
            return 'cost_recovery_required'
        return ''
    except (ValueError,KeyError,TypeError):
        return 'cost_recovery_required'


def claim(ledger,run_id,provider,*,resume=False):
    with ledger._open() as c:
        c.execute('BEGIN IMMEDIATE')
        row,binding,current=_read(c,run_id,active=True)
        prior=_call_read(c,row,binding,current)
        if prior is not None:
            if prior['state']!='blocked' or not resume:
                return prior,False
            old=json.loads(prior['binding_json'])
            result=json.loads(prior['result_json'])
            if old['provider']!=provider or not _safe_blocked(c,result['call']):
                raise ValueError('action_intent_retry_unverified')
            current['revision']+=1
            current['state']='pending'
            current['interpretation']=''

        if current['state']!='pending' or current['question'] is not None or provider not in {'codex','claude-code'}:
            raise ValueError('action_intent_not_interpretable')
        run=c.execute('SELECT * FROM agent_runs WHERE run_id=?',(run_id,)).fetchone()
        if run['state']!=S.PLANNING: raise ValueError('action_intent_not_planning')
        if run['planner_calls']>=BU.MAX_PLANNER_CALLS_PER_RUN:
            raise BU.BudgetExhausted('budget_exhausted','interpretation_calls')
        reference='aint-'+uuid.uuid4().hex
        bound={'reference':reference,'revision':current['revision'],'original_digest':row['binding_digest'],
            'text':current['answers'].get('instruction',binding['objective']),
            'answer_refs':current['answer_refs'],'provider':provider}
        c.execute('INSERT INTO agent_action_interpretations(reference,run_id,revision,binding_json,binding_digest,state) VALUES (?,?,?,?,?,?)',
            (reference,run_id,current['revision'],_json(bound),_hash(bound),'claimed'))
        current['state']='claimed';current['interpretation']=reference
        _save(c,row,current)
        c.execute('UPDATE agent_runs SET planner_calls=planner_calls+1 WHERE run_id=?',(run_id,))
        return dict(c.execute('SELECT * FROM agent_action_interpretations WHERE reference=?',(reference,)).fetchone()),True


def _safe_blocked(c,call):
    from solvio.agent_runtime.orchestrator import PROVIDER_BLOCKERS
    if call.get('ok') or call.get('reason') not in PROVIDER_BLOCKERS or call.get('reason')=='cost_recovery_required':
        return False
    invocation=c.execute('SELECT * FROM agent_provider_invocations WHERE invocation_id=?',(call.get('cost_invocation_id',''),)).fetchone()
    if invocation is None:
        return call.get('dispatch_started') is False
    reservation=c.execute('SELECT * FROM agent_cost_reservations WHERE reservation_id=?',(invocation['reservation_id'],)).fetchone()
    return invocation['state']=='finished' and reservation is not None and reservation['state']=='settled'


def settle(ledger,run_id,reference,call):
    fields={k:getattr(call,k) for k in PL.PlannerCall.__dataclass_fields__}
    with ledger._open() as c:
        c.execute('BEGIN IMMEDIATE')
        row,binding,current=_read(c,run_id,active=True)
        prior=_call_read(c,row,binding,current)
        if prior is None or prior['reference']!=reference or prior['state']!='claimed':
            raise ValueError('action_intent_claim_lost')
        bound=json.loads(prior['binding_json'])
        proposal=PL.call_payload(call) if call.ok else None
        values=F.extract(bound['text'],proposal) if call.ok else None
        if call.ok: _cost_proof(c,binding,bound,fields)
        result={'call':fields,'proposal':proposal,'values':values}
        state='ready' if call.ok else 'blocked' if _safe_blocked(c,fields) else 'failed'
        c.execute('UPDATE agent_action_interpretations SET state=?,result_json=?,result_digest=? WHERE reference=? AND state=?',
            (state,_json(result),_hash({'binding':prior['binding_digest'],'result':result}),reference,'claimed'))
        current['state']=state;current['values']=values
        _save(c,row,current)


def _catalog(service,kind):
    from solvio.capabilities import task_action as TA
    if service is not None and not TA._original(service, TA.TaskServiceAction, TA._TASK_ACTION_SERVICE_METHODS):
        raise ValueError('action_native_client_unavailable')
    wanted=kind.split('.')[0]
    rows=[dict(item) for item in service.accounts() if item['service']==wanted] if service else []
    if len(rows)>100: raise ValueError('action_catalog_too_large')
    # Only actual configured native accounts; opaque choices bind both account
    # and target resource, with no invented provider or account from a model.
    return [{'value':_hash({'service':r['service'],'account':r['account'],'resource':r['resource']}),**r} for r in rows]


def resolve_or_ask(ledger,run_id,service):
    with ledger._open() as c:
        c.execute('BEGIN IMMEDIATE')
        row,binding,current=_read(c,run_id,active=True)
        call=_call_read(c,row,binding,current)
        if current['state']=='resolved': return True
        if call is None or call['state']!='ready' or current['question'] is not None: return False
        anchor=datetime.fromisoformat(binding['anchor'])
        field,payload=F.resolve(current['values'],current['answers'],anchor=anchor)
        catalog=[];selected=None
        if not field:
            catalog=_catalog(service,current['values']['kind'])
            choice=current['answers'].get('account','')
            matches=[r for r in catalog if r['value']==choice] if choice else catalog
            if len(matches)==1: selected=matches[0]
            else: field='account'
        if field:
            prompt,input_type,placeholder=F.QUESTIONS[field]
            if field=='account' and not catalog:
                prompt='Für diesen Dienst ist noch kein verbundenes Konto verfügbar. Verbinde zuerst dein Konto in den Einstellungen.'
            question={'id':'aiq-'+uuid.uuid4().hex,'revision':current['revision'],'field':field,
                'prompt':prompt,'input_type':input_type,'placeholder':placeholder}
            if field=='account': question['options']=[{'value':r['value'],'label':r['label']} for r in catalog]
            question['digest']=_hash({'original':row['binding_digest'],'question':question,'answers':current['answer_refs']})
            current['question']=question;current['state']='waiting';current['waiting_since']=time.time();current['catalog']=catalog if field=='account' else None
            _save(c,row,current)
            c.execute('UPDATE agent_runs SET state=?,boundary=?,result_summary=?,updated_at=? WHERE run_id=? AND state=?',
                (S.WAITING_USER,'',prompt,time.time(),run_id,S.PLANNING))
            return False
        kind=current['values']['kind'];service_name,operation=kind.split('.')
        action={'action_id':'a1','service':service_name,'operation':operation,'account':selected['account'],
            'target':{'calendar_id':selected['resource']} if service_name=='calendar' else {'mailbox':selected['resource'],'to':payload['to']},
            'payload':payload if service_name=='calendar' else {'instruction':binding['objective']+(
                '\nKlarstellung des Auftraggebers: '+current['answers']['instruction'] if 'instruction' in current['answers'] else '')}}
        request=AC.validate_request({'actions':[action]})
        bound=AC.prepare(request,task_id=binding['task_id'],run_id=run_id)
        AC.record_prepared(c,bound,now=time.time())
        cap=bound.capability_grant
        c.execute('UPDATE agent_task_sources SET capabilities=? WHERE run_id=? AND state=?',
            (_json([{'name':cap.name,'version':cap.version,'constraints':cap.constraints}]),run_id,'preparing'))
        current['state']='resolved';current['resolved_request']=request.descriptor
        current['catalog']={'items':catalog,'selected':selected['value'],'source':'unique_configured' if not current['answers'].get('account') else 'owner_answer'}
        _save(c,row,current)
        return True


def check_resolution(ledger,run_id):
    with ledger._open() as c:
        result=_read(c,run_id,active=True)
        if result is None: return True
        row,binding,current=result
        if current['state']!='resolved': return False
        call=_call_read(c,row,binding,current)
        if call is None or call['state']!='ready': raise ValueError('action_intent_unresolved')
        contract=c.execute('SELECT request_json FROM agent_action_contracts WHERE run_id=?',(run_id,)).fetchone()
        if contract is None or json.loads(contract['request_json'])!=current['resolved_request']:
            raise ValueError('action_intent_contract_changed')
        return True


def validate_issue(c,ledger,task_id,run_id,receipt,capabilities):
    """Check inside the very transaction that may insert the first grant."""
    result=_read(c,run_id,active=True)
    if result is None: return
    row,binding,current=result
    source=binding['source']
    if (binding['task_id']!=task_id or receipt.authorizer!=source['authorizer']
            or receipt.reference!=source['receipt_reference'] or receipt.method!=source['receipt_method']):
        raise ValueError('action_intent_grant_source_changed')
    contract=c.execute('SELECT * FROM agent_action_contracts WHERE run_id=?',(run_id,)).fetchone()
    if contract is None: raise ValueError('action_intent_unresolved')
    bound=AC._bound_row(contract)
    validate_bound(c,ledger,bound)
    cap=bound.capability_grant
    if capabilities!=[{'name':cap.name,'version':cap.version,'constraints':cap.constraints}]:
        raise ValueError('action_intent_grant_capabilities_changed')


def validate_bound(c,ledger,bound):
    """The native dispatch/readback retains the original clarification proof."""
    result=_read(c,bound.run_id)
    if result is None: return
    row,binding,current=result
    call=_call_read(c,row,binding,current)
    if current['state']!='resolved' or call is None or call['state']!='ready':
        raise ValueError('action_intent_unresolved')
    if json.loads(bound._request_json)!=current['resolved_request']:
        raise ValueError('action_intent_contract_changed')
    missing,payload=F.resolve(current['values'],current['answers'],anchor=datetime.fromisoformat(binding['anchor']))
    catalog=current['catalog']
    selected=[v for v in catalog['items'] if v['value']==catalog['selected']]
    if missing or len(selected)!=1:
        raise ValueError('action_intent_resolution_changed')
    selection=selected[0]
    if selection['value']!=_hash({k:selection[k] for k in ('service','account','resource')}):
        raise ValueError('action_intent_catalog_changed')
    source=catalog['source']
    if source=='unique_configured':
        if len(catalog['items'])!=1 or current['answers'].get('account'):
            raise ValueError('action_intent_catalog_changed')
    elif source=='owner_answer':
        if current['answers'].get('account')!=selection['value']:
            raise ValueError('action_intent_catalog_changed')
    else:
        raise ValueError('action_intent_catalog_changed')
    kind=current['values']['kind'];service,operation=kind.split('.')
    target={'calendar_id':selection['resource']} if service=='calendar' else {'mailbox':selection['resource'],'to':payload['to']}
    if service=='gmail':
        payload={'instruction':binding['objective']+('\nKlarstellung des Auftraggebers: '+current['answers']['instruction'] if 'instruction' in current['answers'] else '')}
    expected={'actions':[{'action_id':'a1','service':service,'operation':operation,'account':selection['account'],'target':target,'payload':payload}]}
    if current['resolved_request']!=expected:
        raise ValueError('action_intent_resolution_changed')


def refresh_catalog(ledger,run_id,service):
    """An owner resume can only refresh this concrete account-selection wait."""
    with ledger._open() as c:
        c.execute('BEGIN IMMEDIATE')
        result=_read(c,run_id,active=True)
        if result is None: return False
        row,binding,current=result
        question=current['question']
        run=c.execute('SELECT state FROM agent_runs WHERE run_id=?',(run_id,)).fetchone()
        if current['state']!='waiting' or run['state']!=S.WAITING_USER or question is None or question['field']!='account':
            return False
        fresh=_catalog(service,current['values']['kind'])
        if fresh==current['catalog']: return False
        waited=max(0.0,time.time()-current['waiting_since'])
        current['waiting_since']=0.0
        current['question']=None;current['state']='ready';current['revision']+=1
        _save(c,row,current)
        c.execute('UPDATE agent_runs SET state=?,boundary=?,provider_wait_seconds=provider_wait_seconds+?,updated_at=? WHERE run_id=? AND state=?',
            (S.PLANNING,'',waited,time.time(),run_id,S.WAITING_USER))
        return True


def canonical_answer(value):
    from solvio.agent_runtime.task_start_service import request_identifier
    keys={'run_id','question_id','expected_revision','expected_digest','answer','client_request_id'}
    if type(value) is not dict or set(value)!=keys: raise ValueError('invalid_answer')
    if (type(value['run_id']) is not str or not re.fullmatch(r'ar-[a-f0-9]{16}',value['run_id'])
            or type(value['question_id']) is not str or not re.fullmatch(r'aiq-[a-f0-9]{32}',value['question_id'])
            or type(value['expected_revision']) is not int or not 1<=value['expected_revision']<=100
            or type(value['expected_digest']) is not str or not re.fullmatch(r'[a-f0-9]{64}',value['expected_digest'])):
        raise ValueError('invalid_answer')
    text=value['answer']
    if type(text) is not str or text!=text.strip() or not 1<=len(text)<=2000 or any(ord(x)<32 and x not in '\n\t' for x in text):
        raise ValueError('invalid_answer')
    S._refuse_credentials(text,where='action_intent.answer')
    request_identifier(value['client_request_id'])
    _json(value).encode('utf-8')
    return dict(value)


def answer(ledger,body,receipt):
    body=canonical_answer(body)
    from solvio.agent_runtime import research_question as RQST
    run = ledger.get_run(body['run_id'])
    task = ledger.get_task(run.task_id) if run else None
    if task is not None and task.scope == S.SCOPE_RESEARCH:
        return RQST.answer(ledger, body, receipt)
    if type(receipt) is not VerifiedTaskReceipt or receipt.method not in {'dashboard_session','app_session'}:
        raise ValueError('verified_answer_required')
    with ledger._open() as c:
        c.execute('BEGIN IMMEDIATE')
        row,binding,current=_read(c,body['run_id'])
        if receipt.authorizer!=binding['source']['principal']: raise ValueError('not_found')
        prior=c.execute('SELECT * FROM agent_action_intent_answers WHERE run_id=? AND request_id=?',
            (body['run_id'],body['client_request_id'])).fetchone()
        if prior:
            if prior['request_digest']!=_hash(body): raise ValueError('stale_question')
            return json.loads(prior['response_json'])
        _read(c,body['run_id'],active=True)
        question=current['question']
        run=c.execute('SELECT state FROM agent_runs WHERE run_id=?',(body['run_id'],)).fetchone()
        if (current['state']!='waiting' or run['state']!=S.WAITING_USER or not question
                or (question['id'],question['revision'],question['digest'])!=(body['question_id'],body['expected_revision'],body['expected_digest'])):
            raise ValueError('stale_question')
        field=question['field'];text=body['answer']
        if field=='account':
            if text not in [r['value'] for r in current['catalog']]: raise ValueError('invalid_answer')
        else:
            F.validate_answer(field,text,anchor=datetime.fromisoformat(binding['anchor']),
                fields=(current['values'] or {}).get('fields',{})|current['answers'])
        answer_data=body|{'field':field}
        receipt_data={'method':receipt.method,'reference':receipt.reference,'authorizer':receipt.authorizer}
        response={'status':'interpreting','question':None}
        c.execute('INSERT INTO agent_action_intent_answers VALUES (?,?,?,?,?,?,?)',
            (body['run_id'],body['client_request_id'],_hash(body),_json(answer_data),_json(receipt_data),
             _hash({'answer':answer_data,'receipt':receipt_data}),_json(response)))
        current['answers'][field]=text;current['answer_refs'].append(body['client_request_id'])
        waited=max(0.0,time.time()-current['waiting_since'])
        current['waiting_since']=0.0
        current['question']=None;current['revision']+=1;current['state']='ready'
        if field=='instruction':
            current['state']='pending';current['values']=None;current['interpretation']=''
        _save(c,row,current)
        c.execute('UPDATE agent_runs SET state=?,boundary=?,result_summary=?,provider_wait_seconds=provider_wait_seconds+?,updated_at=? WHERE run_id=? AND state=?',
            (S.PLANNING,'','Die Antwort ist angenommen; derselbe Auftrag wird fortgesetzt.',waited,time.time(),body['run_id'],S.WAITING_USER))
        return response


async def advance(orch,run,context):
    from solvio.agent_runtime.cost_dispatch import task_cost_scope
    from solvio.agent_runtime.orchestrator import _ProviderPause, _provider_wait
    context.ledger.paused_seconds=float(orch.ledger.get_run(run.run_id).provider_wait_seconds or 0.0)
    context.ledger.check_step()
    if run.state==S.CREATED:
        orch.ledger.transition(run.run_id,S.PLANNING)
    if resolve_or_ask(orch.ledger,run.run_id,getattr(orch,'action_service',None)):
        orch.task_starts.finish(run.run_id)
        return
    current=view(orch.ledger,run.run_id)
    if current['question'] is not None: return
    route=getattr(orch.planner,'route',{})
    if route.get('billing_mode')!='subscription' or not hasattr(orch.planner,'interpret_action'):
        raise _ProviderPause(PL.PlannerCall(False,reason='provider_unavailable',provider=route.get('provider','unknown')),'action_interpret')
    orch._ensure_provider_route(run.run_id,'action_interpret')
    wait=_provider_wait(orch.ledger.get_run(run.run_id))
    row,fresh=claim(orch.ledger,run.run_id,route['provider'],resume=wait.get('status')=='resuming' and wait.get('phase')=='action_interpret')
    context.ledger.planner_calls=orch.ledger.get_run(run.run_id).planner_calls
    if not fresh:
        if row['state']=='blocked':
            raise _ProviderPause(PL.PlannerCall(**json.loads(row['result_json'])['call']),'action_interpret')
        await orch._finish(run.run_id,S.FAILED,'recovery_required','Die Auftragsklärung wurde nicht sicher abgeschlossen. Es wird kein weiterer Versuch und keine Außenwirkung gestartet.')
        return
    bound=json.loads(row['binding_json'])
    with task_cost_scope(orch.ledger,task_id=run.task_id,run_id=run.run_id,phase='action_interpret',
            operation_id=row['reference'],quote_adapter=orch.cost_quote_adapter,settlement_adapter=orch.cost_settlement_adapter):
        call=await orch.planner.interpret_action(text=bound['text'])
    orch._record_provider_route(run.run_id,call,'action_interpret')
    try:
        settle(orch.ledger,run.run_id,row['reference'],call)
    except (ValueError,TypeError,KeyError):
        await orch._finish(run.run_id,S.FAILED,'recovery_required','Die Auftragsklärung oder ihr Kostenbeleg ist nicht verlässlich. Es wurde keine Außenwirkung begonnen.')
        return
    if not call.ok:
        with orch.ledger._open() as c:
            final=c.execute('SELECT state FROM agent_action_interpretations WHERE reference=?',(row['reference'],)).fetchone()
        if final['state']=='blocked':
            raise _ProviderPause(call,'action_interpret')
        await orch._finish(run.run_id,S.FAILED,'recovery_required' if call.dispatch_started else 'capability_failed',
            'Die Auftragsklärung konnte nicht abgeschlossen werden. Es wurde keine Außenwirkung begonnen.')
        return
    orch._clear_provider_resume(run.run_id,'action_interpret')
    if resolve_or_ask(orch.ledger,run.run_id,getattr(orch,'action_service',None)):
        orch.task_starts.finish(run.run_id)
