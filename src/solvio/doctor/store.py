"""Ein sehr kleines Gedaechtnis: was war kaputt, was wurde versucht, half es.

Bewusst KEIN zweites Ereignisprotokoll. Die Chronik des Kontrollzentrums
projiziert bereits vorhandene Aufzeichnungen; hier steht nur, was sonst nirgends
steht — naemlich die Frage „ist das schon einmal passiert, und was haben wir
damals getan". Ohne diese eine Tabelle faengt der Doctor nach jedem Neustart bei
null an und startet fuer immer dieselbe Komponente neu.

Ein Datensatz je Komponente und Vorfall, mit Zaehler und Sperrzeit. Keine
Rohprotokolle, keine Fehlertexte des Anbieters, keine Gedankengaenge.

Die Verbindungen werden geschlossen. Das steht hier, weil es beim
Hintergrundspeicher genau daran gefehlt hat: `with sqlite3.connect(...)` ist ein
Transaktionskontext und laesst die Verbindung offen. Unter dem Takt des
Kontrollzentrums waren es 227 auf einer Datei, bevor SQLite aufgab.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import time
from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("doctor")

DEFAULT_PATH = os.path.expanduser("~/.solvio/doctor.sqlite3")

#: Umlenkung fuer Tests (DEBT-0223). In Produktion ungesetzt: dann gilt
#: `DEFAULT_PATH` wie bisher. Aufgeloest beim Bau, nicht beim Import.
PATH_ENV = "SOLVIO_DOCTOR_DB"


def default_path() -> str:
    return os.path.expanduser(os.environ.get(PATH_ENV) or DEFAULT_PATH)

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS incidents (
    component TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_playbook TEXT,
    last_result TEXT,
    next_allowed REAL,
    resolved_at REAL,
    PRIMARY KEY (component, first_seen));

CREATE INDEX IF NOT EXISTS idx_incidents_open
    ON incidents(component, resolved_at, last_seen);
"""

#: Wie viele Vorfaelle aufbewahrt werden. Eine Krankenakte, die nie vergisst,
#: wird zur Halde.
KEEP = 200


class DoctorStore:
    def __init__(self, path: str | None = None) -> None:
        path = default_path() if path is None else path
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        self._lock = asyncio.Lock()
        with self._open() as connection:
            connection.executescript(SCHEMA)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    @contextlib.contextmanager
    def _open(self):
        """Eine Verbindung, die auch wieder zugeht — siehe Modulkopf."""
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def _run(self, function, *args):
        async with self._lock:
            return await asyncio.get_running_loop().run_in_executor(
                None, function, *args)

    async def put_incident(self, incident: Any) -> None:
        await self._run(self._put, incident)

    def _put(self, incident: Any) -> None:
        with self._open() as connection:
            connection.execute(
                """INSERT INTO incidents (component, first_seen, last_seen,
                       attempts, last_playbook, last_result, next_allowed,
                       resolved_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(component, first_seen) DO UPDATE SET
                       last_seen=excluded.last_seen,
                       attempts=excluded.attempts,
                       last_playbook=excluded.last_playbook,
                       last_result=excluded.last_result,
                       next_allowed=excluded.next_allowed,
                       resolved_at=excluded.resolved_at""",
                (incident.component, incident.first_seen, incident.last_seen,
                 incident.attempts, incident.last_playbook or "",
                 incident.last_result or "", incident.next_allowed or None,
                 incident.resolved_at or None))

    async def open_incident(self, component: str) -> dict[str, Any] | None:
        """Der offene Vorfall dieser Komponente, falls es einen gibt.

        Damit ueberlebt der Zaehler einen Neustart des Core. Ohne das faengt die
        Zaehlung nach jedem Absturz von vorn an — und ein Dienst, der den Core
        mit in den Abgrund reisst, wuerde endlos neu gestartet.
        """
        return await self._run(self._open_incident, component)

    def _open_incident(self, component: str) -> dict[str, Any] | None:
        with self._open() as connection:
            row = connection.execute(
                """SELECT * FROM incidents WHERE component = ?
                   AND resolved_at IS NULL ORDER BY last_seen DESC LIMIT 1""",
                (component,)).fetchone()
        return dict(row) if row else None

    async def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self._run(self._history, limit)

    def _history(self, limit: int) -> list[dict[str, Any]]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM incidents ORDER BY last_seen DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    async def prune(self, keep: int = KEEP) -> int:
        return await self._run(self._prune, keep)

    def _prune(self, keep: int) -> int:
        with self._open() as connection:
            return connection.execute(
                """DELETE FROM incidents WHERE rowid IN (
                       SELECT rowid FROM incidents ORDER BY last_seen DESC
                       LIMIT -1 OFFSET ?)""", (keep,)).rowcount
