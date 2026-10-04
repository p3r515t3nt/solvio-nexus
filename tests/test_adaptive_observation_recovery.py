"""N4: kanonische Fehler und Wiederaufnahme zaehlen Beobachtungen einmal.

Echte Memory-/Candidate-/Activity-Stores, Pipeline, SubscriptionTransport und
Kostenclaims. Nur der native Prozess-Runner antwortet lokal synthetisch.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

import test_adaptive_subscription_observations as T
from solvio.agent_runtime import store as S
from solvio.contracts.memory import ProvenanceEntry
from solvio.contracts.trust import SourceType, TrustLevel
from solvio.memory.adaptive import candidates as C
from solvio.memory.adaptive.candidates import CandidateStore
from solvio.memory.adaptive.observations import AdaptiveObservations
from solvio.memory.adaptive.pipeline import AdaptiveMemory
from solvio.memory.embedding import HashingEmbeddingProvider
from solvio.memory.service import MemoryService
from solvio.memory.store import SolvioMemory


def source_of(turn):
    encoded = json.dumps([turn.conversation_id, turn.message_id], ensure_ascii=False,
                         separators=(",", ":")).encode()
    return "conversation:" + turn.conversation_id + "#observation:" + hashlib.sha256(
        b"SOLVIO_MEMORY_OBSERVATION_V1\0" + encoded).hexdigest()


async def offer(w, turn):
    require(w.observations.offer(turn, T.source(turn)))
    await w.drain()


async def reopen(w):
    """Alle dauerhaften Lernbuecher frisch oeffnen; kein In-Memory-Dedupbeleg."""
    await w.adaptive.close()
    await w.memory.close()
    w.memory = MemoryService(str(w.path / "memory"), provider=HashingEmbeddingProvider()).open()
    w.ledger = S.AgentRunLedger(w.ledger.path)
    w.adaptive = AdaptiveMemory(w.memory, CandidateStore(w.memory.base_dir), extractor=w.extractor)
    w.observations = AdaptiveObservations(w.adaptive, w.ledger, owner_principal='owner:local',
                                        quote_adapter=lambda *_: T.FREE)


async def t_canonical_adopt_or_reinforce_failure_is_held_and_resumable():
    from solvio.memory import sqlite_backend as be
    for mode in ("adopt", "reinforce"):
        async with T.world() as w:
            prior = T.turn(T.TEXT, message_id="message:first")
            current = T.turn(T.TEXT, message_id="message:current")
            if mode == "reinforce":
                await offer(w, prior)
            target = be if mode == "adopt" else w.memory.semantic.memory
            method = "insert_record" if mode == "adopt" else "reinforce_observation"
            original_insert = be.insert_record
            def interrupted_insert(*args, **kwargs):
                original_insert(*args, **kwargs)
                raise OSError("synthetic failure inside the uncommitted canonical transaction")
            effect = interrupted_insert if mode == "adopt" else AsyncMock(return_value="unavailable")
            with patch.object(target, method, effect):
                await offer(w, current)
            row = w.rows()[-1]
            require_equal(row["state"], "pending")
            require_equal(row["held_reason"], "extractor_interrupted")
            require(mode + "_failed" in w.adaptive.last_outcome.reasons)
            require_equal(len(await w.memory.semantic.memory.active_records()), int(mode == "reinforce"))
            await reopen(w)
            require(not w.observations.offer(current, T.source(current)), "stiller Wiederholungsaufruf")
            require(w.observations.activities.release_hold(row["activity_id"]))
            await offer(w, current)
            records = await w.memory.semantic.memory.active_records()
            require_equal(len(records), 1)
            sources = [entry.source for entry in records[0].provenance]
            require_equal(sources.count(source_of(current)), 1)
            require_equal(len(sources), 1 + int(mode == "reinforce"))
            require_equal(w.rows()[-1]["state"], "completed")


async def t_memory_commit_before_candidate_close_resumes_without_second_evidence():
    async with T.world() as w:
        current = T.turn(T.TEXT)
        original_transition = w.adaptive.candidates.transition
        async def fail_completion(cid, target, **kwargs):
            if target == C.ADOPTED:
                raise OSError("synthetic crash after canonical commit")
            return await original_transition(cid, target, **kwargs)
        with patch.object(w.adaptive.candidates, "transition", fail_completion):
            await offer(w, current)
        record = (await w.memory.semantic.memory.active_records())[0]
        before = list(record.provenance)
        require_equal([e.source for e in before], [source_of(current)])
        row = w.rows()[0]
        require_equal(row["state"], "pending")
        require(row["held_reason"])
        await reopen(w)
        require(w.observations.activities.release_hold(row["activity_id"]))
        await offer(w, current)
        after = await w.memory.semantic.memory.get_visible(record.id)
        require_equal(after.provenance, before, "derselbe Turn wurde zur zweiten Beobachtung")
        require_equal(w.adaptive.last_outcome.reinforced, 0)
        require("observation_already_recorded" in w.adaptive.last_outcome.reasons)
        require_equal(len(await w.adaptive.candidates.list_states(C.ADOPTED)), 1)
        require_equal(w.rows()[0]["state"], "completed")
        require_equal(len(w.calls), 1, "ein gespeicherter Extraktionssatz braucht keinen zweiten Modellaufruf")


async def t_partial_multi_proposal_resume_does_not_repeat_committed_reinforcement():
    from solvio.memory import sqlite_backend as be
    pair = T.proposal(statement="Mag Kaffee.", subject="pref:kaffee")
    pair["proposals"].extend(T.proposal(statement="Bevorzugt kurze Antworten.",
                                       subject="pref:antwortlaenge")["proposals"])
    multi = False

    async def runner(*_):
        payload = pair if multi else T.proposal(statement="Mag Kaffee.", subject="pref:kaffee")
        return T.L.Outcome(True, exit_code=0, process_started=True,
                           text=T.codex_answer(json.dumps(payload)))

    async with T.world(runner=runner) as w:
        original = T.turn(T.TEXT, message_id="message:original")
        await offer(w, original)
        multi = True
        current = T.turn(T.TEXT + " Ich bevorzuge kurze Antworten.", message_id="message:two-proposals")
        with patch.object(be, "insert_record", side_effect=OSError("second proposal cannot commit")):
            await offer(w, current)
        first = (await w.memory.semantic.memory.active_records())[0]
        before = list(first.provenance)
        require_equal([entry.source for entry in before], [source_of(original), source_of(current)])
        row = w.rows()[-1]
        require(row["held_reason"])
        await reopen(w)
        require(w.observations.activities.release_hold(row["activity_id"]))
        await offer(w, current)
        records = await w.memory.semantic.memory.active_records()
        require_equal(len(records), 2)
        coffee = next(record for record in records if record.id == first.id)
        require_equal(coffee.provenance, before, "erste, bereits fertige Teilwirkung wurde verdoppelt")
        answer = next(record for record in records if record.id != first.id)
        require_equal([entry.source for entry in answer.provenance], [source_of(current)])
        require_equal(w.adaptive.last_outcome.reinforced, 0)
        require_equal(w.adaptive.last_outcome.adopted, 1)
        require_equal(w.rows()[-1]["state"], "completed")


async def t_unknown_adoption_stays_held_after_explicit_resume_without_a_second_write():
    async with T.world() as w:
        current = T.turn(T.TEXT)
        # An arbitrary adapter exception is no proof of a canonical rollback.
        with patch.object(w.memory.semantic, "remember", AsyncMock(side_effect=OSError("unknown boundary"))):
            await offer(w, current)
        row = w.rows()[0]
        require_equal(row['state'], 'pending')
        require_equal(len(await w.adaptive.candidates.list_states(C.ADOPTING)), 1)
        await reopen(w)
        require(w.observations.activities.release_hold(row['activity_id']))
        with patch.object(w.memory.semantic, 'remember', AsyncMock(side_effect=AssertionError('blind retry'))) as write:
            await offer(w, current)
        require_equal(write.call_count, 0)
        require_equal(w.rows()[0]['state'], 'pending')
        require(w.rows()[0]['held_reason'])
        require_equal(await w.memory.semantic.memory.active_records(), [])


async def t_purged_interrupted_adoption_is_never_recreated_by_resume():
    calls = []
    async def runner(*_):
        calls.append(1)
        statement = 'Mag Kaffee.' if len(calls) == 1 else 'Mag Kaffee gerne.'
        return T.L.Outcome(True, exit_code=0, process_started=True,
            text=T.codex_answer(json.dumps(T.proposal(statement=statement, subject='pref:kaffee'))))
    async with T.world(runner=runner) as w:
        current = T.turn(T.TEXT)
        original_transition = w.adaptive.candidates.transition
        async def fail_completion(cid, target, **kwargs):
            if target == C.ADOPTED: raise OSError('after commit')
            return await original_transition(cid, target, **kwargs)
        with patch.object(w.adaptive.candidates, 'transition', fail_completion):
            await offer(w, current)
        mem = (await w.memory.semantic.memory.active_records())[0]
        await w.memory.semantic.purge(mem.id, reason='user_request')
        await w.adaptive.note_forgotten(mem.id, content=mem.content)
        row = w.rows()[0]
        await reopen(w)
        require(w.observations.activities.release_hold(row['activity_id']))
        await offer(w, current)
        require_equal(await w.memory.semantic.memory.active_records(), [])
        require_equal(w.rows()[0]['state'], 'pending')
        require(w.rows()[0]['held_reason'])
        require_equal(len(calls), 1, 'Resume must not reformulate a purged observation')


async def t_frozen_batch_resumes_all_original_proposals_after_first_commit_crash():
    calls = []
    initial = T.proposal(statement='Mag Kaffee.', subject='pref:kaffee')
    initial['proposals'].extend(T.proposal(statement='Bevorzugt kurze Antworten.', subject='pref:antwortlaenge')['proposals'])
    async def runner(*_):
        calls.append(1)
        payload = initial if len(calls) == 1 else T.proposal(statement='Mag Kaffee gerne.', subject='pref:kaffee')
        return T.L.Outcome(True, exit_code=0, process_started=True, text=T.codex_answer(json.dumps(payload)))
    async with T.world(runner=runner) as w:
        current = T.turn(T.TEXT + ' Ich bevorzuge kurze Antworten.')
        original = w.adaptive.candidates.transition
        async def fail_completion(cid, target, **kwargs):
            if target == C.ADOPTED: raise OSError('after first canonical commit')
            return await original(cid, target, **kwargs)
        with patch.object(w.adaptive.candidates, 'transition', fail_completion):
            await offer(w, current)
        first = (await w.memory.semantic.memory.active_records())[0]
        row = w.rows()[0]
        await reopen(w)
        require(w.observations.activities.release_hold(row['activity_id']))
        await offer(w, current)
        records = await w.memory.semantic.memory.active_records()
        require_equal(sorted(r.content for r in records), ['Bevorzugt kurze Antworten.', 'Mag Kaffee.'])
        require_equal((await w.memory.semantic.get(first.id)).provenance, first.provenance)
        require_equal(w.rows()[0]['state'], 'completed')
        require_equal(len(calls), 1)


async def t_secret_shaped_proposal_fields_never_enter_the_durable_work_journal():
    from solvio.memory.intent import looks_like_secret
    marker = 'test-only-' + 'q' * 25
    synthetic = 'Mein Passwort ist ' + marker
    require(looks_like_secret(synthetic))
    for field in ('statement', 'subject'):
        payload = T.proposal(**{field: synthetic})
        async def runner(*_):
            return T.L.Outcome(True, exit_code=0, process_started=True, text=T.codex_answer(json.dumps(payload)))
        async with T.world(runner=runner) as w:
            await offer(w, T.turn(T.TEXT))
            require_equal(await w.memory.semantic.memory.active_records(), [])
            require_equal(await w.adaptive.candidates.list_states(), [])
            stored = await w.adaptive.candidates._run(lambda: [r[0] for r in w.adaptive.candidates._conn.execute('SELECT payload FROM extraction_batches')])
            require(all(marker not in value for value in stored))
            require_equal(w.rows()[0]['state'], 'completed')


async def t_finished_extraction_work_expires_without_removing_canonical_memory():
    from datetime import timedelta
    async with T.world() as w:
        await offer(w, T.turn(T.TEXT))
        count = lambda: w.adaptive.candidates._conn.execute('SELECT COUNT(*) FROM extraction_batches').fetchone()[0]
        require_equal(await w.adaptive.candidates._run(count), 1)
        await w.adaptive.maintenance(now=datetime.now(timezone.utc)+timedelta(hours=2))
        require_equal(await w.adaptive.candidates._run(count), 0)
        require_equal(len(await w.memory.semantic.memory.active_records()), 1)


async def t_independent_store_instances_atomically_append_a_message_once():
    async with T.world() as w:
        first_turn = T.turn(T.TEXT, message_id="message:original")
        await offer(w, first_turn)
        first = (await w.memory.semantic.memory.active_records())[0]
        next_turn = T.turn(T.TEXT, message_id="message:next")
        entry = ProvenanceEntry(SourceType.SOLVIO_INFERENCE, source_of(next_turn),
                                TrustLevel.AGENT_GENERATED, datetime.now(timezone.utc), "stated: Mag Kaffee.")
        second = SolvioMemory(w.memory.base_dir)
        try:
            states = await asyncio.gather(w.memory.semantic.memory.reinforce_observation(first.id, entry),
                                           second.reinforce_observation(first.id, entry))
            require_equal(sorted(states), ["added", "present"])
            actual = await second.get_visible(first.id)
            require_equal([e.source for e in actual.provenance], [source_of(first_turn), source_of(next_turn)])
            require(source_of(first_turn) != source_of(next_turn), "Nachrichten desselben Gespraechs verschmolzen")
            await second.forget(first.id, reason="user_request")
            require_equal(await w.memory.semantic.memory.reinforce_observation(first.id, entry), "unavailable")
        finally:
            await second.close()


async def t_durable_purge_tombstone_blocks_other_already_open_store_reinforcement():
    from solvio.memory import sqlite_backend as be

    async with T.world() as w:
        first_turn = T.turn(T.TEXT, message_id="message:original")
        await offer(w, first_turn)
        first = (await w.memory.semantic.memory.active_records())[0]
        before = list(first.provenance)
        next_turn = T.turn(T.TEXT, message_id="message:after-purge")
        entry = ProvenanceEntry(SourceType.SOLVIO_INFERENCE, source_of(next_turn),
                                TrustLevel.AGENT_GENERATED, datetime.now(timezone.utc), "stated: Mag Kaffee.")
        # Beide Instanzen existieren VOR dem Tombstone. Ein frischer Store
        # wuerde bereits beim Oeffnen reconciliieren und die Luecke verdecken.
        second = SolvioMemory(w.memory.base_dir)
        try:
            def crash_after_tombstone():
                raise RuntimeError("synthetic crash after durable purge tombstone")

            with patch.object(w.memory.semantic.memory, "_crash_after_tombstone", crash_after_tombstone):
                try:
                    await w.memory.semantic.memory.purge(first.id, reason="user_request")
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("purge failpoint was not reached")
            require(second.ledger.contains_id(first.id), "durable privacy tombstone missing")
            require(not second._is_purged(first.id), "fixture accidentally refreshed the old cache")
            require_equal(await second.reinforce_observation(first.id, entry), "unavailable")
            require(second._is_purged(first.id), "fresh denial was not retained in the cache")
            # Physischer Rohbestand nur im temporaeren Test: Die abgebrochene
            # Loeschung darf keinen weiteren Provenienzeintrag hinterlassen.
            require_equal(await second._run(be.provenance, second._conn, first.id), before)
            require_equal(await second.get_visible(first.id), None)
        finally:
            await second.close()


async def t_failed_canonical_reads_cannot_create_a_competing_truth():
    for mode in ("twin", "conflict", "conflict_semantic"):
        async with T.world() as w:
            original = T.turn(T.TEXT, message_id="message:original")
            await offer(w, original)
            current = T.turn("Inzwischen mag ich Kaffee sehr gerne." if mode != "twin" else T.TEXT,
                             message_id="message:retry")
            name = "semantic_hits" if mode == "conflict_semantic" else "hybrid_recall"
            with patch.object(w.memory.semantic, name,
                              AsyncMock(side_effect=OSError("canonical read is unavailable"))):
                await offer(w, current)
            require_equal(w.rows()[-1]["state"], "pending")
            require(w.rows()[-1]["held_reason"], mode)
            records = await w.memory.semantic.memory.active_records()
            require_equal(len(records), 1)
            require_equal([e.source for e in records[0].provenance], [source_of(original)])


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
