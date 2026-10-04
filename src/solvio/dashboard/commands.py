"""Authenticated browser memory commands; shared handlers and durable journal."""
import re

from aiohttp import web

from solvio.capabilities.browser_memory_command import BrowserMemoryCommand, read_status, valid_arguments
from solvio.capabilities.policy import OriginClass
from solvio.contracts.trust import TrustContext, TrustLevel
from .auth import owner_browser


def attach(app, router_provider):
    def response(data, status=200):
        return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})

    async def create(request):
        actor = await owner_browser(request, mutating=True)
        if actor is None:
            return response({"error": "unauthorized"}, 401)
        try:
            body = await request.json()
        except (ValueError, web.HTTPRequestEntityTooLarge):
            return response({"error": "invalid_memory_command"}, 400)
        if (type(body) is not dict or set(body) != {"capability", "arguments", "client_request_id"}
                or not isinstance(body["capability"], str)
                or not isinstance(body["client_request_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", body["client_request_id"])
                or not valid_arguments(body["capability"], body["arguments"])):
            return response({"error": "invalid_memory_command"}, 400)
        router = router_provider() if router_provider else None
        if router is None:
            return response({"error": "memory_command_unavailable"}, 503)
        command = BrowserMemoryCommand.bind(actor, body["client_request_id"],
                                           body["capability"], body["arguments"])
        result = await router.execute(body["capability"], body["arguments"],
            trust=TrustContext(TrustLevel.USER_DIRECT, user_authorized=True),
            origin=OriginClass.TRUSTED_DASHBOARD, principal=actor.principal, browser_command=command)
        if result.data and result.data.get("command_id"):
            return response(result.data, 202 if result.data["state"] in {"pending", "outcome_unconfirmed"} else 200)
        return response({"error": result.reason or result.outcome.value},
                        409 if result.reason == "memory_command_conflict" else 403)

    async def status(request):
        actor = await owner_browser(request)
        if actor is None:
            return response({"error": "unauthorized"}, 401)
        cp = app.get("control_plane")
        if cp is None:
            return response({"error": "memory_command_unavailable"}, 503)
        data = await read_status(cp, request.match_info["command_id"], actor.principal)
        return response(data) if data else response({"error": "unknown_memory_command"}, 404)

    async def recent(request):
        actor = await owner_browser(request)
        if actor is None:
            return response({"error": "unauthorized"}, 401)
        cp = app.get("control_plane")
        if cp is None:
            return response({"error": "memory_command_unavailable"}, 503)
        ids = await cp.store.recent_browser_memory_commands(actor.principal, cp.core_instance_id)
        values = [await read_status(cp, key, actor.principal) for key in ids]
        return response({"commands": [value for value in values if value is not None]})

    app.router.add_post("/v1/dashboard/memory-commands", create)
    app.router.add_get("/v1/dashboard/memory-commands", recent)
    app.router.add_get("/v1/dashboard/memory-commands/{command_id}", status)
