"""Public detached purpose-to-draft flow through the native subscription seam.

Only the CLI's protocol answers and native REST transport are synthetic.
There is no real model call, email, new authority or alternative runtime.
"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','src'))
sys.path.insert(0,os.path.dirname(__file__))
from _guard import enforce_assertions,require,require_equal
enforce_assertions()
from test_agent_action_execution import world,admit
from solvio.agent_runtime import action_contract as AC, action_draft_composition as DC
from solvio.agent_runtime import store as S, cost_dispatch as CD, costs as C

CONTENT={'subject':'Termin abstimmen','body':'Fixture body: Bitte schlage einen Termin für das Gespräch vor.'}


@asynccontextmanager
async def composition_world(*,reply=None):
    async with world('gmail') as w:
        action=w.body['action_request']['actions'][0]
        action['operation']='compose_draft'
        action['target']={'mailbox':'me','to':'recipient@example.invalid'}
        action['payload']={'instruction':'Bitte verfasse eine freundliche Bitte um einen Gesprächstermin.'}
        w.body['objective']='Verfasse zu meinem Anliegen einen Entwurf an den angegebenen Empfänger.'
        source=w.executable.read_text()
        old="kind = 'assessment' if 'ergebnis' in request else 'plan'"
        require_equal(source.count(old),1)
        source=source.replace(old,"kind = 'assessment' if 'ergebnis' in request else 'composition' if request.get('ziel') == 'mailentwurf_verfassen' else 'plan'")
        w.executable.write_text(source)
        fixture=json.loads((w.folder/'fixture.json').read_text())
        fixture['composition']=[CONTENT if reply is None else reply]
        (w.folder/'fixture.json').write_text(json.dumps(fixture))
        yield w


def composed_calls(w):
    return [c for c in w.calls() if c['kind']=='composition']


async def prepare(w):
    run_id=await admit(w)
    await w.orch.tick()
    await w.orch.tick()
    require_equal(w.ledger.get_run(run_id).state,S.RUNNING)
    require_equal(w.calls(),[])
    return run_id


async def t_purpose_becomes_native_draft_after_tab_closes_same_grant_no_send():
    async with composition_world() as w:
        run_id=await prepare(w)
        grant=w.orch.task_authority.for_run(run_id)
        await w.detach()
        await w.finish(run_id)
        require_equal(w.orch.task_authority.for_run(run_id).reference,grant.reference)
        require_equal(len(composed_calls(w)),1)
        require_equal(len(w.native.transport.mutations),1)
        require(w.native.transport.mutations[0][1].endswith('/drafts'))
        receipt=AC.read_receipts(w.ledger,run_id)[0]
        observed=receipt['native']['observed']
        require_equal(observed['draft']['subject'],CONTENT['subject'])
        require_equal(observed['draft']['body'].rstrip(),CONTENT['body'])
        require_equal(observed['draft']['to'],'recipient@example.invalid')
        require_equal(observed['sent'],False)
        require(observed['composition_reference'].startswith('acmp-'))
        require_equal(w.ledger.get_run(run_id).planner_calls,1)
        assessment=[call for call in w.calls() if call['kind']=='assessment'][0]
        require(w.body['action_request']['actions'][0]['payload']['instruction'] in assessment['request']['ergebnis'],
            'Final assessment must see the full bound purpose, not only the written draft.')
        w.runtime()
        await w.orch.reconcile()
        await w.orch.tick()
        require_equal(len(w.native.transport.mutations),1)
        require_equal(len(composed_calls(w)),1)


async def t_crash_after_ready_before_native_step_reuses_text_once():
    async with composition_world() as w:
        run_id=await prepare(w)
        run=w.ledger.get_run(run_id)
        context=w.orch._contexts[run_id]
        require(await DC.ensure_composed(w.orch,run,context,context.plan.steps[0]))
        require_equal(len(w.native.transport.mutations),0)
        require_equal(len(composed_calls(w)),1)
        w.runtime()
        await w.orch.reconcile()
        await w.finish(run_id)
        require_equal(len(composed_calls(w)),1)
        require_equal(len(w.native.transport.mutations),1)


async def t_crash_after_text_claim_never_regenerates_or_creates_draft():
    async with composition_world() as w:
        run_id=await prepare(w)
        bound=AC.for_run(w.ledger,run_id)
        _,fresh=DC.claim(w.ledger,bound,bound.actions[0],'codex')
        require(fresh)
        w.runtime()
        await w.orch.reconcile()
        await w.finish(run_id,S.FAILED)
        require_equal(w.ledger.get_run(run_id).failure_category,'recovery_required')
        require_equal(w.calls(),[])
        require_equal(w.native.transport.mutations,[])


async def t_initial_composition_claim_refuses_changed_recipient_before_budget_or_model():
    async with composition_world() as w:
        run_id=await prepare(w)
        bound=AC.for_run(w.ledger,run_id)
        action=bound.actions[0]
        action['target']['to']='other@example.invalid'
        rejected=False
        try:
            DC.claim(w.ledger,bound,action,'codex')
        except ValueError:
            rejected=True
        require(rejected)
        require_equal(w.ledger.get_run(run_id).planner_calls,0)
        with w.ledger._open() as connection:
            require_equal(connection.execute('SELECT count(*) FROM agent_action_compositions').fetchone()[0],0)
        require_equal(w.calls(),[])


async def t_ready_content_can_be_written_without_regenerating_from_unavailable_planner():
    async with composition_world() as w:
        run_id=await prepare(w)
        context=w.orch._contexts[run_id]
        require(await DC.ensure_composed(w.orch,w.ledger.get_run(run_id),context,context.plan.steps[0]))
        w.orch.planner=None
        await w.orch.tick()
        require_equal(len(composed_calls(w)),1)
        require_equal(len(w.native.transport.mutations),1)
        require_equal(AC.read_receipts(w.ledger,run_id)[0]['status'],'completed')


async def t_unsettled_actual_text_cost_cannot_authorize_native_write():
    async with composition_world() as w:
        run_id=await prepare(w)
        context=w.orch._contexts[run_id]
        require(await DC.ensure_composed(w.orch,w.ledger.get_run(run_id),context,context.plan.steps[0]))
        with w.ledger._open() as connection:
            connection.execute("UPDATE agent_cost_reservations SET state='unknown' WHERE reservation_id IN "
                "(SELECT reservation_id FROM agent_provider_invocations WHERE phase='action_compose')")
        await w.finish(run_id,S.FAILED)
        require_equal(len(composed_calls(w)),1)
        require_equal(w.native.transport.mutations,[])


async def t_model_cannot_add_recipient_or_send_action():
    async with composition_world(reply={**CONTENT,'to':'other@example.invalid','send':True}) as w:
        run_id=await prepare(w)
        await w.finish(run_id,S.FAILED)
        require_equal(len(composed_calls(w)),1)
        require_equal(w.native.transport.mutations,[])
        require_equal(AC.completion_evidence(w.ledger,run_id),())


async def t_changed_ready_text_is_not_authority_for_a_draft():
    async with composition_world() as w:
        run_id=await prepare(w)
        context=w.orch._contexts[run_id]
        require(await DC.ensure_composed(w.orch,w.ledger.get_run(run_id),context,context.plan.steps[0]))
        with w.ledger._open() as connection:
            connection.execute("UPDATE agent_action_compositions SET body='Changed text'")
        await w.finish(run_id,S.FAILED)
        require_equal(len(composed_calls(w)),1)
        require_equal(w.native.transport.mutations,[])


async def t_cost_boundary_before_generation_resumes_same_task_with_no_extra_grant():
    async with composition_world() as w:
        run_id=await prepare(w)
        original_quote=w.orch.cost_quote_adapter
        w.orch.cost_quote_adapter=lambda *args:CD.CostQuote()
        await w.orch.tick()
        run=w.ledger.get_run(run_id)
        require_equal(run.state,S.WAITING_USER)
        require_equal(json.loads(run.boundary)['provider_wait']['phase'],'action_compose')
        require_equal(w.calls(),[])
        require_equal(w.native.transport.mutations,[])
        w.runtime()
        await w.orch.reconcile()
        require(await w.orch.resume(run_id))
        await w.finish(run_id)
        require_equal(len(composed_calls(w)),1)
        require_equal(len(w.native.transport.mutations),1)
        require_equal(w.ledger.get_run(run_id).planner_calls,2)


async def t_ten_euro_boundary_precedes_text_and_every_native_effect():
    async with composition_world() as w:
        run_id=await prepare(w)
        w.orch.cost_quote_adapter=lambda *args:CD.CostQuote(1000,
            C.CostEvidence('enforceable_upper_bound','synthetic:bounded-composition'))
        await w.orch.tick()
        run=w.ledger.get_run(run_id)
        require_equal(run.state,S.WAITING_USER)
        require_equal(json.loads(run.boundary)['provider_wait']['reason'],'cost_approval_required')
        require_equal(w.calls(),[])
        require_equal(w.native.transport.mutations,[])


async def t_native_subscription_quota_holds_until_owner_resumes():
    async with composition_world() as w:
        run_id=await prepare(w)
        original=w.executable.read_text()
        marker='    choices = fixture[kind]'
        require_equal(original.count(marker),1)
        w.executable.write_text(original.replace(marker,
            "    if kind == 'composition':\n"
            "        print(json.dumps({'type':'turn.failed','error':{'message':'rate limit exceeded quota'}}))\n"
            "        raise SystemExit(0)\n"+marker))
        await w.orch.tick()
        run=w.ledger.get_run(run_id)
        require_equal(run.state,S.WAITING_USER)
        require_equal(json.loads(run.boundary)['provider_wait']['reason'],'quota')
        require_equal(w.native.transport.mutations,[])
        await w.orch.tick()
        require_equal(len(composed_calls(w)),1)
        w.executable.write_text(original)
        w.runtime()
        await w.orch.reconcile()
        require(await w.orch.resume(run_id))
        await w.finish(run_id)
        require_equal(len(composed_calls(w)),2)
        require_equal(len(w.native.transport.mutations),1)


async def t_cancellation_while_text_is_in_flight_prevents_native_write():
    async with composition_world() as w:
        run_id=await prepare(w)
        fixture=json.loads((w.folder/'fixture.json').read_text())
        fixture['wait']=True
        (w.folder/'fixture.json').write_text(json.dumps(fixture))
        job=asyncio.create_task(w.orch.tick())
        for _ in range(100):
            if composed_calls(w):
                break
            await asyncio.sleep(.02)
        require_equal(len(composed_calls(w)),1)
        require(await w.orch.cancel(run_id))
        await job
        require_equal(w.ledger.get_run(run_id).state,S.CANCELLED)
        require_equal(w.native.transport.mutations,[])
        require_equal(len(composed_calls(w)),1)


async def t_revoked_grant_after_text_result_prevents_native_write():
    async with composition_world() as w:
        run_id=await prepare(w)
        original=w.orch.planner.compose_draft
        async def revoke_then_return(**kwargs):
            call=await original(**kwargs)
            grant=w.orch.task_authority.for_run(run_id)
            require(w.orch.task_authority.revoke(grant.reference,'synthetic-owner-revocation'))
            return call
        with patch.object(w.orch.planner,'compose_draft',side_effect=revoke_then_return):
            await w.orch.tick()
        require(w.ledger.get_run(run_id).terminal)
        require_equal(w.native.transport.mutations,[])
        require_equal(len(composed_calls(w)),1)


async def t_lost_native_reply_keeps_one_text_and_one_write_unknown():
    async with composition_world() as w:
        run_id=await prepare(w)
        w.native.transport.drop_after_write=True
        await w.finish(run_id,S.FAILED)
        w.runtime()
        await w.orch.reconcile()
        await w.orch.tick()
        require_equal(len(composed_calls(w)),1)
        require_equal(len(w.native.transport.mutations),1)
        require_equal(AC.read_receipts(w.ledger,run_id)[0]['status'],'unknown')


if __name__=='__main__':
    from _harness import run_module
    run_module(globals(), __name__)
