"""Eine Chronik — aus vorhandenen Aufzeichnungen, nicht aus einem neuen Journal.

Die Versuchung bei so einer Ansicht ist, ein eigenes Ereignisprotokoll
mitzuschreiben. Das waere eine zweite Wahrheit, und zwei Wahrheiten laufen
auseinander: irgendwann zeigt die Chronik einen Lauf, den es nicht gab, oder sie
verschweigt einen, den es gab. Also wird hier nichts aufgezeichnet, sondern nur
zusammengelesen, was ohnehin dauerhaft steht:

* Laeufe und Meldungen aus dem Hintergrundspeicher,
* Freigaben aus dem Kontrollpfad,
* was der Nutzer angelegt hat.

Was NICHT hineingehoert, ist genauso wichtig: keine Abrufticks, keine
Modellaufrufe, keine Wiederholungen desselben Zustands. Eine Chronik mit hundert
Zeilen „nachgesehen, nichts" ist unlesbar, und unlesbar heisst ungelesen.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("control")

#: Wie weit zurueck. Aelteres interessiert im Alltag niemanden.
DEFAULT_WINDOW = 7 * 86400
DEFAULT_LIMIT = 40


@dataclass
class Event:
    """Ein Ereignis, wie ein Mensch es lesen wuerde."""

    at: float
    kind: str
    title: str
    detail: str = ""
    #: Woran das haengt — damit die Ansicht dorthin springen kann.
    task_id: str = ""
    item_id: str = ""
    approval_id: str = ""
    #: gut | warnung | schlecht — fuer die Farbe, nicht fuer die Logik.
    tone: str = "gut"

    def as_dict(self) -> dict[str, Any]:
        entry = {"zeit": self.at, "art": self.kind, "titel": self.title[:160],
                 "ton": self.tone}
        if self.detail:
            entry["detail"] = self.detail[:200]
        for key, value in (("aufgabe", self.task_id), ("meldung", self.item_id),
                           ("freigabe", self.approval_id)):
            if value:
                entry[key] = value
        return entry


#: Laufzustaende, die eine Zeile wert sind — und wie sie heissen.
_RUN_WORDS = {
    "done": ("Aufgabe gelaufen", "gut"),
    "failed": ("Aufgabe fehlgeschlagen", "schlecht"),
    "approval_pending": ("Aufgabe wartet auf deine Freigabe", "warnung"),
    "skipped": ("Aufgabe übersprungen", "warnung"),
}

#: `no_change` steht ausdruecklich NICHT darin. „Nachgesehen, nichts Neues" ist
#: der haeufigste Ausgang einer Beobachtung; als Chronikzeile waere er reines
#: Rauschen und wuerde alles Wichtige verdecken.
_NOISE = frozenset({"no_change", "claimed"})

_APPROVAL_WORDS = {
    "PENDING": ("Freigabe angefragt", "warnung"),
    "APPROVED": ("Freigabe erteilt", "gut"),
    "CONSUMED": ("Freigabe erteilt und ausgeführt", "gut"),
    "DENIED": ("Freigabe abgelehnt", "warnung"),
    "EXPIRED": ("Freigabe verfallen", "warnung"),
    "FAILED": ("Nach der Freigabe fehlgeschlagen", "schlecht"),
    "EXECUTING": ("Wird gerade ausgeführt", "gut"),
}

#: Faehigkeitsnamen in Worte, die ein Mensch sagt.
_HUMAN = {
    "calendar_list_events": "Kalender gelesen",
    "calendar_search_events": "Kalender durchsucht",
    "gmail_list_recent": "E-Mails gelesen",
    "gmail_search": "E-Mails durchsucht",
    "gmail_send_draft": "E-Mail gesendet",
    "gmail_create_draft": "E-Mail-Entwurf angelegt",
    "ha_turn_on": "Gerät eingeschaltet",
    "ha_turn_off": "Gerät ausgeschaltet",
    "ha_set_brightness": "Helligkeit gesetzt",
    "portal_login": "Bei einem Portal angemeldet",
    "deep_research": "Recherche",
    "background_create": "Hintergrundaufgabe eingerichtet",
    "background_delete": "Hintergrundaufgabe gelöscht",
}


#: Komponenten in Worte. Bewusst dieselben wie in der Ueberwachung des Arztes —
#: zwei Namen fuer dasselbe Ding waeren eine zweite Wahrheit im Kleinen.
_COMPONENTS = {"cognition": "Die Einordnung", "agent_runtime": "Die Auftraege", "hermes": "Die Recherche", "portal": "Der Portal-Zugang",
               "browser": "Der Browser", "scheduler": "Der Hintergrund",
               "calendar": "Der Kalender", "gmail": "Die E-Mail",
               "home_assistant": "Dein Zuhause", "claude": "Der Architekt",
               "codex": "Der Herausforderer", "gateway": "Der Freigabeweg",
               "core": "SOLVIO", "storage": "Die Sicherung",
               "offsite": "Die Fernsicherung",
               "vault": "Der Tresor", "payment": "Die Zahlungen",
               "broker": "Die Anbieter-Vermittlung"}


def human(name: str) -> str:
    return _HUMAN.get(name, name.replace("_", " "))


async def timeline(*, proactive_store: Any = None, approval_db: str = "",
                   doctor_store: Any = None, agent_ledger: Any = None,
                   window: float = DEFAULT_WINDOW,
                   limit: int = DEFAULT_LIMIT,
                   now: float | None = None) -> list[Event]:
    """Die zusammengelesene Chronik, neueste zuerst."""
    moment = now if now is not None else time.time()
    since = moment - window
    events: list[Event] = []
    if proactive_store is not None:
        events.extend(await _from_proactive(proactive_store, since))
    if approval_db:
        events.extend(_from_approvals(approval_db, since))
    if doctor_store is not None:
        events.extend(await _from_doctor(doctor_store, since))
    if agent_ledger is not None:
        events.extend(_from_agent_runs(agent_ledger, since))
    events.sort(key=lambda e: e.at, reverse=True)
    return _deduplicate(events)[:limit]


#: Ereignisarten des Agentenbuchs in Worte. Was hier nicht steht, erscheint
#: nicht in der Chronik — eine Projektion zeigt weniger als ihre Quelle, nie mehr.
_AGENT_WORDS = {
    "state_changed": ("Auftrag", "gut"),
    "approval_requested": ("Freigabe angefragt", "warnung"),
    "approval_resolved": ("Freigabe entschieden", "gut"),
    "boundary_opened": ("Wartet auf dich", "warnung"),
    "boundary_resumed": ("Weiter nach deiner Handlung", "gut"),
    "budget_event": ("Grenze erreicht", "warnung"),
    "recovered": ("Nach Neustart abgeglichen", "warnung"),
    "notice_sent": ("Ergebnis gemeldet", "gut"),
}


def _from_agent_runs(ledger: Any, since: float) -> list[Event]:
    """Die Agentenlaufzeit in der Chronik — eine PROJEKTION, kein zweites Journal.

    Gelesen wird `agent_events`, und zwar nur lesend: dieser Leser schreibt
    nichts und legt nichts an. Er erfindet auch keine Zeile, die im Buch nicht
    steht — die Chronik zeigt weniger als ihre Quelle, nie mehr.

    Die Texte des Buchs sind bereits redigiert und gedeckelt (ADR-0029); hier
    wird nur uebersetzt, damit das Kontrollzentrum nichts ueber Laufzustaende
    wissen muss.
    """
    events: list[Event] = []
    try:
        rows = ledger.recent_events(limit=120)
    except Exception as exc:  # noqa: BLE001
        log.info("control.activity_agent_failed", kind=type(exc).__name__)
        return events
    for row in rows:
        if row.at < since:
            continue
        title, tone = _AGENT_WORDS.get(row.kind, ("Auftrag", "gut"))
        events.append(Event(row.at, "auftrag", f"{title}: {row.summary}"[:180],
                            task_id=row.run_id, tone=tone))
    return events


async def _from_proactive(store: Any, since: float) -> list[Event]:
    events: list[Event] = []
    try:
        tasks = await store.list_tasks()
    except Exception as exc:  # noqa: BLE001
        log.info("control.activity_tasks_failed", kind=type(exc).__name__)
        return events
    titles = {task.task_id: task.title for task in tasks}
    for task in tasks:
        if task.created_at >= since:
            events.append(Event(task.created_at, "aufgabe_angelegt",
                                f"Aufgabe eingerichtet: {task.title}",
                                task_id=task.task_id))
        try:
            runs = await store.runs_for(task.task_id, limit=25)
        except Exception:  # noqa: BLE001
            continue
        for run in runs:
            finished = run.get("finished_at") or run.get("claimed_at") or 0.0
            if finished < since:
                continue
            state = str(run.get("state", ""))
            if state in _NOISE:
                continue
            title, tone = _RUN_WORDS.get(state, (f"Lauf: {state}", "warnung"))
            events.append(Event(finished, "lauf", f"{title}: {task.title}",
                                detail=str(run.get("detail") or "")[:160],
                                task_id=task.task_id, tone=tone))
    try:
        for item in await store.unread(limit=30):
            if item.get("erstellt", 0.0) >= since:
                events.append(Event(item["erstellt"], "meldung",
                                    str(item.get("zusammenfassung", ""))[:140],
                                    task_id=str(item.get("aufgabe") or ""),
                                    item_id=item["id"], tone="gut"))
    except Exception:  # noqa: BLE001
        pass
    return events


async def _from_doctor(store: Any, since: float) -> list[Event]:
    """Was der Arzt getan hat — aus seiner Akte, nicht aus einem neuen Journal.

    Wichtig gerade fuer die leisen Faelle: eine Stoerung, die kurz war und
    behoben wurde, erzeugt bewusst KEINE Meldung im Posteingang — sie hat
    niemanden gestoert, und Selbstlob ist keine Auskunft. Nachvollziehbar muss
    sie trotzdem sein. Sonst heilt SOLVIO unsichtbar, und „unsichtbar geheilt"
    ist von „ist nie passiert" nicht zu unterscheiden. Genau darum steht sie
    hier und nicht dort.
    """
    events: list[Event] = []
    try:
        rows = await store.history(limit=60)
    except Exception as exc:  # noqa: BLE001 - eine fehlende Akte ist keine Stoerung
        log.info("control.doctor_history_failed", kind=type(exc).__name__)
        return events
    for row in rows:
        component = _COMPONENTS.get(row.get("component", ""),
                                    row.get("component", ""))
        resolved = row.get("resolved_at") or 0.0
        at = float(resolved or row.get("last_seen") or 0.0)
        if at < since:
            continue
        result = row.get("last_result") or ""
        attempts = int(row.get("attempts") or 0)
        if resolved and result == "wiederhergestellt":
            events.append(Event(
                at=at, kind="doctor", tone="gut",
                title=f"{component} wiederhergestellt",
                detail="SOLVIO hat das selbst behoben"))
        elif resolved:
            events.append(Event(
                at=at, kind="doctor", tone="gut",
                title=f"{component} ist wieder da",
                detail="hat sich von selbst erledigt"))
        elif attempts:
            events.append(Event(
                at=at, kind="doctor", tone="schlecht",
                title=f"{component}: Störung nicht behoben",
                detail=f"{attempts} Versuch(e), ohne Erfolg"))
        else:
            events.append(Event(
                at=at, kind="doctor", tone="warnung",
                title=f"{component}: Störung erkannt"))
    return events


def _from_approvals(path: str, since: float) -> list[Event]:
    """Freigaben — nur Zustand und Zeitpunkt, nie eine Signatur.

    Gelesen wird ausdruecklich schreibgeschuetzt: die Chronik ist ein Betrachter
    des Freigabepfads und darf ihn unter keinen Umstaenden veraendern.
    """
    events: list[Event] = []
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return events
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT approval_id, tool, state, created_at, decided_at
               FROM approval_requests
               WHERE COALESCE(decided_at, created_at) >= ?
               ORDER BY COALESCE(decided_at, created_at) DESC LIMIT 60""",
            (since,)).fetchall()
    except sqlite3.Error:
        return events
    finally:
        connection.close()
    for row in rows:
        state = str(row["state"])
        title, tone = _APPROVAL_WORDS.get(state, (f"Freigabe: {state}", "warnung"))
        events.append(Event(
            float(row["decided_at"] or row["created_at"]), "freigabe",
            f"{title} — {human(str(row['tool']))}",
            approval_id=str(row["approval_id"]), tone=tone))
    return events


def _deduplicate(events: list[Event]) -> list[Event]:
    """Wiederholungen desselben Vorgangs zusammenfassen.

    Eine Aufgabe, die alle fuenf Minuten laeuft, erzeugt an einem Tag 288 Zeilen
    „gelaufen". Der Nutzer will wissen, DASS sie laeuft, nicht 288 Mal wann.
    Deshalb ueberlebt je Vorgang und Stunde eine Zeile.
    """
    seen: set[tuple] = set()
    kept: list[Event] = []
    for event in events:
        bucket = (event.kind, event.task_id, event.title, int(event.at // 3600))
        if bucket in seen:
            continue
        seen.add(bucket)
        kept.append(event)
    return kept
