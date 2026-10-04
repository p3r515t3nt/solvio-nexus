"""Research run ledger (SQLite) — MAC ONLY (STEP 21.2H).

Separate DB `research_runs.sqlite3`, NOT memory.sqlite3 and NOT background_jobs.sqlite3.
Stores research intent, query-planning metadata, candidate provenance, fetch
references, ranking, and the final evidence report. File 0600, directory restrictive.
Fails closed on an integrity failure — never fabricates runs.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from solvio.research.errors import ResearchStoreCorrupt
from solvio.research.models import (
    EvidenceReport,
    ExternalEgressPolicy,
    ResearchRunSpec,
    ResearchState,
    SourceRecord,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_runs (
    research_run_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    public_query TEXT NOT NULL,
    privacy_class TEXT NOT NULL,
    egress_policy TEXT NOT NULL,
    status TEXT NOT NULL,
    max_sources INTEGER NOT NULL,
    max_search_requests INTEGER NOT NULL,
    max_fetches INTEGER NOT NULL,
    max_duration_s REAL NOT NULL,
    deadline REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    error_detail TEXT
);
CREATE TABLE IF NOT EXISTS research_sources (
    source_id TEXT PRIMARY KEY,
    research_run_id TEXT NOT NULL,
    original_search_url TEXT NOT NULL,
    final_fetch_url TEXT,
    provider TEXT NOT NULL,
    provider_rank INTEGER NOT NULL,
    title TEXT, snippet TEXT,
    retrieved_at REAL, content_sha256 TEXT, content_type TEXT,
    fetch_status TEXT NOT NULL DEFAULT 'PENDING', fetch_error TEXT,
    rank_score REAL, text_excerpt TEXT
);
CREATE INDEX IF NOT EXISTS idx_src_run ON research_sources(research_run_id);
CREATE TABLE IF NOT EXISTS research_reports (
    research_run_id TEXT PRIMARY KEY,
    report_json TEXT NOT NULL
);
"""


def _run_from_row(r: sqlite3.Row) -> ResearchRunSpec:
    return ResearchRunSpec(
        research_run_id=r["research_run_id"], question=r["question"],
        public_query=r["public_query"], privacy_class=r["privacy_class"],
        external_egress_policy=ExternalEgressPolicy(r["egress_policy"]),
        status=ResearchState(r["status"]), max_sources=r["max_sources"],
        max_search_requests=r["max_search_requests"], max_fetches=r["max_fetches"],
        max_duration_s=r["max_duration_s"], deadline=r["deadline"],
        created_at=r["created_at"], updated_at=r["updated_at"],
        error_detail=r["error_detail"])


def _source_from_row(r: sqlite3.Row) -> SourceRecord:
    return SourceRecord(**{k: r[k] for k in r.keys()})


class ResearchStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="solvio-research")
        self._conn: sqlite3.Connection | None = None
        self._closed = False

    async def _run(self, fn, *a):
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *a)

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
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            conn.close()
            raise ResearchStoreCorrupt("integrity_check failed")
        conn.executescript(_SCHEMA)
        if first:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self._conn = conn

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._run(self._close)
        self._pool.shutdown(wait=True)

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- runs ----------------------------------------------------------------
    async def create_run(self, spec: ResearchRunSpec) -> None:
        await self._run(self._create_run, spec)

    def _create_run(self, spec: ResearchRunSpec) -> None:
        self._conn.execute(
            "INSERT INTO research_runs (research_run_id, question, public_query, "
            "privacy_class, egress_policy, status, max_sources, max_search_requests, "
            "max_fetches, max_duration_s, deadline, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (spec.research_run_id, spec.question, spec.public_query, spec.privacy_class,
             spec.external_egress_policy.value, spec.status.value, spec.max_sources,
             spec.max_search_requests, spec.max_fetches, spec.max_duration_s,
             spec.deadline, spec.created_at, spec.updated_at))

    async def set_status(self, run_id: str, status: ResearchState, *, error=None) -> None:
        await self._run(self._set_status, run_id, status, error)

    def _set_status(self, run_id, status, error):
        self._conn.execute(
            "UPDATE research_runs SET status=?, updated_at=?, error_detail=COALESCE(?, error_detail) "
            "WHERE research_run_id=?", (status.value, time.time(), error, run_id))

    async def get_run(self, run_id: str) -> ResearchRunSpec | None:
        return await self._run(self._get_run, run_id)

    def _get_run(self, run_id):
        r = self._conn.execute("SELECT * FROM research_runs WHERE research_run_id=?",
                               (run_id,)).fetchone()
        return _run_from_row(r) if r else None

    async def list_non_terminal(self) -> list[ResearchRunSpec]:
        return await self._run(self._list_non_terminal)

    def _list_non_terminal(self):
        rows = self._conn.execute(
            "SELECT * FROM research_runs WHERE status NOT IN "
            "('READY','PARTIAL','FAILED','CANCELLED')").fetchall()
        return [_run_from_row(r) for r in rows]

    # -- sources -------------------------------------------------------------
    async def add_source(self, s: SourceRecord) -> None:
        await self._run(self._add_source, s)

    def _add_source(self, s: SourceRecord):
        self._conn.execute(
            "INSERT OR REPLACE INTO research_sources (source_id, research_run_id, "
            "original_search_url, final_fetch_url, provider, provider_rank, title, "
            "snippet, retrieved_at, content_sha256, content_type, fetch_status, "
            "fetch_error, rank_score, text_excerpt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s.source_id, s.research_run_id, s.original_search_url, s.final_fetch_url,
             s.provider, s.provider_rank, s.title, s.snippet, s.retrieved_at,
             s.content_sha256, s.content_type, s.fetch_status, s.fetch_error,
             s.rank_score, s.text_excerpt))

    async def get_sources(self, run_id: str) -> list[SourceRecord]:
        return await self._run(self._get_sources, run_id)

    def _get_sources(self, run_id):
        rows = self._conn.execute(
            "SELECT * FROM research_sources WHERE research_run_id=? ORDER BY provider_rank",
            (run_id,)).fetchall()
        return [_source_from_row(r) for r in rows]

    # -- report --------------------------------------------------------------
    async def save_report(self, report: EvidenceReport) -> None:
        await self._run(self._save_report, report)

    def _save_report(self, report: EvidenceReport):
        self._conn.execute(
            "INSERT OR REPLACE INTO research_reports (research_run_id, report_json) "
            "VALUES (?,?)", (report.research_run_id, report.model_dump_json()))

    async def get_report(self, run_id: str) -> EvidenceReport | None:
        return await self._run(self._get_report, run_id)

    def _get_report(self, run_id):
        r = self._conn.execute(
            "SELECT report_json FROM research_reports WHERE research_run_id=?",
            (run_id,)).fetchone()
        return EvidenceReport.model_validate(json.loads(r["report_json"])) if r else None
