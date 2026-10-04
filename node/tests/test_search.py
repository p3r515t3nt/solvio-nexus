""" — node search provider + capability (fake HTTP; no real internet).

Covers Phase 40: provider contract, Brave parsing, secret redaction, query/count
bounds, provider unavailable, 429, timeout, malformed, no external caller params,
background-enabled, registry.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import unittest
from unittest import IsolatedAsyncioTestCase

from pydantic import ValidationError

from solvio_node.capabilities.research.providers.base import (
    SearchProvider,
    SearchProviderBadResponse,
    SearchProviderUnavailable,
    SearchRateLimited,
    SearchRequest,
)
from solvio_node.capabilities.research.providers.brave import BraveSearchProvider
from solvio_node.capabilities.research.providers.registry import (
    KNOWN_PROVIDERS,
    build_default_provider,
)
from solvio_node.capabilities.research.search_models import SearchResponse
from solvio_node.capabilities.research.search_public_web import (
    SearchIn,
    SearchPublicWebCapability,
)

TOKEN = "brave-secret-token-XYZ"

BRAVE_BODY = {
    "query": {"original": "q"},
    "web": {"results": [
        {"url": "https://a.example/1", "title": "A One", "description": "snippet a",
         "page_age": "2026-01-01", "language": "en"},
        {"url": "https://b.example/2", "title": "B Two", "description": "snippet b"},
        {"title": "no url — skipped"},
    ]},
}


class FakeProvider(SearchProvider):
    provider_id = "fake"

    def __init__(self, *, response=None, exc=None, configured=True):
        self._response = response
        self._exc = exc
        self._configured = configured
        self.seen = []

    @property
    def configured(self):
        return self._configured

    async def search(self, request):
        self.seen.append(request)
        if self._exc:
            raise self._exc
        return self._response


class StubBrave(BraveSearchProvider):
    def __init__(self, token, *, status=200, headers=None, body=None, raise_exc=None):
        super().__init__(token)
        self._status = status
        self._headers = headers or {}
        self._body = body
        self._raise = raise_exc
        self.last_params = None

    async def _http_get(self, params):
        self.last_params = params
        if self._raise:
            raise self._raise
        return self._status, self._headers, self._body


class TestRequestBounds(unittest.TestCase):
    def test_query_too_long(self):
        with self.assertRaises(ValidationError):
            SearchRequest(query="x" * 401)

    def test_count_bounds(self):
        with self.assertRaises(ValidationError):
            SearchRequest(query="q", count=11)
        with self.assertRaises(ValidationError):
            SearchRequest(query="q", count=0)

    def test_search_in_forbids_extra(self):
        with self.assertRaises(ValidationError):
            SearchIn(query="q", api_endpoint="https://evil/")   # caller cannot inject
        with self.assertRaises(ValidationError):
            SearchIn(query="x" * 401)


class TestBraveParsing(IsolatedAsyncioTestCase):
    async def test_parses_candidates(self):
        p = StubBrave(TOKEN, body=BRAVE_BODY, headers={"X-Request-Id": "req-1"})
        resp = await p.search(SearchRequest(query="hello world", count=5))
        self.assertEqual(resp.provider, "brave")
        self.assertEqual(len(resp.candidates), 2)   # third has no url -> skipped
        self.assertEqual(resp.candidates[0].url, "https://a.example/1")
        self.assertEqual(resp.candidates[0].title, "A One")
        self.assertEqual(resp.candidates[0].provider_rank, 0)
        self.assertEqual(resp.candidates[0].published_at, "2026-01-01")
        self.assertEqual(resp.query_digest,
                         hashlib.sha256(b"hello world").hexdigest())
        self.assertEqual(resp.provider_request_id, "req-1")

    async def test_token_never_in_output(self):
        p = StubBrave(TOKEN, body=BRAVE_BODY)
        resp = await p.search(SearchRequest(query="q"))
        blob = resp.model_dump_json()
        self.assertNotIn(TOKEN, blob)
        # token IS sent in the request header params? no — header, not query
        self.assertNotIn("token", p.last_params)

    async def test_429_rate_limited(self):
        p = StubBrave(TOKEN, status=429, headers={"Retry-After": "2"})
        with self.assertRaises(SearchRateLimited) as ctx:
            await p.search(SearchRequest(query="q"))
        self.assertEqual(ctx.exception.retry_after, 2.0)

    async def test_malformed(self):
        with self.assertRaises(SearchProviderBadResponse):
            await StubBrave(TOKEN, status=200, body=None).search(SearchRequest(query="q"))
        with self.assertRaises(SearchProviderBadResponse):
            await StubBrave(TOKEN, status=500, body={}).search(SearchRequest(query="q"))
        with self.assertRaises(SearchProviderBadResponse):
            await StubBrave(TOKEN, body={"web": {"results": "notalist"}}).search(
                SearchRequest(query="q"))

    async def test_empty_results_ok(self):
        resp = await StubBrave(TOKEN, body={"web": {"results": []}}).search(
            SearchRequest(query="q"))
        self.assertEqual(resp.candidates, [])

    async def test_timeout_maps_to_unavailable(self):
        p = StubBrave(TOKEN, raise_exc=asyncio.TimeoutError())
        with self.assertRaises(SearchProviderUnavailable):
            await p.search(SearchRequest(query="q"))

    def test_configured(self):
        self.assertFalse(BraveSearchProvider(None).configured)
        self.assertFalse(BraveSearchProvider("   ").configured)
        self.assertTrue(BraveSearchProvider("t").configured)


class TestCapability(IsolatedAsyncioTestCase):
    async def test_success(self):
        resp = SearchResponse(query_digest="d", provider="fake", candidates=[],
                              duration_ms=1.0)
        cap = SearchPublicWebCapability(provider=FakeProvider(response=resp))
        out = await cap.invoke(SearchIn(query="hello"))
        self.assertEqual(out.provider, "fake")
        self.assertEqual(cap._provider.seen[0].query, "hello")

    async def test_unconfigured_provider_unavailable(self):
        cap = SearchPublicWebCapability(provider=FakeProvider(configured=False))
        with self.assertRaises(SearchProviderUnavailable):
            await cap.invoke(SearchIn(query="q"))

    async def test_no_provider_unavailable(self):
        cap = SearchPublicWebCapability(provider=None)
        # in a clean env there is no token file -> provider unconfigured
        if cap._provider is not None and cap._provider.configured:
            self.skipTest("a live token is configured in this environment")
        with self.assertRaises(SearchProviderUnavailable):
            await cap.invoke(SearchIn(query="q"))

    async def test_background_and_health(self):
        cap = SearchPublicWebCapability(provider=FakeProvider(configured=False))
        self.assertTrue(cap.supports_background)
        h = await cap.health()
        self.assertFalse(h["configured"])


class TestRegistry(unittest.TestCase):
    def test_known_providers(self):
        self.assertIn("brave", KNOWN_PROVIDERS)

    def test_build_default_no_token_unconfigured(self):
        # ensure no token file is picked up in a clean env
        old = {k: os.environ.pop(k, None) for k in ("CREDENTIALS_DIRECTORY",
                                                    "SOLVIO_BRAVE_TOKEN_FILE")}
        try:
            p = build_default_provider()
            self.assertIsNotNone(p)
            self.assertFalse(p.configured)
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main(verbosity=2)
