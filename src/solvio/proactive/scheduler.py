"""Die Schleife, die wach wird, wenn niemand da ist.

Sie ist absichtlich langweilig gebaut: alle zwanzig Sekunden nachsehen, was
faellig ist, jede Gelegenheit genau einmal in Besitz nehmen, ausfuehren, den
naechsten Termin ausrechnen. Kein Schlafen bis zum Termin, keine Timer je
Aufgabe, kein zweiter Prozess.

Der Grund fuer diese Langeweile ist der Neustart. Ein Timer, der auf 7:30 wartet,
ist nach einem Neustart weg; eine Tabelle mit `next_run_at` ist es nicht. Die
Schleife hat deshalb kein Gedaechtnis — sie liest jedes Mal neu, was ansteht. Was
sie ueberlebt, ueberlebt in der Datenbank.

Beim Start wird zweierlei aufgeraeumt, und beides betrifft die Wahrheit:

* **Angebrochene Laeufe.** Wer beim Absturz mitten drin war, wird nicht als
  erfolgreich verbucht und auch nicht blind wiederholt — er wird als abgebrochen
  vermerkt. Ob dabei etwas passiert ist, weiss niemand, und so steht es dann auch
  da.
* **Verpasste Gelegenheiten.** Nach zwei Wochen Auszeit will niemand vierzehn
  Morgenberichte. Es gibt hoechstens einen, und nur wenn er noch etwas bedeutet.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from solvio.logging_setup import get_logger
from solvio.proactive import store as S
from solvio.proactive.runner import TaskRunner, backoff_after, run_id_for, MAX_FAILURES
from solvio.proactive.schedule import Schedule
from solvio.proactive.everyday import ACTIONS as PRIVATE_READ_ACTIONS

log = get_logger("proactive")

#: Wie oft nachgesehen wird. Zwanzig Sekunden sind genau genug fuer „in zwei
#: Minuten" und billig genug, um den ganzen Tag zu laufen.
TICK_SECONDS = 20.0

#: Wie viele Laeufe gleichzeitig. Hintergrundarbeit darf den Sprachweg nie
#: ausbremsen, und drei gleichzeitige Recherchen sind schon viel.
MAX_CONCURRENT = 2

#: Wie viele Aufgaben ein Besitzer haben darf.
MAX_TASKS_PER_OWNER = 40

#: Wie lange ein einzelner Lauf hoechstens dauert.
RUN_TIMEOUT = 900.0


class Scheduler:
    """Core-eigen. Kein zweiter Prozess, kein fremder Planer."""

    def __init__(self, dispatcher: Any, store: S.ProactiveStore, *,
                 tick: float = TICK_SECONDS,
                 clock=None) -> None:
        self.store = store
        self.runner = TaskRunner(dispatcher, store)
        self.tick = tick
        #: Einspritzbare Uhr — nur damit ein Abnahmetest nicht bis morgen
        #: frueh warten muss. Produktiv ist es `time.time`.
        self.clock = clock or time.time
        self._task: asyncio.Task | None = None
        self._running: set[str] = set()
        self._gate = asyncio.Semaphore(MAX_CONCURRENT)
        self.ticks = 0

    # -- Lebenszyklus --------------------------------------------------------

    async def start(self) -> None:
        await self.recover()
        self._task = asyncio.create_task(self._loop())
        log.info("proactive.scheduler_started", tick=self.tick)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        log.info("proactive.scheduler_stopped")

    async def recover(self) -> dict[str, int]:
        """Aufraeumen nach einem Neustart. Nichts wird beschoenigt."""
        broken = 0
        for run in await self.store.unfinished_runs():
            await self.store.finish_run(
                str(run["run_id"]), S.FAILED, outcome="interrupted",
                detail="Core endete waehrend des Laufs — Ausgang unbekannt")
            broken += 1
        coalesced = 0
        now = self.clock()
        for task in await self.store.list_tasks(only_active=True):
            schedule = Schedule.from_dict(task.schedule)
            if task.next_run_at is None or task.next_run_at > now:
                continue
            if task.action.get("kind") in PRIVATE_READ_ACTIONS and task.retry_after is not None:
                continue  # Preserve the explicitly scheduled safe read retry.
            due, skipped = schedule.catch_up(task.next_run_at, now)
            if skipped:
                coalesced += skipped
            if due is None:
                # Nichts mehr nachzuholen: auf die naechste echte Gelegenheit.
                task.next_run_at = schedule.next_after(now)
                if task.next_run_at is None:
                    task.state = S.EXPIRED
                    task.last_error = "Gelegenheit verstrichen"
                await self.store.update_running(task)
        if broken or coalesced:
            log.info("proactive.recovered", interrupted=broken,
                     coalesced=coalesced)
        return {"unterbrochen": broken, "uebersprungen": coalesced}

    # -- Die Schleife ---------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.tick)
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - die Schleife stirbt nie
                log.error("proactive.tick_failed", kind=type(exc).__name__)

    async def poll(self) -> int:
        """Ein Durchgang. Gibt zurueck, wie viele Laeufe angestossen wurden."""
        self.ticks += 1
        now = self.clock()
        started = 0
        for task in await self.store.due_tasks(now):
            if task.task_id in self._running:
                # Ueberlappung verhindern: eine Aufgabe laeuft nie zweimal
                # gleichzeitig, auch wenn der vorige Lauf lange braucht.
                continue
            occurrence = float(task.next_run_at or now)
            asyncio.create_task(self._guarded(task, occurrence))
            started += 1
        return started

    async def _guarded(self, task: S.Task, occurrence: float) -> None:
        self._running.add(task.task_id)
        try:
            async with self._gate:
                await asyncio.wait_for(self.run_once(task, occurrence),
                                       timeout=RUN_TIMEOUT)
        except asyncio.TimeoutError:
            log.error("proactive.run_timeout", task=task.task_id)
            await self._after_failure(task, "timeout",
                                      "device_or_service_unavailable:run_timeout")
        except Exception as exc:  # noqa: BLE001
            log.error("proactive.run_error", task=task.task_id,
                      kind=type(exc).__name__)
            await self._after_failure(task, "error", type(exc).__name__)
        finally:
            self._running.discard(task.task_id)

    async def run_once(self, task: S.Task, occurrence: float) -> S.Task:
        """Genau eine Gelegenheit. Der Besitz kommt aus der Datenbank."""
        if not await self.store.is_running_version(task):
            return task
        run_id = run_id_for(task.task_id, occurrence)
        if not await self.store.claim_run(task.task_id, occurrence, run_id):
            # Jemand anderes hat diese Gelegenheit schon — nach einem Neustart
            # der Normalfall. Trotzdem muss der naechste Termin gesetzt werden,
            # sonst bleibt die Aufgabe faellig und dreht sich im Kreis.
            log.info("proactive.run_already_claimed", task=task.task_id)
            return await self._advance(task, occurrence)

        log.info("proactive.run_started", task=task.task_id, run=run_id[:12])
        outcome = await self.runner.execute(task, occurrence, run_id)
        await self.store.finish_run(run_id, outcome.state,
                                    outcome=outcome.outcome, detail=outcome.detail)

        if outcome.item is not None:
            fresh = await self.store.add_item(outcome.item, running_task=task)
            log.info("proactive.item_created" if fresh
                     else "proactive.item_suppressed_duplicate",
                     task=task.task_id)

        task.last_run_at = self.clock()
        if outcome.state == S.FAILED:
            return await self._after_failure(task, outcome.outcome, outcome.detail)

        task.consecutive_failures = 0
        task.retry_after = None
        task.last_error = ""
        task.last_result = {"fingerprint": outcome.fingerprint,
                            "state": outcome.state}
        if outcome.state == S.APPROVAL_PENDING:
            # Wahrheitsgemaess: die Gelegenheit wartet auf einen Menschen. Es
            # wird KEIN anderer Weg gesucht — das waere der Weg an der Freigabe
            # vorbei, und genau den gibt es hier nicht.
            task.last_error = "wartet auf Freigabe"
        return await self._advance(task, occurrence)

    async def _advance(self, task: S.Task, occurrence: float) -> S.Task:
        """Setzt den naechsten Termin — oder schliesst die Aufgabe ab."""
        schedule = Schedule.from_dict(task.schedule)
        following = schedule.next_after(max(occurrence, self.clock()))
        if following is None:
            task.state = S.COMPLETED if schedule.kind.value == "one_shot" else S.EXPIRED
            task.next_run_at = None
            task.enabled = False
            log.info("proactive.task_completed", task=task.task_id,
                     state=task.state)
        else:
            task.next_run_at = following
        await self.store.update_running(task)
        return task

    async def _after_failure(self, task: S.Task, outcome: str,
                             detail: str) -> S.Task:
        task.consecutive_failures += 1
        task.last_error = f"{outcome}: {detail}"[:300]
        wait = backoff_after(task.consecutive_failures)
        task.retry_after = self.clock() + wait
        if task.action.get("kind") in PRIVATE_READ_ACTIONS:
            # These actions have no external write effects. Retry as a new
            # occurrence; preserve the original failed run as evidence.
            task.next_run_at = task.retry_after
        if task.consecutive_failures >= MAX_FAILURES:
            # Nicht loeschen und nicht ewig weiterversuchen. Pausiert, sichtbar,
            # mit dem Grund daneben — ein Mensch entscheidet, was damit wird.
            task.enabled = False
            task.state = S.PAUSED
            log.error("proactive.task_paused_after_failures",
                      task=task.task_id, failures=task.consecutive_failures)
        else:
            log.info("proactive.run_retry_scheduled", task=task.task_id,
                     in_seconds=int(wait))
        await self.store.update_running(task)
        return task
