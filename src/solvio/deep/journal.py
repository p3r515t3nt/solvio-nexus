"""Das Gedaechtnis einer tiefen Aufgabe — und der Ort, an dem SOLVIO Recht behaelt.

Eine tiefe Aufgabe laeuft laenger als ein Gespraech und laenger als ein
Prozessleben. Damit stellen sich drei Fragen, die ohne dauerhaften Zustand nicht
zu beantworten sind: Was lief, als der Mac neu startete? Wem gehoert der
Abbruch? Und was darf ein Executor noch aendern, nachdem SOLVIO entschieden hat?

Die Antworten stehen hier, und zwar in dieser Reihenfolge:

* **Die SOLVIO-Kennung ist die Identitaet.** Die Kennung des Executors steht in
  einer eigenen Spalte. Sie ist ein Griff nach aussen — zum Abbrechen, zum
  Nachfragen — und niemals der Name der Aufgabe. Ein Executor, der seine
  Kennungen neu vergibt, verliert damit keine SOLVIO-Aufgabe.
* **Ein Endzustand ist endgueltig.** `_set_status` schreibt ueber einen
  Endzustand nicht hinweg. Deshalb kann verspaetete Ausgabe eine abgebrochene
  Aufgabe nicht wiederbeleben: sie wird als Beobachtung protokolliert und
  aendert nichts.
* **Die Sequenz gehoert dem Journal.** Sie wird beim Schreiben unter derselben
  Transaktion vergeben, die das Ereignis speichert. Ein Leser, der bei `seq`
  weiterliest, verliert nichts und sieht nichts doppelt — auch dann nicht, wenn
  er sich mitten in einer laufenden Aufgabe neu verbindet.

Technisch bewusst langweilig: `sqlite3` aus der Standardbibliothek in einem
Thread mit genau einem Arbeiter, wie der eingefrorene Freigabepfad es auch tut.
Ein Arbeiter heisst: Schreibvorgaenge reihen sich, und die Sequenzvergabe
braucht kein zusaetzliches Schloss.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from solvio.deep.events import TERMINAL_KINDS, DeepEvent, EventKind
from solvio.logging_setup import get_logger

log = get_logger("deep")

#: Zustaende einer Aufgabe. Bewusst deckungsgleich mit `DeepTaskStatus` aus dem
#: Contract — hier als String, weil das Journal Strings speichert.
QUEUED = "queued"
RUNNING = "running"
WAITING_FOR_USER = "waiting_for_user"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TIMED_OUT = "timed_out"

#: Endzustaende. Wer hier steht, bleibt hier.
TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED, TIMED_OUT})

#: Der globale Not-Aus (§14). Als Zeile in der Datenbank, nicht als Variable im
#: Speicher: ein Not-Aus, der einen Neustart nicht ueberlebt, ist keiner.
PAUSE_FLAG = "deep_paused"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deep_tasks (
    task_id          TEXT PRIMARY KEY,
    task_type        TEXT NOT NULL,
    instruction      TEXT NOT NULL,
    status           TEXT NOT NULL,
    executor_run_id  TEXT NOT NULL DEFAULT '',
    next_seq         INTEGER NOT NULL DEFAULT 0,
    result_json      TEXT NOT NULL DEFAULT '',
    failure_reason   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS deep_events (
    task_id      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (task_id, seq)
);
CREATE TABLE IF NOT EXISTS deep_flags (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS deep_events_task ON deep_events(task_id, seq);
CREATE INDEX IF NOT EXISTS deep_tasks_run ON deep_tasks(executor_run_id);
"""


class DeepJournal:
    """Dauerhafter Zustand aller tiefen Aufgaben."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deep-journal")
        self._conn: sqlite3.Connection | None = None
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    # -- Leben ---------------------------------------------------------------
    async def open(self) -> None:
        await self._call(self._open)

    def _open(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn

    async def close(self) -> None:
        await self._call(self._close)
        self._pool.shutdown(wait=False)

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def _call(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, fn, *args)

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("journal not open")
        return self._conn

    # -- Der Not-Aus ---------------------------------------------------------
    async def set_paused(self, paused: bool) -> None:
        await self._call(self._set_paused, paused)

    def _set_paused(self, paused: bool) -> None:
        self._db.execute(
            "INSERT INTO deep_flags(name,value) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (PAUSE_FLAG, "1" if paused else "0"))
        self._db.commit()

    async def paused(self) -> bool:
        return await self._call(self._paused)

    def _paused(self) -> bool:
        row = self._db.execute("SELECT value FROM deep_flags WHERE name=?",
                               (PAUSE_FLAG,)).fetchone()
        return bool(row) and row["value"] == "1"

    # -- Aufgaben ------------------------------------------------------------
    async def create(self, task_id: str, task_type: str, instruction: str) -> DeepEvent:
        return await self._call(self._create, task_id, task_type, instruction)

    def _create(self, task_id: str, task_type: str, instruction: str) -> DeepEvent:
        self._db.execute(
            "INSERT INTO deep_tasks(task_id,task_type,instruction,status) VALUES(?,?,?,?)",
            (task_id, task_type, instruction, QUEUED))
        event = self._append(task_id, EventKind.TASK_CREATED, {"task_type": task_type})
        self._db.commit()
        return event

    async def attach_run(self, task_id: str, run_id: str) -> bool:
        """Merkt die Executor-Kennung. Falsch, wenn die Aufgabe schon beendet ist.

        Der Rueckgabewert ist die Rennbedingung aus §13 in einem Wert: Wurde
        waehrend der Einreichung abgebrochen, erfaehrt der Einreicher es hier —
        und schickt statt eines Weiterlaufs sofort ein Stopp hinterher.
        """
        return await self._call(self._attach_run, task_id, run_id)

    def _attach_run(self, task_id: str, run_id: str) -> bool:
        row = self._db.execute("SELECT status FROM deep_tasks WHERE task_id=?",
                               (task_id,)).fetchone()
        if row is None:
            return False
        # Immer speichern, auch wenn schon abgebrochen: ohne die Kennung koennte
        # der ferne Lauf nicht mehr gestoppt werden, und genau der laeuft dann.
        self._db.execute("UPDATE deep_tasks SET executor_run_id=? WHERE task_id=?",
                         (run_id, task_id))
        self._db.commit()
        return row["status"] not in TERMINAL

    async def run_id(self, task_id: str) -> str:
        return await self._call(self._run_id, task_id)

    def _run_id(self, task_id: str) -> str:
        row = self._db.execute("SELECT executor_run_id FROM deep_tasks WHERE task_id=?",
                               (task_id,)).fetchone()
        return row["executor_run_id"] if row else ""

    async def task(self, task_id: str) -> dict[str, Any] | None:
        return await self._call(self._task, task_id)

    def _task(self, task_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM deep_tasks WHERE task_id=?",
                               (task_id,)).fetchone()
        return dict(row) if row else None

    async def tasks(self, *, status: str = "") -> list[dict[str, Any]]:
        return await self._call(self._tasks, status)

    def _tasks(self, status: str) -> list[dict[str, Any]]:
        if status:
            rows = self._db.execute("SELECT * FROM deep_tasks WHERE status=? ORDER BY rowid",
                                    (status,)).fetchall()
        else:
            rows = self._db.execute("SELECT * FROM deep_tasks ORDER BY rowid").fetchall()
        return [dict(r) for r in rows]

    async def unfinished(self) -> list[dict[str, Any]]:
        """Was beim letzten Prozessende noch offen war (Neustart-Recovery)."""
        return await self._call(self._unfinished)

    def _unfinished(self) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in TERMINAL)
        rows = self._db.execute(
            f"SELECT * FROM deep_tasks WHERE status NOT IN ({marks}) ORDER BY rowid",
            tuple(sorted(TERMINAL))).fetchall()
        return [dict(r) for r in rows]

    # -- Ereignisse ----------------------------------------------------------
    async def record(self, task_id: str, kind: EventKind,
                     payload: dict[str, Any] | None = None) -> DeepEvent | None:
        """Schreibt ein Ereignis und schiebt es an alle Mitleser.

        Liefert `None`, sobald die Aufgabe bereits beendet ist. Das Ereignis wird
        trotzdem protokolliert — nur eben als Beobachtung, die nichts aendert.
        Der `None`-Rueckgabewert ist gleichzeitig das Signal an den Antrieb: „die
        Aufgabe gehoert dir nicht mehr, hoer auf." Genau daran erkennt die Pumpe
        einen Abbruch, ohne dass jemand sie von aussen abschiessen muesste.
        """
        event = await self._call(self._record, task_id, kind, payload or {})
        if event is not None:
            self._publish(event)
        return event

    def _record(self, task_id: str, kind: EventKind,
                payload: dict[str, Any]) -> DeepEvent | None:
        row = self._db.execute("SELECT status FROM deep_tasks WHERE task_id=?",
                               (task_id,)).fetchone()
        if row is None:
            return None
        already_done = row["status"] in TERMINAL
        if already_done and kind in TERMINAL_KINDS:
            # Der Executor meldet ein Ende fuer etwas, das SOLVIO bereits beendet
            # hat. Die Meldung wird protokolliert, der Zustand nicht angefasst.
            payload = dict(payload)
            payload["late"] = True
            kind = EventKind.OBSERVATION
        event = self._append(task_id, kind, payload)
        status = _STATUS_FOR.get(kind, "")
        if status and not already_done:
            self._set_status(task_id, status)
        self._db.commit()
        if already_done:
            return None
        return event

    def _append(self, task_id: str, kind: EventKind,
                payload: dict[str, Any]) -> DeepEvent:
        row = self._db.execute("SELECT next_seq FROM deep_tasks WHERE task_id=?",
                               (task_id,)).fetchone()
        seq = int(row["next_seq"])
        self._db.execute(
            "INSERT INTO deep_events(task_id,seq,kind,payload_json) VALUES(?,?,?,?)",
            (task_id, seq, kind.value, json.dumps(payload, ensure_ascii=False)))
        self._db.execute("UPDATE deep_tasks SET next_seq=? WHERE task_id=?",
                         (seq + 1, task_id))
        return DeepEvent(task_id=task_id, seq=seq, kind=kind, payload=payload)

    def _set_status(self, task_id: str, status: str) -> None:
        """Setzt den Zustand — aber nie ueber einen Endzustand hinweg."""
        marks = ",".join("?" for _ in TERMINAL)
        self._db.execute(
            f"UPDATE deep_tasks SET status=? WHERE task_id=? AND status NOT IN ({marks})",
            (status, task_id, *sorted(TERMINAL)))

    async def finish(self, task_id: str, kind: EventKind, *,
                     result: Any = None, reason: str = "",
                     status: str = "") -> DeepEvent | None:
        """Beendet eine Aufgabe mitsamt Ergebnis. Ein zweites Mal wirkt nicht.

        `status` ueberschreibt nur die Benennung des Endes, nicht die Regel: ein
        Zeitablauf ist ein Fehlschlag mit eigenem Namen (`timed_out`), damit ein
        Bericht spaeter „hat zu lange gedauert" von „ist gescheitert" trennen kann.
        """
        event = await self._call(self._finish, task_id, kind, result, reason, status)
        if event is not None:
            self._publish(event)
        return event

    def _finish(self, task_id: str, kind: EventKind, result: Any,
                reason: str, status: str = "") -> DeepEvent | None:
        row = self._db.execute("SELECT status FROM deep_tasks WHERE task_id=?",
                               (task_id,)).fetchone()
        if row is None:
            return None
        if row["status"] in TERMINAL:
            payload = {"late": True, "kind": kind.value}
            event = self._append(task_id, EventKind.OBSERVATION, payload)
            self._db.commit()
            return event
        event = self._append(task_id, kind, {"reason": reason} if reason else {})
        self._db.execute(
            "UPDATE deep_tasks SET status=?, result_json=?, failure_reason=? WHERE task_id=?",
            (status or _STATUS_FOR[kind],
             json.dumps(result, ensure_ascii=False) if result is not None else "",
             reason, task_id))
        self._db.commit()
        return event

    async def cancel(self, task_id: str) -> bool:
        """Erklaert den Abbruch lokal fuer verbindlich — vor jedem Netzverkehr.

        Genau diese Reihenfolge ist §13: erst gilt es hier, dann wird es dem
        Executor mitgeteilt. Scheitert die Mitteilung, bleibt der Abbruch
        trotzdem gueltig. Andersherum waere ein nicht erreichbarer Executor ein
        Grund, weiterzulaufen — und das darf er nie sein.
        """
        event = await self._call(self._cancel, task_id)
        if event is not None:
            self._publish(event)
        return event is not None

    def _cancel(self, task_id: str) -> DeepEvent | None:
        row = self._db.execute("SELECT status FROM deep_tasks WHERE task_id=?",
                               (task_id,)).fetchone()
        if row is None or row["status"] in TERMINAL:
            return None
        event = self._append(task_id, EventKind.CANCELLED, {"by": "solvio"})
        self._db.execute("UPDATE deep_tasks SET status=? WHERE task_id=?",
                         (CANCELLED, task_id))
        self._db.commit()
        return event

    # -- Wiedergabe und Mitlesen ---------------------------------------------
    async def replay(self, task_id: str, *, after_seq: int = -1) -> list[DeepEvent]:
        return await self._call(self._replay, task_id, after_seq)

    def _replay(self, task_id: str, after_seq: int) -> list[DeepEvent]:
        rows = self._db.execute(
            "SELECT seq,kind,payload_json FROM deep_events "
            "WHERE task_id=? AND seq>? ORDER BY seq", (task_id, after_seq)).fetchall()
        return [DeepEvent(task_id=task_id, seq=int(r["seq"]),
                          kind=EventKind(r["kind"]),
                          payload=json.loads(r["payload_json"])) for r in rows]

    def _publish(self, event: DeepEvent) -> None:
        for queue in list(self._subscribers.get(event.task_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:      # pragma: no cover - Mitleser haengt
                log.warning("deep.subscriber_full", task_id=event.task_id)

    async def stream(self, task_id: str, *, after_seq: int = -1):
        """Erst nachholen, dann mitlesen — ohne Loch an der Nahtstelle (§12).

        Die Reihenfolge ist der ganze Punkt: der Mitleser haengt sich ZUERST
        ein, dann wird nachgelesen. Umgekehrt fiele alles durch, was zwischen
        Nachlesen und Einhaengen passiert. Doppelt Gesehenes faengt der
        Sequenzvergleich ab — deshalb ist die Sequenz eine Zahl und keine Uhr.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._subscribers.setdefault(task_id, []).append(queue)
        try:
            seen = after_seq
            for event in await self.replay(task_id, after_seq=after_seq):
                seen = event.seq
                yield event
            task = await self.task(task_id)
            if task is not None and task["status"] in TERMINAL:
                return
            while True:
                event = await queue.get()
                if event.seq <= seen:
                    continue
                seen = event.seq
                yield event
                if event.kind in TERMINAL_KINDS:
                    return
        finally:
            waiting = self._subscribers.get(task_id, [])
            if queue in waiting:
                waiting.remove(queue)
            if not waiting:
                self._subscribers.pop(task_id, None)


#: Welches Ereignis welchen Zustand bedeutet. Nur diese fuenf aendern etwas —
#: Beobachtungen aendern nie den Zustand einer Aufgabe.
_STATUS_FOR: dict[EventKind, str] = {
    EventKind.TASK_CREATED: QUEUED,
    EventKind.EXECUTOR_STARTING: QUEUED,
    EventKind.RUNNING: RUNNING,
    EventKind.WAITING_FOR_APPROVAL: WAITING_FOR_USER,
    EventKind.RESUMED: RUNNING,
    EventKind.SUCCEEDED: SUCCEEDED,
    EventKind.FAILED: FAILED,
    EventKind.CANCELLED: CANCELLED,
}
