"""Canonical adaptive memory changes reach the bound compiler/vault.

Real MemoryService, canonical store, CandidateStore, policy, capability mutation
handlers and compiler; only extraction and fault/barrier timing are supplied.
All storage is temporary and embeddings use the local hashing provider. This
suite proves mutation-to-projection, not caller authentication or model quality.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from test_adaptive_memory import Fixture, proposal, turn
from solvio.capabilities.memory import MemoryCapabilities
from solvio.contracts.trust import SourceType
from solvio.knowledge import compiler as K, service as KS, obsidian as O
from solvio.memory.adaptive.candidates import CandidateStore
from solvio.memory.adaptive.pipeline import AdaptiveMemory
from solvio.memory.embedding import HashingEmbeddingProvider
from solvio.memory.service import MemoryService
from solvio.tools.registry import _knowledge_refresher

STATEMENT = "Mag Kaffee."


class World:
    def __init__(self, folder, payloads):
        self.folder = Path(folder)
        self.vault = self.folder / "vault"
        self.service = MemoryService(str(self.folder / "memory"),
                                     provider=HashingEmbeddingProvider()).open()
        self.adaptive = AdaptiveMemory(self.service, CandidateStore(self.service.base_dir),
            extractor=Fixture(*(payloads or (proposal(statement=STATEMENT, subject="pref:kaffee"),))))
        self.capabilities = MemoryCapabilities(self.service, self.adaptive)
        self.hook = _knowledge_refresher(self.service, vault=str(self.vault))
        self.tasks = []
        self.notifications = 0
        self.service.on_change = self.changed

    def changed(self):
        self.notifications += 1
        task = self.hook()
        if task is not None and task not in self.tasks:
            self.tasks.append(task)

    async def flush(self):
        while self.tasks:
            tasks, self.tasks = self.tasks, []
            await asyncio.gather(*tasks)

    async def learn(self, *, conversation="c1"):
        return await self.adaptive.process(turn("Ich mag Kaffee sehr gerne.",
            conversation_id=conversation, message_id="message-" + conversation,
            turn_id="turn-" + conversation), now=datetime.now(timezone.utc))

    async def active(self):
        return await self.service.semantic.memory.active_records()

    def manifest(self):
        return json.loads((self.vault / K.MANIFEST).read_text())["concepts"]

    def note(self, memory_id):
        return self.vault / self.manifest()[memory_id]["path"]


@asynccontextmanager
async def world(*payloads):
    with tempfile.TemporaryDirectory(prefix="solvio-memory-vault-") as folder:
        w = World(folder, payloads)
        try:
            yield w
        finally:
            await w.flush()
            await w.adaptive.close()
            await w.service.close()


async def t_adoption_refreshes_exact_bound_memory_without_opening_the_default_store():
    async with world() as w:
        with patch.object(KS, "memory_base_dir", side_effect=AssertionError("wrong default memory")), \
                patch.object(KS, "vault_dir", side_effect=AssertionError("wrong default vault")), \
                patch.object(KS, "SolvioMemory", side_effect=AssertionError("extra store")):
            outcome = await w.learn()
            await w.flush()
        require_equal(outcome.adopted, 1)
        require_equal(w.notifications, 1)
        records = await w.active()
        require_equal(len(records), 1)
        require_equal(records[0].source_type, SourceType.SOLVIO_INFERENCE)
        text = w.note(records[0].id).read_text()
        require(STATEMENT in text)
        require(O.canonical_hash(records[0]) in text)
        require("solvio_inference" in text and "agent_generated" in text)


async def t_reinforcement_updates_same_note_and_canonical_provenance():
    async with world() as w:
        await w.learn()
        await w.flush()
        first = (await w.active())[0]
        path = w.note(first.id)
        before = path.read_text()
        outcome = await w.learn(conversation="c2")
        await w.flush()
        require_equal(outcome.reinforced, 1)
        require_equal(w.notifications, 2)
        records = await w.active()
        require_equal([r.id for r in records], [first.id])
        require_equal(len(records[0].provenance), len(first.provenance) + 1)
        from test_adaptive_observation_recovery import source_of
        require_equal(records[0].provenance[-1].source,
                      source_of(turn(conversation_id="c2", message_id="message-c2")))
        require_equal(w.note(first.id), path)
        require(path.read_text() != before, "the compiler retained stale provenance metadata")
        require(O.canonical_hash(records[0]) in path.read_text())


async def t_correction_supersedes_machine_truth_and_refreshes_current_vault():
    async with world() as w:
        await w.learn()
        await w.flush()
        previous = (await w.active())[0]
        result = await w.capabilities.correct({"memory_id": previous.id,
                                              "statement": "Mag lieber Tee."})
        require(result["ok"])
        await w.flush()
        records = await w.active()
        require_equal([r.id for r in records], [result["memory_id"]])
        require_equal(records[0].source_type, SourceType.USER_DIRECT)
        require_equal(records[0].supersedes, previous.id)
        require("Mag lieber Tee." in w.note(records[0].id).read_text())
        old_note = w.note(previous.id)
        require(O.ARCHIVE in old_note.parts)
        require(STATEMENT not in old_note.read_text(), "superseded body remains copyable")
        require_equal(w.manifest()[previous.id]["removal_reason"], "superseded")
        # A later erasure must still reclassify an already archived record.
        require((await w.capabilities.purge({"memory_id": previous.id}))["ok"])
        await w.flush()
        erased = w.note(previous.id)
        require_equal(erased.name, "entfernt--" + previous.id[:8] + ".md")
        require(not old_note.exists())
        require_equal(w.manifest()[previous.id]["removal_reason"], "purged")
        require("kaffee" not in erased.read_text().lower())
        require_equal([r.id for r in await w.active()], [result["memory_id"]])


async def t_forget_removes_body_subject_and_filename_from_entire_vault():
    async with world() as w:
        await w.learn()
        await w.flush()
        record = (await w.active())[0]
        original = w.note(record.id)
        require("kaffee" in original.name)
        require((await w.capabilities.forget({"memory_id": record.id}))["ok"])
        await w.flush()
        require_equal(await w.active(), [])
        tombstone = w.note(record.id)
        require(not original.exists())
        require_equal(tombstone.name, "entfernt--" + record.id[:8] + ".md")
        require_equal(w.manifest()[record.id]["removal_reason"], "forgotten")
        require("forgotten" in tombstone.read_text())
        for path in w.vault.rglob("*"):
            require("kaffee" not in path.name.lower(), str(path))
            if path.is_file():
                require("kaffee" not in path.read_text().lower(), str(path))
        repeated = await w.learn(conversation="c2")
        await w.flush()
        require_equal(repeated.adopted, 0)
        require_equal(w.notifications, 2, "suppressed candidate triggered a new projection")


async def t_candidates_and_failed_mutations_never_notify_the_compiler():
    async with world(proposal(statement="Hat Diabetes.", memory_type="user",
                              subject="user:gesundheit", sensitivity="sensitive")) as w:
        outcome = await w.adaptive.process(turn("Ich habe seit einem Jahr Diabetes."))
        await w.flush()
        require_equal(outcome.asked, 1)
        require_equal(w.notifications, 0)
        require_equal(await w.active(), [])
        require(not w.vault.exists())
    async with world() as w:
        with patch.object(w.service.semantic, "remember", AsyncMock(side_effect=OSError("local fault"))):
            require_equal((await w.learn()).adopted, 0)
        await w.flush()
        require_equal(w.notifications, 0)
        require(not w.vault.exists())
        # A generic exception does not prove the canonical write rolled back.
        # A later conversation must not silently release that adoption claim.
        repeated = await w.learn(conversation="c2")
        require_equal(repeated.adopted, 0)
        require("candidate_adoption_unconfirmed" in repeated.reasons)
        await w.flush()
        require_equal(w.notifications, 0)
        require_equal(await w.active(), [])
        require(not w.vault.exists())
    async with world() as w:
        require_equal((await w.learn(conversation="c2")).adopted, 1)
        await w.flush()
        for failure in (AsyncMock(return_value="unavailable"), AsyncMock(side_effect=OSError("local fault"))):
            with patch.object(w.service.semantic.memory, "reinforce_observation", failure):
                require_equal((await w.learn(conversation="c3")).reinforced, 0)
            await w.flush()
            require_equal(w.notifications, 1)


async def t_repeated_refresh_preserves_content_free_tombstone():
    async with world() as w:
        await w.learn()
        await w.flush()
        record = (await w.active())[0]
        require((await w.capabilities.forget({"memory_id": record.id}))["ok"])
        await w.flush()
        tombstone = w.note(record.id)
        require(tombstone.exists())
        for _ in range(2):
            w.service.notify_changed()
            await w.flush()
            require(tombstone.exists(), "a later refresh deleted the existing tombstone")
            require_equal(w.manifest()[record.id]["removal_reason"], "forgotten")
            require("kaffee" not in tombstone.read_text().lower())


async def t_canonical_commit_notifies_even_if_candidate_finalization_fails():
    async with world() as w:
        with patch.object(w.adaptive.candidates, "transition", AsyncMock(side_effect=OSError("after commit"))):
            try:
                await w.learn()
            except OSError:
                pass
        await w.flush()
        records = await w.active()
        require_equal(len(records), 1)
        require_equal(w.notifications, 1)
        require(STATEMENT in w.note(records[0].id).read_text())


async def t_compiler_failure_neither_closes_injected_store_nor_reverts_learning():
    async with world() as w:
        store = w.service.semantic.memory
        with patch.object(store, "close", AsyncMock(wraps=store.close)) as close, \
                patch.object(K, "compile_bundle", side_effect=OSError("temporary vault failure")):
            require_equal((await w.learn()).adopted, 1)
            await w.flush()
            require_equal(close.await_count, 0)
            require_equal(len(await w.active()), 1)
        w.service.notify_changed()
        await w.flush()
        record = (await w.active())[0]
        require(STATEMENT in w.note(record.id).read_text())


async def t_default_and_explicit_base_dir_close_only_their_owned_store():
    async with world() as w:
        await w.learn()
        await w.flush()
        constructor = KS.SolvioMemory
        opened = []

        def create(base):
            require_equal(base, w.service.base_dir)
            store = constructor(base)
            store.close = AsyncMock(wraps=store.close)
            opened.append(store)
            return store

        with patch.object(KS, "SolvioMemory", side_effect=create), \
                patch.object(KS, "memory_base_dir", return_value=w.service.base_dir), \
                patch.object(KS, "vault_dir", return_value=str(w.vault)):
            await KS.compile_knowledge()
            await KS.compile_knowledge(str(w.vault), base_dir=w.service.base_dir)
            with patch.object(K, "compile_bundle", side_effect=OSError("owned failure")):
                try:
                    await KS.compile_knowledge(str(w.vault), base_dir=w.service.base_dir)
                except OSError:
                    pass
                else:
                    raise AssertionError("compiler failure was lost")
        require_equal(len(opened), 3)
        require(all(store.close.await_count == 1 for store in opened))
        require_equal(len(await w.active()), 1, "borrowed live service was closed")


async def t_bound_closed_service_refreshes_its_own_base_dir_without_falling_back():
    async with world() as w:
        await w.learn()
        await w.flush()
        record = (await w.active())[0]
        # The hook is bound to this service even after its live store closes.
        await w.service.close()
        w.note(record.id).unlink()
        with patch.object(KS, "memory_base_dir", side_effect=AssertionError("wrong default memory")):
            w.service.notify_changed()
            await w.flush()
        require(STATEMENT in w.note(record.id).read_text())
        require(w.service.semantic is None, "projection reopened or took over the live service")


async def t_rapid_correction_serializes_snapshots_and_never_leaves_older_truth_last():
    async with world() as w:
        store = w.service.semantic.memory
        active_records = store.active_records
        captured, release = asyncio.Event(), asyncio.Event()
        reads = 0

        async def held_snapshot(*args, **kwargs):
            nonlocal reads
            reads += 1
            records = await active_records(*args, **kwargs)
            if reads == 1:
                captured.set()
                await release.wait()
            return records

        with patch.object(store, "active_records", side_effect=held_snapshot):
            await w.learn()
            await asyncio.wait_for(captured.wait(), 3)
            record = (await active_records())[0]
            try:
                corrected = await w.capabilities.correct({"memory_id": record.id,
                                                          "statement": "Mag lieber Tee."})
                for _ in range(3):
                    w.service.notify_changed()
                await asyncio.sleep(0)
                require_equal(reads, 1, "another compiler read while an older snapshot was pending")
            finally:
                release.set()
            await w.flush()
        require_equal(reads, 2, "rapid notifications were not coalesced")
        current = (await w.active())[0]
        require_equal(current.id, corrected["memory_id"])
        require("Mag lieber Tee." in w.note(current.id).read_text())
        require_equal(w.manifest()[record.id]["removal_reason"], "superseded")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
