"""Attested app tasks keep their authority without access to another owner's memory."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

import test_conversation_learning as T
import test_agent_personal_context as M
import test_autonomous_gap_closure as G
from solvio.agent_runtime import cost_subjects as A, specialists as SP, store as S
from solvio.memory.adaptive.observations import observation_digest
from solvio.memory.adaptive.policy import OwnerTurn
from solvio.specialists.result import SpecialistResult

OWNER = 'local-owner'
FOREIGN = 'another-owner'
OBJECTIVE = 'Vergleiche Hotels Hamburg. Ich mag Kaffee sehr gerne.'


async def app_task(w, principal):
    ctx, client, headers = await w.device(principal, 'task-device-' + principal)
    response = await w.create('task-chat-' + principal, client=client, headers=headers)
    cid = (await response.json())['conversation_id']
    message = {'conversation_id': cid, 'client_message_id': 'task-message', 'text': OBJECTIVE}
    payload = await w.app_proof(ctx, client, headers, message)
    w.cli.assessments.append(T.T.assessment('auftrag_recherche', OBJECTIVE))
    response = await client.post(T.T.CE.PREFIX + '/' + cid + '/messages', json=payload, headers=headers)
    require_equal(response.status, 202, await response.text())
    row = await w.settled(cid, (await response.json())['delivery_id'])
    require_equal(row['status'], 'completed', row['error_code'])
    require(bool(row['task_id']))
    task = w.ledger.get_task(row['task_id'])
    grant = w.orch.task_authority.for_run(row['run_id'])
    require_equal(task.created_principal, principal)
    require_equal(grant.authorizer, principal)
    require_equal(grant.receipt_method, 'app_session')
    return row


async def t_attested_foreign_app_task_works_but_never_learns_into_owner_memory():
    async with T.world() as w:
        w.observations.owner_principal = OWNER
        row = await app_task(w, FOREIGN)
        await T.drain(w)
        require_equal(T.activities(w), [], 'foreign task entered personal learning')
        require_equal(w.learning_calls, [])
        require_equal(await w.memory.semantic.memory.active_records(), [])
        require(not w.orch.offer_task_observation(row['task_id'], row['run_id']))
        await w.orch.tick()
        require_equal(w.ledger.get_run(row['run_id']).state, S.PLANNING)


async def t_attested_owner_task_still_learns_once_and_replay_cannot_reinforce_it():
    async with T.world() as w:
        w.observations.owner_principal = OWNER
        row = await app_task(w, OWNER)
        await T.drain(w)
        require_equal(len(w.learning_calls), 1)
        require_equal(T.activities(w)[0]['source_kind'], 'task')
        require_equal(T.activities(w)[0]['principal'], OWNER)
        require(not w.orch.offer_task_observation(row['task_id'], row['run_id']))
        await T.drain(w)
        require_equal(len(w.learning_calls), 1)
        records = await w.memory.semantic.memory.active_records()
        require_equal(len(records), 1)
        require_equal(len(records[0].provenance), 1)


async def t_held_task_resume_rechecks_configured_owner_without_rebinding_old_source():
    for principal in (FOREIGN, OWNER):
        async with T.world() as w:
            w.observations.owner_principal = OWNER
            # Historical pending rows may predate the new admission guard.
            w.adaptive.enabled = False
            row = await app_task(w, principal)
            w.adaptive.enabled = True
            grant = w.orch.task_authority.for_run(row['run_id'])
            source = A._verified_source(principal=principal, source_kind='task',
                source_ref=grant.reference, conversation_id='task:' + row['task_id'], message_id=row['task_id'])
            turn = OwnerTurn(OBJECTIVE, channel='task_iphone', conversation_id=source.conversation_id,
                session_id=grant.reference, turn_id=row['task_id'], message_id=row['task_id'])
            binding = w.observations.activities.admit(source, content_digest=observation_digest(turn),
                task_id=row['task_id'], run_id=row['run_id'])
            w.observations.activities.hold(binding.activity_id, 'extractor_interrupted')
            if principal == FOREIGN:
                try:
                    w.observations.resume_task(binding.activity_id, principal)
                except ValueError:
                    pass
                else:
                    raise AssertionError('foreign historical task observation resumed')
                require_equal(w.observations.activities.generation(binding.activity_id), 0)
                require_equal(T.activities(w)[0]['held_reason'], 'extractor_interrupted')
            else:
                require(w.observations.resume_task(binding.activity_id, principal))
                await T.drain(w)
                require_equal(w.observations.activities.generation(binding.activity_id), 1)
            require_equal(len(w.learning_calls), int(principal == OWNER))


async def t_task_planning_and_specialist_only_read_the_configured_owners_memory():
    for principal, configured_owner in ((FOREIGN, OWNER), (OWNER, ''), (OWNER, OWNER)):
        async with T.world() as w:
            w.adaptive.enabled = False
            w.orch.memory_owner_principal = configured_owner
            w.orch.personal_memory = w.memory
            await w.memory.semantic.remember(M.record())
            w.orch.planner = M.Planner()
            w.orch.researcher = object()
            requests = []

            async def specialist(request, **kwargs):
                requests.append(request)
                return SP.SpecialistRun(result=SpecialistResult(role='scout', provider='hermes',
                    question=request.objective, ok=True, findings=['Die Recherche ist noch unvollstaendig.']),
                    dispatch_started=False, provider='hermes', billing_mode='subscription', auth='subscription')

            row = await app_task(w, principal)
            lookup = AsyncMock(wraps=w.memory.search)
            with patch.object(w.memory, 'search', lookup), patch.object(SP, 'run_specialist', specialist):
                await w.orch.tick()
                await w.orch.tick()
                require_equal(w.ledger.get_run(row['run_id']).state, S.RUNNING)
                await w.orch.tick()
            require_equal(len(w.orch.planner.contexts), 1)
            require_equal(len(requests), 1)
            contexts = (w.orch.planner.contexts[0], requests[0].context)
            if principal == configured_owner:
                require(lookup.await_count >= 2)
                require(all('Altvorliebe' in value for value in contexts))
            else:
                require_equal(lookup.await_count, 0, 'foreign/unconfigured task searched personal memory')
                require(all('Altvorliebe' not in value for value in contexts))


async def t_task_learning_without_owner_configuration_or_changed_owner_does_not_adopt():
    async with T.world() as w:
        w.observations.owner_principal = ''
        await app_task(w, OWNER)
        await T.drain(w)
        require_equal(T.activities(w), [])
        require_equal(w.learning_calls, [])
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(invocation, prompt):
        entered.set()
        await release.wait()
        return T.L.Outcome(True, exit_code=0, text=T.T.codex_lines(T.json.dumps(
            T.proposal(statement='Mag Kaffee.', subject='pref:kaffee'))), process_started=True)

    async with T.world(runner=paused) as w:
        w.observations.owner_principal = OWNER
        await app_task(w, OWNER)
        await asyncio.wait_for(entered.wait(), 3)
        w.observations.owner_principal = FOREIGN
        release.set()
        await T.drain(w)
        require_equal(await w.memory.semantic.memory.active_records(), [])
        require_equal(T.activities(w)[0]['state'], 'cancelled')


def t_gap_development_cannot_read_personal_knowledge_for_a_different_owner():
    for configured_owner in ('', FOREIGN):
        orch, _, book = G._aufbau()
        orch.memory_owner_principal = configured_owner
        memory = G.Gedaechtnis([G.Treffer('OWNER_ONLY: PRIVATE_KNOWLEDGE')])
        orch.knowledge = memory
        _, run = G.V1A._create(orch, objective=G.ZIEL)
        final = G._bis_zum_parken(orch, run.run_id)
        require_equal(final.state, S.WAITING_CAPABILITY)
        require_equal(memory.fragen, [])
        require('PRIVATE_KNOWLEDGE' not in book.milestone(final.development_ref).contract_json)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
