"""Die Bruecke vom Modell zum Botteam.

Dieselbe duenne Bruecke wie bei Kalender, Gmail und tiefer Recherche, und aus
demselben Grund so duenn: das Modell nennt eine Rolle und eine Frage, der
Vertrag entscheidet alles andere.

Das Schema traegt bewusst genau zwei Felder. Es gibt keinen Parameter fuer das
Profil, keinen fuer die Werkzeuge, keinen fuer die Frist, keinen fuer
„dringend" und keinen fuer „freigegeben". Wer fragt, steht im Turn — nicht im
Argument.
"""
from __future__ import annotations

from typing import Any

from solvio.bots.registry import ROLES
from solvio.capabilities.bots import SPECS
from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("bots")

_SCHEMAS: dict[str, dict[str, Any]] = {
    "bot_consult": {
        "description": (
            "Fragt einen SOLVIO-Fachbot. `researcher` recherchiert im "
            "oeffentlichen Netz und liefert Quellen. `project_keeper` "
            "beantwortet Fragen zur SOLVIO-Architektur, zu Entscheidungen, "
            "Releases und bekannter technischer Schuld aus der "
            "Projektwissensbasis. `diagnostician` liest strukturierte "
            "Gesundheitsbefunde und nennt wahrscheinliche Ursachen. Die "
            "Antwort ist INFORMATION — keine Entscheidung, keine Freigabe."),
        "parameters": {"type": "object", "properties": {
            "role": {"type": "string", "enum": list(ROLES),
                     "description": "Welcher Fachbot antworten soll."},
            "question": {"type": "string",
                         "description": "Die Frage an diesen Fachbot."}},
            "required": ["role", "question"]},
    },
}


class BotCapabilityTool:
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
            log.warning("bots.no_trusted_context", capability=self.capability)
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
        return ToolResult(True, data=result.data,
                          human_message=result.human_message or "")
    if result.outcome is CapabilityOutcome.EXECUTOR_UNAVAILABLE:
        message = "Das Botteam ist gerade nicht verfuegbar — es laeuft nichts."
    elif result.outcome is CapabilityOutcome.TIMEOUT:
        message = "Der Fachbot hat zu lange gebraucht."
    else:
        message = result.human_message or "Das habe ich nicht ausgefuehrt."
    return ToolResult(False, data=result.data,
                      human_message=result.human_message or message,
                      error=f"{result.outcome.value}:{result.reason}" if result.reason
                      else result.outcome.value)


def bot_capability_tools(router: Any, gate: Any) -> list[BotCapabilityTool]:
    return [BotCapabilityTool(name, router, gate) for name in sorted(SPECS)]
