"""STEP 21.1 — Tuning-/Holdout-Harness (PHASE 5/6/7/8).

Effizient: Query-/Doc-Embeddings werden EINMAL berechnet (Provider), dann werden
alle Fusionsstrategien + Threshold-Kalibrierung deterministisch offline verglichen.

Usage:
  python -m tests.memory.benchmark_tuning dev    qwen     # alle Strategien auf DEV
  python -m tests.memory.benchmark_tuning holdout qwen     # FROZEN-Strategie auf HOLDOUT
"""
from __future__ import annotations

import asyncio
import shutil
import statistics
import sys
import tempfile
from datetime import datetime, timezone

from solvio.contracts.memory import MemoryRecord, MemoryType
from solvio.contracts.trust import SourceType, TrustLevel
from solvio.memory.embedding import HashingEmbeddingProvider, QwenLocalEmbeddingProvider
from solvio.memory.fusion import HybridConfig, fuse, is_no_match
from solvio.memory.semantic import SemanticMemory
from tests.memory.benchmark_datasets_v2 import build

K = 50
POS_CLASSES = ("semantic", "exact", "temporal")

# Kleiner, nachvollziehbarer Suchraum (PHASE 5).
GRID = [
    ("fts",                 HybridConfig(strategy="fts")),
    ("semantic",            HybridConfig(strategy="semantic")),
    ("rrf",                 HybridConfig(strategy="rrf")),
    ("weighted_rrf 2:1",    HybridConfig(strategy="weighted_rrf", semantic_weight=2.0, fts_weight=1.0)),
    ("weighted_rrf 3:1",    HybridConfig(strategy="weighted_rrf", semantic_weight=3.0, fts_weight=1.0)),
    ("semantic_dominant",   HybridConfig(strategy="semantic_dominant", dominant_fts_weight=0.15)),
    ("exact_boost 2:1 b2",  HybridConfig(strategy="exact_boost", semantic_weight=2.0, fts_weight=1.0, exact_boost=2.0)),
    ("exact_boost 3:1 b3",  HybridConfig(strategy="exact_boost", semantic_weight=3.0, fts_weight=1.0, exact_boost=3.0)),
]

# ---- Nach DEV-Tuning EINGEFROREN (siehe Report). ----
# DEV: semantic-only gewinnt klar auf allen Klassen (overall R@3=1.000, exact=1.000),
# jede Hybrid-Variante verwaessert -> PHASE-7-Regel: Semantic-primary + FTS-Fallback.
# No-Match-Threshold auf DEV kalibriert (FP=0): 0.6063 (Qwen-Profil-spezifisch).
FROZEN = HybridConfig(strategy="semantic", semantic_threshold=0.6063)


def _utc():
    return datetime.now(timezone.utc)


def _rec(f: dict) -> MemoryRecord:
    return MemoryRecord(
        id="", memory_type=MemoryType(f["memory_type"]), content=f["content"],
        subject=f["subject"], source="bench", source_type=SourceType.USER_DIRECT,
        created_at=_utc(), updated_at=_utc(), trust_level=TrustLevel.USER_DIRECT,
        tags=f.get("tags", []))


async def ingest(sm: SemanticMemory, ds: dict) -> dict:
    goldmap, temporal, plain = {}, {}, []
    for f in ds["facts"]:
        if f["kind"] == "temporal_old":
            temporal.setdefault(f["pair"], {})["old"] = f
        elif f["kind"] == "temporal_new":
            temporal.setdefault(f["pair"], {})["new"] = f
        else:
            plain.append(f)
    for f in plain:
        goldmap[f["id"]] = await sm.memory.remember(_rec(f))
    for _pair, d in sorted(temporal.items()):
        old_id = await sm.memory.remember(_rec(d["old"]))
        new = await sm.memory.supersede(old_id, _rec(d["new"]))
        goldmap[d["old"]["id"]] = old_id
        goldmap[d["new"]["id"]] = new.id
    for dd in ds["distractors"]:
        await sm.memory.remember(_rec(dd))
    await sm.rebuild(batch=128)
    return goldmap


async def collect(sm: SemanticMemory, ds: dict, goldmap: dict) -> dict:
    cache = {}
    for q in ds["queries"]:
        qvec = await sm._embed_query(q["text"])
        sem = await sm.semantic_hits(q["text"], K, qvec=qvec)
        fts = await sm.fts_candidate_ids(q["text"], K)
        recmap = await sm._visible_map(fts + [i for i, _ in sem])
        ctext = {rid: r.subject + " " + r.content for rid, r in recmap.items()}
        cache[q["id"]] = {
            "sem": sem, "fts": fts, "ctext": ctext, "visible": set(recmap),
            "top_sim": sem[0][1] if sem else -1.0,
            "gold": goldmap.get(q["gold_id"]) if q["gold_id"] else None,
            "qclass": q["qclass"], "text": q["text"],
        }
    return cache


def _metrics(ranks: list[int]) -> dict:
    n = max(len(ranks), 1)
    at = lambda k: round(sum(1 for r in ranks if 1 <= r <= k) / n, 3)
    mrr = round(sum((1.0 / r) if r else 0.0 for r in ranks) / n, 3)
    return {"r@1": at(1), "r@3": at(3), "r@5": at(5), "mrr": mrr, "n": len(ranks)}


def eval_ranking(cache: dict, cfg: HybridConfig) -> dict:
    per = {c: [] for c in POS_CLASSES}
    for c in cache.values():
        if c["qclass"] == "negative":
            continue
        fused = fuse(c["fts"], c["sem"], cfg, candidate_text=c["ctext"], query=c["text"]) \
            if c["sem"] else list(c["fts"])
        fused = [i for i in fused if i in c["visible"]]  # fail-closed Active-Truth
        rank = next((p for p, i in enumerate(fused, 1) if i == c["gold"]), 0)
        per[c["qclass"]].append(rank)
    out = {cl: _metrics(rk) for cl, rk in per.items()}
    out["overall"] = _metrics([r for rk in per.values() for r in rk])
    return out


def calibrate_threshold(cache: dict) -> dict:
    pos = [c["top_sim"] for c in cache.values() if c["qclass"] in POS_CLASSES and c["gold"]]
    neg = [c["top_sim"] for c in cache.values() if c["qclass"] == "negative"]
    cands = sorted(set(round(s, 4) for s in pos + neg))
    best = {"threshold": 0.0, "fp": 1.0, "fn": 1.0, "cost": 9.9}
    for thr in cands:
        fp = sum(1 for s in neg if s >= thr) / max(len(neg), 1)   # neg nicht unterdrückt
        fn = sum(1 for s in pos if s < thr) / max(len(pos), 1)    # pos unterdrückt
        cost = fp + fn
        if cost < best["cost"]:
            best = {"threshold": round(thr, 4), "fp": round(fp, 3), "fn": round(fn, 3),
                    "cost": round(cost, 3)}
    best["pos_median"] = round(statistics.median(pos), 4) if pos else None
    best["neg_median"] = round(statistics.median(neg), 4) if neg else None
    return best


def _print_eval(label, ev):
    print(f"  {label:22s} overall r@1={ev['overall']['r@1']:.3f} r@3={ev['overall']['r@3']:.3f} "
          f"mrr={ev['overall']['mrr']:.3f}  | sem r@3={ev['semantic']['r@3']:.3f} "
          f"exact r@3={ev['exact']['r@3']:.3f} temporal r@3={ev['temporal']['r@3']:.3f}")


def _provider(spec):
    if spec == "qwen":
        return QwenLocalEmbeddingProvider("Qwen/Qwen3-Embedding-0.6B", dimension=1024)
    return HashingEmbeddingProvider(dimension=256)


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "dev"
    spec = sys.argv[2] if len(sys.argv) > 2 else "deterministic"
    ds = build("dev" if mode == "dev" else "holdout")
    print(f"[{mode}] pool sha={ds['meta']['sha256'][:12]} classes={ds['meta']['classes']}")
    d = tempfile.mkdtemp(prefix=f"solvio-{mode}-")
    try:
        sm = SemanticMemory(d, _provider(spec))
        goldmap = await ingest(sm, ds)
        cache = await collect(sm, ds, goldmap)
        if mode == "dev":
            print("--- strategies on DEV ---")
            for label, cfg in GRID:
                _print_eval(label, eval_ranking(cache, cfg))
            print("--- no-match threshold calibration (DEV) ---")
            print("  ", calibrate_threshold(cache))
        else:
            ev = eval_ranking(cache, FROZEN)
            print("--- FROZEN strategy on HOLDOUT ---")
            print(f"  frozen = {FROZEN}")
            for cl in ("overall",) + POS_CLASSES:
                m = ev[cl]
                print(f"  {cl:10s} r@1={m['r@1']:.3f} r@3={m['r@3']:.3f} r@5={m['r@5']:.3f} "
                      f"mrr={m['mrr']:.3f} n={m['n']}")
            thr = FROZEN.semantic_threshold  # DEV-eingefroren, KEIN Re-Calibrate auf Holdout
            negs = [c for c in cache.values() if c["qclass"] == "negative"]
            poss = [c for c in cache.values() if c["qclass"] in POS_CLASSES and c["gold"]]
            fp = sum(1 for c in negs if c["top_sim"] >= thr)
            fn = sum(1 for c in poss if c["top_sim"] < thr)
            print(f"  no-match @ DEV-frozen threshold {thr}:")
            print(f"    negatives suppressed (TN): {len(negs) - fp}/{len(negs)}  (false-positive={fp})")
            print(f"    positives suppressed (false-negative): {fn}/{len(poss)}")
        await sm.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
