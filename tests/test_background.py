"""STEP 21.2G — Core background control plane: store, manager, reconcile, trust.

No network. The NodeClient is stubbed. Proves crash-safe submission, idempotent
resend, reconciliation, result digest verification, UNTRUSTED_WEB trust, privacy
routing, durable reopen, and no authority escalation.
"""
from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from unittest import IsolatedAsyncioTestCase

from solvio.background.errors import ResultDigestMismatch, ResultNotReady
from solvio.background.manager import BackgroundManager
from solvio.background.models import LocalJobStatus
from solvio.background.reconciliation import next_reconcile_step
from solvio.background.store import LocalJobStore
from solvio.contracts.trust import AUTHORITY_BEARING, TrustLevel
from solvio.nodes.config import NodeConnectionConfig, TLSClientPaths
from solvio.nodes.errors import (
    NodeCapabilityError,
    NodeUnavailableError,
    PrivacyRoutingError,
)
from solvio.nodes.models import (
    AvailabilityClass,
    DataClass,
    NodeDescriptor,
    NodeHealthState,
    NodePlacement,
    PrivacyZone,
)
from solvio.nodes.registry import NodeRegistry
from solvio.nodes.router import NodeRouter

RESEARCH = "research.fetch_public_url"
SAMPLE_RESULT = {"final_url": "https://example.com/", "status_code": 200,
                 "text": "Example Domain", "sha256": "x"}


def _digest(obj):
    return hashlib.sha256(json.dumps(obj, separators=(",", ":")).encode()).hexdigest()


class StubJobClient:
    def __init__(self):
        self.submits = []
        self._by_idem = {}
        self.offline = False
        self.meta = {"status": "QUEUED", "attempt_count": 1,
                     "result_available": False, "result_digest": None}
        self.result_payload = None
        self.result_digest_override = None
        self.not_found = False
        self.cancelled = []

    async def submit_job(self, cap, payload, *, privacy_class, idempotency_key, max_attempts=3):
        if self.offline:
            raise NodeUnavailableError("node down")
        self.submits.append({"cap": cap, "payload": payload,
                             "privacy": privacy_class, "idem": idempotency_key})
        if idempotency_key in self._by_idem:
            return {"job_id": self._by_idem[idempotency_key], "status": "QUEUED", "duplicate": True}
        rid = f"remote-{len(self._by_idem) + 1}"
        self._by_idem[idempotency_key] = rid
        return {"job_id": rid, "status": "QUEUED", "duplicate": False}

    async def get_job(self, job_id):
        if self.not_found:
            raise NodeCapabilityError("NOT_FOUND", error_code="NOT_FOUND")
        return {"job_id": job_id, **self.meta}

    async def get_job_result(self, job_id):
        if self.result_payload is None:
            return {"available": False, "status": "RUNNING"}
        dg = self.result_digest_override or _digest(self.result_payload)
        return {"available": True, "status": "SUCCEEDED",
                "result": self.result_payload, "result_digest": dg}

    async def cancel_job(self, job_id):
        self.cancelled.append(job_id)
        return {"job_id": job_id, "status": "CANCEL_REQUESTED"}


def build_manager(store, stub):
    reg = NodeRegistry(client_factory=lambda conn: stub)
    desc = NodeDescriptor(node_id="hetzner-main", display_name="h",
                          endpoint="https://10.77.0.1:8443",
                          expected_capabilities=[RESEARCH],
                          availability_class=AvailabilityClass.ALWAYS_ON_REMOTE,
                          placement=NodePlacement.REMOTE_DATACENTER,
                          privacy_zone=PrivacyZone.REMOTE_CONTROLLED)
    conn = NodeConnectionConfig(node_id="hetzner-main",
                                endpoint="https://10.77.0.1:8443",
                                tls=TLSClientPaths("/x", "/x", "/x"),
                                identity="hetzner-main")
    reg.register(desc, conn)
    router = NodeRouter(reg, health_provider=lambda n: NodeHealthState.HEALTHY)
    return BackgroundManager(store, reg, router)


class TestReconciliationLogic(unittest.TestCase):
    def test_steps(self):
        self.assertEqual(next_reconcile_step(LocalJobStatus.LOCAL_PENDING_SUBMIT), "resend")
        self.assertEqual(next_reconcile_step(LocalJobStatus.QUEUED), "refresh")
        self.assertEqual(next_reconcile_step(LocalJobStatus.RUNNING), "refresh")


class TestLocalStore(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "background_jobs.sqlite3")
        self.store = LocalJobStore(self.path)
        await self.store.open()

    async def asyncTearDown(self):
        await self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_permissions(self):
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")

    async def test_create_and_reopen_durable(self):
        await self.store.create_pending(local_job_id="L1", capability_id=RESEARCH,
                                        node_id="hetzner-main",
                                        privacy_class="REMOTE_CONTROLLED_ALLOWED",
                                        idempotency_key="idem-1",
                                        payload={"url": "https://example.com/"},
                                        max_attempts=3)
        await self.store.close()
        store2 = LocalJobStore(self.path)          # reopen (new process simulation)
        await store2.open()
        spec = await store2.get("L1")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.status, LocalJobStatus.LOCAL_PENDING_SUBMIT)
        info = await store2.resend_info("L1")
        self.assertEqual(info["idempotency_key"], "idem-1")
        self.assertEqual(info["payload"], {"url": "https://example.com/"})
        await store2.close()
        self.store = LocalJobStore(self.path)      # for tearDown
        await self.store.open()

    async def test_list_non_terminal(self):
        await self.store.create_pending(local_job_id="A", capability_id=RESEARCH,
                                        node_id="hetzner-main", privacy_class="REMOTE_CONTROLLED_ALLOWED",
                                        idempotency_key="a", payload={}, max_attempts=3)
        await self.store.update_status("A", LocalJobStatus.SUCCEEDED)
        await self.store.create_pending(local_job_id="B", capability_id=RESEARCH,
                                        node_id="hetzner-main", privacy_class="REMOTE_CONTROLLED_ALLOWED",
                                        idempotency_key="b", payload={}, max_attempts=3)
        pending = await self.store.list_non_terminal()
        self.assertEqual([s.local_job_id for s in pending], ["B"])


class TestManager(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = LocalJobStore(os.path.join(self.dir, "bg.sqlite3"))
        await self.store.open()
        self.stub = StubJobClient()
        self.mgr = build_manager(self.store, self.stub)

    async def asyncTearDown(self):
        await self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_submit_maps_and_persists(self):
        lid = await self.mgr.submit(RESEARCH, {"url": "https://example.com/"},
                                    data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        spec = await self.store.get(lid)
        self.assertEqual(spec.status, LocalJobStatus.QUEUED)
        self.assertIsNotNone(spec.remote_job_id)
        self.assertEqual(self.stub.submits[0]["privacy"], "REMOTE_CONTROLLED_ALLOWED")

    async def test_submit_offline_stays_pending(self):
        self.stub.offline = True
        lid = await self.mgr.submit(RESEARCH, {"url": "x"},
                                    data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        spec = await self.store.get(lid)
        self.assertEqual(spec.status, LocalJobStatus.LOCAL_PENDING_SUBMIT)
        self.assertIsNone(spec.remote_job_id)

    async def test_reconcile_resends_same_idempotency_key(self):
        self.stub.offline = True
        lid = await self.mgr.submit(RESEARCH, {"url": "x"},
                                    data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        idem = (await self.store.resend_info(lid))["idempotency_key"]
        self.stub.offline = False
        out = await self.mgr.reconcile()
        self.assertEqual(out["resent"], 1)
        spec = await self.store.get(lid)
        self.assertEqual(spec.status, LocalJobStatus.QUEUED)
        self.assertEqual(self.stub.submits[0]["idem"], idem)   # same key -> node dedups

    async def test_idempotent_dedup_same_key_same_remote(self):
        r1 = await self.stub.submit_job(RESEARCH, {}, privacy_class="REMOTE_CONTROLLED_ALLOWED",
                                        idempotency_key="dup")
        r2 = await self.stub.submit_job(RESEARCH, {}, privacy_class="REMOTE_CONTROLLED_ALLOWED",
                                        idempotency_key="dup")
        self.assertEqual(r1["job_id"], r2["job_id"])
        self.assertTrue(r2["duplicate"])

    async def test_refresh_maps_status(self):
        lid = await self.mgr.submit(RESEARCH, {"url": "x"},
                                    data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        self.stub.meta = {"status": "SUCCEEDED", "attempt_count": 1,
                          "result_available": True, "result_digest": "abc"}
        spec = await self.mgr.refresh(lid)
        self.assertEqual(spec.status, LocalJobStatus.SUCCEEDED)
        self.assertTrue(spec.result_available)

    async def test_result_digest_and_trust(self):
        lid = await self.mgr.submit(RESEARCH, {"url": "x"},
                                    data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        self.stub.result_payload = SAMPLE_RESULT
        out = await self.mgr.result(lid)
        self.assertTrue(out["digest_ok"])
        self.assertEqual(out["trust_level"], TrustLevel.UNTRUSTED_WEB)
        self.assertNotIn(out["trust_level"], AUTHORITY_BEARING)

    async def test_result_digest_mismatch(self):
        lid = await self.mgr.submit(RESEARCH, {"url": "x"},
                                    data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        self.stub.result_payload = SAMPLE_RESULT
        self.stub.result_digest_override = "deadbeef"   # wrong digest
        with self.assertRaises(ResultDigestMismatch):
            await self.mgr.result(lid)

    async def test_result_not_ready(self):
        lid = await self.mgr.submit(RESEARCH, {"url": "x"},
                                    data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        with self.assertRaises(ResultNotReady):
            await self.mgr.result(lid)   # result_payload stays None

    async def test_remote_unknown_on_missing(self):
        lid = await self.mgr.submit(RESEARCH, {"url": "x"},
                                    data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        self.stub.not_found = True
        spec = await self.mgr.refresh(lid)
        self.assertEqual(spec.status, LocalJobStatus.REMOTE_UNKNOWN)

    async def test_cancel(self):
        lid = await self.mgr.submit(RESEARCH, {"url": "x"},
                                    data_class=DataClass.REMOTE_CONTROLLED_ALLOWED)
        status = await self.mgr.cancel(lid)
        self.assertEqual(status, "CANCEL_REQUESTED")
        self.assertEqual(len(self.stub.cancelled), 1)

    async def test_privacy_home_only_rejected(self):
        with self.assertRaises(PrivacyRoutingError):
            await self.mgr.submit(RESEARCH, {"url": "x"}, data_class=DataClass.HOME_ONLY)
        # nothing persisted
        self.assertEqual(await self.store.list_non_terminal(), [])


if __name__ == "__main__":
    from _harness import run_unittest
    raise SystemExit(run_unittest(globals(), __name__))
