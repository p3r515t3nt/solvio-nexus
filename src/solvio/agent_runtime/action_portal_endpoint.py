"""Private owner catalogue of existing, natively bound Portal sessions.

No login, navigation or page content is exposed by this read. Native session
ownership is fixed at open by the Core router; browser/app fields cannot set it.
"""
from __future__ import annotations

import math
import os
import re
import stat
import time
from aiohttp import web

from solvio.capabilities import task_action as TA
from solvio.portal.binding import BINDINGS, PortalBinding
from solvio.portal.build import expected_build


def _response(data, status=200):
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


def _native(orch):
    service = getattr(orch, "action_service", None)
    if not TA._original(service, TA.TaskServiceAction, TA._TASK_ACTION_SERVICE_METHODS):
        raise ValueError("portal_service_unavailable")
    portals = service.portals
    if (not TA._original(portals, TA.PortalCapabilities, TA._PORTAL_METHODS)
            or not TA._original(portals.client, TA.PortalClient, TA._PORTAL_CLIENT_METHODS)):
        raise ValueError("portal_client_unavailable")
    client = portals.client
    native = os.stat(client.socket_path, follow_symlinks=False)
    if not os.path.isabs(client.socket_path) or not stat.S_ISSOCK(native.st_mode):
        raise ValueError("portal_socket_unavailable")
    return (service, portals, client, os.path.realpath(client.socket_path),
            native.st_dev, native.st_ino, native.st_uid,
            os.path.realpath(client.repo_root), expected_build(client.repo_root))


def attach(app, orchestrator, guard):
    async def portals(request):
        from solvio.agent_runtime import portal_connection as PC
        principal = await guard(request)
        if principal is None:
            return _response({'error': 'unauthorized'}, 401)
        orch = orchestrator(request)
        if orch is None:
            return _response({'error': 'agent_runtime_disabled'}, 503)
        owner = getattr(getattr(orch.router, '_mobile', None), 'owner_principal', '')
        if not owner or principal != owner:
            return _response({'error': 'owner_configuration_required'}, 403)
        if request.query:
            return _response({'error': 'invalid_portal_catalogue'}, 400)
        try:
            service = orch.action_service
            snapshots = [PC.native(service, key) for key in sorted(BINDINGS)[:50]]
            await service.portals.client.verify_build()
            items = []
            for snapshot in snapshots:
                binding = BINDINGS[snapshot['portal_id']]
                available = service.portals.vault.has_existing(binding.credential_alias)
                if PC.native(service, binding.portal_id) != snapshot:
                    raise ValueError('portal_native_changed')
                items.append({'portal_id': binding.portal_id, 'label': binding.portal_id,
                    'origin': binding.login_origin, 'credential_available': available,
                    'account': PC.account_for(snapshot)})
            if await guard(request) != principal:
                return _response({'error': 'unauthorized'}, 401)
            if (orchestrator(request) is not orch or orch.action_service is not service
                    or getattr(getattr(orch.router, '_mobile', None), 'owner_principal', '') != owner
                    or any(PC.native(service, s['portal_id']) != s for s in snapshots)):
                raise ValueError('portal_native_changed')
            return _response({'portals': items, 'truncated': len(BINDINGS) > 50})
        except Exception:
            return _response({'error': 'portal_connections_unavailable'}, 503)

    async def sessions(request):
        principal = await guard(request)
        if principal is None:
            return _response({"error": "unauthorized"}, 401)
        orch = orchestrator(request)
        if orch is None:
            return _response({"error": "agent_runtime_disabled"}, 503)
        owner = getattr(getattr(orch.router, "_mobile", None), "owner_principal", "")
        if not owner or principal != owner:
            return _response({"error": "owner_configuration_required"}, 403)
        if (set(request.query) - {"limit"} or len(request.query.getall("limit", [])) > 1
                or not re.fullmatch(r"[1-9][0-9]?", request.query.get("limit", "50"))):
            return _response({"error": "invalid_portal_sessions"}, 400)
        limit = int(request.query.get("limit", "50"))
        if limit > 50:
            return _response({"error": "invalid_portal_sessions"}, 400)
        try:
            identity = _native(orch)
            portals, client = identity[1:3]
            reply = await client.list_sessions(principal, limit=limit)
            if _native(orch) != identity:
                raise ValueError("portal_changed")
            # Revoke while either native roundtrip was pending: return no data.
            if await guard(request) != principal:
                return _response({"error": "unauthorized"}, 401)
            await client.verify_build()
            if await guard(request) != principal:
                return _response({"error": "unauthorized"}, 401)
            if (orchestrator(request) is not orch or _native(orch) != identity
                    or getattr(getattr(orch.router, "_mobile", None), "owner_principal", "") != owner
                    or client.worker_build != identity[-1]):
                raise ValueError("portal_changed")
            items = reply.get("items")
            observed = reply.get("observed_at")
            if (reply.get("build") != identity[-1] or type(items) is not list
                    or len(items) > limit or type(reply.get("truncated")) is not bool
                    or type(observed) not in (int, float) or not math.isfinite(observed)
                    or not 0 <= time.time() - observed <= 60):
                raise ValueError("invalid_portal_catalogue")
            result, seen = [], set()
            for item in items:
                if type(item) is not dict or set(item) != {"session_id", "portal_id", "owner_principal",
                        "binding_digest", "authenticated", "expires_in_s"}:
                    raise ValueError("invalid_portal_session")
                binding = BINDINGS.get(item["portal_id"])
                if (type(binding) is not PortalBinding or item["owner_principal"] != principal
                        or not isinstance(item["session_id"], str)
                        or not re.fullmatch(r"ps-[0-9]+-[0-9]+", item["session_id"])
                        or item["session_id"] in seen
                        or item["binding_digest"] != TA._digest(binding.as_data())
                        or type(item["authenticated"]) is not bool
                        or type(item["expires_in_s"]) is not int or not 0 <= item["expires_in_s"] <= 3600):
                    raise ValueError("invalid_portal_binding")
                seen.add(item["session_id"])
                target = {"portal_id": binding.portal_id, "session_id": item["session_id"]}
                result.append({"account": TA.account_identity("portal", portals, **target),
                    "target": target, "label": binding.portal_id, "origin": binding.login_origin,
                    "authenticated": item["authenticated"], "expires_in_s": item["expires_in_s"]})
            return _response({"service": "portal", "items": result,
                "truncated": reply["truncated"], "observed_at": observed})
        except Exception:
            # Neither native exceptions nor page content become HTTP output.
            return _response({"error": "portal_sessions_unavailable"}, 503)

    app.add_routes([web.get("/v1/agent/action-portal-sessions", sessions),
                    web.get('/v1/agent/action-portals', portals)])
