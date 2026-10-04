"""Die Probe der Agentenlaufzeit — `assess() -> (Wort, Grund)`.

Zeichenketten statt `State`, damit dieses Modul ohne das Kontrollzentrum
testbar ist; die Probe wandelt sie um. Dieselbe Bauart wie bei Speicher, Tresor
und Zahlung.

Zwei Zustaende sind ausdruecklich NICHT gruen, obwohl nichts kaputt ist:

* **wartende Laeufe** faerben nicht — ein Lauf, der auf einen Menschen wartet,
  ist kein Defekt, sondern eine offene Bitte. Er wird gezaehlt und genannt.
* **steckende Laeufe** faerben schon: „nicht-terminal und seit mehr als der
  doppelten Schrittfrist ohne Ereignis" ist die Definition, und ein Lauf, den
  niemand als steckend sieht, ist genau der Lauf, der niemandem auffaellt.

`unknown` ist nie gruen. Wenn das Buch nicht lesbar ist, ist das eine Auskunft
und keine Beruhigung.
"""
from __future__ import annotations

import os

#: Ab wann ein nicht-terminaler Lauf ohne Ereignis als steckend gilt.
STUCK_AFTER = 3600.0


def assess(*, now: float = 0.0, ledger=None) -> tuple[str, str]:
    """Der Zustand der Laufzeit — aus denselben Zeilen, die auch die Chronik liest."""
    from solvio.agent_runtime import store as S

    if os.environ.get("SOLVIO_AGENT_RUNTIME", "1") in ("0", "off", "no"):
        return "unknown", "Agentenlaufzeit ist abgeschaltet"
    try:
        book = ledger if ledger is not None else S.AgentRunLedger()
        counts = book.counts(stuck_after=STUCK_AFTER, now=now)
    except Exception:  # noqa: BLE001
        return "unknown", "Agentenbuch nicht lesbar"

    stuck = counts.get("stuck") or []
    if stuck:
        return "degraded", (f"{len(stuck)} Auftrag/Auftraege haengen seit ueber "
                            f"einer Stunde ohne Regung")

    waiting_user = counts.get("waiting_user", 0)
    waiting_approval = counts.get("waiting_approval", 0)
    if waiting_user:
        # Kein Defekt: eine offene Bitte. Sie faerbt nicht, sie wird gesagt.
        return "healthy", (f"{waiting_user} Auftrag/Auftraege warten auf dich")
    if waiting_approval:
        return "healthy", (f"{waiting_approval} Auftrag/Auftraege warten auf "
                           "eine Freigabe")
    active = counts.get("active", 0)
    if active:
        return "healthy", f"{active} Auftrag/Auftraege laufen"
    return "healthy", "keine offenen Auftraege"


def snapshot(*, now: float = 0.0, ledger=None) -> dict:
    """Die Zahlen fuer das Kontrollzentrum — ohne zweite Wahrheit."""
    from solvio.agent_runtime import store as S

    try:
        book = ledger if ledger is not None else S.AgentRunLedger()
        counts = book.counts(stuck_after=STUCK_AFTER, now=now)
    except Exception:  # noqa: BLE001
        return {"lesbar": False}
    return {
        "lesbar": True,
        "offen": counts.get("open", 0),
        "aktiv": counts.get("active", 0),
        "wartet_auf_freigabe": counts.get("waiting_approval", 0),
        "wartet_auf_dich": counts.get("waiting_user", 0),
        "unterbrochen": counts.get("interrupted", 0),
        "steckend": len(counts.get("stuck") or []),
    }
