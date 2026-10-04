"""Der Zustand des Routers — aus denselben Zeilen, die auch das Buch liest.

Gibt einfache Zeichenketten zurueck, damit das Modul ohne die Ueberwachung
pruefbar ist. `unknown` ist NIE gruen: ein unlesbares Buch ist keine Gesundheit,
sondern eine fehlende Messung.

Ein abgeschalteter Router meldet ebenfalls `unknown` und nicht `healthy` —
„laeuft nicht" und „laeuft gut" sind zwei verschiedene Auskuenfte.
"""
from __future__ import annotations

from solvio.cognition import models as M

#: Ab welchem Anteil gescheiterter Kommissionen der Router als angeschlagen
#: gilt. Ein einzelner Fehlschlag ist kein Defekt — ein Anbieter, der die
#: Haelfte der Stunde nicht erreichbar war, schon.
DEGRADED_FAILURE_SHARE = 0.5

#: Wie viele Kommissionen mindestens vorliegen muessen, bevor der Anteil
#: ueberhaupt etwas aussagt.
MIN_SAMPLE = 4


def assess(*, now: float = 0.0, ledger=None, mode: str = "") -> tuple[str, str]:
    """Zustand und Grund, beide als schlichte Zeichenkette."""
    from solvio.cognition.ledger import CognitionLedger

    if mode == "off":
        return "unknown", "Der kognitive Router ist abgeschaltet"
    try:
        book = ledger if ledger is not None else CognitionLedger()
        counts = book.counts(now=now)
    except Exception:  # noqa: BLE001 - ein unlesbares Buch ist kein gruener Punkt
        return "unknown", "Entscheidungsbuch nicht lesbar"

    recent = int(counts.get("recent", 0))
    failed = int(counts.get("failed", 0))
    escalated = int(counts.get("escalated", 0))

    if recent >= MIN_SAMPLE and failed / recent >= DEGRADED_FAILURE_SHARE:
        return "degraded", (f"{failed} von {recent} Einordnungen der letzten "
                            "Stunde sind gescheitert")
    if escalated >= M.SHADOW_MAX_PER_DAY:
        return "degraded", "auffaellig viele Eskalationen in der letzten Stunde"
    if not recent:
        return "healthy", "keine Einordnung in der letzten Stunde"
    return "healthy", f"{recent} Einordnungen in der letzten Stunde"


def snapshot(*, now: float = 0.0, ledger=None) -> dict:
    """Die Zahlen fuer eine Anzeige. Deutsche Schluessel, wie im Haus ueblich."""
    from solvio.cognition.ledger import CognitionLedger

    try:
        book = ledger if ledger is not None else CognitionLedger()
        counts = book.counts(now=now)
        rechte = book.permissions_ok()
    except Exception:  # noqa: BLE001 - fail-soft
        return {"lesbar": False}
    return {"lesbar": True, "rechte_eng": bool(rechte),
            "entscheidungen": counts.get("total", 0),
            "letzte_stunde": counts.get("recent", 0),
            "gescheitert": counts.get("failed", 0),
            "eskaliert": counts.get("escalated", 0)}
