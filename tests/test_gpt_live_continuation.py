"""Public callback continuation with real local CLI processes, never a provider.

The fake executable chooses an ID only by parsing the exact Core status result
in its actual stdin. It does not receive the temporary task ID out of band.
This proves transport/authority composition, not model intent understanding.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_gpt_live_session as H
import test_live_backend as N
import test_gpt_live_native_delegation as G
from solvio.agent_runtime.voice_delegate import LiveBackend
from solvio.realtime import live_session as L
from solvio.realtime.live_status_tools import CoreStatusTool
from solvio.realtime.control import CoreControl
from solvio.capabilities.agent import AgentCapabilities, SPECS
from solvio.tools.agent_capability_tools import AgentCapabilityTool
from solvio.specialists import subscription as U, providers as P, launcher as Launcher

TEXT = 'Brich meinen letzten Auftrag ab.'


@asynccontextmanager
async def world(*, mode='', delay=0, script_override=None):
    with N.world() as (_, _, native, launches):
        script = '''import json,sys,time
p=json.loads(sys.stdin.read().split('\\n',1)[1])
u=json.loads(p[1]['content']); rows=u.get('core_results',[])
time.sleep(DELAY if rows else 0)
if MODE=='quota_first' or MODE=='quota_second' and rows:
 print(json.dumps({'type':'turn.failed','error':{'message':'Usage limit reached'}})); sys.exit(0)
if MODE=='incomplete' or 'Nein' in u['user_text']:
 reply={'calls':[],'clarification':'Welchen Auftrag meinst du?'}
elif MODE=='write_first':
 reply={'calls':[{'name':'agent_task_research','arguments':{'objective':'Pruefe zwei lokale Testdaten.'}}],'clarification':''}
elif rows and MODE!='read_twice':
 target=rows[0]['result']['data']['auftrag']['kennung']
 reply={'calls':[{'name':'agent_run_cancel','arguments':{'run_id':target}}],'clarification':''}
else:
 reply={'calls':[{'name':'agent_task_status','arguments':{'text':''}}],'clarification':''}
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(reply)}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
'''.replace('DELAY', repr(delay)).replace('MODE', repr(mode))
        if script_override is not None:
            script = script_override
        def invocation(provider, *, workdir, **_):
            require_equal(provider, 'codex')
            return Launcher.Invocation(sys.executable, ('-c', script), cwd=workdir, timeout=65)
        with patch.object(U, 'text_invocation', invocation), patch.object(P, 'claude_status',
                AsyncMock(side_effect=AssertionError('fallback forbidden'))):
            async with H.world() as w:
                d = w.server.dispatcher
                d.live_backend = LiveBackend(w.ledger, transport=native.transport, quote_adapter=native.quote_adapter)
                d.register(CoreStatusTool(CoreControl(d, socket_path='unused-temporary-fixture'), 'agent_task_status'))
                w.cancel_count = 0
                cancel = AgentCapabilities(w.orch).cancel
                async def counted(*args, **kwargs):
                    w.cancel_count += 1
                    return await cancel(*args, **kwargs)
                w.router.register(SPECS['agent_run_cancel'], counted)
                d.register(AgentCapabilityTool('agent_run_cancel', w.router, w.gate, w.ledger))
                w.native_launches = launches
                ws, _ = await H.H.started(w)
                w.client = ws; w.session = w.sessions[0]; w.provider = w.providers[0]
                task = w.ledger.create_task(objective='Vorhandener lokaler Fixtureauftrag', scope='research',
                    created_origin='trusted_dashboard', created_principal=w.session.browser_task_session.principal)
                w.run = w.ledger.create_run(task_id=task.task_id)
                yield w


async def notice(w, key):
    before = len(w.session._delegations)
    await w.provider.events.put(H.delegate(key))
    await H.until(lambda:len(w.session._delegations)>before)
    await asyncio.wait_for(w.session._tool_queue.join(), 5)


async def first(w, text=TEXT):
    await w.provider.events.put(H.transcript(text))
    await notice(w, 'read-first')


def payload(prompt):
    return json.loads(json.loads(prompt.split('\n', 1)[1])[1]['content'])


async def t_public_native_status_then_cancel_uses_original_receipt_once_and_fresh_cost_claim():
    async with world() as w:
        with patch.object(w.session, '_offer_adaptive') as learned:
            await first(w)
            original_turn = w.session._turn['turn_id']
            original_message = w.session._read_continuation.message_id
            actual_read = w.session._tool_results[0]
            require_equal(actual_read['data']['auftrag']['kennung'], w.run.run_id)
            require_equal(len(w.native_launches), 1)
            require_equal(w.cancel_count, 0)
            # Nothing follows automatically; only a new client callback acts.
            await asyncio.sleep(.04)
            require_equal(len(w.native_launches), 1)
            await notice(w, 'dependent-cancel')
            require_equal(w.ledger.get_run(w.run.run_id).state, 'CANCELLED')
            require_equal(w.cancel_count, 1)
            require_equal(w.session._turn['turn_id'], original_turn)
            require_equal(learned.call_count, 1)
            require_equal(len(w.native_launches), 2)
            p = payload(w.native_launches[1])
            require_equal(p['user_text'], TEXT)
            require_equal(p['core_results'], [{'name':'agent_task_status','arguments':{'text':''},'result':actual_read}])
            require_equal(p['revision'], payload(w.native_launches[0])['revision'])
            cs = G.claims(w); require_equal(len(cs), 2)
            require(all(c['state']=='finished' and c['phase']=='voice_delegate' for c in cs))
            with w.ledger._open() as db:
                rows = list(db.execute('SELECT message_id FROM agent_cost_activities'))
                money = list(db.execute('SELECT state,actual_cents FROM agent_cost_reservations'))
            require_equal([r[0] for r in rows], [original_message, original_message])
            require_equal([tuple(r) for r in money], [('settled', 0), ('settled', 0)])
            messages = w.server.conversations.recent_context(w.session.conversation_id)
            require_equal(sum(r['role']=='user' and r['text']==TEXT for r in messages), 1)
            require(w.session._read_continuation is None)
            await w.provider.events.put(H.delegate('dependent-cancel'))
            await notice(w, 'third-callback')
            require_equal(len(w.native_launches), 2); require_equal(w.cancel_count, 1)
            await H.end(w, w.client)
            require_equal(w.ledger.get_run(w.run.run_id).state, 'CANCELLED')
            require_equal(P.claude_status.call_count, 0)


async def t_status_question_cannot_gain_cancel_authority_from_a_successful_read():
    async with world() as w:
        await first(w, text='Wie weit ist mein letzter Auftrag?')
        require_equal(w.session._tool_results[0]['data']['auftrag']['kennung'], w.run.run_id)
        require(w.gate.context().commanded is False)
        require(w.session._read_continuation is None)
        # The same local CLI would select cancel if it received Core results.
        # This callback must not reach that second process at all.
        await notice(w, 'no-effect-from-status-question')
        require_equal(len(w.native_launches), 1)
        require_equal(w.cancel_count, 0)
        require_equal(w.ledger.get_run(w.run.run_id).state, 'CREATED')
        require_equal(len(G.claims(w)), 1)
        await H.end(w, w.client)


async def t_only_one_continuation_even_when_second_selection_reads_again():
    async with world(mode='read_twice') as w:
        await first(w); await notice(w, 'second-read'); await notice(w, 'third-read')
        require_equal(len(w.native_launches), 2); require_equal(w.cancel_count, 0)
        require(w.session._read_continuation is None)
        await H.end(w, w.client)


async def t_new_fragment_invalidates_read_context_instead_of_authorizing_old_cancel():
    async with world() as w:
        await first(w)
        await w.provider.events.put(H.transcript('Nein, noch nicht abbrechen.', 1000, 1400, 'correction'))
        await H.until(lambda:bool(w.session._input_parts))
        require(w.session._read_continuation is None)
        await w.provider.events.put(H.delegate('corrected', 1400))
        await H.until(lambda:'corrected' in w.session._delegations)
        await asyncio.wait_for(w.session._tool_queue.join(), 5)
        require_equal(len(w.native_launches), 2); require_equal(payload(w.native_launches[1])['core_results'], [])
        require_equal(w.cancel_count, 0); require_equal(w.ledger.get_run(w.run.run_id).state, 'CREATED')
        await H.end(w, w.client)


async def t_correction_during_continuation_native_call_prevents_cancel():
    async with world(delay=.2) as w:
        await first(w)
        await w.provider.events.put(H.delegate('delayed-cancel'))
        await H.until(lambda:len(w.native_launches)==2, seconds=4)
        await w.provider.events.put(H.transcript('Nein, nicht abbrechen.', 1000, 1400, 'correction'))
        await H.until(lambda:bool(w.session._input_parts))
        await asyncio.wait_for(w.session._tool_queue.join(), 5)
        require_equal(w.cancel_count, 0); require_equal(w.ledger.get_run(w.run.run_id).state, 'CREATED')
        require(w.session._read_continuation is None)
        await H.end(w, w.client)


async def t_close_and_reconnect_drop_original_read_authority():
    async with world() as w:
        await first(w); require(w.session._read_continuation is not None)
        await H.end(w, w.client)
        require(w.session._read_continuation is None); require_equal(w.cancel_count, 0)
    async with world() as w:
        await first(w)
        w.session._on_provider_lost(w.session.open_attempts)
        require(w.session._read_continuation is None)
        await H.until(lambda:len(w.providers)==2 and w.session._live_started, seconds=4)
        w.provider = w.providers[-1]
        await notice(w, 'after-reconnect')
        require_equal(len(w.native_launches), 1); require_equal(w.cancel_count, 0)
        await H.end(w, w.client)


async def t_expired_or_older_callback_cannot_acquire_read_continuation():
    for stale in ('expired', 'older'):
        async with world() as w:
            await first(w)
            if stale=='expired':
                w.session._read_continuation = replace(w.session._read_continuation, expires_at=0)
                await notice(w, 'expired')
            else:
                await w.provider.events.put(H.delegate('older', 1))
                await H.until(lambda:'older' in w.session._delegations)
                await asyncio.wait_for(w.session._tool_queue.join(), 5)
            require_equal(len(w.native_launches), 1); require_equal(w.cancel_count, 0)
            require(w.session._read_continuation is None)
            await H.end(w, w.client)


async def t_quota_incomplete_and_write_never_offer_retry_or_new_read_authority():
    for mode, expected in [('quota_first', 1), ('quota_second', 2), ('incomplete', 1), ('write_first', 1)]:
        async with world(mode=mode) as w:
            await first(w, text='Brich meinen letzten' if mode=='incomplete' else TEXT)
            if mode=='quota_second': await notice(w, 'quota-second')
            require(w.session._read_continuation is None)
            await notice(w, 'no-retry')
            require_equal(len(w.native_launches), expected); require_equal(w.cancel_count, 0)
            require_equal(P.claude_status.call_count, 0)
            await H.end(w, w.client)


def t_live_research_menu_and_bound_instruction_offer_only_available_native_lookup():
    from solvio.tools.dispatcher import ToolDispatcher
    from solvio.realtime.core_server import tool_instructions_for
    d = ToolDispatcher()
    d.register(AgentCapabilityTool('agent_task_research', None, None))
    original = d.openai_tools()[0]['description']
    old_instructions = tool_instructions_for('off')
    live = L.live_toolkit(d)
    require('schnelle Einzelfrage' in live[0]['description'])
    require('agent_task_research' in L.LIVE_TOOL_INSTRUCTIONS)
    for name in L.LEGACY_API_STARTS:
        require(name not in L.LIVE_TOOL_INSTRUCTIONS)
        require(name not in live[0]['description'])
    require_equal(d.openai_tools()[0]['description'], original)
    require_equal(tool_instructions_for('off'), old_instructions)
    require('deep_research' in original)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
