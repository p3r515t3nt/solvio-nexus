"""Die Inventare aus echten Core-Fakten bauen — und aus nichts sonst.

Der Reiz beim Zusammenstellen einer Geraeteliste ist, sie voll aussehen zu
lassen. Ein Raspberry Pi „ist ja offensichtlich" ein ARM64-Debian, ein Mac „hat
ja" Homebrew. Beides ist geraten, und geratene Inventarfakten sind schlimmer als
fehlende: sie werden nicht mehr hinterfragt, und der darauf aufgebaute Rat ist
selbstbewusst falsch.

Deshalb steht hier eine unbequeme Wahrheit im Code: **ueber den Pi weiss der Core
genau eine Sache — seine Kennung.** Der Handschlag traegt `protocol_version`,
`satellite_id`, `client_nonce`, `auth`. Kein Betriebssystem, keine Architektur,
keine Adresse, die irgendwo bliebe. Wer daraus ein Paket ableiten will, muss
vorher nachsehen; und genau dazu zwingt ein fehlendes Attribut.
"""
from __future__ import annotations

import os
import platform
from typing import Any

from solvio.logging_setup import get_logger
from solvio.resolver.inventory import CapabilityInventory, RuntimeFact, RuntimeInventory

log = get_logger("resolver")


def build_runtime_inventory(settings: Any = None, dispatcher: Any = None) -> RuntimeInventory:
    """Die kleine, wahrheitsgemaesse Liste. Was fehlt, fehlt sichtbar."""
    inventory = RuntimeInventory()

    # Der Mac, auf dem das hier laeuft. Das Einzige, was wirklich gemessen ist.
    inventory.add(RuntimeFact(
        key="mac-core", kind="host",
        attributes={"os": platform.system(), "architektur": platform.machine(),
                    "os_version": platform.release(),
                    "rolle": "SOLVIO Core, Sprachpfad, Freigabe-Gateway"},
        reachable=True, source="lokal gemessen (platform)",
        aliases=("mac", "macbook", "mac mini", "rechner", "core")))

    # Der Satellit. Hier steht bewusst KEIN os und KEINE architektur: der Core
    # kennt beides nicht, und ein Platzhalter waere eine Behauptung.
    for satellite_id in _satellite_ids():
        inventory.add(RuntimeFact(
            key=satellite_id, kind="satellite",
            attributes={"rolle": "Sprach-Satellit (Mikrofon/Lautsprecher)",
                        "verbindung": "WebSocket zum Core, HMAC-authentifiziert"},
            # Ob er GERADE verbunden ist, weiss nur eine laufende Sitzung.
            reachable=_satellite_connected(dispatcher, satellite_id),
            source="~/.solvio/satellite_auth.json (nur Kennung und Geheimnis)",
            aliases=_satellite_aliases(satellite_id)))

    if settings is not None and getattr(settings, "has_home_assistant", False):
        inventory.add(RuntimeFact(
            key="home-assistant", kind="service",
            attributes={"rolle": "Smart Home",
                        "adresse": _host_only(getattr(settings, "home_assistant_url", ""))},
            reachable=None, source="Konfiguration (nicht angefragt)",
            aliases=("home assistant", "homeassistant", "smart home", "hausautomation")))

    deep = getattr(dispatcher, "deep_runtime", None) or _deep_service(dispatcher)
    inventory.add(RuntimeFact(
        key="hermes", kind="runtime",
        attributes={"rolle": "tiefe Recherche, isoliert (Seatbelt)",
                    "werkzeuge": "web"},
        reachable=bool(deep) if deep is not None else None,
        source="Deep-Runtime-Anbindung im Core", aliases=("recherche", "research")))

    portal_socket = "/var/solvio-portal/run/portal.sock"
    inventory.add(RuntimeFact(
        key="portal-worker", kind="runtime",
        attributes={"rolle": "angemeldete Portale, eigener Unix-Nutzer",
                    "socket": portal_socket},
        reachable=os.path.exists(portal_socket),
        source="Existenz des Sockets", aliases=("portal", "portale")))

    inventory.add(RuntimeFact(
        key="browser", kind="runtime",
        attributes={"rolle": "oeffentliches Web, nur lesend",
                    "profil": "Wegwerfprofil je Sitzung"},
        reachable=_browser_available(),
        source="Sondierung der Chrome-Pfade",
        aliases=("webseite", "website", "surfen")))
    return inventory


def _satellite_aliases(satellite_id: str) -> tuple[str, ...]:
    """Wie ein Mensch diesen Satelliten nennt.

    Der Core kennt nur die Kennung `pi-wohnzimmer`. So sagt das niemand. Ohne
    diese Uebersetzung findet „auf meinem Raspberry Pi" das Geraet nicht — und
    der Vorschlag landet dann irgendwo.
    """
    names: list[str] = [satellite_id]
    low = satellite_id.lower()
    if low.startswith("pi-") or low == "pi" or "-pi" in low:
        names += ["raspberry pi", "raspberry", "raspi", "der pi", "dem pi",
                  "meinem pi", "mein pi"]
    room = low.split("-", 1)[1] if "-" in low else ""
    if len(room) > 3:
        names.append(room)
    return tuple(dict.fromkeys(names))


def _satellite_ids() -> list[str]:
    """Die eingetragenen Satelliten — Kennungen, nie Geheimnisse."""
    from solvio.realtime import satellite_auth
    try:
        credentials = satellite_auth.load_credentials()
    except Exception:  # noqa: BLE001 - keine Datei, kein Satellit
        return []
    return list(getattr(credentials, "satellite_ids", []) or [])


def _satellite_connected(dispatcher: Any, satellite_id: str) -> bool | None:
    """`None`, solange niemand nachgesehen hat — nicht `False`.

    Der Unterschied traegt: „nicht verbunden" waere ein Grund, einen anderen Weg
    zu suchen. „nicht nachgesehen" ist keiner.
    """
    server = getattr(dispatcher, "core_server", None)
    if server is None:
        return None
    session = getattr(server, "session", None)
    if session is None:
        return None
    return getattr(session, "satellite_id", "") == satellite_id


def _deep_service(dispatcher: Any) -> Any:
    for attribute in ("deep_service", "deep"):
        value = getattr(dispatcher, attribute, None)
        if value is not None:
            return value
    return None


def _browser_available() -> bool | None:
    try:
        from solvio.browser.cdp import chrome_binary
        return bool(chrome_binary())
    except Exception:  # noqa: BLE001
        return False


def _host_only(url: str) -> str:
    """Nur Schema und Host. Ein Token gehoert nicht in ein Inventar."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return f"{parts.scheme}://{parts.hostname}" if parts.hostname else ""


def build_capability_inventory(dispatcher: Any) -> CapabilityInventory:
    """Der Blick auf die eigenen Faehigkeiten, mit echten Verfuegbarkeits-Sonden.

    Die Sonden sind absichtlich duenn und ehrlich: sie beantworten „ist der
    Ausfuehrende ueberhaupt da", nicht „wird der naechste Aufruf gelingen". Was
    keine Sonde hat, bleibt `unbekannt` — und `unbekannt` zaehlt nicht als
    verfuegbar.
    """
    router = getattr(dispatcher, "capabilities", None)
    probes: dict[str, Any] = {
        "inline": lambda: True,
        "portal": lambda: os.path.exists("/var/solvio-portal/run/portal.sock"),
        "browser": lambda: _browser_available() is True,
        "hermes": lambda: _deep_service(dispatcher) is not None,
    }
    return CapabilityInventory(router, probes=probes,
                               descriptions=_tool_descriptions(dispatcher))


def _tool_descriptions(dispatcher: Any) -> dict[str, str]:
    """Die deutschen Werkzeugbeschreibungen, nach Faehigkeitsname.

    Sie stehen handgeschrieben in den Bruecken und sind damit Core-eigen — nicht
    vom Modell erfunden und nicht aus fremdem Inhalt gelesen.
    """
    try:
        return {entry["name"]: str(entry.get("description", ""))
                for entry in dispatcher.openai_tools()}
    except Exception:  # noqa: BLE001
        return {}


#: Wie lange auf eine Rechercheantwort gewartet wird, und wie oft nachgefragt.
#: `deep_research` ist asynchron: der erste Aufruf liefert eine Kennung und
#: `running`. Wer diese Antwort fuer das Ergebnis haelt, bekommt nie eines — und
#: merkt es nicht, weil „keine Recherche" wie „nichts gefunden" aussieht.
RESEARCH_WAIT = 240.0
RESEARCH_POLL = 5.0

#: Zustaende, bei denen nichts mehr kommt.
_FINAL = ("succeeded", "failed", "cancelled", "timed_out")


def build_researcher(dispatcher: Any):
    """Eine Funktion, die GENAU EINE Frage an Hermes stellt und die Antwort abholt.

    Bewusst eine Funktion und kein Objekt: der Resolver soll den tiefen
    Ausfuehrenden weder starten noch abbrechen noch verwalten koennen. Er darf
    fragen und warten. Mehr Schnittstelle waere mehr Macht als noetig.
    """
    router = getattr(dispatcher, "capabilities", None)
    if router is None or "deep_research" not in set(router.names()):
        return None

    def _context():
        from solvio.capabilities.contract import ArgumentSource
        from solvio.contracts.trust import TrustContext, TrustLevel
        # Die Frage stammt aus dem Core, nicht aus fremdem Inhalt — und sie ist
        # lesend. Autoritaet entsteht keine: `deep_research` ist HARMLESS und
        # READ_ONLY, hier gibt es nichts freizugeben.
        return (TrustContext(origin_trust=TrustLevel.SYSTEM_TRUSTED,
                             user_authorized=False,
                             note="resolver research question"),
                ArgumentSource.TRUSTED_CONTEXT)

    async def ask(question: str) -> dict:
        import asyncio
        import time as _time
        trust, source = _context()
        started = await router.execute(
            "deep_research", {"topic": question[:400]}, trust=trust,
            provenance={"topic": source}, principal="gap-resolver")
        if not started.succeeded:
            return {}
        data = started.data if isinstance(started.data, dict) else {}
        task_id = str(data.get("task_id", ""))
        status = str(data.get("status", ""))
        deadline = _time.monotonic() + RESEARCH_WAIT
        while task_id and status not in _FINAL and _time.monotonic() < deadline:
            await asyncio.sleep(RESEARCH_POLL)
            polled = await router.execute(
                "deep_task_status", {"task_id": task_id}, trust=trust,
                provenance={"task_id": source}, principal="gap-resolver")
            if not polled.succeeded:
                break
            data = polled.data if isinstance(polled.data, dict) else {}
            status = str(data.get("status", ""))
        if status != "succeeded":
            log.info("resolver.research_incomplete", status=status or "unknown")
            return {}
        outcome = data.get("ergebnis")
        return outcome if isinstance(outcome, dict) else data

    return ask


def build_team(dispatcher: Any, repo_root: str = ""):
    """Das Fachteam. Der Kundschafter nutzt denselben Hermes-Weg wie die Recherche.

    Bewusst dieselbe Funktion und nicht eine zweite Anbindung: eine zweite Bauart
    waere eine zweite Isolationsfrage, und die erste ist teuer genug erkauft.
    """
    from solvio.specialists.team import SpecialistTeam
    root = repo_root or os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    return SpecialistTeam(repo_root=os.path.dirname(root),
                          researcher=build_researcher(dispatcher))


def build_resolver(dispatcher: Any, settings: Any = None):
    """Setzt den Resolver aus echten Core-Teilen zusammen."""
    from solvio.resolver.resolver import GapResolver
    return GapResolver(
        capabilities=build_capability_inventory(dispatcher),
        runtimes=build_runtime_inventory(settings, dispatcher),
        researcher=build_researcher(dispatcher),
        team=build_team(dispatcher))
