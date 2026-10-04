"""Keepalive-Benchmark fuer den Core NodeClient (STEP 21.2E, PHASE 19).

Misst cold vs. warm (Keep-Alive) und vergleicht gegen den 21.2D-Fall
(neuer TLS-Handshake pro Request, ~55 ms). Nur synthetische Inputs.

Ausfuehren auf dem Mac (WireGuard-Tunnel oben, config/nodes.json gesetzt):
    .venv/bin/python scripts/node_bench.py
"""
from __future__ import annotations

import asyncio
import hashlib
import statistics
import time

from solvio.nodes.client import NodeClient
from solvio.nodes.config import load_nodes_config

NODE_ID = "hetzner-main"
WARM = 30
SEQ = 100
CONC = 20


def _ms(dt: float) -> float:
    return dt * 1000.0


async def main() -> None:
    cfg = load_nodes_config()
    if NODE_ID not in cfg.connections:
        raise SystemExit("config/nodes.json fehlt oder enthaelt hetzner-main nicht")
    conn = cfg.connections[NODE_ID]

    client = NodeClient(conn)
    try:
        # cold: erster Request baut Verbindung + TLS-Handshake auf
        t0 = time.perf_counter()
        await client.health()
        cold = _ms(time.perf_counter() - t0)

        # warm: wiederverwendete Keep-Alive-Verbindung
        warm: list[float] = []
        for _ in range(WARM):
            t = time.perf_counter()
            await client.health()
            warm.append(_ms(time.perf_counter() - t))
        warm.sort()
        p50 = statistics.median(warm)
        p95 = warm[max(0, int(len(warm) * 0.95) - 1)]

        # 100 sequentiell (Keep-Alive)
        t0 = time.perf_counter()
        for _ in range(SEQ):
            await client.health()
        seq_total = time.perf_counter() - t0

        # 20 gleichzeitig (innerhalb der Node-Limits)
        t0 = time.perf_counter()
        await asyncio.gather(*(client.health() for _ in range(CONC)))
        conc_total = _ms(time.perf_counter() - t0)

        # synthetischer sha256-Roundtrip (Korrektheit)
        text = "synthetic-benchmark-string"
        resp = await client.invoke("compute.sha256", {"text": text})
        ok = resp.result["hex"] == hashlib.sha256(text.encode()).hexdigest()
    finally:
        await client.aclose()

    # Vergleich: neuer Client (neuer Handshake) pro Request, wie 21.2D
    nokeep: list[float] = []
    for _ in range(10):
        c2 = NodeClient(conn)
        t = time.perf_counter()
        await c2.health()
        nokeep.append(_ms(time.perf_counter() - t))
        await c2.aclose()

    print("=== SOLVIO NodeClient Keepalive-Benchmark (PHASE 19) ===")
    print(f"cold (1. Request, inkl. Handshake): {cold:.1f} ms")
    print(f"warm keepalive p50:                 {p50:.1f} ms")
    print(f"warm keepalive p95:                 {p95:.1f} ms")
    print(f"100 sequential (keepalive):         {seq_total:.2f} s -> {SEQ/seq_total:.0f} req/s")
    print(f"{CONC} concurrent:                       {conc_total:.1f} ms")
    print(f"no-keepalive (neuer Handshake) avg: {statistics.mean(nokeep):.1f} ms  (21.2D-Vergleich)")
    print(f"sha256 roundtrip korrekt:           {ok}")


if __name__ == "__main__":
    asyncio.run(main())
