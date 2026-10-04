"""Explicit owner account selection for the same unstarted native action.

Reading this view opens no provider session. A changed credential binding is
an authenticated, immutable amendment; ordinary resume cannot create it.
"""
from aiohttp import web

from solvio.agent_runtime.task_authority import VerifiedTaskReceipt
from solvio.agent_runtime.task_start_service import request_identifier
from solvio.security.mobile_approval import browser_sessions as B


def rebind_view(orch, run_id):
    from solvio.agent_runtime import action_account_rebinding as AR
    from solvio.capabilities import task_action as TA
    pending = AR.pending_rebind(orch.ledger, run_id)
    if pending is None:
        return None
    service = getattr(orch, "action_service", None)
    choices = []
    if TA._original(service, TA.TaskServiceAction, TA._TASK_ACTION_SERVICE_METHODS):
        for row in service.accounts():
            if row["service"] != pending["service"]:
                continue
            target = pending["target"]
            expected_resource = (target.get("calendar_id") if row["service"] == "calendar" else
                                 target.get("mailbox") if row["service"] == "gmail" else "configured_home")
            if row["resource"] == expected_resource:
                choices.append(row)
    return dict(pending, choices=choices,
        selection_required=bool(choices) and all(row["account"] != pending["current_account"] for row in choices))


def attach(app, orchestrator):
    async def change_account(request):
        actor = await B.actor(request, mutating=True)
        if actor is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        orch = orchestrator(request)
        if orch is None:
            return web.json_response({"error": "agent_runtime_disabled"}, status=503)
        run_id = request.match_info["run_id"]
        run = orch.ledger.get_run(run_id)
        task = orch.ledger.get_task(run.task_id) if run else None
        configured_owner = getattr(getattr(orch.router, "_mobile", None), "owner_principal", "")
        if task is None or task.created_principal != actor.principal or actor.principal != configured_owner:
            return web.json_response({"error": "unknown_run"}, status=404)
        try:
            from solvio.agent_runtime.task_endpoint import body
            data = await body(request, {"action_id", "new_account", "expected_account",
                                       "expected_receipt_digest", "client_request_id"})
            if any(type(value) is not str for value in data.values()):
                raise ValueError("invalid_account_selection")
            request_id = request_identifier(data.pop("client_request_id"))
            from solvio.agent_runtime import action_account_rebinding as AR
            # The module first recognizes a replay of this exact authenticated
            # decision. Its native account check is passed as a fixed list of
            # currently configured choices, never caller-supplied authority.
            from solvio.capabilities import task_action as TA
            service = getattr(orch, "action_service", None)
            if not TA._original(service, TA.TaskServiceAction, TA._TASK_ACTION_SERVICE_METHODS):
                raise ValueError("action_native_client_unavailable")
            pending = rebind_view(orch, run_id)
            choices = pending["choices"] if pending is not None else service.accounts()
            if data["new_account"] not in {row["account"] for row in choices}:
                raise ValueError("action_account_not_configured")
            decision = AR.rebind_account(orch.ledger, run_id=run_id,
                receipt=VerifiedTaskReceipt("dashboard_session", "browser:" + actor.session_id + ":" + request_id,
                                            actor.principal), **data)
        except (TypeError, ValueError):
            return web.json_response({"error": "account_selection_changed"}, status=409,
                                     headers={"Cache-Control": "no-store"})
        # Both writes are independently recoverable. Losing this response is
        # not permission for another action; replay locates the same amendment.
        # An exact POST replay acknowledges its original decision, not a new
        # authentication failure or later provider wait. A response lost after
        # the amendment but before resume still has this same durable proof.
        current = AR.pending_rebind(orch.ledger, run_id)
        if (current is not None and current["action_id"] == data["action_id"]
                and current["receipt_digest"] == data["expected_receipt_digest"]
                and current["current_account"] == decision.account):
            await orch.resume(run_id)
        return web.json_response({"id": run_id, "account_bound": True,
            "state": orch.ledger.get_run(run_id).state}, headers={"Cache-Control": "no-store"})

    app.add_routes([web.post('/v1/agent/runs/{run_id}/action-account', change_account)])
