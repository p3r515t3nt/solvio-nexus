"""SOLVIO node test client (runs from the Mac Core side).

Connects over mTLS (client cert signed by the SOLVIO CA) to a node and exercises
health / capabilities / a synthetic compute.sha256 invoke. This is a TEST tool —
it is NOT the eventual Core node-router and performs no Core integration.

Usage:
  python node_client.py --base https://node.example.invalid:8443 --ca ca.crt \
      --cert mac.crt --key mac.key <health|caps|sha256> [text]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import aiohttp

sys.path.insert(0, __file__.rsplit("tools", 1)[0] + "src")
from solvio_node.security import client_ssl_context  # noqa: E402


async def _run(args) -> int:
    ssl_ctx = client_ssl_context(args.ca, args.cert, args.key)
    conn = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=conn) as s:
        if args.command == "health":
            async with s.get(f"{args.base}/v1/health") as r:
                print(r.status, json.dumps(await r.json(), indent=2))
        elif args.command == "caps":
            async with s.get(f"{args.base}/v1/capabilities") as r:
                data = await r.json()
                print(r.status, "capabilities:", [c["id"] for c in data["capabilities"]])
        elif args.command == "sha256":
            payload = {"payload": {"text": args.text}}
            async with s.post(f"{args.base}/v1/capabilities/compute.sha256/invoke",
                              json=payload) as r:
                print(r.status, json.dumps(await r.json(), indent=2))
        else:
            print("unknown command", file=sys.stderr)
            return 2
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--ca", required=True)
    p.add_argument("--cert", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("command", choices=["health", "caps", "sha256"])
    p.add_argument("text", nargs="?", default="synthetic-benchmark-string")
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
