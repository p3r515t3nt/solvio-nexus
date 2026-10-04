"""Core research-engine models (STEP 21.2H).

MAC OWNS RESEARCH INTENT. The `question` never leaves the Mac; only `public_query`
may go to an external search provider, and only when the external-egress policy
allows it. Search + fetched content are UNTRUSTED_WEB; provider rank is not truth.
No LLM synthesis in V1 — the output is a deterministic evidence report.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ExternalEgressPolicy(str, Enum):
    """Separate from placement privacy: even a REMOTE_CONTROLLED_ALLOWED job must not
    be sent to a third-party provider unless egress is explicitly allowed."""
    NO_EXTERNAL_EGRESS = "no_external_egress"                    # default (fail-closed)
    PUBLIC_QUERY_ALLOWED = "public_query_allowed"               # a sanitized public query may go out
    EXPLICIT_THIRD_PARTY_ALLOWED = "explicit_third_party_allowed"


class ResearchState(str, Enum):
    CREATED = "CREATED"
    DISCOVERING = "DISCOVERING"
    DISCOVERED = "DISCOVERED"
    FETCHING = "FETCHING"
    FETCHED = "FETCHED"
    RANKING = "RANKING"
    READY = "READY"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


RESEARCH_TERMINAL: frozenset[ResearchState] = frozenset({
    ResearchState.READY, ResearchState.PARTIAL,
    ResearchState.FAILED, ResearchState.CANCELLED,
})


class ResearchRunSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    research_run_id: str
    question: str                       # Mac-only; never sent to a provider
    public_query: str                   # the ONLY text allowed to a search provider
    privacy_class: str                  # DataClass name, e.g. REMOTE_CONTROLLED_ALLOWED
    external_egress_policy: ExternalEgressPolicy
    status: ResearchState
    max_sources: int = 5
    max_search_requests: int = 1
    max_fetches: int = 5
    max_duration_s: float = 120.0
    deadline: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    error_detail: str | None = None


class SourceRecord(BaseModel):
    """Full provenance for one discovered/fetched source."""
    model_config = ConfigDict(extra="ignore")
    source_id: str
    research_run_id: str
    original_search_url: str
    final_fetch_url: str | None = None
    provider: str
    provider_rank: int
    title: str | None = None
    snippet: str | None = None
    retrieved_at: float | None = None
    content_sha256: str | None = None
    content_type: str | None = None
    fetch_status: str = "PENDING"       # PENDING | OK | ERROR
    fetch_error: str | None = None
    rank_score: float | None = None
    text_excerpt: str | None = None


class Citation(BaseModel):
    citation_id: str
    source_id: str
    url: str
    title: str | None = None
    retrieved_at: float | None = None
    content_digest: str | None = None
    excerpt: str | None = None


class EvidenceReport(BaseModel):
    """Deterministic, NON-generative evidence report. Never claims truth."""
    research_run_id: str
    topic: str                          # the question (Mac-owned)
    search_query: str                   # public_query
    status: ResearchState
    sources_discovered: int
    sources_fetched: int
    top_sources: list[dict] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
    generated_at: float = 0.0
    trust_note: str = ("All sources are UNTRUSTED_WEB. This is retrieved evidence with "
                       "provenance, not a verified answer; no synthesis was performed.")
