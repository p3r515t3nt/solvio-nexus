"""Authenticated Owner follow-ups in the existing task, receipt and cost ledger.

The previous result is read-only context. A new, exact direct Owner receipt
authorizes the next run; neither a run id nor a device transport credential does.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib

from aiohttp import web

from solvio.agent_runtime import task_revisions as TR
from solvio.agent_runtime.task_authority import VerifiedTaskReceipt
from solvio.agent_runtime.task_endpoint import body, response, error
from solvio.agent_runtime.task_start_proof import TaskStartProofService
from solvio.security.mobile_approval import browser_sessions as B, protocol as P

DOMAIN_TASK_FOLLOWUP = b"SOLVIO_APP_TASK_FOLLOWUP_V1"
TYPE_TASK_FOLLOWUP = "app_task_followup_binding"
FIELDS = {"run_id", "text", "expected_revision", "expected_digest", "input_artifact_ids", "client_request_id"}


def request_digest(value):
    return hashlib.sha256(P.canonical_bytes(TR.canonical_followup(value))).hexdigest()


def client_data_hash(raw):
    return hashlib.sha256(DOMAIN_TASK_FOLLOWUP + b"\0" + raw).digest()


class TaskFollowupProofService(TaskStartProofService):
    body_digest = staticmethod(request_digest)
    assertion_hash = staticmethod(client_data_hash)
    challenge_prefix = "app-task-followup:"
    binding_type = TYPE_TASK_FOLLOWUP
    audit_prefix = "app_task_followup"


def _owned(orch, run_id, principal):
    run = orch.ledger.get_run(run_id)
    task = orch.ledger.get_task(run.task_id) if run else None
    return task is not None and task.created_principal == principal


def _accepted(orch, revision):
    return response({key: revision[key] for key in ("task_id", "run_id", "parent_run_id", "revision", "digest")} |
                    {"annahme": "ready" if orch.task_starts.ready(revision["run_id"]) else "preparing"}, 202)


def attach(app, orchestrator):
    async def challenge(request):
        cp = request.app.get("control_plane")
        if cp is None or not request.secure or B.has_credentials(request):
            return error(401, "unauthorized")
        from solvio.security.mobile_approval.gateway import _authed_device
        device_id = await _authed_device(request)
        device = await cp.store.get_device(device_id) if device_id else None
        if device is None:
            return error(401, "unauthorized")
        orch = orchestrator(request)
        if orch is None:
            return error(503, "agent_runtime_disabled")
        run_id = request.match_info["run_id"]
        if not _owned(orch, run_id, device["principal"]):
            return error(404, "unknown_run")
        try:
            data = await body(request, {"followup"})
            value = TR.canonical_followup(data["followup"])
            if value["run_id"] != run_id:
                raise ValueError("invalid_followup")
        except (ValueError, TypeError, KeyError, UnicodeError):
            return error(400, "invalid_followup")
        result = await TaskFollowupProofService(cp).issue(device_id=device_id,
            transport_cred=request.headers.get("X-Transport-Cred", ""), task_body=value)
        return response(result.as_dict()) if result else error(401, "unauthorized")

    async def followup(request):
        if not request.secure:
            return error(401, "unauthorized")
        browser = await B.actor(request, mutating=True)
        if browser is None and B.has_credentials(request):
            return error(401, "unauthorized")
        run_id = request.match_info["run_id"]
        verified = None
        proof_service = None
        try:
            if browser:
                value = TR.canonical_followup(await body(request, FIELDS))
                principal = browser.principal
                receipt = VerifiedTaskReceipt("dashboard_session",
                    "browser:" + browser.session_id + ":" + value["client_request_id"], principal)
            else:
                cp = request.app.get("control_plane")
                if cp is None:
                    return error(401, "unauthorized")
                data = await body(request, {"followup", "proof"})
                value = TR.canonical_followup(data["followup"])
                proof = data["proof"]
                if type(proof) is not dict or set(proof) != {"nonce", "assertion_b64"}:
                    raise ValueError("invalid_proof")
                proof_service = TaskFollowupProofService(cp)
                verified = await proof_service.verify(device_id=request.headers.get("X-Device-Id", ""),
                    nonce=proof["nonce"], task_body=value,
                    assertion=base64.b64decode(proof["assertion_b64"], validate=True))
                if verified is None:
                    return error(401, "unauthorized")
                principal = verified.principal
                receipt = VerifiedTaskReceipt("app_session", "app:followup:" + verified.nonce, principal)
            if value["run_id"] != run_id:
                raise ValueError("invalid_followup")
        except (ValueError, TypeError, KeyError, UnicodeError):
            return error(400 if browser else 401, "invalid_followup" if browser else "unauthorized")
        orch = orchestrator(request)
        if orch is None:
            return error(503, "agent_runtime_disabled")
        if not _owned(orch, run_id, principal):
            return error(404, "unknown_run")

        async def current():
            authorized = (await B.actor(request, mutating=True) == browser if browser else
                          await proof_service.still_current(verified))
            return authorized and orchestrator(request) is orch and _owned(orch, run_id, principal)

        try:
            # Read/file verification may await; recheck the same authenticated
            # input afterwards, immediately before synchronous atomic admission.
            prior = await asyncio.to_thread(TR.replay, orch.ledger, value, receipt)
            if prior is not None:
                if not await current():
                    return error(401, "unauthorized")
                return _accepted(orch, prior)
            prepared = await asyncio.to_thread(TR.prepare, orch.ledger, value, receipt)
            if not await current():
                return error(401, "unauthorized")
            _, run = orch.task_starts.admit_followup(prepared)
            revision = TR.revision_for_run(orch.ledger, run.run_id)
        except (ValueError, TypeError, KeyError, OSError):
            # A parallel identical admission may have committed while this
            # request verified its files. Recover it, never create another run.
            try:
                prior = TR.replay(orch.ledger, value, receipt)
            except (ValueError, TypeError, KeyError, OSError):
                prior = None
            if not await current():
                return error(401, "unauthorized")
            if prior is not None:
                return _accepted(orch, prior)
            return response({"error": "followup_not_available",
                "reason": "Dieser Auftragsstand lässt sich so nicht fortsetzen. Bitte lies den aktuellen Stand erneut."}, 409)
        orch.offer_task_observation(run.task_id, run.run_id)
        return _accepted(orch, revision)

    app.add_routes([web.post("/v1/agent/runs/{run_id}/followup/challenge", challenge),
                    web.post("/v1/agent/runs/{run_id}/followup", followup)])
