"""Node job control-plane HTTP API, identity hardened 21.2G.1).

Endpoints (mTLS-only, like the rest of the node):
    POST /v1/jobs                 submit (idempotent) a background job
    GET  /v1/jobs/{id}            job metadata (owner only)
    GET  /v1/jobs/{id}/result     job result (owner only)
    POST /v1/jobs/{id}/cancel     request cancellation (owner only)

There is NO generic action API: a job may reference ONLY a registered capability
whose supports_background is True. The node never decides authority; the Core does.

Client identity (job ownership) comes from a CA-signed URI SAN
(spiffe://solvio/client/<name>) — never a CN, IP, header, or fingerprint. See
solvio_node.jobs.identity.
"""
from __future__ import annotations

from aiohttp import web
from pydantic import ValidationError

from solvio_node.jobs.identity import client_identity

ALLOWED_PRIVACY = {"REMOTE_CONTROLLED_ALLOWED"}
DEFAULT_MAX_ATTEMPTS = 3
MAX_MAX_ATTEMPTS = 5


def _err(code: str, http: int) -> web.Response:
    return web.json_response({"error_code": code}, status=http)


def register_job_routes(app: web.Application, *, store, worker, registry, logger,
                        max_queued: int) -> None:

    async def submit(req: web.Request) -> web.Response:
        ident = client_identity(req)
        if not ident:
            return _err("UNAUTHENTICATED", 401)
        try:
            body = await req.json()
            if not isinstance(body, dict):
                raise ValueError
        except Exception:  # noqa: BLE001
            return _err("VALIDATION_ERROR", 400)

        cap_id = body.get("capability_id")
        payload = body.get("payload")
        privacy = body.get("privacy_class")
        idem = body.get("idempotency_key")
        if (not isinstance(cap_id, str) or not isinstance(payload, dict)
                or not isinstance(idem, str) or not idem):
            return _err("VALIDATION_ERROR", 400)

        cap = registry.get(cap_id)
        if cap is None:
            return _err("UNKNOWN_CAPABILITY", 404)
        if not getattr(cap, "supports_background", False):
            return _err("CAPABILITY_NOT_BACKGROUND", 400)
        if privacy not in ALLOWED_PRIVACY:
            return _err("PRIVACY_REJECTED", 403)
        try:
            cap.input_model.model_validate(payload)
        except ValidationError:
            return _err("VALIDATION_ERROR", 400)

        try:
            max_attempts = max(1, min(MAX_MAX_ATTEMPTS,
                                      int(body.get("max_attempts", DEFAULT_MAX_ATTEMPTS))))
        except (TypeError, ValueError):
            max_attempts = DEFAULT_MAX_ATTEMPTS

        if await store.count_active() >= max_queued:
            return _err("QUEUE_FULL", 503)

        job_id, created = await store.create(
            client_identity=ident, capability_id=cap_id, privacy_class=privacy,
            idempotency_key=idem, payload=payload, max_attempts=max_attempts)
        if created:
            worker.notify()
        try:
            logger.event("job_submit", job_id=job_id, capability=cap_id,
                         status="QUEUED" if created else "DEDUP")
        except Exception:  # noqa: BLE001
            pass
        return web.json_response(
            {"job_id": job_id, "status": "QUEUED", "duplicate": not created},
            status=201 if created else 200)

    async def get_job(req: web.Request) -> web.Response:
        ident = client_identity(req)
        if not ident:
            return _err("UNAUTHENTICATED", 401)
        job = await store.get(req.match_info["id"], ident)
        if job is None:
            return _err("NOT_FOUND", 404)   # not found OR not owner
        return web.json_response(job.public_dict())

    async def get_result(req: web.Request) -> web.Response:
        ident = client_identity(req)
        if not ident:
            return _err("UNAUTHENTICATED", 401)
        res = await store.get_result(req.match_info["id"], ident)
        if res is None:
            return _err("NOT_FOUND", 404)
        return web.json_response(res)

    async def cancel(req: web.Request) -> web.Response:
        ident = client_identity(req)
        if not ident:
            return _err("UNAUTHENTICATED", 401)
        status = await store.request_cancel(req.match_info["id"], ident)
        if status is None:
            return _err("NOT_FOUND", 404)
        worker.notify()
        return web.json_response({"job_id": req.match_info["id"], "status": status})

    app.router.add_post("/v1/jobs", submit)
    app.router.add_get("/v1/jobs/{id}", get_job)
    app.router.add_get("/v1/jobs/{id}/result", get_result)
    app.router.add_post("/v1/jobs/{id}/cancel", cancel)
