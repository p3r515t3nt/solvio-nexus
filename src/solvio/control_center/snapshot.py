"""Was das iPhone zu sehen bekommt — und was es tun darf.

Zwei Grundsaetze, die das ganze Modul bestimmen.

**Eine Wahrheit.** Hier wird nichts gespeichert. Jede Zahl kommt in dem Moment
aus dem Speicher, aus dem sie ohnehin stammt: Aufgaben aus dem Hintergrundspeicher,
Freigaben aus dem Kontrollpfad, Gesundheit vom Brett. Ein Zwischenspeicher waere
eine zweite Wahrheit, und zwei Wahrheiten weichen voneinander ab — meistens genau
dann, wenn jemand hinsieht.

**Der Tipp ist kein Modellaufruf.** Wenn der Besitzer auf seinem angemeldeten
Geraet „Pause" tippt, ist das eine Handlung des Menschen, keine Bitte eines
Modells. Sie geht deshalb nicht durch den Faehigkeits-Router — dort wuerde die
Herkunftsregel sie zu Recht anheben, weil dort Argumente von einem Modell kommen
koennten. Sie geht direkt an SOLVIOs eigene Buchhaltung.

Das ist ausdruecklich **keine Abkuerzung an der Freigabe vorbei**, und der
Unterschied ist scharf: hier passiert nichts ausserhalb von SOLVIO. Eine Aufgabe
zu pausieren aendert kein Geraet, verschickt keine Nachricht, faesst kein fremdes
System an. Was die Aufgabe spaeter TUT, wird beim Ausfuehren erneut bewertet —
und ein schreibender Schritt landet dann auf demselben iPhone wie immer.
"""
from __future__ import annotations

import re
import time
from typing import Any

from solvio.control_center import activity as A
from solvio.control_center.health import NOT_WELL, HealthBoard, State
from solvio.logging_setup import get_logger
from solvio.proactive import store as S
from solvio.proactive.schedule import Kind, Schedule

log = get_logger("control")

#: Was eine einzelne Ansicht hoechstens liefert. Ein Bildschirm zeigt nicht
#: hundert Zeilen, und hundert Zeilen zu uebertragen kostet nur Zeit.
MAX_TASKS = 50
MAX_ITEMS = 30
MAX_EVENTS = 40

#: Wochentage in Worten — die Ansicht soll nicht rechnen muessen.
_TAGE = ("montags", "dienstags", "mittwochs", "donnerstags", "freitags",
         "samstags", "sonntags")


def describe_schedule(raw: dict[str, Any]) -> str:
    """Ein Zeitplan als Satz. „every_seconds=900" ist keine Auskunft."""
    try:
        plan = Schedule.from_dict(raw)
    except Exception:  # noqa: BLE001
        return "Zeitplan unklar"
    if plan.kind is Kind.ONE_SHOT:
        return "einmalig"
    clock = f"{plan.hour:02d}:{plan.minute:02d}"
    if plan.kind is Kind.DAILY:
        return f"täglich um {clock}"
    if plan.kind is Kind.WEEKLY:
        days = ", ".join(_TAGE[d] for d in plan.weekdays if 0 <= d <= 6)
        return f"{days} um {clock}"
    minutes = plan.every_seconds // 60
    if minutes % 60 == 0 and minutes >= 60:
        return f"alle {minutes // 60} Stunden"
    return f"alle {minutes} Minuten"


#: Aufgabenzustaende in Worten.
_ZUSTAND = {S.ACTIVE: "aktiv", S.PAUSED: "pausiert", S.EXPIRED: "abgelaufen",
            S.COMPLETED: "erledigt"}


class ControlCenter:
    """Liest Core-Wahrheit zusammen und fuehrt direkte Besitzerhandlungen aus."""

    def __init__(self, dispatcher: Any, board: HealthBoard, *,
                 approval_db: str = "", doctor: Any = None, clock=None) -> None:
        self.dispatcher = dispatcher
        self.board = board
        self.approval_db = approval_db
        #: Der Arzt. Bleibt `None`, solange nichts verdrahtet ist — dann zeigt
        #: das Kontrollzentrum weiterhin Zustaende, nur eben keine Ursachen.
        self.doctor = doctor
        self.clock = clock or time.time

    # -- Lesen ---------------------------------------------------------------

    @property
    def store(self) -> Any:
        return getattr(self.dispatcher, "proactive_store", None)

    async def overview(self) -> dict[str, Any]:
        """Der eine Bildschirm, der die Frage „ist alles gut?" beantwortet."""
        await self.board.refresh()
        health = self.board.summary()
        tasks = await self._tasks()
        unread = 0
        store = self.store
        if store is not None:
            try:
                unread = await store.unread_count()
            except Exception:  # noqa: BLE001
                unread = 0
        pending = await self._pending_approvals()

        active = [t for t in tasks if t["zustand"] == "aktiv"]
        troubled = [t for t in tasks if t.get("fehlschlaege", 0) > 0]
        upcoming = sorted((t["naechster_lauf"] for t in active
                           if t.get("naechster_lauf")), key=float)
        attention: list[str] = []
        if pending:
            attention.append(f"{len(pending)} Freigabe{'n' if len(pending) > 1 else ''} "
                             f"wartet" if len(pending) == 1
                             else f"{len(pending)} Freigaben warten")
        for key in health["braucht_dich"]:
            component = self.board.component(key)
            if component is not None:
                attention.append(f"{component.label}: {component.reason}")
        if troubled:
            attention.append(f"{len(troubled)} Aufgabe(n) mit Fehlern")

        return {
            "stand": self.clock(),
            "gesundheit": health,
            "aufmerksamkeit": attention[:4],
            "ungelesen": unread,
            "aufgaben_aktiv": len(active),
            "aufgaben_gesamt": len(tasks),
            "naechster_lauf": upcoming[0] if upcoming else None,
            "freigaben_offen": len(pending),
            "freigabe_id": pending[0] if pending else "",
        }

    async def tasks(self) -> dict[str, Any]:
        return {"aufgaben": await self._tasks(), "stand": self.clock()}

    async def task(self, task_id: str) -> dict[str, Any] | None:
        store = self.store
        if store is None:
            return None
        found = await store.get_task(task_id)
        if found is None:
            return None
        runs = await store.runs_for(task_id, limit=8)
        entry = self._task_entry(found)
        entry["laeufe"] = [
            {"zeit": r.get("finished_at") or r.get("claimed_at"),
             "zustand": r.get("state"), "detail": str(r.get("detail") or "")[:160]}
            for r in runs]
        return entry

    async def inbox(self, limit: int = 20) -> dict[str, Any]:
        store = self.store
        if store is None:
            return {"meldungen": [], "ungelesen": 0}
        items = await store.unread(min(limit, MAX_ITEMS))
        return {"meldungen": [self._item_entry(i) for i in items],
                "ungelesen": await store.unread_count(), "stand": self.clock()}

    async def item(self, item_id: str) -> dict[str, Any] | None:
        store = self.store
        if store is None:
            return None
        found = await store.get_item(item_id)
        return self._item_entry(found, full=True) if found else None

    async def activity(self, limit: int = MAX_EVENTS) -> dict[str, Any]:
        events = await A.timeline(proactive_store=self.store,
                                  approval_db=self.approval_db,
                                  doctor_store=getattr(self.doctor, "store", None),
                                  limit=min(limit, MAX_EVENTS), now=self.clock())
        return {"ereignisse": [e.as_dict() for e in events], "stand": self.clock()}

    async def system(self) -> dict[str, Any]:
        """Die Komponenten — und wo es ein Vorgehen gibt, auch das.

        `reparierbar` sagt ausdruecklich NICHT „das laesst sich schon irgendwie
        richten", sondern „dafuer ist ein Vorgehen hinterlegt". Ohne diese
        Trennung zeigt die Oberflaeche einen Knopf, hinter dem nichts liegt —
        und das ist schlimmer als gar kein Knopf.
        """
        await self.board.refresh()
        components = []
        for component in self.board.known():
            entry = component.as_dict()
            entry["reparierbar"] = self._has_playbook(component.key,
                                                      component.state)
            components.append(entry)
        return {"komponenten": components,
                "zusammenfassung": self.board.summary(), "stand": self.clock()}

    def _has_playbook(self, component: str, state: Any) -> bool:
        from solvio.control_center.health import State as _State
        if state is _State.HEALTHY or self.doctor is None:
            return False
        # Ein abgelaufener Zugang ist nie „reparierbar" — dafuer braucht es
        # einen Menschen, und ein Knopf daneben waere ein Versprechen.
        if state is _State.AUTH_REQUIRED:
            return False
        from solvio.doctor import playbooks as _P
        return bool(_P.for_component(component))

    # -- Der Arzt --------------------------------------------------------------

    async def diagnose(self, component: str) -> dict[str, Any] | None:
        if self.doctor is None:
            return None
        return (await self.doctor.diagnose(component)).as_dict()

    async def repair(self, component: str) -> dict[str, Any]:
        """Ein ausdruecklicher Tipp des Besitzers auf EINE Komponente.

        Der Befund wird frisch gestellt, nicht aus der Anzeige uebernommen:
        zwischen dem Anschauen und dem Tippen koennen Minuten liegen, und ein
        Vorgehen soll zu dem passen, was JETZT ist.
        """
        if self.doctor is None:
            return {"ok": False, "grund": "kein_arzt"}
        diagnosis, attempt = await self.doctor.heal(component,
                                                    requested_by_user=True)
        entry: dict[str, Any] = {"ok": True, "befund": diagnosis.as_dict()}
        if attempt is None:
            entry["ok"] = False
            entry["grund"] = ("braucht_dich" if diagnosis.human_action_required
                              else "kein_vorgehen")
        else:
            entry["versuch"] = attempt.as_dict()
            entry["ok"] = attempt.recovered
            if not attempt.recovered:
                entry["grund"] = attempt.outcome
        return entry

    async def diagnoses(self) -> dict[str, Any]:
        if self.doctor is None:
            return {"befunde": [], "stand": self.clock()}
        found = await self.doctor.diagnose_all()
        return {"befunde": [d.as_dict() for d in found],
                "vorfaelle": [i.as_dict() for i in self.doctor.incidents()][:10],
                "stand": self.clock()}

    async def running(self) -> dict[str, Any]:
        """Was gerade wirklich laeuft. Nichts erfunden, nichts geschaetzt."""
        scheduler = getattr(self.dispatcher, "scheduler", None)
        active = sorted(getattr(scheduler, "_running", set())) if scheduler else []
        names = {}
        store = self.store
        if store is not None:
            for task_id in active:
                found = await store.get_task(task_id)
                if found is not None:
                    names[task_id] = found.title
        return {"laeuft": [{"aufgabe": t, "titel": names.get(t, t)} for t in active],
                "stand": self.clock()}

    # -- Direkte Besitzerhandlungen ------------------------------------------

    async def pause(self, task_id: str) -> dict[str, Any]:
        return await self._set_enabled(task_id, False)

    async def resume(self, task_id: str) -> dict[str, Any]:
        return await self._set_enabled(task_id, True)

    async def run_now(self, task_id: str) -> dict[str, Any]:
        """Zieht die naechste Faelligkeit vor — mehr nicht.

        Der Lauf selbst geht danach den gewoehnlichen Weg. Ist die Aktion
        schreibend, landet sie wie immer als Freigabe auf dem iPhone; hier wird
        nichts vorweggenommen.
        """
        store = self.store
        task = await store.get_task(task_id) if store else None
        if task is None:
            return {"ok": False, "grund": "unbekannte_aufgabe"}
        if not task.enabled:
            return {"ok": False, "grund": "pausiert"}
        task.next_run_at = self.clock()
        task.retry_after = None
        await store.put_task(task)
        log.info("control.task_run_now", task=task_id)
        return {"ok": True, "id": task_id, "faellig": True}

    async def delete(self, task_id: str) -> dict[str, Any]:
        store = self.store
        if store is None:
            return {"ok": False, "grund": "kein_speicher"}
        # Erst nachsehen, dann loeschen: so kann die Antwort sagen, WAS weg ist,
        # statt nur „erledigt". Ein Loeschen ohne Namen ist schwer zu pruefen.
        task = await store.get_task(task_id)
        if task is None:
            return {"ok": False, "grund": "unbekannte_aufgabe"}
        removed = await store.delete_task(task_id)
        log.info("control.task_deleted", task=task_id)
        return {"ok": removed, "id": task_id, "titel": task.title}

    async def mark_read(self, item_id: str) -> dict[str, Any]:
        store = self.store
        if store is None:
            return {"ok": False, "grund": "kein_speicher"}
        marked = await store.mark_read(item_id)
        log.info("control.item_read", item=item_id, changed=marked)
        return {"ok": True, "id": item_id, "geaendert": marked,
                "ungelesen": await store.unread_count()}

    async def _set_enabled(self, task_id: str, enabled: bool) -> dict[str, Any]:
        store = self.store
        task = await store.get_task(task_id) if store else None
        if task is None:
            return {"ok": False, "grund": "unbekannte_aufgabe"}
        task.enabled = enabled
        task.state = S.ACTIVE if enabled else S.PAUSED
        if enabled:
            task.consecutive_failures = 0
            task.retry_after = None
            plan = Schedule.from_dict(task.schedule)
            task.next_run_at = plan.next_after(self.clock())
            if task.next_run_at is None:
                task.state = S.EXPIRED
        await store.put_task(task)
        log.info("control.task_paused" if not enabled else "control.task_resumed",
                 task=task_id)
        return {"ok": True, "id": task_id, "zustand": _ZUSTAND.get(task.state,
                                                                   task.state),
                "aktiv": task.enabled, "naechster_lauf": task.next_run_at}

    # -- Formen ---------------------------------------------------------------

    async def _tasks(self) -> list[dict[str, Any]]:
        store = self.store
        if store is None:
            return []
        try:
            found = await store.list_tasks()
        except Exception as exc:  # noqa: BLE001
            log.info("control.tasks_failed", kind=type(exc).__name__)
            return []
        return [self._task_entry(t) for t in found[:MAX_TASKS]]

    def _task_entry(self, task: Any) -> dict[str, Any]:
        action = task.action or {}
        what = (A.human(str(action.get("capability", "")))
                if action.get("kind") != "research" else "Recherche")
        what = {"tagesueberblick": "Tagesüberblick", "mail_antwort_pruefen": "Auf Mailantwort prüfen"}.get(action.get("kind"), what)
        return {
            "id": task.task_id,
            "titel": task.title,
            "was": what,
            "wann": describe_schedule(task.schedule),
            "zustand": _ZUSTAND.get(task.state, task.state),
            "aktiv": bool(task.enabled),
            "naechster_lauf": task.next_run_at,
            "letzter_lauf": task.last_run_at,
            "fehlschlaege": int(task.consecutive_failures or 0),
            "letzter_fehler": _friendly_error(task.last_error),
            "wartet_auf_freigabe": "Freigabe" in (task.last_error or ""),
            # Der Auftrag im Wortlaut des Menschen — damit sichtbar bleibt,
            # worauf die Automatik zurueckgeht.
            "auftrag": (task.created_from or "")[:200],
        }

    def _item_entry(self, item: dict[str, Any], *, full: bool = False
                    ) -> dict[str, Any]:
        entry = {"id": item["id"], "zusammenfassung": item["zusammenfassung"],
                 "dringlichkeit": item.get("dringlichkeit", "normal"),
                 "zeit": item.get("erstellt"),
                 "aufgabe": item.get("aufgabe") or "",
                 "gelesen": bool(item.get("gelesen")),
                 "quelle": A.human(str(item.get("quelle") or ""))}
        # Existing agent-run reference, never a URL or a new command. The
        # destination still checks the authenticated owner's access itself.
        run_id = item.get("lauf")
        if (item.get("quelle") == "agent_runtime" and isinstance(run_id, str)
                and re.fullmatch(r"ar-[0-9a-f]{16}", run_id)):
            entry["lauf"] = run_id
        if full:
            entry["befunde"] = list(item.get("befunde") or [])[:10]
            # Der Rang des Materials wird genannt, aber nicht als Fachbegriff
            # ausgestellt: „aus einer fremden Quelle" sagt dasselbe.
            if item.get("content_trust"):
                entry["herkunft"] = "aus einer fremden Quelle"
        return entry

    async def _pending_approvals(self) -> list[str]:
        runtime = getattr(self.dispatcher, "approver_runtime", None)
        if runtime is None:
            return []
        try:
            pending = await runtime.approvals.pending()
        except Exception:  # noqa: BLE001
            return []
        return [str(entry.get("approval_id", "")) for entry in pending
                if entry.get("approval_id")]


#: Interne Fehlerworte in etwas, das ein Mensch versteht.
_FEHLER = {
    "credential_or_connection_missing": "Zugang abgelaufen — bitte neu anmelden",
    "device_or_service_unavailable": "war gerade nicht erreichbar",
    "provider_or_quota_unavailable": "Anbieter gerade nicht verfügbar",
    "approval_required": "wartet auf deine Freigabe",
    "policy_hard_stop": "aus Sicherheitsgründen nicht ausgeführt",
    "manual_recovery_required": "unklarer Ausgang — bitte nachsehen",
}


def _friendly_error(raw: str) -> str:
    """Aus `capability_failed: credential_or_connection_missing:invalid_grant`
    wird „Zugang abgelaufen — bitte neu anmelden".

    Der Rohtext ist fuer das Journal richtig und fuer einen Bildschirm falsch.
    """
    if not raw:
        return ""
    low = raw.lower()
    for marker, sentence in _FEHLER.items():
        if marker in low:
            return sentence
    return raw.split(":")[0].replace("_", " ")[:80]
