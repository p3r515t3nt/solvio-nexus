"""Die Bruecke vom Modell zur Kurzrecherche.

Dieselbe duenne Bruecke wie bei der tiefen Recherche
(`deep_capability_tools.py`): das Modell nennt eine Frage, der Vertrag
entscheidet alles andere. Der EINE Unterschied in der Beschreibung, die das
Modell sieht: sie sagt ausdruecklich, die Quelle kurz zu NENNEN und keine URL
vorzulesen — der Antworttext selbst enthaelt schon keine mehr
(`capabilities/research_quick.py` entfernt sie), aber das Modell soll auch
nicht aus `sources[]` eine URL vorlesen.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
from solvio.capabilities.research_quick import SPECS
from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

_SCHEMA: dict[str, Any] = {
    "description": "DER Weg fuer jede aktuelle Sachfrage: Kurs, Wetter, "
                   "Ergebnis, Termin, Zahl, Stand einer Sache. Antwortet SOFORT "
                   "im selben Gespraech, mit Websuche und Quellenangabe. "
                   "Benutze DIESES Werkzeug, nicht die Browserwerkzeuge — die "
                   "sind fuer eine bestimmte Seite, deren Adresse feststeht. "
                   "NENNE die Quelle kurz beim Namen (z. B. die Domain), aber "
                   "lies NIE eine URL vor, und nenne den Stand, auf den sich "
                   "die Zahlen beziehen. Fuer eine gruendliche, laenger "
                   "dauernde Recherche gibt es ein anderes Werkzeug.",
    "parameters": {"type": "object", "properties": {
        "question": {"type": "string",
                     "description": "Die VOLLSTAENDIGE Frage, aus sich heraus "
                                    "verstaendlich. Loese Bezuege aus dem "
                                    "Gespraech selbst auf: aus 'Und gestern?' "
                                    "wird 'DAX-Schlussstand am 6. September "
                                    "2026' — mit Thema UND konkretem Datum. "
                                    "Wer nur 'Und gestern?' weitergibt, sucht "
                                    "nach nichts."}},
        "required": ["question"]},
}


class ResearchQuickCapabilityTool:
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = True

    def __init__(self, router: Any, gate: Any) -> None:
        self.name = "research_quick"
        self.capability = "research_quick"
        self.router = router
        self.gate = gate

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name,
                "description": _SCHEMA["description"],
                "parameters": _SCHEMA["parameters"]}

    async def run(self, arguments: dict) -> ToolResult:
        args = dict(arguments or {})
        context = self.gate.context() if self.gate is not None else None
        if context is None or not context.has_principal:
            log.warning("research_quick.no_trusted_context")
            return ToolResult(False, error="no_trusted_context",
                              human_message="Ich kann gerade nicht sicher feststellen, "
                                            "wer fragt — deshalb mache ich nichts.")
        result: CapabilityResult = await self.router.execute(
            self.capability, args, trust=context.trust,
            provenance=self.gate.provenance_for(args), principal=context.principal,
            origin=context.origin, commanded=context.commanded)
        return _speak(result)


def _speak(result: CapabilityResult) -> ToolResult:
    if result.succeeded:
        return ToolResult(True, data=result.data, human_message=result.human_message or "")
    if result.outcome is CapabilityOutcome.EXECUTOR_UNAVAILABLE:
        message = "Die Kurzrecherche ist gerade nicht verfuegbar — es ist nichts passiert."
    elif result.outcome is CapabilityOutcome.TIMEOUT:
        message = "Die Suche hat zu lange gebraucht."
    else:
        message = result.human_message or "Das habe ich nicht ausgefuehrt."
    return ToolResult(False, data=result.data,
                      human_message=result.human_message or message,
                      error=f"{result.outcome.value}:{result.reason}" if result.reason
                      else result.outcome.value)


def research_quick_capability_tools(router: Any,
                                    gate: Any) -> list[ResearchQuickCapabilityTool]:
    assert sorted(SPECS) == ["research_quick"]
    return [ResearchQuickCapabilityTool(router, gate)]
