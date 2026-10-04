"""Closed compose schema and native adapter boundaries; no real mail/model calls."""
from __future__ import annotations
from copy import deepcopy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_agent_action_services import native_fixture, action_fixture
from test_agent_action_composition import composition_world, prepare, DC
from solvio.agent_runtime import action_contract as AC, cost_dispatch as CD, store as S
from solvio.agent_runtime.task_start_service import TaskStepAuthority
from solvio.capabilities import task_action as TA


def action(account='gmail-fixture'):
    return {'action_id':'a1','service':'gmail','operation':'compose_draft','account':account,
        'target':{'mailbox':'me','to':'recipient@example.invalid'},
        'payload':{'instruction':'Verfasse eine freundliche Terminfrage.'}}


def rejected(call):
    try: call()
    except ValueError: return
    raise AssertionError('unbound compose fields were accepted')


def t_compose_requires_only_bound_recipient_and_bounded_purpose():
    valid=action()
    require_equal(AC.from_payload({'actions':[valid]}).actions[0],valid)
    changes=[('target',{'to':''}),('target',{'to':'first@example.invalid, second@example.invalid'}),
        ('target',{'reply_to_message':'message1'}),('target',{'mailbox':'another'}),
        ('payload',{'send':True}),('payload',{'subject':'User subject'}),
        ('payload',{'to':'other@example.invalid'}),('payload',{'instruction':''}),
        ('payload',{'instruction':'x'*4001})]
    for field,values in changes:
        changed=deepcopy(valid); changed[field].update(values)
        rejected(lambda:AC.from_payload({'actions':[changed]}))
    allowed=deepcopy(valid); allowed['payload']['instruction']='x'*4000
    require_equal(len(AC.from_payload({'actions':[allowed]}).actions[0]['payload']['instruction']),4000)


async def t_unprepared_compose_cannot_fall_back_to_instruction_as_body():
    with native_fixture() as n, action_fixture(n,action(TA.account_identity('gmail',n.gmail))) as w:
        outcome=await w.adapter.execute(w.arguments,w.binding)
        require_equal(outcome.state,'not_dispatched')
        require_equal(n.transport.calls,[])
        require_equal(n.transport.mutations,[])


async def t_native_quote_binds_ready_composition_and_rejects_changed_content():
    async with composition_world() as w:
        run_id=await prepare(w)
        context=w.orch._contexts[run_id]
        require(await DC.ensure_composed(w.orch,w.ledger.get_run(run_id),context,context.plan.steps[0]))
        bound=AC.for_run(w.ledger,run_id)
        composed=DC.read_composed(w.ledger,bound,bound.actions[0])
        # Resource/quote inspection alone claims no step and cannot dispatch.
        binding=TaskStepAuthority(bound.grant_reference,bound.task_id,run_id,'inspection-only')
        arguments=bound.action_arguments('a1')
        resources=w.orch.action_service.resources(TA.SPEC,arguments,binding)
        require_equal(resources['composition'],{'reference':composed.reference,'content_digest':composed.content_digest})
        invocation=CD.ServiceInvocation.bind(capability=TA.SPEC.name,version=1,
            service='native.task-action',operation='execute',arguments=arguments,resources=resources)
        quote=w.orch.action_service.quote('native.task-action',invocation)
        require_equal(quote.upper_bound_cents,0)
        require(quote.validate_before_dispatch())
        with w.ledger._open() as c:
            c.execute("UPDATE agent_action_compositions SET body='Changed persisted content'")
        require(not quote.validate_before_dispatch())
        require_equal(w.native.transport.mutations,[])


async def t_composed_native_mismatch_cannot_become_success_evidence():
    async with composition_world() as w:
        run_id=await prepare(w)
        w.native.transport.tamper_after_write=True
        await w.finish(run_id,S.FAILED)
        require_equal(len(w.native.transport.mutations),1)
        require_equal(AC.completion_evidence(w.ledger,run_id),())
        observed=AC.read_receipts(w.ledger,run_id)[0]['native']['observed']
        require_equal(observed['confirmed'],False)
        require(observed['composition_reference'].startswith('acmp-'))
        require_equal(len(observed['content_digest']),64)
        require(not any(url.endswith('/send') for _,url in w.native.transport.calls))


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
