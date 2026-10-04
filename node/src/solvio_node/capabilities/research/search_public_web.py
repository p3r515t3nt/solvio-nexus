"""research.search_public_web — bounded external web search.

Returns normalised search candidates from an external provider (Brave first). The
caller supplies ONLY a bounded query + params — no headers, provider URL, endpoint,
API key, or proxy. Results (title/url/snippet/rank) are DATA; the Mac Core classifies
them as UNTRUSTED_WEB and never treats provider rank as truth. Read-only, stateless.
If no provider is configured (no key), invoke fails closed with a provider-unavailable
error — the node stays healthy.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from solvio_node.capabilities.base import (
    Capability,
    ExecutionMode,
    PrivacyClass,
    RiskLevel,
)
from solvio_node.capabilities.research.providers.base import (
    SearchProviderUnavailable,
    SearchRequest,
)
from solvio_node.capabilities.research.providers.registry import build_default_provider
from solvio_node.capabilities.research.search_models import (
    SearchCandidate,
    SearchResponse,
)


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # no caller-supplied provider/url/key/headers
    query: str = Field(max_length=400)
    count: int = Field(default=5, ge=1, le=10)
    country: str | None = Field(default=None, max_length=8)
    language: str | None = Field(default=None, max_length=16)
    freshness: str | None = Field(default=None, max_length=16)


class SearchOut(BaseModel):
    query_digest: str
    provider: str
    candidates: list[SearchCandidate] = Field(default_factory=list)
    duration_ms: float | None = None
    provider_request_id: str | None = None


class SearchPublicWebCapability(Capability):
    id = "research.search_public_web"
    version = "1"
    description = ("Search the public web via an external provider (Brave). Bounded "
                   "query only; results are UNTRUSTED_WEB, provider rank is not truth.")
    risk_level = RiskLevel.SAFE
    privacy_class = PrivacyClass.SYNTHETIC
    execution_mode = ExecutionMode.INLINE
    supports_batch = False
    supports_background = True
    max_concurrency = 2
    timeout_s = 15.0
    persistent_data = False
    input_model = SearchIn
    output_model = SearchOut

    def __init__(self, *, provider=None) -> None:
        self._provider = provider if provider is not None else build_default_provider()

    async def invoke(self, data: BaseModel) -> SearchOut:
        assert isinstance(data, SearchIn)
        if self._provider is None or not self._provider.configured:
            raise SearchProviderUnavailable("no search provider configured")
        resp: SearchResponse = await self._provider.search(SearchRequest(
            query=data.query, count=data.count, country=data.country,
            language=data.language, freshness=data.freshness))
        return SearchOut(query_digest=resp.query_digest, provider=resp.provider,
                         candidates=resp.candidates, duration_ms=resp.duration_ms,
                         provider_request_id=resp.provider_request_id)

    async def health(self) -> dict:
        configured = self._provider is not None and self._provider.configured
        return {"id": self.id, "ok": True, "configured": configured,
                "provider": (self._provider.provider_id if self._provider else None)}
