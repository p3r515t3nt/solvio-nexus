"""Shared canonical memory handlers for explicitly authorized changes.

The existing iPhone path signs one concrete action with Face ID. N5 additionally
admits five explicit owner-browser commands through the same S1/execution
journal, with a separate closed BrowserCommandAuthorization. There is no
ambient permission for models, room audio or other dashboard mutations.

Every command binds its actual target and arguments. A historical success needs
a matching execution receipt; an uncertain effect never silently runs again.
The exact N5 security amendment is documented in docs/plan/N5_PRIVATE_DASHBOARD.md.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.contract import CapabilitySpec, ExecutionClass
from solvio.nodes.models import DataClass
from solvio.logging_setup import get_logger
from solvio.security.mobile_approval.execution import NON_IDEMPOTENT_WRITE
from solvio.tools.base import RiskLevel

log = get_logger("capabilities")


def _command_channel() -> str:
    # Origin is set by the authenticated Core entry, never a memory argument.
    from solvio.secret_vault.context import current
    from solvio.capabilities.policy import OriginClass
    return "dashboard" if current().origin is OriginClass.TRUSTED_DASHBOARD else "device"

#: Alle fuenf sind mutierend. `purge` ist zusaetzlich unwiderruflich und traegt
#: deshalb die hoechste Stufe — der Freigabetext sagt das auch.
SPECS: dict[str, CapabilitySpec] = {
    "memory_confirm_candidate": CapabilitySpec(
        name="memory_confirm_candidate", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.MUTATING,
        semantics=NON_IDEMPOTENT_WRITE, data_class=DataClass.HOME_ONLY,
        description="Einen Wissensvorschlag als bestaetigtes Wissen uebernehmen"),
    "memory_decline_candidate": CapabilitySpec(
        name="memory_decline_candidate", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.MUTATING,
        semantics=NON_IDEMPOTENT_WRITE, data_class=DataClass.HOME_ONLY,
        description="Einen Wissensvorschlag ablehnen und nicht wieder vorschlagen"),
    "memory_correct": CapabilitySpec(
        name="memory_correct", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.MUTATING,
        semantics=NON_IDEMPOTENT_WRITE, data_class=DataClass.HOME_ONLY,
        description="Eine Erinnerung korrigieren; die alte bleibt Historie"),
    "memory_forget": CapabilitySpec(
        name="memory_forget", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.MUTATING,
        semantics=NON_IDEMPOTENT_WRITE, data_class=DataClass.HOME_ONLY,
        description="Eine Erinnerung aus dem aktiven Gedaechtnis nehmen"),
    "memory_purge": CapabilitySpec(
        name="memory_purge", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.CRITICAL,
        semantics=NON_IDEMPOTENT_WRITE, data_class=DataClass.HOME_ONLY,
        description="Eine Erinnerung endgueltig loeschen. Nicht umkehrbar"),
}

#: Beschriftungen fuer den Freigabetext. Sie muessen je Faehigkeit EINDEUTIG
#: sein — zwei Argumente mit derselben Beschriftung waeren zwei Handlungen mit
#: demselben autorisierenden Text.
LABELS: dict[str, tuple[str, dict[str, str]]] = {
    "memory_confirm_candidate": ("Als bestaetigtes Wissen speichern", {
        "candidate_id": "Vorschlag", "statement": "Aussage",
        "replaces": "Ersetzt"}),
    "memory_decline_candidate": ("Vorschlag ablehnen und nicht wieder vorschlagen", {
        "candidate_id": "Vorschlag", "statement": "Aussage"}),
    "memory_correct": ("Erinnerung korrigieren", {
        "memory_id": "Erinnerung", "old_statement": "Bisher",
        "statement": "Neu"}),
    "memory_forget": ("Aus dem Gedaechtnis nehmen", {
        "memory_id": "Erinnerung", "statement": "Aussage"}),
    "memory_purge": ("ENDGUELTIG loeschen — nicht umkehrbar", {
        "memory_id": "Erinnerung", "statement": "Aussage"}),
}


class MemoryCapabilities:
    """Die Handler. Sie laufen erst nach der gebundenen Autorisierung dieser Handlung.

    DIE AUFRUFKONVENTION: jeder Handler nimmt GENAU EIN Argument — das
    Argument-Dict. So ruft `router._call` auf (`handler(args)`), und so machen
    es Kalender, Gmail, HA und alle anderen.

    Sie standen hier zuerst als `def forget(self, memory_id="", statement="")`
    — also mit Schluesselwoertern. Live gemessen: das ganze Dict landete in
    `memory_id`, der Datenbankzugriff flog, und weil die Ausnahme NACH dem
    durablen Anspruch kam, meldete der eingefrorene Pfad korrekt
    `unknown_outcome` -> RECOVERY_REQUIRED. Der Mensch hatte mit Face ID
    freigegeben, und nichts geschah.

    Ein Test, der die Handler mit `**args` ruft, findet das nie. Deshalb ruft
    die Zusicherung sie jetzt so auf, wie der Router es tut.

    Jeder Handler bekommt die Zielkennung als Argument — und genau diese
    Kennung stand im Text, den der Mensch unterschrieben hat. Weicht sie beim
    Einloesen ab, faellt die Ausfuehrung vorher in `approval_drift`.
    """

    def __init__(self, service: Any, adaptive: Any = None) -> None:
        self.service = service            # MemoryService
        self.adaptive = adaptive          # AdaptiveMemory (optional)

    # ------------------------------------------------------------ Kandidaten
    async def confirm_candidate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        candidate_id = str(arguments.get("candidate_id", "") or "")
        if self.adaptive is None:
            return {"ok": False, "reason": "adaptive_memory_unavailable"}
        memory_id = await self.adaptive.confirm_candidate(candidate_id, via=_command_channel())
        if not memory_id:
            return {"ok": False, "reason": "candidate_not_open"}
        await _reproject(self.service)
        return {"ok": True, "memory_id": memory_id}
    async def decline_candidate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        candidate_id = str(arguments.get("candidate_id", "") or "")
        if self.adaptive is None:
            return {"ok": False, "reason": "adaptive_memory_unavailable"}
        ok = await self.adaptive.decline_candidate(candidate_id)
        return {"ok": ok, "reason": "" if ok else "candidate_not_open"}
    async def correct(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Korrektur schlaegt Maschine — und schlaegt auch eigenes frueheres Wort.

        Es entsteht ein neuer `user_direct`-Record, der den alten abloest. Die
        Kette traegt `corrected:`; die Historie bleibt ueber `history()`
        erreichbar.
        """
        from datetime import datetime, timezone

        from solvio.contracts.memory import MemoryRecord, ProvenanceEntry
        from solvio.contracts.trust import SourceType, TrustLevel
        from solvio.memory.adaptive import lifecycle as L

        memory_id = str(arguments.get("memory_id", "") or "")
        statement = str(arguments.get("statement", "") or "")
        semantic = getattr(self.service, "semantic", None)
        if semantic is None:
            return {"ok": False, "reason": "memory_unavailable"}
        old = await semantic.get(memory_id)
        if old is None:
            return {"ok": False, "reason": "unknown_memory"}
        if not (statement or "").strip():
            return {"ok": False, "reason": "empty_statement"}

        now = datetime.now(timezone.utc)
        source = "correction:" + _command_channel()
        record = MemoryRecord(
            id="", memory_type=old.memory_type, content=statement.strip(),
            subject=old.subject, source=source,
            source_type=SourceType.USER_DIRECT,
            trust_level=TrustLevel.USER_DIRECT,
            created_at=now, updated_at=now, sensitivity=old.sensitivity,
            confidence=1.0, importance=old.importance,
            provenance=[ProvenanceEntry(
                source_type=SourceType.USER_DIRECT, source=source,
                trust_level=TrustLevel.USER_DIRECT, at=now,
                note=f"{L.CORRECTED_NOTE} {statement.strip()}"[:160])],
            tags=["corrected"], supersedes=memory_id,
            metadata={"corrected_from": memory_id, "explicit_intent": False})
        from solvio.memory.store import MemoryTargetChanged
        from solvio.security.mobile_approval.execution import SafeExecutionFailure
        try:
            new = await semantic.supersede(memory_id, record)
        except MemoryTargetChanged as exc:
            # The canonical transaction refused before INSERT. This exception
            # alone proves no write; later failures still have unknown outcome.
            raise SafeExecutionFailure("memory_target_changed") from exc
        if self.adaptive is not None:
            # Der alte Wortlaut soll nicht als Vermutung wiederkommen.
            await self.adaptive.note_forgotten(memory_id, content=old.content)
        await _reproject(self.service)
        log.info("memory.corrected", old=memory_id[:8], new=new.id[:8])
        return {"ok": True, "memory_id": new.id, "superseded": memory_id}

    async def forget(self, arguments: dict[str, Any]) -> dict[str, Any]:
        memory_id = str(arguments.get("memory_id", "") or "")
        semantic = getattr(self.service, "semantic", None)
        if semantic is None:
            return {"ok": False, "reason": "memory_unavailable"}
        record = await semantic.get(memory_id)
        content = record.content if record is not None else ""
        ok = await semantic.forget(memory_id, reason="user_request")
        if ok and self.adaptive is not None:
            # Ohne die Unterdrueckung waere „vergiss das" eine Bitte, die beim
            # naechsten Vorkommen wieder zur Frage wird.
            await self.adaptive.note_forgotten(memory_id, content=content)
        if ok:
            await _reproject(self.service)
        log.info("memory.forgotten", memory=memory_id[:8], ok=ok)
        return {"ok": ok, "reason": "" if ok else "unknown_memory"}

    async def purge(self, arguments: dict[str, Any]) -> dict[str, Any]:
        memory_id = str(arguments.get("memory_id", "") or "")
        semantic = getattr(self.service, "semantic", None)
        if semantic is None:
            return {"ok": False, "reason": "memory_unavailable"}
        record = await semantic.get(memory_id)
        content = record.content if record is not None else ""
        ok = await semantic.purge(memory_id, reason="user_request")
        if ok and self.adaptive is not None:
            await self.adaptive.note_forgotten(memory_id, content=content)
        if ok:
            await _reproject(self.service)
        log.info("memory.purged", memory=memory_id[:8], ok=ok)
        return {"ok": ok, "reason": "" if ok else "unknown_memory"}


async def _reproject(service: Any) -> None:
    """Nach jeder Mutation: die abgeleiteten Ansichten nachziehen lassen.

    Dieses Modul weiss NICHT, wer nachzieht, und importiert `solvio.knowledge`
    ausdruecklich nicht. Der Grund ist eine bestehende Zusicherung aus Knowledge
    Architecture V2: kein Modul unter `security/`, `capabilities/` oder
    `approval/` darf das Wissenspaket importieren, damit eine perfekt
    gefaelschte Wissensdatei keinen Aufrufer im Freigabepfad haette.

    Diese Zusicherung wurde beim ersten vollen Tor verletzt — von genau dieser
    Funktion, die vorher selbst kompilierte. Sie wurde nicht gelockert, sondern
    die Abhaengigkeit umgedreht: der Gedaechtnisdienst meldet die Aenderung, und
    wer darauf reagiert, entscheidet der Zusammenbau.

    Faellt das Nachziehen aus, scheitert die Handlung nicht: die kanonische
    Wahrheit ist bereits geaendert, die Ansicht zieht spaeter nach.
    """
    notify = getattr(service, "notify_changed", None)
    if notify is None:
        return
    try:
        notify()
    except Exception as exc:  # noqa: BLE001
        log.info("memory.reproject_deferred", kind=type(exc).__name__)


def register(router: Any, capabilities: MemoryCapabilities) -> list[str]:
    # Die Beschriftungen gehoeren in denselben Aufruf wie die Faehigkeiten: der
    # Text auf dem iPhone IST der autorisierende Text (`task` -> `action_digest`),
    # und ein generischer Rueckfall waere zwar eindeutig, aber unlesbar. Ein
    # Mensch soll unterschreiben, was er versteht.
    from solvio.capabilities.approval_gateway import ACTION_LABELS
    ACTION_LABELS.update(LABELS)

    handlers = {
        "memory_confirm_candidate": capabilities.confirm_candidate,
        "memory_decline_candidate": capabilities.decline_candidate,
        "memory_correct": capabilities.correct,
        "memory_forget": capabilities.forget,
        "memory_purge": capabilities.purge,
    }
    for name, handler in handlers.items():
        router.register(SPECS[name], handler)
    return sorted(handlers)
