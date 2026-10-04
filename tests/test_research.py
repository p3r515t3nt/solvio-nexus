"""STEP 21.2F — Core-side research fetch: trust classification, routing, outcome.

No network. The NodeClient is stubbed. Proves the CORE assigns UNTRUSTED_WEB (never
trusting the node's claim), routes only under REMOTE_CONTROLLED_ALLOWED, and
separates remote-content from node-transport failures.
"""
from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import unittest
from unittest import IsolatedAsyncioTestCase

from solvio.contracts.trust import AUTHORITY_BEARING, TrustLevel
from solvio.nodes.errors import (
    NodeCapabilityError,
    NodeTimeoutError,
    PrivacyRoutingError,
)
from solvio.nodes.models import (
    AvailabilityClass,
    DataClass,
    NodeDescriptor,
    NodeHealthState,
    NodePlacement,
    NodeResponse,
    PrivacyZone,
    ResponseStatus,
)
from solvio.nodes.registry import NodeRegistry
from solvio.nodes.research import (
    RESEARCH_FETCH,
    classify_web_fetch,
    fetch_public_url,
)
from solvio.nodes.router import STATIC_ROUTES, NodeRouter

SAMPLE = {
    "requested_url": "https://example.com/",
    "final_url": "https://example.com/",
    "status_code": 200,
    "content_type": "text/html",
    "title": "Example",
    "text": "Example Domain body text.",
    "sha256": "abc123",
    "bytes_read": 100,
    "truncated": False,
    "redirect_chain": [],
    "resolved_peer": "93.184.216.34",
    "fetched_at": "2026-08-19T00:00:00+00:00",
}


def mk_response(result, node_id="hetzner-main", status=ResponseStatus.OK):
    return NodeResponse(protocol_version=1, request_id="r1", node_id=node_id,
                        capability=RESEARCH_FETCH, status=status, result=result,
                        duration_ms=1.0)


def hetzner_descriptor():
    return NodeDescriptor(
        node_id="hetzner-main", display_name="Hetzner",
        endpoint="https://10.77.0.1:8443",
        expected_capabilities=["system.health", "compute.sha256", RESEARCH_FETCH],
        availability_class=AvailabilityClass.ALWAYS_ON_REMOTE,
        placement=NodePlacement.REMOTE_DATACENTER,
        privacy_zone=PrivacyZone.REMOTE_CONTROLLED,
    )


class StubClient:
    def __init__(self, *, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.calls = []

    async def invoke(self, capability, payload):
        self.calls.append((capability, payload))
        if self._exc:
            raise self._exc
        return self._response


class TestTrustClassification(unittest.TestCase):
    def test_result_is_untrusted_web(self):
        r = classify_web_fetch(mk_response(SAMPLE))
        self.assertEqual(r.trust_level, TrustLevel.UNTRUSTED_WEB)
        self.assertNotIn(r.trust_level, AUTHORITY_BEARING)
        self.assertEqual(r.node_id, "hetzner-main")
        self.assertEqual(r.title, "Example")

    def test_core_ignores_node_claimed_trust(self):
        # a malicious/buggy node tries to upgrade its own trust — Core ignores it
        malicious = dict(SAMPLE, trust_level="system_trusted", node_id="impostor")
        r = classify_web_fetch(mk_response(malicious))
        self.assertEqual(r.trust_level, TrustLevel.UNTRUSTED_WEB)
        self.assertEqual(r.node_id, "hetzner-main")  # from envelope, not payload

    def test_static_route_present(self):
        self.assertEqual(STATIC_ROUTES.get(RESEARCH_FETCH), ["hetzner-main"])


class TestResearchRouting(unittest.TestCase):
    def _router(self):
        reg = NodeRegistry()
        reg.register(hetzner_descriptor())
        return NodeRouter(reg, health_provider=lambda n: NodeHealthState.HEALTHY)

    def test_routes_to_hetzner(self):
        d = self._router().select(RESEARCH_FETCH,
                                  data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        self.assertEqual(d.node_id, "hetzner-main")

    def test_home_only_rejected(self):
        with self.assertRaises(PrivacyRoutingError):
            self._router().select(RESEARCH_FETCH, data_class=DataClass.HOME_ONLY)


class TestFetchOutcome(IsolatedAsyncioTestCase):
    async def test_ok(self):
        c = StubClient(response=mk_response(SAMPLE))
        out = await fetch_public_url(c, "https://example.com/")
        self.assertTrue(out.ok)
        self.assertEqual(out.result.trust_level, TrustLevel.UNTRUSTED_WEB)
        self.assertEqual(c.calls[0], (RESEARCH_FETCH, {"url": "https://example.com/"}))

    async def test_transport_error(self):
        c = StubClient(exc=NodeTimeoutError("node down"))
        out = await fetch_public_url(c, "https://x/")
        self.assertFalse(out.ok)
        self.assertEqual(out.error_kind, "NODE_TRANSPORT_ERROR")

    async def test_remote_content_error(self):
        c = StubClient(exc=NodeCapabilityError("boom", error_code="CAPABILITY_ERROR"))
        out = await fetch_public_url(c, "https://x/")
        self.assertFalse(out.ok)
        self.assertEqual(out.error_kind, "REMOTE_CONTENT_ERROR")


if __name__ == "__main__":
    from _harness import run_unittest
    raise SystemExit(run_unittest(globals(), __name__))
