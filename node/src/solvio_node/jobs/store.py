"""Durable job store (SQLite) for the node execution plane.

Separate DB (`jobs.sqlite3`), NOT memory/semantic/ledger. WAL + foreign_keys +
busy_timeout; file 0600. All operations run in a single-thread executor, which
serialises them — so a claim (SELECT next QUEUED + UPDATE to RUNNING) is atomic
without extra locking, even with multiple worker tasks.

This store holds EPHEMERAL OPERATIONAL STATE only. Input payloads are purged the
moment a job is terminal; result payloads have a TTL; metadata has a longer TTL.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from solvio_node.jobs.model import Job, JobStatus, hash_idempotency_key

RESULT_TTL_S = 72 * 3600        # result payload kept 72h
METADATA_TTL_S = 7 * 86400      # metadata kept 7 days

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id            TEXT PRIMARY KEY,
    client_identity   TEXT NOT NULL,
    capability_id     TEXT NOT NULL,
    privacy_class     TEXT NOT NULL,
    idempotency_hash  TEXT NOT NULL,
    status            TEXT NOT NULL,
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 3,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    submitted_at      REAL,
    payload_size      INTEGER NOT NULL,
    input_payload     TEXT,
    result_payload    TEXT,
    result_size       INTEGER,
    result_digest     TEXT,
    error_code        TEXT,
    result_expires_at REAL,
    metadata_expires_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_idem
    ON jobs(client_identity, capability_id, idempotency_hash);
CREATE INDEX IF NOT EXISTS idx_status ON jobs(status);
"""


class JobStoreError(Exception):
    pass


class JobStoreCorrupt(JobStoreError):
    pass


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        job_id=row["job_id"], client_identity=row["client_identity"],
        capability_id=row["capability_id"], privacy_class=row["privacy_class"],
        status=JobStatus(row["status"]), attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"], created_at=row["created_at"],
        updated_at=row["updated_at"], submitted_at=row["submitted_at"],
        payload_size=row["payload_size"], result_size=row["result_size"],
        result_digest=row["result_digest"], error_code=row["error_code"],
        result_available=bool(row["result_payload_present"]),
        result_expires_at=row["result_expires_at"],
        metadata_expires_at=row["metadata_expires_at"],
    )


class JobStore:
    def __init__(self, path: str, *, result_ttl_s: float = RESULT_TTL_S,
                 metadata_ttl_s: float = METADATA_TTL_S) -> None:
        self.path = path
        self.result_ttl_s = result_ttl_s
        self.metadata_ttl_s = metadata_ttl_s
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="solvio-jobs")
        self._conn: sqlite3.Connection | None = None

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    # -- lifecycle -----------------------------------------------------------
    async def open(self) -> None:
        await self._run(self._open)

    def _open(self) -> None:
        first = not os.path.exists(self.path)
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            conn.close()
            raise JobStoreCorrupt("integrity_check failed")
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

    # -- create (idempotent) -------------------------------------------------
    async def create(self, *, client_identity: str, capability_id: str,
                     privacy_class: str, idempotency_key: str, payload: dict,
                     max_attempts: int) -> tuple[str, bool]:
        return await self._run(self._create, client_identity, capability_id,
                               privacy_class, idempotency_key, payload, max_attempts)

    def _create(self, client_identity, capability_id, privacy_class,
                idempotency_key, payload, max_attempts) -> tuple[str, bool]:
        c = self._conn
        idem = hash_idempotency_key(client_identity, capability_id, idempotency_key)
        row = c.execute(
            "SELECT job_id FROM jobs WHERE client_identity=? AND capability_id=? "
            "AND idempotency_hash=?", (client_identity, capability_id, idem)).fetchone()
        if row is not None:
            return row["job_id"], False   # duplicate submit -> existing job, no re-exec
        now = time.time()
        job_id = uuid.uuid4().hex
        raw = json.dumps(payload, separators=(",", ":"))
        c.execute(
            "INSERT INTO jobs (job_id, client_identity, capability_id, privacy_class, "
            "idempotency_hash, status, attempt_count, max_attempts, created_at, "
            "updated_at, submitted_at, payload_size, input_payload, "
            "metadata_expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, client_identity, capability_id, privacy_class, idem,
             JobStatus.QUEUED.value, 0, max_attempts, now, now, now, len(raw), raw,
             now + self.metadata_ttl_s))
        return job_id, True

    # -- read (ownership-enforced) ------------------------------------------
    async def get(self, job_id: str, client_identity: str) -> Job | None:
        return await self._run(self._get, job_id, client_identity)

    def _get(self, job_id, client_identity) -> Job | None:
        row = self._conn.execute(
            "SELECT *, (result_payload IS NOT NULL) AS result_payload_present "
            "FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None or row["client_identity"] != client_identity:
            return None                    # not owner -> behaves as not-found
        return _row_to_job(row)

    async def get_result(self, job_id: str, client_identity: str) -> dict | None:
        return await self._run(self._get_result, job_id, client_identity)

    def _get_result(self, job_id, client_identity):
        row = self._conn.execute(
            "SELECT client_identity, status, result_payload, result_digest "
            "FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None or row["client_identity"] != client_identity:
            return None
        if row["result_payload"] is None:
            return {"available": False, "status": row["status"]}
        return {"available": True, "status": row["status"],
                "result": json.loads(row["result_payload"]),
                "result_digest": row["result_digest"]}

    # -- worker claim + finish ----------------------------------------------
    async def claim_next(self):
        return await self._run(self._claim_next)

    def _claim_next(self):
        c = self._conn
        row = c.execute(
            "SELECT job_id, capability_id, input_payload, attempt_count, max_attempts "
            "FROM jobs WHERE status='QUEUED' ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            return None
        now = time.time()
        cur = c.execute("UPDATE jobs SET status='RUNNING', attempt_count=attempt_count+1, "
                        "updated_at=? WHERE job_id=? AND status='QUEUED'",
                        (now, row["job_id"]))
        if cur.rowcount == 0:
            return None
        payload = json.loads(row["input_payload"]) if row["input_payload"] else {}
        return {"job_id": row["job_id"], "capability_id": row["capability_id"],
                "payload": payload, "attempt_count": row["attempt_count"] + 1,
                "max_attempts": row["max_attempts"]}

    async def is_cancel_requested(self, job_id: str) -> bool:
        return await self._run(self._is_cancel_requested, job_id)

    def _is_cancel_requested(self, job_id) -> bool:
        row = self._conn.execute("SELECT status FROM jobs WHERE job_id=?",
                                 (job_id,)).fetchone()
        return row is not None and row["status"] == "CANCEL_REQUESTED"

    async def finish_success(self, job_id: str, result: dict, digest: str) -> None:
        await self._run(self._finish_success, job_id, result, digest)

    def _finish_success(self, job_id, result, digest):
        c = self._conn
        cur = c.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if cur is None:
            return
        raw = json.dumps(result, separators=(",", ":"))
        now = time.time()
        # honour a cancel requested mid-run
        status = (JobStatus.CANCELLED.value if cur["status"] == "CANCEL_REQUESTED"
                  else JobStatus.SUCCEEDED.value)
        c.execute("UPDATE jobs SET status=?, result_payload=?, result_size=?, "
                  "result_digest=?, input_payload=NULL, updated_at=?, "
                  "result_expires_at=? WHERE job_id=?",
                  (status, raw, len(raw), digest, now, now + self.result_ttl_s, job_id))

    async def finish_failed(self, job_id: str, error_code: str) -> None:
        await self._run(self._finish_failed, job_id, error_code)

    def _finish_failed(self, job_id, error_code):
        now = time.time()
        self._conn.execute(
            "UPDATE jobs SET status='FAILED', error_code=?, input_payload=NULL, "
            "updated_at=? WHERE job_id=?", (error_code, now, job_id))

    async def finish_cancelled(self, job_id: str) -> None:
        await self._run(self._finish_cancelled, job_id)

    def _finish_cancelled(self, job_id):
        now = time.time()
        self._conn.execute(
            "UPDATE jobs SET status='CANCELLED', input_payload=NULL, updated_at=? "
            "WHERE job_id=?", (now, job_id))

    async def requeue(self, job_id: str) -> None:
        await self._run(self._requeue, job_id)

    def _requeue(self, job_id):
        now = time.time()
        self._conn.execute(
            "UPDATE jobs SET status='QUEUED', updated_at=? WHERE job_id=? "
            "AND status IN ('RUNNING','CANCEL_REQUESTED')", (now, job_id))

    # -- cancel --------------------------------------------------------------
    async def request_cancel(self, job_id: str, client_identity: str) -> str | None:
        return await self._run(self._request_cancel, job_id, client_identity)

    def _request_cancel(self, job_id, client_identity):
        c = self._conn
        row = c.execute("SELECT client_identity, status FROM jobs WHERE job_id=?",
                        (job_id,)).fetchone()
        if row is None or row["client_identity"] != client_identity:
            return None
        st, now = row["status"], time.time()
        if st == "QUEUED":
            c.execute("UPDATE jobs SET status='CANCELLED', input_payload=NULL, "
                      "updated_at=? WHERE job_id=?", (now, job_id))
            return "CANCELLED"
        if st == "RUNNING":
            c.execute("UPDATE jobs SET status='CANCEL_REQUESTED', updated_at=? "
                      "WHERE job_id=?", (now, job_id))
            return "CANCEL_REQUESTED"
        return st  # already terminal / cancel already requested

    # -- recovery / bounds / cleanup ----------------------------------------
    async def recover(self) -> dict:
        return await self._run(self._recover)

    def _recover(self) -> dict:
        c, now = self._conn, time.time()
        req = c.execute("UPDATE jobs SET status='QUEUED', updated_at=? WHERE "
                        "status='RUNNING' AND attempt_count < max_attempts", (now,)).rowcount
        failed = c.execute("UPDATE jobs SET status='FAILED', error_code='RECOVERY_EXHAUSTED', "
                           "input_payload=NULL, updated_at=? WHERE status='RUNNING'",
                           (now,)).rowcount
        canc = c.execute("UPDATE jobs SET status='CANCELLED', input_payload=NULL, "
                         "updated_at=? WHERE status='CANCEL_REQUESTED'", (now,)).rowcount
        return {"requeued": req, "failed": failed, "cancelled": canc}

    async def count_active(self) -> int:
        return await self._run(self._count_active)

    def _count_active(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN "
            "('QUEUED','RUNNING','CANCEL_REQUESTED')").fetchone()[0]

    async def cleanup_expired(self) -> dict:
        return await self._run(self._cleanup_expired)

    def _cleanup_expired(self) -> dict:
        c, now = self._conn, time.time()
        exp = c.execute(
            "UPDATE jobs SET status='RESULT_EXPIRED', result_payload=NULL, "
            "updated_at=? WHERE status='SUCCEEDED' AND result_expires_at IS NOT NULL "
            "AND result_expires_at < ?", (now, now)).rowcount
        deleted = c.execute(
            "DELETE FROM jobs WHERE metadata_expires_at IS NOT NULL "
            "AND metadata_expires_at < ? AND status IN "
            "('SUCCEEDED','FAILED','CANCELLED','RESULT_EXPIRED')", (now,)).rowcount
        return {"result_expired": exp, "metadata_deleted": deleted}

    async def has_queued(self) -> bool:
        return await self._run(self._has_queued)

    def _has_queued(self) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM jobs WHERE status='QUEUED' LIMIT 1").fetchone() is not None
