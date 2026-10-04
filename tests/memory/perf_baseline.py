"""Performance-Baseline der SOLVIO Memory Foundation (STEP 20).

Kein Benchmark-Framework, nur stdlib. Legt 1000 Records an und misst
Lookup/Recall/Search/History. Zielvorgabe: Lookups deutlich < 500 ms.

Ausfuehren:  python -m tests.memory.perf_baseline
"""
from __future__ import annotations

import asyncio
import statistics
import tempfile
import time
from datetime import datetime, timezone

from solvio.contracts.memory import MemoryRecord, MemoryType
from solvio.contracts.trust import SourceType, TrustLevel
from solvio.memory import SolvioMemory

N = 1000
WORDS = ["coffee", "berlin", "munich", "invoice", "meeting", "calendar", "family",
         "project", "reminder", "preference", "flight", "recipe", "budget", "garden"]


def _utc():
    return datetime.now(timezone.utc)


def rec(i: int) -> MemoryRecord:
    now = _utc()
    w = WORDS[i % len(WORDS)]
    return MemoryRecord(
        id="", memory_type=list(MemoryType)[i % len(list(MemoryType))],
        content=f"record {i} about {w} and {WORDS[(i * 7) % len(WORDS)]} number {i}",
        subject=f"subject:{w}:{i % 50}", source="voice",
        source_type=SourceType.USER_DIRECT, created_at=now, updated_at=now,
        trust_level=TrustLevel.USER_DIRECT, tags=[w, f"tag{i % 20}"],
    )


def ms(seconds: float) -> float:
    return round(seconds * 1000, 3)


async def main() -> None:
    d = tempfile.mkdtemp(prefix="solvio-mem-perf-")
    mem = SolvioMemory(d)
    ids: list[str] = []

    t0 = time.perf_counter()
    for i in range(N):
        ids.append(await mem.remember(rec(i)))
    ingest = time.perf_counter() - t0

    async def timeit(coro_factory, rounds=200):
        samples = []
        for k in range(rounds):
            t = time.perf_counter()
            await coro_factory(k)
            samples.append(time.perf_counter() - t)
        return samples

    get_s = await timeit(lambda k: mem.get(ids[k * 5 % N]))
    recall_s = await timeit(lambda k: mem.recall(WORDS[k % len(WORDS)], limit=10))
    search_s = await timeit(lambda k: mem.search(WORDS[k % len(WORDS)], limit=20))
    hist_s = await timeit(lambda k: mem.history(f"subject:coffee:{k % 50}"))

    def report(name, s):
        print(f"  {name:10s}  median={ms(statistics.median(s)):8.3f} ms  "
              f"p95={ms(sorted(s)[int(len(s)*0.95)]):8.3f} ms  "
              f"max={ms(max(s)):8.3f} ms")

    total = await mem.count()
    print(f"SOLVIO Memory perf baseline  (records={total})")
    print(f"  ingest {N} records: {ingest:.3f} s  ({ms(ingest / N):.3f} ms/record)")
    report("get", get_s)
    report("recall", recall_s)
    report("search", search_s)
    report("history", hist_s)

    med_get = ms(statistics.median(get_s))
    med_recall = ms(statistics.median(recall_s))
    ok = med_get < 500 and med_recall < 500
    print(f"  RESULT: lookups << 500ms target: {'PASS' if ok else 'FAIL'} "
          f"(get={med_get}ms, recall={med_recall}ms)")

    await mem.close()
    import shutil
    shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
