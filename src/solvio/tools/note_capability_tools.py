"""Sprachwerkzeug fuer die Notizfaehigkeit — der fehlende Ausfuehrungsweg.

**Warum es das gibt, gemessen und nicht vermutet.** Der Satz „Schreib mir eine
Notiz mit dem Text Zahnarzt Dienstag 9 Uhr." wurde am echten Spracheinstieg
gemessen (`scripts/note_routing_live.py`, ein Modellaufruf):

    route_proposed = route_final = kein_auftrag
    confidence     = 0.98
    outcome        = handed_back
    agent_tasks    = 0   agent_runs = 0

**Die Route ist richtig.** `kein_auftrag` heisst laut Anweisung: „SOLVIO kann
das SELBST, hier und jetzt. Entweder direkt beantworten, oder mit den
Faehigkeiten, die es ohnehin hat." Eine Notiz zu schreiben ist genau das —
einschrittig, sofort, kein Auftrag, der laenger dauert als das Gespraech.

**Falsch war, dass es diese Faehigkeit im Gespraech nicht gab.** `note_write`
war nur der Agentenlaufzeit zugaenglich. Der Einstieg fuehrte damit ins Leere:
das Sprachmodell haette „ist notiert" sagen koennen, ohne dass irgendetwas
geschrieben wurde. Eine Antwort ohne Wirkung ist kein Erfolg.

**Das ist keine neue Architektur.** Es ist derselbe Adapter, den
`document_capability_tools`, `ha_capability_tools` und
`telephony_capability_tools` seit jeher benutzen: das Werkzeug fuehrt nichts
selbst aus, es reicht an `router.execute` weiter und traegt die Autoritaet des
Turns mit — Trust, Herkunft, Principal, `commanded`.

**Und es schwaecht nichts.** Gemessen an der Freigabematrix ist `note_write`
aus **jeder** Herkunft `require_face_id` — Raumstimme, iPhone, lokaler
Eigentuemer, Hintergrund. Der einzige Unterschied zum Agentenweg ist, dass es
dann **eine** Freigabe gibt statt zweier: die des Schreibens, nicht zusaetzlich
die des Auftragsanlegens.

> **Capabilities bleiben Werkzeuge, keine Nutzerbefehle.** Der Nutzer sagt
> „schreib mir eine Notiz", nicht `note_write`. Welches Werkzeug das wird,
> entscheidet SOLVIO — genau die Trennung, die der Auftrag verlangt.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.envelope import CapabilityResult
from solvio.capabilities.notes import SPECS, notes_root
from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

#: Was das Sprachmodell ueber die Faehigkeit erfaehrt.
#:
#: **Der Zielort steht ausdruecklich NICHT als freier Pfad drin.** Das Modell
#: soll einen Dateinamen waehlen, keinen Ort — der Ort kommt aus der
#: konfigurierten Ablage (`capabilities/notes.py`). Ein Prompt, der zum
#: Pfadraten einlaedt, erzeugt genau die Ablehnungen, die niemand braucht.
_SCHEMA: dict[str, Any] = {
    "description": (
        "Schreibt eine Notiz fuer den Eigentuemer: haengt eine Zeile an eine "
        "Notizdatei an. Nimm das, wenn er sich etwas notieren lassen will - "
        "'schreib mir auf', 'notier dir', 'merk dir das schriftlich'. "
        "Der Ablageort steht fest; du waehlst nur den Dateinamen und den Text. "
        "Nicht dafuer: etwas, das du dir bloss merken sollst - dafuer gibt es "
        "das Gedaechtnis."),
    "parameters": {
        "type": "object",
        "properties": {
            "pfad": {
                "type": "string",
                "description": ("Dateiname in der Notizablage, etwa "
                                "'notizen.md'. Kein Verzeichnis, kein "
                                "absoluter Pfad."),
            },
            "text": {
                "type": "string",
                "description": ("Die Zeile, die angehaengt wird — in den "
                                "Worten des Nutzers. Nicht umformulieren."),
            },
        },
        "required": ["pfad", "text"],
    },
}


class NoteCapabilityTool:
    """Ein Werkzeug, das nichts kann ausser weiterreichen."""

    risk_level = RiskLevel.HARMLESS
    expose_to_llm = True

    def __init__(self, router: Any, gate: Any) -> None:
        self.name = "note_write"
        self.capability = "note_write"
        self.router = router
        self.gate = gate
        self.ledger = None

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name,
                "description": _SCHEMA["description"],
                "parameters": _SCHEMA["parameters"]}

    async def run(self, arguments: dict) -> ToolResult:
        args = dict(arguments or {})
        context = self.gate.context() if self.gate is not None else None
        if context is None or not context.has_principal:
            # Ohne belegten Turn wird nichts geschrieben. Das ist dieselbe
            # Schranke wie bei jedem anderen Faehigkeitswerkzeug.
            log.warning("note_tool.no_trusted_context")
            return ToolResult(False, error="no_trusted_context",
                              human_message="Ich kann gerade nicht sicher "
                                            "feststellen, wer fragt — deshalb "
                                            "schreibe ich nichts.")
        result: CapabilityResult = await self.router.execute(
            self.capability, args, trust=context.trust,
            provenance=self.gate.provenance_for(args),
            principal=context.principal, origin=context.origin,
            commanded=context.commanded)
        from solvio.tools.agent_capability_tools import remember_pending_start
        remember_pending_start(self.ledger, capability=self.capability,
                               result=result, arguments=args, context=context)
        if result.succeeded:
            return ToolResult(True, data=result.data,
                              human_message=result.human_message or "")
        return ToolResult(False, data=result.data,
                          human_message=result.human_message or "",
                          error=(f"{result.outcome.value}:{result.reason}"
                                 if result.reason else result.outcome.value))


def note_capability_tools(router: Any, gate: Any) -> list[NoteCapabilityTool]:
    if "note_write" not in SPECS:
        return []
    return [NoteCapabilityTool(router, gate)]


def notes_location() -> str:
    """Wo die Notizen liegen — fuer Runbook und Diagnose, nicht fuer das Modell."""
    return notes_root()
