"""Home Assistant als Faehigkeit — der erste echte Verbraucher des Vertrags.

Home Assistant bleibt Werkzeugschicht, nicht Gehirn: **SOLVIO entscheidet, HA
fuehrt aus.** Deshalb laeuft hier nichts an Trust, Risiko und Freigabe vorbei.

Zwei Entscheidungen tragen dieses Modul:

**Die Freigabeliste ist die Faehigkeitsgrenze.** HA weiss selbst, welche Geraete
einem Sprachassistenten gezeigt werden duerfen — der Besitzer pflegt das in HAs
eigener Oberflaeche (`homeassistant/expose_entity/list`, Assistent
`conversation`). Auf dieser Anlage sind das 15 von 119 Entities. SOLVIO erfindet
keine zweite Liste, sondern uebernimmt diese, und zwar an **beiden** Stellen:
beim Auffinden und noch einmal beim Ausfuehren. Ein Geraet, dessen `entity_id`
das Modell errraet, wird dadurch nicht ausfuehrbar.

**Namen und Raeume statt entity_id.** Das Modell nennt „Licht" und „Wohnzimmer",
nicht `light.wiz_rgbww_tunable_8b68a2`. Die Aufloesung auf eine `entity_id`
passiert danach, im vertrauenswuerdigen Code, gegen die Registry — die Area steht
naemlich nicht im Zustand, sondern nur dort (nachgemessen: 0 von 119 States
tragen ein `area`-Attribut).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from solvio.capabilities.contract import (
    AmbiguousExecution, CapabilityDeclined, CapabilityRefused, CapabilitySpec,
    ExecutionClass, ExecutorUnavailable,
)
from solvio.capabilities.router import CapabilityRouter
from solvio.integrations.home_assistant import HomeAssistant
from solvio.logging_setup import get_logger
from solvio.security.mobile_approval.execution import IDEMPOTENT_WRITE, READ_ONLY
from solvio.tools.base import RiskLevel

log = get_logger("ha")

#: Der Assistent, dessen Freigabeliste gilt. HA pflegt sie pro Assistent.
ASSISTANT = "conversation"

#: Domains, die SOLVIO in V1 schalten darf. Bewusst kurz: reversibel, idempotent,
#: ohne physische Sicherheitswirkung.
EXECUTABLE_DOMAINS: frozenset[str] = frozenset({"light", "switch", "input_boolean"})

#: Domains, die eine physische Sicherheitswirkung haben. Sie bleiben AUFFINDBAR
#: (SOLVIO darf sagen, dass die Haustuer verschlossen ist), aber NICHT ausfuehrbar.
#: Sie stillschweigend als harmlos zu fuehren, weil HA einen Service-Endpunkt
#: anbietet, waere die gefaehrlichste Art von Bequemlichkeit.
SECURITY_SENSITIVE_DOMAINS: frozenset[str] = frozenset({
    "lock", "alarm_control_panel", "cover", "valve", "siren",
})

#: Geraeteklassen, die auch innerhalb einer harmlosen Domain Sicherheitswirkung haben.
SECURITY_SENSITIVE_DEVICE_CLASSES: frozenset[str] = frozenset({
    "garage", "gate", "door", "lock", "shutter", "awning",
})

#: Geraete, die der BESITZER ausdruecklich als sicherheitsrelevant benennt —
#: unabhaengig davon, was Home Assistant ueber sie sagt.
#:
#: Sie schliesst die eine Luecke, die sich aus HA-Metadaten allein nicht
#: schliessen laesst: ein Garagenrelais, das als nackter `switch` ohne
#: `device_class` exponiert ist, sieht fuer jeden Klassifizierer aus wie eine
#: Steckdose. Kein Wortabgleich rettet das — „Tor" kann eine Lampe heissen und
#: „Flurlicht" ein Tor. Was hilft, ist eine ausdrueckliche Angabe des Menschen,
#: der sein Haus kennt.
#:
#: Core-eigen, aus der Konfiguration, nie aus einer Modellausgabe oder einer
#: Selbstauskunft von Home Assistant.
SECURITY_OVERRIDE_ENTITIES: frozenset[str] = frozenset()

#: ZUTRITT. Geraete, hinter denen ein Mensch ins Haus kommt oder eine
#: Sicherungsanlage nachgibt. Hier gilt die HA_SECURITY-Zeile der Matrix.
ACCESS_DOMAINS: frozenset[str] = frozenset({
    "lock", "alarm_control_panel", "siren", "valve",
})
ACCESS_DEVICE_CLASSES: frozenset[str] = frozenset({
    "garage", "gate", "door", "lock",
})

#: KOMFORT. Was das Wohnen angenehm macht und keine Sicherungsfunktion hat.
#: Der Auftrag nennt sie ausdruecklich: Licht, Rollladen, Steckdosen, Fernseher,
#: Heizung, gewoehnliche Szenen.
#:
#: Die Trennung entstand am ECHTEN Haus. Die erste Fassung fuehrte die ganze
#: Domaene `cover` als sicherheitsrelevant — geerbt von der aelteren Frage
#: „darf SOLVIO das ueberhaupt schalten", wo Vorsicht nichts kostete. Als
#: Klasse fuer die Freigabepolitik ist sie falsch: in der Freigabeliste dieses
#: Hauses stehen genau zwei `cover`, ein Vorhang und ein Schlafzimmer-Rollladen,
#: und fuer die jedes Mal Face ID zu verlangen waere Reibung ohne Gegenwert.
#:
#: Ein Garagentor traegt `device_class: garage`, ein Tor `gate`, eine Tuer
#: `door` — die drei stehen oben und werden zuerst geprueft. Bleibt ein `cover`
#: ganz ohne Angabe, gilt es als Komfort; wer ein Zutrittsgeraet ohne
#: `device_class` exponiert, benennt es in `home_assistant_security_entities`.
#: Das ist die ehrliche Grenze dieser Klassifikation und steht so auch im
#: Threat Model.
COMFORT_DOMAINS: frozenset[str] = frozenset({
    "light", "switch", "input_boolean", "cover", "media_player", "climate",
    "fan", "humidifier",
})

_CACHE_SECONDS = 60.0


@dataclass(frozen=True)
class ExposedEntity:
    """Ein Geraet, das der Besitzer dem Sprachassistenten gezeigt hat."""
    entity_id: str
    name: str
    domain: str
    area: str
    state: str
    device_class: str = ""
    attributes: dict[str, Any] | None = None

    def action_class(self, overrides: frozenset[str] = frozenset()):
        """Die Aktionsklasse DIESES Geraets — am Geraet gemessen, nicht am Wort.

        Wer die Haustuer „Flurlicht" nennt, aendert damit ihre `device_class`
        nicht. Deshalb entscheidet hier ausschliesslich, was Home Assistant und
        der Besitzer ueber das Geraet sagen, und nie, wie es im Satz hiess.

        Fail-closed in der letzten Zeile: eine exponierte Entitaet in einer
        Domaene, die SOLVIO nicht als gewoehnliche Haustechnik kennt, wird
        NICHT stillschweigend gewoehnlich. Sie wird unbekannt — und Unbekanntes
        kostet eine Freigabe.
        """
        from solvio.capabilities.policy import ActionClass
        if self.entity_id in overrides:
            return ActionClass.HA_SECURITY
        if self.domain in ACCESS_DOMAINS:
            return ActionClass.HA_SECURITY
        if self.device_class in ACCESS_DEVICE_CLASSES:
            return ActionClass.HA_SECURITY
        if self.domain in COMFORT_DOMAINS:
            return ActionClass.HA_NORMAL
        return ActionClass.UNCLASSIFIED

    @property
    def executable(self) -> bool:
        """Darf SOLVIO das schalten? Fail-closed in beide Richtungen."""
        if self.domain in SECURITY_SENSITIVE_DOMAINS:
            return False
        if self.device_class in SECURITY_SENSITIVE_DEVICE_CLASSES:
            return False
        return self.domain in EXECUTABLE_DOMAINS


def _norm(text: str) -> str:
    return "".join((text or "").lower().split())


class HAExposure:
    """Laedt und haelt die Freigabegrenze. Bei jedem Zweifel: leer.

    Die Liste ist kurzlebig zwischengespeichert, damit ein Sprach-Turn nicht drei
    Registries neu zieht. Faellt HA aus, wird der Zwischenstand NICHT weiter
    benutzt: eine veraltete Freigabeliste ist eine Behauptung ueber einen Zustand,
    den wir nicht mehr kennen.
    """

    def __init__(self, ha: HomeAssistant, ttl: float = _CACHE_SECONDS) -> None:
        self.ha = ha
        self.ttl = ttl
        self._entities: dict[str, ExposedEntity] = {}
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()
        self.last_error = ""

    async def boundary(self, *, force: bool = False) -> dict[str, ExposedEntity]:
        """Die Freigabegrenze samt Name/Raum/Art — OHNE Zustand.

        Nur das darf zwischengespeichert werden. Namen und Raeume aendern sich
        selten; ein Zustand aendert sich staendig, und ein zwischengespeicherter
        Zustand ist keine Auskunft, sondern eine Erinnerung.
        """
        async with self._lock:
            fresh = (time.monotonic() - self._loaded_at) < self.ttl
            if self._entities and fresh and not force:
                return dict(self._entities)
            try:
                loaded = await self._load()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._entities = {}
                self._loaded_at = 0.0
                log.error("ha.exposure_unavailable", kind=type(exc).__name__)
                raise ExecutorUnavailable("home assistant exposure unavailable") from exc
            self.last_error = ""
            self._entities = loaded
            self._loaded_at = time.monotonic()
            return dict(loaded)

    async def _load(self) -> dict[str, ExposedEntity]:
        replies = await self.ha.ws_commands([
            "homeassistant/expose_entity/list",
            "config/entity_registry/list",
            "config/device_registry/list",
            "config/area_registry/list",
        ])
        exposure = replies.get("homeassistant/expose_entity/list")
        if not exposure:
            # Ohne Freigabeliste gibt es keine Grenze — und ohne Grenze nichts.
            raise ExecutorUnavailable("home assistant did not return the exposure list")
        raw = exposure.get("exposed_entities", exposure) if isinstance(exposure, dict) else {}
        allowed = {eid for eid, flags in (raw or {}).items()
                   if isinstance(flags, dict) and flags.get(ASSISTANT)}

        registry = {e["entity_id"]: e for e in (replies.get("config/entity_registry/list") or [])}
        device_area = {d["id"]: d.get("area_id")
                       for d in (replies.get("config/device_registry/list") or [])}
        area_name = {a["area_id"]: a.get("name", "")
                     for a in (replies.get("config/area_registry/list") or [])}

        out: dict[str, ExposedEntity] = {}
        for state in await self.ha.states():
            entity_id = state.get("entity_id", "")
            if entity_id not in allowed:
                continue
            attrs = state.get("attributes") or {}
            entry = registry.get(entity_id, {})
            area_id = entry.get("area_id") or device_area.get(entry.get("device_id"))
            friendly = (attrs.get("friendly_name") or entry.get("name")
                        or entry.get("original_name") or entity_id)
            out[entity_id] = ExposedEntity(
                entity_id=entity_id, name=friendly,
                domain=entity_id.split(".")[0] if "." in entity_id else "",
                area=area_name.get(area_id, "") or "",
                state="",           # bewusst leer: Zustand kommt IMMER frisch
                device_class=str(attrs.get("device_class") or ""),
                attributes=None)
        return out

    async def snapshot(self) -> dict[str, ExposedEntity]:
        """Die Grenze mit FRISCHEN Zustaenden — fuer jede Auskunft nach aussen."""
        border = await self.boundary()
        try:
            states = {st.get("entity_id", ""): st for st in await self.ha.states()}
        except Exception as exc:  # noqa: BLE001
            raise ExecutorUnavailable("home assistant state read failed") from exc
        out: dict[str, ExposedEntity] = {}
        for entity_id, meta in border.items():
            st = states.get(entity_id) or {}
            attrs = st.get("attributes") or {}
            out[entity_id] = ExposedEntity(
                entity_id=entity_id, name=meta.name, domain=meta.domain, area=meta.area,
                state=st.get("state", "unknown"), device_class=meta.device_class,
                attributes=attrs)
        return out

    async def live_state(self, entity_id: str) -> str:
        """Der Zustand EINES Geraets, frisch gelesen. Fuer die Rueckpruefung."""
        try:
            st = await self.ha.state(entity_id)
        except Exception:  # noqa: BLE001
            return "unknown"
        return st.get("state", "unknown")

    async def is_exposed(self, entity_id: str) -> bool:
        """Die zweite Durchsetzung: auch beim Ausfuehren wird erneut geprueft."""
        return entity_id in (await self.boundary())

    async def resolve(self, name: str, area: str = "",
                      domains: frozenset[str] | None = None) -> list[ExposedEntity]:
        """Findet Geraete NUR innerhalb der Freigabegrenze.

        Reihenfolge von streng nach tolerant. Ein Raum grenzt zusaetzlich ein — er
        weitet nie aus. Was nicht freigegeben ist, existiert hier nicht, auch wenn
        die `entity_id` exakt genannt wird.
        """
        pool = list((await self.boundary()).values())
        if domains:
            pool = [e for e in pool if e.domain in domains]
        if area:
            wanted = _norm(area)
            in_area = [e for e in pool if _norm(e.area) == wanted]
            if not in_area:
                in_area = [e for e in pool if wanted and wanted in _norm(e.area)]
            pool = in_area
        target = _norm(name)
        if not target:
            return pool if area else []
        exact_id = [e for e in pool if e.entity_id.lower() == (name or "").strip().lower()]
        if exact_id:
            return exact_id
        exact = [e for e in pool if _norm(e.name) == target]
        if exact:
            return exact
        partial = [e for e in pool if target in _norm(e.name)]
        if partial:
            return partial
        words = [w for w in (name or "").lower().split() if w]
        if words:
            return [e for e in pool if all(w in e.name.lower() for w in words)]
        return []


# =====================================================================
# Die Faehigkeiten
# =====================================================================

_TARGET_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "area": {"type": "string"},
    },
    "required": ["name"],
}

SPECS: dict[str, CapabilitySpec] = {
    "ha_list_devices": CapabilitySpec(
        name="ha_list_devices", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object",
                      "properties": {"area": {"type": "string"},
                                     "domain": {"type": "string"}}},
        description="Nennt die freigegebenen Geraete, optional nach Raum oder Art."),
    "ha_get_state": CapabilitySpec(
        name="ha_get_state", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema=_TARGET_SCHEMA,
        description="Liest den Zustand oder Messwert eines freigegebenen Geraets."),
    # Licht an/aus und Helligkeit sind in SOLVIOs eigener Risikoskala ausdruecklich
    # HARMLESS ("Licht an/aus, Helligkeit, lesen -> direkt", tools/base.py). Sie
    # sind reversibel und idempotent: zweimal „aus" ist derselbe Zustand wie einmal.
    # Erst die Herkunft der Argumente hebt das Risiko — dann greift die Freigabe.
    "ha_turn_on": CapabilitySpec(
        name="ha_turn_on", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.HARMLESS, semantics=IDEMPOTENT_WRITE,
        input_schema=_TARGET_SCHEMA, description="Schaltet ein freigegebenes Geraet ein."),
    "ha_turn_off": CapabilitySpec(
        name="ha_turn_off", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.HARMLESS, semantics=IDEMPOTENT_WRITE,
        input_schema=_TARGET_SCHEMA, description="Schaltet ein freigegebenes Geraet aus."),
    "ha_set_brightness": CapabilitySpec(
        name="ha_set_brightness", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.HARMLESS, semantics=IDEMPOTENT_WRITE,
        input_schema={"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "area": {"type": "string"},
                                     "brightness_pct": {"type": "integer"}},
                      "required": ["name", "brightness_pct"]},
        description="Setzt die Helligkeit einer freigegebenen Lampe in Prozent."),
}


def _not_found(name: str) -> CapabilityDeclined:
    """Auch der haeufigste Fall: das Geraet ist da, aber nicht freigegeben.

    Beides klingt fuer den Nutzer gleich, und das ist Absicht — SOLVIO verraet nicht,
    welche Geraete es gibt, wenn der Besitzer sie dem Assistenten nicht gezeigt hat.
    """
    return CapabilityDeclined(
        "target_not_found",
        f"Ich habe kein freigegebenes Geraet, das zu '{name}' passt.")


def _ambiguous(options: list[ExposedEntity]) -> CapabilityDeclined:
    names = ", ".join(f"{e.name} ({e.area})" if e.area else e.name for e in options[:6])
    return CapabilityDeclined(
        "target_ambiguous",
        f"Da passen mehrere: {names}. Welches meinst du?",
        data={"options": [{"name": e.name, "area": e.area} for e in options[:6]]})


def _not_executable(entity: ExposedEntity) -> CapabilityRefused:
    return CapabilityRefused(
        "not_executable",
        f"{entity.name} kann ich dir anzeigen, aber nicht schalten.",
        data={"name": entity.name, "domain": entity.domain})


class HACapabilities:
    """Die Handler. Jeder bekommt nur Argumente — Autoritaet kommt vom Router."""

    def __init__(self, exposure: HAExposure,
                 security_entities: frozenset[str] = SECURITY_OVERRIDE_ENTITIES) -> None:
        self.exposure = exposure
        self.security_entities = frozenset(security_entities or ())

    async def classify(self, arguments: dict[str, Any]):
        """Welche Klasse hat DIESER Aufruf? Vor jeder Freigabeentscheidung.

        Loest dasselbe Ziel auf wie der Handler danach — mit derselben
        Funktion, nicht mit einer zweiten Fassung davon. Eine Aufloesung, die
        hier anders ausfiele als dort, waere genau die Luecke, die diese
        Klassifikation schliessen soll.

        Ein mehrdeutiges oder unbekanntes Ziel wird hier zur Rueckfrage. Das ist
        ein Fortschritt: bisher fiel sie erst NACH der Freigabe auf.
        """
        from solvio.capabilities.policy import ActionClass, Classification
        entity = await self._one(arguments)
        klass = entity.action_class(self.security_entities)
        if klass is not ActionClass.HA_NORMAL:
            # DIESE drei Faehigkeiten sind gewoehnliche Haustechnik und sonst
            # nichts. Ein Schloss, ein Tor, eine Alarmanlage gehoeren in eigene,
            # ehrlich benannte Aktionen mit eigenen Freigabetexten — „Geraet
            # einschalten: Haustuer" waere ein Satz, den niemand bewusst
            # bestaetigen kann.
            #
            # Die Absage faellt hier und nicht im Handler, also VOR jeder
            # Freigabefrage. Sonst haette der Mensch mit Face ID etwas
            # bestaetigt, das gleich danach ohnehin verweigert wird — und genau
            # dieses Muster gewoehnt einem das Hinsehen ab.
            raise _not_executable(entity)
        return Classification(klass, targets=(entity.entity_id,),
                              reason=f"ha:{entity.domain}:{entity.device_class or '-'}")

    async def _one(self, arguments: dict[str, Any],
                   domains: frozenset[str] | None = None) -> ExposedEntity:
        matches = await self.exposure.resolve(
            str(arguments.get("name", "")), str(arguments.get("area", "") or ""), domains)
        if not matches:
            raise _not_found(str(arguments.get("name", "")))
        if len(matches) > 1:
            raise _ambiguous(matches)
        return matches[0]

    async def list_devices(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entities = list((await self.exposure.snapshot()).values())
        area = str(arguments.get("area", "") or "")
        domain = str(arguments.get("domain", "") or "")
        if area:
            entities = [e for e in entities if _norm(area) in _norm(e.area)]
        if domain:
            entities = [e for e in entities if e.domain == domain]
        return {"count": len(entities),
                "devices": [{"name": e.name, "area": e.area, "domain": e.domain,
                             "state": e.state, "controllable": e.executable}
                            for e in sorted(entities, key=lambda e: (e.area, e.name))]}

    async def get_state(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity = await self._one(arguments)
        # Frisch nachlesen: die Grenze darf aus dem Zwischenspeicher kommen, die
        # Auskunft ueber den Zustand niemals.
        live = (await self.exposure.snapshot()).get(entity.entity_id)
        entity = live or entity
        out: dict[str, Any] = {"name": entity.name, "area": entity.area,
                               "domain": entity.domain, "state": entity.state}
        attrs = entity.attributes or {}
        for key in ("current_temperature", "temperature", "humidity", "brightness",
                    "unit_of_measurement", "media_title"):
            if key in attrs and attrs[key] is not None:
                out[key] = attrs[key]
        return out

    async def _switch(self, arguments: dict[str, Any], service: str) -> dict[str, Any]:
        entity = await self._one(arguments, domains=None)
        if not entity.executable:
            raise _not_executable(entity)
        # ZWEITE Durchsetzung der Grenze, unmittelbar vor der Wirkung. Zwischen
        # Auffinden und Ausfuehren kann der Besitzer die Freigabe entzogen haben.
        if not await self.exposure.is_exposed(entity.entity_id):
            raise _not_found(entity.name)
        await self._call(entity.domain, service, {"entity_id": entity.entity_id})
        expected = "on" if service == "turn_on" else "off"
        observed, confirmed = await self._verify(entity.entity_id, expected)
        return {"name": entity.name, "area": entity.area, "action": service,
                "state": observed, "confirmed": confirmed}

    async def turn_on(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._switch(arguments, "turn_on")

    async def turn_off(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._switch(arguments, "turn_off")

    async def set_brightness(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            pct = int(arguments.get("brightness_pct"))
        except (TypeError, ValueError):
            raise CapabilityDeclined(
                "invalid_brightness",
                "Ich brauche eine Helligkeit zwischen 0 und 100.") from None
        pct = max(0, min(100, pct))
        entity = await self._one(arguments, domains=frozenset({"light"}))
        if not entity.executable:
            raise _not_executable(entity)
        if not await self.exposure.is_exposed(entity.entity_id):
            raise _not_found(entity.name)
        await self._call("light", "turn_on",
                         {"entity_id": entity.entity_id, "brightness_pct": pct})
        observed, confirmed = await self._verify(entity.entity_id, "on")
        return {"name": entity.name, "area": entity.area, "brightness_pct": pct,
                "state": observed, "confirmed": confirmed}

    async def _verify(self, entity_id: str, expected: str) -> tuple[str, bool]:
        """Liest nach, ob die Wirkung wirklich eingetreten ist.

        HA nimmt einen Service-Aufruf an, ohne dass das Geraet ihn ausgefuehrt haben
        muss — die Antwort auf `call_service` war bei einem echten Schalter dieser
        Anlage schlicht `[]`. „Angenommen" ist deshalb kein Beleg fuer „passiert".
        Zwei kurze Versuche; danach wird der beobachtete Zustand berichtet, wie er
        ist, statt Erfolg zu behaupten.
        """
        for delay in (0.25, 0.6):
            await asyncio.sleep(delay)
            observed = await self.exposure.live_state(entity_id)
            if observed == expected:
                return observed, True
        return observed, False

    async def _call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        """Ruft HA und uebersetzt Netzfehler in die Sprache des Vertrags."""
        try:
            await self.exposure.ha.call_service(domain, service, data)
        except asyncio.TimeoutError as exc:
            # Die Anfrage ging raus, die Antwort kam nicht: der Ausgang ist
            # unbekannt. Bei idempotenten Schaltvorgaengen darf das wiederholt
            # werden — das entscheidet der Vertrag, nicht dieses Modul.
            raise AmbiguousExecution("home assistant did not answer in time") from exc
        except Exception as exc:  # noqa: BLE001
            raise ExecutorUnavailable(f"home assistant call failed: {type(exc).__name__}") from exc


def register(router: CapabilityRouter, capabilities: HACapabilities) -> list[str]:
    """Haengt die HA-Faehigkeiten in den Router. Gibt die Namen zurueck."""
    handlers = {
        "ha_list_devices": capabilities.list_devices,
        "ha_get_state": capabilities.get_state,
        "ha_turn_on": capabilities.turn_on,
        "ha_turn_off": capabilities.turn_off,
        "ha_set_brightness": capabilities.set_brightness,
    }
    # Nur die schaltenden Faehigkeiten brauchen den Verfeinerer: bei den
    # lesenden faellt die Klasse ohnehin aus der Spec (`READ_ONLY`), und eine
    # zweite Aufloesung waere dort nur eine Anfrage mehr an Home Assistant.
    refines = {"ha_turn_on", "ha_turn_off", "ha_set_brightness"}
    for name, handler in handlers.items():
        router.register(SPECS[name], handler,
                        classify=capabilities.classify if name in refines else None)
    return sorted(handlers)
