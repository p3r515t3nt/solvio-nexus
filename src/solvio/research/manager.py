"""ResearchManager — orchestrates DISCOVER -> SELECT -> FETCH -> RANK -> REPORT
over the existing durable Background foundation (STEP 21.2H).

MAC OWNS RESEARCH. It never builds a second job queue: DISCOVER is a
research.search_public_web background job and each FETCH is a research.fetch_public_url
background job. The question stays on the Mac; only public_query goes out, and only
past the fail-closed egress gate. Search + fetched content are UNTRUSTED_WEB; the
manager never interprets web text as a command (no tool invocation), never writes
canonical memory, and never raises authority. No LLM synthesis in V1.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

from solvio.background.errors import ResultNotReady
from solvio.background.models import LOCAL_TERMINAL, LocalJobStatus
from solvio.research.errors import (
    ExternalEgressDenied,
    ResearchRunNotFound,
    SearchProviderError,
)
from solvio.research.models import (
    EvidenceReport,
    ExternalEgressPolicy,
    ResearchRunSpec,
    ResearchState,
    SourceRecord,
)
from solvio.research.planner import plan_public_query
from solvio.research.provenance import make_source_id, select_candidates
from solvio.research.ranking import rank_sources
from solvio.research.report import build_report
from solvio.nodes.models import DataClass

SEARCH_CAP = "research.search_public_web"
FETCH_CAP = "research.fetch_public_url"
_EXCERPT = 1000


@dataclass
class _Outcome:
    ok: bool
    result: dict | None = None
    error_kind: str | None = None
    error_detail: str | None = None


class ResearchManager:
    def __init__(self, store, background_manager, *, embedder=None, clock=time.time,
                 poll_interval: float = 0.3) -> None:
        self.store = store
        self.bg = background_manager
        self.embedder = embedder
        self.clock = clock
        self.poll_interval = poll_interval

    async def create_run(self, question: str, public_query: str, *, privacy_class: str,
                         egress_policy: ExternalEgressPolicy, max_sources: int = 5,
                         max_fetches: int = 5, max_duration_s: float = 120.0) -> str:
        run_id = uuid.uuid4().hex
        now = self.clock()
        spec = ResearchRunSpec(
            research_run_id=run_id, question=question, public_query=public_query,
            privacy_class=privacy_class, external_egress_policy=egress_policy,
            status=ResearchState.CREATED, max_sources=max_sources,
            max_fetches=max_fetches, max_duration_s=max_duration_s,
            created_at=now, updated_at=now)
        await self.store.create_run(spec)
        return run_id

    async def get_run(self, run_id: str) -> ResearchRunSpec:
        run = await self.store.get_run(run_id)
        if run is None:
            raise ResearchRunNotFound(run_id)
        return run

    async def get_report(self, run_id: str) -> EvidenceReport | None:
        return await self.store.get_report(run_id)

    async def run(self, run_id: str) -> EvidenceReport:
        run = await self.get_run(run_id)

        # egress gate — fail-closed, BEFORE any provider/query leaves the Mac
        try:
            query = plan_public_query(run.public_query, run.privacy_class,
                                      run.external_egress_policy)
        except ExternalEgressDenied as exc:
            await self.store.set_status(run_id, ResearchState.FAILED,
                                        error=type(exc).__name__)
            raise

        # DISCOVER (background job research.search_public_web)
        await self.store.set_status(run_id, ResearchState.DISCOVERING)
        candidates = await self._discover(run, query)
        for c in candidates:
            await self.store.add_source(SourceRecord(
                source_id=make_source_id(run_id, c["url"]), research_run_id=run_id,
                original_search_url=c["url"], provider=c.get("provider", "?"),
                provider_rank=c.get("provider_rank", 0), title=c.get("title"),
                snippet=c.get("snippet")))
        await self.store.set_status(run_id, ResearchState.DISCOVERED)

        # SELECT (deterministic, bounded)
        selected = select_candidates(candidates, run.max_sources)[: run.max_fetches]

        # FETCH (each via the SSRF-guarded research.fetch_public_url job)
        await self.store.set_status(run_id, ResearchState.FETCHING)
        await self._fetch_selected(run, selected)
        await self.store.set_status(run_id, ResearchState.FETCHED)

        # RANK (deterministic; optional local Qwen embedder)
        await self.store.set_status(run_id, ResearchState.RANKING)
        sources = await self.store.get_sources(run_id)
        ranked = rank_sources(sources, query, embedder=self.embedder)
        for s in ranked:
            await self.store.add_source(s)

        # EVIDENCE REPORT (deterministic, non-generative)
        report = build_report(run, ranked, clock=self.clock)
        await self.store.save_report(report)
        await self.store.set_status(run_id, report.status)
        return report

    async def reconcile(self) -> dict:
        """After a Core restart: refresh the durable background jobs. Research runs and
        their persisted reports are found by reopening the store."""
        bg = await self.bg.reconcile()
        runs = await self.store.list_non_terminal()
        return {"background": bg, "non_terminal_runs": len(runs)}

    # -- internals -----------------------------------------------------------
    async def _discover(self, run: ResearchRunSpec, query: str) -> list[dict]:
        lid = await self.bg.submit(SEARCH_CAP, {"query": query, "count": min(10, run.max_sources * 2)},
                                   data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        outcome = await self._await_job(lid, run.max_duration_s)
        if not outcome.ok:
            detail = outcome.error_detail or outcome.error_kind or "search failed"
            await self.store.set_status(run.research_run_id, ResearchState.FAILED, error=detail)
            raise SearchProviderError(detail)
        result = outcome.result or {}
        return [c for c in result.get("candidates", []) if isinstance(c, dict) and c.get("url")]

    async def _fetch_selected(self, run: ResearchRunSpec, selected: list[dict]) -> None:
        for c in selected:
            src = SourceRecord(
                source_id=make_source_id(run.research_run_id, c["url"]),
                research_run_id=run.research_run_id, original_search_url=c["url"],
                provider=c.get("provider", "?"), provider_rank=c.get("provider_rank", 0),
                title=c.get("title"), snippet=c.get("snippet"))
            try:
                lid = await self.bg.submit(FETCH_CAP, {"url": c["url"]},
                                           data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
                outcome = await self._await_job(lid, run.max_duration_s)
            except Exception as exc:  # noqa: BLE001 - partial failure, keep going
                src.fetch_status = "ERROR"
                src.fetch_error = type(exc).__name__
                await self.store.add_source(src)
                continue
            if outcome.ok and outcome.result:
                r = outcome.result
                src.final_fetch_url = r.get("final_url")
                src.content_sha256 = r.get("sha256")
                src.title = r.get("title") or src.title
                src.content_type = r.get("content_type")
                src.text_excerpt = (r.get("text") or "")[:_EXCERPT]
                src.retrieved_at = self.clock()
                src.fetch_status = "OK"
            else:
                src.fetch_status = "ERROR"
                src.fetch_error = outcome.error_detail or outcome.error_kind or "fetch failed"
            await self.store.add_source(src)

    async def _await_job(self, local_job_id: str, timeout: float) -> _Outcome:
        deadline = self.clock() + timeout
        spec = await self.bg.refresh(local_job_id)
        while spec.status not in LOCAL_TERMINAL and self.clock() < deadline:
            await asyncio.sleep(self.poll_interval)
            spec = await self.bg.refresh(local_job_id)
        if spec.status == LocalJobStatus.SUCCEEDED:
            try:
                out = await self.bg.result(local_job_id)
                return _Outcome(True, result=out["result"])
            except ResultNotReady:
                return _Outcome(False, error_kind="RESULT_NOT_READY")
        if spec.status not in LOCAL_TERMINAL:
            return _Outcome(False, error_kind="TIMEOUT")
        kind = ("NODE_TRANSPORT_ERROR" if spec.status == LocalJobStatus.REMOTE_UNKNOWN
                else "REMOTE_CONTENT_ERROR")
        return _Outcome(False, error_kind=kind, error_detail=spec.error_detail)
