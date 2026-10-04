"""STEP 21 — Semantic-Memory-Korrektheits- & Privacy-Tests.

Nur Standardbibliothek + deterministischer Provider (kein Netz, kein Modell, keine
Cloud). Validiert die gesamte Pipeline und alle STEP-20-Garantien im semantischen
Pfad: Active-Truth-Filter, Purge/Forget/Supersession, Crash-Stale-Vektor-Deny,
Rebuild, Migration, Provider-/Index-Fallback, Restore.

Ausfuehren:  python tests/memory/test_semantic.py
"""
from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from solvio.contracts.memory import MemoryRecord, MemoryType, Sensitivity
from solvio.contracts.trust import SourceType, TrustLevel
from solvio.memory.embedding import (
    EmbeddingProfile,
    HashingEmbeddingProvider,
    l2_normalize,
)
from solvio.memory.embedding_text import embedding_text, content_hash, record_embedding_text_and_hash
from solvio.memory.semantic import SemanticMemory
from solvio.memory.semantic_index import decode_vector, encode_vector


def _utc():
    return datetime.now(timezone.utc)


def mk(content, subject="user:x", **over) -> MemoryRecord:
    now = _utc()
    base = dict(
        id="", memory_type=MemoryType.USER, content=content, subject=subject,
        source="voice", source_type=SourceType.USER_DIRECT, created_at=now,
        updated_at=now, trust_level=TrustLevel.USER_DIRECT,
    )
    base.update(over)
    return MemoryRecord(**base)


class FailingProvider:
    """Provider, dessen Embedding immer fehlschlaegt (Fallback-Tests)."""
    def __init__(self, dim=256):
        self._p = EmbeddingProfile("failing", "boom", dim, "1")

    @property
    def profile(self):
        return self._p

    async def embed_documents(self, texts):
        raise RuntimeError("provider unavailable")

    async def embed_queries(self, texts):
        raise RuntimeError("provider unavailable")

    async def health(self):
        return {"ok": False}


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="solvio-sem-")
        self.provider = HashingEmbeddingProvider(dimension=256)
        self.sm = SemanticMemory(self.dir, self.provider)

    async def asyncTearDown(self):
        try:
            await self.sm.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


# ============================================= embedding_text / hash / vector
class TestEmbeddingText(Base):
    async def test_embedding_text_deterministic(self):
        r = mk("the user drinks flat white coffee", subject="user:beverage",
               tags=["Coffee", "habit", "coffee"])
        t1 = embedding_text(r)
        t2 = embedding_text(r)
        self.assertEqual(t1, t2)
        self.assertIn("subject: user:beverage", t1)
        self.assertIn("flat white coffee", t1)
        self.assertIn("tags: coffee, habit", t1)  # dedupe + sort + lowercase

    async def test_content_hash_changes_with_content(self):
        h1 = content_hash(embedding_text(mk("blue")))
        h2 = content_hash(embedding_text(mk("green")))
        self.assertNotEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    async def test_secret_reference_content_not_embedded(self):
        r = mk("secret_ref://keychain/openai_key", subject="secret:openai",
               sensitivity=Sensitivity.SECRET_REFERENCE)
        txt = embedding_text(r)
        self.assertIn("subject: secret:openai", txt)
        self.assertNotIn("secret_ref://", txt)  # Verweis wird NICHT eingebettet
        self.assertNotIn("keychain", txt)

    async def test_vector_roundtrip(self):
        v = l2_normalize([0.1, -0.2, 0.3, 0.4])
        back = decode_vector(encode_vector(v))
        for a, b in zip(v, back):
            self.assertAlmostEqual(a, b, places=5)


# ============================================= semantic / hybrid recall
class TestRecall(Base):
    async def _seed(self):
        ids = {}
        ids["coffee"] = await self.sm.remember(mk(
            "the user drinks flat white coffee every morning", subject="user:beverage",
            memory_type=MemoryType.PREFERENCE))
        ids["city"] = await self.sm.remember(mk(
            "the user lives in Munich Germany", subject="user:city"))
        ids["dog"] = await self.sm.remember(mk(
            "the user has a golden retriever dog named Rex", subject="user:pet"))
        for i in range(20):
            await self.sm.remember(mk(f"unrelated distractor fact number {i} about weather",
                                      subject=f"noise:{i}"))
        return ids

    async def test_semantic_recall_finds_target(self):
        ids = await self._seed()
        res = await self.sm.semantic_recall("coffee every morning", limit=5)
        self.assertIn(ids["coffee"], [r.id for r in res])

    async def test_hybrid_recall_finds_target(self):
        ids = await self._seed()
        res = await self.sm.hybrid_recall("where does the user live", limit=5)
        self.assertIn(ids["city"], [r.id for r in res])

    async def test_negative_query_returns_no_strong_match(self):
        await self._seed()
        # Query ohne Token-Ueberlappung zu echten Fakten -> keine Fakten erfunden.
        res = await self.sm.semantic_recall("quantum chromodynamics lagrangian", limit=5)
        # Retrieval darf nichts Falsches als sichere Wahrheit liefern; leere oder
        # nur schwache Treffer sind akzeptabel — hier: kein Ziel-Fakt.
        self.assertNotIn("golden retriever", " ".join(r.content for r in res))

    async def test_retrieval_returns_only_existing_records(self):
        ids = await self._seed()
        res = await self.sm.hybrid_recall("coffee", limit=5)
        for r in res:
            self.assertIsNotNone(await self.sm.get(r.id))


# ============================================= purge / forget / supersession
class TestActiveTruthFilter(Base):
    async def test_purge_removes_vector_and_filters(self):
        rid = await self.sm.remember(mk("secret diary coffee entry", subject="user:diary"))
        self.assertEqual(await self.sm.semantic_count(), 1)
        self.assertTrue(await self.sm.purge(rid, reason="erase"))
        self.assertEqual(await self.sm.semantic_count(), 0)  # Vektor entfernt
        self.assertEqual(await self.sm.semantic_recall("secret diary coffee"), [])
        self.assertEqual(await self.sm.hybrid_recall("secret diary coffee"), [])

    async def test_crash_stale_vector_still_denied(self):
        rid = await self.sm.remember(mk("secret diary coffee entry", subject="user:diary"))
        # Simuliere Crash: kanonisch purgen, aber Index-Vektor bleibt (kein sem.purge).
        await self.sm.memory.purge(rid, reason="erase")
        self.assertEqual(await self.sm.semantic_count(), 1)  # stale Vektor da
        # fail-closed: Retrieval liefert den Record NICHT (Active-Truth-Filter).
        self.assertEqual(await self.sm.semantic_recall("secret diary coffee"), [])
        self.assertEqual(await self.sm.hybrid_recall("secret diary coffee"), [])
        # reconcile entfernt den stale Vektor.
        out = await self.sm.reconcile()
        self.assertEqual(out["removed_stale"], 1)
        self.assertEqual(await self.sm.semantic_count(), 0)

    async def test_forget_filters_from_semantic(self):
        rid = await self.sm.remember(mk("forgettable coffee note", subject="user:n"))
        self.assertTrue(await self.sm.forget(rid, reason="user asked"))
        self.assertEqual(await self.sm.semantic_recall("coffee note"), [])
        self.assertEqual(await self.sm.semantic_count(), 0)

    async def test_supersession_only_new_truth(self):
        old = await self.sm.remember(mk("the user lives in Berlin", subject="user:city"))
        new = await self.sm.supersede(old, mk("the user lives in Munich", subject="user:city"))
        res = await self.sm.hybrid_recall("where does the user live", limit=5)
        got = [r.id for r in res]
        self.assertIn(new.id, got)
        self.assertNotIn(old, got)

    async def test_update_reembeds_on_content_change(self):
        rid = await self.sm.remember(mk("the user likes tea", subject="user:beverage"))
        _, h_old = record_embedding_text_and_hash(await self.sm.get(rid))
        await self.sm.update(rid, {"content": "the user likes strong espresso coffee"})
        stored = await self.sm.get(rid)
        _, h_new = record_embedding_text_and_hash(stored)
        self.assertNotEqual(h_old, h_new)
        idx_hash = await self.sm._irun(self.sm.index.content_hash_of, rid, self.sm.profile.key)
        self.assertEqual(idx_hash, h_new)  # Index traegt neuen Hash, nicht den alten


# ============================================= profiles / mismatch / migration
class TestProfiles(Base):
    async def test_dimension_mismatch_skipped(self):
        await self.sm.remember(mk("coffee fact one", subject="user:a"))
        # Query mit falscher Dimension darf keine Kandidaten liefern (kein Mix).
        wrong_dim_query = [0.0] * 128
        hits = await self.sm._irun(self.sm.index.search, wrong_dim_query, self.sm.profile.key, 5)
        self.assertEqual(hits, [])

    async def test_profile_isolation(self):
        # Zwei verschiedene Profile duerfen nicht vermischt werden.
        p2 = HashingEmbeddingProvider(dimension=256, profile_version="2")
        self.assertNotEqual(self.sm.profile.key, p2.profile.key)

    async def test_migration_rebuilds_new_profile(self):
        ids = [await self.sm.remember(mk(f"coffee fact {i}", subject=f"user:{i}")) for i in range(5)]
        p2 = HashingEmbeddingProvider(dimension=128, profile_version="2")
        out = await self.sm.migrate_to(p2)
        self.assertEqual(out["indexed"], 5)
        self.assertEqual(self.sm.profile.key, p2.profile.key)
        # Retrieval funktioniert unter neuem Profil weiter.
        res = await self.sm.semantic_recall("coffee fact", limit=5)
        self.assertTrue(res)
        self.assertIn(res[0].id, ids)


# ============================================= rebuild
class TestRebuild(Base):
    async def test_rebuild_after_full_drop(self):
        ids = [await self.sm.remember(mk(f"coffee memory {i}", subject=f"user:{i}")) for i in range(6)]
        await self.sm.memory.forget(ids[0], reason="x")  # nicht mehr aktiv
        self.sm.index.drop_all()
        self.assertEqual(await self.sm.semantic_count(), 0)
        out = await self.sm.rebuild()
        self.assertEqual(out["indexed"], 5)  # nur aktive Records (6 - 1 forgotten)
        res = await self.sm.semantic_recall("coffee memory", limit=10)
        self.assertNotIn(ids[0], [r.id for r in res])  # forgotten bleibt draussen

    async def test_rebuild_resumable_skips_current(self):
        for i in range(4):
            await self.sm.remember(mk(f"coffee memory {i}", subject=f"user:{i}"))
        out = await self.sm.rebuild()  # alles schon aktuell -> nichts neu
        self.assertEqual(out["indexed"], 0)


# ============================================= failure / fallback
class TestFailureFallback(Base):
    async def test_provider_failure_falls_back_to_lexical(self):
        rid = await self.sm.remember(mk("the user drinks flat white coffee", subject="user:b"))
        # Provider durch fehlerhaften ersetzen -> hybrid faellt auf FTS zurueck.
        self.sm.provider = FailingProvider(dim=256)
        res = await self.sm.hybrid_recall("flat white coffee", limit=5)
        self.assertIn(rid, [r.id for r in res])  # via FTS gefunden
        self.assertEqual(await self.sm.semantic_recall("flat white coffee"), [])  # semantic leer

    async def test_missing_index_file_rebuildable(self):
        rid = await self.sm.remember(mk("coffee fact for rebuild", subject="user:b"))
        await self.sm._irun(self.sm.index.close)
        os.remove(self.sm.index_path)
        for sfx in ("-wal", "-shm"):
            p = self.sm.index_path + sfx
            if os.path.exists(p):
                os.remove(p)
        self.sm._open_index()  # neu anlegen
        self.assertEqual(await self.sm.semantic_count(), 0)
        await self.sm.rebuild()
        res = await self.sm.semantic_recall("coffee fact rebuild", limit=5)
        self.assertIn(rid, [r.id for r in res])

    async def test_corrupt_index_falls_back_to_lexical(self):
        rid = await self.sm.remember(mk("the user drinks flat white coffee", subject="user:b"))
        await self.sm._irun(self.sm.index.close)
        with open(self.sm.index_path, "r+b") as f:
            f.seek(0)
            f.write(b"\x00" * 200)  # Header zerstoeren
        self.sm._open_index()  # korrupt -> semantic_available=False
        self.assertFalse(self.sm.semantic_available)
        # Memory + FTS funktionieren weiter.
        res = await self.sm.hybrid_recall("flat white coffee", limit=5)
        self.assertIn(rid, [r.id for r in res])


# ============================================= restore
class TestRestore(Base):
    async def test_restore_pre_purge_semantic_does_not_reactivate(self):
        keep = await self.sm.remember(mk("public coffee fact", subject="user:keep"))
        secret = await self.sm.remember(mk("the secret diary therapy entry", subject="user:diary"))
        bk = await self.sm.backup()                      # PRE-PURGE (secret im Backup)
        await self.sm.purge(secret, reason="erase")
        # stale Pre-Purge-Vektor kuenstlich wieder einsetzen (simuliert Alt-Index).
        txt, h = record_embedding_text_and_hash(mk("the secret diary therapy entry", subject="user:diary"))
        vec = (await HashingEmbeddingProvider(256).embed_documents([txt]))[0]
        await self.sm._irun(self.sm.index.upsert, secret, self.sm.profile.key, h, vec)
        # Restore des alten Backups.
        stats = await self.sm.restore(bk["path"])
        self.assertIsNone(await self.sm.get(secret))                 # bleibt gepurgt
        # Invariante: der gepurgte Secret-Record kehrt NICHT zurueck (weder als id
        # noch als Inhalt). Dass der noch aktive keep-Record als schwacher top-k-
        # Kandidat erscheint, ist erlaubt (Retrieval erfindet nichts).
        for res in (await self.sm.semantic_recall("secret diary therapy"),
                    await self.sm.hybrid_recall("secret diary therapy")):
            self.assertNotIn(secret, [r.id for r in res])
            self.assertNotIn("therapy", " ".join(r.content for r in res))
        self.assertIsNotNone(await self.sm.get(keep))
        self.assertGreaterEqual(stats["semantic"]["removed_stale"], 1)  # stale Vektor entfernt


# ============================================= privacy: no network in unit tests
class TestNoCloud(Base):
    async def test_provider_is_offline(self):
        h = await self.provider.health()
        self.assertFalse(h.get("network", False))  # deterministic provider = offline


if __name__ == "__main__":
    from _harness import run_unittest
    raise SystemExit(run_unittest(globals(), __name__))
