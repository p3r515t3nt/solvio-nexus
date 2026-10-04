"""Was das Modell ueber den Tresor erfahren darf — und was nicht.

**Genau EIN Werkzeug, und es ist lesend.** `secret_list` beantwortet die Frage
„welche Zugaenge gibt es und wofuer sind sie da". Damit kann ein Modell etwas
Sinnvolles tun — naemlich eine legitime Faehigkeit vorschlagen, die einen
Zugang benutzt — ohne je in die Naehe eines Wertes zu kommen.

**Was hier ausdruecklich fehlt, ist der Punkt dieser Datei.** Es gibt kein
Werkzeug zum Anlegen, Ersetzen, Loeschen oder Umwidmen. Diese Faehigkeiten sind
am Router angemeldet, aber sie haben keine Sprachseite: erreichbar sind sie
ausschliesslich ueber den attestierten Tresor-Weg des iPhones
(`solvio.secret_vault.endpoint`). Ein Modell kann sie nicht aufrufen, weil es
sie nicht nennen kann.

Und es gibt erst recht kein `get_secret`, `reveal_secret`, `dump_vault` oder
`export_credentials`. Nicht als deaktiviertes Werkzeug, nicht als auskommentierte
Zeile, nicht als Funktion mit einem anderen Namen. Ein Test zaehlt die
Werkzeugflaeche und faellt um, wenn das aufhoert zu stimmen.

Der Grund, warum eine LISTE trotzdem in Ordnung ist: ein Verweis ist Metadatum.
`secret://amazon/gregor` sagt, DASS es einen Zugang gibt — nicht, welcher er
ist. Genau darauf beruht die ganze Konstruktion.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

#: Die Werkzeugflaeche des Tresors. Eine Zeile. Bewusst.
_SCHEMAS: dict[str, dict[str, Any]] = {
    "secret_list": {
        "description": (
            "Nennt die im Tresor hinterlegten Zugaenge: Verweis "
            "(secret://dienst/konto), Name, Art, Zustand und wofuer sie benutzt "
            "werden duerfen. Liefert NIE einen Wert — Passwoerter, Schluessel und "
            "Token verlassen den Tresor nur intern zum jeweiligen Executor. Wenn "
            "der Nutzer einen Zugang hinterlegen oder aendern will, verweise ihn "
            "auf System → Tresor in der App."),
        "parameters": {"type": "object", "properties": {
            "capability": {"type": "string",
                           "description": "nur Zugaenge fuer diese Faehigkeit"}}},
    },
}


class SecretVaultTool:
    """Lesend, und mehr gibt es hier nicht."""

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
            log.warning("secret_vault.no_trusted_context", capability=self.capability)
            return ToolResult(False, error="no_trusted_context",
                              human_message="Ich kann gerade nicht sicher feststellen, "
                                            "wer fragt — deshalb sage ich nichts.")
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
        message = "Der Tresor ist gerade nicht erreichbar."
    else:
        message = result.human_message or "Das sage ich nicht."
    return ToolResult(False, data=result.data, human_message=message,
                      error=f"{result.outcome.value}:{result.reason}" if result.reason
                      else result.outcome.value)


def secret_vault_tools(router: Any, gate: Any) -> list[SecretVaultTool]:
    return [SecretVaultTool(name, router, gate) for name in sorted(_SCHEMAS)]
