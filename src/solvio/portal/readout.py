"""Wo aus einer Seite ein Bericht wird — und warum das je Portal verschieden ist.

Die generische Lesefunktion holt, was auf jeder Seite gleich heisst: Titel,
Ueberschriften, ausgewiesene Warnungen, beschriftete Zahlen. Das ist die richtige
Grundlage, aber am ersten echten Portal auch sichtbar zu wenig gewesen: SOLVIO
Studio baut seine Navigation nicht aus `<nav>`, seine Kennzahlen nicht aus
`<dl>`, und sein Konto steht in einem Menue ohne eine einzige der ueblichen
Beschriftungen. Der generische Griff fand genau eine Ueberschrift und einen
Hinweis — auf einer Seite voller Zustand.

Die Versuchung ist, den generischen Griff so lange zu erweitern, bis er dieses
eine Portal trifft. Das ist der Anfang vom Ende: jede Erweiterung ist die
Handschrift eines bestimmten Anbieters, und in einem halben Jahr steht in der
Grundlage ein Dutzend fremder Klassennamen, den niemand mehr aendern darf.

Also bleibt die Grundlage generisch, und die Ortskenntnis bekommt ihren eigenen
Platz: eine Reduktion je Portal, angemeldet unter der Portal-Kennung. Wer sie
nicht hat, bekommt weiterhin die generische Auswahl — kein Portal ist auf eine
Reduktion angewiesen.

Zwei Regeln, die fuer jede Reduktion gelten:

* **Nichts erfinden.** Ein Muster, das nicht trifft, ergibt keinen Eintrag —
  keine Null, keinen Strich, kein „unbekannt". Ein erfundener Wert in einem
  Kennzahlenbericht ist schlimmer als eine Luecke, weil er gelesen wird.
* **Weniger herausgeben, nicht mehr.** Die Seite enthaelt vollstaendige
  Textentwuerfe. Ein *Statusbericht* braucht davon die Zahl, nicht den Text.
  Was hier nicht ausdruecklich ausgewaehlt wird, erreicht das Modell nicht.

Der Inhalt bleibt in jedem Fall `untrusted_web`. Eine Reduktion macht aus einer
Seite keine Quelle von Autoritaet — sie macht sie nur lesbar.
"""
from __future__ import annotations

from typing import Any, Callable

#: Portal-Kennung -> Reduktion. Wird von den portalspezifischen Modulen gefuellt.
REDUCERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def register(portal_id: str, reducer: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    REDUCERS[portal_id] = reducer


def reduce_for(portal_id: str, reply: dict[str, Any]) -> dict[str, Any] | None:
    """Die Reduktion dieses Portals — oder `None`, wenn es keine gibt.

    Ein Fehler in einer Reduktion darf den Lesevorgang nicht kippen: dann gilt
    wieder die generische Auswahl. Ein Bericht mit weniger Feldern ist ein
    Ergebnis; eine Ausnahme mitten im Lesen ist keines.
    """
    reducer = REDUCERS.get(portal_id)
    if reducer is None:
        return None
    try:
        return reducer(reply)
    except Exception:  # noqa: BLE001
        return None


def lines_of(text: str, *, limit: int = 4000) -> list[str]:
    """Die sichtbaren Zeilen einer Seite, leere verworfen.

    Der gemeinsame Nenner aller Reduktionen. Zeilen sind stabiler als
    Klassennamen: ein Anbieter baut sein CSS oefter um als seine Beschriftungen.
    """
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line:
            out.append(line)
        if len(out) >= limit:
            break
    return out


def after(lines: list[str], anchor: str) -> int:
    """Der Index NACH einer wortgleichen Zeile, sonst -1."""
    try:
        return lines.index(anchor) + 1
    except ValueError:
        return -1
