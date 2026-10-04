"""Die Bruecke vom Sprach-Werkzeugpfad zum Capability-Vertrag.

Der Realtime-Core ruft weiterhin `dispatch()` — daran wird nichts geaendert, das
ist die einzige Aufrufstelle, die der Sicherheitspfad festnagelt. Was sich aendert:
hinter dem Werkzeug entscheidet nicht mehr das Werkzeug, sondern der Vertrag.

    Sprachanfrage -> Dispatcher -> dieses Werkzeug
                  -> vertrauenswuerdiger Aufrufkontext (Principal, Trust, Herkunft)
                  -> Capability-Router (Trust / Risiko / Freigabe)
                  -> HA-Executor -> Ergebnis-Umschlag

**Warum `risk_level = HARMLESS` hier richtig ist.** Das alte Risiko-Gate des
Dispatchers verlangt bei hoeheren Stufen `arguments["confirmed"]` — ein Feld, das
das Sprachmodell selbst fuellt. Diese Bruecke traegt deshalb bewusst die unterste
Stufe: die echte Entscheidung faellt eine Schicht tiefer, aus Herkunft und
Autoritaet, wo das Modell nichts zu melden hat. Das Gate durch ein Modellargument
zu befriedigen waere Theater; hier wird es schlicht nicht gebraucht.

Das dem Modell gezeigte Schema enthaelt **kein** Feld fuer Herkunft, Principal
oder Bestaetigung. Was es nicht gibt, kann das Modell nicht behaupten.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
from solvio.capabilities.home_assistant import SPECS
from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

#: Was das Modell sagen darf. Absichtlich nur Zielangaben in Nutzersprache.
_SCHEMAS: dict[str, dict[str, Any]] = {
    "ha_list_devices": {
        "description": "Nennt die freigegebenen Geraete im Haus, optional gefiltert "
                       "nach Raum (area) oder Art (domain: light, switch, cover, "
                       "media_player, sensor).",
        "parameters": {"type": "object", "properties": {
            "area": {"type": "string", "description": "z. B. Wohnzimmer, Flur"},
            "domain": {"type": "string", "description": "optionale Geraeteart"}}},
    },
    "ha_get_state": {
        "description": "Liest Zustand oder Messwert eines Geraets, z. B. ob eine Lampe "
                       "an ist oder wie warm es irgendwo ist.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Geraetename, wie der Nutzer ihn nennt"},
            "area": {"type": "string", "description": "optionaler Raum zur Eingrenzung"}},
            "required": ["name"]},
    },
    "ha_turn_on": {
        "description": "Schaltet ein Geraet ein (Licht, Schalter, Steckdose).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Geraetename, wie der Nutzer ihn nennt"},
            "area": {"type": "string", "description": "optionaler Raum zur Eingrenzung"}},
            "required": ["name"]},
    },
    "ha_turn_off": {
        "description": "Schaltet ein Geraet aus (Licht, Schalter, Steckdose).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Geraetename, wie der Nutzer ihn nennt"},
            "area": {"type": "string", "description": "optionaler Raum zur Eingrenzung"}},
            "required": ["name"]},
    },
    "ha_set_brightness": {
        "description": "Setzt die Helligkeit einer Lampe in Prozent (0-100).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Lampenname, wie der Nutzer sie nennt"},
            "area": {"type": "string", "description": "optionaler Raum zur Eingrenzung"},
            "brightness_pct": {"type": "integer", "minimum": 0, "maximum": 100}},
            "required": ["name", "brightness_pct"]},
    },
}


class HACapabilityTool:
    """Ein Werkzeug, das nichts entscheidet — es reicht weiter und uebersetzt zurueck."""

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
                "description": entry["description"],
                "parameters": entry["parameters"]}

    async def run(self, arguments: dict) -> ToolResult:
        args = dict(arguments or {})
        # Alles, was Autoritaet traegt, kommt aus dem Kontext — nie aus `args`.
        context = self.gate.context() if self.gate is not None else None
        if context is None or not context.has_principal:
            # Fail-closed: ohne bewiesenen Aufrufer wird nichts Wirksames getan.
            log.warning("ha_capability.no_trusted_context", capability=self.capability)
            return ToolResult(False, error="no_trusted_context",
                              human_message="Ich kann gerade nicht sicher feststellen, "
                                            "wer fragt — deshalb mache ich nichts.")
        provenance = self.gate.provenance_for(args)
        result: CapabilityResult = await self.router.execute(
            self.capability, args, trust=context.trust, provenance=provenance,
            principal=context.principal, origin=context.origin, commanded=context.commanded)
        return _speak(result)


def _speak(result: CapabilityResult) -> ToolResult:
    """Uebersetzt den Umschlag in das, was der Sprachpfad erwartet.

    Der Ausgang bleibt als stabiler Code erhalten, damit die Unterscheidung nicht
    im alten Bool verschwindet. Rohe Ausnahmen gehen nie mit.
    """
    if result.succeeded:
        data = result.data if isinstance(result.data, dict) else {}
        if "confirmed" in data and not data.get("confirmed"):
            # Angenommen, aber nicht bestaetigt. Der Nutzer erfaehrt das, statt sich
            # auf eine Zusage zu verlassen, die das Geraet nie eingeloest hat.
            return ToolResult(True, data=result.data,
                              human_message=f"Ich habe es geschaltet, aber "
                                            f"{data.get('name', 'das Geraet')} meldet "
                                            f"noch '{data.get('state', 'unbekannt')}'.")
        return ToolResult(True, data=result.data,
                          human_message=result.human_message or "")
    if result.outcome is CapabilityOutcome.EXECUTOR_UNAVAILABLE:
        message = ("Ich erreiche das Smart Home gerade nicht — es ist nichts passiert.")
    elif result.outcome is CapabilityOutcome.TIMEOUT:
        message = "Das Smart Home hat nicht rechtzeitig geantwortet."
    elif result.outcome is CapabilityOutcome.RECOVERY_REQUIRED:
        message = ("Ich weiss nicht sicher, ob das durchging — bitte pruef es, "
                   "bevor wir es wiederholen.")
    else:
        message = result.human_message or "Das habe ich nicht ausgefuehrt."
    return ToolResult(False, data=result.data,
                      human_message=result.human_message or message,
                      error=f"{result.outcome.value}:{result.reason}" if result.reason
                      else result.outcome.value)


def ha_capability_tools(router: Any, gate: Any) -> list[HACapabilityTool]:
    """Die dem Modell gezeigten HA-Werkzeuge — genau die registrierten Faehigkeiten."""
    return [HACapabilityTool(name, router, gate) for name in sorted(SPECS)]
