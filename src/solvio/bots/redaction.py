"""Was nach einem Anmeldedatum aussieht — an einer Stelle, fuer alle Botpfade.

Der Rohtext eines Botlaufs ist fremder Text aus einem fremden Prozess. Er kann
einen Schluessel tragen, weil das Werkzeug ihn in einer Fehlermeldung nennt oder
weil Hermes ihn im ausfuehrlichen Modus selbst ausgibt (`sk-proj-...ABCD`).
Entfernt wird das, bevor irgendetwas damit passiert — auch bevor es in ein
Protokoll geraet.

Die Formen selbst stehen bewusst nicht hier zum zweiten Mal: die vollstaendigen
Schluesselformen kennt der Starter des Fachteams schon, und zwei Listen driften
irgendwann auseinander. Hinzu kommt genau eine Form, die dort fehlt — die
verkuerzte Schreibweise, deren Punkte das Muster brechen.
"""
from __future__ import annotations

import re

from solvio.specialists.launcher import MASK, redact as _redact_full

#: Die verkuerzten Schluesselformen, die fremde Werkzeuge selbst ausgeben:
#: Hermes schreibt `sk-proj-...ABCD`, OpenAI antwortet mit
#: `sk-inval***...zzzz`. Beide sind schon maskiert — aber eine Regel, die sie
#: ganz entfernt, ist besser als das Vertrauen darauf, dass der Fremde richtig
#: maskiert hat.
_MASKED_KEY = re.compile(r"sk-[A-Za-z0-9_\-]{0,20}[.*]{3,}[A-Za-z0-9_\-]{2,}")


def redact(text: str) -> str:
    """Entfernt, was nach Anmeldedaten aussieht — vollstaendig wie verkuerzt."""
    return _MASKED_KEY.sub(MASK, _redact_full(text or ""))
