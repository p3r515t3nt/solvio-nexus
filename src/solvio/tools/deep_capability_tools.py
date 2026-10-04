"""Die Bruecke vom Modell zur tiefen Recherche.

Dieselbe Bruecke wie bei Kalender, Home Assistant und Gmail, und aus demselben
Grund so duenn: das Modell nennt ein Thema, der Vertrag entscheidet alles andere.
Kein Feld dieser Schemata traegt Autoritaet — es gibt keinen Parameter fuer
„dringend", keinen fuer „freigegeben", keinen fuer den Auftraggeber. Wer fragt,
steht im Turn, nicht im Argument.

`risk_level` bleibt HARMLESS, weil die Entscheidung im Vertrag faellt und nicht
hier. Tiefe Recherche ist rein lesend; sie kommt ohne Freigabe aus, und das soll
sie auch, sonst wuerde jede Frage am iPhone haengen.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.deep import SPECS
from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

_SCHEMAS: dict[str, dict[str, Any]] = {
    "deep_research": {
        "description": "Recherchiert ein Thema gruendlich im oeffentlichen Netz und "
                       "liefert Zusammenfassung, Quellen und offene Fragen. Dauert "
                       "laenger als eine normale Antwort.",
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string",
                      "description": "Das zu recherchierende Thema oder die Frage."}},
            "required": ["topic"]},
    },
    "deep_task_status": {
        "description": "Sagt, wie weit eine begonnene Recherche ist, und liefert das "
                       "Ergebnis, sobald es vorliegt.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string",
                        "description": "Die Kennung aus deep_research."}},
            "required": ["task_id"]},
    },
    "deep_cancel": {
        "description": "Bricht eine laufende Recherche ab.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string",
                        "description": "Die Kennung aus deep_research."}},
            "required": ["task_id"]},
    },
}


class DeepCapabilityTool:
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = True

    def __init__(self, capability: str, router: Any, gate: Any) -> None:
        self.name = capability
        self.capability = capability
        self.router = router
        self.gate = gate

    def schema(self) -> dict[str, Any]:
        entry = _SCHEMAS[self.capability]
        return {"type": "function", "name": self.name,
                "description": entry["description"], "parameters": entry["parameters"]}

    async def run(self, arguments: dict) -> ToolResult:
        args = dict(arguments or {})
        context = self.gate.context() if self.gate is not None else None
        if context is None or not context.has_principal:
            log.warning("deep_capability.no_trusted_context", capability=self.capability)
            return ToolResult(False, error="no_trusted_context",
                              human_message="Ich kann gerade nicht sicher feststellen, "
                                            "wer fragt — deshalb mache ich nichts.")
        result: CapabilityResult = await self.router.execute(
            self.capability, args, trust=context.trust,
            provenance=self.gate.provenance_for(args), principal=context.principal,
            origin=context.origin, commanded=context.commanded)
        return _speak(result)


#: Was der Mensch bei einem abwesenden Rechercheweg hoeren soll — je nach
#: GRUND, nicht als ein Satz fuer alle vier Lagen.
#:
#: Diese Texte existierten schon, und sie waren tot: `_speak` bevorzugte
#: `result.human_message`, und die kam immer vom Router („Dafuer ist gerade
#: nichts erreichbar — es ist nichts passiert"). Gesprochen wurde am
#: 2026-08-29 daraus „Ich bekomme gerade keinen Zugriff auf die Recherche im
#: Netz" — bei erschoepftem Kontingent die falscheste aller moeglichen
#: Aussagen: sie klingt nach „ich kann das nicht", obwohl die Faehigkeit da
#: ist und sich von selbst erholt.
#:
#: Jetzt gewinnt der Grund. Fuer alles ohne eigenen Eintrag bleibt der
#: allgemeine Satz.
_UNAVAILABLE_SPEECH: dict[str, str] = {
    # Kontingent ist kein Koennen. Die Kappe ist ein UTC-Kalendertag; „in der
    # Nacht" ist deshalb ehrlich, eine Uhrzeit in Ortszeit waere geraten.
    "provider_quota":
        "Recherchieren kann ich grundsaetzlich — aber das Tagesbudget dafuer "
        "ist gerade aufgebraucht. Es erneuert sich von selbst in der Nacht, "
        "danach geht es wieder.",
    # Die Aufgabe selbst war zu gross. Das erholt sich nie von allein, und
    # deshalb steht hier kein Wort ueber Warten.
    "task_budget_exhausted":
        "Die Recherche ist fuer diesen Auftrag zu umfangreich geworden und "
        "wurde gestoppt. Enger gefasst versuche ich es gern noch einmal.",
    # Ein Einrichtungsproblem. Warten hilft nicht, und es ist kein Kontingent.
    "provider_auth":
        "Der Zugang zum Anbieter stimmt nicht — das ist ein "
        "Einrichtungsproblem, kein Kontingent.",
    "executor_unavailable":
        "Der Rechercheweg ist gerade nicht erreichbar — es laeuft nichts.",
}

_UNAVAILABLE_DEFAULT = _UNAVAILABLE_SPEECH["executor_unavailable"]


def _speak(result: CapabilityResult) -> ToolResult:
    if result.succeeded:
        return ToolResult(True, data=result.data, human_message=result.human_message or "")
    if result.outcome is CapabilityOutcome.EXECUTOR_UNAVAILABLE:
        # Hier gewinnt der eigene Text ueber den des Routers, und nur hier.
        # Der Router kennt den Unterschied zwischen Kontingent, Zugang und
        # Ausfall nicht; die Recherche kennt ihn.
        message = _UNAVAILABLE_SPEECH.get(result.reason, _UNAVAILABLE_DEFAULT)
        return ToolResult(False, data=result.data, human_message=message,
                          error=f"{result.outcome.value}:{result.reason}"
                          if result.reason else result.outcome.value)
    if result.outcome is CapabilityOutcome.TIMEOUT:
        message = "Die Recherche hat zu lange gebraucht."
    else:
        message = result.human_message or "Das habe ich nicht ausgefuehrt."
    return ToolResult(False, data=result.data,
                      human_message=result.human_message or message,
                      error=f"{result.outcome.value}:{result.reason}" if result.reason
                      else result.outcome.value)


def deep_capability_tools(router: Any, gate: Any) -> list[DeepCapabilityTool]:
    return [DeepCapabilityTool(name, router, gate) for name in sorted(SPECS)]
