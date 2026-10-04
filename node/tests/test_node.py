"""SOLVIO node test suite (stdlib unittest + aiohttp).

Covers protocol, identity, config, registry, limits, capabilities, runtime,
HTTP integration, no-shell structure, and log redaction.
Run:  python -m unittest discover -s tests -p 'test_*.py'
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest

from aiohttp.test_utils import AioHTTPTestCase
from pydantic import BaseModel, ValidationError

from solvio_node.capabilities.base import (Capability, ExecutionMode, PrivacyClass,
                                           RiskLevel)
from solvio_node.capabilities.compute.sha256 import Sha256Capability, Sha256In
from solvio_node.capabilities.system.health import HealthCapability
from solvio_node.config import Limits, NodeConfig, TLSPaths
from solvio_node.identity import NodeIdentity
from solvio_node.limits import BusyError, LimitGuard, RequestTooLarge
from solvio_node.logging import NodeLogger
from solvio_node.protocol import (ErrorCode, NodeRequest, NodeResponse,
                                  PROTOCOL_VERSION, Status)
from solvio_node.registry import CapabilityRegistry, build_registry
from solvio_node.runtime import Runtime
from solvio_node.server import build_app

_TMP = tempfile.mkdtemp(prefix="solvio-node-test-")


def _log(name="node.jsonl"):
    return NodeLogger(os.path.join(_TMP, name), "test-node", to_stderr=False)


def _runtime(reg=None):
    reg = reg or build_registry(["system.health", "compute.sha256"])
    return Runtime(NodeIdentity("test-node"), reg, LimitGuard(Limits()), _log())


# ------------------------------------------------------------------ protocol
class TestProtocol(unittest.TestCase):
    def test_version(self):
        self.assertEqual(PROTOCOL_VERSION, 1)

    def test_request_defaults_and_forbid_extra(self):
        r = NodeRequest(capability="compute.sha256")
        self.assertEqual(r.protocol_version, 1)
        self.assertTrue(r.request_id)
        with self.assertRaises(ValidationError):
            NodeRequest(capability="x", bogus=1)

    def test_response_helpers(self):
        ok = NodeResponse.ok(request_id="r", node_id="n", capability="c", result={"a": 1})
        self.assertEqual(ok.status, Status.OK)
        err = NodeResponse.error(request_id="r", node_id="n", capability="c",
                                 code=ErrorCode.TIMEOUT, message="x")
        self.assertEqual(err.error_code, ErrorCode.TIMEOUT)


# ------------------------------------------------------------------ identity
class TestIdentity(unittest.TestCase):
    def test_valid(self):
        i = NodeIdentity("hetzner-main")
        self.assertEqual(i.node_id, "hetzner-main")
        self.assertTrue(i.node_instance_id)

    def test_invalid(self):
        for bad in ["Hetzner", "a", "x_y", "1.2.3.4", "with space"]:
            with self.assertRaises(ValueError):
                NodeIdentity(bad)


# ------------------------------------------------------------------ registry
class TestRegistry(unittest.TestCase):
    def test_register_and_duplicate(self):
        reg = CapabilityRegistry()
        reg.register(HealthCapability())
        with self.assertRaises(ValueError):
            reg.register(HealthCapability())

    def test_unknown_returns_none(self):
        self.assertIsNone(CapabilityRegistry().get("nope"))

    def test_build_rejects_unknown_config(self):
        with self.assertRaises(ValueError):
            build_registry(["system.health", "danger.shell"])

    def test_build_ok(self):
        reg = build_registry(["system.health", "compute.sha256"])
        self.assertEqual(reg.ids(), ["compute.sha256", "system.health"])


# ------------------------------------------------------------------ limits
class TestLimits(unittest.IsolatedAsyncioTestCase):
    def test_size(self):
        g = LimitGuard(Limits(max_request_bytes=10))
        g.check_size(10)
        with self.assertRaises(RequestTooLarge):
            g.check_size(11)

    async def test_queue_full_busy(self):
        g = LimitGuard(Limits(max_concurrency=1, max_queue=0))
        async with g.slot():                      # occupy the single slot
            with self.assertRaises(BusyError):    # queue==0 -> immediate busy
                async with g.slot():
                    pass

    async def test_rate_limit(self):
        g = LimitGuard(Limits(rate_limit_per_min=2, max_concurrency=4, max_queue=8))
        async with g.slot():
            pass
        async with g.slot():
            pass
        with self.assertRaises(BusyError):
            async with g.slot():
                pass


# ------------------------------------------------------------------ capabilities
class TestCapabilities(unittest.IsolatedAsyncioTestCase):
    async def test_sha256(self):
        cap = Sha256Capability()
        out = await cap.invoke(Sha256In(text="hallo"))
        self.assertEqual(out.hex, hashlib.sha256(b"hallo").hexdigest())
        self.assertEqual(out.input_length, 5)

    async def test_health(self):
        out = await HealthCapability().invoke(HealthCapability.input_model())
        self.assertEqual(out.status, "ok")
        self.assertGreaterEqual(out.uptime_s, 0.0)

    def test_descriptors_declare_contract(self):
        d = Sha256Capability.descriptor()
        for k in ("id", "version", "risk_level", "privacy_class", "execution_mode",
                  "supports_batch", "max_concurrency", "timeout_s", "persistent_data",
                  "input_schema", "output_schema"):
            self.assertIn(k, d)
        self.assertFalse(d["persistent_data"])

    def test_sha256_input_length_capped(self):
        with self.assertRaises(ValidationError):
            Sha256In(text="x" * 20000)


# ------------------------------------------------------------------ runtime
class _SlowCap(Capability):
    id = "compute.slow"; version = "1"; description = "sleeps"
    risk_level = RiskLevel.SAFE; privacy_class = PrivacyClass.SYNTHETIC
    execution_mode = ExecutionMode.INLINE; timeout_s = 0.05
    input_model = Sha256In; output_model = Sha256In

    async def invoke(self, data):
        await asyncio.sleep(0.5)
        return data


class TestRuntime(unittest.IsolatedAsyncioTestCase):
    async def test_ok(self):
        rt = _runtime()
        resp = await rt.execute(NodeRequest(capability="compute.sha256",
                                            payload={"text": "abc"}), payload_size=10)
        self.assertEqual(resp.status, Status.OK)
        self.assertEqual(resp.result["hex"], hashlib.sha256(b"abc").hexdigest())

    async def test_unknown_capability(self):
        resp = await _runtime().execute(NodeRequest(capability="nope"), payload_size=2)
        self.assertEqual(resp.error_code, ErrorCode.UNKNOWN_CAPABILITY)

    async def test_validation_error(self):
        resp = await _runtime().execute(NodeRequest(capability="compute.sha256",
                                        payload={"wrong": 1}), payload_size=10)
        self.assertEqual(resp.error_code, ErrorCode.VALIDATION_ERROR)

    async def test_protocol_mismatch(self):
        req = NodeRequest(capability="system.health")
        req.protocol_version = 999
        resp = await _runtime().execute(req, payload_size=2)
        self.assertEqual(resp.error_code, ErrorCode.PROTOCOL_MISMATCH)

    async def test_node_target_mismatch(self):
        resp = await _runtime().execute(NodeRequest(capability="system.health",
                                        target_node_id="other"), payload_size=2)
        self.assertEqual(resp.error_code, ErrorCode.VALIDATION_ERROR)

    async def test_timeout(self):
        reg = CapabilityRegistry()
        reg.register(_SlowCap())
        resp = await _runtime(reg).execute(NodeRequest(capability="compute.slow",
                                           payload={"text": "x"}), payload_size=2)
        self.assertEqual(resp.error_code, ErrorCode.TIMEOUT)

    async def test_error_message_is_payload_free(self):
        # A capability error returns only the exception TYPE, never input content.
        resp = await _runtime().execute(NodeRequest(capability="compute.sha256",
                                        payload={"text": "SUPERSECRET"}), payload_size=2)
        # (valid input -> ok); ensure result carries hash, not echo of secret in errors
        self.assertNotIn("SUPERSECRET", json.dumps(resp.model_dump(mode="json")))


# ------------------------------------------------------------------ logging redaction
class TestLogRedaction(unittest.TestCase):
    def test_event_rejects_content_keys(self):
        lg = _log("redact.jsonl")
        lg.event("startup", detail="ok", count=2)          # allowed
        with self.assertRaises(ValueError):
            lg.event("bad", payload="SECRET")               # disallowed key

    def test_request_logs_sizes_not_content(self):
        path = os.path.join(_TMP, "reqlog.jsonl")
        lg = NodeLogger(path, "n", to_stderr=False)
        lg.request(request_id="r", capability="compute.sha256", status="ok",
                   duration_ms=1.2, payload_size=123, result_size=45)
        text = open(path, encoding="utf-8").read()
        self.assertIn("payload_size", text)
        self.assertNotIn("text", text.replace("compute", ""))  # no field content


# ------------------------------------------------------------------ no-shell structure
class TestNoShell(unittest.TestCase):
    def test_no_shell_capability(self):
        reg = build_registry(["system.health", "compute.sha256"])
        for cid in reg.ids():
            self.assertFalse(any(w in cid for w in
                             ("shell", "exec", "eval", "run", "cmd", "bash", "powershell")))

    def test_routes_are_fixed(self):
        cfg = NodeConfig(tls=TLSPaths(ca_cert="x", server_cert="y", server_key="z"),
                         log_path=os.path.join(_TMP, "routes.jsonl"))
        app = build_app(cfg)
        paths = {r.resource.canonical for r in app.router.routes()}
        self.assertEqual(paths, {"/v1/health", "/v1/capabilities",
                                 "/v1/capabilities/{id}", "/v1/capabilities/{id}/invoke"})


# ------------------------------------------------------------------ HTTP integration
class TestHTTP(AioHTTPTestCase):
    async def get_application(self):
        cfg = NodeConfig(tls=TLSPaths(ca_cert="x", server_cert="y", server_key="z"),
                         log_path=os.path.join(_TMP, "http.jsonl"))
        return build_app(cfg)

    async def test_health(self):
        r = await self.client.get("/v1/health")
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["node_id"], "hetzner-main")
        self.assertIn("compute.sha256", body["capabilities"])

    async def test_capabilities(self):
        r = await self.client.get("/v1/capabilities")
        data = await r.json()
        self.assertEqual({c["id"] for c in data["capabilities"]},
                         {"system.health", "compute.sha256"})

    async def test_capability_one_and_404(self):
        r = await self.client.get("/v1/capabilities/compute.sha256")
        self.assertEqual(r.status, 200)
        r2 = await self.client.get("/v1/capabilities/nope")
        self.assertEqual(r2.status, 404)

    async def test_invoke_sha256(self):
        r = await self.client.post("/v1/capabilities/compute.sha256/invoke",
                                   json={"payload": {"text": "hallo"}})
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["result"]["hex"], hashlib.sha256(b"hallo").hexdigest())

    async def test_invoke_unknown_404(self):
        r = await self.client.post("/v1/capabilities/nope/invoke", json={"payload": {}})
        self.assertEqual(r.status, 404)

    async def test_invoke_malformed_400(self):
        r = await self.client.post("/v1/capabilities/compute.sha256/invoke",
                                   data=b"not json")
        self.assertEqual(r.status, 400)

    async def test_invoke_validation_400(self):
        r = await self.client.post("/v1/capabilities/compute.sha256/invoke",
                                   json={"payload": {"wrong": 1}})
        self.assertEqual(r.status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
