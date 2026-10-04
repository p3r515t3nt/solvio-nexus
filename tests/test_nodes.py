"""STEP 21.2E - Core Node-Layer Tests (Client/Registry/Router/Resilience/Trust).

Nur Standardbibliothek + der Node-Layer. KEIN Test braucht Hetzner: der Client
wird ueber die Naht `_raw_request` mit einem Fake bespielt. Der einzige echte
Netz-Test ist TestLiveNode und laeuft nur mit SOLVIO_LIVE_NODE=1.

Ausfuehren:  .venv/bin/python -m unittest tests.test_nodes -v
"""
from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import IsolatedAsyncioTestCase

from solvio.contracts.trust import AUTHORITY_BEARING, TrustLevel
from solvio.nodes.client import NodeClient, RawResponse
from solvio.nodes.config import (
    NodeConnectionConfig,
    NodesConfig,
    TLSClientPaths,
    load_nodes_config,
    parse_nodes_config,
)
from solvio.nodes.errors import (
    NodeBusyError,
    NodeCapabilityError,
    NodeConfigError,
    NodeIdentityError,
    NodeProtocolError,
    NodeRequestMismatchError,
    NodeTimeoutError,
    NodeUnavailableError,
    NoSuitableNodeError,
    PrivacyRoutingError,
    UnknownCapabilityError,
)
from solvio.nodes.health import NodeHealthMonitor, classify_error, classify_report
from solvio.nodes.models import (
    AvailabilityClass,
    DataClass,
    HealthReport,
    NodeDescriptor,
    NodeHealthState,
    NodePlacement,
    PrivacyZone,
    zone_permits,
)
from solvio.nodes.registry import NodeRegistry, build_registry
from solvio.nodes.resilience import Backoff, CircuitBreaker, CircuitState
from solvio.nodes.router import NodeRouter
from solvio.nodes.trust_map import node_result_trust, result_bears_authority


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def mk_conn(node_id="hetzner-main", *, identity=None, endpoint="https://10.77.0.1:8443",
            protocol=1, certs=("/no/ca.crt", "/no/client.crt", "/no/client.key")):
    return NodeConnectionConfig(
        node_id=node_id, endpoint=endpoint,
        tls=TLSClientPaths(*certs), identity=identity or node_id,
        protocol_version=protocol,
    )


def mk_descriptor(node_id, *, caps, zone=PrivacyZone.REMOTE_CONTROLLED,
                  placement=NodePlacement.REMOTE_DATACENTER,
                  avail=AvailabilityClass.ALWAYS_ON_REMOTE, priority=100,
                  enabled=True, protocol=1, identity=None):
    return NodeDescriptor(
        node_id=node_id, display_name=node_id, endpoint="https://10.77.0.1:8443",
        expected_capabilities=caps, availability_class=avail, placement=placement,
        privacy_zone=zone, priority=priority, enabled=enabled,
        protocol_version=protocol, expected_identity=identity,
    )


class FakeClient(NodeClient):
    """NodeClient, dessen einzige Netz-Naht durch einen Handler ersetzt ist."""

    def __init__(self, config, handler):
        super().__init__(config)
        self._handler = handler

    async def _raw_request(self, method, path, *, json_body=None):
        res = self._handler(method, path, json_body)
        if isinstance(res, Exception):
            raise res
        return res


def health_ok(node_id="hetzner-main", protocol=1, status="ok"):
    def h(method, path, json_body):
        return RawResponse(200, {
            "node_id": node_id, "protocol_version": protocol, "status": status,
            "version": "0.1.0", "capabilities": ["system.health", "compute.sha256"],
            "system": {"status": "ok", "uptime_s": 1.0, "load1": 0.1, "ram_available_mb": 5000},
        })
    return h


def invoke_ok(node_id="hetzner-main", protocol=1, result=None, echo=True, request_id=None):
    def h(method, path, json_body):
        rid = request_id if request_id is not None else ((json_body or {}).get("request_id") if echo else "server-generated-xyz")
        return RawResponse(200, {
            "protocol_version": protocol, "request_id": rid, "node_id": node_id,
            "capability": "compute.sha256", "status": "ok",
            "result": result or {"algorithm": "sha256", "hex": "abc", "input_length": 1},
            "duration_ms": 0.1,
        })
    return h


def invoke_error(error_code, node_id="hetzner-main"):
    def h(method, path, json_body):
        return RawResponse(200, {
            "protocol_version": 1, "request_id": (json_body or {}).get("request_id"),
            "node_id": node_id, "capability": "compute.sha256", "status": "error",
            "duration_ms": 0.0, "error_code": error_code, "error_message": "node capability error",
        })
    return h


# --------------------------------------------------------------------------
# Models / Privacy
# --------------------------------------------------------------------------
class TestModelsPrivacy(unittest.TestCase):
    def test_identity_is_not_ip(self):
        d = mk_descriptor("hetzner-main", caps=["system.health"])
        self.assertEqual(d.identity, "hetzner-main")  # default = node_id
        d2 = mk_descriptor("n2", caps=[], identity="cert-cn-2")
        self.assertEqual(d2.identity, "cert-cn-2")
        self.assertNotIn("10.77", d.identity)  # IP ist nicht Identitaet

    def test_zone_permits_ordering(self):
        # HOME_ONLY darf nur HOME
        self.assertTrue(zone_permits(DataClass.HOME_ONLY, PrivacyZone.HOME))
        self.assertFalse(zone_permits(DataClass.HOME_ONLY, PrivacyZone.LAN))
        self.assertFalse(zone_permits(DataClass.HOME_ONLY, PrivacyZone.REMOTE_CONTROLLED))
        # REMOTE_CONTROLLED_ALLOWED darf home/lan/remote, aber nicht cloud
        self.assertTrue(zone_permits(DataClass.REMOTE_CONTROLLED_ALLOWED, PrivacyZone.REMOTE_CONTROLLED))
        self.assertTrue(zone_permits(DataClass.REMOTE_CONTROLLED_ALLOWED, PrivacyZone.LAN))
        self.assertFalse(zone_permits(DataClass.REMOTE_CONTROLLED_ALLOWED, PrivacyZone.THIRD_PARTY_CLOUD))


# --------------------------------------------------------------------------
# Trust mapping + Authority (PHASE 12 / 22)
# --------------------------------------------------------------------------
class TestTrustMap(unittest.TestCase):
    def test_foundation_caps_are_external_tool_result(self):
        self.assertEqual(node_result_trust("system.health"), TrustLevel.EXTERNAL_TOOL_RESULT)
        self.assertEqual(node_result_trust("compute.sha256"), TrustLevel.EXTERNAL_TOOL_RESULT)

    def test_future_families(self):
        self.assertEqual(node_result_trust("research.web_search"), TrustLevel.UNTRUSTED_WEB)
        self.assertEqual(node_result_trust("agent.claude-code"), TrustLevel.AGENT_GENERATED)

    def test_never_authority_bearing(self):
        for cap in ["system.health", "compute.sha256", "research.x", "agent.y",
                    "embedding.qwen3-0.6b", "totally.unknown", ""]:
            self.assertNotIn(node_result_trust(cap), AUTHORITY_BEARING)
            self.assertFalse(result_bears_authority(cap))

    def test_unknown_defaults_conservative(self):
        self.assertEqual(node_result_trust("totally.unknown"), TrustLevel.AGENT_GENERATED)


class TestAuthorityStructure(unittest.TestCase):
    def test_client_has_no_shell_like_methods(self):
        forbidden = ["shell", "exec", "run", "run_command", "bash", "cmd",
                     "powershell", "python_exec", "eval", "system"]
        for name in forbidden:
            self.assertFalse(hasattr(NodeClient, name), f"NodeClient must not expose {name}")

    def test_client_public_api_is_allowlisted(self):
        public = {n for n in dir(NodeClient) if not n.startswith("_")}
        allow = {"health", "list_capabilities", "get_capability", "invoke", "aclose", "submit_job", "get_job", "get_job_result", "cancel_job"}
        self.assertEqual(public, allow)

    def test_node_layer_does_not_import_dispatcher(self):
        # PHASE 22: keine direkte ToolDispatcher-Verbindung.
        import solvio.nodes as pkg
        import sys
        loaded = [m for m in sys.modules if m.startswith("solvio.nodes")]
        self.assertTrue(loaded)
        self.assertNotIn("solvio.tools.dispatcher", sys.modules,
                         "node layer must not pull in the tool dispatcher")


# --------------------------------------------------------------------------
# Circuit Breaker + Backoff (PHASE 14/15)
# --------------------------------------------------------------------------
class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class TestCircuitBreaker(unittest.TestCase):
    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, clock=FakeClock(0.0))
        self.assertTrue(cb.allow())
        for _ in range(3):
            cb.record_failure()
        self.assertIs(cb.state, CircuitState.OPEN)
        self.assertFalse(cb.allow())

    def test_half_open_then_recovery(self):
        clk = FakeClock(0.0)
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout_s=30.0, clock=clk)
        cb.record_failure(); cb.record_failure()
        self.assertIs(cb.state, CircuitState.OPEN)
        clk.t = 31.0  # Timeout abgelaufen
        self.assertIs(cb.state, CircuitState.HALF_OPEN)
        self.assertTrue(cb.allow())      # eine Probe erlaubt
        self.assertFalse(cb.allow())     # zweite nicht
        cb.record_success()
        self.assertIs(cb.state, CircuitState.CLOSED)

    def test_half_open_failure_reopens(self):
        clk = FakeClock(0.0)
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=10.0, clock=clk)
        cb.record_failure()
        self.assertIs(cb.state, CircuitState.OPEN)
        clk.t = 11.0
        self.assertIs(cb.state, CircuitState.HALF_OPEN)
        cb.record_failure()
        self.assertIs(cb.state, CircuitState.OPEN)


class TestBackoff(unittest.TestCase):
    def test_bounded_and_monotone_upper(self):
        import random
        bo = Backoff(base_s=0.5, factor=2.0, max_s=8.0, jitter=0.0, rng=random.Random(1))
        self.assertAlmostEqual(bo.delay(1), 0.5)
        self.assertAlmostEqual(bo.delay(2), 1.0)
        self.assertAlmostEqual(bo.delay(3), 2.0)
        self.assertLessEqual(bo.delay(99), 8.0)  # gedeckelt

    def test_jitter_within_bounds(self):
        import random
        bo = Backoff(base_s=1.0, factor=2.0, max_s=100.0, jitter=0.5, rng=random.Random(7))
        for attempt in range(1, 6):
            raw = min(100.0, 1.0 * 2 ** (attempt - 1))
            d = bo.delay(attempt)
            self.assertGreaterEqual(d, raw * 0.5 - 1e-9)
            self.assertLessEqual(d, raw + 1e-9)


# --------------------------------------------------------------------------
# Config / Registry (PHASE 8 / 17 / 23)
# --------------------------------------------------------------------------
SAMPLE_CFG = {
    "nodes": [{
        "node_id": "hetzner-main",
        "display_name": "Hetzner Main",
        "endpoint": "https://10.77.0.1:8443",
        "expected_capabilities": ["system.health", "compute.sha256"],
        "availability_class": "always_on_remote",
        "placement": "remote_datacenter",
        "privacy_zone": "remote_controlled",
        "priority": 100,
        "enabled": True,
        "expected_identity": "hetzner-main",
        "tls": {"ca_cert": "/p/ca.crt", "client_cert": "/p/client.crt", "client_key": "/p/client.key"},
    }]
}


class TestConfigRegistry(unittest.TestCase):
    def test_parse_config(self):
        cfg = parse_nodes_config(SAMPLE_CFG)
        self.assertEqual(len(cfg.descriptors), 1)
        d = cfg.descriptors[0]
        self.assertEqual(d.node_id, "hetzner-main")
        self.assertEqual(d.privacy_zone, PrivacyZone.REMOTE_CONTROLLED)
        conn = cfg.connections["hetzner-main"]
        self.assertEqual(conn.identity, "hetzner-main")
        self.assertEqual(conn.tls.client_key, "/p/client.key")

    def test_registry_duplicate_rejected(self):
        reg = NodeRegistry()
        reg.register(mk_descriptor("n", caps=["system.health"]))
        with self.assertRaises(ValueError):
            reg.register(mk_descriptor("n", caps=[]))

    def test_registry_enabled_and_unknown(self):
        reg = NodeRegistry()
        reg.register(mk_descriptor("a", caps=["x"]))
        reg.register(mk_descriptor("b", caps=["x"], enabled=False))
        self.assertEqual([d.node_id for d in reg.enabled()], ["a"])
        with self.assertRaises(NodeConfigError):
            reg.get("does-not-exist")

    def test_no_auto_discovery(self):
        # Eine frische Registry ohne Konfig kennt NICHTS (kein LAN-Scan).
        reg = NodeRegistry()
        self.assertEqual(reg.all(), [])

    def test_empty_config_yields_empty_registry(self):
        # PHASE 23: fehlt die Datei, ist die Registry leer (kein Crash).
        cfg = load_nodes_config(explicit="/definitely/not/here.json")
        self.assertTrue(cfg.is_empty)
        reg = build_registry(cfg)
        self.assertEqual(reg.all(), [])


# --------------------------------------------------------------------------
# Router (PHASE 9-11 / 21)
# --------------------------------------------------------------------------
class TestRouter(unittest.TestCase):
    def _registry_two(self):
        reg = NodeRegistry()
        reg.register(mk_descriptor("hetzner-main", caps=["system.health", "compute.sha256"], priority=100))
        reg.register(mk_descriptor("hetzner-2", caps=["compute.sha256"], priority=200))
        return reg

    def test_healthy_primary_selected(self):
        reg = self._registry_two()
        router = NodeRouter(reg, health_provider=lambda n: NodeHealthState.HEALTHY)
        chosen = router.select("compute.sha256", data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        self.assertEqual(chosen.node_id, "hetzner-main")  # static route + lower priority

    def test_offline_primary_skipped(self):
        reg = self._registry_two()
        state = {"hetzner-main": NodeHealthState.OFFLINE, "hetzner-2": NodeHealthState.HEALTHY}
        router = NodeRouter(reg, health_provider=lambda n: state[n])
        chosen = router.select("compute.sha256", data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        self.assertEqual(chosen.node_id, "hetzner-2")

    def test_privacy_violation_rejected(self):
        reg = self._registry_two()
        router = NodeRouter(reg, health_provider=lambda n: NodeHealthState.HEALTHY)
        # HOME_ONLY-Daten duerfen NICHT auf einen remote-controlled Knoten.
        with self.assertRaises(PrivacyRoutingError):
            router.select("compute.sha256", data_class=DataClass.HOME_ONLY)

    def test_no_suitable_when_all_offline(self):
        reg = self._registry_two()
        router = NodeRouter(reg, health_provider=lambda n: NodeHealthState.OFFLINE)
        with self.assertRaises(NoSuitableNodeError):
            router.select("compute.sha256", data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)

    def test_capability_absent(self):
        reg = self._registry_two()
        router = NodeRouter(reg, health_provider=lambda n: NodeHealthState.HEALTHY)
        with self.assertRaises(NoSuitableNodeError):
            router.select("research.web", data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)

    def test_protocol_incompatible(self):
        reg = NodeRegistry()
        reg.register(mk_descriptor("future", caps=["compute.sha256"], protocol=2))
        router = NodeRouter(reg, health_provider=lambda n: NodeHealthState.HEALTHY)
        with self.assertRaises(NoSuitableNodeError):
            router.select("compute.sha256", data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)

    def test_circuit_open_skips_node(self):
        reg = self._registry_two()
        # hetzner-main Circuit offen -> auf hetzner-2 ausweichen
        router = NodeRouter(
            reg,
            health_provider=lambda n: NodeHealthState.HEALTHY,
            circuit_provider=lambda n: n != "hetzner-main",
        )
        chosen = router.select("compute.sha256", data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        self.assertEqual(chosen.node_id, "hetzner-2")

    def test_unknown_state_is_eligible(self):
        reg = NodeRegistry()
        reg.register(mk_descriptor("hetzner-main", caps=["system.health"]))
        router = NodeRouter(reg, health_provider=lambda n: NodeHealthState.UNKNOWN)
        chosen = router.select("system.health", data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        self.assertEqual(chosen.node_id, "hetzner-main")


# --------------------------------------------------------------------------
# Health monitor (PHASE 13)
# --------------------------------------------------------------------------
class TestHealth(IsolatedAsyncioTestCase):
    async def test_healthy_and_cache(self):
        clk = FakeClock(100.0)
        client = FakeClient(mk_conn(), health_ok())
        mon = NodeHealthMonitor("hetzner-main", client, ttl_s=15.0, clock=clk)
        self.assertEqual(mon.cached_state(), NodeHealthState.UNKNOWN)
        st = await mon.state()
        self.assertEqual(st, NodeHealthState.HEALTHY)
        self.assertTrue(mon.is_fresh())
        self.assertTrue(mon.available())

    async def test_incompatible_protocol(self):
        client = FakeClient(mk_conn(protocol=1), health_ok(protocol=2))
        mon = NodeHealthMonitor("hetzner-main", client, expected_protocol=1)
        st = await mon.refresh()
        self.assertEqual(st.state, NodeHealthState.INCOMPATIBLE)
        self.assertFalse(mon.available())

    async def test_offline_on_unavailable(self):
        def boom(*a, **k):
            return NodeUnavailableError("down")
        client = FakeClient(mk_conn(), boom)
        mon = NodeHealthMonitor("hetzner-main", client)
        st = await mon.refresh()
        self.assertEqual(st.state, NodeHealthState.OFFLINE)
        self.assertFalse(mon.available())


# --------------------------------------------------------------------------
# Client (PHASE 6 / 20)
# --------------------------------------------------------------------------
class TestClient(IsolatedAsyncioTestCase):
    async def test_health_ok(self):
        c = FakeClient(mk_conn(), health_ok())
        rep = await c.health()
        self.assertIsInstance(rep, HealthReport)
        self.assertEqual(rep.node_id, "hetzner-main")

    async def test_identity_mismatch(self):
        c = FakeClient(mk_conn(node_id="hetzner-main"), health_ok(node_id="impostor"))
        with self.assertRaises(NodeIdentityError):
            await c.health()

    async def test_protocol_mismatch(self):
        c = FakeClient(mk_conn(protocol=1), health_ok(protocol=2))
        with self.assertRaises(NodeProtocolError):
            await c.health()

    async def test_list_capabilities(self):
        def caps(method, path, json_body):
            return RawResponse(200, {"node_id": "hetzner-main", "protocol_version": 1,
                                     "capabilities": [{"id": "compute.sha256"}, {"id": "system.health"}]})
        c = FakeClient(mk_conn(), caps)
        result = await c.list_capabilities()
        self.assertEqual({d.id for d in result}, {"compute.sha256", "system.health"})

    async def test_get_capability_unknown_404(self):
        c = FakeClient(mk_conn(), lambda m, p, j: RawResponse(404, {"error": "x"}))
        with self.assertRaises(UnknownCapabilityError):
            await c.get_capability("nope")

    async def test_invoke_ok_and_request_id_echo(self):
        c = FakeClient(mk_conn(), invoke_ok(result={"hex": "deadbeef", "input_length": 3}))
        resp = await c.invoke("compute.sha256", {"text": "abc"})
        self.assertEqual(resp.result["hex"], "deadbeef")

    async def test_invoke_request_id_mismatch(self):
        c = FakeClient(mk_conn(), invoke_ok(echo=False))
        with self.assertRaises(NodeRequestMismatchError):
            await c.invoke("compute.sha256", {"text": "abc"})

    async def test_invoke_unknown_capability(self):
        c = FakeClient(mk_conn(), invoke_error("UNKNOWN_CAPABILITY"))
        with self.assertRaises(UnknownCapabilityError):
            await c.invoke("system.exec", {})

    async def test_invoke_resource_busy(self):
        c = FakeClient(mk_conn(), invoke_error("RESOURCE_BUSY"))
        with self.assertRaises(NodeBusyError):
            await c.invoke("compute.sha256", {"text": "abc"})

    async def test_invoke_malformed_response(self):
        c = FakeClient(mk_conn(), lambda m, p, j: RawResponse(200, None))
        with self.assertRaises(NodeProtocolError):
            await c.invoke("compute.sha256", {"text": "abc"})

    async def test_invoke_payload_too_large(self):
        c = FakeClient(mk_conn(), lambda m, p, j: RawResponse(413, None))
        with self.assertRaises(NodeCapabilityError) as ctx:
            await c.invoke("compute.sha256", {"text": "x"})
        self.assertEqual(ctx.exception.error_code, "PAYLOAD_TOO_LARGE")

    async def test_timeout_propagates(self):
        c = FakeClient(mk_conn(), lambda m, p, j: NodeTimeoutError("slow"))
        with self.assertRaises(NodeTimeoutError):
            await c.health()

    async def test_offline_propagates(self):
        c = FakeClient(mk_conn(), lambda m, p, j: NodeUnavailableError("down"))
        with self.assertRaises(NodeUnavailableError):
            await c.health()


# --------------------------------------------------------------------------
# mTLS context + optional startup (PHASE 17 / 23 / 27)
# --------------------------------------------------------------------------
_HAS_OPENSSL = shutil.which("openssl") is not None


class TestMTLSContext(unittest.TestCase):
    def test_missing_certs_raise_only_on_build(self):
        # Konstruktion mit fehlenden Certs crasht NICHT (lazy, PHASE 23) ...
        c = NodeClient(mk_conn(certs=("/no/ca", "/no/crt", "/no/key")))
        # ... erst der SSL-Aufbau meldet die Fehlkonfiguration.
        with self.assertRaises(NodeConfigError):
            c._build_ssl()

    @unittest.skipUnless(_HAS_OPENSSL, "openssl not available")
    def test_build_ssl_enforces_mtls(self):
        d = tempfile.mkdtemp()
        try:
            cert = os.path.join(d, "c.pem")
            key = os.path.join(d, "k.pem")
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "ec",
                 "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                 "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=test"],
                check=True, capture_output=True,
            )
            import ssl
            c = NodeClient(mk_conn(certs=(cert, cert, key)))
            ctx = c._build_ssl()
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(ctx.check_hostname)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestOptionalStartup(unittest.TestCase):
    def test_import_has_no_side_effects(self):
        # Import darf nichts oeffnen; Router auf leerer Registry wirft sauber.
        reg = build_registry(NodesConfig())
        router = NodeRouter(reg)
        with self.assertRaises(NoSuitableNodeError):
            router.select("system.health", data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)


# --------------------------------------------------------------------------
# Logging redaction (PHASE 28 / 30)
# --------------------------------------------------------------------------
class TestLoggingRedaction(IsolatedAsyncioTestCase):
    async def test_no_payload_in_logs(self):
        import solvio.nodes.client as client_mod

        recorded = []

        class Rec:
            def debug(self, event, **kw): recorded.append((event, kw))
            def info(self, event, **kw): recorded.append((event, kw))
            def warning(self, event, **kw): recorded.append((event, kw))

        orig = client_mod._log
        client_mod._log = Rec()
        try:
            secret = "TOP-SECRET-PAYLOAD-9931"
            c = FakeClient(mk_conn(), invoke_ok(result={"hex": "s3cr3t-hash", "input_length": 1}))
            await c.invoke("compute.sha256", {"text": secret})
        finally:
            client_mod._log = orig
        blob = repr(recorded)
        self.assertNotIn(secret, blob)
        self.assertNotIn("s3cr3t-hash", blob)
        # Nur Metadaten-Schluessel erlaubt.
        for _event, kw in recorded:
            self.assertTrue(set(kw).issubset({"node_id", "capability", "status", "duration_ms"}))


# --------------------------------------------------------------------------
# Live node (nur mit SOLVIO_LIVE_NODE=1) - PHASE 18
# --------------------------------------------------------------------------
@unittest.skipUnless(os.environ.get("SOLVIO_LIVE_NODE") == "1",
                     "live node test disabled (set SOLVIO_LIVE_NODE=1)")
class TestLiveNode(IsolatedAsyncioTestCase):
    async def test_health_caps_invoke(self):
        import hashlib
        cfg = load_nodes_config()
        self.assertFalse(cfg.is_empty, "config/nodes.json required for live test")
        conn = cfg.connections["hetzner-main"]
        c = NodeClient(conn)
        try:
            rep = await c.health()
            self.assertEqual(rep.node_id, "hetzner-main")
            caps = await c.list_capabilities()
            self.assertIn("compute.sha256", {d.id for d in caps})
            text = "synthetic-live-benchmark-string"
            resp = await c.invoke("compute.sha256", {"text": text})
            self.assertEqual(resp.result["hex"], hashlib.sha256(text.encode()).hexdigest())
        finally:
            await c.aclose()


class TestResearchRoutingConfig(unittest.TestCase):
    """STEP 21.2H.1: the tracked example config plus the normal loader/registry/router
    path route research.search_public_web to hetzner-main - no in-memory augmentation."""

    def _example_cfg(self):
        from solvio.config import PROJECT_ROOT
        return load_nodes_config(explicit=PROJECT_ROOT / "config" / "nodes.example.json")

    def test_example_config_lists_search_capability(self):
        cfg = self._example_cfg()
        hz = next(d for d in cfg.descriptors if d.node_id == "hetzner-main")
        self.assertIn("research.search_public_web", hz.expected_capabilities)
        for cap in ("system.health", "compute.sha256", "research.fetch_public_url"):
            self.assertIn(cap, hz.expected_capabilities)

    def test_normal_router_path_routes_search_to_hetzner(self):
        cfg = self._example_cfg()
        reg = NodeRegistry.from_config(cfg)
        router = NodeRouter(reg, health_provider=lambda n: NodeHealthState.HEALTHY)
        chosen = router.select("research.search_public_web",
                               data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        self.assertEqual(chosen.node_id, "hetzner-main")


if __name__ == "__main__":
    from _harness import run_unittest
    raise SystemExit(run_unittest(globals(), __name__))
