"""HTTP transport for the node (aiohttp). Binds to the WireGuard overlay IP only,
never the public interface. mTLS is required by the SSL context.

Endpoints:
    GET  /v1/health
    GET  /v1/capabilities
    GET  /v1/capabilities/{id}
    POST /v1/capabilities/{id}/invoke   (registered capabilities only)
    POST /v1/jobs                        (durable background jobs, if enabled)
    GET  /v1/jobs/{id}
    GET  /v1/jobs/{id}/result
    POST /v1/jobs/{id}/cancel

There is intentionally NO endpoint that runs arbitrary strings, shells, or scripts.
"""
from __future__ import annotations

import json

from aiohttp import web
from pydantic import ValidationError

from solvio_node import PROTOCOL_VERSION, __version__
from solvio_node.config import NodeConfig
from solvio_node.identity import NodeIdentity
from solvio_node.jobs.api import register_job_routes
from solvio_node.jobs.store import JobStore
from solvio_node.jobs.worker import JobWorkerPool
from solvio_node.limits import LimitGuard
from solvio_node.logging import build_logger
from solvio_node.protocol import ErrorCode, NodeRequest, NodeResponse
from solvio_node.registry import build_registry
from solvio_node.runtime import Runtime
from solvio_node.security import server_ssl_context

_HTTP = {
    ErrorCode.PROTOCOL_MISMATCH: 400, ErrorCode.UNKNOWN_CAPABILITY: 404,
    ErrorCode.VALIDATION_ERROR: 400, ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.RESOURCE_BUSY: 503, ErrorCode.TIMEOUT: 504,
    ErrorCode.CAPABILITY_ERROR: 500, ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.UNAUTHENTICATED: 401,
}


def _resp(node: NodeResponse) -> web.Response:
    status = 200 if node.error_code is None else _HTTP.get(node.error_code, 500)
    return web.json_response(node.model_dump(mode="json", exclude_none=True), status=status)


def build_app(config: NodeConfig, *, registry=None) -> web.Application:
    identity = NodeIdentity(node_id=config.node_id)
    registry = registry if registry is not None else build_registry(config.enabled_capabilities)
    limits = LimitGuard(config.limits)
    logger = build_logger(config.log_path, config.node_id)
    runtime = Runtime(identity, registry, limits, logger)

    async def health(_req: web.Request) -> web.Response:
        body = {"node_id": identity.node_id, "node_instance_id": identity.node_instance_id,
                "protocol_version": PROTOCOL_VERSION, "version": __version__,
                "status": "ok", "capabilities": registry.ids()}
        cap = registry.get("system.health")
        if cap is not None:
            out = await cap.invoke(cap.input_model())
            body["system"] = out.model_dump(mode="json")
        return web.json_response(body)

    async def capabilities(_req: web.Request) -> web.Response:
        return web.json_response({"node_id": identity.node_id,
                                  "protocol_version": PROTOCOL_VERSION,
                                  "capabilities": registry.descriptors()})

    async def capability_one(req: web.Request) -> web.Response:
        cap = registry.get(req.match_info["id"])
        if cap is None:
            return web.json_response({"error_code": "UNKNOWN_CAPABILITY"}, status=404)
        return web.json_response(cap.descriptor())

    async def invoke(req: web.Request) -> web.Response:
        cap_id = req.match_info["id"]
        raw = await req.read()                       # client_max_size caps this
        try:
            limits.check_size(len(raw))
        except Exception:  # RequestTooLarge
            return _resp(NodeResponse.error(request_id="-", node_id=identity.node_id,
                         capability=cap_id, code=ErrorCode.PAYLOAD_TOO_LARGE,
                         message="request too large"))
        try:
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError
            data["capability"] = cap_id              # URL is authoritative
            request = NodeRequest.model_validate(data)
        except (ValueError, ValidationError):
            return _resp(NodeResponse.error(request_id="-", node_id=identity.node_id,
                         capability=cap_id, code=ErrorCode.VALIDATION_ERROR,
                         message="malformed request envelope"))
        node_resp = await runtime.execute(request, payload_size=len(raw),
                                          peer=req.remote)
        return _resp(node_resp)

    app = web.Application(client_max_size=config.limits.max_request_bytes + 4096)
    app.router.add_get("/v1/health", health)
    app.router.add_get("/v1/capabilities", capabilities)
    app.router.add_get("/v1/capabilities/{id}", capability_one)
    app.router.add_post("/v1/capabilities/{id}/invoke", invoke)
    app["logger"] = logger

    # --- durable background jobs — optional, opt-in ---
    if config.jobs.enabled:
        store = JobStore(config.jobs.db_path, result_ttl_s=config.jobs.result_ttl_s,
                         metadata_ttl_s=config.jobs.metadata_ttl_s)
        worker = JobWorkerPool(store, registry, logger, workers=config.jobs.workers)
        register_job_routes(app, store=store, worker=worker, registry=registry,
                            logger=logger, max_queued=config.jobs.max_queued)
        app["job_store"] = store
        app["job_worker"] = worker

        async def _start_jobs(_a: web.Application) -> None:
            await store.open()
            await worker.start()

        async def _stop_jobs(_a: web.Application) -> None:
            await worker.stop()
            await store.close()

        app.on_startup.append(_start_jobs)
        app.on_cleanup.append(_stop_jobs)

    logger.event("startup", detail=config.node_id, port=config.listen_port,
                 count=len(registry.ids()))
    return app


def main() -> None:
    config = NodeConfig.load()
    app = build_app(config)
    ssl_ctx = server_ssl_context(config.tls)
    web.run_app(app, host=config.listen_host, port=config.listen_port,
                ssl_context=ssl_ctx, print=None)


if __name__ == "__main__":
    main()
