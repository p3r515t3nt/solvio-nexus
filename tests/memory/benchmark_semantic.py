"""Semantic-Retrieval-Benchmark-Harness (STEP 21, PHASE 13/14).

Provider-parametrisiert: derselbe Harness laeuft mit deterministic / Qwen-local /
OpenAI. Misst Recall@1/3/5 + MRR fuer FTS-only, Semantic-only, Hybrid und fuehrt
Frozen Test 1 aus (0 Halluzinationen = es werden ausschliesslich existierende,
aktive MemoryRecords zurueckgegeben).

Standalone (deterministic):  python tests/memory/benchmark_semantic.py
"""
from __future__ import annotations

import asyncio
import statistics
import tempfile
import shutil
from datetime import datetime, timezone

from solvio.contracts.memory import MemoryRecord, MemoryType
from solvio.contracts.trust import SourceType, TrustLevel
from solvio.memory.embedding import HashingEmbeddingProvider
from solvio.memory.semantic import SemanticMemory
from tests.memory.benchmark_dataset import build_dataset


def _utc():
    return datetime.now(timezone.utc)


def _record(entry: dict) -> MemoryRecord:
    now = _utc()
    return MemoryRecord(
        id="", memory_type=MemoryType(entry["memory_type"]), content=entry["content"],
        subject=entry["subject"], source="benchmark", source_type=SourceType.USER_DIRECT,
        created_at=now, updated_at=now, trust_level=TrustLevel.USER_DIRECT,
        tags=entry.get("tags", []),
    )


async def ingest(sm: SemanticMemory, ds: dict) -> dict:
    """Kanonisch einspielen, dann batched rebuild (effizient fuer echte Modelle)."""
    goldmap = {}
    for f in ds["facts"]:
        goldmap[f["id"]] = await sm.memory.remember(_record(f))
    for d in ds["distractors"]:
        await sm.memory.remember(_record(d))
    await sm.rebuild(batch=128)
    return goldmap


async def _ranked_ids(sm: SemanticMemory, mode: str, query: str, k: int) -> list[str]:
    if mode == "fts":
        recs = await sm.lexical_recall(query, limit=k)
    elif mode == "semantic":
        recs = await sm.semantic_recall(query, limit=k, candidate_k=max(k, 50))
    else:  # hybrid
        recs = await sm.hybrid_recall(query, limit=k, fts_k=max(k, 50), sem_k=max(k, 50))
    return [r.id for r in recs]


def _metrics(ranks: list[int]) -> dict:
    """ranks: 1-basierte Position des Gold-Treffers, 0 = nicht gefunden."""
    n = len(ranks)
    def at(k):
        return round(sum(1 for r in ranks if 1 <= r <= k) / n, 4)
    mrr = round(sum((1.0 / r) if r >= 1 else 0.0 for r in ranks) / n, 4)
    return {"recall@1": at(1), "recall@3": at(3), "recall@5": at(5), "mrr": mrr, "n": n}


async def evaluate(sm: SemanticMemory, ds: dict, goldmap: dict,
                   modes=("fts", "semantic", "hybrid"), k: int = 10) -> dict:
    results = {}
    all_ids = set(goldmap.values())
    for mode in modes:
        ranks, halluc = [], 0
        for q in ds["queries"]:
            gold = goldmap[q["gold_id"]]
            ranked = await _ranked_ids(sm, mode, q["text"], k)
            rank = 0
            for pos, rid in enumerate(ranked, 1):
                if rid not in all_ids and rid not in {goldmap[f["id"]] for f in ds["facts"]}:
                    pass  # distractors are valid existing records, not hallucinations
                if rid == gold:
                    rank = pos
                    break
            ranks.append(rank)
        results[mode] = _metrics(ranks)
    return results


async def frozen_test_1(provider_factory, *, n_facts=50, n_distractors=500, n_queries=20) -> dict:
    """Frozen Test 1: >=18/20 korrekt (Gold in Top-3), 0 Halluzinationen."""
    ds = build_dataset(n_relevant=n_facts, n_distractors=n_distractors)
    ds["queries"] = ds["queries"][:n_queries]
    d = tempfile.mkdtemp(prefix="solvio-frozen1-")
    try:
        sm = SemanticMemory(d, provider_factory())
        goldmap = await ingest(sm, ds)
        all_existing = set()
        for r in await sm.memory.active_records():
            all_existing.add(r.id)
        out = {}
        for mode in ("fts", "semantic", "hybrid"):
            correct, halluc = 0, 0
            for q in ds["queries"]:
                gold = goldmap[q["gold_id"]]
                ranked = await _ranked_ids(sm, mode, q["text"], 3)
                for rid in ranked:
                    if rid not in all_existing:
                        halluc += 1  # nicht-existierender/inaktiver Record = Halluzination
                if gold in ranked:
                    correct += 1
            out[mode] = {"correct": correct, "total": len(ds["queries"]),
                         "hallucinations": halluc,
                         "pass": correct >= 18 and halluc == 0}
        await sm.close()
        return out
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def run_full(provider_factory, *, n_relevant=100, n_distractors=1000, label="provider") -> dict:
    ds = build_dataset(n_relevant=n_relevant, n_distractors=n_distractors)
    d = tempfile.mkdtemp(prefix="solvio-bench-")
    try:
        sm = SemanticMemory(d, provider_factory())
        goldmap = await ingest(sm, ds)
        res = await evaluate(sm, ds, goldmap)
        await sm.close()
        return {"label": label, "dataset": ds["meta"], "quality": res}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def print_results(r: dict) -> None:
    print(f"\n=== {r['label']} ===  dataset sha={r['dataset']['sha256'][:12]} "
          f"(facts={r['dataset']['n_facts']}, dist={r['dataset']['n_distractors']}, "
          f"q={r['dataset']['n_queries']})")
    print(f"{'mode':10s} {'R@1':>7} {'R@3':>7} {'R@5':>7} {'MRR':>7}")
    for mode, m in r["quality"].items():
        print(f"{mode:10s} {m['recall@1']:>7} {m['recall@3']:>7} {m['recall@5']:>7} {m['mrr']:>7}")


async def _main():
    r = await run_full(lambda: HashingEmbeddingProvider(dimension=256),
                       label="deterministic (hashing) — pipeline proof")
    print_results(r)
    f1 = await frozen_test_1(lambda: HashingEmbeddingProvider(dimension=256))
    print("\nFrozen Test 1 (>=18/20 top-3, 0 halluc):")
    for mode, o in f1.items():
        print(f"  {mode:10s} correct={o['correct']}/{o['total']} "
              f"halluc={o['hallucinations']} pass={o['pass']}")


if __name__ == "__main__":
    asyncio.run(_main())
