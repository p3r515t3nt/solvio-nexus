"""Nachsehen, wenn niemand hinsieht — und dabei still bleiben.

Die Versuchung bei einer Ueberwachung ist, alles zu melden. Das Ergebnis ist eine
Flut, in der die eine Meldung untergeht, auf die es ankommt. Deshalb gilt hier
die umgekehrte Regel: **gemeldet wird nur, was der Mensch wissen MUSS.**

Vier Faelle, sonst Schweigen:

* Etwas braucht ihn — ein Zugang ist abgelaufen, und nur er kann das.
* Eine Reparatur ist fehlgeschlagen.
* Eine Stoerung haelt an, ohne dass es ein Vorgehen gibt.
* Eine echte Stoerung wurde behoben — aber nur, wenn sie lange genug dauerte,
  um jemandem aufgefallen zu sein.

Der letzte Punkt ist der feinste: ein Dienst, der zwanzig Sekunden weg war und
wiederkam, hat niemanden gestoert. Das zu melden waere Selbstlob, keine Auskunft.

Die Kadenz ist bewusst traege. Der Arzt ist nicht die Gesundheitsanzeige — die
laeuft im Kontrollzentrum im Sekundentakt und kostet nichts. Hier geht es um
Eingriffe, und Eingriffe sollen selten sein.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

from solvio.control_center.health import State
from solvio.doctor import playbooks as P
from solvio.logging_setup import get_logger

log = get_logger("doctor")

#: Wie oft nachgesehen wird. Eine Minute reicht: schneller waere blosse Unruhe,
#: und das Kontrollzentrum zeigt den Zustand ohnehin sofort.
TICK_SECONDS = 60.0

#: Wie lange eine Stoerung dauern muss, bevor sie eine Meldung wert ist —
#: auch dann, wenn sie behoben wurde.
WORTH_TELLING = 180.0

#: Wie lange nach einer Meldung ueber dieselbe Komponente geschwiegen wird.
QUIET_SECONDS = 6 * 3600.0


class Supervisor:
    """Laesst den Arzt in Ruhe seine Runde machen."""

    def __init__(self, doctor: Any, *, store: Any = None,
                 tick: float = TICK_SECONDS, clock=None) -> None:
        self.doctor = doctor
        #: Der Posteingang des Hintergrunds — dieselbe Ablage wie fuer geplante
        #: Aufgaben. Ein eigener waere eine zweite Stelle, an der man nachsehen
        #: muesste.
        self.store = store
        self.tick = tick
        self.clock = clock or time.time
        self._task: asyncio.Task | None = None
        self._told: dict[str, float] = {}
        self.rounds = 0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        log.info("doctor.supervisor_started", tick=self.tick)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        log.info("doctor.supervisor_stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.tick)
                await self.round()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - die Runde stirbt nie
                log.error("doctor.round_failed", kind=type(exc).__name__)

    async def round(self) -> list[dict[str, Any]]:
        """Eine Runde: sehen, wo noetig handeln, selten sprechen."""
        self.rounds += 1
        reported: list[dict[str, Any]] = []
        for diagnosis in await self.doctor.diagnose_all():
            component = diagnosis.component
            incident = self.doctor._incidents.get(component)
            outage = (self.clock() - incident.first_seen) if incident else 0.0

            if not diagnosis.repair_available:
                # Nichts zu tun — aber vielleicht etwas zu sagen.
                if diagnosis.human_action_required or outage >= WORTH_TELLING:
                    told = await self._tell(diagnosis, attempt=None, outage=outage)
                    if told:
                        reported.append(told)
                continue

            attempt = await self.doctor.repair(diagnosis)
            if attempt.recovered and outage < WORTH_TELLING:
                # Kurz weg, gleich wieder da. Das ist kein Ereignis.
                log.info("doctor.quiet_recovery", component=component,
                         outage=int(outage))
                continue
            told = await self._tell(diagnosis, attempt=attempt, outage=outage)
            if told:
                reported.append(told)
        return reported

    async def _tell(self, diagnosis: Any, *, attempt: Any,
                    outage: float) -> dict[str, Any] | None:
        """Legt eine Meldung ab — hoechstens eine je Komponente und Ruhezeit."""
        component = diagnosis.component
        last = self._told.get(component, 0.0)
        if self.clock() - last < QUIET_SECONDS:
            log.info("doctor.suppressed_duplicate", component=component)
            return None
        if self.store is None:
            return None

        label = _label(diagnosis)
        if attempt is not None and attempt.recovered:
            summary = (f"{label} war {_duration(outage)} nicht erreichbar "
                       f"und wurde wiederhergestellt.")
            priority = "normal"
        elif attempt is not None:
            summary = (f"{label} ist nicht erreichbar. Ich habe es versucht, "
                       f"aber es hat nicht geholfen.")
            priority = "wichtig"
        elif diagnosis.human_action_required:
            summary = f"{label}: {diagnosis.probable_cause}"
            priority = "wichtig"
        else:
            summary = (f"{label} ist seit {_duration(outage)} nicht erreichbar. "
                       f"Dafuer habe ich kein Mittel.")
            priority = "wichtig"

        findings = [diagnosis.user_impact] if diagnosis.user_impact else []
        if diagnosis.probable_cause and not diagnosis.human_action_required:
            findings.append(diagnosis.probable_cause)
        # Der Fingerabdruck traegt das Ruhefenster, und `task_id` ist LEER statt
        # `None`. Beides zusammen ist die eigentliche Sperre:
        #
        # SQLite haelt NULL-Werte in einer UNIQUE-Bedingung fuer verschieden.
        # Mit `task_id = None` konnte `UNIQUE(task_id, fingerprint)` also nie
        # greifen — gemessen: derselbe Fingerabdruck lag zweimal in der Ablage.
        # Die einzige Bremse war die Ruhezeit im Arbeitsspeicher, und die stirbt
        # mit jedem Neustart des Core. Genau so entstanden doppelte Meldungen.
        #
        # Das Fenster im Fingerabdruck sorgt dafuer, dass die Sperre wieder
        # loest: dieselbe Stoerung naechste Woche ist eine neue Meldung. Faellt
        # eine Stoerung genau auf eine Fenstergrenze, kann sie zweimal gemeldet
        # werden — das ist der Preis dafuer, dass sie ueberhaupt je wieder
        # gemeldet wird, und die Ruhezeit im Arbeitsspeicher faengt den Fall im
        # laufenden Betrieb ohnehin ab.
        window = int(self.clock() // QUIET_SECONDS)
        item = {
            "notification_id": "dn-" + hashlib.sha256(
                f"{component}|{window}".encode()).hexdigest()[:20],
            "task_id": "", "run_id": None, "created_at": self.clock(),
            "priority": priority, "summary": summary, "findings": findings,
            "source_capability": "doctor", "content_trust": "",
            "fingerprint": f"doctor:{component}:{diagnosis.repair.value}:{window}",
        }
        fresh = await self.store.add_item(item)
        self._told[component] = self.clock()
        log.info("doctor.reported" if fresh else "doctor.suppressed_duplicate",
                 component=component, priority=priority)
        return item if fresh else None


#: Namen, wie der Mensch sie kennt — dieselben wie im Kontrollzentrum.
_LABELS = {"cognition": "Die Einordnung", "agent_runtime": "Die Auftraege", "hermes": "Die Recherche", "portal": "Der Portal-Zugang",
           "browser": "Der Browser", "scheduler": "Der Hintergrund",
           "calendar": "Der Kalender", "gmail": "Die E-Mail",
           "home_assistant": "Dein Zuhause", "claude": "Der Architekt",
           "codex": "Der Herausforderer", "gateway": "Der Freigabeweg",
           "core": "SOLVIO", "storage": "Die Sicherung",
           "offsite": "Die Fernsicherung",
           "vault": "Der Tresor", "payment": "Die Zahlungen",
           "broker": "Die Anbieter-Vermittlung"}


def _label(diagnosis: Any) -> str:
    return _LABELS.get(diagnosis.component, diagnosis.component)


def _duration(seconds: float) -> str:
    """Eine Dauer, wie man sie sagt. „412 Sekunden" sagt niemand."""
    if seconds < 120:
        return "kurz"
    minutes = int(seconds // 60)
    if minutes < 90:
        return f"{minutes} Minuten"
    hours = minutes / 60
    return f"{hours:.0f} Stunden"
