"""Wie es dem Speicher geht — in der Sprache, die das Kontrollzentrum spricht.

Hier wird kein neues Gesundheitsmodell erfunden. Es gibt genau eine Funktion,
die ein `(State, Grund)`-Paar liefert, und die haengt als eigene `Probe` in
`control_center/probes.py`. Alles andere im Haus bleibt, wie es ist.

Die eigentliche Arbeit steckt in der Zuordnung, und die hat eine Leitregel:

> **Eine abgezogene Platte ist kein Defekt.**

Das ist die Kernaussage des ganzen Milestones, hier in Code gegossen. SOLVIO
darf nicht kraenkeln, weil ein Mensch eine externe Platte mitgenommen hat.
Solange die letzte Sicherung frisch ist, ist der Zustand **gesund** — mit einem
Grund, der die Wahrheit sagt („nicht angeschlossen, letzte Sicherung vor vier
Stunden"). Erst wenn daraus ein echtes Risiko wird, aendert sich die Farbe.

Die Zuordnung im Einzelnen:

| Lage | Zustand | warum |
|---|---|---|
| nichts eingerichtet | `unknown` | nicht gemessen, nicht „gesund" |
| Platte weg, Sicherung frisch | `healthy` | genau so soll es sein |
| Platte weg, Sicherung alt | `degraded` | jetzt fehlt wirklich etwas |
| Platte da, gesperrt | `auth_required` | nur ein Mensch kann das |
| Platte da, unverschluesselt | `unavailable` | es wird nichts Privates geschrieben |
| falsche Platte | `unavailable` | Identitaet stimmt nicht |
| Platte da, wenig Platz | `degraded` | bald geht es nicht mehr |
| Sicherung scheitert wiederholt | `degraded` | gruen waere gelogen |

`auth_required` fuer die gesperrte Platte ist kein Kunstgriff: der Arzt
behandelt genau diesen Zustand als „nur ein Mensch kann das" und versucht keine
Reparatur. Es ist dasselbe Wort wie fuer eine abgelaufene Google-Anmeldung, und
es bedeutet dasselbe.
"""
from __future__ import annotations

import os
import time
from typing import Any

from solvio.storage import engine, volume
from solvio.storage.volume import VolumeState

#: Ab hier ist eine Sicherung „alt". Bei taeglichem Lauf sind zwei verpasste
#: Tage ein Zeichen, kein Zufall.
WARN_AFTER_SECONDS = 48 * 3600.0

#: Ab hier ist sie ein Problem, das der Mensch wissen muss.
STALE_AFTER_SECONDS = 7 * 24 * 3600.0

#: Unter diesem Freiplatz wird gewarnt. 20 GB reichen fuer viele Saetze —
#: darunter wird es knapp, bevor es weh tut.
LOW_SPACE_BYTES = 20 * 1024 ** 3


def _age_text(seconds: float) -> str:
    """Ein Alter, wie man es sagt."""
    if seconds < 90 * 60:
        return f"vor {max(1, int(seconds // 60))} Minuten"
    if seconds < 36 * 3600:
        return f"vor {int(seconds // 3600)} Stunden"
    return f"vor {int(seconds // 86400)} Tagen"


def _gb(n: int) -> str:
    return f"{n / 1024 ** 3:.0f} GB"


def collect(*, now: float | None = None,
            state: VolumeState | None = None,
            backup_state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Alle Speicherzahlen an einem Ort. Zahlen, nie Inhalt.

    Bewusst ohne Urteil: `assess()` urteilt, `collect()` misst. Das trennt die
    Frage „was ist" von der Frage „was heisst das", und nur die zweite ist eine
    Produktentscheidung.
    """
    now = now if now is not None else time.time()
    vol = state if state is not None else volume.probe()
    st = backup_state if backup_state is not None else engine.load_state()
    last_ok = float(st.get("last_success_at") or 0.0)
    return {
        "volume": vol.as_dict(),
        "backup": {
            "last_success_at": last_ok or None,
            "last_attempt_at": st.get("last_attempt_at"),
            "last_backup_id": st.get("last_backup_id"),
            "last_error": st.get("last_error"),
            "consecutive_failures": int(st.get("consecutive_failures") or 0),
            "last_duration_seconds": st.get("last_duration_seconds"),
            "last_total_bytes": st.get("last_total_bytes"),
            "age_seconds": (now - last_ok) if last_ok else None,
        },
    }


def assess(report: dict[str, Any] | None = None, *,
           now: float | None = None) -> tuple[str, str]:
    """(Zustandswort, Grund) — die Rohform der Probe, ohne Import des Kontrollzentrums.

    Gibt Zeichenketten zurueck statt `State`, damit dieses Modul ohne das
    Kontrollzentrum testbar ist. Die Probe wandelt sie in `State` um.
    """
    now = now if now is not None else time.time()
    rep = report if report is not None else collect(now=now)
    vol = rep["volume"]
    bak = rep["backup"]
    age = bak["age_seconds"]

    if not vol.get("configured"):
        return "unknown", "keine Speicherplatte eingerichtet"

    problems = list(vol.get("problems") or ())

    if vol.get("present") and vol.get("locked"):
        return "auth_required", "Speicherplatte ist gesperrt — Passphrase fehlt"

    if vol.get("present") and not vol.get("encrypted"):
        return "unavailable", ("Speicherplatte ist nicht verschluesselt — "
                               "es wird nichts Privates darauf geschrieben")

    if vol.get("present") and problems:
        # Alles Uebrige an Problemen heisst: es haengt etwas da, dem nicht zu
        # trauen ist. Das ist ausdruecklich schlimmer als „nichts da".
        return "unavailable", "; ".join(problems)[:140]

    if not vol.get("present"):
        if age is None:
            return "degraded", "Platte nicht angeschlossen, noch nie gesichert"
        if age >= STALE_AFTER_SECONDS:
            return "degraded", (f"Platte seit Langem nicht angeschlossen, "
                                f"letzte Sicherung {_age_text(age)}")
        if age >= WARN_AFTER_SECONDS:
            return "degraded", (f"Platte nicht angeschlossen, letzte Sicherung "
                                f"{_age_text(age)}")
        # Der Kernsatz des Milestones: das hier ist gesund.
        return "healthy", f"Platte nicht angeschlossen, Sicherung {_age_text(age)}"

    free = int(vol.get("free_bytes") or 0)
    fails = int(bak.get("consecutive_failures") or 0)

    if fails >= 2:
        return "degraded", f"Sicherung scheiterte {fails}-mal hintereinander"
    if age is None:
        return "degraded", "Platte bereit, aber noch nie gesichert"
    if age >= STALE_AFTER_SECONDS:
        return "degraded", f"letzte erfolgreiche Sicherung {_age_text(age)}"
    if free and free < LOW_SPACE_BYTES:
        return "degraded", f"nur noch {_gb(free)} frei"
    if age >= WARN_AFTER_SECONDS:
        return "degraded", f"letzte Sicherung {_age_text(age)}"

    return "healthy", (f"verschluesselt, {_gb(free)} frei, "
                       f"Sicherung {_age_text(age)}")


def summary(report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ein kleiner Bericht fuer CLI und Runbook. Ohne Geheimnisse."""
    rep = report if report is not None else collect()
    state, reason = assess(rep)
    vol = rep["volume"]
    smart = vol.get("smart") or {}
    return {
        "zustand": state,
        "grund": reason,
        "platte": {
            "eingerichtet": vol.get("configured"),
            "angeschlossen": vol.get("present"),
            "verschluesselt": vol.get("encrypted"),
            "dateisystem": vol.get("filesystem"),
            "name": vol.get("volume_name"),
            "uuid": vol.get("volume_uuid"),
            "einhaengepunkt": vol.get("mount_point"),
            "frei_bytes": vol.get("free_bytes"),
            "gesamt_bytes": vol.get("total_bytes"),
            "smart": vol.get("smart_status"),
            # SMART in Klartext: Verschleiss in Prozent, Betriebsstunden,
            # Medienfehler. Mehr braucht ein Mensch nicht, und mehr sagt die
            # Platte auch nicht ehrlich.
            "verschleiss_prozent": smart.get("PERCENTAGE_USED"),
            "betriebsstunden": smart.get("POWER_ON_HOURS_0"),
            "medienfehler": smart.get("MEDIA_ERRORS_0"),
            "temperatur_c": (round(smart["TEMPERATURE"] - 273.15, 1)
                             if isinstance(smart.get("TEMPERATURE"), (int, float))
                             else None),
        },
        "sicherung": rep["backup"],
    }
