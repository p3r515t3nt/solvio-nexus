"""N4: real queue, subscription protocol, source/cost binding and local stores.

The native provider is substituted only at its process runner. No production
memory, vault, account or audio connection is used.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from test_adaptive_memory import proposal, turn
from test_app_voice_task_authority import world as voice_world
from test_subscription_planner import codex_answer
from solvio.agent_runtime import store as S, costs as C, cost_dispatch as D
from solvio.agent_runtime.cost_subjects import _verified_source
from solvio.memory.adaptive.observations import AdaptiveObservations, ObservationBinding, observation_digest
from solvio.memory.adaptive.extractor import SubscriptionExtractor
from solvio.memory.adaptive.candidates import CandidateStore
from solvio.memory.adaptive.pipeline import AdaptiveMemory
from solvio.memory.embedding import HashingEmbeddingProvider
from solvio.memory.service import MemoryService
from solvio.specialists import providers as P, launcher as L
from solvio.specialists.subscription import SubscriptionTransport

FREE = D.CostQuote(0, C.CostEvidence("free_local", "test:local-runner-only"))
TEXT = "Ich mag Kaffee sehr gerne."


class World:
    def __init__(self, folder, *, quote=FREE, runner=None):
        self.path = Path(folder)
        self.ledger = S.AgentRunLedger(str(self.path / "agent.sqlite3"))
        self.memory = MemoryService(str(self.path / "memory"), provider=HashingEmbeddingProvider()).open()
        self.calls = []

        async def run(invocation, prompt):
            scope = D.current_scope()
            self.calls.append((scope.binding, invocation, prompt))
            if runner:
                return await runner(invocation, prompt)
            return L.Outcome(True, exit_code=0, text=codex_answer(json.dumps(
                proposal(statement="Mag Kaffee.", subject="pref:kaffee"))), process_started=True)

        self.extractor = SubscriptionExtractor(transport=SubscriptionTransport(runner=run))
        self.adaptive = AdaptiveMemory(self.memory, CandidateStore(self.memory.base_dir), extractor=self.extractor)
        self.observations = AdaptiveObservations(self.adaptive, self.ledger, owner_principal='owner:local',
                                               quote_adapter=lambda *_: quote)

    async def drain(self):
        await asyncio.wait_for(self.adaptive._queue.join(), timeout=5)

    def rows(self):
        with self.ledger._open() as db:
            return [dict(r) for r in db.execute("SELECT * FROM agent_cost_activities ORDER BY accepted_at,activity_id")]


@asynccontextmanager
async def world(**kwargs):
    with tempfile.TemporaryDirectory(prefix="solvio-adaptive-subscription-") as folder:
        w = World(folder, **kwargs)
        with patch.object(P, "resolve", return_value="/synthetic/local-codex"), \
                patch.object(P, "codex_status", AsyncMock(return_value=P.ProviderStatus(
                    "codex", True, auth="chatgpt", billing_mode="subscription"))):
            try:
                yield w
            finally:
                await w.adaptive.close()
                await w.memory.close()


def source(t, *, principal="owner:local", reference="proof:session"):
    return _verified_source(principal=principal, source_kind="app", source_ref=reference,
        conversation_id=t.conversation_id, message_id=t.message_id)


async def t_queue_a_b_a_uses_each_message_binding_and_never_inherits_starting_task():
    async with world() as w:
        parent = w.ledger.create_task(objective="Eine andere konkrete Aufgabe.", scope="research",
            created_origin="trusted_dashboard", created_principal="owner:local")
        run = w.ledger.create_run(task_id=parent.task_id)
        turns = [turn(TEXT, conversation_id=c, message_id=m) for c, m in
                 (("conversation:a", "message:one"), ("conversation:b", "message:two"), ("conversation:a", "message:three"))]
        with D.task_cost_scope(w.ledger, task_id=parent.task_id, run_id=run.run_id,
                              phase="plan", operation_id="unrelated-plan"):
            for t in turns:
                require(w.observations.offer(t, source(t)))
        await w.drain()
        require_equal(len(w.calls), 3)
        for t, (binding, invocation, prompt) in zip(turns, w.calls):
            require_equal((binding.conversation_id, binding.message_id, binding.content_digest),
                          (t.conversation_id, t.message_id, observation_digest(t)))
            require_equal((binding.task_id, binding.run_id), (None, None))
            require("features.shell_tool=false" in invocation.argv)
        ids = [call[0].subject_id for call in w.calls]
        require(ids[0] == ids[2] and ids[0] != ids[1])
        require(all(row["state"] == "completed" for row in w.rows()))
        require_equal(len(await w.memory.semantic.memory.active_records()), 1)
        require_equal(len(w.ledger.recent_runs()), 1, "learning invented agent runs")


async def t_unbound_context_and_changed_message_never_reach_subscription_transport():
    async with world() as w:
        t = turn(TEXT)
        with D.task_cost_scope(w.ledger, task_id="made-up", run_id="made-up",
                              phase="adaptive_extract", operation_id="made-up"):
            result = await w.extractor.propose(t)
        require_equal(result.reason, "cost_context_missing")
        bound = w.observations.activities.admit(source(t), content_digest=observation_digest(t))
        with D.interaction_cost_scope(w.ledger, activity_id=bound.activity_id,
                content_digest=bound.content_digest, quote_adapter=lambda *_: FREE):
            result = await w.extractor.propose(replace(t, text="Ich mag lieber Tee."))
        require_equal(result.reason, "activity_binding_mismatch")
        for changed in (replace(t, text="Ich mag lieber Tee."), replace(t, message_id="different")):
            outcome = await w.adaptive.process(changed, cost_binding=ObservationBinding(bound.activity_id, source(t)))
            require_equal(outcome.adopted, 0)
        require_equal(w.calls, [])


async def t_room_audio_roles_secrets_missing_ids_and_fake_source_create_no_activity():
    async with world() as w:
        base = turn(TEXT)
        for t in (replace(base, channel="voice_satellite"), replace(base, role="assistant"),
                  replace(base, text="Mein Passwort lautet geheim123!"), replace(base, message_id="")):
            require(not w.observations.offer(t, source(t) if t.message_id else source(base)))
        require(not w.observations.offer(base, {"principal": "owner:local"}))
        require(not w.observations.offer(base, source(base), context="Mein Passwort lautet geheim123!"))
        require_equal(w.rows(), [])
        require_equal(w.calls, [])


async def t_duplicate_offer_and_fresh_observer_do_not_extract_or_reinforce_twice():
    async with world() as w:
        t = turn(TEXT)
        require(w.observations.offer(t, source(t)))
        require(not w.observations.offer(t, source(t)))
        await w.drain()
        fresh = AdaptiveObservations(w.adaptive, S.AgentRunLedger(w.ledger.path), owner_principal='owner:local',
                                     quote_adapter=lambda *_: FREE)
        require(not fresh.offer(t, source(t)))
        require_equal(len(w.calls), 1)
        require_equal(len(w.rows()), 1)
        record = (await w.memory.semantic.memory.active_records())[0]
        require_equal(len(record.provenance), 1)


async def t_unknown_cost_and_quota_are_held_durably_without_automatic_retry_or_switch():
    async def quota(*_):
        return L.Outcome(False, reason="quota", exit_code=1, process_started=True)
    for kwargs, expected, count in (({"quote": D.CostQuote()}, "cost_unbounded", 0),
                                    ({"runner": quota}, "quota", 1)):
        async with world(**kwargs) as w:
            t = turn(TEXT)
            require(w.observations.offer(t, source(t)))
            await w.drain()
            row = w.rows()[0]
            require_equal(row["held_reason"], expected)
            fresh = AdaptiveObservations(w.adaptive, S.AgentRunLedger(w.ledger.path), owner_principal='owner:local',
                                         quote_adapter=lambda *_: FREE)
            require(not fresh.offer(t, source(t)))
            require_equal(len(w.calls), count)
            require_equal(await w.memory.semantic.memory.active_records(), [])


async def t_unknown_provider_outcome_cannot_be_repeated_even_after_explicit_hold_release():
    async def unknown(*_):
        return L.Outcome(False, reason="communication_failed", exit_code=None, process_started=True)
    async with world(runner=unknown) as w:
        t = turn(TEXT)
        require(w.observations.offer(t, source(t)))
        await w.drain()
        row = w.rows()[0]
        invocations = D.subject_invocations(w.ledger, row["subject_id"])
        require_equal([i["state"] for i in invocations], ["unknown"])
        require(w.observations.activities.release_hold(row["activity_id"]))
        require(w.observations.offer(t, source(t)))
        await w.drain()
        require_equal(len(w.calls), 1)
        require_equal(await w.memory.semantic.memory.active_records(), [])


async def t_activity_finishes_only_after_canonical_handling_and_failure_stays_unfinished():
    for fail in (False, True):
        async with world() as w:
            entered, release = asyncio.Event(), asyncio.Event()
            original = w.adaptive._handle
            async def handle(*args):
                entered.set(); await release.wait()
                if fail:
                    raise RuntimeError("synthetic canonical failure")
                return await original(*args)
            w.adaptive._handle = handle
            t = turn(TEXT)
            require(w.observations.offer(t, source(t)))
            await asyncio.wait_for(entered.wait(), 3)
            require_equal(w.rows()[0]["state"], "pending")
            release.set(); await w.drain()
            row = w.rows()[0]
            require_equal(row["state"], "pending" if fail else "completed")
            if fail:
                require(row["held_reason"])
                require(not w.observations.offer(t, source(t)))


async def t_real_app_proof_allows_bounded_learning_after_conversation_end():
    async with voice_world() as voice, world() as w:
        require(voice.proven)
        t = turn(TEXT, session_id=voice.session.session_id)
        proof = voice.session.app_task_session
        require(w.observations.offer_voice(t, proof, persisted=AsyncMock(return_value=True)))
        voice.socket.closed = True
        voice.session.app_task_session = None
        await w.drain()
        require_equal(len(w.calls), 1)
        require_equal(w.calls[0][0].principal, "local-owner")
        require_equal(len(await w.memory.semantic.memory.active_records()), 1)
        require(not w.observations.offer_voice(replace(t, message_id="later"), proof,
                                              persisted=AsyncMock(return_value=True)))


async def t_revoked_device_or_missing_persistence_blocks_queued_app_learning():
    for revoke in (True, False):
        async with voice_world() as voice, world() as w:
            gate = asyncio.Event()
            async def persisted():
                await gate.wait()
                return revoke
            t = turn(TEXT, session_id=voice.session.session_id)
            if revoke:
                await voice.cp.revoke_device(voice.device.device_id)
            require(w.observations.offer_voice(t, voice.session.app_task_session, persisted=persisted))
            gate.set(); await w.drain()
            require_equal(w.rows()[0]["state"], "cancelled")
            require_equal(w.calls, [])


async def t_invalid_app_proof_or_boolean_is_not_personal_learning_authority():
    async with voice_world(invalid=True) as voice, world() as w:
        t = turn(TEXT, session_id=voice.session.session_id)
        for proof in (voice.session.app_task_session, True, {"principal": "local-owner"}):
            require(not w.observations.offer_voice(t, proof, persisted=AsyncMock(return_value=True)))
        require_equal(w.rows(), [])


async def t_core_reader_waits_for_real_persistence_and_rejects_unstored_message_id():
    from solvio.conversation.store import ConversationStore
    from solvio.realtime.core_server import Session
    async with voice_world() as voice, world() as w:
        conversations = ConversationStore(str(w.path / "conversations.sqlite3")).open()
        conversation, _ = conversations.begin_session(voice.session.session_id)
        sess = SimpleNamespace(server=SimpleNamespace(conversations=conversations,
            dispatcher=SimpleNamespace(adaptive_memory=w.adaptive, memory_observations=w.observations)),
            satellite_id="iphone-device", channel="voice_iphone", conversation_id=conversation,
            session_id=voice.session.session_id, app_task_session=voice.session.app_task_session,
            _turn={"turn_id": "turn-persisted"}, conversation_mode="active",
            _persist_pending=0, _persist_queue=asyncio.Queue(), persist_failures=0)
        worker = asyncio.create_task(Session._persist_loop(sess))
        try:
            message = Session._persist_message(sess, "user", TEXT)
            Session._offer_adaptive(sess, TEXT, message_id=message, explicit=False)
            await w.drain()
            require_equal(len(w.calls), 1)
            require_equal(conversations.message(conversation, message)["text"], TEXT)
            require_equal(w.calls[0][0].message_id, message)
            Session._offer_adaptive(sess, TEXT, message_id="m-not-persisted", explicit=False)
            await w.drain()
            require_equal(len(w.calls), 1)
            require_equal(w.rows()[-1]["state"], "cancelled")
            Session._offer_adaptive(sess, TEXT, message_id="m-explicit", explicit=True)
            require_equal(len(w.rows()), 2)
        finally:
            worker.cancel()
            await worker
            conversations.close()


async def t_device_revocation_during_provider_call_prevents_canonical_adoption():
    async with voice_world() as voice:
        async def revoke(*_):
            await voice.cp.revoke_device(voice.device.device_id)
            return L.Outcome(True, exit_code=0, process_started=True,
                text=codex_answer(json.dumps(proposal(statement="Mag Kaffee.", subject="pref:kaffee"))))
        async with world(runner=revoke) as w:
            t = turn(TEXT, session_id=voice.session.session_id)
            require(w.observations.offer_voice(t, voice.session.app_task_session,
                                               persisted=AsyncMock(return_value=True)))
            await w.drain()
            require_equal(len(w.calls), 1)
            require_equal(w.rows()[0]["state"], "cancelled")
            require_equal(await w.memory.semantic.memory.active_records(), [])


async def t_device_revocation_during_native_auth_probe_prevents_physical_dispatch():
    async with voice_world() as voice, world() as w:
        async def status(*args, **kwargs):
            await voice.cp.revoke_device(voice.device.device_id)
            return P.ProviderStatus("codex", True, auth="chatgpt", billing_mode="subscription")
        t = turn(TEXT, session_id=voice.session.session_id)
        with patch.object(P, "codex_status", status):
            require(w.observations.offer_voice(t, voice.session.app_task_session,
                                               persisted=AsyncMock(return_value=True)))
            await w.drain()
        require_equal(w.calls, [])
        require_equal(w.rows()[0]["state"], "cancelled")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
