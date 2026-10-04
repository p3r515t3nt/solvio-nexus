"""Die Bruecke vom Modell zum Browser.

Dieselbe duenne Bruecke wie bei Kalender, Home Assistant, Gmail und Recherche.
Auffaellig ist hier vor allem, was **nicht** darinsteht: es gibt kein Werkzeug
„JavaScript ausfuehren", keines fuer einen CSS-Pfad, keines fuer einen
Dateipfad und keinen Parameter, der eine Freigabe behaupten koennte. Ein Ziel
wird ueber Rolle und sichtbaren Namen benannt — genau so, wie ein Mensch es
beschreiben wuerde.

`risk_level` bleibt HARMLESS, weil die Entscheidung im Vertrag faellt. Lesen im
offenen Netz braucht keine Freigabe, und das soll es auch nicht: eine Webseite
zu oeffnen darf nicht am iPhone haengen. Schreiben kann diese Stufe ohnehin
nicht — der Browser laesst nichts ausser `GET` hinaus.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.browser import SPECS
from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

_SCHEMAS: dict[str, dict[str, Any]] = {
    "browser_open": {
        "description": "Oeffnet EINE bestimmte, bereits bekannte Webseite in "
                       "SOLVIOs eigenem Browser und liefert Titel und eine Vorschau. "
                       "Fuer Seiten, deren Inhalt erst durch JavaScript entsteht. "
                       "NICHT fuer allgemeine Recherche: wer einen Kurs, ein "
                       "Ergebnis oder einen aktuellen Stand sucht, nimmt "
                       "research_quick — dieses Werkzeug braucht eine konkrete "
                       "Adresse und findet von sich aus nichts. "
                       "Keine Anmeldung, kein lokales Netz.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string",
                    "description": "Vollstaendige oeffentliche http(s)-Adresse."}},
            "required": ["url"]},
    },
    "browser_extract": {
        "description": "Liest den sichtbaren Inhalt einer bereits geoeffneten Seite.",
        "parameters": {"type": "object", "properties": {
            "page_id": {"type": "string", "description": "Kennung aus browser_open."}},
            "required": ["page_id"]},
    },
    "browser_links": {
        "description": "Listet die Verweise einer geoeffneten Seite, optional gefiltert.",
        "parameters": {"type": "object", "properties": {
            "page_id": {"type": "string", "description": "Kennung aus browser_open."},
            "contains": {"type": "string",
                         "description": "Nur Verweise, deren Text das enthaelt."}},
            "required": ["page_id"]},
    },
    "browser_click": {
        "description": "Klickt ein Element, um weiterzulesen — benannt ueber seinen "
                       "sichtbaren Namen. Formulare, Uploads und Downloads werden "
                       "abgelehnt. Passt der Name auf mehrere Stellen, wird nachgefragt.",
        "parameters": {"type": "object", "properties": {
            "page_id": {"type": "string", "description": "Kennung aus browser_open."},
            "name": {"type": "string",
                     "description": "Der sichtbare Text oder das Label des Elements."},
            "role": {"type": "string",
                     "description": "Optional: link, button, checkbox, combobox, textbox."}},
            "required": ["page_id", "name"]},
    },
    "browser_back": {
        "description": "Geht auf der Seite einen Schritt zurueck.",
        "parameters": {"type": "object", "properties": {
            "page_id": {"type": "string", "description": "Kennung aus browser_open."}},
            "required": ["page_id"]},
    },
    "browser_wait": {
        "description": "Wartet kurz, bis nachgeladener Inhalt sichtbar ist.",
        "parameters": {"type": "object", "properties": {
            "page_id": {"type": "string", "description": "Kennung aus browser_open."},
            "seconds": {"type": "number", "description": "Hoechstens 10."}},
            "required": ["page_id"]},
    },
    "browser_close": {
        "description": "Schliesst eine Seite. Danach passiert dort nichts mehr.",
        "parameters": {"type": "object", "properties": {
            "page_id": {"type": "string", "description": "Kennung aus browser_open."}},
            "required": ["page_id"]},
    },
}


class BrowserCapabilityTool:
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
            log.warning("browser_capability.no_trusted_context", capability=self.capability)
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
        message = "Der Browser laeuft gerade nicht — es ist nichts passiert."
    elif result.outcome is CapabilityOutcome.TIMEOUT:
        message = "Die Seite hat zu lange gebraucht."
    else:
        message = result.human_message or "Das habe ich nicht ausgefuehrt."
    return ToolResult(False, data=result.data,
                      human_message=result.human_message or message,
                      error=f"{result.outcome.value}:{result.reason}" if result.reason
                      else result.outcome.value)


def browser_capability_tools(router: Any, gate: Any) -> list[BrowserCapabilityTool]:
    return [BrowserCapabilityTool(name, router, gate) for name in sorted(SPECS)]
