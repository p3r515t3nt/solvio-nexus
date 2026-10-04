"""MODEL_FIT, EXECUTION_FIT, ROUTE — drei Fragen, getrennt beantwortet.

Die Trennung ist der ganze Zweck. Wer nur festhaelt, WAS gelaufen ist,
verliert die Information, was besser gewesen waere — und nach drei Monaten
sieht eine dauerhaft kapazitaetsbedingte Notloesung aus wie eine
Architekturentscheidung.

* **MODEL_FIT** — welcher Builder waere fuer diese Aufgabe qualitativ die
  beste Wahl? Diese Bewertung kennt **weder Kontingent noch Sperre noch
  Kosten**. Sie darf nachtraeglich nicht verfaelscht werden.
* **EXECUTION_FIT** — welche davon sind verfuegbar, erlaubt und haben
  Kapazitaet?
* **ROUTE** — welche wird tatsaechlich genommen, und warum diese.

Nicht bei jedem Ministritt neu bewertet. Neu bewertet wird bei: neuer Phase,
neuer groesserer Aufgabe, neuem Finding, wiederholtem Fehlversuch,
Builder-Failover.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("autopilot")

#: Anlaesse fuer eine Neubewertung. Geschlossen — sonst wird bei jedem
#: Handgriff neu entschieden, und die Route waere kein Beschluss mehr.
NEW_PHASE = "new_phase"
NEW_TASK = "new_task"
NEW_FINDING = "new_finding"
REPEATED_FAILURE = "repeated_failure"
BUILDER_FAILOVER = "builder_failover"
TRIGGERS = frozenset({NEW_PHASE, NEW_TASK, NEW_FINDING, REPEATED_FAILURE,
                      BUILDER_FAILOVER})

#: Gruende, warum ROUTE von MODEL_FIT abweicht. Geschlossen.
BEST_FIT = "best_fit"
CAPACITY = "capacity"
SECURITY_POLICY = "security_policy"
UNAVAILABLE = "unavailable"
REASONS = frozenset({BEST_FIT, CAPACITY, SECURITY_POLICY, UNAVAILABLE})

#: Welcher Builder fuer welche Arbeit qualitativ am besten passt.
#:
#: Das ist eine PRODUKTPRAEFERENZ, keine Verfuegbarkeitsaussage — genau
#: deshalb steht sie hier und nicht in `capacity.py`. Sie stammt aus der
#: Profiltabelle der Agentenlaufzeit: Claude entwirft und baut, Codex greift
#: an und prueft.
PREFERENCE: dict[str, tuple[str, ...]] = {
    "design": ("claude", "codex"),
    "implementation": ("claude", "codex"),
    "repair": ("codex", "claude"),
    "review": ("codex", "claude"),
    "routine": ("codex", "claude"),
}
DEFAULT_KIND = "implementation"


@dataclass
class Route:
    """Was gewaehlt wurde — und was besser gewesen waere."""

    trigger: str
    task_kind: str
    model_fit: str
    execution_fit: tuple[str, ...]
    route: str
    reason: str
    detail: str = ""

    @property
    def compromised(self) -> bool:
        """Weicht die Route von der Qualitaetswahl ab?"""
        return bool(self.route) and self.route != self.model_fit

    def as_dict(self) -> dict[str, Any]:
        return {"trigger": self.trigger, "task_kind": self.task_kind,
                "model_fit": self.model_fit,
                "execution_fit": list(self.execution_fit),
                "route": self.route, "reason": self.reason,
                "detail": self.detail, "compromised": self.compromised}

    def line(self) -> str:
        """Eine Zeile fuers Buch — so, wie der Auftrag sie verlangt."""
        return (f"preferred = {self.model_fit or '—'}, "
                f"actual = {self.route or '—'}, reason = {self.reason}")


def decide(*, trigger: str, task_kind: str, adapters: dict,
           options: dict, size: str = "MEDIUM") -> Route:
    """Die drei Fragen, in dieser Reihenfolge.

    MODEL_FIT wird **zuerst** und **ohne Kenntnis der Lage** bestimmt. Wer
    zuerst schaut, was verfuegbar ist, bekommt eine Praeferenz, die genau das
    bevorzugt, was gerade laeuft — und kann nie mehr sagen, was gefehlt hat.
    """
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown_trigger:{trigger}")
    art = task_kind if task_kind in PREFERENCE else DEFAULT_KIND
    reihenfolge = PREFERENCE[art]

    # 1) MODEL_FIT — reine Qualitaetsfrage.
    modell_fit = next((n for n in reihenfolge if n in adapters), "")

    # 2) EXECUTION_FIT — was davon geht tatsaechlich.
    from solvio.autopilot import builders as B
    moeglich = []
    for name in reihenfolge:
        adapter = adapters.get(name)
        if adapter is None:
            continue
        if adapter.capability() != B.WRITER:
            continue
        bericht = options.get(name)
        if bericht is None or not bericht.accepts(size):
            continue
        moeglich.append(name)

    # 3) ROUTE — und der Grund, warum nicht die erste Wahl.
    gewaehlt = moeglich[0] if moeglich else ""
    if not gewaehlt:
        grund, detail = UNAVAILABLE, "kein zugelassener Builder mit Kapazitaet"
    elif gewaehlt == modell_fit:
        grund, detail = BEST_FIT, ""
    else:
        adapter = adapters.get(modell_fit)
        if adapter is not None and adapter.capability() != B.WRITER:
            grund = SECURITY_POLICY
            detail = adapter.blocked_reason()
        else:
            bericht = options.get(modell_fit)
            # `capacity` und `unavailable` sind nicht dasselbe, und der
            # Unterschied ist der, den ein Bericht spaeter braucht:
            #
            # * `capacity`    — der Builder KOENNTE, sein Kontingent ist alle.
            #                   Morgen laeuft dieselbe Route wieder.
            # * `unavailable` — er kann gerade grundsaetzlich nicht: keine
            #                   Anmeldung, nicht erreichbar, nicht angemeldet.
            #                   Da hilft kein Warten, da hilft eine Handlung.
            #
            # Beides `capacity` zu nennen liesse „dem Autopiloten fehlt eine
            # Anmeldung" wie „das Kontingent war knapp" aussehen — und genau
            # diese Verwechslung waere die, die niemand mehr aufloest.
            lage = bericht.state if bericht else ""
            grund = CAPACITY if lage == "EXHAUSTED" else UNAVAILABLE
            detail = (f"{modell_fit}: {lage}" if bericht
                      else f"{modell_fit}: nicht gemessen")

    route = Route(trigger=trigger, task_kind=art, model_fit=modell_fit,
                  execution_fit=tuple(moeglich), route=gewaehlt,
                  reason=grund, detail=detail)
    log.info("autopilot.route", trigger=trigger, preferred=modell_fit,
             actual=gewaehlt, reason=grund)
    return route
