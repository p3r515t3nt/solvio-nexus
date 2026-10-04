"""Public Live WS -> actual subscription CLI -> Core task grant, composed.

Only the external Live peer and the native executable's answer are local test
fixtures. LiveBackend, SubscriptionTransport, Launcher, costs, conversation
storage, authenticated HTTP/WS and task authorization are the real components.
No actual model, microphone, provider request, or permanent store is involved.
"""
from contextlib import asynccontextmanager
from dataclasses import replace
import asyncio
import json
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_gpt_live_session as H
import test_live_backend as N
from solvio.agent_runtime.voice_delegate import LiveBackend
from solvio.agent_runtime import cost_dispatch as D, costs as C, store as S
from solvio.specialists import subscription as U, providers as P

ANSWER={'calls':[{'name':'agent_task_research','arguments':{'objective':H.TEXT}}],'clarification':''}


@asynccontextmanager
async def world(*,quota=False,delay=False,sleep=False):
    # Reuse the actual local process/native JSON fixture. Its first temporary
    # ledger is unused; only the public Core's existing ledger binds execution.
    with N.world(ANSWER,quota=quota,sleep=sleep) as (root,_,native,launches):
        original=U.text_invocation
        def delayed(*args,**kwargs):
            inv=original(*args,**kwargs)
            return replace(inv,argv=('-c','import time; time.sleep(.2); '+inv.argv[1]))
        with patch.object(U,'text_invocation',delayed if delay else original), \
                patch.object(P,'claude_status',AsyncMock(side_effect=AssertionError('provider fallback forbidden'))):
            async with H.world() as w:
                w.server.dispatcher.live_backend=LiveBackend(w.ledger,transport=native.transport,
                    quote_adapter=native.quote_adapter)
                w.native_root,w.native_launches=root,launches
                yield w


def claims(w):
    with w.ledger._open() as db:
        return [dict(r) for r in db.execute('SELECT * FROM agent_provider_invocations ORDER BY claimed_at')]


def commentary(w):
    return [e for e in w.providers[0].sent if e['type']=='session.commentary.append']


async def fragments(w):
    provider=w.providers[0]
    # Notice precedes context; the final sentence consists of two real parser
    # events. No fabricated LiveSelection enters the Core.
    await provider.events.put(H.delegate())
    await provider.events.put(H.transcript('Vergleiche drei Hotels ',0,450,'fragment-1'))
    await provider.events.put(H.transcript('in Hamburg mit belastbaren Quellen.',450,1000,'fragment-2'))
    await provider.events.put(H.delegate())  # Exact duplicate notice is inert.


async def t_fragmented_public_delegation_claims_once_and_real_task_survives_close_and_reopen():
    async with world() as w:
        ws,_=await H.H.started(w)
        await fragments(w)
        await H.until(lambda:len(w.ledger.recent_runs())==1,seconds=4)
        await H.until(lambda:bool(commentary(w)),seconds=4)
        run=w.ledger.recent_runs()[0];task=w.ledger.get_task(run.task_id)
        require_equal(task.objective,H.TEXT)
        require_equal(task.created_origin,'trusted_dashboard')
        grant=w.orch.task_authority.for_run(run.run_id)
        require_equal(grant.receipt_method,'dashboard_session')
        require_equal(await w.store.list_pending(),[])
        require_equal(len(w.native_launches),1)
        native=claims(w);require_equal(len(native),1)
        require_equal((native[0]['phase'],native[0]['state']),('voice_delegate','finished'))
        require_equal(native[0]['task_id'],None) # Selection is not a fake task.
        with w.ledger._open() as db:
            activity=dict(db.execute('SELECT * FROM agent_cost_activities WHERE activity_id=?',
                (native[0]['activity_id'],)).fetchone())
            money=dict(db.execute('SELECT * FROM agent_cost_reservations WHERE reservation_id=?',
                (native[0]['reservation_id'],)).fetchone())
        require_equal((activity['purpose'],activity['source_kind'],activity['state']),
            ('voice_delegate','dashboard','completed'))
        require_equal(activity['principal'],task.created_principal)
        require_equal((money['state'],money['actual_cents']),('settled',0))
        stored=w.server.conversations.message(activity['conversation_id'],activity['message_id'])
        require_equal(stored['text'],H.TEXT);require_equal(stored['role'],'user')
        require_equal(stored['source_session_id'],w.sessions[0].session_id)
        result=w.sessions[0]._tool_results[0]
        require(result['success']);require_equal(result['data']['run_id'],run.run_id)
        prefix='Geprüfte Core-Auskunft: '
        updates=commentary(w)
        require(all(e['content'].startswith(prefix) for e in updates))
        actual=''.join(e['content'][len(prefix):] for e in updates)
        # The shared Dispatcher currently returns its verified JSON payload for
        # this tool. The Live commentary must preserve exactly that result.
        require_equal(json.loads(actual),result)
        await H.end(w,ws)
        reopened=S.AgentRunLedger(w.ledger.path)
        require_equal(len(reopened.recent_runs()),1)
        require_equal(reopened.get_task(task.task_id).objective,H.TEXT)
        require(w.orch.task_authority.active(grant.reference,task_id=run.task_id,run_id=run.run_id).allowed)
        require_equal(len(claims(w)),1);require_equal(len(w.native_launches),1)
        require_equal(P.claude_status.call_count,0)


async def t_public_native_quota_returns_boundary_without_task_or_fallback_or_notice_retry():
    async with world(quota=True) as w:
        ws,_=await H.H.started(w)
        await fragments(w)
        await H.until(lambda:bool(commentary(w)),seconds=4)
        require_equal(w.ledger.recent_runs(),[]);require_equal(await w.store.list_pending(),[])
        require_equal(len(w.native_launches),1)
        require('Kontingent' in json.dumps(commentary(w),ensure_ascii=False))
        require_equal(claims(w)[0]['state'],'finished')
        await w.providers[0].events.put(H.delegate())
        await asyncio.sleep(.05)
        require_equal(len(w.native_launches),1);require_equal(P.claude_status.call_count,0)
        require_equal(len(claims(w)),1)
        await H.end(w,ws)
        require_equal(S.AgentRunLedger(w.ledger.path).recent_runs(),[])


async def t_new_fragment_while_real_cli_runs_discards_old_selection_before_task_authority():
    async with world(delay=True) as w:
        ws,_=await H.H.started(w)
        await fragments(w)
        await H.until(lambda:len(w.native_launches)==1,seconds=4)
        before=w.sessions[0]._revision
        await w.providers[0].events.put(H.transcript(' Nein, noch nicht starten.',1000,1600,'correction'))
        await H.until(lambda:w.sessions[0]._revision>before)
        await asyncio.wait_for(w.sessions[0]._tool_queue.join(),4)
        require_equal(w.ledger.recent_runs(),[]);require_equal(await w.store.list_pending(),[])
        require_equal(len(w.native_launches),1)
        require_equal(claims(w)[0]['state'],'finished')
        require_equal(commentary(w),[])
        await H.end(w,ws)


async def t_public_cost_hold_explains_subscription_boundary_before_any_agent_or_search():
    async with world() as w:
        w.server.dispatcher.live_backend.quote_adapter = lambda *_: D.CostQuote()
        ws, _ = await H.H.started(w)
        await fragments(w)
        await H.until(lambda: bool(commentary(w)), seconds=4)
        message = ' '.join(row['content'] for row in commentary(w))
        require('Kostenprüfung' in message, message)
        require('Abo' in message, message)
        require('nicht gestartet' in message, message)
        require('nicht verfügbar' not in message, message)
        require_equal(w.native_launches, [])
        require_equal(claims(w), [])
        require_equal(w.ledger.recent_runs(), [])
        require_equal(P.claude_status.call_count, 0)
        await H.end(w, ws)


async def t_public_close_reaps_claimed_native_child_and_preserves_unknown_without_task():
    async with world(sleep=True) as w:
        ws,_=await H.H.started(w)
        await fragments(w)
        await H.until(lambda:(w.native_root/'pid').exists(),seconds=4)
        pid=int((w.native_root/'pid').read_text())
        require_equal(claims(w)[0]['state'],'claimed')
        await H.end(w,ws)
        try:os.kill(pid,0)
        except ProcessLookupError:pass
        else:raise AssertionError('closed Live session retained its native process')
        require_equal(w.ledger.recent_runs(),[]);require_equal(await w.store.list_pending(),[])
        native=claims(w);require_equal(len(native),1);require_equal(native[0]['state'],'unknown')
        require_equal(C.CostLedger(w.ledger).view_subject(native[0]['subject_id'])['counts'],{'unknown':1})
        require_equal(commentary(w),[]);require_equal(len(w.native_launches),1)


async def t_information_question_through_native_selector_returns_weather_to_same_voice_chat():
    question = 'Wie ist das Wetter heute in Dietzenbach'
    weather = 'In Dietzenbach sind es 23 Grad; später ist Regen möglich.'
    async def issued(w, client=None, headers=None):
        chat, _ = w.server.conversations.create_conversation(owner_principal='local-owner',
            client_request_id='native-weather-question')
        response = await (client or w.client).post(H.H.V.SESSION_PATH,
            json={'conversation_id': chat['conversation_id']}, headers=headers or w.headers)
        require_equal(response.status, 201)
        return await response.json()
    from solvio.realtime import live_session as L
    selection = {'calls':[{'name':'agent_task_research',
        'arguments':{'objective':'Ermittle das aktuelle Wetter.'}}], 'clarification':''}
    with patch.dict(ANSWER, selection, clear=True), patch.object(H.H, 'issued', issued), \
            patch.object(L, 'RESEARCH_POLL_SECONDS', .01):
        async with world() as w:
            ws, _ = await H.H.started(w)
            await w.providers[0].events.put(H.transcript(question))
            await w.providers[0].events.put(H.delegate())
            await H.until(lambda: len(w.ledger.recent_runs()) == 1, seconds=4)
            await asyncio.wait_for(w.sessions[0]._tool_queue.join(), 2)
            run = w.ledger.recent_runs()[0]
            task = w.ledger.get_task(run.task_id)
            require_equal(task.objective, question)
            require_equal(task.conversation_ref, w.sessions[0].conversation_id)
            require_equal(await w.store.list_pending(), [])
            require(w.gate.context().commanded is False)
            w.ledger.transition(run.run_id, S.PLANNING)
            w.ledger.transition(run.run_id, S.RUNNING)
            w.ledger.transition(run.run_id, S.SUCCEEDED, result_summary=weather)
            await H.until(lambda: weather in json.dumps(commentary(w),ensure_ascii=False), seconds=1)
            require_equal(len(w.native_launches), 1)
            require_equal(len(w.ledger.recent_runs()), 1)
            require_equal(claims(w)[0]['state'], 'finished')
            await H.end(w, ws)


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
