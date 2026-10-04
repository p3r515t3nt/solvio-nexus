"""Ein Ergebnis, das der Wahrheit standhaelt.

Der Tool-Layer kannte bisher `ToolResult(success: bool, ...)`. Das reicht, solange
alles entweder klappt oder nicht. Sobald Freigaben, Timeouts, abwesende Executoren und
mehrdeutige Ausgaenge dazukommen, ist ein einzelnes Bool eine Luege mit zwei Werten:
„nicht erfolgreich" faellt zusammen fuer „die Policy hat es verboten", „der Nutzer muss
erst bestaetigen", „der Container ist aus" und — am schlimmsten — „es koennte passiert
sein, wir wissen es nicht".

Diese Faelle sind fuer den Nutzer verschieden, also sind sie hier verschieden.

Zwei Sichten pro Ergebnis:

* `as_dict()`   — was das Modell sieht. Stabile Codes, nie eine Provider-Exception.
* `detail`      — was im Log steht. Bleibt intern, geht nie in den Modellkontext.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from solvio.tools.base import ToolResult


class CapabilityOutcome(str, Enum):
    """Der Ausgang eines Aufrufs. Genau ein Wert, immer gesetzt."""

    SUCCESS = "success"
    #: Ausgefuehrt, Wirkung eingetreten.

    INVALID_INPUT = "invalid_input"
    #: Die Argumente passen nicht zum Schema. Nichts ist passiert.

    REJECTED_BY_POLICY = "rejected_by_policy"
    #: Vertrag/Trust/Autoritaet verbieten es. Nichts ist passiert. Der haeufigste
    #: Grund ist fremder Inhalt, der zu handeln versucht.

    APPROVAL_REQUIRED = "approval_required"
    #: Ein Mensch muss zuerst freigeben. Nichts ist passiert. `data.request_id` ist
    #: ein IDENTIFIER, keine Autoritaet — er allein fuehrt nichts aus.

    CANCELLED = "cancelled"
    #: Vor oder waehrend der Ausfuehrung abgebrochen.

    TIMEOUT = "timeout"
    #: Die Frist lief ab. Bei schreibenden Faehigkeiten ist der Ausgang damit
    #: UNBEKANNT — deshalb entscheidet der Router, ob daraus RECOVERY_REQUIRED wird.

    EXECUTOR_UNAVAILABLE = "executor_unavailable"
    #: Der Executor war nicht erreichbar. Nichts ist passiert.

    CAPABILITY_FAILED = "capability_failed"
    #: Die Faehigkeit selbst schlug fehl und sagt zu: keine Wirkung.

    RECOVERY_REQUIRED = "recovery_required"
    #: Mehrdeutig. Die Wirkung KANN eingetreten sein. Nichts wird automatisch
    #: wiederholt; ein Mensch muss klaeren. Das ist die ehrliche Aussage, die
    #: P1C/F4 dem Sicherheitspfad bereits abverlangt.


#: Ausgaenge, bei denen mit Sicherheit KEINE Aussenwirkung eingetreten ist.
NO_EFFECT_OUTCOMES: frozenset[CapabilityOutcome] = frozenset({
    CapabilityOutcome.INVALID_INPUT,
    CapabilityOutcome.REJECTED_BY_POLICY,
    CapabilityOutcome.APPROVAL_REQUIRED,
    CapabilityOutcome.EXECUTOR_UNAVAILABLE,
    CapabilityOutcome.CAPABILITY_FAILED,
})


@dataclass
class CapabilityResult:
    """Das Ergebnis eines Capability-Aufrufs.

    `call_id` identifiziert genau DIESEN Versuch und ueberlebt ihn: er ist der
    Anker, an dem spaeter Runtime-State, HUD, Approval-UI und Audit haengen. Er ist
    ausdruecklich NICHT die `execution_id` aus dem eingefrorenen Sicherheitspfad:
    `execution_id_for(core_instance_id, approval_id)` ist die *stabile, abgeleitete*
    Identitaet einer freigegebenen Aktion, die einen Neustart und jeden Retry
    unveraendert ueberlebt, damit die Gegenseite deduplizieren kann. Ein Aufruf, der
    an der Policy scheitert, hat gar keine Freigabe — und darf deshalb auch keine
    solche Identitaet erfinden. Zwei Begriffe, zwei Namen.
    """
    outcome: CapabilityOutcome
    call_id: str
    capability: str
    data: Any = None
    human_message: str = ""
    reason: str = ""    # stabiler Maschinencode, z. B. "untrusted_origin"
    detail: str = ""    # Diagnose fuer das Log — NIE im Modellkontext

    @property
    def succeeded(self) -> bool:
        return self.outcome is CapabilityOutcome.SUCCESS

    @property
    def had_no_effect(self) -> bool:
        """True nur, wenn mit Sicherheit nichts nach aussen gewirkt hat."""
        return self.outcome in NO_EFFECT_OUTCOMES

    def as_dict(self) -> dict[str, Any]:
        """Die modellzugewandte Sicht. Ohne `detail`."""
        d: dict[str, Any] = {
            "outcome": self.outcome.value,
            "call_id": self.call_id,
            "capability": self.capability,
            "success": self.succeeded,
        }
        if self.data is not None:
            d["data"] = self.data
        if self.human_message:
            d["human_message"] = self.human_message
        if self.reason:
            d["reason"] = self.reason
        return d

    def as_tool_result(self) -> ToolResult:
        """Rueckwaertskompatibel fuer den bestehenden Tool-Pfad.

        Der Ausgang geht als stabiler `error`-Code mit, damit die Unterscheidung
        nicht verloren geht, wenn ein Aufrufer noch das alte Format erwartet.
        """
        if self.succeeded:
            return ToolResult(True, data=self.data, human_message=self.human_message)
        code = self.outcome.value if not self.reason else f"{self.outcome.value}:{self.reason}"
        return ToolResult(False, data=self.data, human_message=self.human_message,
                          error=code)
