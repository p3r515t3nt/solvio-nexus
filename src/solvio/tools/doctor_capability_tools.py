"""Die Bruecke vom Modell zum Arzt.

Auffaellig ist wieder, was NICHT im Schema steht: `system_heal` hat keine
Parameter. Kein Komponentenname, keine Liste, kein Vorgehen. Das Modell kann
sagen „richte, was kaputt ist" — es kann nicht sagen, WAS angefasst wird.

Das ist der Unterschied zwischen einem Helfer und einer Fernbedienung fuer die
Innereien. Ein Feld, in das ein Komponentenname passt, waere ein Modell, das
das Ziel einer Handlung bestimmt; ein Feld, in das ein Befehl passt, waere eine
Katastrophe. Beides gibt es hier nicht, und zwar bauartbedingt.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
from solvio.capabilities.doctor import SPECS
from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

_SCHEMAS: dict[str, dict[str, Any]] = {
    "system_diagnose": {
        "description": ("Sagt, warum etwas an SOLVIO gerade nicht funktioniert "
                        "— mit vermuteter Ursache und wie sicher das ist. Ohne "
                        "Angabe: alles, was nicht in Ordnung ist. Nutze das bei "
                        "Fragen wie 'Warum geht mein Kalender nicht?'."),
        "parameters": {"type": "object", "properties": {
            "komponente": {"type": "string",
                           "description": "Optional. kalender, email, "
                                          "recherche, browser, portal, "
                                          "hintergrund, zuhause."}}},
    },
    "system_heal": {
        "description": ("Versucht zu richten, was gerade kaputt ist. Nimmt "
                        "bewusst keine Angabe entgegen: repariert wird nur, was "
                        "SOLVIO selbst befundet hat, und nur mit hinterlegten "
                        "Vorgehen. Nutze das bei 'Repariere, was kaputt ist'. "
                        "Was einen Menschen braucht, wird genannt statt "
                        "angefasst."),
        "parameters": {"type": "object", "properties": {}},
    },
}


class DoctorCapabilityTool:
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
            log.warning("doctor_capability.no_trusted_context",
                        capability=self.capability)
            return ToolResult(False, error="no_trusted_context",
                              human_message="Ich kann gerade nicht sicher "
                                            "feststellen, wer fragt — deshalb "
                                            "mache ich nichts.")
        result: CapabilityResult = await self.router.execute(
            self.capability, args, trust=context.trust,
            provenance=self.gate.provenance_for(args), principal=context.principal,
            origin=context.origin, commanded=context.commanded)
        return _speak(result)


def _speak(result: CapabilityResult) -> ToolResult:
    if result.succeeded:
        spoken = ""
        if isinstance(result.data, dict):
            spoken = str(result.data.get("antwort", "") or "")
        return ToolResult(True, data=result.data,
                          human_message=result.human_message or spoken)
    if result.outcome is CapabilityOutcome.EXECUTOR_UNAVAILABLE:
        message = "Ich kann meinen eigenen Zustand gerade nicht pruefen."
    else:
        message = result.human_message or "Das habe ich nicht ausgefuehrt."
    return ToolResult(False, data=result.data,
                      human_message=result.human_message or message,
                      error=f"{result.outcome.value}:{result.reason}" if result.reason
                      else result.outcome.value)


def doctor_capability_tools(router: Any, gate: Any) -> list[DoctorCapabilityTool]:
    return [DoctorCapabilityTool(name, router, gate) for name in sorted(SPECS)]
