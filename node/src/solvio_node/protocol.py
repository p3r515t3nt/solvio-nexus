"""SOLVIO Node Protocol V1 (request/response envelope).

Framework-neutral, transport-agnostic. The HTTP layer (server.py) is only one
possible carrier. Payload CONTENT is never logged (privacy.py/logging.py enforce it).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1


class Status(str, Enum):
    OK = "ok"
    ERROR = "error"


class ErrorCode(str, Enum):
    PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"
    UNKNOWN_CAPABILITY = "UNKNOWN_CAPABILITY"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    RESOURCE_BUSY = "RESOURCE_BUSY"
    TIMEOUT = "TIMEOUT"
    CAPABILITY_ERROR = "CAPABILITY_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"


def new_request_id() -> str:
    return uuid.uuid4().hex


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class NodeRequest(BaseModel):
    """A capability invocation request."""
    model_config = ConfigDict(extra="forbid")

    protocol_version: int = PROTOCOL_VERSION
    request_id: str = Field(default_factory=new_request_id)
    target_node_id: str | None = None          # expected node identity (optional)
    capability: str
    operation: str = "invoke"
    timestamp: str = Field(default_factory=utcnow_iso)
    payload: dict[str, Any] = Field(default_factory=dict)


class NodeResponse(BaseModel):
    """A capability invocation response. `error_message` MUST stay payload-free."""
    model_config = ConfigDict(extra="forbid")

    protocol_version: int = PROTOCOL_VERSION
    request_id: str
    node_id: str
    capability: str
    status: Status
    result: dict[str, Any] | None = None
    duration_ms: float = 0.0
    error_code: ErrorCode | None = None
    error_message: str | None = None

    @classmethod
    def error(cls, *, request_id: str, node_id: str, capability: str,
              code: ErrorCode, message: str, duration_ms: float = 0.0) -> "NodeResponse":
        return cls(request_id=request_id, node_id=node_id, capability=capability,
                   status=Status.ERROR, error_code=code, error_message=message,
                   duration_ms=round(duration_ms, 3))

    @classmethod
    def ok(cls, *, request_id: str, node_id: str, capability: str,
           result: dict[str, Any], duration_ms: float = 0.0) -> "NodeResponse":
        return cls(request_id=request_id, node_id=node_id, capability=capability,
                   status=Status.OK, result=result, duration_ms=round(duration_ms, 3))
