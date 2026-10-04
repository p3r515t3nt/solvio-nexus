"""Brave Search API adapter — the FIRST provider, not a dependency.

Uses ONLY the official endpoint with the X-Subscription-Token header. The token lives
in this instance only and is NEVER placed in git, config JSON, logs, exception text,
the HTTP result, or the Core. The endpoint host is fixed (not caller-controlled), so
there is no SSRF surface here; the caller supplies only a bounded query + params.
"""
from __future__ import annotations

import asyncio
import hashlib
import ssl
import time

import aiohttp

from solvio_node.capabilities.research.providers.base import (
    MAX_COUNT,
    SearchProvider,
    SearchProviderBadResponse,
    SearchProviderUnavailable,
    SearchRateLimited,
    SearchRequest,
)
from solvio_node.capabilities.research.search_models import (
    SearchCandidate,
    SearchResponse,
)


def _str_or_none(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _parse_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class BraveSearchProvider(SearchProvider):
    provider_id = "brave"
    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, token: str | None, *, timeout_s: float = 10.0) -> None:
        self._token = (token or "").strip() or None
        self._timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        return self._token is not None

    async def search(self, request: SearchRequest) -> SearchResponse:
        if not self.configured:
            raise SearchProviderUnavailable("brave not configured")
        params = {"q": request.query, "count": str(min(request.count, MAX_COUNT))}
        if request.country:
            params["country"] = request.country
        if request.language:
            params["search_lang"] = request.language
        if request.freshness:
            params["freshness"] = request.freshness

        started = time.perf_counter()
        try:
            status, headers, body = await self._http_get(params)
        except asyncio.TimeoutError:
            raise SearchProviderUnavailable("brave timeout") from None
        except aiohttp.ClientError:
            raise SearchProviderUnavailable("brave connection error") from None
        duration_ms = (time.perf_counter() - started) * 1000

        if status == 429:
            raise SearchRateLimited(retry_after=_parse_float(headers.get("Retry-After")))
        if status != 200 or not isinstance(body, dict):
            raise SearchProviderBadResponse(f"brave http {status}")
        results = ((body.get("web") or {}).get("results")) or []
        if not isinstance(results, list):
            raise SearchProviderBadResponse("brave response shape")

        candidates: list[SearchCandidate] = []
        for i, r in enumerate(results[: request.count]):
            if not isinstance(r, dict):
                continue
            url = r.get("url")
            if not isinstance(url, str) or not url:
                continue
            candidates.append(SearchCandidate(
                candidate_id=hashlib.sha256(f"{i}:{url}".encode()).hexdigest()[:16],
                url=url, title=(str(r.get("title") or ""))[:300],
                snippet=(str(r.get("description") or ""))[:500], provider="brave",
                provider_rank=i, published_at=_str_or_none(r.get("page_age") or r.get("age")),
                language=_str_or_none(r.get("language"))))

        return SearchResponse(
            query_digest=hashlib.sha256(request.query.encode("utf-8")).hexdigest(),
            provider="brave", candidates=candidates, duration_ms=duration_ms,
            provider_request_id=_str_or_none(headers.get("X-Request-Id")))

    async def _http_get(self, params: dict) -> tuple[int, dict, dict | None]:
        """The single network seam (unit tests override this — no real Brave call)."""
        headers = {"X-Subscription-Token": self._token, "Accept": "application/json"}
        ssl_ctx = ssl.create_default_context()
        timeout = aiohttp.ClientTimeout(total=self._timeout_s, connect=5)
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False,
                                         connector=connector) as session:
            async with session.get(self.ENDPOINT, params=params, headers=headers) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001
                    body = None
                return resp.status, dict(resp.headers), body
