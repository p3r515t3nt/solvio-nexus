"""Der EINE Weg des Routers zum Modell — ueber den Provider Broker.

Nicht, weil der Core keinen Schluessel haette (er hat ihn; er betreibt den
Broker), sondern weil dort die Frage „was hat ein Tag gekostet, und wer war es"
schon beantwortet wird: Lease je Aufruf, Kappen je Auftraggeber, eine Buchzeile
je Anfrage. Der Router traegt einen Broker-Token, keinen Anbieterschluessel —
und ohne offenes Lease oeffnet der nichts.

**Der Token wird je Aufruf frisch gepraegt.** Live gefunden beim Planer: der
Broker praegt neu, sobald das letzte Lease eines Auftraggebers schliesst. Ein
gemerkter Token ist ab dem zweiten Aufruf `401` — beim Planer ging der erste
Plan durch und die Nachplanung lief in die Ablehnung. Das ist kein Umweg um die
Rotation, sondern ihre bestimmungsgemaesse Benutzung.

**Die Stufe waehlt der Aufrufer, nie das Modell.** `assess(tier=...)` bekommt
die Stufe von der Politik. Der Auftraggeber und damit der Zugang zum grossen
Modell haengt an der Stufe — ein Modell, das `gpt-5.4` in seine Antwort
schriebe, bekaeme davon keinen Zugang, sondern eine verworfene Antwort.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from solvio.cognition import models as M
from solvio.cognition import prompt as P
from solvio.cognition.types import ModelTier
from solvio.logging_setup import get_logger

log = get_logger("cognition")


@dataclass
class ModelCall:
    """Ein Aufruf und sein Ausgang. Kein Text ausser der Antwort selbst."""

    ok: bool = False
    text: str = ""
    reason: str = ""
    tokens: int = 0
    tier: ModelTier = ModelTier.MINI
    elapsed: float = 0.0


async def broker_transport(payload: dict, *, token: str, port: int = 0,
                           timeout: float = M.ASSESS_TIMEOUT) -> dict:
    """POST an den Broker. Ein Anbieterfehler ist keine Schema-Frage."""
    import aiohttp

    from solvio.provider_broker.service import configured_port

    chosen = int(port) or configured_port()
    url = f"http://127.0.0.1:{chosen}/v1/responses"
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    limit = aiohttp.ClientTimeout(total=float(timeout))
    try:
        async with aiohttp.ClientSession(timeout=limit) as session:
            async with session.post(url, json=payload, headers=headers) as response:
                body = await response.text()
                if response.status != 200:
                    # Der Grund des Brokers steht im Rumpf und ist eine
                    # geschlossene Menge — er wird GELESEN, nicht geraten. Aus
                    # ihm entscheidet sich spaeter, ob der Mensch „Kontingent"
                    # oder „nicht erreichbar" hoert.
                    reason = _denied_reason(body) or f"broker_{response.status}"
                    log.warning("cognition.rejected", status=response.status,
                                reason=reason)
                    return {"ok": False, "reason": reason}
                try:
                    data = json.loads(body)
                except ValueError:
                    return {"ok": False, "reason": "broker_unreadable"}
    except aiohttp.ClientError as exc:
        return {"ok": False, "reason": f"broker_unreachable:{type(exc).__name__}"}
    except TimeoutError:
        return {"ok": False, "reason": "broker_timeout"}
    return {"ok": True, "text": response_text(data), "tokens": response_tokens(data)}


def _denied_reason(body: str) -> str:
    """`{"error": {"type": "solvio_broker", "code": "<grund>"}}` — oder nichts."""
    try:
        parsed = json.loads(body or "")
    except ValueError:
        return ""
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(error, dict):
        return ""
    return str(error.get("code", "") or "")


def response_text(data: Any) -> str:
    """Die Antwort, egal in welcher der drei Fassungen sie kommt."""
    if not isinstance(data, dict):
        return ""
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    for item in data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []) or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
    for choice in data.get("choices", []) or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def response_tokens(data: Any) -> int:
    """Was der Anbieter selbst gemeldet hat. Null heisst: nichts gemeldet."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return 0
    for key in ("total_tokens", "total_token_count"):
        value = usage.get(key)
        if isinstance(value, int):
            return value
    put = usage.get("input_tokens")
    out = usage.get("output_tokens")
    if isinstance(put, int) and isinstance(out, int):
        return put + out
    return 0


class Assessor:
    """Die Bruecke zum Modell. Haelt keinen Zustand ausser dem Transport."""

    def __init__(self, *, broker: Any = None, transport: Any = None,
                 port: int = 0) -> None:
        self.broker = broker
        # Ohne ausdruecklichen Transport der Weg ueber den Broker. Ein Test
        # reicht seinen eigenen herein und kommt damit ohne Netz aus.
        self._transport = transport if transport is not None else broker_transport
        self._port = port

    def _principal(self, tier: ModelTier) -> str:
        return M.PRINCIPAL_FOR_TIER[tier]

    def ensure_principal(self, tier: ModelTier) -> str:
        """Praegt einen FRISCHEN Token — je Aufruf, nicht je Lebenszeit."""
        if self.broker is None:
            return ""
        return self.broker.register_principal(self._principal(tier))

    async def call(self, payload: dict, *, tier: ModelTier, ref: str) -> ModelCall:
        """Genau ein gemaklerter Aufruf, mit eigenem Lease im `finally`."""
        started = time.monotonic()
        if self.broker is None and self._transport is broker_transport:
            return ModelCall(False, reason="broker_absent", tier=tier)

        name = self._principal(tier)
        token = self.ensure_principal(tier)
        lease_id = ""
        if self.broker is not None:
            try:
                lease_id = self.broker.open_lease(
                    name, str(ref or "")[:80],
                    deadline=time.time() + M.LEASE_SECONDS)
            except Exception as exc:  # noqa: BLE001 - eine Kappe ist kein Absturz
                log.info("cognition.lease_refused", principal=name,
                         kind=type(exc).__name__)
                return ModelCall(False, reason=getattr(exc, "reason", "lease_refused"),
                                 tier=tier)
        try:
            result = await self._transport(payload, token=token, port=self._port)
            return ModelCall(ok=bool(result.get("ok")),
                             text=str(result.get("text", "") or ""),
                             reason=str(result.get("reason", "") or ""),
                             tokens=int(result.get("tokens", 0) or 0),
                             tier=tier,
                             elapsed=time.monotonic() - started)
        except Exception as exc:  # noqa: BLE001 - ein Fehlschlag ist keine Erlaubnis
            log.warning("cognition.call_failed", kind=type(exc).__name__)
            return ModelCall(False, reason="assessor_failed", tier=tier,
                             elapsed=time.monotonic() - started)
        finally:
            # Steht in einem `finally` und darf deshalb nie werfen — dieselbe
            # Regel wie beim Broker selbst.
            if self.broker is not None and lease_id:
                try:
                    self.broker.close_lease(lease_id)
                except Exception as exc:  # noqa: BLE001 - nie den Core stoeren
                    log.info("cognition.lease_close_failed",
                             kind=type(exc).__name__)

    async def assess(self, *, tier: ModelTier, user_text: str, register: str,
                     recent_context: str = "", clarified_text: str = "",
                     asked_back: bool = False,
                     related_context: str | None = None,
                     task_profiles: bool = False,
                     repair_hint: str = "", ref: str = "") -> ModelCall:
        """Eine Einschaetzung auf der Stufe, die die Politik gewaehlt hat.

        `asked_back` sagt, WIE stark der vorige Turn bindet: hat SOLVIO
        ausdruecklich nachgefragt, oder ist es blosse Fortsetzung? Der Wert
        aendert nur die Rahmung, nie den Umfang — beides steht in der Anfrage.
        """
        payload = P.build_request(model=M.MODEL_FOR_TIER[tier],
                                  user_text=user_text, register=register,
                                  recent_context=recent_context,
                                  clarified_text=clarified_text,
                                  asked_back=asked_back,
                                  related_context=related_context,
                                  task_profiles=task_profiles)
        if repair_hint:
            payload["input"].append(P.repair_turn(repair_hint))
        return await self.call(payload, tier=tier, ref=ref or "assess")

    async def reason(self, *, tier: ModelTier, user_text: str,
                     recent_context: str = "", ref: str = "") -> ModelCall:
        """Der Weg `nachdenken`: eine Antwort, die gesprochen wird."""
        payload = P.build_reason_request(model=M.MODEL_FOR_TIER[tier],
                                         user_text=user_text,
                                         recent_context=recent_context)
        return await self.call(payload, tier=tier, ref=ref or "reason")
