"""M2 — die beiden Werkzeuge, über die das Modell Gedächtnis anfragen kann.

DIE AUTORITÄTSFRAGE

Das Modell darf einen Eintrag VORSCHLAGEN. Ob geschrieben wird, entscheidet SOLVIO
anhand der aktuellen Äußerung des lokalen Besitzers. Ohne diesen Riegel könnte ein
Modell aus seiner eigenen Antwort, aus einem Werkzeugergebnis oder aus einer
abgerufenen Seite dauerhaftes Wissen erzeugen — und ein späterer Abruf würde das
zurückspielen, als hätte der Nutzer es gesagt.

Abgerufenes Gedächtnis geht als `function_call_output` zurück, also als gewöhnlicher
Gesprächsinhalt niedriger Autorität. Es landet nie in der System-Anweisung.
"""
from __future__ import annotations

import asyncio

from typing import Any

from solvio.logging_setup import get_logger
from solvio.memory.intent import MemoryIntent
from solvio.memory.service import MemoryService, MemoryUnavailable
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger(__name__)


class PermitRefused(Exception):
    """Warum kein Mandat vorlag — kategorisch, damit ein Log es benennen kann."""


class MemoryIntentGate:
    """Die Erlaubnis, dauerhaft zu schreiben — gebunden an GENAU EINEN Nutzerturn.

    ZWEI DINGE, DIE NICHT ZUSAMMENFALLEN DÜRFEN

    Sicherheit: ein Mandat aus Turn N darf Turn N+1 NIE autorisieren, und weder Modell
    noch Werkzeug noch Abruf dürfen eines erzeugen.

    Produkt: der Provider ruft `memory_remember` oft, BEVOR das finalisierte Transkript
    desselben Turns eingetroffen ist — gemessen liegen dazwischen im Median rund eine
    halbe Sekunde. Diese ganz normale Reihenfolge darf nicht als Ablehnung beim Nutzer
    ankommen ("du musst 'merk dir' sagen", obwohl er es gesagt hat).

    Deshalb wartet ein Werkzeugaufruf BEGRENZT auf die Entscheidung SEINES Turns —
    nicht auf irgendeine. Trifft die Entscheidung ein, gilt sie. Wechselt der Turn,
    schlägt die Transkription fehl, schließt die Sitzung oder läuft die Frist ab, wird
    abgelehnt. Es wird nie geliehen und nie spekuliert.
    """

    #: Wie lange ein Werkzeugaufruf auf das Transkript seines Turns wartet.
    PERMIT_WAIT_SECONDS = 4.0

    def __init__(self) -> None:
        self._clear()
        self._arrival: asyncio.Event = asyncio.Event()
        # Das Gate lebt so lange wie der Prozess; Sitzungen kommen und gehen. Ein
        # dauerhaftes "geschlossen"-Flag waere deshalb falsch — es hat den Riegel nach
        # der ERSTEN Sitzung fuer immer verschlossen, und ab da konnte niemand mehr
        # etwas merken lassen, bis der Core neu startete.
        #
        # Stattdessen eine Epoche: jedes Schliessen und jeder Turnwechsel zaehlt sie
        # hoch. Wer wartet, merkt sich die Epoche beim Eintritt und lehnt ab, sobald sie
        # sich aendert — mit dem Grund, der sie hochgezaehlt hat. Eine NEUE Sitzung meldet
        # danach ihren Turn an und arbeitet ganz normal weiter.
        self._epoch = 0
        self._last_break = "session_closed"

    def _clear(self) -> None:
        self.intent: MemoryIntent | None = None
        self.conversation_id = ""
        self.message_id = ""
        self.session_id = ""
        self.turn_id = ""
        self.consumed = False
        self.decided = False              # liegt fuer diesen Turn eine Entscheidung vor?
        self._expected: tuple[str, str] | None = None

    def offer_user_turn(self, intent: MemoryIntent | None, *, conversation_id: str = "",
                        message_id: str = "", session_id: str = "",
                        turn_id: str = "") -> None:
        """Die Entscheidung EINES finalisierten Nutzertranskripts hinterlegen.

        Auch `None` wird hinterlegt — ein Turn ohne Merk-Absicht muss das Mandat des
        vorigen ausdrücklich löschen und wartende Aufrufe wecken, damit sie ablehnen.
        """
        expected = self._expected
        self._clear()
        self._expected = expected
        self.intent = intent
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.session_id = session_id
        self.turn_id = turn_id
        self.decided = True
        self._wake()

    def expect_turn(self, session_id: str, turn_id: str) -> None:
        """Der Reader meldet, für welchen Turn die folgende Werkzeugrunde arbeitet."""
        previous = self._expected
        self._expected = (session_id or "", turn_id or "")
        if previous is not None and previous != self._expected:
            self._break("turn_changed")   # Turnwechsel: Wartende sollen ablehnen

    def _break(self, reason: str) -> None:
        """Eine neue Epoche beginnen und alle Wartenden wecken."""
        self._epoch += 1
        self._last_break = reason
        self._wake()

    def clear(self) -> None:
        """Sitzungsende: das Mandat verfaellt, Wartende lehnen ab — aber das Gate bleibt
        fuer die naechste Sitzung benutzbar."""
        self._clear()
        self._break("session_closed")

    def _wake(self) -> None:
        self._arrival.set()
        self._arrival = asyncio.Event()

    def _match(self) -> MemoryIntent | None:
        if self.intent is None or self.consumed or self._expected is None:
            return None
        if (self.session_id, self.turn_id) != self._expected:
            return None
        self.consumed = True
        return self.intent

    def take(self) -> MemoryIntent | None:
        """Sofort einlösen, ohne zu warten. Für Aufrufer, die nicht warten können."""
        return self._match()

    async def await_permit(self, *, timeout: float | None = None) -> MemoryIntent:
        """Auf die Entscheidung des EIGENEN Turns warten — begrenzt.

        Wirft `PermitRefused` mit kategorischem Grund, statt still nichts zu tun.
        """
        expected = self._expected
        if expected is None:
            raise PermitRefused("no_turn_announced")
        epoch = self._epoch
        found = self._match()
        if found is not None:
            return found
        if self.decided and (self.session_id, self.turn_id) == expected:
            raise PermitRefused("no_explicit_intent")   # Entscheidung liegt vor: Nein.
        loop = asyncio.get_event_loop()
        deadline = loop.time() + (self.PERMIT_WAIT_SECONDS if timeout is None else timeout)
        while True:
            waiter = self._arrival
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise PermitRefused("transcript_timeout")
            try:
                await asyncio.wait_for(waiter.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                raise PermitRefused("transcript_timeout") from None
            if self._epoch != epoch:
                # Sitzung geschlossen ODER Turn gewechselt — in beiden Faellen gehoert
                # dieses Mandat nicht mehr hierher.
                raise PermitRefused(self._last_break)
            if self._expected != expected:
                raise PermitRefused("turn_changed")
            found = self._match()
            if found is not None:
                return found
            if self.decided and (self.session_id, self.turn_id) == expected:
                raise PermitRefused("no_explicit_intent")


class MemoryRememberTool:
    """Schreibt dauerhaftes Wissen — aber nur, wenn der Nutzer es gerade verlangt hat."""

    name = "memory_remember"
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = True

    def __init__(self, service: MemoryService, gate: MemoryIntentGate) -> None:
        self.service = service
        self.gate = gate

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name,
                "description": (
                    "Merkt sich dauerhaft etwas, das der Nutzer AUSDRUECKLICH merken "
                    "lassen will (z. B. 'merk dir ...', 'behalte im Gedaechtnis ...'). "
                    "Nur aufrufen, wenn der Nutzer genau das gerade gesagt hat. "
                    "SOLVIO prueft das und lehnt sonst ab."),
                "parameters": {"type": "object", "properties": {
                    "memory_type": {"type": "string",
                                    "description": "optional: user, preference, project, "
                                                   "rule, people, episodic, semantic"}},
                    "required": []}}

    async def run(self, arguments: dict) -> ToolResult:
        try:
            # Begrenzt auf die Entscheidung DIESES Turns warten. Der Provider ruft das
            # Werkzeug regelmaessig vor dem finalisierten Transkript auf; das ist normale
            # Reihenfolge und darf nicht als Ablehnung beim Nutzer ankommen.
            intent = await self.gate.await_permit()
        except PermitRefused as refusal:
            reason = str(refusal)
            log.info("memory.remember_refused", reason=reason)
            spoken = {
                "no_explicit_intent": "Ich speichere nur, was du mich ausdruecklich "
                                      "merken laesst.",
                "transcript_timeout": "Ich habe nicht sicher verstanden, was ich mir "
                                      "merken soll. Sag es bitte noch einmal.",
            }.get(reason, "Ich konnte mir das gerade nicht merken.")
            return ToolResult(False, error=reason, human_message=spoken)
        try:
            result = await self.service.remember(
                intent, conversation_id=self.gate.conversation_id,
                source_message_id=self.gate.message_id,
                source_session_id=self.gate.session_id,
                source_turn_id=self.gate.turn_id,
                memory_type=str(arguments.get("memory_type", "") or ""))
        except MemoryUnavailable as exc:
            log.error("memory.remember_failed", kind=type(exc).__name__)
            return ToolResult(False, error="memory_unavailable",
                              human_message="Ich konnte mir das gerade nicht merken.")
        if not result.ok:
            log.info("memory.remember_refused", reason=result.reason)
            message = {
                "looks_like_secret": "Das sieht nach einem Zugangsdatum aus — das merke "
                                     "ich mir nicht.",
                "ambiguous_correction": "Ich bin nicht sicher, welche Erinnerung du "
                                        "korrigieren willst. Sag mir bitte, welche.",
            }.get(result.reason, "Ich konnte mir das gerade nicht merken.")
            return ToolResult(False, error=result.reason, human_message=message)
        log.info("memory.remembered", action=result.action, memory_id=result.memory_id,
                 conversation_id=self.gate.conversation_id, chars=len(result.content),
                 indexed=result.indexed)
        # Dauerhaft gespeichert heisst nicht automatisch abrufbereit. Faellt die
        # Indizierung aus, liegt der Eintrag sicher im kanonischen Speicher — aber ein
        # schlichtes "Gemerkt." verspricht eine Abrufbereitschaft, die es noch nicht
        # gibt. Beides wird getrennt gesagt, und das Modell bekommt `indexed` mit.
        if not result.indexed:
            return ToolResult(True, data={"action": result.action,
                                          "memory_id": result.memory_id,
                                          "indexed": False},
                              human_message="Ich habe es dauerhaft gespeichert, aber mein "
                                            "Gedaechtnisindex ist gerade noch nicht "
                                            "bereit — abrufen kann ich es erst gleich.")
        spoken = {"created": "Gemerkt.", "duplicate": "Das wusste ich schon.",
                  "superseded": "Korrigiert, ich merke mir jetzt die neue Fassung."}
        return ToolResult(True, data={"action": result.action,
                                      "memory_id": result.memory_id,
                                      "indexed": True},
                          human_message=spoken.get(result.action, "Gemerkt."))


class MemorySearchTool:
    """Liest begrenzt aus dem Langzeitgedächtnis. Das Ergebnis ist Information."""

    name = "memory_search"
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = True

    def __init__(self, service: MemoryService, *, top_k: int = 3,
                 max_chars: int = 600) -> None:
        self.service = service
        self.top_k = top_k
        self.max_chars = max_chars

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name,
                "description": (
                    "Durchsucht SOLVIOs Langzeitgedaechtnis nach Fakten, Vorlieben "
                    "oder Projektwissen. Nutzen, wenn der Nutzer nach etwas fragt, "
                    "das er dir frueher gesagt haben koennte — und immer, bevor du "
                    "etwas vergisst oder korrigierst.\n"
                    "Jeder Treffer traegt `id` (fuer memory_forget / memory_correct), "
                    "`herkunft` (ausdruecklich gemerkt, bestaetigt oder abgeleitet) "
                    "und `warum` — den Satz, den du sagst, wenn der Nutzer fragt, "
                    "woher du das weisst. Sag diesen Satz WOERTLICH und erfinde nie "
                    "einen eigenen. Die `id` niemals aussprechen."),
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string", "description": "Wonach gesucht wird"}},
                    "required": ["query"]}}

    async def run(self, arguments: dict) -> ToolResult:
        query = str(arguments.get("query", "") or "").strip()
        if not query:
            return ToolResult(False, error="empty_query")
        try:
            hits = await self.service.search(query, top_k=self.top_k,
                                             max_chars=self.max_chars)
        except MemoryUnavailable:
            log.error("memory.search_failed", reason="unavailable")
            # Kein erfundener Treffer. Lieber sagen, dass gerade nichts abrufbar ist.
            return ToolResult(False, error="memory_unavailable",
                              human_message="Ich komme gerade nicht an mein Gedaechtnis.")
        log.info("memory.searched", hits=len(hits), chars=sum(len(h.content) for h in hits))
        if not hits:
            return ToolResult(True, data={"memories": []},
                              human_message="Dazu habe ich nichts gemerkt.")
        # Kennung und Herkunft gehen MIT zurueck — aber als Daten, nicht als
        # gesprochener Text.
        #
        # Frueher blieben beide hier, „damit sie nicht in einer gesprochenen
        # Antwort landen". Der Gedanke war richtig, die Folge falsch: ohne
        # Kennung konnte das Modell `memory_forget` und `memory_correct` gar
        # nicht aufrufen, und auf „woher weisst du das?" musste es die Antwort
        # erfinden — genau das, was Contract V2 §21 einen Bruch nennt.
        #
        # `warum` kommt aus der Ableitung, nicht aus dem Modell. Es ist der
        # vorlesbare Satz; die Kennung bleibt ungesprochen, weil das Schema es
        # sagt und weil eine Kennung im Ohr niemandem hilft.
        from solvio.memory.adaptive import lifecycle as L
        out = []
        for hit in hits:
            entry = {"id": hit.memory_id, "content": hit.content,
                     "remembered_at": hit.created_at[:10]}
            record = None
            try:
                record = await self.service.semantic.get(hit.memory_id)
            except Exception:  # noqa: BLE001 - ohne Record bleibt der Treffer nutzbar
                record = None
            if record is not None:
                entry["herkunft"] = L.lifecycle_of(record)
                entry["warum"] = L.explain(record)
            out.append(entry)
        return ToolResult(True, data={"memories": out})
