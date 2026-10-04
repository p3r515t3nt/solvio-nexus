"""Wie es dem Tresor geht — ohne ihn dafuer aufzumachen.

Die Regel, die diese Datei klein haelt: **eine Gesundheitspruefung
entschluesselt nichts.** Ein Zustandsbericht, der jeden Zugang oeffnet, um zu
sagen, dass es sie gibt, ist selbst ein Angriffspfad — und er laeuft alle
dreissig Sekunden.

Was stattdessen geprueft wird, ist billig und trotzdem aussagekraeftig:

| Lage | Wort | warum |
|---|---|---|
| kein Tresor angelegt | `healthy` | ein leerer Tresor ist kein Defekt |
| Datenbank beschaedigt | `unavailable` | hier hilft nur Wiederherstellung |
| Datei fuer andere lesbar | `degraded` | Rechte, die offen standen, gelten als gelesen |
| Schluesselbund antwortet nicht | `auth_required` | nur ein Mensch kann das |
| Eintraege da, aber kein Schluessel | `unavailable` | der schlimmste Fall, ehrlich benannt |
| kein Wiederherstellungsumschlag | `degraded` | die Sicherung waere nach einem Mac-Verlust wertlos |
| Umschlag passt nicht zum Schluessel | `degraded` | veraltet ist schlimmer als fehlend, weil es aussieht wie da |
| Klartextdublette in `.env` | `degraded` | der Zugang existiert zweimal |
| sonst | `healthy` | |

`auth_required` ist dasselbe Wort, das die verschluesselte Sicherungsplatte
benutzt, wenn sie gesperrt ist — und es bedeutet dasselbe: es ist nichts kaputt,
es fehlt eine menschliche Handlung.
"""
from __future__ import annotations

import os

from solvio.secret_vault import keyring as K
from solvio.secret_vault import migration as M
from solvio.secret_vault import recovery as RC
from solvio.secret_vault.policy import Status
from solvio.secret_vault.store import VaultStore, db_path, vault_dir

HEALTHY = "healthy"
DEGRADED = "degraded"
AUTH_REQUIRED = "auth_required"
UNAVAILABLE = "unavailable"


def assess() -> tuple[str, str]:
    """Ein Wort und ein Satz. Nennt nie einen Verweis mit Wert und nie einen Wert."""
    path = db_path()
    if not os.path.exists(path):
        return HEALTHY, "Tresor ist noch nicht eingerichtet"

    try:
        store = VaultStore(path)
    except Exception:  # noqa: BLE001 - eine unlesbare Datei ist eine Aussage
        # Schon das OEFFNEN scheitert, wenn die Datei keine Datenbank mehr ist.
        # Gefunden, nicht vermutet: eine beschaedigte Datei warf hier eine
        # Ausnahme, statt einen Zustand zu melden — und eine Gesundheitspruefung,
        # die wirft, meldet gar nichts.
        return UNAVAILABLE, "Tresordatei ist nicht lesbar"
    if not store.integrity_ok():
        return UNAVAILABLE, "Tresordatei ist beschaedigt"

    count = store.count()
    active = sum(1 for p in store.policies() if p.status is Status.ACTIVE)

    try:
        kek = K.read_kek()
    except K.VaultLocked:
        return AUTH_REQUIRED, "Schluesselbund ist gesperrt — Anmeldung fehlt"
    except K.VaultError:
        return UNAVAILABLE, "Hauptschluessel ist nicht lesbar"
    if kek is None and count > 0:
        return UNAVAILABLE, f"{count} Zugaenge ohne Hauptschluessel"
    fingerprint = RC.key_fingerprint(kek) if kek else ""
    del kek

    problems: list[str] = []
    if not store.permissions_ok():
        problems.append("Tresordatei steht fuer andere offen")

    envelope = RC.read()
    if envelope is None:
        problems.append("kein Wiederherstellungsumschlag hinterlegt")
    elif fingerprint and envelope.kek_fingerprint != fingerprint:
        problems.append("Wiederherstellungsumschlag passt nicht zum Schluessel")

    leftover = M.leftover_plaintext(refs=set(store.refs()))
    if leftover:
        problems.append(f"{len(leftover)} Zugaenge liegen noch als Klartext daneben")

    if problems:
        return DEGRADED, "; ".join(problems)[:140]
    if count == 0:
        return HEALTHY, "Tresor ist bereit, noch kein Zugang hinterlegt"
    return HEALTHY, f"{active} von {count} Zugaengen aktiv, Wiederherstellung aktuell"


def detail() -> dict[str, object]:
    """Die sichere Langfassung — fuer Runbook und Kontrollzentrum, nie fuers Modell.

    Enthaelt Zahlen, Zustaende und Pfade. Keine Verweise mit Kontobezeichnung,
    keine Werte, keinen Fingerabdruck des Schluessels.
    """
    path = db_path()
    word, reason = assess()
    out: dict[str, object] = {
        "zustand": word,
        "grund": reason,
        "verzeichnis": vault_dir(),
        "eingerichtet": os.path.exists(path),
        "schluesselspeicher": "datei (Test)" if K.is_test_backend() else "schluesselbund",
    }
    if not os.path.exists(path):
        return out
    try:
        store = VaultStore(path)
        policies = store.policies()
    except Exception:  # noqa: BLE001 - dieselbe Aussage wie oben
        out["zugaenge"] = "nicht lesbar"
        return out
    out["zugaenge"] = len(policies)
    out["aktiv"] = sum(1 for p in policies if p.status is Status.ACTIVE)
    out["deaktiviert"] = sum(1 for p in policies if p.status is Status.DISABLED)
    out["widerrufen"] = sum(1 for p in policies if p.status is Status.REVOKED)
    out["rechte_ok"] = store.permissions_ok()
    current = RC.is_current()
    out["wiederherstellung"] = ("aktuell" if current is True
                                else "veraltet" if current is False
                                else "nicht feststellbar")
    out["klartextdubletten"] = M.leftover_plaintext(refs=set(store.refs()))
    return out
