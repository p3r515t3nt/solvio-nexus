"""Core local job ledger (SQLite) — the authoritative durable record (STEP 21.2G).

Separate operative DB (`background_jobs.sqlite3`), NOT memory.sqlite3 and clearly
separated from canonical memory. File 0600, directory restrictive. All operations
run in a single-thread executor. On integrity failure it fails closed — it never
fabricates jobs and never destructively recreates itself.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from solvio.background.errors import LocalStoreCorrupt
from solvio.background.models import BackgroundJobSpec, LocalJobStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_jobs (
    local_job_id     TEXT PRIMARY KEY,
    remote_job_id    TEXT,
    capability_id    TEXT NOT NULL,
    node_id          TEXT NOT NULL,
    privacy_class    TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL,
    status           TEXT NOT NULL,
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    submitted_at     REAL,
    input_payload    TEXT,
    result_available INTEGER NOT NULL DEFAULT 0,
    result_digest    TEXT,
    error_detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_local_status ON local_jobs(status);
"""


def _row_to_spec(row: sqlite3.Row) -> BackgroundJobSpec:
    return BackgroundJobSpec(
        local_job_id=row["local_job_id"], remote_job_id=row["remote_job_id"],
        capability_id=row["capability_id"], node_id=row["node_id"],
        privacy_class=row["privacy_class"], status=LocalJobStatus(row["status"]),
        attempt_count=row["attempt_count"], max_attempts=row["max_attempts"],
        created_at=row["created_at"], submitted_at=row["submitted_at"],
        updated_at=row["updated_at"], result_available=bool(row["result_available"]),
        result_digest=row["result_digest"], error_detail=row["error_detail"])


class LocalJobStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="solvio-bg")
        self._conn: sqlite3.Connection | None = None

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    async def open(self) -> None:
        await self._run(self._open)

    def _open(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        first = not os.path.exists(self.path)
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            conn.close()
            raise LocalStoreCorrupt("integrity_check failed")
        conn.executescript(_SCHEMA)
        if first:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self._conn = conn

    async def close(self) -> None:
        await self._run(self._close)
        self._pool.shutdown(wait=True)

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- crash-safe submit lifecycle ----------------------------------------
    async def create_pending(self, *, local_job_id, capability_id, node_id,
                             privacy_class, idempotency_key, payload,
                             max_attempts) -> None:
        await self._run(self._create_pending, local_job_id, capability_id, node_id,
                        privacy_class, idempotency_key, payload, max_attempts)

    def _create_pending(self, local_job_id, capability_id, node_id, privacy_class,
                        idempotency_key, payload, max_attempts) -> None:
        now = time.time()
        self._conn.execute(
            "INSERT INTO local_jobs (local_job_id, capability_id, node_id, "
            "privacy_class, idempotency_key, status, max_attempts, created_at, "
            "updated_at, input_payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (local_job_id, capability_id, node_id, privacy_class, idempotency_key,
             LocalJobStatus.LOCAL_PENDING_SUBMIT.value, max_attempts, now, now,
             json.dumps(payload, separators=(",", ":"))))

    async def mark_submitted(self, local_job_id, remote_job_id, status) -> None:
        await self._run(self._mark_submitted, local_job_id, remote_job_id, status)

    def _mark_submitted(self, local_job_id, remote_job_id, status):
        now = time.time()
        self._conn.execute(
            "UPDATE local_jobs SET remote_job_id=?, status=?, submitted_at=?, "
            "updated_at=? WHERE local_job_id=?",
            (remote_job_id, status.value, now, now, local_job_id))

    async def update_status(self, local_job_id, status, *, attempt_count=None,
                            error_detail=None) -> None:
        await self._run(self._update_status, local_job_id, status, attempt_count,
                        error_detail)

    def _update_status(self, local_job_id, status, attempt_count, error_detail):
        now = time.time()
        sets = ["status=?", "updated_at=?"]
        args = [status.value, now]
        if attempt_count is not None:
            sets.append("attempt_count=?")
            args.append(attempt_count)
        if error_detail is not None:
            sets.append("error_detail=?")
            args.append(error_detail)
        args.append(local_job_id)
        self._conn.execute(f"UPDATE local_jobs SET {', '.join(sets)} WHERE local_job_id=?", args)

    async def set_result_meta(self, local_job_id, available, digest) -> None:
        await self._run(self._set_result_meta, local_job_id, available, digest)

    def _set_result_meta(self, local_job_id, available, digest):
        now = time.time()
        self._conn.execute(
            "UPDATE local_jobs SET result_available=?, result_digest=?, updated_at=? "
            "WHERE local_job_id=?", (1 if available else 0, digest, now, local_job_id))

    # -- reads ---------------------------------------------------------------
    async def get(self, local_job_id) -> BackgroundJobSpec | None:
        return await self._run(self._get, local_job_id)

    def _get(self, local_job_id):
        row = self._conn.execute("SELECT * FROM local_jobs WHERE local_job_id=?",
                                 (local_job_id,)).fetchone()
        return _row_to_spec(row) if row else None

    async def resend_info(self, local_job_id) -> dict | None:
        return await self._run(self._resend_info, local_job_id)

    def _resend_info(self, local_job_id):
        row = self._conn.execute(
            "SELECT capability_id, node_id, privacy_class, idempotency_key, "
            "input_payload, max_attempts FROM local_jobs WHERE local_job_id=?",
            (local_job_id,)).fetchone()
        if row is None:
            return None
        return {"capability_id": row["capability_id"], "node_id": row["node_id"],
                "privacy_class": row["privacy_class"],
                "idempotency_key": row["idempotency_key"],
                "payload": json.loads(row["input_payload"]) if row["input_payload"] else {},
                "max_attempts": row["max_attempts"]}

    async def list_non_terminal(self) -> list[BackgroundJobSpec]:
        return await self._run(self._list_non_terminal)

    def _list_non_terminal(self):
        rows = self._conn.execute(
            "SELECT * FROM local_jobs WHERE status NOT IN "
            "('SUCCEEDED','FAILED','CANCELLED','RESULT_EXPIRED')").fetchall()
        return [_row_to_spec(r) for r in rows]
