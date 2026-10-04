"""Modellwerkzeuge fuer bestaetigte Empfaenger und Nachrichtenversand."""
from __future__ import annotations

from typing import Any

from solvio.capabilities.envelope import CapabilityResult
from solvio.capabilities.communication import SPECS
from solvio.tools.base import RiskLevel, ToolResult
from solvio.tools.gmail_capability_tools import _speak

_SCHEMAS = {
    "communication_resolve_recipient": {
        "description": "Loest eine Person oder Beziehung aus einer Bitte wie ‘sag, schreib oder melde X, dass …’ auf. Unbestaetigte Gmail-Treffer sind nur Vorschlaege und niemals Autoritaet.",
        "parameters": SPECS["communication_resolve_recipient"].input_schema},
    "communication_confirm_binding": {
        "description": "Bestaetigt nach ausdruecklicher Nutzerfreigabe, welche Kontaktwege zu einem Alias gehoeren.",
        "parameters": SPECS["communication_confirm_binding"].input_schema},
    "communication_send": {
        "description": "Sendet den wortgetreuen Inhalt einer Nachricht an einen bereits bestaetigten Alias und dessen gebundenen Kontaktweg.",
        "parameters": SPECS["communication_send"].input_schema},
}


class CommunicationCapabilityTool:
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = True

    def __init__(self, capability: str, router: Any, gate: Any) -> None:
        self.name = self.capability = capability
        self.router, self.gate = router, gate
        #: Auftragsbuch fuer den Merkzettel eines Versands, der auf Face ID wartet
        #: (ADR-0041); gesetzt, sobald die Agentenlaufzeit steht.
        self.ledger = None

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
        from solvio.tools.agent_capability_tools import remember_pending_start
        remember_pending_start(self.ledger, capability=self.capability,
                               result=result, arguments=args, context=context)
        return _speak(result)


def communication_capability_tools(router: Any, gate: Any) -> list[CommunicationCapabilityTool]:
    # Imports belong to the attested app endpoint, never the model toolkit.
    return [CommunicationCapabilityTool(name, router, gate) for name in sorted(_SCHEMAS)]
