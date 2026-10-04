"""Misst, was der Core am Eingang wirklich tut — mit einem Satelliten aus Papier.

Der Satellit ist nicht erreichbar, aber die Frage, an der dieser Umbau haengt,
ist eine ueber den CORE: nimmt er waehrend des Oeffnens der Anbietersitzung
Audio entgegen, oder steht sein Eingang still?

Frueher stand er still. `await sess.open()` lag in derselben Schleife, die die
Frames entgegennimmt, und blockierte sie fuer gemessene 530 bis 1675 ms.
websockets bremst den Absender dann aus (max_queue=16, rund 320 ms Audio), und
was der echte Satellit in seiner eigenen Warteschlange nicht mehr unterbringt,
entscheidet er allein — der Core hat es verursacht und konnte es nicht sehen.

Dieses Werkzeug verhaelt sich wie ein Satellit, der SOFORT nach `session_start`
zu sprechen anfaengt, und berichtet, wie viel davon ankam, bevor der Anbieter
bereit war. Es sendet ein Muster, keine Aufnahme: es gibt hier kein Mikrofon.

    python scripts/voice_ingress_probe.py [--seconds 2.0]

Die echte Sprachpruefung ersetzt das nicht. Es beantwortet genau eine Frage,
und zwar die, ohne deren Antwort der Umbau blosse Vermutung waere.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from websockets.asyncio.client import connect            # noqa: E402

from solvio.realtime import satellite_auth as SA          # noqa: E402

#: Was ein Satellit schickt: 16 kHz, 16 bit, ein Kanal, 20-ms-Haeppchen.
RATE = 16000
FRAME_MS = 20
FRAME_BYTES = int(RATE * FRAME_MS / 1000) * 2


async def probe(url: str, satellite_id: str, secret: bytes,
                seconds: float) -> dict:
    started = time.monotonic()
    marks: dict[str, float] = {}
    async with connect(url, max_size=None) as ws:
        marks["verbunden"] = time.monotonic()

        challenge = json.loads(await ws.recv())
        client_nonce = os.urandom(16).hex()
        auth = SA.compute_auth(
            secret, protocol_version=challenge["protocol_version"],
            server_nonce=challenge["server_nonce"], satellite_id=satellite_id,
            client_nonce=client_nonce)
        await ws.send(json.dumps({
            "type": "hello", "protocol_version": challenge["protocol_version"],
            "satellite_id": satellite_id, "client_nonce": client_nonce,
            "auth": auth}))
        marks["angemeldet"] = time.monotonic()

        await ws.send(json.dumps({"type": "session_start"}))
        marks["session_start"] = time.monotonic()

        # Ab hier wird gesprochen — ohne auf `session_ready` zu warten. Genau
        # das ist der Unterschied zum heutigen Satelliten, und genau das soll
        # der Core aushalten.
        ready = asyncio.Event()
        sent = {"frames": 0, "before_ready": 0}

        async def listen():
            try:
                async for message in ws:
                    if isinstance(message, (bytes, bytearray)):
                        continue
                    event = json.loads(message)
                    kind = event.get("type", "")
                    if kind == "session_ready":
                        marks["session_ready"] = time.monotonic()
                        ready.set()
                    elif kind == "session_end":
                        marks["session_end"] = time.monotonic()
                        ready.set()
                        return
            except Exception:  # noqa: BLE001
                ready.set()

        listener = asyncio.create_task(listen())
        frame = bytes(FRAME_BYTES)          # Stille als Muster, kein Raumklang
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await ws.send(frame)
            sent["frames"] += 1
            if not ready.is_set():
                sent["before_ready"] += 1
            await asyncio.sleep(FRAME_MS / 1000)

        await ws.send(json.dumps({"type": "session_end"}))
        listener.cancel()

    def ms(a: str, b: str):
        if a in marks and b in marks:
            return int((marks[b] - marks[a]) * 1000)
        return None

    return {
        "frames_gesendet": sent["frames"],
        "davon_vor_bereit": sent["before_ready"],
        "anmeldung_ms": ms("verbunden", "angemeldet"),
        "bis_session_ready_ms": ms("session_start", "session_ready"),
        "session_ready_kam": "session_ready" in marks,
        "session_end_kam": "session_end" in marks,
        "gesamt_ms": int((time.monotonic() - started) * 1000),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--seconds", type=float, default=2.5)
    parser.add_argument("--credentials",
                        default=os.path.expanduser("~/.solvio/satellite_auth.json"))
    args = parser.parse_args()

    credentials = SA.load_credentials(args.credentials)
    satellite_id = credentials.satellite_ids[0]
    secret = credentials._secrets[satellite_id]

    result = asyncio.run(probe(f"ws://{args.host}:{args.port}", satellite_id,
                               secret, args.seconds))
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
