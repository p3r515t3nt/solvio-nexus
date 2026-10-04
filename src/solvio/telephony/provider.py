"""Die Naht zum Telefonieanbieter — und ein Fake, der sie ernst nimmt.

Ein `TelephonyProvider` kann genau zwei Dinge: einen Anruf beginnen und den
Ausgang eines begonnenen Anrufs nachlesen. Er entscheidet nichts, bindet nichts
und weiss nichts ueber Freigaben. Diese Enge ist der Zweck: was ein Anbieter
nicht kann, kann auch kein Anbieterwechsel kaputtmachen.

Der Fake ist absichtlich kein Attrappenschatten des echten Adapters, sondern
spricht dieselbe Sprache: er liefert Anbieter-Rohdaten in genau der Form, die
auch ElevenLabs liefert, und laesst sie durch dieselbe Uebersetzung laufen. Die
Lehre dahinter ist teuer bezahlt — Agent Runtime V1 hat zwei Laeufe lang flache
Testattrappen gehabt, die den echten Umschlag verdeckten. Ein Fake, der eine
bequemere Form liefert als die Wirklichkeit, testet die Uebersetzung nicht.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from solvio.logging_setup import get_logger
from solvio.telephony import contract as C
from solvio.telephony import elevenlabs as EL
from solvio.telephony import upstream as U

log = get_logger("telephony")


class TelephonyError(RuntimeError):
    """Der Anbieter konnte den Auftrag nicht annehmen. Traegt nie ein Geheimnis."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class AmbiguousCall(TelephonyError):
    """Der Anruf KANN stattgefunden haben. Der Unterschied ist teuer.

    Getrennt von `TelephonyError`, weil beide zu voellig verschiedenen
    Ledgereintraegen fuehren muessen. Eine belegte Ablehnung des Anbieters
    (4xx, `success: false`) heisst: es hat nicht geklingelt, FAILED ist ehrlich.
    Alles andere — eine Zeitueberschreitung, ein angenommener Auftrag ohne
    Faden — heisst: es kann geklingelt haben, und dann ist FAILED eine
    Behauptung, die niemand geprueft hat. Sie waere ausserdem gefaehrlich: ein
    terminaler Zustand ohne Faden laesst einen zweiten Anruf zu.
    """


@dataclass(frozen=True)
class StartedCall:
    """Was nach dem Anrufauftrag feststeht — und was ausdruecklich nicht.

    `accepted` heisst: der Anbieter hat den AUFTRAG angenommen. Es heisst nicht,
    dass es klingelt, und schon gar nicht, dass jemand abhebt. Die
    `conversation_id` ist das einzige, was zaehlt: sie ueberlebt einen
    Core-Neustart und ist der Faden zum Ausgang.
    """

    accepted: bool
    conversation_id: str
    provider_call_id: str = ""
    message: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CallOutcome:
    """Der nachgelesene Ausgang, schon uebersetzt — aber noch ohne Deutung."""

    call_state: str
    finished: bool
    accepted_at: float | None
    started_at: float | None
    duration_secs: int | None
    transcript: tuple[Mapping[str, Any], ...]
    transcript_summary: str
    call_successful: str
    cost_credits: int | None
    cost_fiat: float | None
    termination_reason: str
    raw: Mapping[str, Any] = field(default_factory=dict)


class TelephonyProvider(Protocol):
    """Zwei Methoden. Mehr darf ein Anbieter in SOLVIO nicht."""

    name: str

    async def start_call(self, *, to_number: str, variables: Mapping[str, str],
                         max_duration_secs: int) -> StartedCall: ...

    async def fetch_outcome(self, conversation_id: str) -> CallOutcome: ...


def _outcome_from_conversation(body: Mapping[str, Any]) -> CallOutcome:
    """Uebersetzt eine Anbieter-Conversation in einen Ausgang. Anbieterneutral testbar.

    Diese Funktion ist bewusst frei von Netzverkehr, damit der Fake und der
    echte Adapter denselben Weg nehmen. Sie rät nichts: fehlt ein Feld, wird das
    Ergebnis unbestimmter, nicht optimistischer.
    """
    body = body or {}
    status = str(body.get("status") or "")
    metadata = body.get("metadata") or {}
    analysis = body.get("analysis") or {}

    accepted_raw = metadata.get("accepted_time_unix_secs")
    accepted_at = float(accepted_raw) if accepted_raw not in (None, "") else None

    error = metadata.get("error") or {}
    termination = str(metadata.get("termination_reason") or "")
    # Der Fehlertyp kann an zwei Stellen stehen; beide sind dokumentiert.
    error_type = str(error.get("reason") or error.get("type") or "")
    if not error_type and termination:
        error_type = termination

    state = C.call_state_from_provider(
        status, accepted=accepted_at is not None, error_type=error_type)

    started_raw = metadata.get("start_time_unix_secs")
    duration_raw = metadata.get("call_duration_secs")
    transcript = tuple(body.get("transcript") or ())

    return CallOutcome(
        call_state=state,
        finished=status in C.PROVIDER_STATUS_TERMINAL,
        accepted_at=accepted_at,
        started_at=float(started_raw) if started_raw not in (None, "") else None,
        duration_secs=int(duration_raw) if isinstance(duration_raw, int) else None,
        transcript=transcript,
        transcript_summary=str(analysis.get("transcript_summary") or ""),
        call_successful=str(analysis.get("call_successful") or "unknown"),
        cost_credits=metadata.get("cost") if isinstance(metadata.get("cost"), int) else None,
        cost_fiat=(float(metadata["cost_fiat"])
                   if isinstance(metadata.get("cost_fiat"), (int, float)) else None),
        termination_reason=termination,
        raw=body,
    )


class ElevenLabsTelephonyProvider:
    """Der V1-Adapter. Kennt genau zwei Anbieterpfade und keinen dritten."""

    name = "elevenlabs"

    def __init__(self, agent_id: str, phone_number_id: str,
                 upstream: U.ElevenLabsUpstream | None = None) -> None:
        self.agent_id = agent_id
        self.phone_number_id = phone_number_id
        self._up = upstream or U.ElevenLabsUpstream()

    async def _call_upstream(self, method: str, path: str, *, scope: str,
                             body: dict[str, Any] | None = None) -> Any:
        """Die Uebersetzungsstelle. Hier endet die Anbietersprache.

        `TelephonyUpstreamError` ist die Sprache des Transports — Netzfehler,
        Tresorabsagen, Pfadverweigerungen. Die Faehigkeit kennt nur
        `TelephonyError`. Solange diese Umwandlung fehlte, entkam der Behandlung
        genau das, worauf es ankommt: eine Zeitueberschreitung beim Waehlen oder
        eine Tresorabsage nach einem Neustart flog ungefangen durch, und die
        Ledgerzeile blieb auf PREPARED stehen — obwohl der Anruf stattgefunden
        haben konnte.
        """
        try:
            return await self._up.call(method, path, scope=scope, body=body)
        except U.TelephonyPathRefused as exc:
            # Der Aufruf hat den Rechner nie verlassen. Das ist Wissen.
            raise TelephonyError(exc.reason, exc.detail) from None
        except U.TelephonyUpstreamError as exc:
            # Alles andere ist MEHRDEUTIG, und beim Waehlen ist das der
            # teuerste Unterschied: eine Zeitueberschreitung nach 20 Sekunden
            # sagt nichts darueber, ob der Anbieter den Auftrag schon
            # angenommen hat. Wer hier "gescheitert" aufschreibt, behauptet,
            # dass nichts passiert ist — und laesst einen zweiten Anruf zu.
            raise AmbiguousCall(exc.reason, exc.detail) from None

    async def start_call(self, *, to_number: str, variables: Mapping[str, str],
                         max_duration_secs: int) -> StartedCall:
        body: dict[str, Any] = {
            "agent_id": self.agent_id,
            "agent_phone_number_id": self.phone_number_id,
            "to_number": to_number,
            # Der gebundene Auftrag reist als Laufzeitvariablen. Der Systemprompt
            # des Agenten ist beim Anbieter fest und per Ueberschreibung gesperrt
            # — deshalb kann dieser Aufruf den Auftrag ergaenzen, aber nicht die
            # Regeln aendern, unter denen der Agent spricht.
            "conversation_initiation_client_data": {
                "dynamic_variables": dict(variables),
                # Die freigegebene Hoechstdauer reist MIT. Vorher wurde sie
                # geprueft, im Freigabetext angezeigt, in den Digest gebunden,
                # im Ledger festgehalten — und dann nie gesendet. Eine Grenze,
                # die den Anbieter nie erreicht, ist keine Grenze; auf dem
                # Display stand eine Zahl, an die sich niemand halten musste.
                #
                # Damit dieser Wert wirkt, muss am Agenten GENAU dieser eine
                # Ueberschreibungsschalter offen sein. Alle anderen bleiben zu;
                # der Preflight prueft beides.
                "conversation_config_override": {
                    "conversation": {
                        "max_duration_seconds": int(max_duration_secs),
                    },
                },
            },
            # §11: keine Aufzeichnung. Ausdruecklich gesetzt und nicht der
            # Anbietervorgabe ueberlassen.
            "call_recording_enabled": False,
        }
        response = await self._call_upstream(
            "POST", "/v1/convai/twilio/outbound-call",
            scope=EL.CAPABILITY_CALL, body=body)

        payload = response.body if isinstance(response.body, dict) else {}
        if response.status >= 400 or not payload.get("success"):
            raise TelephonyError(
                "provider_refused",
                f"http {response.status}: {str(payload.get('message') or '')[:200]}")

        conversation_id = str(payload.get("conversation_id") or "")
        if not conversation_id:
            # Ohne diesen Faden ist der Ausgang unauffindbar. Das ist ein
            # MEHRDEUTIGER Ausgang, kein Fehlschlag: der Anbieter hat mit 2xx
            # und success=true geantwortet, das Telefon kann also klingeln.
            # Der Aufrufer muss das unterscheiden koennen — sonst schriebe er
            # „nichts passiert" und liesse einen zweiten Anruf zu.
            raise AmbiguousCall("no_conversation_id",
                                "provider accepted the call without an id")
        return StartedCall(
            accepted=True, conversation_id=conversation_id,
            provider_call_id=str(payload.get("callSid") or ""),
            message=str(payload.get("message") or ""), raw=payload)

    async def fetch_outcome(self, conversation_id: str) -> CallOutcome:
        if not conversation_id:
            raise TelephonyError("no_conversation_id")
        response = await self._call_upstream(
            "GET", f"/v1/convai/conversations/{conversation_id}",
            scope=EL.CAPABILITY_RESULT)
        if response.status >= 400:
            raise TelephonyError("outcome_unavailable", f"http {response.status}")
        body = response.body if isinstance(response.body, dict) else {}
        return _outcome_from_conversation(body)


class FakeTelephonyProvider:
    """Ein Anbieter fuer Tests, der dieselbe Rohform liefert wie der echte.

    Er kennt eine Folge von Conversation-Zustaenden und gibt sie nacheinander
    heraus — so laesst sich `processing` -> `done` ohne Netz durchspielen. Er
    zaehlt ausserdem seine Anrufe: der Test "genau ein Anruf pro Freigabe"
    braucht einen Zeugen, keine Zusicherung.
    """

    name = "fake"

    def __init__(self, conversations: list[Mapping[str, Any]] | None = None,
                 *, accept: bool = True, conversation_id: str = "conv-fake-1",
                 fail_start: str = "") -> None:
        self.conversations = list(conversations or [])
        self.accept = accept
        self.conversation_id = conversation_id
        self.fail_start = fail_start
        self.calls: list[dict[str, Any]] = []
        self.fetches: list[str] = []
        self._cursor = 0

    async def start_call(self, *, to_number: str, variables: Mapping[str, str],
                         max_duration_secs: int) -> StartedCall:
        self.calls.append({"to_number": to_number, "variables": dict(variables),
                           "max_duration_secs": max_duration_secs,
                           "at": time.time()})
        if self.fail_start:
            raise TelephonyError(self.fail_start)
        if not self.accept:
            raise TelephonyError("provider_refused", "fake refuses")
        return StartedCall(accepted=True, conversation_id=self.conversation_id,
                           provider_call_id="CA-fake", message="queued")

    async def fetch_outcome(self, conversation_id: str) -> CallOutcome:
        self.fetches.append(conversation_id)
        if not self.conversations:
            raise TelephonyError("outcome_unavailable", "fake has no conversation")
        index = min(self._cursor, len(self.conversations) - 1)
        self._cursor += 1
        return _outcome_from_conversation(self.conversations[index])
