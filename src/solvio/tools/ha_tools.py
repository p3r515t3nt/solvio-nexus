"""ALTBESTAND — nicht verdrahtet, und ausdruecklich nicht modell-erreichbar.

Dieses Modul ist die HA-Flaeche VOR dem Faehigkeitsvertrag. Es wird von
`tools/registry.py` nicht importiert; die produktive Flaeche ist
`ha_capability_tools.py` mit Freigabeliste, Sicherheitsdomaenen und
Klassifikation.

Warum es trotzdem gefaehrlich war: seine fuenf Werkzeuge trugen
`expose_to_llm = True`, sie loesen gegen ALLE `/api/states` auf (ohne
Freigabeliste), sie kennen keine sicherheitsnahen Domaenen — und sie rufen
`call_service("homeassistant", "turn_on"/"turn_off", ...)` auf der
Meta-Domaene auf, die auch ein Schloss oder ein Tor trifft. Eine einzige Zeile
im Registrar haette die gesamte Freigabeliste umgangen.

Gefunden bei der Inventur fuer den Geheimnistresor. Die Werkzeuge stehen jetzt
auf `expose_to_llm = False`: selbst wenn sie jemand versehentlich anmeldet,
erreicht sie kein Modell — `tools/dispatcher.py` prueft das Flag nicht nur beim
Auflisten, sondern auch beim Ausfuehren. Die Entfernung des Moduls ist als
Schuld gefuehrt; sie kostet den Umbau von elf Tests und gehoert nicht in einen
Sicherheits-Milestone, der gerade den Tresor baut.
"""
from __future__ import annotations

from typing import Any

from solvio.integrations.home_assistant import HomeAssistant, entity_summary
from solvio.tools.base import RiskLevel, ToolResult

# Domains, die "an/aus" sinnvoll unterstuetzen.
SWITCHABLE = ("light", "switch", "fan", "media_player", "input_boolean")
# Steuerbare Domains fuer die Standard-Auflistung (ohne Sensor-Rauschen).
CONTROLLABLE = ("light", "switch", "cover", "climate", "media_player", "fan", "input_boolean")


def _norm(s: str) -> str:
    """Kleinschreibung ohne Leerzeichen, fuer robusten Namensabgleich."""
    return "".join((s or "").lower().split())


class HAContext:
    """Teilt HA-Client + Entity-Aufloesung unter den Tools."""

    def __init__(self, ha: HomeAssistant) -> None:
        self.ha = ha

    async def _all(self) -> list[dict]:
        return [entity_summary(s) for s in await self.ha.states()]

    async def entities(self, domain: str | None = None) -> list[dict]:
        ents = await self._all()
        if domain:
            ents = [e for e in ents if e["domain"] == domain]
        else:
            ents = [e for e in ents if e["domain"] in CONTROLLABLE]
        return ents

    async def resolve(self, name: str, domains: tuple[str, ...] | None = None) -> list[dict]:
        name = (name or "").strip()
        ents = await self._all()
        if domains:
            ents = [e for e in ents if e["domain"] in domains]
        low = name.lower()
        nn = _norm(name)
        if not nn:
            return []
        # 1. exakte entity_id
        r = [e for e in ents if e["entity_id"].lower() == low]
        if r:
            return r
        # 2. exakter friendly_name (leerzeichen-/schreibweise-tolerant)
        r = [e for e in ents if _norm(e["friendly_name"]) == nn]
        if r:
            return r
        # 3. Teilstring (normalisiert): "flurlicht" in "flurlicht..."
        r = [e for e in ents if nn in _norm(e["friendly_name"])]
        if r:
            return r
        # 4. alle Suchwoerter kommen im friendly_name vor
        words = [w for w in low.split() if w]
        if words:
            r = [e for e in ents if all(w in e["friendly_name"].lower() for w in words)]
        return r


def _ambiguous(matches: list[dict]) -> ToolResult:
    opts = [{"entity_id": m["entity_id"], "friendly_name": m["friendly_name"]} for m in matches[:8]]
    names = ", ".join(m["friendly_name"] for m in matches[:8])
    return ToolResult(False, data={"options": opts},
                      human_message=f"Mehrere Geraete passen: {names}. Welches meinst du?")


def _not_found(name: str) -> ToolResult:
    return ToolResult(False, human_message=f"Ich finde kein Geraet namens '{name}'.")


class ListEntities:
    name = "home_assistant_list_entities"
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = False

    def __init__(self, ctx: HAContext) -> None:
        self.ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name,
                "description": "Listet steuerbare Home-Assistant-Geraete. Ohne domain nur "
                               "steuerbare (Licht, Schalter, Rolladen, Klima, Media). Mit domain "
                               "gefiltert (light, switch, cover, climate, media_player, sensor).",
                "parameters": {"type": "object", "properties": {
                    "domain": {"type": "string",
                               "description": "optionaler Domain-Filter"}}}}

    async def run(self, arguments: dict) -> ToolResult:
        domain = (arguments.get("domain") or "").strip() or None
        ents = await self.ctx.entities(domain)
        slim = [{"entity_id": e["entity_id"], "friendly_name": e["friendly_name"],
                 "state": e["state"]} for e in ents]
        return ToolResult(True, data={"count": len(slim), "entities": slim[:80]},
                          human_message=f"Ich sehe {len(slim)} steuerbare Geraete" +
                          (f" in {domain}." if domain else "."))


class GetState:
    name = "home_assistant_get_state"
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = False

    def __init__(self, ctx: HAContext) -> None:
        self.ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name,
                "description": "Liest den Zustand eines Geraets (Name oder entity_id).",
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "Name oder entity_id"}},
                    "required": ["name"]}}

    async def run(self, arguments: dict) -> ToolResult:
        name = arguments.get("name", "")
        m = await self.ctx.resolve(name)
        if not m:
            return _not_found(name)
        if len(m) > 1:
            return _ambiguous(m)
        e = m[0]
        return ToolResult(True, data=e,
                          human_message=f"{e['friendly_name']} ist {e['state']}.")


class _OnOff:
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = False
    _service = "turn_on"
    _word = "an"

    def __init__(self, ctx: HAContext) -> None:
        self.ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name,
                "description": f"Schaltet ein Geraet (Licht/Schalter/o.ae.) {self._word}.",
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "Name oder entity_id"}},
                    "required": ["name"]}}

    async def run(self, arguments: dict) -> ToolResult:
        name = arguments.get("name", "")
        m = await self.ctx.resolve(name, domains=SWITCHABLE)
        if not m:
            return _not_found(name)
        if len(m) > 1:
            return _ambiguous(m)
        e = m[0]
        await self.ctx.ha.call_service("homeassistant", self._service,
                                       {"entity_id": e["entity_id"]})
        return ToolResult(True, data={"entity_id": e["entity_id"]},
                          human_message=f"{e['friendly_name']} ist jetzt {self._word}.")


class TurnOn(_OnOff):
    name = "home_assistant_turn_on"
    _service = "turn_on"
    _word = "an"


class TurnOff(_OnOff):
    name = "home_assistant_turn_off"
    _service = "turn_off"
    _word = "aus"


class SetBrightness:
    name = "home_assistant_set_brightness"
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = False

    def __init__(self, ctx: HAContext) -> None:
        self.ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name,
                "description": "Setzt die Helligkeit einer Lampe in Prozent (0-100).",
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "Name oder entity_id der Lampe"},
                    "brightness_pct": {"type": "integer", "minimum": 0, "maximum": 100}},
                    "required": ["name", "brightness_pct"]}}

    async def run(self, arguments: dict) -> ToolResult:
        name = arguments.get("name", "")
        try:
            pct = int(arguments.get("brightness_pct"))
        except (TypeError, ValueError):
            return ToolResult(False, error="invalid_brightness",
                              human_message="Ich brauche eine Helligkeit zwischen 0 und 100.")
        pct = max(0, min(100, pct))
        m = await self.ctx.resolve(name, domains=("light",))
        if not m:
            return _not_found(name)
        if len(m) > 1:
            return _ambiguous(m)
        e = m[0]
        await self.ctx.ha.call_service("light", "turn_on",
                                       {"entity_id": e["entity_id"], "brightness_pct": pct})
        return ToolResult(True, data={"entity_id": e["entity_id"], "brightness_pct": pct},
                          human_message=f"{e['friendly_name']} steht jetzt auf {pct} Prozent.")


def ha_tools(ctx: HAContext) -> list:
    return [ListEntities(ctx), GetState(ctx), TurnOn(ctx), TurnOff(ctx), SetBrightness(ctx)]
