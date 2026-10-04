"""Authentifizierte Auftragsannahme und Owner-Entscheidungen am Core-Gateway.

Die Anwendung stellt Identitaet, Sitzungen und Attestation bereit. Herkunft und
Beleg werden hier aus deren Ergebnissen gebaut, nie aus Aufgabenargumenten.
"""
from __future__ import annotations

import asyncio
import base64
from aiohttp import web

from solvio.agent_runtime.task_authority import VerifiedTaskReceipt
from solvio.agent_runtime.task_start_service import AuthorizedTaskStart, request_identifier
from solvio.agent_runtime.task_start_proof import TaskStartProofService, canonical_task_body
from solvio.agent_runtime.task_cost_approval_proof import (
    TaskCostApprovalProofService, canonical_cost_approval, approval_reference)
from solvio.capabilities.contract import ArgumentSource
from solvio.capabilities.policy import OriginClass
from solvio.capabilities.envelope import CapabilityOutcome
from solvio.contracts.trust import TrustContext, TrustLevel
from solvio.security.mobile_approval import browser_sessions as B

PREFIX = "/v1/agent"


def response(data, status=200):
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})


def error(status, reason):
    return response({"error": reason}, status)


async def body(request, fields, *, task_start=False):
    if request.content_type != "application/json":
        raise ValueError("json_required")
    # Only these two task entrances accept file bytes. The input contract
    # bounds the sum of all decoded files; the envelope allows base64 and
    # bounded request/proof metadata. Other routes retain their existing limit.
    from solvio.agent_runtime.file_inputs import MAX_TOTAL_BYTES
    reader = request.clone(client_max_size=4 * ((MAX_TOTAL_BYTES + 2) // 3) + 32 * 1024) if task_start else request
    value = await reader.json()
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError("invalid_fields")
    return value


def dashboard_ha_catalog_context(actor):
    """Fixed read context after the HA catalog endpoint verifies its owner.

    Origin stamping stays at this authenticated entrance. The caller supplies
    the actual BrowserActor returned by browser_sessions, never an origin or
    capability name. This context cannot select a native switching operation.
    """
    if type(actor) is not B.BrowserActor or not actor.principal or not actor.session_id:
        raise ValueError('verified_browser_actor_required')
    from solvio.secret_vault import context as SC
    return SC.UseContext(origin=OriginClass.TRUSTED_DASHBOARD,
                         principal=actor.principal, capability='ha_list_devices')


async def native_ha_catalog_context(request):
    """Read-only HA discovery using the existing enrolled-device check.

    A transport credential proves a reader, never a new task or a switch. The
    endpoint checks the configured owner and repeats this authentication after
    native awaits. A browser-cookie failure cannot fall back to device headers.
    """
    if not request.secure or B.has_credentials(request) or 'control_plane' not in request.app:
        return None
    from solvio.agent_runtime.endpoint import _owner_device
    device_id = await _owner_device(request)
    if device_id is None:
        return None
    device = await request.app['control_plane'].store.get_device(device_id)
    if not device or not device.get('principal'):
        return None
    from solvio.secret_vault import context as SC
    return (device_id, device['principal'], SC.UseContext(
        origin=OriginClass.TRUSTED_INTERACTIVE_APP, principal=device['principal'], capability='ha_list_devices'))


def conversation_store(request):
    """Der Gespraechsspeicher zur ANFRAGEZEIT — nie beim Anhaengen eingesammelt.

    Erste Wahl ist ein Provider unter `app["conversation_store_provider"]` (den der
    Chat-Endpunkt setzt); sonst der Speicher des laufenden Sprachservers
    (`app["voice_core_server"].conversations`). Fehlt beides, gibt es keinen
    Chat, an den ein Auftrag gebunden werden koennte — und ein
    `conversation_ref` ist dann `unknown_conversation`, nie still ignoriert.
    """
    provider = request.app.get("conversation_store_provider")
    if callable(provider):
        return provider()
    return getattr(request.app.get("voice_core_server"), "conversations", None)


def attach(app, *, orchestrator):
    async def native_credits(request):
        actor = await B.actor(request, mutating=request.method == "POST")
        orch = orchestrator(request)
        adapter = getattr(orch, "cost_quote_adapter", None)
        policy = getattr(adapter, "credit_policy", None)
        if actor is None or not request.secure:
            return error(401, "unauthorized")
        if policy is None or policy.approvals is None:
            return error(503, "credit_consent_unavailable")
        if actor.principal != policy.approvals.owner_principal:
            return error(403, "owner_configuration_required")
        try:
            account = await adapter.credit_account()
            if await B.actor(request, mutating=request.method == "POST") != actor:
                return error(401, "unauthorized")
            if orchestrator(request) is not orch:
                return error(409, "credit_runtime_changed")
            if request.method == "POST":
                data = await body(request, {"account", "enabled"})
                if data["account"] != account or type(data["enabled"]) is not bool:
                    return error(409, "credit_account_changed")
                async def same_account(expected):
                    return await adapter.credit_account() == expected
                request.app.setdefault("native_credit_policies", set()).add(policy)
                approval_id = await policy.request(account, data["enabled"], account_check=same_account)
                return response({"state": "approval_required", "approval_id": approval_id})
            from solvio.agent_runtime.native_credit_policy import TERMS
            return response({"account": account, "enabled": bool(policy.grant(account)), "terms": TERMS})
        except (ValueError, TypeError, OSError):
            return error(409, "credit_account_unconfirmed")

    async def close_native_credits(_app):
        # Policy lives with the same Core approval runtime; no orphan watcher
        # may consume an approval after this HTTP application has stopped.
        for policy in _app.get("native_credit_policies", set()):
            await policy.close()
    app.on_cleanup.append(close_native_credits)
    async def challenge(request):
        cp = request.app.get("control_plane")
        if cp is None or not request.secure:
            return error(401, "unauthorized")
        try:
            data = await body(request, {"task"}, task_start=True)
            task = canonical_task_body(data["task"])
        except (ValueError, TypeError):
            return error(400, "invalid_task")
        result = await TaskStartProofService(cp).issue(
            device_id=request.headers.get("X-Device-Id", ""),
            transport_cred=request.headers.get("X-Transport-Cred", ""), task_body=task)
        return response(result.as_dict()) if result else error(401, "unauthorized")

    async def start(request):
        if not request.secure:
            return error(401, "unauthorized")
        browser = await B.actor(request, mutating=True)
        try:
            data = await body(request, {"task"} if browser else {"task", "proof"}, task_start=True)
            task = canonical_task_body(data["task"])
        except (ValueError, TypeError):
            return error(400 if browser else 401, "invalid_task" if browser else "unauthorized")
        request_id = task["client_request_id"]
        if browser:
            principal, origin = browser.principal, OriginClass.TRUSTED_DASHBOARD
            receipt = VerifiedTaskReceipt("dashboard_session",
                "browser:" + browser.session_id + ":" + request_id, principal)
        else:
            cp = request.app.get("control_plane")
            if cp is None:
                return error(401, "unauthorized")
            try:
                proof = data["proof"]
                if type(proof) is not dict or set(proof) != {"nonce", "assertion_b64"}:
                    raise ValueError("invalid_proof")
                raw = base64.b64decode(proof["assertion_b64"], validate=True)
                verified = await TaskStartProofService(cp).verify(
                    device_id=request.headers.get("X-Device-Id", ""), nonce=proof["nonce"],
                    task_body=task, assertion=raw)
            except (ValueError, TypeError):
                verified = None
            if verified is None:
                return error(401, "unauthorized")
            principal, origin = verified.principal, OriginClass.TRUSTED_INTERACTIVE_APP
            receipt = VerifiedTaskReceipt("app_session", "app:" + verified.nonce, principal)
        orch = orchestrator(request)
        if orch is None or orch.router is None:
            return error(503, "agent_runtime_disabled")
        from solvio.agent_runtime import portal_connection as PC
        portal_admission = None
        if PC.action_of(task.get('action_request')) is not None:
            try:
                portal_admission = await PC.preflight(orch, task['action_request'], principal, request_id)
            except Exception:
                return error(409, 'portal_connection_unavailable')
            if browser:
                if await B.actor(request, mutating=True) != browser:
                    return error(401, 'unauthorized')
            elif not await TaskStartProofService(cp).still_current(verified):
                return error(401, 'unauthorized')
            if orchestrator(request) is not orch:
                return error(409, 'portal_runtime_changed')
        name = "agent_task_" + task["scope"]
        arguments = {"objective": task["objective"]}
        if task["scope"] == "build":
            arguments["repository"] = task["target_repo"]
        # N8/C3: ein Auftrag aus einem Chat. Der Chat muss dem geprueften Principal
        # gehoeren (aktiv, explizit) — sonst 404, und kein Auftrag entsteht.
        conversation_ref = task.get("conversation_ref", "")
        store = None
        if conversation_ref:
            store = conversation_store(request)
            try:
                # Der Gespraechsspeicher haelt einen Lock ueber jede Methode; wie die
                # Nachbarn nie auf dem Loop warten (Review Runde 2, A-2).
                owned = store is not None and await asyncio.to_thread(
                    store.conversation_owned, conversation_ref, principal, for_write=True)
            except Exception:  # noqa: BLE001 - ein unlesbarer Speicher bindet nichts
                owned = False
            if not owned:
                return error(404, "unknown_conversation")
        start_receipt = AuthorizedTaskStart.bind(receipt=receipt, request_id=request_id,
            capability=name, arguments=arguments, document_request=task.get("document_request"),
            file_request=task.get("file_request"),
            action_request=task.get("action_request"), action_intent=task.get("action_intent"),
            portal_admission=portal_admission, conversation_ref=conversation_ref)
        result = await orch.router.execute(name, arguments,
            trust=TrustContext(TrustLevel.USER_DIRECT, user_authorized=True),
            provenance={key: ArgumentSource.USER_DIRECT for key in arguments},
            principal=principal, origin=origin, task_start=start_receipt)
        if result.outcome is not CapabilityOutcome.SUCCESS:
            return error(409, result.reason or result.outcome.value)
        task_id, run_id = result.data.get("task_id", ""), result.data.get("run_id", "")
        if conversation_ref and task_id and run_id:
            try:
                await asyncio.to_thread(store.add_task_link, conversation_ref, task_id, run_id,
                                        revision=1, source="task:" + request_id)
            except Exception as exc:  # noqa: BLE001 - der Auftrag ist angenommen; der Link fehlt
                from solvio.logging_setup import get_logger
                get_logger("agent_runtime").warning("agent_runtime.conversation_link_failed",
                    kind=type(exc).__name__, task_id=task_id)
        orch.offer_task_observation(task_id, run_id)
        return response(result.data, 202 if result.data.get("annahme") == "preparing" else 201)

    async def pending(request):
        actor = await B.actor(request)
        if actor is None:
            return error(401, "unauthorized")
        cp = request.app.get("control_plane")
        if cp is None:
            return error(503, "approval_unavailable")
        rows = await cp.store.list_pending(principal=actor.principal)
        # Dashboard-OK ist nur fuer diese zwei Auftragsarten autorisiert.
        return response({"approvals": [{key: row[key] for key in
            ("approval_id", "tool", "task", "action_digest", "expires_at")}
            for row in rows if row["tool"] in {"agent_task_research", "agent_task_build"}]})

    async def decide(request):
        actor = await B.actor(request, mutating=True)
        if actor is None:
            return error(401, "unauthorized")
        try:
            data = await body(request, {"action_digest", "decision"})
            if not isinstance(data["action_digest"], str) or data["decision"] not in ("APPROVE", "DENY"):
                raise ValueError("invalid_decision")
        except (ValueError, TypeError):
            return error(400, "invalid_decision")
        coordinator = request.app.get("coordinator")
        if coordinator is None:
            return error(503, "approval_unavailable")
        result, status = await coordinator.apply_dashboard_decision(actor=actor,
            approval_id=request.match_info["approval_id"], **data)
        return response(result) if result is not None else error(409, status)

    async def cost_policy(request):
        actor = await B.actor(request, mutating=request.method == "PUT")
        if actor is None:
            return error(401, "unauthorized")
        orch = orchestrator(request)
        if orch is None:
            return error(503, "agent_runtime_disabled")
        configured_owner = getattr(getattr(orch.router, "_mobile", None), "owner_principal", "")
        if not configured_owner or actor.principal != configured_owner:
            return error(403, "owner_configuration_required")
        if request.method == "PUT":
            try:
                data = await body(request, {"ask_threshold_cents", "client_request_id"})
                reference = "browser:" + actor.session_id + ":" + request_identifier(data["client_request_id"])
                orch.costs.set_default_threshold(data["ask_threshold_cents"], reference)
            except (ValueError, TypeError):
                return error(400, "invalid_cost_policy")
        return response(orch.costs.settings())

    async def cost_challenge(request):
        cp = request.app.get("control_plane")
        if cp is None or not request.secure or B.has_credentials(request):
            return error(401, "unauthorized")
        from solvio.security.mobile_approval.gateway import _authed_device
        device_id = await _authed_device(request)
        device = await cp.store.get_device(device_id) if device_id else None
        if device is None:
            return error(401, "unauthorized")
        try:
            data = await body(request, {"cost_approval"})
            value = canonical_cost_approval(data["cost_approval"])
            if value["task_id"] != request.match_info["task_id"]:
                raise ValueError("invalid_task_id")
        except (ValueError, TypeError, KeyError, UnicodeError):
            return error(400, "invalid_cost_approval")
        orch = orchestrator(request)
        if orch is None:
            return error(503, "agent_runtime_disabled")
        task = orch.ledger.get_task(value["task_id"])
        if task is None or task.created_principal != device["principal"]:
            return error(404, "unknown_task")
        result = await TaskCostApprovalProofService(cp).issue(device_id=device_id,
            transport_cred=request.headers.get("X-Transport-Cred", ""), task_body=value)
        if result is None:
            return error(401, "unauthorized")
        if orchestrator(request) is not orch or request.app.get("control_plane") is not cp:
            return error(409, "agent_runtime_changed")
        current_task = orch.ledger.get_task(task.task_id)
        if current_task is None or current_task.created_principal != device["principal"]:
            return error(404, "unknown_task")
        return response(result.as_dict())

    async def approve_cost(request):
        if not request.secure:
            return error(401, "unauthorized")
        browser = await B.actor(request, mutating=True)
        if browser is None and B.has_credentials(request):
            return error(401, "unauthorized")
        orch = orchestrator(request)
        if orch is None:
            return error(503, "agent_runtime_disabled")
        task_id = request.match_info["task_id"]
        proof_service = verified = cp = None
        try:
            if browser:
                principal = browser.principal
                task = orch.ledger.get_task(task_id)
                if task is None or task.created_principal != principal:
                    return error(404, "unknown_task")
                value = await body(request, {"max_total_cents", "client_request_id"})
                reference = "browser:" + browser.session_id + ":" + request_identifier(value["client_request_id"])
            else:
                cp = request.app.get("control_plane")
                if cp is None:
                    return error(401, "unauthorized")
                data = await body(request, {"cost_approval", "proof"})
                value = canonical_cost_approval(data["cost_approval"])
                if value["task_id"] != task_id:
                    raise ValueError("invalid_task_id")
                proof = data["proof"]
                if type(proof) is not dict or set(proof) != {"nonce", "assertion_b64"}:
                    raise ValueError("invalid_proof")
                proof_service = TaskCostApprovalProofService(cp)
                verified = await proof_service.verify(device_id=request.headers.get("X-Device-Id", ""),
                    nonce=proof["nonce"], task_body=value,
                    assertion=base64.b64decode(proof["assertion_b64"], validate=True))
                if verified is None:
                    return error(401, "unauthorized")
                principal = verified.principal
                reference = approval_reference(principal, value["client_request_id"])
        except (ValueError, TypeError, KeyError, UnicodeError):
            return error(409 if browser else 401, "invalid_cost_approval" if browser else "unauthorized")
        current = (await B.actor(request, mutating=True) == browser if browser else
                   await proof_service.still_current(verified))
        if not current or (not browser and request.app.get("control_plane") is not cp):
            return error(401, "unauthorized")
        if orchestrator(request) is not orch:
            return error(409, "agent_runtime_changed")
        task = orch.ledger.get_task(task_id)
        if task is None or task.created_principal != principal:
            return error(404, "unknown_task")
        try:
            # No await between the last authority/owner/runtime checks and the
            # existing synchronous ledger transaction. This starts/resumes nothing.
            costs = orch.costs.approve(task.task_id, max_total_cents=value["max_total_cents"], approval_ref=reference)
        except (ValueError, TypeError):
            return error(409, "invalid_cost_approval")
        if browser:
            return response(costs)
        return response({"task_id": task_id, "client_request_id": value["client_request_id"],
                         "max_total_cents": value["max_total_cents"], "costs": costs})

    app.add_routes([
        web.post(PREFIX + "/tasks/challenge", challenge),
        web.post(PREFIX + "/tasks", start),
        web.get(PREFIX + "/approvals", pending),
        web.post(PREFIX + "/approvals/{approval_id}/decision", decide),
        web.get(PREFIX + "/cost-policy", cost_policy),
        web.put(PREFIX + "/cost-policy", cost_policy),
        web.get(PREFIX + "/native-credits", native_credits),
        web.post(PREFIX + "/native-credits", native_credits),
        web.post(PREFIX + "/tasks/{task_id}/cost-approval/challenge", cost_challenge),
        web.post(PREFIX + "/tasks/{task_id}/cost-approval", approve_cost),
    ])
