"""Deterministic evidence report builder (STEP 21.2H, PHASE 27/28). NO LLM synthesis.

Produces an EvidenceReport with provenance and a stable citation model. It never makes
a truth claim and never summarises with a model — it lists ranked sources, bounded
excerpts, timestamps, hashes, and partial-failure errors.
"""
from __future__ import annotations

import time

from solvio.research.models import (
    Citation,
    EvidenceReport,
    ResearchRunSpec,
    ResearchState,
    SourceRecord,
)

_EXCERPT = 400


def build_report(run: ResearchRunSpec, ranked: list[SourceRecord], *,
                 clock=time.time) -> EvidenceReport:
    fetched = [s for s in ranked if s.fetch_status == "OK"]
    # Only genuine fetch failures are errors. A discovered candidate that was never
    # SELECTED for fetch stays PENDING and is neither evidence nor a failure — it must
    # not pollute errors[] nor drag an otherwise-complete run to PARTIAL (STEP 21.2H).
    errors = [{"url": s.original_search_url, "final_url": s.final_fetch_url,
               "status": s.fetch_status, "error": s.fetch_error}
              for s in ranked if s.fetch_status == "ERROR"]

    top_sources = []
    citations = []
    for s in ranked:
        excerpt = (s.text_excerpt or "")[:_EXCERPT] if s.fetch_status == "OK" else None
        top_sources.append({
            "title": s.title, "url": s.final_fetch_url or s.original_search_url,
            "provider": s.provider, "provider_rank": s.provider_rank,
            "rank_score": s.rank_score, "fetch_status": s.fetch_status,
            "retrieved_at": s.retrieved_at, "content_sha256": s.content_sha256,
            "excerpt": excerpt,
        })
        if s.fetch_status == "OK":
            citations.append(Citation(
                citation_id=f"cite-{s.source_id}", source_id=s.source_id,
                url=s.final_fetch_url or s.original_search_url, title=s.title,
                retrieved_at=s.retrieved_at, content_digest=s.content_sha256,
                excerpt=excerpt))

    if fetched and not errors:
        status = ResearchState.READY
    elif fetched:
        status = ResearchState.PARTIAL
    else:
        status = ResearchState.FAILED

    return EvidenceReport(
        research_run_id=run.research_run_id, topic=run.question,
        search_query=run.public_query, status=status,
        sources_discovered=len(ranked), sources_fetched=len(fetched),
        top_sources=top_sources, citations=citations, errors=errors,
        generated_at=clock())
