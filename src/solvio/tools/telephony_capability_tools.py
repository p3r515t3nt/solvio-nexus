"""Modellwerkzeug fuer den Telefonanruf.

Die Bruecke ist absichtlich duenn und hat genau eine Aufgabe: den Wunsch des
Sprachmodells an den `CapabilityRouter` zu geben. Sie entscheidet nichts.

Zu `risk_level = RiskLevel.HARMLESS`, das hier falsch aussieht und richtig ist:
`ToolDispatcher` verlangt ab `MUTATING` ein Argument `confirmed`, das eine
Sprachrunde nie mitschickt (`tools/dispatcher.py`). Drei von vier
Zahlungswerkzeugen waren dadurch strukturell tot — sie konnten gar nicht
ausgeloest werden, und niemand merkte es, weil ein nie gerufenes Werkzeug auch
nie scheitert. Die echte Reibung sitzt eine Schicht tiefer: `telephony_call`
steht in `VERY_CRITICAL_BY_BIRTH`, und der Router holt dafuer Face ID. Ein
harmloses Werkzeug vor einer sehr strengen Faehigkeit ist genau die richtige
Aufteilung.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.envelope import CapabilityResult
from solvio.capabilities.telephony import SPECS
from solvio.tools.base import RiskLevel, ToolResult
from solvio.tools.gmail_capability_tools import _speak

_SCHEMAS = {
    "telephony_call": {
        "description": (
            "Ruft eine Person an und richtet ihr eine Nachricht aus, zum Beispiel bei "
            "‘ruf X an und sag ihm …’. Der Empfaenger wird als Alias oder Beziehung "
            "genannt — niemals als Rufnummer; die kommt ausschliesslich aus der "
            "bestaetigten Kontaktbindung. Der Anruf braucht immer die Freigabe des "
            "Eigentuemers und kostet Geld."),
        # NIE von Hand nachbauen: `router._validate` weist unbekannte Argumente
        # ab, und ein handgeschriebenes Schema laeuft frueher oder spaeter
        # auseinander.
        "parameters": SPECS["telephony_call"].input_schema},
}


class TelephonyCapabilityTool:
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = True

    def __init__(self, capability: str, router: Any, gate: Any) -> None:
        self.name = self.capability = capability
        self.router, self.gate = router, gate

    def schema(self) -> dict[str, Any]:
        entry = _SCHEMAS[self.capability]
        return {"type": "function", "name": self.name,
                "description": entry["description"], "parameters": entry["parameters"]}

    async def run(self, arguments: dict) -> ToolResult:
        args = dict(arguments or {})
        context = self.gate.context() if self.gate is not None else None
        if context is None or not context.has_principal:
            return ToolResult(False, error="no_trusted_context",
                              human_message="Ich kann nicht sicher feststellen, wer fragt.")
        result: CapabilityResult = await self.router.execute(
            self.capability, args, trust=context.trust,
            provenance=self.gate.provenance_for(args), principal=context.principal,
            origin=context.origin, commanded=context.commanded)
        return _speak(result)


def telephony_capability_tools(router: Any, gate: Any) -> list[TelephonyCapabilityTool]:
    return [TelephonyCapabilityTool(name, router, gate) for name in sorted(SPECS)]
