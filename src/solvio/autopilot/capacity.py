"""Capacity Preflight — drei Rollen, drei getrennte Antworten (Amendment 8).

Die Trennung ist der ganze Zweck. Technical Lead, Builder und Advisor haengen
an **verschiedenen Anbietern**; wer sie in einer Spalte fuehrt, kann nicht
ausdruecken, dass ein Claude-Limit einen Codex-Build nicht anfassen darf. Genau
deshalb laeuft der Technical Lead ueber den Provider Broker und nicht ueber
Claude: sonst koennten Builder-Quota und TL-Quota gemeinsam ausfallen.

**Was hier NICHT passiert:** eine Resttokenzahl schaetzen. Es gibt keine
belastbare Quelle dafuer, und eine geratene Zahl waere schlimmer als keine —
sie wuerde geglaubt. `UNKNOWN` bleibt `UNKNOWN`.

Die Zustaende sind Auskunft ueber Verfuegbarkeit, nicht ueber Guthaben:

* `AVAILABLE`    — erreichbar, angemeldet, kein frischer Quota-Treffer
* `LIMITED`      — erreichbar, aber ein Quota-Treffer liegt noch im Fenster
* `EXHAUSTED`    — gemessener Quota-Treffer, Reset nicht erreicht
* `UNAVAILABLE`  — nicht erreichbar oder nicht angemeldet
* `UNKNOWN`      — nicht gemessen; kein Ersatzwert
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import Any

from solvio.autopilot import store as S
from solvio.logging_setup import get_logger

log = get_logger("autopilot")

AVAILABLE = "AVAILABLE"
LIMITED = "LIMITED"
EXHAUSTED = "EXHAUSTED"
UNAVAILABLE = "UNAVAILABLE"
UNKNOWN = "UNKNOWN"
STATES = (AVAILABLE, LIMITED, EXHAUSTED, UNAVAILABLE, UNKNOWN)

#: Wie lange ein gemessener Quota-Treffer nachwirkt, wenn der Anbieter KEINE
#: Reset-Zeit nennt. Konservativ und ausdruecklich eine Annahme — deshalb
#: faellt der Zustand danach auf `LIMITED`, nicht auf `AVAILABLE`: erst ein
#: erfolgreicher Aufruf macht wieder `AVAILABLE` daraus.
QUOTA_COOLDOWN = 3600.0
#: Danach gilt ein Treffer als Geschichte.
QUOTA_FORGET = 6 * 3600.0

#: Aufgabengroessen, die eine Rolle bei `LIMITED` noch annehmen soll. Grob,
#: absichtlich: eine feinere Regel braeuchte Zahlen, die es nicht gibt.
LIMITED_MAX_SIZE = "SMALL"


@dataclass
class CapacityReport:
    """Was ueber eine Rolle gemessen wurde — und woran es liegt."""

    role: str
    provider: str
    state: str
    signals: dict[str, Any] = field(default_factory=dict)
    measured_at: float = 0.0

    @property
    def usable(self) -> bool:
        return self.state in (AVAILABLE, LIMITED)

    def accepts(self, size: str) -> bool:
        """Darf dieser Rolle eine Aufgabe dieser Groesse gegeben werden?

        Bei `LIMITED` nur kleine: eine grosse Aufgabe mitten im knappen
        Kontingent endet mit hoher Wahrscheinlichkeit im Failover — und dann
        war die halbe Arbeit umsonst.
        """
        if self.state == AVAILABLE:
            return True
        if self.state == LIMITED:
            return size == LIMITED_MAX_SIZE
        return False

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "provider": self.provider,
                "state": self.state, "signals": dict(self.signals),
                "measured_at": self.measured_at}


def _from_signals(role: str, provider: str, *, reachable: bool,
                  authenticated: bool, last_success_at: float,
                  last_quota_hit_at: float, known_reset_at: float,
                  extra: dict | None = None, now: float) -> CapacityReport:
    """Aus belastbaren Signalen einen Zustand — in dieser Reihenfolge.

    Erreichbarkeit vor Anmeldung vor Kontingent: ein nicht erreichbares Werkzeug
    ist nicht „erschoepft", und ein abgemeldetes ist kein Kontingentproblem.
    Wer die Reihenfolge vertauscht, meldet dem Menschen die falsche Handlung.
    """
    signale = {"reachable": reachable, "authenticated": authenticated,
               "last_success_at": last_success_at or None,
               "last_quota_hit_at": last_quota_hit_at or None,
               "known_reset_at": known_reset_at or None, **(extra or {})}
    if not reachable:
        return CapacityReport(role, provider, UNAVAILABLE, signale, now)
    if not authenticated:
        return CapacityReport(role, provider, UNAVAILABLE,
                              signale | {"grund": "logged_out"}, now)
    if last_quota_hit_at:
        alter = now - last_quota_hit_at
        if known_reset_at and now < known_reset_at:
            return CapacityReport(role, provider, EXHAUSTED, signale, now)
        if not known_reset_at and alter < QUOTA_COOLDOWN:
            return CapacityReport(role, provider, EXHAUSTED, signale, now)
        if alter < QUOTA_FORGET and (not last_success_at
                                     or last_success_at < last_quota_hit_at):
            # Der Treffer ist alt genug zum Weitermachen, aber seither hat
            # nichts funktioniert. Das ist nicht „verfuegbar", das ist „wir
            # probieren es vorsichtig".
            return CapacityReport(role, provider, LIMITED, signale, now)
    return CapacityReport(role, provider, AVAILABLE, signale, now)


def _port_open(host: str, port: int, *, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _history(ledger, milestone_id: str, role: str) -> tuple[float, float]:
    """`(letzter Erfolg, letzter Quota-Treffer)` aus dem eigenen Verbrauchsbuch.

    Das lokale Buch ist die einzige Quelle, die wir wirklich besitzen. Ein
    Anbieter-Kontostand waere fremde Auskunft; hier steht, was bei UNS passiert
    ist.
    """
    erfolg = quota = 0.0
    for zeile in ledger.usage_rows(milestone_id, role=role):
        if zeile["note"] == "quota":
            quota = max(quota, zeile["recorded_at"])
        elif zeile["note"] in ("ok", ""):
            erfolg = max(erfolg, zeile["recorded_at"])
    return erfolg, quota


async def lead_capacity(ledger, milestone_id: str, *,
                        now: float = 0.0) -> CapacityReport:
    """Der Technical Lead haengt am Provider Broker, nicht an einem CLI.

    Gemessen wird, was messbar ist: laeuft der Broker auf seinem Port? Der
    Broker selbst haelt Kappen je Prinzipal — ein Kontostand ist deshalb weder
    noetig noch verfuegbar.
    """
    moment = now or time.time()
    from solvio.provider_broker.service import configured_port
    port = configured_port()
    erreichbar = _port_open("127.0.0.1", port)
    erfolg, quota = _history(ledger, milestone_id, S.ROLE_LEAD)
    return _from_signals(S.ROLE_LEAD, "provider-broker", reachable=erreichbar,
                         authenticated=erreichbar, last_success_at=erfolg,
                         last_quota_hit_at=quota, known_reset_at=0.0,
                         extra={"port": port}, now=moment)


async def builder_capacity(ledger, milestone_id: str, adapter, *,
                           now: float = 0.0) -> CapacityReport:
    """Ein Builder-Adapter. Gesperrte Adapter sind nie `AVAILABLE`.

    Wichtig: `BLOCKED_BY_SECURITY_POLICY` wird als `UNAVAILABLE` gemeldet, aber
    mit dem Sicherheitsgrund in den Signalen. Ein Failover, der ihn deshalb
    ueberspringt, tut das aus dem richtigen Grund — und der Bericht kann
    hinterher sagen, welcher es war.
    """
    from solvio.autopilot import builders as B
    moment = now or time.time()
    if adapter.capability() == B.BLOCKED_BY_SECURITY_POLICY:
        return CapacityReport(S.ROLE_BUILDER, adapter.name, UNAVAILABLE,
                              {"blocked_by_security_policy": True,
                               "grund": adapter.blocked_reason()}, moment)
    lage = await adapter.status()
    erfolg, quota = _history(ledger, milestone_id, S.ROLE_BUILDER)
    return _from_signals(S.ROLE_BUILDER, adapter.name,
                         reachable=bool(lage.get("available")),
                         authenticated=bool(lage.get("available")),
                         last_success_at=erfolg, last_quota_hit_at=quota,
                         known_reset_at=0.0,
                         extra={"anmeldung": lage.get("anmeldung", ""),
                                "grund": lage.get("reason", "")}, now=moment)


async def advisor_capacity(ledger, milestone_id: str, *,
                           now: float = 0.0) -> CapacityReport:
    """Der Challenger. Claude darf beraten — nur nicht schreiben."""
    moment = now or time.time()
    from solvio.specialists import providers as P
    lage = await P.claude_status()
    erfolg, quota = _history(ledger, milestone_id, S.ROLE_ADVISOR)
    return _from_signals(S.ROLE_ADVISOR, "claude-code",
                         reachable=lage.available or lage.reason == "logged_out",
                         authenticated=lage.available,
                         last_success_at=erfolg, last_quota_hit_at=quota,
                         known_reset_at=0.0,
                         extra={"grund": lage.reason}, now=moment)


async def preflight(ledger, milestone_id: str, adapters: dict, *,
                    now: float = 0.0) -> dict[str, CapacityReport]:
    """Alle drei Rollen messen und buchen. Vor jeder groesseren Phase.

    Der Builder-Bericht ist der des **bevorzugten** Adapters; wer sonst noch
    koennte, steht in `builder_options`. Das trennt „womit arbeiten wir gerade"
    von „was gaebe es sonst noch" — und genau diese Trennung braucht der
    Failover.
    """
    moment = now or time.time()
    berichte: dict[str, CapacityReport] = {}
    berichte[S.ROLE_LEAD] = await lead_capacity(ledger, milestone_id, now=moment)
    berichte[S.ROLE_ADVISOR] = await advisor_capacity(ledger, milestone_id,
                                                      now=moment)
    optionen: dict[str, CapacityReport] = {}
    for name, adapter in adapters.items():
        optionen[name] = await builder_capacity(ledger, milestone_id, adapter,
                                                now=moment)
    bevorzugt = choose_builder(optionen)
    berichte[S.ROLE_BUILDER] = (optionen[bevorzugt] if bevorzugt else
                                CapacityReport(S.ROLE_BUILDER, "", UNKNOWN,
                                               {"grund": "kein Adapter"}, moment))
    for rolle, bericht in berichte.items():
        ledger.set_capacity(milestone_id, role=rolle, provider=bericht.provider,
                            state=bericht.state, signals=bericht.signals,
                            now=moment)
    berichte["builder_options"] = optionen           # type: ignore[assignment]
    log.info("autopilot.preflight", lead=berichte[S.ROLE_LEAD].state,
             builder=berichte[S.ROLE_BUILDER].state,
             advisor=berichte[S.ROLE_ADVISOR].state)
    return berichte


def choose_builder(options: dict[str, CapacityReport], *,
                   size: str = "MEDIUM", exclude: tuple[str, ...] = ()) -> str:
    """Wer baut? `AVAILABLE` vor `LIMITED`, gesperrte nie.

    Die Sortierung ist stabil (Name als zweiter Schluessel), damit derselbe
    Zustand zweimal denselben Builder ergibt — ein Failover, der wuerfelt,
    ist nicht nachvollziehbar.
    """
    rang = {AVAILABLE: 0, LIMITED: 1}
    kandidaten = [(rang[b.state], name) for name, b in sorted(options.items())
                  if name not in exclude and b.state in rang
                  and b.accepts(size)]
    if not kandidaten:
        # Zweiter Anlauf ohne die Groessenregel: lieber ein `LIMITED`-Builder
        # mit grosser Aufgabe als gar keiner — der Failover faengt das auf.
        kandidaten = [(rang[b.state], name) for name, b in sorted(options.items())
                      if name not in exclude and b.state in rang]
    if not kandidaten:
        return ""
    return min(kandidaten)[1]
