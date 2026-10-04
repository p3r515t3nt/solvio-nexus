"""Provider-neutral search models.

The node normalises every search provider into these types. A provider's raw JSON is
NEVER passed unfiltered through the stack. Search titles/snippets/urls and provider
ranking are DATA, not truth: the Mac Core classifies the content as UNTRUSTED_WEB and
never treats provider rank as a truth signal.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SearchCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    candidate_id: str
    url: str
    title: str = ""
    snippet: str = ""
    provider: str
    provider_rank: int
    published_at: str | None = None
    language: str | None = None


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    query_digest: str            # sha256 of the query — the query text is never returned/logged
    provider: str
    candidates: list[SearchCandidate] = Field(default_factory=list)
    duration_ms: float | None = None
    provider_request_id: str | None = None
