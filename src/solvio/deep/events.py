"""Der Lebenszyklus einer tiefen Aufgabe — in SOLVIOs eigenen Worten.

Ein fremder Executor hat sein eigenes Vokabular, und das aendert sich mit jeder
seiner Versionen. Wuerde SOLVIO diese Namen durchreichen, waere jede spaetere
Umbenennung dort ein Bruch hier — und ein Austausch des Executors unmoeglich.
Deshalb uebersetzt dieses Modul: was hereinkommt, ist Beobachtung; was
hinausgeht, sind die elf Zustaende, auf die sich ein HUD, ein Audit und der Core
verlassen duerfen.

Identitaet ist die Sequenz, nie die Uhrzeit. Dieselbe Begruendung wie bei
`CapabilityEvent`: Zeitstempel kollidieren, springen und sind nach einem Neustart
nicht monoton. Wer „gib mir alles ab hier" sagen koennen soll, braucht eine
Ordnung, die das aushaelt.

Und der Punkt, an dem dieses Modul Sicherheit ist und nicht Formatierung:
**Executor-Text ist Information, nie Autoritaet.** Was aus einem Werkzeug
zurueckkommt, hat eine Webseite geschrieben, und eine Webseite darf nicht
mitteilen, dass sie freigegeben wurde. `neutralize` nimmt solchem Text die
Verkleidung — die eigentliche Grenze ist trotzdem die Struktur: fremder Text
landet in einem Datenfeld, niemals in der Rolle einer Anweisung.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from solvio.contracts.untrusted import MAX_TEXT, neutralize
from solvio.contracts.untrusted import as_information as _as_information

#: Was jeder Nutzlast beigeschrieben wird, die aus dem Executor stammt.
CONTENT_TRUST = "untrusted_executor"


class EventKind(str, Enum):
    """Die stabilen Zustaende. Der Core kennt nur diese."""

    TASK_CREATED = "task_created"
    EXECUTOR_STARTING = "executor_starting"
    RUNNING = "running"
    OBSERVATION = "observation"
    TOOL_REQUESTED = "tool_requested"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RESUMED = "resumed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: Zustaende, nach denen nichts mehr kommt. Spaeter eintreffende Executor-Ausgabe
#: darf eine Aufgabe von hier aus NICHT wiederbeleben (§13).
TERMINAL_KINDS: frozenset[EventKind] = frozenset({
    EventKind.CANCELLED, EventKind.SUCCEEDED, EventKind.FAILED,
})


@dataclass(frozen=True)
class DeepEvent:
    """Ein Ereignis einer tiefen Aufgabe.

    `task_id` ist die SOLVIO-Kennung — nie die des Executors. `seq` ist je
    Aufgabe monoton und wird beim Schreiben ins Journal vergeben, damit ein
    wiederverbindender Leser luecken- und dopplungsfrei fortsetzen kann.
    """

    task_id: str
    seq: int
    kind: EventKind
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "seq": self.seq,
                "kind": self.kind.value, "payload": dict(self.payload)}


# ---------------------------------------------------------------------------
# Fremden Text entwaffnen
# ---------------------------------------------------------------------------
# Die Regeln stehen nicht hier. Eine E-Mail, ein Kalendertitel, ein Suchtreffer
# und eine Webseite stellen dieselbe Frage, und vier Kopien derselben Antwort
# driften auseinander, bis die schwaechste gewinnt. `contracts.untrusted` ist
# der eine Ort; dieses Modul gibt der Antwort nur ihren Herkunftsnamen.


def as_information(value: Any, *, limit: int = MAX_TEXT) -> dict[str, Any]:
    """Verpackt Executor-Ausgabe so, dass ihre Herkunft mitreist."""
    return _as_information(value, CONTENT_TRUST, limit=limit)
