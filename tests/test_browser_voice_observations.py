"""Browser voice uses the canonical conversation and N4 source/cost pipeline.

Temporary memory/ledgers, synthetic transcript and existing local model runner.
No microphone, actual provider, production data or second memory store.
"""
import asyncio
from dataclasses import replace
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0,os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_browser_voice_task_authority as V
import test_adaptive_subscription_observations as M
from solvio.memory.adaptive.policy import OwnerTurn
from solvio.memory.adaptive.observations import AdaptiveObservations


def turn(w):
    return OwnerTurn(M.TEXT,'voice_browser',session_id=w.voice.session_id,
        conversation_id='c-browser-memory',turn_id='turn-browser-memory',message_id='m-browser-memory')


async def t_browser_turn_persists_then_learns_once_with_its_own_cost_activity():
    from solvio.conversation.store import ConversationStore
    from solvio.realtime.core_server import Session
    async with V.world() as voice, M.world() as w:
        conversations=ConversationStore(str(w.path/'conversations.sqlite3')).open()
        conversation,_=conversations.begin_session(voice.voice.session_id)
        sess=SimpleNamespace(server=SimpleNamespace(conversations=conversations,
            dispatcher=SimpleNamespace(adaptive_memory=w.adaptive,memory_observations=w.observations)),
            satellite_id=voice.proof.principal,channel='voice_browser',conversation_id=conversation,
            session_id=voice.voice.session_id,browser_task_session=voice.proof,app_task_session=None,
            _turn={'turn_id':'turn-browser-memory'},conversation_mode='active',
            _persist_pending=0,_persist_queue=asyncio.Queue(),persist_failures=0)
        worker=asyncio.create_task(Session._persist_loop(sess))
        try:
            message=Session._persist_message(sess,'user',M.TEXT)
            Session._offer_adaptive(sess,M.TEXT,message_id=message,explicit=False)
            voice.voice.closed=True
            await w.drain()
            require_equal(len(w.calls),1)
            binding=w.calls[0][0]
            require_equal((binding.principal,binding.source_kind,binding.message_id),
                (voice.actor.principal,'dashboard',message))
            require_equal((binding.task_id,binding.run_id),(None,None))
            require(voice.proof.session_nonce not in binding.source_ref)
            require_equal(conversations.message(conversation,message)['text'],M.TEXT)
            require_equal(w.rows()[0]['state'],'completed')
            records=await w.memory.semantic.memory.active_records()
            require_equal(len(records),1)
            # A closed connection cannot admit another message even if the
            # browser account remains logged in.
            require(not w.observations.offer_voice(turn(voice),voice.proof,
                persisted=AsyncMock(return_value=True)))
            require_equal(len(w.calls),1)
        finally:
            worker.cancel()
            await worker
            conversations.close()


async def t_room_audio_unstored_messages_and_forged_identity_do_not_reach_learning():
    async with V.world() as voice,M.world() as w:
        t=turn(voice)
        for changed,proof in ((replace(t,channel='voice_satellite'),voice.proof),
                (replace(t,role='assistant'),voice.proof),(t,True),
                (t,replace(voice.proof,_seal=None))):
            require(not w.observations.offer_voice(changed,proof,persisted=AsyncMock(return_value=True)))
        require_equal(w.rows(),[])
        require(w.observations.offer_voice(t,voice.proof,persisted=AsyncMock(return_value=False)))
        await w.drain()
        require_equal(w.calls,[])
        require_equal(w.rows()[0]['state'],'cancelled')
        require_equal(await w.memory.semantic.memory.active_records(),[])


async def t_logout_during_native_auth_or_extraction_prevents_new_canonical_memory():
    for phase in ('auth','extraction','persistence'):
        async with V.world() as voice:
            async def revoke():
                await voice.sessions.revoke(voice.actor.session_id,principal=voice.actor.principal)
            async def runner(*_):
                if phase=='extraction':await revoke()
                return M.L.Outcome(True,exit_code=0,process_started=True,text=M.codex_answer(json.dumps(
                    M.proposal(statement='Mag Kaffee.',subject='pref:kaffee'))))
            async with M.world(runner=runner) as w:
                async def status(*_,**__):
                    if phase=='auth':await revoke()
                    return M.P.ProviderStatus('codex',True,auth='chatgpt',billing_mode='subscription')
                async def persisted():
                    if phase=='persistence':await revoke()
                    return True
                with patch.object(M.P,'codex_status',status):
                    require(w.observations.offer_voice(turn(voice),voice.proof,persisted=persisted))
                    await w.drain()
                require_equal(len(w.calls),1 if phase=='extraction' else 0,phase)
                require_equal(w.rows()[0]['state'],'cancelled',phase)
                require_equal(await w.memory.semantic.memory.active_records(),[],phase)


async def t_spoken_task_is_not_observed_again_as_a_separate_task_conversation():
    async with V.world() as voice,M.world() as w:
        V.begin(voice)
        result=await V.tool(voice).run(V.TASK)
        require(result.success)
        observations=AdaptiveObservations(w.adaptive,voice.ledger,owner_principal='local-owner',
                                         quote_adapter=lambda *_:M.FREE)
        require(not observations.offer_task(result.data['task_id'],result.data['run_id']))
        require_equal(w.calls,[])
        with voice.ledger._open() as connection:
            require_equal(connection.execute('SELECT COUNT(*) FROM agent_cost_activities').fetchone()[0],0)


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
