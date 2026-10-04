"""Wie es den Teilen wirklich geht — und nicht, ob ein Prozess existiert.

„Laeuft" ist die bequemste und nutzloseste Auskunft. Ein Core, der laeuft, aber
dessen Google-Zugang abgelaufen ist, ist fuer den Nutzer kaputt; ein Hermes, der
gestartet wurde, aber nicht antwortet, ebenso. Deshalb fragt jede Pruefung hier
dort nach, wo es wehtut — und sagt, wenn sie es nicht konnte.

Zwei Regeln haben die Form bestimmt:

* **Nicht haemmern.** Eine Netzpruefung je Bildschirmaktualisierung waere bei
  fuenf Sekunden Takt ein Dauerfeuer auf Google und Home Assistant. Also hat jede
  Pruefung eine Haltbarkeit, und teure haben eine lange.
* **`unbekannt` ist eine Antwort.** Wer nicht nachgesehen hat, sagt das, statt
  „gesund" zu melden. Ein gruener Punkt, der nur bedeutet „ich habe nicht
  gefragt", ist schlimmer als ein grauer.

Die Zustaende sind bewusst dieselben Worte wie beim Gap Resolver: ein abgelaufener
Zugang ist `auth_required` und kein Ausfall, ein erschoepftes Kontingent ist
`quota_limited` und kein Mangel an Faehigkeit.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from solvio.logging_setup import get_logger

log = get_logger("control")


class State(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    AUTH_REQUIRED = "auth_required"
    QUOTA_LIMITED = "quota_limited"
    UNKNOWN = "unknown"


#: Welche Zustaende den Nutzer wirklich angehen. Ein voruebergehender Ausfall
#: nicht — der geht vorbei. Ein abgelaufener Zugang schon: ohne ihn passiert
#: nichts mehr, und nur ein Mensch kann ihn erneuern.
NEEDS_USER = frozenset({State.AUTH_REQUIRED})

#: Was in der Uebersicht als Warnung zaehlt.
NOT_WELL = frozenset({State.DEGRADED, State.UNAVAILABLE, State.AUTH_REQUIRED,
                      State.QUOTA_LIMITED})


@dataclass
class Component:
    key: str
    label: str
    state: State = State.UNKNOWN
    reason: str = ""
    last_checked_at: float = 0.0
    last_success_at: float = 0.0

    @property
    def user_action_required(self) -> bool:
        return self.state in NEEDS_USER

    def as_dict(self) -> dict[str, Any]:
        return {"komponente": self.key, "name": self.label,
                "zustand": self.state.value,
                "grund": self.reason[:160],
                "geprueft_um": self.last_checked_at or None,
                "zuletzt_gesund": self.last_success_at or None,
                "braucht_dich": self.user_action_required}


@dataclass
class Probe:
    """Eine Pruefung mit Haltbarkeit."""

    key: str
    label: str
    check: Callable[[], Awaitable[tuple[State, str]]]
    #: Wie lange das Ergebnis gilt. Netzpruefungen lange, oertliche kurz.
    ttl: float = 30.0
    #: Nur zur Anzeige und fuer Tests: ob diese Pruefung ins Netz geht.
    network: bool = False


#: Haltbarkeiten. Bewusst grosszuegig bei allem, was einen fremden Dienst kostet.
TTL_LOCAL = 15.0
TTL_PROCESS = 30.0
TTL_NETWORK = 180.0
TTL_PROVIDER = 300.0

#: Wie lange eine Pruefung dauern darf, bevor sie als unerreichbar gilt.
PROBE_TIMEOUT = 8.0


class HealthBoard:
    """Haelt den letzten bekannten Zustand jedes Teils — mit Haltbarkeit."""

    def __init__(self, probes: list[Probe], *, clock=None) -> None:
        self.probes = {probe.key: probe for probe in probes}
        self.clock = clock or time.time
        self._state: dict[str, Component] = {
            probe.key: Component(probe.key, probe.label) for probe in probes}
        self._running: set[str] = set()

    def known(self) -> list[Component]:
        return [self._state[key] for key in self.probes]

    def component(self, key: str) -> Component | None:
        return self._state.get(key)

    def stale(self) -> list[str]:
        now = self.clock()
        return [key for key, probe in self.probes.items()
                if now - self._state[key].last_checked_at >= probe.ttl]

    async def refresh(self, keys: list[str] | None = None) -> list[Component]:
        """Prueft, was abgelaufen ist. Alles gleichzeitig, jedes mit Frist."""
        due = [key for key in (keys or self.stale()) if key in self.probes
               and key not in self._running]
        if due:
            await asyncio.gather(*(self._one(key) for key in due))
        return self.known()

    async def _one(self, key: str) -> None:
        probe = self.probes[key]
        component = self._state[key]
        self._running.add(key)
        try:
            state, reason = await asyncio.wait_for(probe.check(),
                                                   timeout=PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            state, reason = State.UNAVAILABLE, "antwortet nicht rechtzeitig"
        except Exception as exc:  # noqa: BLE001 - eine Pruefung kippt nie das Brett
            log.info("control.probe_failed", component=key,
                     kind=type(exc).__name__)
            state, reason = State.UNKNOWN, "konnte nicht geprüft werden"
        finally:
            self._running.discard(key)
        component.state = state
        component.reason = reason
        component.last_checked_at = self.clock()
        if state is State.HEALTHY:
            component.last_success_at = component.last_checked_at

    # -- Zusammenfassung -----------------------------------------------------

    def summary(self) -> dict[str, Any]:
        components = self.known()
        troubled = [c for c in components if c.state in NOT_WELL]
        needs_user = [c for c in components if c.user_action_required]
        if needs_user:
            overall, sentence = State.AUTH_REQUIRED, _sentence_for(needs_user)
        elif any(c.state is State.UNAVAILABLE for c in troubled):
            overall = State.UNAVAILABLE
            sentence = _sentence_for([c for c in troubled
                                      if c.state is State.UNAVAILABLE])
        elif troubled:
            overall, sentence = State.DEGRADED, _sentence_for(troubled)
        elif all(c.state is State.UNKNOWN for c in components):
            overall, sentence = State.UNKNOWN, "Ich habe noch nicht nachgesehen."
        else:
            overall, sentence = State.HEALTHY, "Alles läuft."
        return {"zustand": overall.value, "satz": sentence,
                "auffaellig": [c.key for c in troubled],
                "braucht_dich": [c.key for c in needs_user]}


def _sentence_for(components: list[Component]) -> str:
    """Ein Satz statt einer Fehlerliste — und beschreibend, nicht auffordernd.

    „Brauchen einen Blick" verspricht eine Handlung. Bei einer Stoerung gibt es
    aber keine: sie geht vorbei, und die Rubrik „Braucht dich" bleibt leer.
    Ueberschrift und Rubrik wuerden sich widersprechen. Wo wirklich jemand
    gefragt ist, steht der Grund ohnehin im Satz.
    """
    names = [c.label for c in components][:3]
    if len(names) == 1:
        return f"{names[0]}: {components[0].reason or 'nicht in Ordnung'}"
    if len(names) == 2:
        return f"{names[0]} und {names[1]} sind gerade nicht in Ordnung."
    return ", ".join(names[:-1]) + f" und {names[-1]} sind gerade nicht in Ordnung."
