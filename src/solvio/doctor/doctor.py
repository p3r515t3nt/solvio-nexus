"""Erst verstehen, dann anfassen — und danach nachsehen, ob es geholfen hat.

Drei Regeln, in dieser Reihenfolge, und jede einzelne ist teuer erkauft:

**Diagnose vor Reparatur.** Ein System, das bei jedem roten Punkt etwas neu
startet, verdeckt Ursachen, statt sie zu beheben. Der Neustart hilft beim ersten
Mal, beim zehnten Mal ist er ein Ritual — und niemand weiss mehr, warum.

**Reparieren nur mit bewiesener Befugnis.** Zugelassen ist ausschliesslich, was
aus einer geschlossenen Liste kommt, und die besteht aus Funktionen, nicht aus
Text. Ein Modell darf beim Verstehen helfen; es kann kein Vorgehen erfinden, weil
Erfinden hiesse, Code zu schreiben.

**Nach der Reparatur pruefen.** Ein Vorgehen, das ohne Fehler durchlief, hat gar
nichts bewiesen. Bewiesen ist es, wenn die Komponente danach wirklich gesund
antwortet — mit einer FRISCHEN Messung, nicht mit dem zwischengespeicherten
Zustand von vorhin. Wer das verwechselt, meldet jeden Neustart als Erfolg.

Dazu kommt die unauffaelligste und wichtigste Regel: **es wird nicht ewig
versucht.** Zweimal, dann Ruhe, dann ein Mensch. Ein Wiederbelebungsversuch alle
zwanzig Sekunden ist kein Selbstheilen, sondern ein Dauerschaden mit gutem
Gewissen.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from solvio.control_center.health import HealthBoard, State
from solvio.doctor import playbooks as P
from solvio.doctor.diagnosis import (
    Confidence, Diagnosis, RepairClass, conclude, healthy,
)
from solvio.logging_setup import get_logger

log = get_logger("doctor")

#: Wie lange ein Befund als derselbe Vorfall gilt. Danach faengt die Zaehlung
#: von vorn an — sonst waere eine Komponente nach einem schlechten Tag fuer
#: immer gesperrt.
INCIDENT_WINDOW = 6 * 3600.0

#: Wie oft SOLVIO denselben Vorfall hoechstens selbst anfasst.
MAX_ATTEMPTS = 2

#: Wie lange eine Genesung halten muss, damit der naechste Ausfall als NEUER
#: Vorfall gilt.
#:
#: Ohne diese Grenze zaehlt jede Stoerung derselben Komponente sechs Stunden
#: lang auf dasselbe Konto — auch dann, wenn jede Reparatur dazwischen geglueckt
#: ist. Ein Dienst, dem zweimal erfolgreich geholfen wurde, waere fuer den Rest
#: des Fensters gesperrt. Das ist keine Bremse mehr, sondern Aufgeben.
#:
#: Deutlich laenger als die Abkuehlzeit eines Vorgehens (600s): so kann ein
#: Dienst, der sofort wieder stirbt, den Zaehler niemals zuruecksetzen. Genau
#: das soll er nicht koennen — Flattern ist derselbe Vorfall.
STABLE_SECONDS = 900.0

#: Wie lange ein blosses Zucken dauern darf, bevor es ein Vorfall ist. Ein Dienst,
#: der zwei Sekunden nicht antwortet, braucht keinen Arzt.
PERSIST_SECONDS = 60.0


@dataclass
class Incident:
    """Ein Vorfall je Komponente — kompakt, dauerhaft, ohne Rohprotokolle."""

    component: str
    first_seen: float
    last_seen: float
    attempts: int = 0
    last_playbook: str = ""
    last_result: str = ""
    next_allowed: float = 0.0
    resolved_at: float = 0.0

    @property
    def open(self) -> bool:
        return not self.resolved_at

    def as_dict(self) -> dict[str, Any]:
        return {"komponente": self.component, "seit": self.first_seen,
                "zuletzt": self.last_seen, "versuche": self.attempts,
                "letztes_vorgehen": self.last_playbook,
                "letztes_ergebnis": self.last_result,
                "wieder_erlaubt_ab": self.next_allowed or None,
                "behoben_um": self.resolved_at or None}


class Doctor:
    """Stellt Befunde, repariert das Zugelassene, prueft nach."""

    def __init__(self, dispatcher: Any, board: HealthBoard, *, clock=None,
                 store: Any = None) -> None:
        self.dispatcher = dispatcher
        self.board = board
        self.clock = clock or time.time
        #: Der dauerhafte Vorfallspeicher. Optional: ohne ihn arbeitet der
        #: Doctor genauso, merkt sich aber nichts ueber Neustarts hinweg.
        self.store = store
        self._incidents: dict[str, Incident] = {}
        #: Welche Komponenten schon aus der Ablage geholt wurden. Einmal je
        #: Laufzeit reicht — danach ist der Arbeitsspeicher die Wahrheit.
        self._loaded: set[str] = set()
        self._working: set[str] = set()

    # -- Diagnose --------------------------------------------------------------

    async def diagnose(self, component: str, *,
                       requested_by_user: bool = False) -> Diagnosis:
        """Ein Befund aus dem, was messbar ist. Keine Ursache ohne Beleg.

        `requested_by_user` hebt die Schonfrist auf, nicht die Obergrenze. Die
        Frist gibt es, damit der Hintergrund nicht auf jedes Zucken reagiert —
        ein Mensch, der gerade fragt, ist aber selbst der Beleg dafuer, dass es
        aufgefallen ist. Ihm „alles in Ordnung" zu sagen, weil die Stoerung erst
        zwanzig Sekunden alt ist, waere eine falsche Antwort mit gutem Gewissen.
        """
        await self._restore(component)
        await self.board.refresh([component])
        found = self.board.component(component)
        if found is None:
            return conclude(component, State.UNKNOWN,
                            symptoms=["diese Komponente kenne ich nicht"],
                            repair=RepairClass.UNSUPPORTED_REPAIR)
        if found.state is State.HEALTHY:
            await self._resolve(component)
            return healthy(component)

        incident = self._touch(component)
        evidence = [f"Zustand {found.state.value} seit "
                    f"{int(self.clock() - incident.first_seen)}s"]
        if found.reason:
            evidence.append(found.reason)
        persistent = (requested_by_user
                      or (self.clock() - incident.first_seen) >= PERSIST_SECONDS)

        # Der wichtigste Zweig: ein abgelaufener Zugang ist keine Stoerung, und
        # er ist nichts, was SOLVIO selbst richten koennte. Wer das zusammen mit
        # „nicht erreichbar" behandelt, versucht ewig etwas, das nur ein Mensch
        # kann.
        if found.state is State.AUTH_REQUIRED:
            return conclude(
                component, found.state, symptoms=[found.reason],
                cause=_auth_cause(component, found.reason), evidence=evidence,
                repair=RepairClass.REAUTH_REQUIRED, persistent=True,
                impact=_impact(component),
                verification="nach der Anmeldung meldet sich die Komponente "
                             "wieder als in Ordnung")

        if found.state is State.QUOTA_LIMITED:
            return conclude(
                component, found.state, symptoms=[found.reason],
                cause="Das Kontingent des Anbieters ist gerade erschoepft",
                evidence=evidence, repair=RepairClass.NO_ACTION,
                persistent=False, impact=_impact(component),
                verification="das Kontingent erneuert sich von selbst")

        available = P.for_component(component)
        if not available:
            return conclude(
                component, found.state, symptoms=[found.reason],
                cause=_cause_from(found.reason), evidence=evidence,
                repair=(RepairClass.UNSUPPORTED_REPAIR if persistent
                        else RepairClass.NO_ACTION),
                persistent=persistent, impact=_impact(component),
                verification="")

        if not persistent:
            # Noch zu frueh. Ein Zucken ist kein Vorfall.
            return conclude(component, found.state, symptoms=[found.reason],
                            cause="", evidence=evidence,
                            repair=RepairClass.NO_ACTION, persistent=False,
                            impact=_impact(component))

        book = available[0]
        return conclude(
            component, found.state, symptoms=[found.reason],
            cause=_cause_from(found.reason), evidence=evidence,
            repair=book.repair, playbook=book.key, persistent=True,
            impact=_impact(component), verification=book.verification)

    async def diagnose_all(self, *,
                           requested_by_user: bool = False) -> list[Diagnosis]:
        """Die Runde. Das Kranke wird befundet — das Gesunde wird abgehakt.

        Der zweite Teil sieht ueberfluessig aus und ist es nicht: eine
        Komponente, die von selbst zurueckkommt (etwa weil der Core neu
        gestartet hat), wird hier nie wieder befundet. Ohne das ausdrueckliche
        Abhaken bliebe ihre Akte fuer immer offen — mit ausgereiztem Zaehler,
        der die naechste, voellig andere Stoerung ungeprueft abweist.
        """
        await self.board.refresh()
        found: list[Diagnosis] = []
        for component in self.board.known():
            if component.state is State.HEALTHY:
                await self._restore(component.key)
                await self._resolve(component.key)
                continue
            found.append(await self.diagnose(
                component.key, requested_by_user=requested_by_user))
        return found

    # -- Reparatur --------------------------------------------------------------

    async def repair(self, diagnosis: Diagnosis, *,
                     requested_by_user: bool = False) -> P.Attempt:
        """Fuehrt ein zugelassenes Vorgehen aus — und prueft danach nach."""
        component = diagnosis.component
        attempt = P.Attempt(playbook=diagnosis.playbook, component=component,
                            started=self.clock())
        book = P.get(diagnosis.playbook) if diagnosis.playbook else None
        if book is None:
            attempt.outcome = P.REPAIR_FAILED
            attempt.detail = "fuer diesen Fall gibt es kein zugelassenes Vorgehen"
            return attempt
        if book.component != component:
            # Ein Vorgehen gehoert genau einer Komponente. Sonst koennte ein
            # Befund ueber A eine Handlung an B ausloesen.
            attempt.outcome = P.REPAIR_FAILED
            attempt.detail = "Vorgehen gehoert nicht zu dieser Komponente"
            return attempt

        await self._restore(component)
        incident = self._touch(component)
        allowed, why = self._may_attempt(incident, book,
                                         requested_by_user=requested_by_user)
        if not allowed:
            attempt.outcome = P.REPAIR_FAILED
            attempt.detail = why
            log.info("doctor.repair_refused", component=component, reason=why)
            return attempt
        if component in self._working:
            attempt.outcome = P.REPAIR_FAILED
            attempt.detail = "laeuft bereits"
            return attempt

        self._working.add(component)
        incident.attempts += 1
        incident.last_playbook = book.key
        log.info("doctor.repair_started", component=component,
                 playbook=book.key, attempt=incident.attempts)
        try:
            attempt.ran = bool(await asyncio.wait_for(
                book.action(self.dispatcher), timeout=P.REPAIR_TIMEOUT))
        except asyncio.TimeoutError:
            attempt.detail = "das Vorgehen brauchte zu lange"
        except Exception as exc:  # noqa: BLE001 - ein Fehlschlag ist ein Ergebnis
            attempt.detail = f"{type(exc).__name__}"
            log.info("doctor.repair_raised", component=component,
                     kind=type(exc).__name__)
        finally:
            self._working.discard(component)

        # Die Nachpruefung. Sie entscheidet, nicht der Rueckgabewert oben.
        recovered, note = await self.verify(component)
        attempt.recovered = recovered
        attempt.elapsed = self.clock() - attempt.started
        if recovered:
            attempt.outcome = P.RECOVERED
            attempt.detail = note
            await self._resolve(component)
        elif attempt.ran:
            # Durchgelaufen, aber nicht gesund. Genau das ist der Fall, den ein
            # naiver Arzt als Erfolg meldet.
            attempt.outcome = P.INCONCLUSIVE
            attempt.detail = attempt.detail or note
        else:
            attempt.outcome = P.REPAIR_FAILED
            attempt.detail = attempt.detail or note
        incident.last_result = attempt.outcome
        incident.next_allowed = self.clock() + book.cooldown
        await self._persist(incident)
        log.info("doctor.repair_finished", component=component,
                 outcome=attempt.outcome, recovered=recovered)
        return attempt

    async def verify(self, component: str) -> tuple[bool, str]:
        """Frisch messen — nie den zwischengespeicherten Zustand nehmen.

        Das Gesundheitsbrett haelt Ergebnisse bis zu fuenf Minuten. Nach einer
        Reparatur ist genau dieser alte Wert die falsche Auskunft: er stammt aus
        der Zeit VOR dem Eingriff. Deshalb wird die Haltbarkeit hier bewusst
        uebergangen.
        """
        await asyncio.sleep(P.SETTLE_SECONDS)
        await self.board.refresh([component])
        found = self.board.component(component)
        if found is None:
            return False, "Komponente unbekannt"
        return (found.state is State.HEALTHY), (found.reason or found.state.value)

    async def heal(self, component: str, *, requested_by_user: bool = False
                   ) -> tuple[Diagnosis, P.Attempt | None]:
        """Der ganze Ablauf fuer eine Komponente."""
        diagnosis = await self.diagnose(component,
                                        requested_by_user=requested_by_user)
        if not diagnosis.repair_available:
            return diagnosis, None
        return diagnosis, await self.repair(diagnosis,
                                            requested_by_user=requested_by_user)

    async def heal_all(self, *, requested_by_user: bool = False
                       ) -> list[tuple[Diagnosis, P.Attempt | None]]:
        """„Repariere alles, was kaputt ist."

        Ausdruecklich NICHT „tu alles, was noetig waere": angefasst wird nur, wofuer
        ein zugelassenes Vorgehen existiert. Der Rest wird benannt und bleibt
        liegen — das ist die ehrliche Lesart der Bitte.
        """
        results = []
        for diagnosis in await self.diagnose_all(
                requested_by_user=requested_by_user):
            if diagnosis.repair_available:
                results.append((diagnosis, await self.repair(
                    diagnosis, requested_by_user=requested_by_user)))
            else:
                results.append((diagnosis, None))
        return results

    # -- Schleifenschutz --------------------------------------------------------

    def _may_attempt(self, incident: Incident, book: P.Playbook, *,
                     requested_by_user: bool) -> tuple[bool, str]:
        """Darf jetzt? Zweimal, dann Ruhe, dann ein Mensch.

        Ein ausdruecklicher Tipp des Nutzers hebt die Abkuehlzeit auf — er hat ja
        gerade hingesehen — aber NICHT die Obergrenze. Sonst waere ein
        ungeduldiger Finger dasselbe wie eine Endlosschleife.
        """
        now = self.clock()
        if incident.attempts >= MAX_ATTEMPTS:
            return False, (f"schon {incident.attempts} Mal versucht — "
                           f"da sieht besser jemand nach")
        if not requested_by_user and incident.next_allowed > now:
            return False, (f"noch {int(incident.next_allowed - now)}s Ruhe "
                           f"nach dem letzten Versuch")
        return True, ""

    async def _restore(self, component: str) -> None:
        """Den offenen Vorfall aus der Ablage holen — einmal je Laufzeit.

        Ohne das faengt die Zaehlung nach jedem Neustart des Core bei null an.
        Das ist genau der Fall, der weh tut: eine Komponente, die den Core mit
        hinunterreisst, wuerde nach jedem Hochfahren erneut zweimal angefasst —
        also fuer immer, in ordentlichen Zweierschritten. Die Ablage gab es
        dafuer von Anfang an; gelesen hat sie niemand.
        """
        if component in self._loaded:
            return
        self._loaded.add(component)
        if self.store is None:
            return
        try:
            row = await self.store.open_incident(component)
        except Exception as exc:  # noqa: BLE001 - Buchfuehrung kippt nie die Behandlung
            log.info("doctor.restore_failed", kind=type(exc).__name__)
            return
        if not row:
            return
        if (self.clock() - float(row["last_seen"])) > INCIDENT_WINDOW:
            # Zu alt, um noch derselbe Vorfall zu sein. `_touch` wuerde ihn
            # ohnehin verwerfen — hier stehenzulassen waere nur Ballast.
            return
        self._incidents[component] = Incident(
            component=component, first_seen=float(row["first_seen"]),
            last_seen=float(row["last_seen"]), attempts=int(row["attempts"] or 0),
            last_playbook=row["last_playbook"] or "",
            last_result=row["last_result"] or "",
            next_allowed=float(row["next_allowed"] or 0.0))
        log.info("doctor.incident_restored", component=component,
                 attempts=int(row["attempts"] or 0))

    def _touch(self, component: str) -> Incident:
        now = self.clock()
        incident = self._incidents.get(component)
        stale = incident is not None and (now - incident.last_seen) > INCIDENT_WINDOW
        held = (incident is not None and incident.resolved_at
                and (now - incident.resolved_at) >= STABLE_SECONDS)
        if incident is None or stale or held:
            # `held`: die letzte Genesung hat gehalten. Was jetzt kommt, ist ein
            # neuer Vorfall und kein Rueckfall — er faengt bei null an.
            incident = Incident(component=component, first_seen=now, last_seen=now)
            self._incidents[component] = incident
        else:
            incident.last_seen = now
            incident.resolved_at = 0.0
        return incident

    async def _resolve(self, component: str) -> None:
        """Vorfall schliessen — auch dauerhaft.

        Die Bedingung ist nicht nur Sparsamkeit: `diagnose` laeuft fuer jede
        gesunde Komponente in jeder Runde durch diesen Weg. Ohne `open` waere
        das ein Schreibvorgang je Komponente und Minute, fuer immer.
        """
        incident = self._incidents.get(component)
        if incident is not None and incident.open:
            incident.resolved_at = self.clock()
            await self._persist(incident)
            log.info("doctor.incident_resolved", component=component,
                     attempts=incident.attempts)

    async def _persist(self, incident: Incident) -> None:
        if self.store is None:
            return
        try:
            await self.store.put_incident(incident)
        except Exception as exc:  # noqa: BLE001 - Buchfuehrung kippt nie die Behandlung
            log.info("doctor.persist_failed", kind=type(exc).__name__)

    def incidents(self) -> list[Incident]:
        return sorted(self._incidents.values(), key=lambda i: -i.last_seen)


#: Ursachen aus dem, was die Gesundheitspruefung gemeldet hat. Bewusst knapp:
#: was hier nicht steht, bleibt „nicht bekannt", und das ist eine gueltige
#: Antwort.
_CAUSES = (
    ("prozess beendet", "Der Kindprozess laeuft nicht mehr"),
    ("laeuft nicht", "Die Komponente wurde nicht gestartet"),
    ("nicht erreichbar", "Die Komponente antwortet nicht"),
    ("antwortet nicht", "Die Komponente antwortet nicht"),
    ("kein isolierter prozess", "Die Isolation steht nicht"),
    ("nicht verdrahtet", "Die Komponente laeuft, ist aber nicht angebunden"),
    ("kein freigabeweg", "Der Freigabeweg ist nicht verdrahtet"),
    ("arbeiter laeuft nicht", "Der Arbeiterprozess laeuft nicht"),
    ("kein browser gefunden", "Es ist kein Browser installiert"),
)


def _cause_from(reason: str) -> str:
    low = (reason or "").lower()
    for marker, sentence in _CAUSES:
        if marker in low:
            return sentence
    return ""


_AUTH_CAUSES = {
    "calendar": "Google hat den Zugang abgelehnt — die Anmeldung ist abgelaufen",
    "gmail": "Google hat den Zugang abgelehnt — die Anmeldung ist abgelaufen",
    "claude": "Die Claude-Sitzung ist abgelaufen",
    "codex": "Die Codex-Sitzung ist abgelaufen",
    "agent_runtime": "Ein Auftrag haengt seit ueber einer Stunde",
    "storage": "Die Speicherplatte ist gesperrt und braucht ihre Passphrase",
    "offsite": "Die Fernsicherung kommt nicht an ihren Schluessel oder ihren "
               "Anbieterzugang — Schluesselbund oder Tresor gesperrt",
    "vault": "Der Schluesselbund ist gesperrt — der Tresor braucht deine Anmeldung",
    "payment": "Einem Zahlungsmittel fehlt sein Zugang im Tresor",
}


def _auth_cause(component: str, reason: str) -> str:
    return _AUTH_CAUSES.get(component, "Der Zugang ist abgelaufen")


#: Was der Mensch davon merkt. Ohne diesen Satz ist eine Diagnose eine
#: Betriebsmeldung, keine Auskunft.
_IMPACT = {
    "calendar": "Ich kann deine Termine gerade nicht lesen.",
    "gmail": "Ich kann deine E-Mails gerade nicht lesen.",
    "home_assistant": "Ich erreiche dein Zuhause gerade nicht.",
    "hermes": "Ich kann gerade nicht recherchieren.",
    "claude": "Der Architekt steht gerade nicht zur Verfuegung.",
    "codex": "Der Herausforderer steht gerade nicht zur Verfuegung.",
    "agent_runtime": "Ein Auftrag kommt gerade nicht weiter.",
    "cognition": "Ich ordne gerade schlechter ein, was du von mir moechtest.",
    "portal": "Ich komme gerade nicht in deine Portale.",
    "browser": "Ich kann gerade keine Webseiten lesen.",
    "scheduler": "Geplante Aufgaben laufen gerade nicht.",
    "gateway": "Freigaben erreichen dein iPhone gerade nicht.",
    "core": "SOLVIO selbst hat ein Problem.",
    "storage": "Deine Sicherung ist gerade nicht auf dem Stand, auf dem sie "
               "sein sollte.",
    "offsite": "Deine Sicherung ausserhalb des Hauses ist nicht nachweislich "
               "brauchbar — gegen Feuer, Wasser und Diebstahl zaehlt nur sie.",
    "vault": "Ich komme gerade nicht an deine hinterlegten Zugaenge.",
    "payment": "Ich kann fuer dich gerade nichts bezahlen.",
    # Ohne diese Zeile blieb `user_impact` leer, und der Brief des Arztes
    # bekam eine leere Befundliste — eine Meldung, die nichts sagt.
    "broker": "Recherche und Fachleute koennen gerade keine Anfragen stellen.",
}


def _impact(component: str) -> str:
    return _IMPACT.get(component, "")
