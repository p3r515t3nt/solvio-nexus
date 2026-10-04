"""Subscription-written mail content bound to one existing action.

The recipient and purpose are immutable user input. Only subject/body come
from the tool-free text route. A durable claim precedes that route; ready
content is reused after restart, and an interrupted claim never authorizes
another generation. No draft is sent by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import time
import uuid

from solvio.agent_runtime import action_contract as AC, budget as BU, planner as PL, store as S

SCHEMA = """CREATE TABLE IF NOT EXISTS agent_action_compositions (
 reference TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
 resource_id TEXT NOT NULL, contract_digest TEXT NOT NULL, grant_reference TEXT NOT NULL,
 action_id TEXT NOT NULL, action_digest TEXT NOT NULL, ordinal INTEGER NOT NULL,
 provider TEXT NOT NULL, binding_digest TEXT NOT NULL, created_at REAL NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('claimed','ready','blocked','failed')),
 subject TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
 content_digest TEXT NOT NULL DEFAULT '', call_json TEXT NOT NULL DEFAULT '',
 result_digest TEXT NOT NULL DEFAULT '',
 UNIQUE(task_id,action_id,ordinal)
);"""
_BINDING = ('reference','task_id','run_id','resource_id','contract_digest',
    'grant_reference','action_id','action_digest','ordinal','provider','created_at')
_RESULT = ('state','subject','body','content_digest','call_json')


@dataclass(frozen=True)
class Composition:
    reference: str
    content_digest: str
    subject: str
    body: str


def _hash(value):
    return AC._hash('SOLVIO_ACTION_COMPOSITION_V1', value)


def _content(value):
    if type(value) is not dict or set(value) != {'subject','body'}:
        raise ValueError('action_composition_fields_invalid')
    for key, limit in (('subject',500),('body',8000)):
        value_at = value[key]
        if (type(value_at) is not str or not value_at.strip() or len(value_at)>limit
                or any(ord(c)<32 and c not in '\n\t' for c in value_at)
                or any(0xD800<=ord(c)<=0xDFFF for c in value_at)
                or (key=='subject' and any(c in '\r\n' for c in value_at))):
            raise ValueError('action_composition_text_invalid')
        S._refuse_credentials(value_at,where='action_composition')
    return value


def _exists(connection):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_action_compositions'").fetchone() is not None


def _cost_proof(connection, row, call):
    invocation = connection.execute('SELECT * FROM agent_provider_invocations WHERE invocation_id=? AND task_id=?',
        (call.get('cost_invocation_id',''), row['task_id'])).fetchone()
    reservation = (connection.execute('SELECT * FROM agent_cost_reservations WHERE reservation_id=?',
        (call.get('cost_reservation_id',''),)).fetchone())
    if (invocation is None or reservation is None or invocation['state']!='finished'
            or invocation['run_id']!=row['run_id'] or invocation['phase']!='action_compose'
            or invocation['operation_id']!=row['reference'] or invocation['provider']!=row['provider']
            or invocation['reservation_id']!=reservation['reservation_id']
            or reservation['state']!='settled' or reservation['subject_id']!=row['task_id']
            or reservation['invocation_id']!=invocation['invocation_id']):
        raise ValueError('action_composition_cost_unverified')


def _read(connection, ledger, bound, action):
    if (action not in bound.actions or action['service']!='gmail'
            or action['operation']!='compose_draft'):
        raise ValueError('action_composition_not_bound')
    if not _exists(connection):
        return None
    row = connection.execute('SELECT * FROM agent_action_compositions WHERE task_id=? AND action_id=? ORDER BY ordinal DESC LIMIT 1',
        (bound.task_id, action['action_id'])).fetchone()
    if row is None:
        return None
    if (action not in bound.actions or action['service']!='gmail' or action['operation']!='compose_draft'
            or any(row[k]!=getattr(bound,k) for k in ('task_id','run_id','resource_id','contract_digest','grant_reference'))
            or row['action_digest']!=AC._hash('SOLVIO_ACTION_V1',action)
            or row['binding_digest']!=_hash({k:row[k] for k in _BINDING})):
        raise ValueError('action_composition_binding_changed')
    if row['state']=='claimed':
        if any(row[k] for k in ('subject','body','content_digest','call_json','result_digest')):
            raise ValueError('action_composition_claim_changed')
        return row
    if row['result_digest']!=_hash(dict(binding_digest=row['binding_digest'], **{k:row[k] for k in _RESULT})):
        raise ValueError('action_composition_result_changed')
    call=json.loads(row['call_json'])
    if row['state']=='ready':
        content=_content({'subject':row['subject'],'body':row['body']})
        if (row['content_digest']!=_hash(dict(reference=row['reference'],**content))
                or call.get('ok') is not True or call.get('provider')!=row['provider']
                or call.get('billing_mode')!='subscription'):
            raise ValueError('action_composition_content_changed')
        _cost_proof(connection,row,call)
    elif any(row[k] for k in ('subject','body','content_digest')):
        raise ValueError('action_composition_unready_content')
    return row


def read_composed(ledger, bound, action, *, active=True):
    with ledger._open() as connection:
        connection.execute('BEGIN')
        bound=AC._with_grant(connection,ledger,bound,active=active)
        row=_read(connection,ledger,bound,action)
        return (Composition(row['reference'],row['content_digest'],row['subject'],row['body'])
                if row is not None and row['state']=='ready' else None)


def original_instruction(ledger, run_id, action_id):
    """Fresh full user purpose for assessment, never a provider's paraphrase."""
    with ledger._open() as connection:
        connection.execute('BEGIN')
        contract=connection.execute('SELECT * FROM agent_action_contracts WHERE run_id=?',(run_id,)).fetchone()
        if contract is None:
            raise ValueError('action_contract_missing')
        bound=AC._with_grant(connection,ledger,AC._bound_row(contract),active=False)
        action=next(a for a in bound.actions if a['action_id']==action_id)
        row=_read(connection,ledger,bound,action)
        if row is None or row['state']!='ready':
            raise ValueError('action_composition_missing')
        return {'action_id':action_id,'recipient':action['target']['to'],
            'instruction':action['payload']['instruction']}


def validate_native_receipt(connection,ledger,bound,action,body):
    if action['operation']!='compose_draft' or body['status']!='completed':
        return
    row=_read(connection,ledger,bound,action)
    native=body['native']['observed']
    draft=native.get('draft') or {}
    if (row is None or row['state']!='ready'
            or native.get('composition_reference')!=row['reference']
            or native.get('content_digest')!=row['content_digest']
            or (native.get('confirmed') is True and (draft.get('subject')!=row['subject']
                or str(draft.get('body','')).rstrip('\r\n')!=row['body'].rstrip('\r\n')))):
        raise ValueError('action_native_composition_changed')


def claim(ledger,bound,action,provider,*,resume=False):
    """Reserve the existing planner count and text attempt in one transaction."""
    with ledger._open() as connection:
        if provider not in {'codex','claude-code'}:
            raise ValueError('action_composition_provider_unavailable')
        connection.executescript(SCHEMA)
        connection.execute('BEGIN IMMEDIATE')
        bound=AC._with_grant(connection,ledger,bound,active=True)
        prior=_read(connection,ledger,bound,action)
        if prior is not None and (prior['state']!='blocked' or not resume):
            return dict(prior),False
        run=connection.execute('SELECT * FROM agent_runs WHERE run_id=?',(bound.run_id,)).fetchone()
        if run is None or run['state']!=S.RUNNING:
            raise ValueError('action_composition_run_not_running')
        if run['planner_calls']>=BU.MAX_PLANNER_CALLS_PER_RUN:
            raise BU.BudgetExhausted('budget_exhausted','composition_calls')
        row=dict(reference='acmp-'+uuid.uuid4().hex,task_id=bound.task_id,run_id=bound.run_id,
            resource_id=bound.resource_id,contract_digest=bound.contract_digest,
            grant_reference=bound.grant_reference,action_id=action['action_id'],
            action_digest=AC._hash('SOLVIO_ACTION_V1',action),ordinal=prior['ordinal']+1 if prior else 1,
            provider=provider,created_at=time.time())
        row['binding_digest']=_hash(row)
        row['state']='claimed'
        columns=tuple(row)
        connection.execute('INSERT INTO agent_action_compositions ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',tuple(row.values()))
        connection.execute('UPDATE agent_runs SET planner_calls=planner_calls+1 WHERE run_id=?',(bound.run_id,))
        return row,True


def settle(ledger,bound,action,reference,call,*,blocked=False):
    """Validate and preserve text, including its actual settled provider claim."""
    fields={k:getattr(call,k) for k in PL.PlannerCall.__dataclass_fields__}
    content=_content(PL.call_payload(call)) if call.ok else None
    with ledger._open() as connection:
        connection.execute('BEGIN IMMEDIATE')
        bound=AC._with_grant(connection,ledger,bound,active=True)
        row=_read(connection,ledger,bound,action)
        if row is None or row['reference']!=reference or row['state']!='claimed':
            raise ValueError('action_composition_claim_lost')
        result=dict(state='ready' if content else 'blocked' if blocked else 'failed',
            subject=content['subject'] if content else '',body=content['body'] if content else '',
            content_digest=_hash(dict(reference=reference,**content)) if content else '',
            call_json=json.dumps(fields,sort_keys=True,separators=(',',':')))
        if content:
            _cost_proof(connection,row,fields)
        result['result_digest']=_hash(dict(binding_digest=row['binding_digest'],**result))
        connection.execute('UPDATE agent_action_compositions SET '+','.join(k+'=?' for k in result)+' WHERE reference=? AND state=\'claimed\'',
            (*result.values(),reference))


async def ensure_composed(orch,run,context,planned):
    """Prepare content before the native action step/claim can be created."""
    bound=AC.for_run(orch.ledger,run.run_id)
    action=next(a for a in bound.actions if a['action_id']==planned.arguments['action_id'])
    if action['operation']!='compose_draft':
        return True
    from solvio.agent_runtime.orchestrator import PROVIDER_BLOCKERS, _ProviderPause, _provider_wait
    from solvio.agent_runtime.cost_dispatch import task_cost_scope
    # Finished text no longer needs a text provider. Its immutable cost and
    # content receipt must still verify; final assessment keeps its own route.
    if read_composed(orch.ledger,bound,action) is not None:
        orch._clear_provider_resume(run.run_id,'action_compose')
        return True
    route=getattr(orch.planner,'route',{})
    if route.get('billing_mode')!='subscription' or not hasattr(orch.planner,'compose_draft'):
        raise _ProviderPause(PL.PlannerCall(False,reason='provider_unavailable',provider=route.get('provider','unknown')),'action_compose')
    orch._ensure_provider_route(run.run_id,'action_compose')
    wait=_provider_wait(orch.ledger.get_run(run.run_id))
    row,fresh=claim(orch.ledger,bound,action,route['provider'],resume=wait.get('status')=='resuming' and wait.get('phase')=='action_compose')
    context.ledger.planner_calls=orch.ledger.get_run(run.run_id).planner_calls
    if not fresh:
        if row['state']=='ready':
            orch._clear_provider_resume(run.run_id,'action_compose')
            return True
        if row['state']=='blocked':
            raise _ProviderPause(PL.PlannerCall(**json.loads(row['call_json'])),'action_compose')
        await orch._finish(run.run_id,S.FAILED,'recovery_required' if row['state']=='claimed' else 'capability_failed',
            'Der Textentwurf konnte nicht sicher abgeschlossen werden. Es wurde kein neuer Mailentwurf angelegt.')
        return False
    with task_cost_scope(orch.ledger,task_id=run.task_id,run_id=run.run_id,
            phase='action_compose',operation_id=row['reference'],
            quote_adapter=orch.cost_quote_adapter,settlement_adapter=orch.cost_settlement_adapter):
        call=await orch.planner.compose_draft(recipient=action['target']['to'],instruction=action['payload']['instruction'])
    orch._record_provider_route(run.run_id,call,'action_compose')
    blocked=not call.ok and call.reason in PROVIDER_BLOCKERS
    try:
        settle(orch.ledger,bound,action,row['reference'],call,blocked=blocked)
    except (ValueError,TypeError,KeyError):
        await orch._finish(run.run_id,S.FAILED,'recovery_required',
            'Der erzeugte Text oder sein Ausführungsbeleg ist nicht verlässlich. Es wurde kein Mailentwurf angelegt.')
        return False
    if blocked:
        raise _ProviderPause(call,'action_compose')
    if not call.ok:
        await orch._finish(run.run_id,S.FAILED,'capability_failed','Der Text konnte nicht verfasst werden. Es wurde kein Mailentwurf angelegt.')
        return False
    orch._clear_provider_resume(run.run_id,'action_compose')
    return True
