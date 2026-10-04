"""Minimaler Client fuer die OpenAI Realtime API ueber WebSocket.

Bewusst ohne zusaetzliche Abstraktionsbibliothek. Die offizielle
Dokumentation beschreibt fuer Server-zu-Server-Anbindungen genau diesen Weg:
eine WebSocket-Verbindung mit dem Standard-API-Schluessel im
Authorization-Header.

Der Schluessel wird niemals protokolliert.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import connect

from solvio.logging_setup import get_logger
from solvio.redaction import redact_text

REALTIME_URL = "wss://api.openai.com/v1/realtime"

log = get_logger("solvio.realtime")


class RealtimeError(RuntimeError):
    """Fehler beim Sprechen mit der Realtime-Schnittstelle."""


@dataclass
class Timings:
    """Zeitstempel der Messpunkte T0 bis T4, jeweils als Monotonzeit."""

    t0_connect_start: float = 0.0
    t1_session_ready: float = 0.0
    t2_request_sent: float = 0.0
    t3_first_chunk: float = 0.0
    t4_complete: float = 0.0

    @staticmethod
    def _ms(start: float, end: float) -> int | None:
        if not start or not end or end < start:
            return None
        return round((end - start) * 1000)

    @property
    def connection_ms(self) -> int | None:
        return self._ms(self.t0_connect_start, self.t1_session_ready)

    @property
    def time_to_first_response_ms(self) -> int | None:
        return self._ms(self.t2_request_sent, self.t3_first_chunk)

    @property
    def total_response_ms(self) -> int | None:
        return self._ms(self.t2_request_sent, self.t4_complete)


@dataclass
class RoundtripResult:
    text: str = ""
    session_id: str = ""
    model: str = ""
    event_types: list[str] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    timings: Timings = field(default_factory=Timings)
    closed_cleanly: bool = False
    error: str | None = None


class RealtimeClient:
    """Oeffnet genau eine Realtime-Sitzung, fuehrt einen Textdurchlauf aus
    und schliesst sie wieder. Kein Dauerbetrieb, das kommt spaeter."""

    def __init__(self, api_key: str, model: str, timeout: float = 30.0) -> None:
        if not api_key.strip():
            raise RealtimeError("Kein API-Schluessel konfiguriert.")
        self._api_key = api_key.strip()
        self.model = model.strip()
        self.timeout = timeout

    @property
    def url(self) -> str:
        return f"{REALTIME_URL}?model={self.model}"

    def _headers(self) -> dict[str, str]:
        # Wird bewusst nur hier erzeugt und nirgends protokolliert.
        return {"Authorization": f"Bearer {self._api_key}"}

    async def text_roundtrip(self, prompt: str, instructions: str = "") -> RoundtripResult:
        result = RoundtripResult(model=self.model)
        t = result.timings
        t.t0_connect_start = time.monotonic()

        log.info("realtime.connect", model=self.model, url=REALTIME_URL)
        try:
            async with connect(
                self.url,
                additional_headers=self._headers(),
                open_timeout=self.timeout,
                close_timeout=5,
                max_size=None,
            ) as ws:
                # Auf session.created warten, das ist der Bereitschaftspunkt.
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.timeout)
                    event = json.loads(raw)
                    etype = event.get("type", "")
                    result.event_types.append(etype)
                    if etype == "session.created":
                        t.t1_session_ready = time.monotonic()
                        result.session_id = event.get("session", {}).get("id", "")
                        log.info(
                            "realtime.session_created",
                            session_id=result.session_id,
                            latency_ms=t.connection_ms,
                        )
                        break
                    if etype == "error":
                        raise RealtimeError(self._describe_error(event))

                # Sitzung auf reinen Textbetrieb stellen.
                session: dict[str, Any] = {
                    "type": "realtime",
                    "output_modalities": ["text"],
                }
                if instructions:
                    session["instructions"] = instructions
                await ws.send(json.dumps({"type": "session.update", "session": session}))

                # Benutzernachricht anlegen.
                await ws.send(
                    json.dumps(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": prompt}],
                            },
                        }
                    )
                )

                # Antwort anfordern.
                await ws.send(
                    json.dumps({"type": "response.create", "response": {"output_modalities": ["text"]}})
                )
                t.t2_request_sent = time.monotonic()
                log.info("realtime.request_sent", chars=len(prompt))

                # Antwortereignisse einsammeln.
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.timeout)
                    event = json.loads(raw)
                    etype = event.get("type", "")
                    result.event_types.append(etype)

                    if etype == "response.output_text.delta":
                        if not t.t3_first_chunk:
                            t.t3_first_chunk = time.monotonic()
                            log.info("realtime.first_chunk", latency_ms=t.time_to_first_response_ms)
                        result.text += event.get("delta", "")

                    elif etype == "response.done":
                        t.t4_complete = time.monotonic()
                        response = event.get("response", {})
                        result.usage = response.get("usage")
                        if not result.text:
                            result.text = self._extract_text(response)
                        log.info(
                            "realtime.response_done",
                            total_ms=t.total_response_ms,
                            chars=len(result.text),
                        )
                        break

                    elif etype == "error":
                        raise RealtimeError(self._describe_error(event))

                result.closed_cleanly = True

        except RealtimeError as exc:
            result.error = str(exc)
            log.error("realtime.error", error=redact_text(str(exc)))
        except asyncio.TimeoutError:
            result.error = f"Zeitueberschreitung nach {self.timeout} s"
            log.error("realtime.timeout", timeout_s=self.timeout)
        except Exception as exc:  # noqa: BLE001
            # Fehlertext bewusst gekuerzt, damit keine Header durchrutschen.
            result.error = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.error("realtime.exception", kind=type(exc).__name__)

        return result

    @staticmethod
    def _describe_error(event: dict[str, Any]) -> str:
        err = event.get("error", {})
        code = err.get("code") or err.get("type") or "unbekannt"
        message = err.get("message", "keine Meldung")
        return f"[{code}] {message}"

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
        """Fallback: Text aus dem abgeschlossenen Antwortobjekt lesen."""
        parts: list[str] = []
        for item in response.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in ("output_text", "text"):
                    parts.append(content.get("text", ""))
        return "".join(parts)
