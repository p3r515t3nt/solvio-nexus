"""Packaged, private dashboard on the existing HTTPS gateway.

This module reads existing runtime objects. Opening it neither starts an agent
nor probes an external provider. Unknown or old health remains visible as such.
"""
from pathlib import Path
import asyncio
import time

from aiohttp import web

from .auth import OWNER, owner_browser

ASSETS = Path(__file__).with_name("assets")
FILES = {"app.js": "text/javascript", "presence.js": "text/javascript",
         "chat.js": "text/javascript", "canonical.js": "text/javascript",
         "action-composer.js": "text/javascript",
         "portal-action.js": "text/javascript",
         "action-intent.js": "text/javascript",
         "window-share.js": "text/javascript",
         "browser-voice.js": "text/javascript", "voice-worklet.js": "text/javascript",
         "app.css": "text/css", "solvio-mascot.svg": "image/svg+xml",
         "solvio-glasses.svg": "image/svg+xml", "solvio-icon.svg": "image/svg+xml"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; "
       "frame-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; object-src 'none'")


def response(data, status=200):
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


@web.middleware
async def privacy_headers(request, handler):
    result = await handler(request)
    if request.path.startswith(("/dashboard", "/v1/")):
        result.headers.update({"Cache-Control": "no-store", "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
    if request.path.startswith("/dashboard"):
        result.headers.update({"Content-Security-Policy": CSP,
            "Permissions-Policy": ("microphone=(self), camera=(), geolocation=()"
                if request.path == "/dashboard/" else "microphone=(), camera=(), geolocation=()"),
            "X-Frame-Options": "DENY"})
        if request.path.startswith('/dashboard/hermes/'):
            from .hermes_view import CSP as native_csp
            result.headers['Content-Security-Policy'] = native_csp
            result.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return result


def attach(app, *, owner_principal, environment="private", voice_server_provider=None, router_provider=None):
    if not owner_principal:
        raise ValueError("configured dashboard owner required")
    app[OWNER] = owner_principal
    from .commands import attach as attach_commands
    attach_commands(app, router_provider)
    from .hermes_view import attach as attach_hermes
    attach_hermes(app)
    from .window_share import attach as attach_windows
    attach_windows(app)
    app.middlewares.append(privacy_headers)

    async def page(request):
        if not request.secure:
            return response({"error": "https_required"}, 403)
        return web.Response(body=(ASSETS / "index.html").read_bytes(), content_type="text/html")

    async def redirect(request):
        return web.HTTPSeeOther("/dashboard/")

    async def asset(request):
        name = request.match_info["name"]
        if not request.secure or name not in FILES:
            return response({"error": "not_found"}, 404)
        return web.Response(body=(ASSETS / name).read_bytes(), content_type=FILES[name])

    async def state(request):
        if await owner_browser(request) is None:
            return response({"error": "unauthorized"}, 401)
        # Lazy: the gateway starts before the runtime. No cached None at attach.
        provider = app.get("agent_runtime_provider")
        runtime = provider() if callable(provider) else app.get("agent_runtime")
        center = app.get("control_center")
        board = getattr(center, "board", None)
        workspaces = getattr(runtime, "workspaces", None)
        repositories = workspaces.configured_repositories() if workspaces else ()
        components = [c.as_dict() for c in board.known()] if board else None
        observations = getattr(runtime, 'memory_observations', None)
        activities = getattr(observations, 'activities', None)
        learning = {'state': 'unavailable'}
        if activities is not None:
            learning = await asyncio.to_thread(activities.learning_view, app[OWNER])
            learning['state'] = 'enabled' if getattr(getattr(observations, 'adaptive', None), 'enabled', False) else 'disabled'
        return response({"stand": time.time(), "environment": environment,
            "runtime": "available" if runtime else "unavailable",
            "memory": "available" if getattr(app.get("memory_service"), "semantic", None) else "unavailable",
            "inbox": "available" if getattr(center, "store", None) else "unavailable",
            "cost_controls": "configured" if getattr(runtime, "cost_quote_adapter", None) else "unavailable",
            "repositories": [{"path": p, "name": Path(p).name} for p in repositories],
            "components": components,
            "learning": learning,
            "health_source": "existing_health_board_no_probe",
            "quota": {"state": "unknown", "percent_remaining": None,
                      "on_limit": "owner_decision_required"}})

    async def audio(request):
        if await owner_browser(request) is None:
            return response({"error": "unauthorized"}, 401)
        server = voice_server_provider() if voice_server_provider else None
        observer = getattr(server, "audio_observations", None)
        if observer is None:
            return response({"error": "audio_observations_unavailable"}, 503)
        health = getattr(server, "satellite_health", None)
        return response(observer.snapshot(health.known() if health else ()))

    async def resume_learning(request):
        actor = await owner_browser(request, mutating=True)
        if actor is None:
            return response({'error': 'unauthorized'}, 401)
        provider = app.get('agent_runtime_provider')
        runtime = provider() if callable(provider) else app.get('agent_runtime')
        observations = getattr(runtime, 'memory_observations', None)
        if observations is None:
            return response({'error': 'observation_resume_unavailable'}, 503)
        try:
            activity_id = request.match_info['activity_id']
            processor = app.get('conversation_processor')
            if processor is not None and observations.activities.held_chat(activity_id, actor.principal):
                await processor.resume_learning(activity_id, actor.principal)
            else:
                observations.resume_task(activity_id, actor.principal)
        except ValueError as exc:
            return response({'error': str(exc)}, 409)
        return response({'state': 'queued'}, 202)

    app.router.add_get("/", redirect)
    app.router.add_get("/dashboard", redirect)
    app.router.add_get("/dashboard/", page)
    app.router.add_get("/dashboard/assets/{name}", asset)
    app.router.add_get("/v1/dashboard/state", state)
    app.router.add_get("/v1/control/audio", audio)
    app.router.add_post('/v1/dashboard/learning/{activity_id}/resume', resume_learning)
