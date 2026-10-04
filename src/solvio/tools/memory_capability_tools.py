"""Die Bruecke vom Sprachweg zu den mutierenden Gedaechtnisfaehigkeiten.

WARUM ES DIESE DATEI GIBT

Ohne sie haetten die fuenf Faehigkeiten aus `capabilities/memory.py` **keinen
Aufrufer**. Der iPhone-WISSEN-Bildschirm ist ein eigener Milestone; bis dahin
koennte niemand einen Vorschlag bestaetigen, eine Erinnerung korrigieren oder
etwas vergessen lassen — obwohl der Contract genau das verlangt. Ein
Sicherheitspfad, der gebaut ist und nie gerufen wird, ist in diesem Projekt
schon zweimal als Schuld aufgefallen.

WAS SIE NICHT AENDERT

Das Modell darf **anfragen**, mehr nicht. Jede dieser Faehigkeiten ist
`RiskLevel.MUTATING` (bzw. `CRITICAL` fuer `purge`); der Router fuehrt sie damit
in den Freigabeweg, das iPhone zeigt den Text, und erst Face ID loest die
Ausfuehrung aus. Die Zielkennung steht im autorisierenden Text und ist im
`action_digest` gebunden — eine Freigabe fuer Erinnerung A kann Erinnerung B
nicht ausfuehren.

Und das Modell kann keine Kennung erfinden, die etwas Fremdes trifft: es kennt
nur, was ihm `memory_search` in diesem Gespraech gezeigt hat, und was es nicht
kennt, kann es nicht benennen. Wo es doch daneben greift, sieht der Mensch die
falsche Aussage im Freigabetext und lehnt ab.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
from solvio.capabilities.memory import SPECS
from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

_MEMORY_ID = {"type": "string",
              "description": "Kennung der Erinnerung, wie memory_search sie genannt hat"}
_STATEMENT = {"type": "string",
              "description": "Die betroffene Aussage im Wortlaut — sie steht im "
                             "Freigabetext, den der Nutzer liest"}

_SCHEMAS: dict[str, dict[str, Any]] = {
    "memory_confirm_candidate": {
        "description": ("Uebernimmt einen Wissensvorschlag als bestaetigtes Wissen. "
                        "Nur aufrufen, wenn der Nutzer ausdruecklich zustimmt. "
                        "Braucht seine Freigabe am Telefon."),
        "parameters": {"type": "object", "properties": {
            "candidate_id": {"type": "string", "description": "Kennung des Vorschlags"},
            "statement": _STATEMENT}, "required": ["candidate_id"]},
    },
    "memory_decline_candidate": {
        "description": ("Lehnt einen Wissensvorschlag ab; er wird nicht wieder "
                        "vorgeschlagen. Braucht die Freigabe des Nutzers."),
        "parameters": {"type": "object", "properties": {
            "candidate_id": {"type": "string", "description": "Kennung des Vorschlags"},
            "statement": _STATEMENT}, "required": ["candidate_id"]},
    },
    "memory_correct": {
        "description": ("Korrigiert eine Erinnerung. Die alte bleibt als Historie "
                        "erhalten. Nur aufrufen, wenn der Nutzer sagt, dass etwas "
                        "nicht stimmt. Braucht seine Freigabe."),
        "parameters": {"type": "object", "properties": {
            "memory_id": _MEMORY_ID,
            "statement": {"type": "string", "description": "Was stattdessen gilt"},
            "old_statement": {"type": "string", "description": "Was bisher gespeichert war"}},
            "required": ["memory_id", "statement"]},
    },
    "memory_forget": {
        "description": ("Nimmt eine Erinnerung aus dem aktiven Gedaechtnis und "
                        "schlaegt sie nicht wieder vor. Nur auf ausdruecklichen "
                        "Wunsch. Braucht die Freigabe des Nutzers."),
        "parameters": {"type": "object", "properties": {
            "memory_id": _MEMORY_ID, "statement": _STATEMENT},
            "required": ["memory_id"]},
    },
    "memory_purge": {
        "description": ("Loescht eine Erinnerung ENDGUELTIG und unwiderruflich, "
                        "samt Suchindex. Nur auf ausdruecklichen Wunsch. Braucht "
                        "die Freigabe des Nutzers."),
        "parameters": {"type": "object", "properties": {
            "memory_id": _MEMORY_ID, "statement": _STATEMENT},
            "required": ["memory_id"]},
    },
}


class MemoryCapabilityTool:
    """Reicht weiter und uebersetzt zurueck — entscheidet nichts."""

    risk_level = RiskLevel.HARMLESS       # die Stufe der FAEHIGKEIT gilt, nicht diese
    expose_to_llm = True

    def __init__(self, capability: str, router: Any, gate: Any) -> None:
        self.name = capability
        self.capability = capability
        self.router = router
        self.gate = gate

    def schema(self) -> dict[str, Any]:
        entry = _SCHEMAS[self.capability]
        return {"type": "function", "name": self.name,
                "description": entry["description"],
                "parameters": entry["parameters"]}

    async def run(self, arguments: dict) -> ToolResult:
        context = self.gate.context() if self.gate is not None else None
        if context is None or not context.has_principal:
            # Ohne belegten Anrufer wird an persoenlichem Gedaechtnis nichts
            # geaendert. Dieselbe Regel wie ueberall: fail-closed, und der
            # Grund wird ausgesprochen statt verschwiegen.
            log.warning("memory_capability.no_trusted_context",
                        capability=self.capability)
            return ToolResult(False, error="no_trusted_context",
                              human_message="Ich kann gerade nicht sicher feststellen, "
                                            "wer fragt — deshalb aendere ich nichts.")
        result: CapabilityResult = await self.router.execute(
            self.capability, dict(arguments or {}), trust=context.trust,
            provenance=self.gate.provenance_for(arguments or {}),
            principal=context.principal, origin=context.origin, commanded=context.commanded)
        return _speak(result)


def _speak(result: CapabilityResult) -> ToolResult:
    if result.succeeded:
        data = result.data if isinstance(result.data, dict) else {}
        if data.get("ok") is False:
            return ToolResult(False, data=result.data,
                              human_message="Das konnte ich nicht aendern.",
                              error=str(data.get("reason") or "refused"))
        return ToolResult(True, data=result.data,
                          human_message=result.human_message or "Erledigt.")
    if result.outcome is CapabilityOutcome.RECOVERY_REQUIRED:
        message = ("Ich weiss nicht sicher, ob das durchging — sieh bitte nach, "
                   "bevor wir es wiederholen.")
    elif result.outcome is CapabilityOutcome.EXECUTOR_UNAVAILABLE:
        message = "Ich komme gerade nicht an das Gedaechtnis — es ist nichts passiert."
    else:
        message = result.human_message or "Das habe ich nicht ausgefuehrt."
    return ToolResult(False, data=result.data,
                      human_message=result.human_message or message,
                      error=f"{result.outcome.value}:{result.reason}" if result.reason
                      else result.outcome.value)


def memory_capability_tools(router: Any, gate: Any) -> list[MemoryCapabilityTool]:
    return [MemoryCapabilityTool(name, router, gate) for name in sorted(SPECS)]
