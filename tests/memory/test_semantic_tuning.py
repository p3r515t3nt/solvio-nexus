"""STEP 21.1 — Tests fuer Fusion, No-Match, Dedup, Holdout-Trennung (PHASE 19).

Nur stdlib + deterministische Provider. Kein Netz, kein Modell.
Ausfuehren:  python tests/memory/test_semantic_tuning.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone

# Repo-Root auf den Pfad, damit 'tests.memory.*' auch beim Skript-Aufruf importierbar ist.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

from solvio.contracts.memory import MemoryRecord, MemoryType
from solvio.contracts.trust import SourceType, TrustLevel
from solvio.memory.embedding import EmbeddingProfile, HashingEmbeddingProvider
from solvio.memory.fusion import HybridConfig, exact_tokens, fuse, is_no_match
from solvio.memory.semantic import SemanticMemory
from tests.memory.benchmark_datasets_v2 import build


def _utc():
    return datetime.now(timezone.utc)


def mk(content, subject="user:x", **over):
    now = _utc()
    base = dict(id="", memory_type=MemoryType.USER, content=content, subject=subject,
                source="t", source_type=SourceType.USER_DIRECT, created_at=now,
                updated_at=now, trust_level=TrustLevel.USER_DIRECT)
    base.update(over)
    return MemoryRecord(**base)


class CountingProvider:
    """Zaehlt embed_queries-Aufrufe (fuer den Dedup-Test)."""
    def __init__(self):
        self._p = HashingEmbeddingProvider(256)
        self.query_calls = 0
        self.doc_calls = 0

    @property
    def profile(self):
        return self._p.profile

    async def embed_documents(self, texts):
        self.doc_calls += 1
        return await self._p.embed_documents(texts)

    async def embed_queries(self, texts):
        self.query_calls += 1
        return await self._p.embed_queries(texts)

    async def health(self):
        return {"network": False}


class FailingProvider:
    def __init__(self):
        self._p = EmbeddingProfile("failing", "boom", 256, "1")

    @property
    def profile(self):
        return self._p

    async def embed_documents(self, texts):
        raise RuntimeError("down")

    async def embed_queries(self, texts):
        raise RuntimeError("down")

    async def health(self):
        return {"network": False}


# ================================================= pure fusion unit tests
class TestFusion(unittest.TestCase):
    def test_semantic_strategy_is_semantic_order(self):
        sem = [("A", 0.9), ("B", 0.5), ("C", 0.1)]
        self.assertEqual(fuse(["C"], sem, HybridConfig(strategy="semantic")), ["A", "B", "C"])

    def test_fts_strategy_is_fts_order(self):
        self.assertEqual(fuse(["X", "Y"], [("A", 0.9)], HybridConfig(strategy="fts")), ["X", "Y"])

    def test_weighted_rrf_favours_semantic(self):
        fused = fuse(["B"], [("A", 0.9)],
                     HybridConfig(strategy="weighted_rrf", semantic_weight=3.0, fts_weight=1.0))
        self.assertEqual(fused[0], "A")

    def test_exact_boost_lifts_exact_token_match(self):
        sem = [("A", 0.95), ("B", 0.30)]     # A semantisch oben
        fts = ["B"]
        ctext = {"A": "irgendein text ohne code", "B": "Rechnung INV-123 Details"}
        cfg = HybridConfig(strategy="exact_boost", semantic_weight=1.0, fts_weight=1.0, exact_boost=5.0)
        fused = fuse(fts, sem, cfg, candidate_text=ctext, query="Wie hoch ist INV-123?")
        self.assertEqual(fused[0], "B")      # exakter Token-Treffer wird geboostet

    def test_exact_tokens_are_conservative(self):
        toks = exact_tokens("Rechnung INV-123 mag der")
        self.assertIn("inv-123", toks)       # Zahl/ID
        self.assertIn("rechnung", toks)       # Grossanfang
        self.assertNotIn("mag", toks)         # Allerweltswort raus
        self.assertNotIn("der", toks)

    def test_no_match_threshold(self):
        cfg = HybridConfig(semantic_threshold=0.5)
        self.assertTrue(is_no_match([("A", 0.2)], cfg))
        self.assertFalse(is_no_match([("A", 0.8)], cfg))
        self.assertTrue(is_no_match([], cfg))


# ================================================= holdout separation
class TestHoldoutSeparation(unittest.TestCase):
    def test_dev_and_holdout_disjoint(self):
        dev, hol = build("dev"), build("holdout")
        self.assertNotEqual(dev["meta"]["sha256"], hol["meta"]["sha256"])
        dev_facts = {f["content"] for f in dev["facts"]}
        hol_facts = {f["content"] for f in hol["facts"]}
        self.assertEqual(dev_facts & hol_facts, set())       # keine ueberlappenden Fakten
        dev_q = {q["text"] for q in dev["queries"]}
        hol_q = {q["text"] for q in hol["queries"]}
        # Queries ueberlappen hoechstens in generischen temporalen Formulierungen -> pruefe Anteil
        self.assertLess(len(dev_q & hol_q) / len(dev_q), 0.15)


# ================================================= integration on SemanticMemory
class TestIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="solvio-tune-")

    async def asyncTearDown(self):
        try:
            await self.sm.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_query_embedded_once_per_hybrid(self):
        prov = CountingProvider()
        self.sm = SemanticMemory(self.dir, prov)
        await self.sm.remember(mk("der nutzer trinkt kaffee", subject="u:a"))
        prov.query_calls = 0
        await self.sm.hybrid_recall("kaffee", limit=5)
        self.assertEqual(prov.query_calls, 1)   # genau EIN Query-Embedding (Dedup)

    async def test_active_truth_after_fusion(self):
        self.sm = SemanticMemory(self.dir, HashingEmbeddingProvider(256))
        rid = await self.sm.remember(mk("geheimer kaffee eintrag", subject="u:s"))
        # kanonisch purgen, Index-Vektor bleibt (Crash-Simulation)
        await self.sm.memory.purge(rid, reason="erase")
        res = await self.sm.hybrid_recall("geheimer kaffee", limit=5)
        self.assertNotIn(rid, [r.id for r in res])   # nach Fusion trotzdem gefiltert

    async def test_fts_fallback_when_provider_fails(self):
        self.sm = SemanticMemory(self.dir, HashingEmbeddingProvider(256))
        rid = await self.sm.remember(mk("flat white coffee morning", subject="u:b"))
        self.sm.provider = FailingProvider()
        res = await self.sm.hybrid_recall("flat white coffee", limit=5,
                                          config=HybridConfig(strategy="weighted_rrf"))
        self.assertIn(rid, [r.id for r in res])       # via FTS gefunden

    async def test_no_match_suppresses_semantic_recall(self):
        self.sm = SemanticMemory(self.dir, HashingEmbeddingProvider(256))
        await self.sm.remember(mk("kaffee fakt", subject="u:a"))
        cfg = HybridConfig(semantic_threshold=1.5)    # unerreichbar hoch -> immer No-Match
        self.assertEqual(await self.sm.semantic_recall("kaffee", config=cfg), [])

    async def test_index_cache_invalidated_on_write(self):
        self.sm = SemanticMemory(self.dir, HashingEmbeddingProvider(256))
        a = await self.sm.remember(mk("alpha kaffee eins", subject="u:a"))
        r1 = await self.sm.semantic_recall("alpha kaffee eins", limit=5)  # baut Cache
        self.assertIn(a, [r.id for r in r1])
        b = await self.sm.remember(mk("alpha kaffee zwei", subject="u:b"))  # Write -> invalidate
        self.assertIn(b, [r.id for r in await self.sm.semantic_recall("alpha kaffee zwei", limit=5)])
        await self.sm.purge(a, reason="x")               # Write -> invalidate
        self.assertNotIn(a, [r.id for r in await self.sm.semantic_recall("alpha kaffee eins", limit=5)])


if __name__ == "__main__":
    from _harness import run_unittest
    raise SystemExit(run_unittest(globals(), __name__))
