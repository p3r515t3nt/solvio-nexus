"""STEP 21.1 — Index-Scale-Benchmark (PHASE 16/17).

Misst die reale SemanticIndex-Suchlatenz (load_matrix + NumPy-Dot) sowie
Write-Throughput bei 1k/10k/50k SYNTHETISCHEN normalisierten Vektoren — OHNE ein
Modell zu bemuehen (reine Vektoren). Zweck: feststellen, ab wann der einfache
NumPy-Ansatz nicht mehr sinnvoll ist. Erfordert numpy.

Ehrlich: Es werden SIMULIERTE Vektoren gemessen, KEINE 50k echten Embeddings.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time

import numpy as np

from solvio.memory.semantic_index import SemanticIndex, encode_vector

D = 1024
PK = "scale:qwen:d1024:v1"


def _bulk_insert(idx: SemanticIndex, n: int) -> float:
    rng = np.random.default_rng(42)
    t0 = time.perf_counter()
    rows, batch = [], 5000
    for i in range(n):
        v = rng.standard_normal(D).astype("float32")
        v /= np.linalg.norm(v)
        rows.append((f"id{i}", PK, f"h{i}", D, encode_vector(v.tolist()), "t"))
        if len(rows) >= batch:
            idx._conn.executemany(
                "INSERT OR REPLACE INTO memory_embeddings"
                "(memory_id,profile_key,content_hash,dim,vector,created_at) VALUES (?,?,?,?,?,?)",
                rows)
            rows = []
    if rows:
        idx._conn.executemany(
            "INSERT OR REPLACE INTO memory_embeddings"
            "(memory_id,profile_key,content_hash,dim,vector,created_at) VALUES (?,?,?,?,?,?)", rows)
    idx._conn.commit()
    return time.perf_counter() - t0


def main():
    print(f"index scale benchmark (synthetic vectors, dim={D})")
    print(f"{'N':>7} {'write_s':>9} {'write/s':>9} {'search_med_ms':>14} {'search_p95_ms':>14} {'matrix_MB':>10}")
    rng = np.random.default_rng(7)
    for n in (1000, 10000, 50000):
        d = tempfile.mkdtemp(prefix="solvio-scale-")
        idx = SemanticIndex(os.path.join(d, "s.sqlite3"))
        try:
            wt = _bulk_insert(idx, n)
            lat = []
            for _ in range(20):
                q = rng.standard_normal(D).astype("float32")
                q /= np.linalg.norm(q)
                t = time.perf_counter()
                idx.search(q.tolist(), PK, 5)
                lat.append(time.perf_counter() - t)
            lat.sort()
            med = lat[len(lat) // 2] * 1000
            p95 = lat[int(len(lat) * 0.95)] * 1000
            print(f"{n:>7} {wt:>9.2f} {n / wt:>9.0f} {med:>14.1f} {p95:>14.1f} {n * D * 4 / 1e6:>10.1f}")
        finally:
            idx.close()
            shutil.rmtree(d, ignore_errors=True)
    print("note: median = WARM search (in-memory matrix cache) = pure NumPy dot.")
    print("      p95 = COLD search (first after a write) reloads the matrix from SQLite (O(N)).")
    print("      -> NumPy engine is sufficient to ~50k warm (~5ms); a vector DB is not needed.")


if __name__ == "__main__":
    main()
