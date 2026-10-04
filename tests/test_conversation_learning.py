"""Text learning through real chat ingress, durable stores and cost claims; fake CLI only."""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

import test_conversation_processing as T
from solvio.agent_runtime import endpoint as agent_endpoint
from solvio.dashboard import endpoint as dashboard_endpoint
from test_adaptive_memory import proposal
from test_adaptive_observation_recovery import source_of
from solvio.memory.adaptive.candidates import CandidateStore
from solvio.memory.adaptive.extractor import SubscriptionExtractor
from solvio.memory.adaptive.observations import AdaptiveObservations
from solvio.memory.adaptive.pipeline import AdaptiveMemory
from solvio.memory.embedding import HashingEmbeddingProvider
from solvio.memory.service import MemoryService
from solvio.specialists import launcher as L
from solvio.specialists.subscription import SubscriptionTransport

TEXT = 'Ich mag Kaffee sehr gerne. Was passt dazu?'


@asynccontextmanager
async def chat_world():
    original = agent_endpoint.attach
    def attach(app, *args, **kwargs):
        result = original(app, *args, **kwargs)
        dashboard_endpoint.attach(app, owner_principal='local-owner', environment='isolated_test')
        return result
    with patch.object(agent_endpoint, 'attach', attach):
        async with T.world() as w:
            yield w


@asynccontextmanager
async def world(*, runner=None):
    async with chat_world() as w:
        w.memory = MemoryService(os.path.join(w.folder, 'learning'), provider=HashingEmbeddingProvider()).open()
        w.learning_calls = []

        async def run(invocation, prompt):
            w.learning_calls.append((T.D.current_scope().binding, prompt))
            if runner:
                return await runner(invocation, prompt)
            return L.Outcome(True, exit_code=0, text=T.codex_lines(json.dumps(
                proposal(statement='Mag Kaffee.', subject='pref:kaffee'))), process_started=True)

        w.extractor = SubscriptionExtractor(transport=SubscriptionTransport(runner=run))
        w.adaptive = AdaptiveMemory(w.memory, CandidateStore(w.memory.base_dir), extractor=w.extractor)
        w.observations = AdaptiveObservations(w.adaptive, w.ledger, owner_principal='local-owner',
                                            quote_adapter=lambda *_: T.FREE)
        w.orch.memory_observations = w.observations
        try:
            yield w
        finally:
            await w.processor.wait_idle(10)
            await w.adaptive.close()
            await w.memory.close()


async def ask(w, cid, text=TEXT, **kwargs):
    w.cli.assessments.append(T.assessment('kein_auftrag', text))
    w.cli.answers.append('ASSISTANT_ONLY: Ich mag Tee, und die historische Quelle mag Kakao.')
    row = await w.ask(cid, text, **kwargs)
    await drain(w)
    require_equal(row['status'], 'completed', row['error_code'])
    return w.chat_store.delivery(cid, row['delivery_id'])


async def drain(w):
    require(await w.processor.wait_idle(10))
    await asyncio.wait_for(w.adaptive._queue.join(), 5)


def activities(w):
    with w.ledger._open() as db:
        return [dict(r) for r in db.execute("SELECT * FROM agent_cost_activities WHERE purpose='adaptive_extract'")]


async def resume(w, activity_id, *, headers=None):
    return await w.client.post('/v1/dashboard/learning/' + activity_id + '/resume',
                               headers=w.headers if headers is None else headers)


async def reopen_learning(w):
    await w.adaptive.close()
    await w.memory.close()
    w.memory = MemoryService(os.path.join(w.folder, 'learning'), provider=HashingEmbeddingProvider()).open()
    w.adaptive = AdaptiveMemory(w.memory, CandidateStore(w.memory.base_dir), extractor=w.extractor)
    w.observations = AdaptiveObservations(w.adaptive, w.ledger, owner_principal='local-owner',
                                        quote_adapter=lambda *_: T.FREE)
    w.orch.memory_observations = w.observations


async def t_answer_learns_only_current_original_user_input_once():
    async with world() as w:
        cid = await w.chat()
        w.chat_store.add_message(cid, 'user', 'HISTORY_ONLY: Ich mag Kakao.')
        row = await ask(w, cid)
        require_equal(row['status'], 'completed')
        require_equal(json.loads(row['dispatch'])['learning'], 'offered')
        require_equal(w.tasks(), 0)
        require_equal(len(w.learning_calls), 1)
        binding, prompt = w.learning_calls[0]
        require(TEXT in prompt)
        require('HISTORY_ONLY' not in prompt and 'ASSISTANT_ONLY' not in prompt)
        require_equal((binding.conversation_id, binding.message_id, binding.task_id),
                      (cid, row['message_id'], None))
        records = await w.memory.semantic.memory.active_records()
        require_equal(len(records), 1)
        require_equal(len(records[0].provenance), 1)
        # Repeated ingress and recovery never reinforce the same message twice.
        await w.post(cid, TEXT)
        await w.processor.recover()
        await drain(w)
        require_equal(len(w.learning_calls), 1)
        require_equal(len(activities(w)), 1)


async def t_crash_after_completion_before_offer_is_recovered_without_reanswering():
    async with world() as w:
        cid = await w.chat()
        with patch.object(w.processor, '_offer_learning', AsyncMock()):
            row = await ask(w, cid)
        require_equal(json.loads(row['dispatch'])['learning'], 'pending')
        require_equal(activities(w), [])
        answer_calls = len(w.cli.calls)
        second = w.second_processor()
        require_equal(await second.recover(), 0)  # no open delivery exists
        await drain(w)
        require_equal(len(w.learning_calls), 1)
        require_equal(len(w.cli.calls), answer_calls)
        require_equal(len(w.chat_store.messages(cid)), 2)


async def t_queue_full_has_durable_hold_and_only_explicit_owner_resume():
    async with world() as w:
        cid = await w.chat()
        with patch.object(w.adaptive, 'observe_turn', return_value=False):
            row = await ask(w, cid)
        held = activities(w)[0]
        require_equal(held['held_reason'], 'observation_queue_unavailable')
        require_equal(json.loads(row['dispatch'])['learning'], 'offered')
        await w.processor.recover()
        require_equal(w.learning_calls, [])
        require_equal((await resume(w, held['activity_id'], headers={})).status, 401)
        replies = await asyncio.gather(*(resume(w, held['activity_id']) for _ in range(2)))
        require_equal(sorted(r.status for r in replies), [202, 409])
        await drain(w)
        require_equal(len(w.learning_calls), 1)
        require_equal(activities(w)[0]['state'], 'completed')


async def t_rejected_offer_without_admission_remains_recoverable():
    async with world() as w:
        cid = await w.chat()
        with patch.object(w.observations, 'offer', return_value=False):
            row = await ask(w, cid)
        require_equal(activities(w), [])
        require_equal(json.loads(row['dispatch'])['learning'], 'pending')
        await w.processor.recover()
        await drain(w)
        require_equal(len(w.learning_calls), 1)


async def t_crash_after_admission_before_marker_ack_never_automatically_reextracts():
    async with world() as w:
        cid = await w.chat()
        with patch.object(w.chat_store, 'learning_offered', side_effect=OSError('synthetic crash gap')):
            row = await ask(w, cid)
        require_equal(json.loads(row['dispatch'])['learning'], 'pending')
        await reopen_learning(w)
        await w.processor.recover()
        await drain(w)
        require_equal(len(w.learning_calls), 1)
        require_equal(len((await w.memory.semantic.memory.active_records())[0].provenance), 1)
        require_equal(w.chat_store.pending_learning(), [])


async def t_saved_batch_resumes_after_restart_without_second_provider_or_evidence():
    async with world() as w:
        cid = await w.chat()
        candidates = w.adaptive.candidates
        transition = candidates.transition
        async def interrupted(candidate_id, action, *args, **kwargs):
            if action == 'adopted':
                raise OSError('synthetic crash after canonical adoption')
            return await transition(candidate_id, action, *args, **kwargs)
        with patch.object(candidates, 'transition', interrupted):
            row = await ask(w, cid)
        held = activities(w)[0]
        require_equal(held['held_reason'], 'extractor_interrupted')
        require_equal(len(w.learning_calls), 1)
        await reopen_learning(w)
        await w.processor.recover()
        require_equal(len(w.learning_calls), 1)
        require_equal((await resume(w, held['activity_id'])).status, 202)
        await drain(w)
        require_equal(len(w.learning_calls), 1)
        records = await w.memory.semantic.memory.active_records()
        require_equal(len(records), 1)
        require_equal(len(records[0].provenance), 1)
        observation = await w.processor._learning_observation(row, w.processor.runtime())
        require_equal(records[0].provenance[0].source, source_of(observation[0]))
        require_equal(activities(w)[0]['state'], 'completed')


async def t_unknown_provider_outcome_cannot_repeat_even_with_explicit_resume():
    async def unknown(*_):
        return L.Outcome(False, reason='communication_failed', exit_code=None, process_started=True)
    async with world(runner=unknown) as w:
        cid = await w.chat()
        await ask(w, cid)
        held = activities(w)[0]
        require(bool(held['held_reason']))
        await reopen_learning(w)
        await w.processor.recover()
        require_equal(len(w.learning_calls), 1)
        require_equal((await resume(w, held['activity_id'])).status, 202)
        await drain(w)
        require_equal(len(w.learning_calls), 1)
        require_equal(await w.memory.semantic.memory.active_records(), [])
        require(bool(activities(w)[0]['held_reason']))


async def t_deleted_chat_or_revoked_original_source_cannot_resume():
    for invalidation in ('delete', 'logout', 'core', 'foreign', 'edited_message'):
        async with world() as w:
            cid = await w.chat()
            with patch.object(w.adaptive, 'observe_turn', return_value=False):
                row = await ask(w, cid)
            activity = activities(w)[0]
            if invalidation == 'delete':
                w.chat_store.delete_conversation(cid)
            elif invalidation == 'logout':
                await w.sessions.revoke(await w.auth_session_id(), principal=row['principal'])
            elif invalidation == 'core':
                w.cp.core_instance_id = 'replacement-core'
            elif invalidation == 'edited_message':
                with w.chat_store.conn:
                    w.chat_store.conn.execute('UPDATE conversation_messages SET text=? WHERE message_id=?',
                                              ('Ich mag Tee.', row['message_id']))
            try:
                await w.processor.resume_learning(activity['activity_id'],
                    'foreign-owner' if invalidation == 'foreign' else row['principal'])
            except ValueError:
                pass
            else:
                raise AssertionError('invalid source resumed: ' + invalidation)
            require_equal(w.learning_calls, [])
            require(bool(activities(w)[0]['held_reason']))


async def t_source_deleted_during_extraction_prevents_adoption():
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(*_):
        entered.set()
        await release.wait()
        return L.Outcome(True, exit_code=0, text=T.codex_lines(json.dumps(
            proposal(statement='Mag Kaffee.', subject='pref:kaffee'))), process_started=True)
    async with world(runner=delayed) as w:
        cid = await w.chat()
        w.cli.assessments.append(T.assessment('kein_auftrag', TEXT))
        w.cli.answers.append('Eine Antwort.')
        await w.ask(cid, TEXT)
        await asyncio.wait_for(entered.wait(), 3)
        w.chat_store.delete_conversation(cid)
        release.set()
        await drain(w)
        require_equal(await w.memory.semantic.memory.active_records(), [])
        require_equal(activities(w)[0]['state'], 'cancelled')


async def t_task_and_disabled_learning_do_not_create_chat_observations():
    async with world() as w:
        cid = await w.chat()
        text = 'Recherchiere drei verifizierte Quellen ueber Kaffee und vergleiche sie.'
        w.cli.assessments.append(T.assessment('auftrag_recherche', text))
        row = await w.ask(cid, text)
        await drain(w)
        require(bool(row['task_id']))
        require('learning' not in json.loads(row['dispatch']))
        require(all(a['source_kind'] == 'task' for a in activities(w)))
        require_equal(len(activities(w)), 1)
        w.adaptive.enabled = False
        other = await w.chat('chat-disabled')
        row = await ask(w, other)
        require('learning' not in json.loads(row['dispatch']))
        require_equal(len(activities(w)), 1)


async def t_app_owner_alone_gets_personal_briefing_and_learning():
    from solvio.agent_runtime import personal_context
    async with world() as w:
        w.app[T.CE.MEMORY_PROVIDER_KEY] = lambda: w.memory
        briefing = AsyncMock(return_value='PERSONAL_OWNER_ONLY: Bevorzugt Kaffee.')
        with patch.object(personal_context, 'for_call', briefing):
            for index, principal in enumerate(('local-owner', 'another-owner')):
                ctx, client, headers = await w.device(principal, 'learning-device-' + str(index))
                response = await w.create('chat-device-' + str(index), client=client, headers=headers)
                cid = (await response.json())['conversation_id']
                message = {'conversation_id': cid, 'client_message_id': 'device-message', 'text': TEXT}
                payload = await w.app_proof(ctx, client, headers, message)
                w.cli.assessments.append(T.assessment('kein_auftrag', TEXT))
                w.cli.answers.append('Eine Antwort ohne privaten Inhalt.')
                before = len(w.cli.calls)
                result = await client.post(T.CE.PREFIX + '/' + cid + '/messages', json=payload, headers=headers)
                require_equal(result.status, 202)
                row = await w.settled(cid, (await result.json())['delivery_id'])
                await drain(w)
                require_equal(row['status'], 'completed', row['error_code'])
                prompts = [c['prompt'] for c in w.cli.calls[before:]]
                if principal == 'local-owner':
                    require_equal(briefing.await_count, 2)  # routing continuity + answer
                    require(all('PERSONAL_OWNER_ONLY' in p for p in prompts))
                    require_equal(len(w.learning_calls), 1)
                    require_equal(activities(w)[0]['source_kind'], 'app')
                else:
                    require_equal(briefing.await_count, 2)
                    require(all('PERSONAL_OWNER_ONLY' not in p for p in prompts))
                    require_equal(len(w.learning_calls), 1)
                    require('learning' not in json.loads(row['dispatch']))


async def t_missing_owner_configuration_exposes_no_personal_memory_or_learning():
    from solvio.agent_runtime import personal_context
    async with world() as w:
        cid = await w.chat()
        w.app[T.CE.MEMORY_PROVIDER_KEY] = lambda: w.memory
        resolve = w.processor._resolve
        w.processor._resolve = lambda: replace(resolve(), owner_principal='')
        briefing = AsyncMock(return_value='PERSONAL_OWNER_ONLY')
        with patch.object(personal_context, 'for_call', briefing):
            row = await ask(w, cid)
        require_equal(briefing.await_count, 0)
        require_equal(activities(w), [])
        require('learning' not in json.loads(row['dispatch']))


async def t_attachment_and_clarification_answers_are_not_learning_inputs():
    async with world() as w:
        cid = await w.chat()
        attachment = {'operation': 'extract_text', 'format': 'rtf',
            'content_b64': base64.b64encode(b'{\\rtf1\\ansi ATTACHMENT_ONLY: I like tea.}').decode()}
        w.cli.assessments.append(T.assessment('kein_auftrag', 'Was steht im Dokument?'))
        row = await w.ask(cid, 'Was steht im Dokument?', attachments=attachment)
        await drain(w)
        require_equal(row['status'], 'completed')
        require('learning' not in json.loads(row['dispatch']))
        other = await w.chat('chat-clarification')
        w.cli.assessments.append(T.assessment('klaerung', 'Hotels vergleichen', klaerungsfrage='Fuer welchen Ort?'))
        row = await w.ask(other, 'Vergleiche mir einige Hotels.')
        await drain(w)
        require_equal(row['status'], 'completed')
        require('learning' not in json.loads(row['dispatch']))
        require_equal(activities(w), [])
        require_equal(w.learning_calls, [])


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
