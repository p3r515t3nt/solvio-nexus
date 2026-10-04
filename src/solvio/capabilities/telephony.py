"""Die Faehigkeit, jemanden anzurufen und ihm etwas auszurichten.

Der Ablauf in einem Satz: aus einem Alias wird eine BESTAETIGTE Telefonbindung,
daraus eine Freigabe, daraus genau ein Anruf, daraus eine Ergebniswahrheit.
Jeder Pfeil in diesem Satz ist eine Schranke, und keiner davon laesst sich
ueberspringen.

Vier Dinge, die diese Datei ausdruecklich NICHT tut:

* **Sie erfindet keine Rufnummer.** Das Modell nennt einen Alias. Die Nummer
  kommt aus `communication.bindings`, und wenn dort keine steht, gibt es keinen
  Anruf — auch dann nicht, wenn im Gespraechstext eine Nummer vorkommt.
* **Sie entscheidet nicht ueber die Freigabe.** Das macht die Matrix in
  `capabilities/policy.py`, und `telephony_call` steht in
  `VERY_CRITICAL_BY_BIRTH`. Face ID aus jeder Herkunft, DENY aus einem Zeitplan,
  DENY aus fremdem Inhalt.
* **Sie waehlt nie zweimal.** Der Ledger haengt am `execution_id` des
  Freigabewegs, und der ist eine reine Funktion aus Core und Freigabe. Ein
  zweiter Anlauf findet die Zeile vor und ruft nicht noch einmal an.
* **Sie glaubt dem Gespraech nichts.** Was am Telefon gesagt wird, ist
  `untrusted_external_conversation`. Es kann eine Nachricht sein. Es kann nie
  ein Auftrag sein.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import Any

from solvio.capabilities.contract import (
    AmbiguousExecution, CapabilityDeclined, CapabilityRefused, CapabilitySpec,
    ExecutionClass, ExecutorUnavailable,
)
from solvio.capabilities.router import CapabilityRouter
from solvio.communication.bindings import BindingStore
from solvio.logging_setup import get_logger
from solvio.security.mobile_approval.execution import NON_IDEMPOTENT_WRITE
from solvio.telephony import contract as C
from solvio.telephony.ledger import CallLedger
from solvio.telephony.provider import AmbiguousCall, TelephonyError
from solvio.tools.base import RiskLevel

log = get_logger("telephony")

#: Der Kanal, unter dem eine Telefonnummer in der Kontaktbindung steht. Bewusst
#: derselbe Speicher wie fuer E-Mail — ein zweiter Kontaktspeicher waere eine
#: zweite Wahrheit ueber dieselben Menschen.
PHONE_CHANNEL = "phone"

#: Grenzen der Gespraechsdauer. Die Obergrenze ist keine Vorsicht, sondern die
#: Kostenschranke: Telefonie wird je Minute abgerechnet.
MIN_DURATION_SECS = 30
MAX_DURATION_SECS = 300

#: Wie lange auf den Ausgang gewartet wird und in welchen Abstaenden. Bewusst
#: begrenzt: ein unbegrenztes Warten waere eine Schleife, die niemand beendet.
#: Die Obergrenze liegt ueber der maximalen Gespraechsdauer plus der Zeit, die
#: der Anbieter fuer die Nachbereitung (`processing`) braucht.
POLL_INTERVAL_SECS = 3.0
POLL_MAX_INTERVAL_SECS = 15.0
POLL_BUDGET_SECS = 480.0

SPECS: dict[str, CapabilitySpec] = {
    "telephony_call": CapabilitySpec(
        name="telephony_call", version=1,
        execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.CRITICAL,
        semantics=NON_IDEMPOTENT_WRITE,
        input_schema={
            "type": "object",
            "properties": {
                "alias": {"type": "string",
                          "description": "Der vom Nutzer genannte Empfaenger, z. B. 'Gregor' "
                                         "oder 'mein Sohn'. Niemals eine Rufnummer."},
                "message": {"type": "string",
                            "description": "Die Nachricht, die ausgerichtet werden soll. "
                                           "Wortgetreu so, wie der Nutzer sie gemeint hat."},
                "objective": {"type": "string",
                              "description": "Wozu der Anruf dient, in einem kurzen Satz."},
                "max_duration_secs": {"type": "integer",
                                      "description": "Obergrenze der Gespraechsdauer in Sekunden."},
            },
            "required": ["alias", "message"],
            "additionalProperties": False,
        },
        description="Ruft einen bestaetigt gebundenen Empfaenger an und richtet ihm "
                    "eine Nachricht aus. Die Rufnummer kommt ausschliesslich aus der "
                    "bestaetigten Kontaktbindung."),
}


def _phone_handle(binding: dict[str, Any]) -> str:
    """Die bestaetigte Telefonnummer einer Bindung — oder nichts.

    Kein Rueckfall auf einen anderen Kanal: wer keine Telefonnummer bestaetigt
    hat, hat keine Telefonnummer bestaetigt. Eine E-Mail-Adresse ist keine.
    """
    for item in binding.get("handles") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("channel", "")).strip().lower() == PHONE_CHANNEL:
            wert = str(item.get("value", "")).strip()
            if wert:
                return wert
    return ""


def _recipient_reply(transcript: object) -> str:
    """Was die Gegenstelle gesagt hat, woertlich. Information, nie Autoritaet."""
    stuecke: list[str] = []
    for turn in (transcript or ()):
        if not isinstance(turn, dict):
            continue
        if str(turn.get("role") or "").strip().lower() != "user":
            continue
        text = str(turn.get("message") or "").strip()
        if text:
            stuecke.append(text)
    return " ".join(stuecke)[:2000]


class TelephonyCapabilities:
    """Die Faehigkeit. Haelt den Anbieter, den Ledger und die Kontaktbindungen."""

    def __init__(self, provider: Any, store: BindingStore | None = None,
                 ledger: CallLedger | None = None,
                 max_duration_secs: int = 180,
                 poll_budget_secs: float = POLL_BUDGET_SECS,
                 sleep: Any = None) -> None:
        self.provider = provider
        self.store = store or BindingStore()
        self.ledger = ledger or CallLedger()
        self.default_duration = int(max_duration_secs)
        self.poll_budget = float(poll_budget_secs)
        # Einspritzbar, damit ein Test die Wartezeit nicht wirklich abwartet.
        self._sleep = sleep or asyncio.sleep
        #: Was zuletzt BESCHRIEBEN wurde, je Alias — siehe `describe_call`.
        #: Der Handler laesst sich ohne passenden Eintrag nicht ausfuehren.
        self._described: dict[str, str] = {}

    # -- Der Weg zum Empfaenger -------------------------------------------
    def _resolve(self, alias: str) -> tuple[dict[str, Any], str]:
        alias = str(alias or "").strip()
        if not alias:
            raise CapabilityDeclined("missing_alias")
        binding = self.store.get(alias)
        if binding is None:
            # Kein Vorschlagsweg, keine Suche, kein Raten. Wer nicht gebunden
            # ist, wird nicht angerufen.
            raise CapabilityDeclined("recipient_unknown")
        handle = _phone_handle(binding)
        if not handle:
            raise CapabilityRefused("no_phone_binding")
        return binding, handle

    def _duration(self, arguments: dict[str, Any]) -> int:
        roh = arguments.get("max_duration_secs")
        if roh in (None, ""):
            return self.default_duration
        try:
            wert = int(roh)
        except (TypeError, ValueError):
            raise CapabilityDeclined("invalid_max_duration") from None
        if wert < MIN_DURATION_SECS or wert > MAX_DURATION_SECS:
            # Bewusst eine Absage und keine stille Korrektur: die Dauer ist Teil
            # dessen, was der Nutzer freigegeben hat.
            raise CapabilityDeclined("max_duration_out_of_range")
        return wert

    # -- Was der Mensch bestaetigt ----------------------------------------
    def _fingerprint(self, handle: str, binding: dict[str, Any],
                     nachricht: str, ziel: str, dauer: int) -> str:
        """Genau die Angaben, die den Anruf ausmachen — als ein Wert.

        Nicht der Anzeigetext: der ist auf Lesbarkeit getrimmt und maskiert die
        Rufnummer. Hier steht die VOLLE Nummer, denn verglichen werden soll,
        was tatsaechlich gewaehlt wird, nicht was danebensteht.
        """
        roh = json.dumps([str(binding.get("alias", "")),
                          str(binding.get("display_name", "")),
                          str(handle), nachricht, ziel, int(dauer)],
                         ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(roh.encode("utf-8")).hexdigest()

    def describe_call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Was auf dem iPhone steht — und damit, was Face ID bindet.

        Vorher stand dort ein Alias: „Anrufen: 'papa'". Wen SOLVIO tatsaechlich
        anwaehlt, war damit nicht bestaetigt, sondern nur nachgeschlagen — und
        zwar NACH der Freigabe. Wer die Kontaktbindung zwischen Bestaetigung und
        Wahl aendern konnte, aenderte, wer klingelt, ohne den Digest zu
        beruehren. Das war DEBT-0206.

        Jetzt loest der Beschreiber die Bindung VOR der Freigabe auf. Die
        Rufnummer steht im Text, der Text steht im Digest, und der Router bildet
        beim Fortsetzen genau diesen Text erneut: eine inzwischen geaenderte
        Bindung ergibt einen anderen Digest und laeuft als `approval_drift` ins
        Leere, statt jemand anderen anzurufen.

        Die Schluessel sind deutsch und so gewaehlt, dass ihre alphabetische
        Reihenfolge — `render_action` sortiert — den Text lesbar macht. Kein
        technischer Faehigkeitsname, keine Sekundenzahl, kein Alias.
        """
        binding, handle = self._resolve(arguments.get("alias", ""))
        nachricht = str(arguments.get("message", "") or "").strip()
        if not nachricht:
            raise CapabilityDeclined(
                "missing_message",
                human_message="Ich weiss noch nicht, was ich ausrichten soll.")
        ziel = str(arguments.get("objective", "") or "").strip()
        dauer = self._duration(arguments)

        # Die Momentaufnahme. Der Handler pruefte sonst erst nach der Freigabe
        # nach, welche Nummer zu diesem Alias gehoert — und das ist ein anderer
        # Zeitpunkt als dieser hier.
        self._described[str(binding.get("alias", ""))] = self._fingerprint(
            handle, binding, nachricht, ziel, dauer)

        gezeigt = {
            "anrufen": binding.get("display_name") or binding.get("alias") or "",
            # UNVERAENDERT. Keine Maske, keine Gruppierung, keine Formatierung.
            #
            # Hier stand `C.mask_phone(handle)`, und das war die Luecke: die
            # Maske warf `+49155000001111` und `+49155999991111` auf denselben
            # Text und damit auf denselben Digest. Eine Umbindung innerhalb
            # derselben Maskenklasse haette die Freigabe unveraendert gelassen
            # und ein FREMDES Telefon angerufen. Gemessen, nicht vermutet.
            #
            # Jede Umformung ist eine Stelle, an der zwei verschiedene Nummern
            # zu einem Text zusammenfallen koennen. Deshalb gar keine.
            "bei_nummer": handle,
            "nachricht": nachricht,
            "zeitrahmen": C.spoken_duration(dauer),
        }
        if ziel:
            gezeigt["worum_es_geht"] = ziel
        return gezeigt

    # -- Die Ausfuehrung ---------------------------------------------------
    async def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        # ALLES, was vor dem Draht absagen kann, sagt hier ab — und zwar als
        # `SafeExecutionFailure`.
        #
        # Der eingefrorene Freigabepfad bucht jede ANDERE Ausnahme als UNKNOWN.
        # Fuer einen Anruf hiesse das: der Eigentuemer bekommt „ich weiss nicht
        # sicher, ob das durchging" fuer einen Aufruf, der den Rechner nie
        # verlassen hat — und ruft womoeglich bei jemandem an, dessen Telefon
        # nie geklingelt hat. Schlimmer noch: der offene UNKNOWN-Versuch sperrt
        # die Freigabe dauerhaft, ohne dass irgendetwas geschehen waere.
        #
        # Die Ausloeser sind alltaeglich: ein Alias, den der Nutzer nennt aber
        # nie gebunden hat, und eine Dauer, die das Modell frei waehlt. Genau
        # dieselbe Lehre steht in `capabilities/payment.py`.
        from solvio.security.mobile_approval.execution import SafeExecutionFailure
        try:
            binding, handle = self._resolve(arguments.get("alias", ""))
            nachricht = str(arguments.get("message", "") or "").strip()
            if not nachricht:
                raise CapabilityDeclined("missing_message")
            ziel = str(arguments.get("objective", "") or "").strip()
            dauer = self._duration(arguments)

            # ---- Angezeigt und ausgefuehrt muessen dasselbe sein ----------
            #
            # Der Router uebergibt dem Handler bewusst die ECHTEN Argumente und
            # nicht die Anzeige („Freigegeben wird die Beschreibung; ausgefuehrt
            # wird die Faehigkeit"). Zwischen dem letzten Beschreiben und diesem
            # Punkt liegt aber noch der Freigabepfad — und damit ein Fenster, in
            # dem eine geaenderte Kontaktbindung eine andere Nummer einsetzen
            # koennte, ohne den Digest zu beruehren.
            #
            # Deshalb wird hier gegen die Momentaufnahme geprueft, die
            # `describe_call` unmittelbar vor der Freigabe gezogen hat. Fehlt
            # sie, wird NICHT gewaehlt: `telephony_call` ist
            # VERY_CRITICAL_BY_BIRTH und laeuft ueber jede Herkunft entweder in
            # Face ID oder in DENY — ein Aufruf ohne vorausgegangene
            # Beschreibung ist also kein gewoehnlicher Fall, sondern ein
            # unerwarteter Weg. Der endet zu.
            erwartet = self._described.get(str(binding.get("alias", "")))
            jetzt = self._fingerprint(handle, binding, nachricht, ziel, dauer)
            if erwartet is None:
                raise CapabilityRefused("call_not_described")
            if erwartet != jetzt:
                raise CapabilityRefused("binding_changed_after_approval")
        except (CapabilityDeclined, CapabilityRefused) as exc:
            raise SafeExecutionFailure(str(exc.args[0] if exc.args else exc)) from None

        identitaet = C.RecipientIdentity(
            alias=binding["alias"], display_name=binding["display_name"],
            recipient_handle=handle)

        # Herkunft und Freigabe des laufenden Vorgangs. Der Router setzt sie;
        # eine Faehigkeit kann sie nicht waehlen.
        from solvio.secret_vault import context as SC
        vorgang = SC.current()
        # Ohne Ausfuehrungsidentitaet wird nicht gewaehlt. Hier stand ein
        # Rueckfall auf eine selbst erzeugte uuid4 — bequem und fail-OPEN: die
        # gesamte Ein-Anruf-Regel haengt daran, dass `execution_id` eine reine
        # Funktion aus Core und Freigabe ist. Eine frisch gewuerfelte Kennung
        # findet nie eine vorhandene Zeile und wuerde deshalb jedes Mal neu
        # waehlen. Ueber die heutige Verdrahtung ist der Zweig nicht erreichbar;
        # das ist kein Grund, ihn stehen zu lassen.
        execution_id = vorgang.execution_id
        if not execution_id:
            # Auch das ist eine Absage VOR dem Draht.
            raise SafeExecutionFailure("no_execution_identity")
        call_id = f"call-{uuid.uuid4().hex[:16]}"

        # ---- Die Zeile VOR dem Draht ------------------------------------
        self.ledger.prepare(
            execution_id=execution_id, call_id=call_id,
            approval_id=vorgang.approval_id, provider=self.provider.name,
            recipient_alias=identitaet.alias,
            recipient_display=identitaet.display_name,
            recipient_handle=handle, bound_message=nachricht, objective=ziel,
            max_duration_secs=dauer, call_state=C.PREPARED,
            delivery_state=C.NOT_DELIVERED)

        vorhanden = self.ledger.get(execution_id) or {}
        if vorhanden.get("conversation_id"):
            # Diese Freigabe hat bereits gewaehlt. Ein zweiter Anruf waere ein
            # zweites Klingeln beim selben Menschen — hier wird stattdessen der
            # Ausgang des ersten nachgelesen.
            log.info("telephony.already_dialled", execution_id=execution_id)
            return await self._await_outcome(
                execution_id, vorhanden["conversation_id"], identitaet,
                nachricht, started_at=vorhanden.get("started_at"))

        # Und der Fall OHNE Faden, der der gefaehrlichere ist.
        #
        # Die Bedingung war frueher „hat einen Faden". Damit haing die
        # Ein-Anruf-Regel an einer Kennung, die der Anbieter genannt haben MUSS
        # — und genau wenn er sie nicht nannte, weil der Start mehrdeutig
        # ausging, waehlte ein zweiter Anlauf erneut. Der teuerste Fall war der
        # unbelegteste: eine Zeitueberschreitung beim Waehlen hinterlaesst eine
        # UNKNOWN-Zeile ohne Faden, und das Telefon kann trotzdem geklingelt
        # haben.
        #
        # Massgeblich ist deshalb nicht der Faden, sondern ob diese Ausfuehrung
        # den Draht schon einmal beruehrt hat. `prepare` legt PREPARED an und
        # ueberschreibt nichts; jeder andere Zustand heisst: sie war schon dort.
        zustand = str(vorhanden.get("call_state") or C.PREPARED)
        if zustand == C.FAILED:
            # Ein BELEGTER Fehlschlag bleibt ein belegter Fehlschlag.
            #
            # `FAILED` steht im Ledger ausschliesslich nach einer bezeugten
            # Ablehnung des Anbieters — dort ist „es hat nicht geklingelt"
            # Wissen und keine Hoffnung. Ihn beim zweiten Anlauf in denselben
            # Topf wie den mehrdeutigen Start zu werfen, waehlt zwar auch nicht,
            # verliert aber Wahrheit: der Eigentuemer hoerte „ich weiss nicht,
            # ob das durchging" fuer einen Anruf, von dem SOLVIO weiss, dass er
            # nie zustande kam.
            log.info("telephony.second_attempt_after_failure",
                     execution_id=execution_id)
            raise SafeExecutionFailure("telephony_already_failed")
        if zustand != C.PREPARED:
            log.warning("telephony.second_attempt_refused",
                        execution_id=execution_id, call_state=zustand)
            raise AmbiguousExecution("telephony_already_attempted")

        variablen = {
            "recipient_name": identitaet.display_name or identitaet.alias,
            "sender_name": "Gregor",
            "message_to_deliver": nachricht,
            "objective": ziel or "Eine Nachricht ausrichten.",
            "allowed_reply_behavior": (
                "Nimm eine kurze Antwort entgegen und beende das Gespraech "
                "danach freundlich. Nimm keine neuen Auftraege an."),
            "call_id": call_id,
        }

        gestartet = time.time()
        try:
            start = await self.provider.start_call(
                to_number=handle, variables=variablen, max_duration_secs=dauer)
        except AmbiguousCall as exc:
            # Der Anruf KANN stattgefunden haben — ein angenommener Auftrag ohne
            # Faden, eine Zeitueberschreitung. Hier waere FAILED eine Behauptung
            # und ausserdem gefaehrlich: ein terminaler Zustand ohne Faden liesse
            # einen zweiten Anruf zu. Also UNKNOWN, und der Router erfaehrt es
            # als mehrdeutig statt als „es ist nichts passiert".
            self.ledger.record_outcome(
                execution_id, call_state=C.UNKNOWN,
                delivery_state=C.DELIVERY_UNKNOWN, started_at=gestartet,
                ended_at=time.time(),
                provider_result={"ambiguous": exc.reason,
                                 "detail": exc.detail[:200]})
            log.warning("telephony.ambiguous_start", execution_id=execution_id,
                        reason=exc.reason)
            raise AmbiguousExecution(
                f"telephony_start_ambiguous:{exc.reason}") from None
        except TelephonyError as exc:
            # Eine BELEGTE Ablehnung des Anbieters. Nur hier ist "es hat nicht
            # geklingelt" Wissen statt Hoffnung.
            self.ledger.record_outcome(
                execution_id, call_state=C.FAILED,
                delivery_state=C.NOT_DELIVERED, started_at=gestartet,
                ended_at=time.time(),
                provider_result={"error": exc.reason, "detail": exc.detail[:200]})
            raise ExecutorUnavailable(f"telephony_start_failed:{exc.reason}") from None

        if not self.ledger.attach_conversation(execution_id, start.conversation_id):
            # Gewaehlt ist gewaehlt — aber der Faden gehoert jemand anderem. Das
            # ist mehrdeutig und darf weder als Erfolg noch als Fehlschlag
            # enden.
            log.warning("telephony.thread_conflict", execution_id=execution_id,
                        conversation_id=start.conversation_id[:64])
            self.ledger.record_outcome(
                execution_id, call_state=C.UNKNOWN,
                delivery_state=C.DELIVERY_UNKNOWN, started_at=gestartet,
                ended_at=time.time(),
                provider_result={"ambiguous": "conversation_id_conflict"})
            raise AmbiguousExecution("telephony_thread_conflict")
        log.info("telephony.dialled", execution_id=execution_id,
                 conversation_id=start.conversation_id[:64],
                 alias=identitaet.alias[:40])
        return await self._await_outcome(execution_id, start.conversation_id,
                                         identitaet, nachricht,
                                         started_at=gestartet)

    async def _await_outcome(self, execution_id: str, conversation_id: str,
                             identitaet: C.RecipientIdentity, nachricht: str,
                             started_at: float | None) -> dict[str, Any]:
        """Wartet begrenzt auf den Ausgang. Endet immer — notfalls mit UNKNOWN.

        Die Schleife hat drei Ausgaenge und keinen vierten: ein terminaler
        Anbieterzustand, ein aufgebrauchtes Zeitbudget, oder ein Anbieter, der
        nicht antwortet. In allen drei Faellen wird der Ledger geschlossen, und
        der letzte Fall wird ausdruecklich UNKNOWN — nicht FAILED. Wer nach
        einem Netzfehler "gescheitert" aufschreibt, behauptet, dass nichts
        passiert ist.
        """
        frist = time.time() + self.poll_budget
        abstand = POLL_INTERVAL_SECS
        letzter: Any = None
        while time.time() < frist:
            try:
                letzter = await self.provider.fetch_outcome(conversation_id)
            except TelephonyError as exc:
                log.warning("telephony.outcome_unavailable", reason=exc.reason,
                            conversation_id=conversation_id[:64])
                letzter = None
            if letzter is not None and letzter.finished:
                return self._close(execution_id, letzter, identitaet, nachricht,
                                   started_at, conversation_id)
            await self._sleep(abstand)
            abstand = min(abstand * 1.5, POLL_MAX_INTERVAL_SECS)

        # Budget aufgebraucht. Der Anruf kann trotzdem stattgefunden haben —
        # deshalb bleibt der Faden stehen und die Wiederaufnahme kann ihn spaeter
        # nachlesen.
        zustand = letzter.call_state if letzter is not None else C.UNKNOWN
        if zustand not in C.TERMINAL_CALL_STATES:
            zustand = C.UNKNOWN
        ergebnis = C.CallResult(
            call_id=execution_id, provider=self.provider.name,
            recipient_identity=identitaet, call_state=zustand,
            message_delivery_state=C.DELIVERY_UNKNOWN,
            conversation_id=conversation_id, started_at=started_at)
        self.ledger.record_outcome(
            execution_id, call_state=ergebnis.call_state,
            delivery_state=ergebnis.message_delivery_state,
            started_at=started_at, ended_at=time.time(),
            provider_result={"reason": "poll_budget_exhausted"})
        return _as_dict(ergebnis)

    def _close(self, execution_id: str, outcome: Any,
               identitaet: C.RecipientIdentity, nachricht: str,
               started_at: float | None, conversation_id: str) -> dict[str, Any]:
        """Uebersetzt einen terminalen Anbieterbefund in die Ergebniswahrheit."""
        zustellung = C.delivery_state_from_evidence(
            outcome.call_state,
            agent_delivered=C.message_evidence(outcome.transcript, nachricht),
            acknowledged=C.acknowledgement_evidence(outcome.transcript))

        ergebnis = C.CallResult(
            call_id=execution_id, provider=self.provider.name,
            recipient_identity=identitaet, call_state=outcome.call_state,
            message_delivery_state=zustellung,
            # Der Faden kommt aus dem EIGENEN Ledger, nicht aus dem
            # Antwortkoerper. Gewaehlt wurde unter dieser Kennung; was der
            # Anbieter zurueckmeldet, ist Fremdinhalt und darf sie nicht
            # ersetzen. Fehlte das Feld in der Antwort, haette hier sonst ein
            # leerer Faden gestanden — bei einem Anruf, der stattgefunden hat.
            conversation_id=conversation_id,
            started_at=started_at or outcome.started_at,
            ended_at=time.time(),
            recipient_response=_recipient_reply(outcome.transcript),
            transcript_summary=outcome.transcript_summary,
            provider_result={"status": str(outcome.raw.get("status") or ""),
                             "call_successful": outcome.call_successful,
                             "termination_reason": outcome.termination_reason},
            cost_truth=C.CostTruth(
                credits=outcome.cost_credits, fiat=outcome.cost_fiat,
                duration_secs=outcome.duration_secs,
                complete=outcome.cost_fiat is not None))

        self.ledger.record_outcome(
            execution_id, call_state=ergebnis.call_state,
            delivery_state=ergebnis.message_delivery_state,
            started_at=ergebnis.started_at, ended_at=ergebnis.ended_at,
            transcript_summary=ergebnis.transcript_summary,
            recipient_response=ergebnis.recipient_response,
            cost_credits=outcome.cost_credits, cost_fiat=outcome.cost_fiat,
            duration_secs=outcome.duration_secs,
            provider_result=ergebnis.provider_result)
        log.info("telephony.closed", execution_id=execution_id,
                 call_state=ergebnis.call_state,
                 delivery=ergebnis.message_delivery_state)
        return _as_dict(ergebnis)

    # -- Wiederaufnahme nach einem Neustart --------------------------------
    async def recover_open_calls(self) -> list[dict[str, Any]]:
        """Liest den Ausgang jedes offenen Anrufs nach. Ruft NIEMANDEN an.

        Das ist der ganze Unterschied zwischen Wiederaufnahme und Wiederholung:
        hier wird ausschliesslich gelesen. `telephony_call` ist
        `NON_IDEMPOTENT_WRITE`, und `recovery_decision()` beantwortet das mit
        MANUAL_RECOVERY_REQUIRED — also wird nicht nachgewaehlt, sondern
        nachgesehen.
        """
        # Ein eigener Vorgang, sonst gibt der Tresor gar nichts heraus.
        #
        # Ausserhalb eines Router-Vorgangs ist die Herkunft UNSPECIFIED, und der
        # Tresor verweigert sie — richtigerweise. Gebunden wird deshalb
        # ausdruecklich BACKGROUND_AUTOMATION mit der LESENDEN Faehigkeit: genau
        # das deckt `allow_background=True` der Tresorzeile ab, und mehr als
        # lesen soll hier auch niemand duerfen. Ein Anruf waere aus dieser
        # Herkunft ohnehin DENY.
        from solvio.capabilities import policy as AP
        from solvio.secret_vault import context as SC
        from solvio.telephony import elevenlabs as EL

        wieder: list[dict[str, Any]] = []
        offen = self.ledger.unfinished(C.TERMINAL_CALL_STATES)
        if not offen:
            return wieder
        with SC.bound(SC.UseContext(
                origin=AP.OriginClass.BACKGROUND_AUTOMATION,
                capability=EL.CAPABILITY_RESULT,
                automation_id="telephony-recovery",
                user_present=False)):
            return await self._recover_each(offen)

    async def _recover_each(self, offen: list[dict[str, Any]]) -> list[dict[str, Any]]:
        wieder: list[dict[str, Any]] = []
        for zeile in offen:
            identitaet = C.RecipientIdentity(
                alias=zeile["recipient_alias"],
                display_name=zeile["recipient_display"],
                recipient_handle=zeile["recipient_handle"])
            try:
                outcome = await self.provider.fetch_outcome(zeile["conversation_id"])
            except TelephonyError as exc:
                log.warning("telephony.recover_failed", reason=exc.reason,
                            execution_id=zeile["execution_id"])
                continue
            if not outcome.finished:
                continue
            wieder.append(self._close(zeile["execution_id"], outcome, identitaet,
                                      zeile["bound_message"],
                                      zeile.get("started_at"),
                                      zeile["conversation_id"]))
        if wieder:
            log.info("telephony.recovered", anzahl=len(wieder))
        return wieder


def _as_dict(result: C.CallResult) -> dict[str, Any]:
    """Die Ergebnishuelle als schlichte Abbildung — so erwartet sie der Router."""
    return {
        "call_id": result.call_id,
        "provider": result.provider,
        "conversation_id": result.conversation_id,
        "recipient_identity": {
            "alias": result.recipient_identity.alias,
            "display_name": result.recipient_identity.display_name,
            # Gekuerzt: der Freigabetext zeigt dem MENSCHEN die volle Nummer,
            # der Rueckgabewert landet im Kontext des Sprachmodells. Dort ist
            # die vollstaendige Rufnummer eines Dritten nicht noetig.
            "recipient_handle": C.mask_phone(
                result.recipient_identity.recipient_handle),
        },
        "call_state": result.call_state,
        "message_delivery_state": result.message_delivery_state,
        "started_at": result.started_at,
        "ended_at": result.ended_at,
        "recipient_response": result.recipient_response,
        "transcript_summary": result.transcript_summary,
        "provider_result": dict(result.provider_result),
        "cost_truth": {
            "credits": result.cost_truth.credits,
            "fiat": result.cost_truth.fiat,
            "duration_secs": result.cost_truth.duration_secs,
            "complete": result.cost_truth.complete,
            "note": result.cost_truth.note,
        },
        "content_trust": result.content_trust,
    }


def register(router: CapabilityRouter,
             capabilities: TelephonyCapabilities) -> list[str]:
    handlers = {"telephony_call": capabilities.call}
    describers = {"telephony_call": capabilities.describe_call}
    for name, handler in handlers.items():
        router.register(SPECS[name], handler, describe=describers.get(name))
    return sorted(handlers)
