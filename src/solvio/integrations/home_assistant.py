"""Home-Assistant-REST-Client (Schritt 17), offizielle REST-API.

Auth: Bearer Long-Lived Access Token. Endpunkte: /api/ (Status), /api/states,
/api/states/<entity_id>, /api/services, POST /api/services/<domain>/<service>.
Der Token wird NIE geloggt.

**Woher der Token kommt.** Seit dem Geheimnistresor bevorzugt aus ihm:
`secret://home-assistant/core`, geholt ueber `SecretBroker.use()` genau in dem
Moment, in dem eine Kopfzeile gebraucht wird, und nur fuer diese eine Anfrage.
Der Klartext lebt damit fuer die Dauer eines Aufrufs statt fuer die Lebensdauer
des Prozesses. Ein Wert aus `.env` bleibt als Rueckfall stehen, solange die
Wanderung laeuft — danach nicht mehr.

**Die WS-Kommandoliste ist geschlossen (DEBT-0108).** Home Assistant liefert
ueber `backup/config/info` sein Sicherungspasswort im Klartext an jeden Aufrufer
mit gueltigem Token. SOLVIO hat dieses Kommando nie aufgerufen — gemessen, nicht
angenommen: `git log -S "backup/config/info" -- src/` ist leer, und die einzigen
zwei Aufrufer von `ws_commands` uebergeben feste Listen. Aber `ws_commands` nahm
BELIEBIGE Kommandostrings entgegen, und damit haette eine einzige kuenftige
Zeile — ein durchgereichtes Argument, ein neuer Aufrufer — den Weg dorthin
geoeffnet. Die Liste unten macht aus „tut es nicht" ein „kann es nicht".
"""
from __future__ import annotations

import contextlib
from typing import Any

import aiohttp

from solvio.logging_setup import get_logger

log = get_logger("ha")

#: Der Verweis auf den Tresor-Eintrag. Ein Name, kein Wert.
CREDENTIAL_REF = "secret://home-assistant/core"

#: Die WebSocket-Kommandos, die SOLVIO ueberhaupt senden darf. GESCHLOSSEN.
#:
#: Genau die fuenf, die heute benutzt werden: vier Registries fuer die
#: Freigabeliste und `backup/info` fuer den Sicherungsbestand. Ein sechstes
#: Kommando kostet hier eine Zeile und damit ein Review — und `backup/config/info`
#: bekommt diese Zeile nicht (DEBT-0108).
ALLOWED_WS_COMMANDS: frozenset[str] = frozenset({
    "homeassistant/expose_entity/list",
    "config/entity_registry/list",
    "config/device_registry/list",
    "config/area_registry/list",
    "backup/info",
})

#: Kommandos, die ausdruecklich verboten sind, obwohl die Liste oben ohnehin
#: abschliessend ist. Sie stehen hier, damit ein Test sie NAMENTLICH pruefen
#: kann: eine Zusicherung gegen eine leere Menge sichert nichts, eine gegen
#: einen benannten Angriffsweg schon.
DENIED_WS_COMMANDS: frozenset[str] = frozenset({
    "backup/config/info",
    "backup/config/update",
    "auth/long_lived_access_token",
    "config/auth_provider/homeassistant/admin_change_password",
})


class HomeAssistantCommandRefused(PermissionError):
    """Dieses WS-Kommando steht nicht auf der Liste. Traegt nie eine Antwort."""


class ExecutorTokenMissing(RuntimeError):
    """Es gibt kein Zugangsdatum fuer Home Assistant. Traegt nie einen Wert."""


class HomeAssistant:
    #: Der Verweis auf den Tresor-Eintrag — als KLASSENATTRIBUT.
    #:
    #: Gefunden beim ersten echten Start, nicht von einem Test: die Konstante
    #: stand nur auf Modulebene, waehrend `registry.py`, `probes.py` und
    #: `storage/job.py` sie ueber die Klasse ansprechen — so wie bei Gmail und
    #: Kalender, wo sie im Klassenrumpf steht. Der Core lief damit in eine
    #: Absturzschleife, und keine der 2283 Zusicherungen hat es bemerkt, weil
    #: keine `build_dispatcher` mit echtem Home Assistant durchlaeuft.
    CREDENTIAL_REF = CREDENTIAL_REF

    def __init__(self, url: str, token: str = "", timeout: float = 6.0, *,
                 broker: Any = None, credential_ref: str = CREDENTIAL_REF) -> None:
        self.base = url.rstrip("/")
        self._token = token
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._broker = broker
        self._credential_ref = credential_ref

    @property
    def uses_vault(self) -> bool:
        return self._broker is not None and bool(self._credential_ref)

    def _headers_with(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    @property
    def _headers(self) -> dict[str, str]:
        """Der Rueckfall aus der Konfiguration. Waehrend der Wanderung noch da.

        Nach abgeschlossener Wanderung ist `self._token` leer, und dann ist
        dieser Weg eine Sackgasse statt eines stillen Zugriffs mit einem
        Zugangsdatum, das dort nicht mehr stehen sollte.
        """
        return self._headers_with(self._token)

    @contextlib.contextmanager
    def _auth(self):
        """Die Kopfzeile fuer GENAU EINE Anfrage.

        Der Wert kommt aus dem Tresor und lebt bis zum Ende des Blocks. Steht
        kein Tresor bereit, gilt der Rueckfall aus der Konfiguration — und wenn
        auch der leer ist, faellt der Aufruf aus, statt unauthentifiziert
        hinauszugehen.

        Der `use()`-Aufruf steht ABSICHTLICH hier und nicht in einem Helfer: der
        Tresor prueft den Modulnamen des Aufrufers gegen den behaupteten
        Executor. Ein gemeinsamer Helfer in einem anderen Modul wuerde genau
        diese Bindung aufheben.
        """
        if self.uses_vault:
            from solvio.secret_vault.policy import ExecutorId
            with self._broker.use(self._credential_ref,
                                  executor=ExecutorId.HOME_ASSISTANT,
                                  target=self.base) as material:
                yield self._headers_with(material.plaintext())
            return
        if not self._token:
            raise ExecutorTokenMissing("no home assistant credential available")
        yield self._headers

    async def _get(self, path: str) -> Any:
        with self._auth() as headers:
            async with aiohttp.ClientSession(timeout=self.timeout) as s:
                async with s.get(self.base + path, headers=headers) as r:
                    r.raise_for_status()
                    return await r.json()

    async def api_ok(self) -> bool:
        try:
            with self._auth() as headers:
                async with aiohttp.ClientSession(timeout=self.timeout) as s:
                    async with s.get(self.base + "/api/", headers=headers) as r:
                        return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    async def states(self) -> list[dict]:
        return await self._get("/api/states")

    async def state(self, entity_id: str) -> dict:
        return await self._get("/api/states/" + entity_id)

    async def services(self) -> list[dict]:
        return await self._get("/api/services")

    async def call_service(self, domain: str, service: str, data: dict | None = None) -> Any:
        with self._auth() as headers:
            async with aiohttp.ClientSession(timeout=self.timeout) as s:
                async with s.post(f"{self.base}/api/services/{domain}/{service}",
                                  headers=headers, json=data or {}) as r:
                    r.raise_for_status()
                    return await r.json()

    # -- WebSocket ----------------------------------------------------------
    # Areas und die Freigabeliste stehen NICHT in der REST-API: `/api/states`
    # liefert kein `area`-Attribut (nachgemessen: 0 von 119 Entities), weil Areas
    # in der Registry leben. Beides gibt es nur ueber die WS-API — dieselben
    # Zugangsdaten, kein zweiter Token, keine zweite Konfiguration.

    @property
    def ws_url(self) -> str:
        if self.base.startswith("https://"):
            return "wss://" + self.base[len("https://"):] + "/api/websocket"
        return "ws://" + self.base[len("http://"):] + "/api/websocket"

    async def ws_commands(self, types: list[str]) -> dict[str, Any]:
        """Fuehrt mehrere WS-Kommandos in EINER Verbindung aus.

        Gebuendelt, weil sonst jedes Kommando einen eigenen Handshake samt
        Authentifizierung kostet — bei drei Registries je Auffrischung dreimal
        derselbe Aufbau. Ergebnis: {kommando: result}; ein einzeln fehlgeschlagenes
        Kommando fehlt im Ergebnis, statt die uebrigen mitzureissen.
        """
        unknown = [c for c in types if c not in ALLOWED_WS_COMMANDS]
        if unknown:
            # VOR dem Verbindungsaufbau, damit ein verbotenes Kommando nicht
            # einmal eine authentifizierte Sitzung kostet. Gemeldet wird nur der
            # NAME des Kommandos — nie eine Antwort, nie ein Argument.
            log.warning("ha.ws_command_refused", commands=",".join(unknown)[:120])
            raise HomeAssistantCommandRefused(
                "websocket command is not on the allowlist")
        out: dict[str, Any] = {}
        with self._auth() as headers:
            token = headers["Authorization"].removeprefix("Bearer ")
            async with aiohttp.ClientSession(timeout=self.timeout) as s:
                async with s.ws_connect(self.ws_url, timeout=self.timeout.total) as ws:
                    await ws.receive_json()                  # auth_required
                    await ws.send_json({"type": "auth", "access_token": token})
                    auth = await ws.receive_json()
                    if auth.get("type") != "auth_ok":
                        raise PermissionError(
                            "home assistant refused the websocket token")
                    for index, command in enumerate(types, start=1):
                        await ws.send_json({"id": index, "type": command})
                        reply = await ws.receive_json()
                        if reply.get("success"):
                            out[command] = reply.get("result")
                        else:
                            log.warning("ha.ws_command_failed", command=command,
                                        code=(reply.get("error") or {}).get("code", ""))
        return out


def entity_summary(state: dict) -> dict:
    """Reduziert einen HA-State auf das fuer SOLVIO Wichtige (keine Rohdaten-Flut)."""
    attrs = state.get("attributes", {}) or {}
    eid = state.get("entity_id", "")
    return {
        "entity_id": eid,
        "friendly_name": attrs.get("friendly_name", eid),
        "domain": eid.split(".")[0] if "." in eid else "",
        "area": attrs.get("area") or attrs.get("area_id") or "",
        "state": state.get("state", ""),
        "capabilities": {
            k: attrs.get(k) for k in ("brightness", "supported_color_modes",
                                      "current_temperature", "temperature",
                                      "media_title") if k in attrs
        },
    }
