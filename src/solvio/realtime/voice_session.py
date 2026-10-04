"""End-to-End-Sprachsitzung: Satellit <-> SOLVIO Core <-> OpenAI Realtime.

Bewusst klein und nachvollziehbar gehalten. Der Satellit bleibt ein duenner
Zubringer, der ausschliesslich rohes Audio liefert und abspielt. Saemtliche
Logik, Umrechnung und vor allem der API-Schluessel bleiben auf dem Mac.

Datenfluss:
    Pi (16 kHz mono) --WebSocket binaer--> Mac --16->24 kHz--> OpenAI
    OpenAI (24 kHz)  --24->16 kHz--> Mac --WebSocket binaer--> Pi
"""

from __future__ import annotations

import asyncio
import base64
import json
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve as ws_serve

from solvio.audio.resample import Resampler
from solvio.logging_setup import get_logger

REALTIME_URL = "wss://api.openai.com/v1/realtime"
SATELLITE_RATE = 16000
OPENAI_RATE = 24000

log = get_logger("solvio.voice")

SYSTEM_INSTRUCTIONS = (
    "Du bist SOLVIO, ein persoenlicher Sprachassistent im Zuhause des Benutzers. "
    "Sprich IMMER Deutsch, auch wenn der Benutzer etwas auf Englisch sagt. Antworte natuerlich, freundlich und eher kurz. "
    "Dies ist ein gesprochenes Gespraech, vermeide Aufzaehlungen und lange Erklaerungen. "
    "Wenn der Benutzer dich unterbricht, hoere sofort auf zu sprechen und hoere ihm zu."
)


@dataclass
class Metrics:
    """Sammelt Messwerte des Durchlaufs. Reine Beobachtung, kein Eingriff."""

    rtt_samples_ms: list[float] = field(default_factory=list)
    chunks_from_pi: int = 0
    bytes_from_pi: int = 0
    chunks_to_pi: int = 0
    bytes_to_pi: int = 0
    speech_started: list[float] = field(default_factory=list)
    speech_stopped: list[float] = field(default_factory=list)
    response_created: list[float] = field(default_factory=list)
    first_audio: list[float] = field(default_factory=list)
    turn_latencies_ms: list[float] = field(default_factory=list)
    barge_ins: int = 0
    event_counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    transcripts: list[str] = field(default_factory=list)

    def note_event(self, etype: str) -> None:
        self.event_counts[etype] = self.event_counts.get(etype, 0) + 1

    @property
    def rtt_median_ms(self) -> float | None:
        return statistics.median(self.rtt_samples_ms) if self.rtt_samples_ms else None

    def render(self) -> str:
        lines = ["", "=========== MESSWERTE ==========="]
        rtt = self.rtt_median_ms
        if rtt is not None:
            lines.append(f"Pi <-> Mac Rundlaufzeit (Median): {rtt:.1f} ms")
            lines.append(f"  daraus geschaetzt einfacher Weg: {rtt / 2:.1f} ms")
            lines.append(f"  Messpunkte: {len(self.rtt_samples_ms)}")
        else:
            lines.append("Pi <-> Mac Rundlaufzeit: nicht gemessen")
        lines.append("")
        lines.append(f"Audio vom Pi:  {self.chunks_from_pi} Chunks, {self.bytes_from_pi / 1024:.0f} KiB")
        lines.append(f"Audio zum Pi:  {self.chunks_to_pi} Chunks, {self.bytes_to_pi / 1024:.0f} KiB")
        lines.append("")
        lines.append(f"Sprechbeginn erkannt: {len(self.speech_started)}x")
        lines.append(f"Sprechende erkannt:   {len(self.speech_stopped)}x")
        lines.append(f"Antworten begonnen:   {len(self.response_created)}x")
        lines.append(f"Barge-in ausgeloest:  {self.barge_ins}x")
        if self.turn_latencies_ms:
            lines.append("")
            lines.append("Sprechende -> erstes Antwort-Audio auf dem Mac:")
            for i, v in enumerate(self.turn_latencies_ms, 1):
                lines.append(f"  Zug {i}: {v:.0f} ms")
            lines.append(f"  Median: {statistics.median(self.turn_latencies_ms):.0f} ms")
        if self.transcripts:
            lines.append("")
            lines.append("Was SOLVIO gesagt hat:")
            for t in self.transcripts:
                lines.append(f"  \"{t.strip()}\"")
        if self.errors:
            lines.append("")
            lines.append("Fehler:")
            for e in self.errors:
                lines.append(f"  {e}")
        lines.append("")
        lines.append("Ereignisse: " + ", ".join(f"{k}={v}" for k, v in sorted(self.event_counts.items())))
        lines.append("=================================")
        return "\n".join(lines)


class VoiceSession:
    def __init__(self, api_key: str, model: str, host: str, port: int,
                 reasoning_effort: str = "minimal", voice: str = "marin",
                 eagerness: str = "high") -> None:
        self._api_key = api_key.strip()
        self.model = model
        self.host = host
        self.port = port
        self.reasoning_effort = reasoning_effort
        self.voice = voice
        self.eagerness = eagerness

        self.metrics = Metrics()
        self.up = Resampler(SATELLITE_RATE, OPENAI_RATE)     # Pi -> OpenAI
        self.down = Resampler(OPENAI_RATE, SATELLITE_RATE)   # OpenAI -> Pi

        self._pi_ws: Any = None
        self._oa_ws: Any = None
        self._stop = asyncio.Event()
        self._pending_turn_start: float | None = None
        self._awaiting_first_audio = False

    # ---------------------------------------------------------------- OpenAI

    async def _configure_session(self, ws: Any) -> None:
        """Sitzung fuer gesprochene Unterhaltung einrichten.

        Bewusst in zwei Schritten: zuerst die sicher verifizierte
        Audio-Konfiguration, danach separat der Denkaufwand. Sollte der
        Server den zweiten Teil nicht kennen, bleibt die Sitzung trotzdem
        vollstaendig arbeitsfaehig.
        """
        session: dict[str, Any] = {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": SYSTEM_INSTRUCTIONS,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": OPENAI_RATE},
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": self.eagerness,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": OPENAI_RATE},
                    "voice": self.voice,
                },
            },
        }
        await ws.send(json.dumps({"type": "session.update", "session": session}))
        log.info("voice.session_configured", voice=self.voice, eagerness=self.eagerness)

        await ws.send(json.dumps({
            "type": "session.update",
            "session": {"type": "realtime", "reasoning": {"effort": self.reasoning_effort}},
        }))
        log.info("voice.reasoning_requested", effort=self.reasoning_effort)

    async def _openai_reader(self) -> None:
        """Ereignisse der Realtime-Schnittstelle verarbeiten."""
        assert self._oa_ws is not None
        async for raw in self._oa_ws:
            event = json.loads(raw)
            etype = event.get("type", "")
            self.metrics.note_event(etype)

            # Der Ereignisname fuer Ausgabe-Audio ist in der Dokumentation
            # nicht eindeutig. Beide bekannten Schreibweisen akzeptieren.
            if etype in ("response.output_audio.delta", "response.audio.delta"):
                await self._handle_audio_delta(event)

            elif etype == "input_audio_buffer.speech_started":
                self.metrics.speech_started.append(time.monotonic())
                log.info("voice.user_speaking")
                await self._flush_playback()

            elif etype == "input_audio_buffer.speech_stopped":
                now = time.monotonic()
                self.metrics.speech_stopped.append(now)
                self._pending_turn_start = now
                self._awaiting_first_audio = True
                log.info("voice.user_stopped")

            elif etype == "response.created":
                self.metrics.response_created.append(time.monotonic())

            elif etype in ("response.output_audio_transcript.done",
                           "response.audio_transcript.done"):
                text = event.get("transcript", "")
                if text:
                    self.metrics.transcripts.append(text)
                    print(f"\n  SOLVIO sagt: {text.strip()}\n")

            elif etype == "response.done":
                log.info("voice.response_done")

            elif etype == "error":
                err = event.get("error", {})
                msg = f"[{err.get('code') or err.get('type')}] {err.get('message', '')}"
                self.metrics.errors.append(msg)
                log.error("voice.api_error", detail=msg[:200])

    async def _handle_audio_delta(self, event: dict[str, Any]) -> None:
        b64 = event.get("delta", "")
        if not b64:
            return
        if self._awaiting_first_audio and self._pending_turn_start is not None:
            latency = (time.monotonic() - self._pending_turn_start) * 1000
            self.metrics.turn_latencies_ms.append(latency)
            self._awaiting_first_audio = False
            log.info("voice.first_audio", latency_ms=int(latency))

        pcm24 = base64.b64decode(b64)
        pcm16 = self.down.process(pcm24)
        await self._send_to_pi(pcm16)

    # ------------------------------------------------------------- Satellit

    async def _send_to_pi(self, pcm: bytes) -> None:
        if self._pi_ws is None or not pcm:
            return
        try:
            await self._pi_ws.send(pcm)
            self.metrics.chunks_to_pi += 1
            self.metrics.bytes_to_pi += len(pcm)
        except Exception as exc:  # noqa: BLE001
            log.error("voice.send_to_pi_failed", kind=type(exc).__name__)

    async def _flush_playback(self) -> None:
        """Barge-in: bereits gepuffertes Antwort-Audio auf dem Pi verwerfen."""
        if self._pi_ws is None:
            return
        self.metrics.barge_ins += 1
        self.down.reset()
        try:
            await self._pi_ws.send(json.dumps({"type": "flush"}))
            log.info("voice.barge_in_flush")
        except Exception:  # noqa: BLE001
            pass

    async def _handle_pi(self, ws: Any) -> None:
        """Verbindung des Satelliten annehmen und Mikrofonaudio weiterreichen."""
        if self._pi_ws is not None:
            await ws.close(code=1013, reason="bereits ein Satellit verbunden")
            return

        self._pi_ws = ws
        log.info("voice.satellite_connected", peer=str(ws.remote_address))
        print("\n>>> Satellit verbunden. SOLVIO ist bereit. Sprich jetzt ganz normal mit ihm.\n")

        ping_task = asyncio.create_task(self._ping_loop(ws))
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    self.metrics.chunks_from_pi += 1
                    self.metrics.bytes_from_pi += len(message)
                    pcm24 = self.up.process(message)
                    if self._oa_ws is not None and pcm24:
                        await self._oa_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm24).decode("ascii"),
                        }))
                else:
                    await self._handle_pi_control(json.loads(message))
        except Exception as exc:  # noqa: BLE001
            log.error("voice.satellite_error", kind=type(exc).__name__)
        finally:
            ping_task.cancel()
            self._pi_ws = None
            log.info("voice.satellite_disconnected")
            self._stop.set()

    async def _handle_pi_control(self, msg: dict[str, Any]) -> None:
        if msg.get("type") == "pong":
            sent = msg.get("t0")
            if isinstance(sent, (int, float)):
                self.metrics.rtt_samples_ms.append((time.monotonic() - sent) * 1000)
        elif msg.get("type") == "hello":
            log.info("voice.satellite_hello", info=str(msg.get("info", ""))[:120])
        elif msg.get("type") == "underrun":
            log.warning("voice.pi_underrun", count=msg.get("count"))

    async def _ping_loop(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(2.0)
                await ws.send(json.dumps({"type": "ping", "t0": time.monotonic()}))
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------------- Lauf

    async def run(self, duration: float | None = None) -> Metrics:
        url = f"{REALTIME_URL}?model={self.model}"
        log.info("voice.connect_openai", model=self.model)

        async with ws_connect(
            url,
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            open_timeout=20,
            max_size=None,
        ) as oa:
            self._oa_ws = oa

            # Auf session.created warten, dann konfigurieren.
            while True:
                event = json.loads(await asyncio.wait_for(oa.recv(), timeout=20))
                self.metrics.note_event(event.get("type", ""))
                if event.get("type") == "session.created":
                    log.info("voice.openai_ready", session_id=event.get("session", {}).get("id", ""))
                    break
                if event.get("type") == "error":
                    raise RuntimeError(str(event.get("error", {}))[:200])

            await self._configure_session(oa)

            reader = asyncio.create_task(self._openai_reader())
            async with ws_serve(self._handle_pi, self.host, self.port, max_size=None):
                print(f"\n=== SOLVIO Core bereit ===")
                print(f"Satelliten-Server laeuft auf ws://{self.host}:{self.port}")
                print("Warte auf den Raspberry Pi ...")
                try:
                    if duration:
                        await asyncio.wait_for(self._stop.wait(), timeout=duration)
                    else:
                        await self._stop.wait()
                except asyncio.TimeoutError:
                    log.info("voice.duration_reached")
                except KeyboardInterrupt:
                    pass

            reader.cancel()
            self._oa_ws = None

        return self.metrics
