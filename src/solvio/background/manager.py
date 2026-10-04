"""BackgroundManager — Core control plane orchestration (STEP 21.2G).

Owns the crash-safe submission pattern, status sync, result retrieval with digest
verification, cancellation, and reconciliation. It never raises its own authority:
a research.* result is classified UNTRUSTED_WEB regardless of the node, storage, or
elapsed time. It never writes canonical memory and never invokes a tool.

There is deliberately NO always-on poller here (PHASE 28): refresh()/reconcile() are
explicit. A standing coordinator is a later step.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from solvio.background.errors import (
    JobNotFound,
    ResultDigestMismatch,
    ResultNotReady,
)
from solvio.background.models import (
    LOCAL_TERMINAL,
    NODE_TO_LOCAL,
    BackgroundJobSpec,
    LocalJobStatus,
)
from solvio.background.reconciliation import next_reconcile_step
from solvio.background.store import LocalJobStore
from solvio.nodes.errors import NodeCapabilityError, NodeUnavailableError
from solvio.nodes.models import DataClass
from solvio.nodes.trust_map import node_result_trust


class BackgroundManager:
    def __init__(self, store: LocalJobStore, registry, router) -> None:
        self.store = store
        self.registry = registry
        self.router = router

    def _client(self, node_id: str):
        return self.registry.managed(node_id).client

    # -- crash-safe submit (PHASE 16) ---------------------------------------
    async def submit(self, capability_id: str, payload: dict, *,
                     data_class: DataClass, max_attempts: int = 3) -> str:
        # Privacy-aware routing FIRST: HOME_ONLY/LAN raise PrivacyRoutingError here,
        # before any bytes reach a remote node.
        node = self.router.select(capability_id, data_class=data_class)
        local_job_id = uuid.uuid4().hex
        idem = uuid.uuid4().hex
        privacy = data_class.name  # e.g. "REMOTE_CONTROLLED_ALLOWED"
        await self.store.create_pending(
            local_job_id=local_job_id, capability_id=capability_id,
            node_id=node.node_id, privacy_class=privacy, idempotency_key=idem,
            payload=payload, max_attempts=max_attempts)
        # A crash here leaves LOCAL_PENDING_SUBMIT; reconcile resends with `idem`.
        try:
            resp = await self._client(node.node_id).submit_job(
                capability_id, payload, privacy_class=privacy,
                idempotency_key=idem, max_attempts=max_attempts)
        except NodeUnavailableError:
            return local_job_id  # uncertain; stays LOCAL_PENDING_SUBMIT
        await self.store.mark_submitted(local_job_id, resp["job_id"],
                                        LocalJobStatus.QUEUED)
        return local_job_id

    async def refresh(self, local_job_id: str) -> BackgroundJobSpec:
        spec = await self.store.get(local_job_id)
        if spec is None:
            raise JobNotFound(local_job_id)
        if spec.remote_job_id is None or spec.status in LOCAL_TERMINAL:
            return spec
        try:
            meta = await self._client(spec.node_id).get_job(spec.remote_job_id)
        except NodeCapabilityError as exc:
            if (exc.error_code or "") == "NOT_FOUND":
                await self.store.update_status(local_job_id, LocalJobStatus.REMOTE_UNKNOWN)
                return await self.store.get(local_job_id)
            raise
        new_status = NODE_TO_LOCAL.get(meta.get("status"), spec.status)
        await self.store.update_status(local_job_id, new_status,
                                       attempt_count=meta.get("attempt_count"),
                                       error_detail=meta.get("error_code"))
        await self.store.set_result_meta(local_job_id,
                                         bool(meta.get("result_available")),
                                         meta.get("result_digest"))
        return await self.store.get(local_job_id)

    async def result(self, local_job_id: str) -> dict:
        spec = await self.store.get(local_job_id)
        if spec is None:
            raise JobNotFound(local_job_id)
        if spec.remote_job_id is None:
            raise ResultNotReady("not submitted")
        res = await self._client(spec.node_id).get_job_result(spec.remote_job_id)
        if not res.get("available"):
            raise ResultNotReady(res.get("status", "unknown"))
        payload = res["result"]
        raw = json.dumps(payload, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        claimed = res.get("result_digest")
        if claimed and claimed != digest:
            raise ResultDigestMismatch(local_job_id)
        # The CORE assigns trust; research.* is UNTRUSTED_WEB, never authority-bearing.
        trust = node_result_trust(spec.capability_id)
        return {"result": payload, "digest": digest,
                "digest_ok": (claimed == digest), "trust_level": trust,
                "capability_id": spec.capability_id, "node_id": spec.node_id}

    async def cancel(self, local_job_id: str) -> str:
        spec = await self.store.get(local_job_id)
        if spec is None:
            raise JobNotFound(local_job_id)
        if spec.remote_job_id is None:
            await self.store.update_status(local_job_id, LocalJobStatus.CANCELLED)
            return LocalJobStatus.CANCELLED.value
        resp = await self._client(spec.node_id).cancel_job(spec.remote_job_id)
        status = NODE_TO_LOCAL.get(resp.get("status"), LocalJobStatus.CANCEL_REQUESTED)
        await self.store.update_status(local_job_id, status)
        return status.value

    # -- reconciliation after a Core restart (PHASE 26) ---------------------
    async def reconcile(self) -> dict:
        out = {"resent": 0, "refreshed": 0, "remote_unknown": 0, "unreachable": 0}
        for spec in await self.store.list_non_terminal():
            try:
                if next_reconcile_step(spec.status) == "resend":
                    info = await self.store.resend_info(spec.local_job_id)
                    resp = await self._client(spec.node_id).submit_job(
                        info["capability_id"], info["payload"],
                        privacy_class=info["privacy_class"],
                        idempotency_key=info["idempotency_key"],
                        max_attempts=info["max_attempts"])
                    await self.store.mark_submitted(spec.local_job_id, resp["job_id"],
                                                    LocalJobStatus.QUEUED)
                    out["resent"] += 1
                else:
                    before = spec.status
                    updated = await self.refresh(spec.local_job_id)
                    if updated.status == LocalJobStatus.REMOTE_UNKNOWN and before != LocalJobStatus.REMOTE_UNKNOWN:
                        out["remote_unknown"] += 1
                    else:
                        out["refreshed"] += 1
            except NodeUnavailableError:
                out["unreachable"] += 1  # leave as-is; the Core stays usable, retry later
        return out
