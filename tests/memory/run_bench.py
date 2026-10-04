"""Provider-Benchmark-Runner (STEP 21). Nur SYNTHETISCHE Daten.

Usage:
    python -m tests.memory.run_bench deterministic
    python -m tests.memory.run_bench qwen
    python -m tests.memory.run_bench openai-large     # braucht OPENAI_API_KEY im env
    python -m tests.memory.run_bench openai-small

Latenzen sind indikativ auf der jeweiligen Maschine (NICHT die produktive M4-Zahl).
"""
from __future__ import annotations

import asyncio
import sys
import time

from solvio.memory.embedding import (
    HashingEmbeddingProvider,
    OpenAIEmbeddingProvider,
    QwenLocalEmbeddingProvider,
)
from tests.memory.benchmark_semantic import run_full, frozen_test_1, print_results


def factory(spec: str):
    if spec == "deterministic":
        return lambda: HashingEmbeddingProvider(dimension=256)
    if spec == "qwen":
        return lambda: QwenLocalEmbeddingProvider("Qwen/Qwen3-Embedding-0.6B", dimension=1024)
    if spec == "openai-large":
        return lambda: OpenAIEmbeddingProvider("text-embedding-3-large")
    if spec == "openai-small":
        return lambda: OpenAIEmbeddingProvider("text-embedding-3-small")
    raise SystemExit(f"unknown provider spec: {spec}")


async def _latency_probe(spec: str) -> dict:
    """Cold-Load + Warm-Embed-Latenz (indikativ)."""
    prov = factory(spec)()
    t0 = time.perf_counter()
    await prov.embed_documents(["kalter Start Aufwaermtext"])          # cold (laedt Modell)
    cold = time.perf_counter() - t0
    warm = []
    for _ in range(10):
        t = time.perf_counter()
        await prov.embed_queries(["Wie heißt sein Hund?"])
        warm.append(time.perf_counter() - t)
    warm.sort()
    return {"cold_load_s": round(cold, 3),
            "warm_embed_ms_median": round(warm[len(warm) // 2] * 1000, 2)}


async def main():
    spec = sys.argv[1] if len(sys.argv) > 1 else "deterministic"
    t0 = time.perf_counter()
    if spec != "deterministic":
        try:
            print("latency:", await _latency_probe(spec))
        except Exception as exc:  # noqa: BLE001
            print(f"latency probe failed: {type(exc).__name__}: {exc}")
    r = await run_full(factory(spec), label=spec)
    print_results(r)
    f1 = await frozen_test_1(factory(spec))
    print("\nFrozen Test 1 (>=18/20 top-3, 0 halluc):")
    for mode, o in f1.items():
        print(f"  {mode:10s} correct={o['correct']}/{o['total']} "
              f"halluc={o['hallucinations']} pass={o['pass']}")
    print(f"\ntotal wall: {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
