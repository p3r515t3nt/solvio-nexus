"""Die Bruecke vom Modell zur Arbeit — genau ein Werkzeug.

Dieselbe duenne Bruecke wie bei Kalender, Gmail und tiefer Recherche, und aus
demselben Grund so duenn: das Modell nennt ein Ziel, der Core entscheidet alles
andere.

Das Schema traegt bewusst GENAU EIN Feld. Es gibt keinen Parameter fuer den
Weg, keinen fuer das Modell, keinen fuer den Fachmann, keinen fuer „dringend"
und keinen fuer „freigegeben". Wer fragt, steht im Turn — nicht im Argument.
Und `auftrag` selbst entscheidet nichts: eingeschaetzt wird der abgeschlossene
Turn-Text, den das Provenienz-Tor ohnehin misst. Das Feld existiert, damit das
Modell sich festlegt, dass es ueberhaupt etwas beauftragt.

`risk_level` bleibt HARMLESS, und das ist nicht Bequemlichkeit, sondern die
Lehre aus dem stillgelegten `codex_task`: die Barriere des Dispatchers verlangt
ein `confirmed`, das eine Sprachrunde nie schickt — drei von vier
Zahlungswerkzeugen waren dadurch strukturell tot, und kein einziger der 2355
gruenen Tests sah es. Die Entscheidung faellt im Vertrag, hinter der Route, an
derselben Matrix wie zuvor.
"""
from __future__ import annotations

from typing import Any

from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

TOOL_NAME = "solvio_task"

SCHEMA: dict[str, Any] = {
    "description": (
        "Nimm das fuer alles, was ueber eine Antwort hinausgeht: nachsehen, "
        "herausfinden, pruefen, vergleichen, diagnostizieren, bauen oder "
        "reparieren. SOLVIO entscheidet danach selbst, wie es erledigt wird — "
        "ob es eine kurze Recherche ist, ein Fachmann, eine Diagnose oder ein "
        "laufender Auftrag. Du musst das nicht wissen und nicht waehlen.\n"
        "Tu es von SELBST, sobald ein Ziel danach klingt. Der Nutzer muss dich "
        "nicht darum bitten und kein bestimmtes Wort sagen.\n"
        "NICHT dafuer: eine einfache Frage, die du selbst beantworten kannst, "
        "ein Geraet, das du selbst schaltest, oder etwas, das schon laeuft "
        "(dafuer gibt es die Statuswerkzeuge)."),
    "parameters": {
        "type": "object",
        "properties": {
            "auftrag": {
                "type": "string",
                "description": ("Was getan werden soll, in den Worten des "
                                "Nutzers. Nicht umformulieren."),
            },
        },
        "required": ["auftrag"],
    },
}


class CognitionTool:
    """Ein Werkzeug, das nichts kann ausser fragen."""

    risk_level = RiskLevel.HARMLESS
    expose_to_llm = True

    def __init__(self, router: Any, gate: Any) -> None:
        self.name = TOOL_NAME
        self.router = router
        self.gate = gate

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name,
                "description": SCHEMA["description"],
                "parameters": SCHEMA["parameters"]}

    async def run(self, arguments: dict) -> ToolResult:
        args = dict(arguments or {})
        context = self.gate.context() if self.gate is not None else None
        if context is None or not context.has_principal:
            log.warning("cognition_tool.no_trusted_context")
            return ToolResult(False, error="no_trusted_context",
                              human_message="Ich kann gerade nicht sicher "
                                            "feststellen, wer fragt — deshalb "
                                            "mache ich nichts.")
        commission = await self.router.commission(
            context, auftrag=str(args.get("auftrag", "") or ""))
        if commission.ok:
            return ToolResult(True, data=commission.data,
                              human_message=commission.human_message or "")
        return ToolResult(False, data=commission.data,
                          human_message=commission.human_message
                          or "Das habe ich nicht ausgefuehrt.",
                          error=commission.error or "commission_failed")


def cognition_tools(router: Any, gate: Any) -> list[CognitionTool]:
    return [CognitionTool(router, gate)]
