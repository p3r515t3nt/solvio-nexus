"""Der dauerhafte Bestand: Aufgaben, Laeufe, Meldungen.

Die schwierigste Zusage dieses Meilensteins ist „genau einmal". Sie laesst sich in
Anwendungscode nicht ehrlich geben: zwischen „ich habe nachgesehen, ob schon
gelaufen wurde" und „ich fange an" liegt immer ein Moment, in dem ein Neustart
dazwischenfahren kann.

Also gibt die **Datenbank** sie. Ein Lauf hat einen Schluessel aus Aufgabe und
Gelegenheit, und dieser Schluessel ist eindeutig. Wer ihn einfuegen kann, besitzt
den Lauf; wer es nicht kann, laesst die Finger davon. Das gilt ueber Neustarts,
ueber gleichzeitige Prozesse und ueber jeden Absturz hinweg, weil es nicht von
einer Reihenfolge abhaengt, sondern von einer Bedingung.

Dieselbe Idee bei den Meldungen: eindeutig ueber (Aufgabe, Fingerabdruck). Eine
Beobachtung, die zum zwanzigsten Mal dasselbe sieht, kann gar keine zwanzig
Meldungen erzeugen — nicht weil jemand daran gedacht hat zu pruefen, sondern weil
die zweite Einfuegung scheitert.

Was hier NICHT liegt: E-Mail-Texte, Webseiten, Hermes-Protokolle. Gespeichert
wird die Schlussfolgerung und ein Verweis, nicht das Material.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("proactive")

DEFAULT_PATH = os.path.expanduser("~/.solvio/proactive.sqlite3")

#: Umlenkung fuer Tests (DEBT-0223). In Produktion ungesetzt: dann gilt
#: `DEFAULT_PATH` wie bisher. Aufgeloest beim Bau, nicht beim Import.
PATH_ENV = "SOLVIO_PROACTIVE_DB"


def default_path() -> str:
    return os.path.expanduser(os.environ.get(PATH_ENV) or DEFAULT_PATH)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    created_from TEXT NOT NULL,
    schedule TEXT NOT NULL,
    action TEXT NOT NULL,
    state TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run_at REAL,
    last_run_at REAL,
    last_result TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    retry_after REAL,
    conversation_ref TEXT,
    updated_at REAL NOT NULL);

CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(enabled, next_run_at);

-- Der Schluessel, der „genau einmal" traegt: eine Gelegenheit je Aufgabe.
CREATE TABLE IF NOT EXISTS task_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    occurrence REAL NOT NULL,
    claimed_at REAL NOT NULL,
    finished_at REAL,
    state TEXT NOT NULL,
    outcome TEXT,
    detail TEXT,
    UNIQUE(task_id, occurrence),
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE);

CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs(task_id, occurrence);

CREATE TABLE IF NOT EXISTS proactive (
    notification_id TEXT PRIMARY KEY,
    task_id TEXT,
    run_id TEXT,
    created_at REAL NOT NULL,
    priority TEXT NOT NULL,
    summary TEXT NOT NULL,
    findings TEXT,
    source_capability TEXT,
    content_trust TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    read_at REAL,
    expires_at REAL,
    UNIQUE(task_id, fingerprint));

CREATE INDEX IF NOT EXISTS idx_proactive_unread ON proactive(read_at, created_at);

-- Gebundene Vorab-Autorisierung fuer eine wiederkehrende Haus-Automatisierung
-- (Approval Policy V2, ADR-0022). Sie liegt hier und nicht im Aufgabentext:
-- Autoritaet gehoert in eine eigene Zeile mit eigenem Digest, nicht in Prosa,
-- die jemand spaeter umformuliert.
--
-- Genau eine je Automatisierung. Eine materielle Aenderung erzeugt eine neue
-- Revision mit neuem Digest und ersetzt die alte — es gibt keinen Weg, unter
-- der alten Bindung etwas anderes auszufuehren.
CREATE TABLE IF NOT EXISTS automation_preauth (
    automation_id TEXT PRIMARY KEY,
    preauth_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    action_class TEXT NOT NULL,
    targets TEXT NOT NULL,
    digest TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    created_origin TEXT NOT NULL,
    created_principal TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    revoked_at REAL,
    revoked_reason TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(automation_id) REFERENCES tasks(task_id) ON DELETE CASCADE);
"""

#: Laufzustaende. Absichtlich klein und ohne Zwischenstufen, die niemand liest.
CLAIMED = "claimed"
DONE = "done"
FAILED = "failed"
NO_CHANGE = "no_change"
APPROVAL_PENDING = "approval_pending"
SKIPPED = "skipped"

#: Aufgabenzustaende.
ACTIVE = "active"
PAUSED = "paused"
EXPIRED = "expired"
COMPLETED = "completed"



def _refuse_credentials(*texts: str, where: str) -> None:
    """Der Zaun des Tresors an den zwei einzigen INSERTs dieses Speichers.

    Der proaktive Eingang traegt freien Text aus Faehigkeitsergebnissen —
    `summary` kommt woertlich aus `data["zusammenfassung"]`, `findings` aus jedem
    skalaren Feld eines Ergebnisses, und `created_from` ist die rohe Aeusserung
    des Nutzers. Eine Mail mit einem Token im Koerper landete damit als Meldung
    in einer Datenbank, die niemand fuer geheimnisbehaftet hielt.

    Verweigert wird, statt zu redigieren: eine Meldung ist eine Aussage, und
    eine halbe Aussage ueber etwas, das man nicht zeigen darf, ist schlechter
    als keine.
    """
    from solvio.secret_vault.firewall import any_credential, refuse_if_credential
    found = any_credential(*texts)
    if found:
        refuse_if_credential(found, where=where)


class StoreError(RuntimeError):
    pass


@dataclass
class Task:
    task_id: str
    owner: str
    title: str
    created_at: float
    created_from: str
    schedule: dict[str, Any]
    action: dict[str, Any]
    state: str = ACTIVE
    enabled: bool = True
    next_run_at: float | None = None
    last_run_at: float | None = None
    last_result: dict[str, Any] | None = None
    last_error: str = ""
    consecutive_failures: int = 0
    retry_after: float | None = None
    conversation_ref: str = ""
    updated_at: float = 0.0

    @property
    def running(self) -> bool:
        return self.enabled and self.state == ACTIVE

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.task_id, "titel": self.title, "zustand": self.state,
                "aktiv": self.enabled, "zeitplan": self.schedule,
                "aktion": {k: v for k, v in self.action.items() if k != "arguments"},
                "naechster_lauf": self.next_run_at,
                "letzter_lauf": self.last_run_at,
                "fehlschlaege_in_folge": self.consecutive_failures,
                "letzter_fehler": self.last_error or "",
                "auftrag": self.created_from[:300]}


class ProactiveStore:
    """SQLite, WAL, alles Schreibende in einem Thread — wie die anderen Speicher."""

    def __init__(self, path: str | None = None) -> None:
        path = default_path() if path is None else path
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        self._lock = asyncio.Lock()
        with self._open() as connection:
            connection.executescript(SCHEMA)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # `foreign_keys` gilt PRO VERBINDUNG, nicht pro Datenbank.
        #
        # Im Schema stand es seit jeher — und wirkte genau einmal, naemlich auf
        # der Verbindung, die das Schema anlegte. Jede spaetere Operation oeffnet
        # eine neue und hatte die Regel damit aus. Die Kaskaden haben nie
        # gefeuert; aufgefallen ist es, als eine Vorab-Autorisierung das
        # Loeschen ihrer Automatisierung ueberlebte.
        #
        # Damit haette die Sicherheit allein daran gehangen, dass ausgerechnet
        # der Loeschpfad vorher widerruft. Eine Loeschung, die an ihrer eigenen
        # Reihenfolge haengt, ist keine.
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextlib.contextmanager
    def _open(self):
        """Eine Verbindung, die auch wieder zugeht.

        `with sqlite3.connect(...) as c:` sieht aus wie ein Schliessen und ist
        keines — es ist ein TRANSAKTIONS-Kontext, der am Ende festschreibt oder
        zuruecknimmt und die Verbindung offen laesst.

        Gemessen im Live-Lauf: unter dem Drei-Sekunden-Takt des Kontrollzentrums
        standen 227 offene Verbindungen auf dieselbe Datei, bis SQLite mit
        `unable to open database file` aufgab. Der Fehler war die ganze Zeit da;
        gebraucht hat es eine Ansicht, die oft genug fragt.
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def _run(self, function, *args):
        async with self._lock:
            return await asyncio.get_running_loop().run_in_executor(
                None, function, *args)

    # -- Aufgaben ------------------------------------------------------------

    async def put_task(self, task: Task) -> None:
        await self._run(self._put_task, task)

    def _put_task(self, task: Task) -> None:
        _refuse_credentials(
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "created_from", "") or ""),
            json.dumps(getattr(task, "action", None) or {}, ensure_ascii=False),
            where="proactive.put_task")
        task.updated_at = max(time.time(), task.updated_at + 0.000001)
        with self._open() as connection:
            connection.execute(
                """INSERT INTO tasks (task_id, owner, title, created_at, created_from,
                       schedule, action, state, enabled, next_run_at, last_run_at,
                       last_result, last_error, consecutive_failures, retry_after,
                       conversation_ref, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                       title=excluded.title, schedule=excluded.schedule,
                       action=excluded.action, state=excluded.state,
                       enabled=excluded.enabled, next_run_at=excluded.next_run_at,
                       last_run_at=excluded.last_run_at,
                       last_result=excluded.last_result,
                       last_error=excluded.last_error,
                       consecutive_failures=excluded.consecutive_failures,
                       retry_after=excluded.retry_after,
                       updated_at=excluded.updated_at""",
                (task.task_id, task.owner, task.title, task.created_at,
                 task.created_from, json.dumps(task.schedule),
                 json.dumps(task.action), task.state, int(task.enabled),
                 task.next_run_at, task.last_run_at,
                 json.dumps(task.last_result) if task.last_result else None,
                 task.last_error, task.consecutive_failures, task.retry_after,
                 task.conversation_ref, task.updated_at))


    async def is_running_version(self, task: Task) -> bool:
        def read():
            with self._open() as db:
                return db.execute('SELECT 1 FROM tasks WHERE task_id=? AND enabled=1 AND state=? AND updated_at=?',
                                  (task.task_id, ACTIVE, task.updated_at)).fetchone() is not None
        return await self._run(read)

    async def update_running(self, task: Task) -> bool:
        """Completion can update only the exact live task; never recreate it.

        The existing updated_at column is the optimistic version. A pause,
        deletion, resume or manual reschedule invalidates an old running copy.
        """
        def write():
            stamp = max(time.time(), task.updated_at + 0.000001)
            with self._open() as db:
                changed = db.execute('UPDATE tasks SET state=?,enabled=?,next_run_at=?,last_run_at=?,last_result=?,last_error=?, '
                    'consecutive_failures=?,retry_after=?,updated_at=? WHERE task_id=? AND enabled=1 AND state=? AND updated_at=?',
                    (task.state,int(task.enabled),task.next_run_at,task.last_run_at,
                     json.dumps(task.last_result) if task.last_result else None,task.last_error,task.consecutive_failures,
                     task.retry_after,stamp,task.task_id,ACTIVE,task.updated_at)).rowcount
            if changed: task.updated_at = stamp
            return bool(changed)
        return await self._run(write)


    # -- Vorab-Autorisierung -------------------------------------------------
    #
    # Die Erlaubnis haengt an der Aufgabe (ON DELETE CASCADE): wer die
    # Automatisierung loescht, loescht ihre Autoritaet mit. Ein verwaistes
    # Recht, das eine spaetere Aufgabe mit derselben Kennung erben koennte,
    # soll gar nicht erst entstehen koennen.

    async def put_preauth(self, grant) -> None:
        await self._run(self._put_preauth, grant)

    def _put_preauth(self, grant) -> None:
        with self._open() as connection:
            connection.execute(
                """INSERT INTO automation_preauth (automation_id, preauth_id,
                       capability, action_class, targets, digest, revision,
                       created_at, created_origin, created_principal, enabled,
                       revoked_at, revoked_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(automation_id) DO UPDATE SET
                       preauth_id=excluded.preauth_id,
                       capability=excluded.capability,
                       action_class=excluded.action_class,
                       targets=excluded.targets, digest=excluded.digest,
                       revision=excluded.revision,
                       created_at=excluded.created_at,
                       created_origin=excluded.created_origin,
                       created_principal=excluded.created_principal,
                       enabled=excluded.enabled, revoked_at=excluded.revoked_at,
                       revoked_reason=excluded.revoked_reason""",
                (grant.automation_id, grant.preauth_id, grant.capability,
                 grant.action_class, json.dumps(list(grant.targets)),
                 grant.digest, grant.revision, grant.created_at,
                 grant.created_origin, grant.created_principal,
                 int(grant.enabled), grant.revoked_at, grant.revoked_reason))

    async def get_preauth(self, automation_id: str):
        return await self._run(self._get_preauth, automation_id)

    def _get_preauth(self, automation_id: str):
        with self._open() as connection:
            row = connection.execute(
                "SELECT * FROM automation_preauth WHERE automation_id = ?",
                (automation_id,)).fetchone()
        return _to_preauth(row) if row else None

    async def list_preauth(self) -> list:
        return await self._run(self._list_preauth)

    def _list_preauth(self) -> list:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM automation_preauth ORDER BY created_at").fetchall()
        return [_to_preauth(r) for r in rows]

    async def revoke_preauth(self, automation_id: str, reason: str) -> bool:
        """Widerruf ist endgueltig fuer DIESE Bindung.

        Kein Wiederaufleben: eine erneute Erlaubnis ist eine neue Revision mit
        neuem Digest, praegbar nur aus einem echten Nutzerakt.
        """
        return await self._run(self._revoke_preauth, automation_id, reason)

    def _revoke_preauth(self, automation_id: str, reason: str) -> bool:
        with self._open() as connection:
            cursor = connection.execute(
                "UPDATE automation_preauth SET enabled = 0, revoked_at = ?, "
                "revoked_reason = ? WHERE automation_id = ? AND revoked_at IS NULL",
                (time.time(), (reason or "")[:200], automation_id))
            return cursor.rowcount > 0

    async def get_task(self, task_id: str) -> Task | None:
        return await self._run(self._get_task, task_id)

    def _get_task(self, task_id: str) -> Task | None:
        with self._open() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?",
                                     (task_id,)).fetchone()
        return _to_task(row) if row else None

    async def list_tasks(self, owner: str = "", *, only_active: bool = False
                         ) -> list[Task]:
        return await self._run(self._list_tasks, owner, only_active)

    def _list_tasks(self, owner: str, only_active: bool) -> list[Task]:
        query = "SELECT * FROM tasks"
        clauses, args = [], []
        if owner:
            clauses.append("owner = ?"); args.append(owner)
        if only_active:
            clauses.append("enabled = 1 AND state = ?"); args.append(ACTIVE)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY COALESCE(next_run_at, 9e18), created_at"
        with self._open() as connection:
            return [_to_task(r) for r in connection.execute(query, args).fetchall()]

    async def due_tasks(self, now: float) -> list[Task]:
        return await self._run(self._due_tasks, now)

    def _due_tasks(self, now: float) -> list[Task]:
        with self._open() as connection:
            rows = connection.execute(
                """SELECT * FROM tasks
                   WHERE enabled = 1 AND state = ? AND next_run_at IS NOT NULL
                     AND next_run_at <= ?
                     AND (retry_after IS NULL OR retry_after <= ?)
                   ORDER BY next_run_at""",
                (ACTIVE, now, now)).fetchall()
        return [_to_task(r) for r in rows]

    async def delete_task(self, task_id: str) -> bool:
        return await self._run(self._delete_task, task_id)

    def _delete_task(self, task_id: str) -> bool:
        with self._open() as connection:
            cursor = connection.execute("DELETE FROM tasks WHERE task_id = ?",
                                        (task_id,))
        return cursor.rowcount > 0

    # -- Laeufe: hier steckt „genau einmal" ----------------------------------

    async def claim_run(self, task_id: str, occurrence: float, run_id: str
                        ) -> bool:
        """Nimmt die Gelegenheit in Besitz — oder stellt fest, dass es jemand tat.

        `INSERT` gegen eine eindeutige Spalte. Kein Lesen-dann-Schreiben, also
        auch kein Fenster dazwischen, in das ein Neustart fallen koennte.
        """
        return await self._run(self._claim_run, task_id, occurrence, run_id)

    def _claim_run(self, task_id: str, occurrence: float, run_id: str) -> bool:
        try:
            with self._open() as connection:
                connection.execute(
                    """INSERT INTO task_runs (run_id, task_id, occurrence,
                           claimed_at, state)
                       VALUES (?,?,?,?,?)""",
                    (run_id, task_id, occurrence, time.time(), CLAIMED))
            return True
        except sqlite3.IntegrityError:
            # Diese Gelegenheit gehoert schon jemandem. Das ist der Normalfall
            # nach einem Neustart und ausdruecklich kein Fehler.
            return False

    async def finish_run(self, run_id: str, state: str, *, outcome: str = "",
                         detail: str = "") -> None:
        await self._run(self._finish_run, run_id, state, outcome, detail[:500])

    def _finish_run(self, run_id: str, state: str, outcome: str,
                    detail: str) -> None:
        with self._open() as connection:
            connection.execute(
                """UPDATE task_runs SET state = ?, outcome = ?, detail = ?,
                       finished_at = ? WHERE run_id = ?""",
                (state, outcome, detail, time.time(), run_id))

    async def unfinished_runs(self) -> list[dict[str, Any]]:
        """Laeufe, die beim Absturz mittendrin waren. Werden nie stillschweigend
        als erfolgreich verbucht."""
        return await self._run(self._unfinished_runs)

    def _unfinished_runs(self) -> list[dict[str, Any]]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM task_runs WHERE finished_at IS NULL "
                "ORDER BY claimed_at").fetchall()
        return [dict(r) for r in rows]

    async def runs_for(self, task_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return await self._run(self._runs_for, task_id, limit)

    def _runs_for(self, task_id: str, limit: int) -> list[dict[str, Any]]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM task_runs WHERE task_id = ? "
                "ORDER BY occurrence DESC LIMIT ?", (task_id, limit)).fetchall()
        return [dict(r) for r in rows]

    # -- Meldungen -----------------------------------------------------------

    async def add_item(self, item: dict[str, Any], *, running_task: Task | None = None) -> bool:
        """Legt eine Meldung ab — oder stellt fest, dass sie schon dalag.

        `False` heisst „dasselbe schon gemeldet". Das ist der haeufigste Fall bei
        einer Beobachtung und der eigentliche Grund, warum sie nicht nervt.
        """
        return await self._run(self._add_item, item, running_task)

    def _add_item(self, item: dict[str, Any], running_task: Task | None = None) -> bool:
        _refuse_credentials(
            str(item.get("summary") or ""),
            *[str(f) for f in (item.get("findings") or [])],
            where="proactive.add_item")
        try:
            with self._open() as connection:
                connection.execute('BEGIN IMMEDIATE')
                if running_task is not None and connection.execute(
                    'SELECT 1 FROM tasks WHERE task_id=? AND enabled=1 AND state=? AND updated_at=?',
                    (running_task.task_id, ACTIVE, running_task.updated_at)).fetchone() is None:
                    return False
                connection.execute(
                    """INSERT INTO proactive (notification_id, task_id, run_id,
                           created_at, priority, summary, findings,
                           source_capability, content_trust, fingerprint,
                           expires_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (item["notification_id"], item.get("task_id"),
                     item.get("run_id"), item.get("created_at", time.time()),
                     item.get("priority", "normal"), item["summary"][:1200],
                     json.dumps(item.get("findings") or []),
                     item.get("source_capability", ""),
                     item.get("content_trust", ""), item["fingerprint"],
                     item.get("expires_at")))
            return True
        except sqlite3.IntegrityError:
            return False

    async def unread(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self._run(self._unread, limit)

    def _unread(self, limit: int) -> list[dict[str, Any]]:
        now = time.time()
        with self._open() as connection:
            rows = connection.execute(
                """SELECT * FROM proactive
                   WHERE read_at IS NULL AND (expires_at IS NULL OR expires_at > ?)
                   ORDER BY CASE priority WHEN 'wichtig' THEN 0 WHEN 'normal'
                            THEN 1 ELSE 2 END, created_at DESC LIMIT ?""",
                (now, limit)).fetchall()
        return [_to_item(r) for r in rows]

    async def get_item(self, notification_id: str) -> dict[str, Any] | None:
        return await self._run(self._get_item, notification_id)

    def _get_item(self, notification_id: str) -> dict[str, Any] | None:
        with self._open() as connection:
            row = connection.execute(
                "SELECT * FROM proactive WHERE notification_id = ?",
                (notification_id,)).fetchone()
        return _to_item(row) if row else None

    async def mark_read(self, notification_id: str) -> bool:
        return await self._run(self._mark_read, notification_id)

    def _mark_read(self, notification_id: str) -> bool:
        with self._open() as connection:
            cursor = connection.execute(
                "UPDATE proactive SET read_at = ? WHERE notification_id = ? "
                "AND read_at IS NULL", (time.time(), notification_id))
        return cursor.rowcount > 0

    async def unread_count(self) -> int:
        return await self._run(self._unread_count)

    def _unread_count(self) -> int:
        now = time.time()
        with self._open() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM proactive WHERE read_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?)", (now,)).fetchone()
        return int(row["n"])

    async def prune(self, *, keep: int = 200, older_than: float = 0.0) -> int:
        """Aufraeumen. Ein Posteingang, der nie vergisst, wird zur Halde."""
        return await self._run(self._prune, keep, older_than)

    def _prune(self, keep: int, older_than: float) -> int:
        with self._open() as connection:
            removed = connection.execute(
                """DELETE FROM proactive WHERE notification_id IN (
                       SELECT notification_id FROM proactive
                       WHERE (read_at IS NOT NULL AND read_at < ?)
                          OR (expires_at IS NOT NULL AND expires_at < ?)
                       ORDER BY created_at)""",
                (older_than or (time.time() - 30 * 86400), time.time())).rowcount
            overflow = connection.execute(
                """DELETE FROM proactive WHERE notification_id IN (
                       SELECT notification_id FROM proactive
                       ORDER BY created_at DESC LIMIT -1 OFFSET ?)""",
                (keep,)).rowcount
        return removed + overflow


def _to_preauth(row: sqlite3.Row):
    from solvio.capabilities import preauth as PA
    return PA.Preauthorization(
        preauth_id=row["preauth_id"], automation_id=row["automation_id"],
        capability=row["capability"], action_class=row["action_class"],
        targets=tuple(json.loads(row["targets"] or "[]")),
        digest=row["digest"], revision=int(row["revision"]),
        created_at=float(row["created_at"]),
        created_origin=row["created_origin"],
        created_principal=row["created_principal"],
        enabled=bool(row["enabled"]), revoked_at=row["revoked_at"],
        revoked_reason=row["revoked_reason"] or "")


def _to_task(row: sqlite3.Row) -> Task:
    return Task(
        task_id=row["task_id"], owner=row["owner"], title=row["title"],
        created_at=row["created_at"], created_from=row["created_from"],
        schedule=json.loads(row["schedule"]), action=json.loads(row["action"]),
        state=row["state"], enabled=bool(row["enabled"]),
        next_run_at=row["next_run_at"], last_run_at=row["last_run_at"],
        last_result=json.loads(row["last_result"]) if row["last_result"] else None,
        last_error=row["last_error"] or "",
        consecutive_failures=row["consecutive_failures"],
        retry_after=row["retry_after"], conversation_ref=row["conversation_ref"] or "",
        updated_at=row["updated_at"])


def _to_item(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["notification_id"], "aufgabe": row["task_id"],
            "lauf": row["run_id"], "erstellt": row["created_at"],
            "dringlichkeit": row["priority"], "zusammenfassung": row["summary"],
            "befunde": json.loads(row["findings"]) if row["findings"] else [],
            "quelle": row["source_capability"], "content_trust": row["content_trust"],
            "gelesen": row["read_at"] is not None}
