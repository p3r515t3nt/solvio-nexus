""" — node durable background jobs: model, store, worker, API, logging.

No test needs the internet. Fake background-safe capabilities exercise the worker;
the API is tested over aiohttp's test client with a stubbed client identity (mTLS
identity extraction is validated live). Covers Phase 45.

Run:  PYTHONPATH=src python tests/test_jobs.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import unittest
from unittest import IsolatedAsyncioTestCase

from aiohttp.test_utils import AioHTTPTestCase
from pydantic import BaseModel

import solvio_node.jobs.api as jobs_api
from solvio_node.capabilities.base import (
    Capability,
    ExecutionMode,
    PrivacyClass,
    RiskLevel,
)
from solvio_node.config import JobConfig, NodeConfig, TLSPaths
from solvio_node.jobs.model import (
    TERMINAL,
    JobStatus,
    can_transition,
    hash_idempotency_key,
)
from solvio_node.jobs.store import JobStore, JobStoreCorrupt
from solvio_node.jobs.worker import JobWorkerPool
from solvio_node.logging import NodeLogger
from solvio_node.registry import CapabilityRegistry
from solvio_node.server import build_app


# --------------------------------------------------------------------------
# Fake capabilities
# --------------------------------------------------------------------------
class EchoIn(BaseModel):
    value: str


class EchoOut(BaseModel):
    echoed: str


class EchoCap(Capability):
    id = "test.echo"
    version = "1"
    description = "echo (background test)"
    risk_level = RiskLevel.SAFE
    privacy_class = PrivacyClass.SYNTHETIC
    execution_mode = ExecutionMode.INLINE
    supports_background = True
    timeout_s = 5.0
    input_model = EchoIn
    output_model = EchoOut

    async def invoke(self, data):
        return EchoOut(echoed=data.value)


class NonBgCap(EchoCap):
    id = "test.nonbg"
    supports_background = False


class FlakyCap(Capability):
    id = "test.flaky"
    version = "1"
    description = "fails transiently N times"
    risk_level = RiskLevel.SAFE
    privacy_class = PrivacyClass.SYNTHETIC
    execution_mode = ExecutionMode.INLINE
    supports_background = True
    timeout_s = 5.0
    input_model = EchoIn
    output_model = EchoOut

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    async def invoke(self, data):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("transient")   # classified transient -> retry
        return EchoOut(echoed=f"ok-after-{self.calls}")


class SlowCap(Capability):
    id = "test.slow"
    version = "1"
    description = "slow, for cancellation"
    risk_level = RiskLevel.SAFE
    privacy_class = PrivacyClass.SYNTHETIC
    execution_mode = ExecutionMode.INLINE
    supports_background = True
    timeout_s = 30.0
    input_model = EchoIn
    output_model = EchoOut

    async def invoke(self, data):
        await asyncio.sleep(30)
        return EchoOut(echoed="never")


def registry_with(*caps) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    for c in caps:
        reg.register(c)
    return reg


def tmp_db():
    d = tempfile.mkdtemp()
    return d, os.path.join(d, "jobs.sqlite3")


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------
class TestModel(unittest.TestCase):
    def test_transitions(self):
        self.assertTrue(can_transition(JobStatus.QUEUED, JobStatus.RUNNING))
        self.assertTrue(can_transition(JobStatus.RUNNING, JobStatus.SUCCEEDED))
        self.assertTrue(can_transition(JobStatus.RUNNING, JobStatus.QUEUED))  # requeue
        self.assertTrue(can_transition(JobStatus.SUCCEEDED, JobStatus.RESULT_EXPIRED))
        self.assertFalse(can_transition(JobStatus.SUCCEEDED, JobStatus.RUNNING))
        self.assertFalse(can_transition(JobStatus.FAILED, JobStatus.QUEUED))

    def test_terminal(self):
        self.assertIn(JobStatus.SUCCEEDED, TERMINAL)
        self.assertIn(JobStatus.CANCELLED, TERMINAL)
        self.assertNotIn(JobStatus.QUEUED, TERMINAL)
        self.assertNotIn(JobStatus.RUNNING, TERMINAL)

    def test_idempotency_hash_scoped(self):
        a = hash_idempotency_key("cn:a", "cap", "k1")
        b = hash_idempotency_key("cn:b", "cap", "k1")   # different client
        c = hash_idempotency_key("cn:a", "cap", "k1")   # same
        self.assertNotEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual(len(a), 64)


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------
class TestStore(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir, self.path = tmp_db()
        self.store = JobStore(self.path)
        await self.store.open()

    async def asyncTearDown(self):
        await self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def _mk(self, client="cn:a", key="k1", cap="test.echo", payload=None):
        return await self.store.create(client_identity=client, capability_id=cap,
                                       privacy_class="REMOTE_CONTROLLED_ALLOWED",
                                       idempotency_key=key,
                                       payload=payload or {"value": "x"}, max_attempts=3)

    async def test_db_permissions(self):
        mode = oct(os.stat(self.path).st_mode & 0o777)
        self.assertEqual(mode, "0o600")

    async def test_create_and_get(self):
        job_id, created = await self._mk()
        self.assertTrue(created)
        job = await self.store.get(job_id, "cn:a")
        self.assertEqual(job.status, JobStatus.QUEUED)
        self.assertEqual(job.capability_id, "test.echo")

    async def test_idempotent_dedup(self):
        j1, c1 = await self._mk(key="same")
        j2, c2 = await self._mk(key="same")
        self.assertTrue(c1)
        self.assertFalse(c2)          # duplicate -> not created
        self.assertEqual(j1, j2)      # same job id

    async def test_ownership(self):
        job_id, _ = await self._mk(client="cn:a")
        self.assertIsNone(await self.store.get(job_id, "cn:b"))       # not owner
        self.assertIsNone(await self.store.get_result(job_id, "cn:b"))
        self.assertIsNone(await self.store.request_cancel(job_id, "cn:b"))

    async def test_claim_finish_result_digest(self):
        job_id, _ = await self._mk()
        claimed = await self.store.claim_next()
        self.assertEqual(claimed["job_id"], job_id)
        job = await self.store.get(job_id, "cn:a")
        self.assertEqual(job.status, JobStatus.RUNNING)
        await self.store.finish_success(job_id, {"echoed": "x"}, "digest123")
        res = await self.store.get_result(job_id, "cn:a")
        self.assertTrue(res["available"])
        self.assertEqual(res["result"], {"echoed": "x"})
        self.assertEqual(res["result_digest"], "digest123")
        job = await self.store.get(job_id, "cn:a")
        self.assertEqual(job.status, JobStatus.SUCCEEDED)

    async def test_input_purged_after_terminal(self):
        job_id, _ = await self._mk()
        await self.store.claim_next()
        await self.store.finish_success(job_id, {"echoed": "x"}, "d")

        # input_payload must be NULL now (run the check inside the store's thread)
        def _check():
            return self.store._conn.execute(
                "SELECT input_payload FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        row = await self.store._run(_check)
        self.assertIsNone(row["input_payload"])

    async def test_cancel_queued_and_running(self):
        j1, _ = await self._mk(key="q")
        self.assertEqual(await self.store.request_cancel(j1, "cn:a"), "CANCELLED")
        j2, _ = await self._mk(key="r")
        await self.store.claim_next()
        self.assertEqual(await self.store.request_cancel(j2, "cn:a"), "CANCEL_REQUESTED")
        self.assertTrue(await self.store.is_cancel_requested(j2))

    async def test_recover_running_to_queued(self):
        job_id, _ = await self._mk()
        await self.store.claim_next()   # -> RUNNING
        await self.store.close()
        store2 = JobStore(self.path)
        await store2.open()
        stats = await store2.recover()
        self.assertEqual(stats["requeued"], 1)
        job = await store2.get(job_id, "cn:a")
        self.assertEqual(job.status, JobStatus.QUEUED)
        await store2.close()
        self.store = JobStore(self.path)      # for tearDown
        await self.store.open()

    async def test_queue_bound_count(self):
        await self._mk(key="a")
        await self._mk(key="b")
        self.assertEqual(await self.store.count_active(), 2)

    async def test_result_ttl_expires(self):
        store = JobStore(self.path + ".ttl", result_ttl_s=-1)
        await store.open()
        jid, _ = await store.create(client_identity="cn:a", capability_id="test.echo",
                                    privacy_class="REMOTE_CONTROLLED_ALLOWED",
                                    idempotency_key="k", payload={"value": "x"}, max_attempts=3)
        await store.claim_next()
        await store.finish_success(jid, {"echoed": "x"}, "d")
        stats = await store.cleanup_expired()
        self.assertGreaterEqual(stats["result_expired"], 1)
        res = await store.get_result(jid, "cn:a")
        self.assertFalse(res["available"])
        self.assertEqual(res["status"], "RESULT_EXPIRED")
        await store.close()

    async def test_metadata_ttl_deletes_terminal_only(self):
        store = JobStore(self.path + ".mtl", metadata_ttl_s=-1)
        await store.open()
        # terminal job -> deleted; active job -> kept
        jt, _ = await store.create(client_identity="cn:a", capability_id="test.echo",
                                   privacy_class="REMOTE_CONTROLLED_ALLOWED",
                                   idempotency_key="t", payload={"value": "x"}, max_attempts=3)
        await store.claim_next()
        await store.finish_failed(jt, "X")
        ja, _ = await store.create(client_identity="cn:a", capability_id="test.echo",
                                   privacy_class="REMOTE_CONTROLLED_ALLOWED",
                                   idempotency_key="a", payload={"value": "x"}, max_attempts=3)
        stats = await store.cleanup_expired()
        self.assertGreaterEqual(stats["metadata_deleted"], 1)
        self.assertIsNone(await store.get(jt, "cn:a"))       # terminal deleted
        self.assertIsNotNone(await store.get(ja, "cn:a"))    # active kept
        await store.close()

    async def test_corrupt_db_detected(self):
        d, p = tmp_db()
        with open(p, "wb") as fh:
            fh.write(b"this is not a sqlite database at all, garbage bytes" * 10)
        store = JobStore(p)
        with self.assertRaises(Exception):   # JobStoreCorrupt or sqlite DatabaseError
            await store.open()
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
class TestWorker(IsolatedAsyncioTestCase):
    async def _pool(self, reg, **kw):
        d, p = tmp_db()
        store = JobStore(p)
        await store.open()
        pool = JobWorkerPool(store, reg, None, workers=1,
                             base_backoff=0.001, max_backoff=0.005, poll_interval=0.05, **kw)
        await pool.start()
        self.addAsyncCleanup(self._teardown, pool, store, d)
        return store, pool

    async def _teardown(self, pool, store, d):
        await pool.stop()
        await store.close()
        shutil.rmtree(d, ignore_errors=True)

    async def _wait_terminal(self, store, jid, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            job = await store.get(jid, "cn:a")
            if job and job.status in TERMINAL:
                return job
            await asyncio.sleep(0.02)
        return await store.get(jid, "cn:a")

    async def _submit(self, store, pool, cap="test.echo", payload=None, max_attempts=3):
        jid, _ = await store.create(client_identity="cn:a", capability_id=cap,
                                    privacy_class="REMOTE_CONTROLLED_ALLOWED",
                                    idempotency_key=f"{cap}-{time.monotonic()}",
                                    payload=payload or {"value": "hi"}, max_attempts=max_attempts)
        pool.notify()
        return jid

    async def test_success_and_digest(self):
        store, pool = await self._pool(registry_with(EchoCap()))
        jid = await self._submit(store, pool)
        job = await self._wait_terminal(store, jid)
        self.assertEqual(job.status, JobStatus.SUCCEEDED)
        res = await store.get_result(jid, "cn:a")
        self.assertEqual(res["result"], {"echoed": "hi"})
        import hashlib
        import json
        raw = json.dumps({"echoed": "hi"}, separators=(",", ":"))
        self.assertEqual(res["result_digest"], hashlib.sha256(raw.encode()).hexdigest())

    async def test_unsupported_capability_fails(self):
        store, pool = await self._pool(registry_with(NonBgCap()))
        jid = await self._submit(store, pool, cap="test.nonbg")
        job = await self._wait_terminal(store, jid)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.error_code, "UNSUPPORTED_CAPABILITY")

    async def test_retry_then_success(self):
        store, pool = await self._pool(registry_with(FlakyCap(fail_times=1)))
        jid = await self._submit(store, pool, cap="test.flaky", max_attempts=3)
        job = await self._wait_terminal(store, jid)
        self.assertEqual(job.status, JobStatus.SUCCEEDED)
        self.assertGreaterEqual(job.attempt_count, 2)

    async def test_retry_exhausted_fails(self):
        store, pool = await self._pool(registry_with(FlakyCap(fail_times=9)))
        jid = await self._submit(store, pool, cap="test.flaky", max_attempts=2)
        job = await self._wait_terminal(store, jid)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.attempt_count, 2)

    async def test_cancel_running(self):
        store, pool = await self._pool(registry_with(SlowCap()))
        jid = await self._submit(store, pool, cap="test.slow")
        # wait until RUNNING
        end = time.monotonic() + 3
        while time.monotonic() < end:
            job = await store.get(jid, "cn:a")
            if job.status == JobStatus.RUNNING:
                break
            await asyncio.sleep(0.02)
        await store.request_cancel(jid, "cn:a")
        job = await self._wait_terminal(store, jid, timeout=3)
        self.assertEqual(job.status, JobStatus.CANCELLED)


# --------------------------------------------------------------------------
# API (aiohttp test client, stubbed client identity)
# --------------------------------------------------------------------------
class TestApi(AioHTTPTestCase):
    async def get_application(self):
        self.dir, self.path = tmp_db()
        self._identity = "cn:client-a"
        self._orig_ci = jobs_api.client_identity
        jobs_api.client_identity = lambda req: self._identity   # stub mTLS identity
        cfg = NodeConfig(
            tls=TLSPaths(ca_cert="/x", server_cert="/x", server_key="/x"),
            jobs=JobConfig(enabled=True, db_path=self.path, workers=1, max_queued=3),
            log_path=os.path.join(self.dir, "log.jsonl"),
        )
        return build_app(cfg, registry=registry_with(EchoCap(), NonBgCap(), SlowCap()))

    def tearDown(self):
        jobs_api.client_identity = self._orig_ci
        shutil.rmtree(self.dir, ignore_errors=True)

    async def _submit(self, **over):
        body = {"capability_id": "test.echo", "payload": {"value": "hi"},
                "privacy_class": "REMOTE_CONTROLLED_ALLOWED", "idempotency_key": "k1"}
        body.update(over)
        return await self.client.post("/v1/jobs", json=body)

    async def test_submit_get_result(self):
        r = await self._submit()
        self.assertEqual(r.status, 201)
        jid = (await r.json())["job_id"]
        # poll result
        for _ in range(200):
            rr = await self.client.get(f"/v1/jobs/{jid}/result")
            data = await rr.json()
            if data.get("available"):
                break
            await asyncio.sleep(0.02)
        self.assertTrue(data["available"])
        self.assertEqual(data["result"], {"echoed": "hi"})
        self.assertEqual(len(data["result_digest"]), 64)

    async def test_dedup(self):
        r1 = await self._submit(idempotency_key="dup")
        r2 = await self._submit(idempotency_key="dup")
        self.assertEqual(r1.status, 201)
        self.assertEqual(r2.status, 200)
        self.assertEqual((await r1.json())["job_id"], (await r2.json())["job_id"])
        self.assertTrue((await r2.json())["duplicate"])

    async def test_privacy_reject(self):
        for pc in ["HOME_ONLY", "LAN_ALLOWED", "CLOUD_THIRD_PARTY_POLICY_REQUIRED"]:
            r = await self._submit(privacy_class=pc, idempotency_key=f"p-{pc}")
            self.assertEqual(r.status, 403)

    async def test_unsupported_and_unknown_capability(self):
        r1 = await self._submit(capability_id="test.nonbg", idempotency_key="n1")
        self.assertEqual(r1.status, 400)   # CAPABILITY_NOT_BACKGROUND
        r2 = await self._submit(capability_id="does.not.exist", idempotency_key="n2")
        self.assertEqual(r2.status, 404)   # UNKNOWN_CAPABILITY

    async def test_ownership_cross_client_404(self):
        r = await self._submit(idempotency_key="own")
        jid = (await r.json())["job_id"]
        self._identity = "cn:client-b"     # switch identity
        rr = await self.client.get(f"/v1/jobs/{jid}")
        self.assertEqual(rr.status, 404)
        rc = await self.client.post(f"/v1/jobs/{jid}/cancel")
        self.assertEqual(rc.status, 404)

    async def test_unauthenticated(self):
        self._identity = None
        r = await self._submit(idempotency_key="noauth")
        self.assertEqual(r.status, 401)

    async def test_queue_full(self):
        # max_queued=3; use non-draining slow jobs so the bound is deterministic
        codes = []
        for i in range(6):
            r = await self._submit(capability_id="test.slow", idempotency_key=f"qf-{i}")
            codes.append(r.status)
        self.assertIn(503, codes)          # at least one QUEUE_FULL


# --------------------------------------------------------------------------
# Logging redaction
# --------------------------------------------------------------------------
class TestLoggingRedaction(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = NodeLogger(os.path.join(self.dir, "l.jsonl"), "n", to_stderr=False)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_job_metadata_keys_allowed(self):
        self.log.event("job", job_id="j1", status="QUEUED", capability="c",
                       error_code=None, attempt=1)  # must not raise

    def test_payload_keys_rejected(self):
        for bad in [{"url": "x"}, {"text": "x"}, {"payload": "x"}, {"result": "x"},
                    {"idempotency_key": "x"}]:
            with self.assertRaises(ValueError):
                self.log.event("job", **bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
