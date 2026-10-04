"""Vendor-neutral search provider contract.

A provider turns a bounded SearchRequest into a normalised SearchResponse. The Core's
research contracts never see a provider-specific model. Brave is the FIRST provider,
not an architectural dependency; Tavily / OpenAI web search / others can be added
without changing the Core contract.

The caller (the capability) supplies ONLY a bounded query + limited search params —
never headers, a provider URL, an API endpoint, an API key, a proxy, or raw query
params. Secrets live only in the provider instance, never in the request.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from solvio_node.capabilities.research.search_models import SearchResponse

MAX_QUERY_LEN = 400
MAX_COUNT = 10


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(max_length=MAX_QUERY_LEN)
    count: int = Field(default=5, ge=1, le=MAX_COUNT)
    country: str | None = Field(default=None, max_length=8)
    language: str | None = Field(default=None, max_length=16)
    freshness: str | None = Field(default=None, max_length=16)


class SearchProviderError(Exception):
    """Base for provider errors (surfaced payload-free, only a type name)."""


class SearchProviderUnavailable(SearchProviderError):
    """Provider not configured (no key) or down. Distinct from a node/transport error."""


class SearchRateLimited(SearchProviderError):
    """Provider returned 429. Carries an optional retry-after (seconds)."""

    def __init__(self, message: str = "rate limited", *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class SearchProviderBadResponse(SearchProviderError):
    """Provider returned an unexpected/malformed response."""


class SearchProvider(ABC):
    provider_id: ClassVar[str]

    @property
    def configured(self) -> bool:
        return True

    @abstractmethod
    async def search(self, request: SearchRequest) -> SearchResponse:
        ...

    async def health(self) -> dict:
        return {"provider": self.provider_id, "configured": self.configured}

    def capabilities(self) -> dict:
        return {"provider": self.provider_id, "max_query_len": MAX_QUERY_LEN,
                "max_count": MAX_COUNT}
