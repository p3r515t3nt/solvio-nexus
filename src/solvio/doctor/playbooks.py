"""Was SOLVIO selbst reparieren darf — eine geschlossene, Core-eigene Liste.

Die entscheidende Entscheidung steht in der Bauform, nicht in einer Regel: ein
Vorgehen ist eine **registrierte Funktion**, keine Zeichenkette. Es gibt in
diesem ganzen Modul keinen Ort, an dem aus Text ein Befehl wuerde. Ein Modell
darf beim Diagnostizieren helfen; es kann kein Vorgehen hinzufuegen, weil
Hinzufuegen bedeutet, Python-Code in dieser Datei zu schreiben.

Die Auswahlregel ist dieselbe wie beim Kontrollzentrum, und aus demselben Grund:
zugelassen ist, was **oertlich, umkehrbar und auf SOLVIO-eigene, kurzlebige
Laufzeit beschraenkt** ist und keine Aussenwirkung hat. Einen eigenen
Kindprozess neu zu starten faellt darunter — er gehoert SOLVIO, er ist ohnehin
wegwerfbar, und nach dem Neustart ist der Zustand derselbe wie vorher.

Was ausdruecklich NICHT darunter faellt und deshalb hier fehlt: Zugangsdaten
anfassen, Pakete installieren, Dateien ausserhalb der eigenen Laufzeit loeschen,
etwas an einem fremden Konto aendern. Fuer all das gibt es kein Vorgehen — und
„kein Vorgehen" heisst hier `UNSUPPORTED_REPAIR` und nicht „dann eben von Hand
etwas ausdenken".

Jedes Vorgehen bringt seine eigene Nachpruefung mit. Ein Neustart, der mit Code 0
endet, ist kein Beweis: bewiesen ist es erst, wenn die Komponente wieder gesund
antwortet.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from solvio.doctor.diagnosis import RepairClass
from solvio.logging_setup import get_logger

log = get_logger("doctor")

#: Wie lange ein einzelner Reparaturversuch dauern darf.
REPAIR_TIMEOUT = 120.0

#: Wie lange nach einer Reparatur gewartet wird, bevor nachgesehen wird.
#: Ein Kindprozess braucht einen Moment, bis er antwortet — sofort zu pruefen
#: hiesse, jeden geglueckten Neustart als Fehlschlag zu melden.
SETTLE_SECONDS = 6.0


@dataclass(frozen=True)
class Playbook:
    """Ein zugelassenes Vorgehen. Die Handlung ist Code, nie Text."""

    key: str
    component: str
    #: Was es tut, in Worten des Nutzers — steht spaeter in der Chronik.
    title: str
    repair: RepairClass
    #: Die Handlung. Eine Funktion aus DIESER Datei, kein uebergebener Aufruf.
    action: Callable[[Any], Awaitable[bool]]
    #: Was danach gelten muss. In Worten, fuer den Bericht.
    verification: str
    #: Ob das ohne Rueckfrage laufen darf. Bei allem, was hier steht: ja,
    #: begruendet durch oertlich + umkehrbar + eigene Laufzeit.
    autonomous: bool = True
    #: Wie oft in einem Zeitfenster hoechstens.
    max_attempts: int = 2
    cooldown: float = 600.0


async def _restart_hermes(dispatcher: Any) -> bool:
    """Den tiefen Ausfuehrenden neu starten — ueber seinen eigenen Lebenszyklus.

    Kein `kill`, kein Signal von aussen: der Dienst weiss selbst, wie er
    aufhoert und wieder anfaengt, samt Gefaengnis, Umgebung und Haltungspruefung.
    Ihn daran vorbei zu starten hiesse, genau die Isolation zu umgehen, die
    teuer erkauft ist.
    """
    service = getattr(dispatcher, "deep_service", None)
    if service is None:
        return False
    try:
        await service.stop()
    except Exception as exc:  # noqa: BLE001 - ein Stoppfehler darf den Start nicht verhindern
        log.info("doctor.hermes_stop_failed", kind=type(exc).__name__)
    runtime = await service.start()
    from solvio.tools.registry import attach_deep_runtime
    attach_deep_runtime(dispatcher, runtime)
    log.info("doctor.hermes_restarted")
    return True


async def _restart_portal_worker(dispatcher: Any) -> bool:
    """Den Portal-Arbeiter neu starten — ueber den Core-eigenen Vorgang.

    Der Arbeiter hat dafuer eine eigene Anweisung, die nur der Core senden darf,
    und er prueft beim Wiederkommen seinen Bauzustand selbst. Genau deshalb ist
    das hier zugelassen: es gibt nichts zu erraten und nichts zu erzwingen.
    """
    from solvio.portal.client import PortalClient
    reply = await PortalClient().restart()
    log.info("doctor.portal_restart_requested", ok=bool(reply.get("ok")))
    return bool(reply.get("ok"))


async def _recycle_browser(dispatcher: Any) -> bool:
    """Den Browser wegwerfen. Er ist dafuer gebaut.

    Jede Sitzung bekommt ohnehin ein frisches Wegwerfprofil; ihn zu beenden
    verliert nichts, was jemandem gehoert.
    """
    browser = getattr(dispatcher, "browser", None)
    if browser is None:
        return False
    try:
        await browser.stop()
    except Exception as exc:  # noqa: BLE001
        log.info("doctor.browser_stop_failed", kind=type(exc).__name__)
        return False
    log.info("doctor.browser_recycled")
    return True


async def _restart_scheduler(dispatcher: Any) -> bool:
    """Die Hintergrundschleife neu aufsetzen.

    Der Bestand liegt in SQLite, nicht in der Schleife — sie neu zu starten
    verliert keine Aufgabe und keine Gelegenheit. `recover()` raeumt dabei auf,
    was beim Abbruch offen blieb.
    """
    scheduler = getattr(dispatcher, "scheduler", None)
    if scheduler is None:
        return False
    await scheduler.stop()
    await scheduler.start()
    log.info("doctor.scheduler_restarted")
    return True


async def _reset_backoff(dispatcher: Any) -> bool:
    """Den Rueckzug einer Hintergrundaufgabe aufheben.

    Nur sinnvoll, wenn eine frische Pruefung bewiesen hat, dass der Anbieter
    wieder antwortet — sonst waere es blosses Draengeln. Der Aufrufer stellt das
    sicher; hier wird nur der Zaehler zurueckgesetzt.
    """
    store = getattr(dispatcher, "proactive_store", None)
    if store is None:
        return False
    changed = 0
    for task in await store.list_tasks():
        if task.retry_after or task.consecutive_failures:
            task.retry_after = None
            task.consecutive_failures = 0
            await store.put_task(task)
            changed += 1
    log.info("doctor.backoff_cleared", tasks=changed)
    return True


#: Zwei Dinge, die ausdruecklich NICHT hier stehen — und warum.
#:
#: **Der Freigabe-Gateway.** `ApproverRuntime.stop()` raeumt den HTTP-Server ab,
#: laesst aber Speicher und Koordinator stehen; ein zweiter `start()` baut den
#: Approver im Arbeitsspeicher NEU auf. Jede gerade laufende Freigabe waere
#: damit still gegenstandslos, und die Waisenbereinigung liefe anschliessend
#: gegen Anfragen, die dieser Prozess selbst erzeugt hat. Ein Neustart, der
#: unbemerkt eine erteilte Zustimmung kassiert, ist keine Reparatur.
#:
#: **Der Core selbst.** `KeepAlive=true` bedeutet, dass jedes Beenden ein
#: Neustart ist — die Versuchung ist also gross. Aber dieser Prozess haelt die
#: bestaetigten Entscheidungen, den Treiber des Recherche-Journals und jede
#: offene Sprachsitzung. Ein Arzt, der seinen eigenen Patienten erschiesst,
#: damit er neu geboren wird, heilt nichts.
#:
#: Beides ist keine Nachlaessigkeit, sondern eine Entscheidung. Wer eines davon
#: spaeter hinzufuegen will, soll erst diesen Absatz widerlegen.
#: Was NIE automatisch neu gestartet wird.
#:
#: `payment` steht hier, obwohl es gar keinen Dienst gibt, den man neu starten
#: koennte — und genau deshalb. Eine haengende Zahlung wird nachgeschlagen, nie
#: neu angestossen; ein Neustart als Mittel waere die eine Handlung, die aus
#: einem unklaren Ausgang zwei Belastungen machen kann. Das als Verbot
#: hinzuschreiben ist belastbarer, als sich darauf zu verlassen, dass niemand
#: spaeter ein Playbook dafuer erfindet.
#:
#: `broker` steht aus demselben Grund hier. Der Provider Broker haelt seine
#: Registratur ausschliesslich im Arbeitsspeicher: ein Alleinneustart leerte sie,
#: waehrend alle vier Kaefig-`.env` ihre alten Token behielten — und niemand
#: praegt sie nach. Deep und die drei Bots bekaemen ab da `401`, und zwar
#: dauerhaft. Der Broker startet mit dem Core; ein belegter Port ist ein Fehler,
#: den der Arzt melden, aber nicht wegstarten kann.
#:
#: `offsite` steht hier, weil es fuer die Sicherung ausserhalb des Hauses
#: ueberhaupt KEINE automatische Reparatur geben darf (Vertrag §12). Jede
#: denkbare zerfaellt in zwei Sorten, und beide sind verboten: entweder sie
#: ist Menschensache (ein gesperrter Schluesselbund, ein fehlender
#: Anbieterzugang — daran kann kein Programm etwas aendern), oder sie ist
#: gefaehrlich (loeschen, neu schluesseln, eine Generation ersetzen). Dazu
#: kommt die harte Grenze: SOLVIO hat beim Anbieter nachweislich kein
#: Loeschrecht — ein „Aufraeum"-Playbook koennte gar nicht funktionieren und
#: wuerde nur einen roten Punkt gegen eine falsche Hoffnung tauschen. Der Arzt
#: meldet hier; heilen kann er nicht, und das ist die richtige Arbeitsteilung.
FORBIDDEN_RESTARTS = ("gateway", "core", "approver", "payment", "broker",
                      "offsite")


#: Die geschlossene Liste. Wer etwas hinzufuegen will, schreibt Code hier —
#: und das ist Absicht.
async def _agent_reconcile(dispatcher: Any) -> bool:
    """Der Neustart-Abgleich der Agentenlaufzeit als oertliche, umkehrbare Reparatur.

    Genau das, was beim Start ohnehin passiert: nicht-terminale Laeufe als
    unterbrochen markieren, verwaiste Arbeitsbereiche terminaler Laeufe
    entfernen, verifizierte Waisenprozesse beenden. Alles davon ist idempotent.

    Was der Arzt hier ausdruecklich NICHT tut: einen aktiven Lauf abbrechen (das
    ist eine Nutzerhandlung), eine Freigabe erteilen (die gibt es nur am
    Freigabeweg), oder einen ungewissen Ausgang wiederholen. Ein Prozess wird
    nur beendet, wenn pgid UND Startzeit UND Programmpfad zusammenpassen — eine
    PID ist kein Besitztitel.
    """
    runtime = getattr(dispatcher, "agent_runtime", None)
    if runtime is None:
        return False
    report = await runtime.reconcile()
    log.info("doctor.agent_reconciled",
             interrupted=len(report.get("interrupted") or []),
             killed=len(report.get("killed") or []),
             reported=len(report.get("reported") or []))
    return True


PLAYBOOKS: dict[str, Playbook] = {
    "agent_reconcile": Playbook(
        key="agent_reconcile", component="agent_runtime",
        title="Agentenauftraege abgeglichen",
        repair=RepairClass.RETRY, action=_agent_reconcile,
        verification="Unterbrochene Laeufe markiert, verwaiste Bereiche entfernt"),
    "hermes_restart": Playbook(
        key="hermes_restart", component="hermes",
        title="Recherche neu gestartet",
        repair=RepairClass.RESTART_COMPONENT, action=_restart_hermes,
        verification="Recherche antwortet wieder"),
    "portal_restart": Playbook(
        key="portal_restart", component="portal",
        title="Portal-Arbeiter neu gestartet",
        repair=RepairClass.RESTART_COMPONENT, action=_restart_portal_worker,
        verification="Arbeiter erreichbar und Bauzustand geprueft"),
    "browser_recycle": Playbook(
        key="browser_recycle", component="browser",
        title="Browser neu aufgesetzt",
        repair=RepairClass.RESTART_COMPONENT, action=_recycle_browser,
        verification="Browser wieder bereit"),
    "scheduler_restart": Playbook(
        key="scheduler_restart", component="scheduler",
        title="Hintergrund neu gestartet",
        repair=RepairClass.RESTART_COMPONENT, action=_restart_scheduler,
        verification="Hintergrund laeuft wieder"),
    "backoff_reset": Playbook(
        key="backoff_reset", component="scheduler",
        title="Wartezeiten aufgehoben",
        repair=RepairClass.RETRY, action=_reset_backoff,
        verification="Aufgaben sind wieder faellig", cooldown=1800.0),
}


def for_component(component: str) -> list[Playbook]:
    """Die Vorgehen dieser Komponente — und fuer die Gesperrten: keine.

    Die zweite Bedingung ist nicht ueberfluessig, obwohl in `PLAYBOOKS` ohnehin
    nichts fuer sie steht: sie macht die Sperre zu einer Aussage im Code statt
    zu einer Abwesenheit, die niemandem auffaellt.
    """
    if component in FORBIDDEN_RESTARTS:
        return []
    return [p for p in PLAYBOOKS.values() if p.component == component]


def get(key: str) -> Playbook | None:
    """Ein Vorgehen holen — oder `None`.

    Es gibt bewusst keinen Weg, hier etwas zu erzeugen, das nicht schon in
    `PLAYBOOKS` steht. Ein `KeyError` waere ein Absturz; `None` ist eine
    Antwort, und der Aufrufer muss sie behandeln.
    """
    return PLAYBOOKS.get(key)


@dataclass
class Attempt:
    """Was ein Reparaturversuch ergeben hat."""

    playbook: str
    component: str
    started: float
    ran: bool = False
    #: Ob die Komponente DANACH wirklich gesund war. Das ist der Beweis.
    recovered: bool = False
    outcome: str = ""
    detail: str = ""
    elapsed: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"vorgehen": self.playbook, "komponente": self.component,
                "ausgefuehrt": self.ran, "wiederhergestellt": self.recovered,
                "ergebnis": self.outcome, "detail": self.detail[:200],
                "dauer_s": round(self.elapsed, 1), "zeit": self.started}


#: Die moeglichen Ausgaenge. „Ausgefuehrt" ist keiner davon — ein Vorgehen, das
#: durchlief, ohne dass die Komponente gesund wurde, ist NICHT geglueckt.
RECOVERED = "wiederhergestellt"
REPAIR_FAILED = "reparatur_fehlgeschlagen"
INCONCLUSIVE = "unklar"
