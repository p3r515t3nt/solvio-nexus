"""Home Assistant sichert sich selbst — SOLVIO holt die Sicherung nur ab.

Die Versuchung war, die 13-GB-VM-Platte zu kopieren. Das waere falsch: sie wird
im Betrieb dauernd beschrieben, eine Kopie im Laufen ist der klassische
inkonsistente Schnappschuss, und sie kostet je Satz 13 GB.

Gemessen liegt der richtige Weg direkt daneben. Dieses Home Assistant ist HA OS
mit Supervisor, es legt **taeglich von selbst** eine vollstaendige Sicherung an
(Konfiguration, Datenbank, alle Add-ons), behaelt drei davon — und eine solche
Sicherung wiegt 4,7 MB. Sie liegt allerdings INNERHALB der VM. Stirbt die VM,
sterben ihre Sicherungen mit ihr. Genau diese Luecke schliesst dieser Schritt:
abholen, nicht neu erzeugen.

Deshalb wird hier ausdruecklich **keine** Sicherung ausgeloest. Das waere ein
schreibender Eingriff in ein fremdes System fuer einen Nutzen, den es schon
gibt.

Zwei Ehrlichkeiten, die in den Wiederherstellungsplan gehoeren
-------------------------------------------------------------
* Die Sicherungen sind **passwortgeschuetzt** (`protected: true`). Ohne das
  HA-Sicherungspasswort ist die Datei unlesbar. Dieses Passwort steht in HAs
  eigener Konfiguration und **nicht** in SOLVIOs Sicherung — es gehoert in den
  Passwortmanager des Menschen.
* Der Supervisor-Proxy `/api/hassio/` weist den Long-Lived-Token mit 401 ab.
  Gebraucht wird er nicht: `backup/info` ueber die WebSocket-API und
  `/api/backup/download/<id>` ueber REST reichen, und beide antworten mit
  demselben Token.
"""
from __future__ import annotations

import json
import os
from typing import Any

import aiohttp

from solvio.integrations.home_assistant import HomeAssistant
from solvio.storage.sqlite_snapshot import sha256_file

#: Der lokale Sicherungsagent von HA OS. Ein zweiter existiert hier nicht.
AGENT_ID = "hassio.local"

#: Frist fuer den Download. Eine 5-MB-Datei ueber das eigene LAN.
DOWNLOAD_TIMEOUT = 300.0


async def newest_backup(ha: HomeAssistant) -> dict[str, Any] | None:
    """Die juengste Sicherung, die Home Assistant SELBST enthaelt.

    Add-on-Sicherungen (`homeassistant_included: false`) werden uebergangen —
    sie enthalten ein Add-on, nicht das System, und wer sie fuer eine Sicherung
    haelt, hat nichts.
    """
    result = await ha.ws_commands(["backup/info"])
    info = result.get("backup/info")
    if not info:
        return None
    usable = [b for b in info.get("backups", [])
              if b.get("homeassistant_included")
              and AGENT_ID in (b.get("agents") or {})]
    if not usable:
        return None
    return max(usable, key=lambda b: str(b.get("date", "")))


async def download(ha: HomeAssistant, backup_id: str, dest: str) -> int:
    """Laedt eine HA-Sicherung als Datei. Gibt die Groesse zurueck.

    Der Header kommt aus `ha._auth()` — dem einen kanonischen Weg, den jede
    andere HA-Anfrage nimmt. Bis zum 2026-08-31 stand hier `ha._headers`;
    dessen Token ist seit der Tresor-Wanderung dokumentiert leer, und genau
    diese eine uebersehene Aufrufstelle schickte `Authorization: Bearer ` —
    18 Saetze in Folge scheiterten mit 401, waehrend `backup/info` daneben
    gruen blieb (DEBT-0165).
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    url = f"{ha.base}/api/backup/download/{backup_id}?agent_id={AGENT_ID}"
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
    tmp = dest + ".part"
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with ha._auth() as headers:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                with open(tmp, "wb") as fh:
                    async for chunk in response.content.iter_chunked(1 << 20):
                        fh.write(chunk)
    os.replace(tmp, dest)
    os.chmod(dest, 0o600)
    return os.path.getsize(dest)


def _previous_entry(previous_root: str | None, backup_id: str) -> dict[str, Any] | None:
    """Liegt dieselbe HA-Sicherung schon im vorigen Satz?

    HA legt taeglich eine NEUE an, also ist der Normalfall ein Download. Aber
    der Sicherungsjob zuendet auch beim Einhaengen eines Volumes — ohne diese
    Pruefung wuerde jedes angesteckte Laufwerk dieselben 4,9 MB neu ziehen und
    ein zweites Mal ablegen.
    """
    if not previous_root:
        return None
    try:
        with open(os.path.join(previous_root, "manifest.json"), encoding="utf-8") as fh:
            previous = json.load(fh)
    except (OSError, ValueError):
        return None
    for entry in previous.get("entries", []):
        if entry.get("name") == "home-assistant" and entry.get("ha_backup_id") == backup_id:
            return entry
    return None


async def fetch_into(incoming_root: str, ha: HomeAssistant,
                     previous_root: str | None = None) -> dict[str, Any] | None:
    """Holt die juengste HA-Systemsicherung in einen Sicherungssatz.

    Gibt einen Manifest-Eintrag zurueck, oder `None`, wenn es nichts zu holen
    gibt. Wirft bei einem echten Fehler — der Aufrufer entscheidet, ob das den
    Satz kippt (es tut es nicht: HA ist eine Faehigkeitsschicht, kein Kern).
    """
    newest = await newest_backup(ha)
    if newest is None:
        return None
    bid = str(newest["backup_id"])
    rel = f"HomeAssistant/{newest['date'][:10]}-{bid}.tar"
    dest = os.path.join(incoming_root, rel)
    reused = None
    if not os.path.exists(dest):
        previous = _previous_entry(previous_root, bid)
        if previous:
            source = os.path.join(str(previous_root), str(previous.get("dest") or ""))
            if os.path.isfile(source):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                try:
                    os.link(source, dest)
                    # Nachgerechnet, nicht geglaubt.
                    if previous.get("sha256") and sha256_file(dest) != previous["sha256"]:
                        os.remove(dest)
                    else:
                        reused = previous.get("backup_id") or True
                except OSError:
                    pass
    if os.path.exists(dest):
        size = os.path.getsize(dest)
    else:
        size = await download(ha, bid, dest)
        reused = None
    agent = (newest.get("agents") or {}).get(AGENT_ID) or {}
    return {
        "name": "home-assistant", "kind": "file", "category": "homeassistant",
        "dest": rel, "bytes": size, "sha256": sha256_file(dest),
        "why": "HAs eigene Systemsicherung, abgeholt statt neu erzeugt.",
        "ha_backup_id": bid,
        "ha_backup_date": newest.get("date"),
        "ha_version": newest.get("homeassistant_version"),
        "database_included": bool(newest.get("database_included")),
        "addons": [a.get("slug") for a in (newest.get("addons") or [])],
        "protected": bool(agent.get("protected")),
        "reused_from": reused,
        "restore_note": ("Passwortgeschuetzt. Das HA-Sicherungspasswort steht in "
                         "Home Assistant, NICHT in dieser Sicherung."),
    }
