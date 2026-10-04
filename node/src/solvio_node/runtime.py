"""Invocation runtime — the single, narrow path from request to result.

Order: protocol check -> node-target check -> capability lookup -> input schema
validation -> limit slot -> bounded-timeout execution -> output validation.

There is no branch anywhere that executes arbitrary strings. Error messages are
payload-free (only exception TYPE names), so request content can never leak via
errors or logs.
"""
from __future__ import annotations

import asyncio
import json
import time

from pydantic import ValidationError

from solvio_node.identity import NodeIdentity
from solvio_node.limits import BusyError, LimitGuard, RequestTooLarge
from solvio_node.logging import NodeLogger
from solvio_node.protocol import ErrorCode, NodeRequest, NodeResponse, PROTOCOL_VERSION
from solvio_node.registry import CapabilityRegistry


class Runtime:
    def __init__(self, identity: NodeIdentity, registry: CapabilityRegistry,
                 limits: LimitGuard, logger: NodeLogger) -> None:
        self.identity = identity
        self.registry = registry
        self.limits = limits
        self.log = logger

    async def execute(self, request: NodeRequest, *, payload_size: int,
                      peer: str | None = None) -> NodeResponse:
        nid = self.identity.node_id
        rid = request.request_id
        started = time.perf_counter()

        def err(code: ErrorCode, msg: str) -> NodeResponse:
            dur = (time.perf_counter() - started) * 1000
            self.log.request(request_id=rid, capability=request.capability,
                             status="error", duration_ms=dur, payload_size=payload_size,
                             result_size=0, error_code=code.value, peer=peer)
            return NodeResponse.error(request_id=rid, node_id=nid,
                                      capability=request.capability, code=code,
                                      message=msg, duration_ms=dur)

        if request.protocol_version != PROTOCOL_VERSION:
            return err(ErrorCode.PROTOCOL_MISMATCH,
                       f"expected protocol {PROTOCOL_VERSION}")
        if request.target_node_id and request.target_node_id != nid:
            return err(ErrorCode.VALIDATION_ERROR, "node target mismatch")

        cap = self.registry.get(request.capability)
        if cap is None:
            return err(ErrorCode.UNKNOWN_CAPABILITY, "no such capability")

        try:
            data = cap.input_model.model_validate(request.payload)
        except ValidationError:
            return err(ErrorCode.VALIDATION_ERROR, "input schema validation failed")

        try:
            async with self.limits.slot():
                out = await asyncio.wait_for(cap.invoke(data), timeout=cap.timeout_s)
        except BusyError:
            return err(ErrorCode.RESOURCE_BUSY, "node overloaded")
        except asyncio.TimeoutError:
            return err(ErrorCode.TIMEOUT, "capability timed out")
        except Exception as exc:  # noqa: BLE001 - never leak payload; type name only
            return err(ErrorCode.CAPABILITY_ERROR, type(exc).__name__)

        try:
            result = cap.output_model.model_validate(out).model_dump(mode="json")
        except ValidationError:
            return err(ErrorCode.INTERNAL_ERROR, "output schema validation failed")

        dur = (time.perf_counter() - started) * 1000
        result_size = len(json.dumps(result, separators=(",", ":")))
        self.log.request(request_id=rid, capability=request.capability, status="ok",
                         duration_ms=dur, payload_size=payload_size,
                         result_size=result_size, peer=peer)
        return NodeResponse.ok(request_id=rid, node_id=nid,
                               capability=request.capability, result=result,
                               duration_ms=dur)


def raise_size(guard: LimitGuard, nbytes: int) -> None:
    """Helper for the transport layer to enforce the body cap early."""
    try:
        guard.check_size(nbytes)
    except RequestTooLarge:
        raise
