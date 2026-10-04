"""Core-side background job model (STEP 21.2G).

The Mac Core is the AUTHORITATIVE control plane. It owns job intent, node choice,
idempotency, the remote job handle, the final status, and trust classification. The
node owns only operational execution. These local states include two the node never
has: LOCAL_PENDING_SUBMIT (persisted before we know the remote accepted) and
REMOTE_UNKNOWN (the remote lost the job — surfaced, never silently dropped).
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class LocalJobStatus(str, Enum):
    LOCAL_PENDING_SUBMIT = "LOCAL_PENDING_SUBMIT"  # persisted, remote-accept unknown
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REMOTE_UNKNOWN = "REMOTE_UNKNOWN"              # remote lost it (needs attention)
    RESULT_EXPIRED = "RESULT_EXPIRED"


LOCAL_TERMINAL: frozenset[LocalJobStatus] = frozenset({
    LocalJobStatus.SUCCEEDED, LocalJobStatus.FAILED,
    LocalJobStatus.CANCELLED, LocalJobStatus.RESULT_EXPIRED,
})

# node status string -> local status
NODE_TO_LOCAL: dict[str, LocalJobStatus] = {
    "QUEUED": LocalJobStatus.QUEUED,
    "RUNNING": LocalJobStatus.RUNNING,
    "SUCCEEDED": LocalJobStatus.SUCCEEDED,
    "FAILED": LocalJobStatus.FAILED,
    "CANCEL_REQUESTED": LocalJobStatus.CANCEL_REQUESTED,
    "CANCELLED": LocalJobStatus.CANCELLED,
    "RESULT_EXPIRED": LocalJobStatus.RESULT_EXPIRED,
}


class BackgroundJobSpec(BaseModel):
    """The Core's durable record of a background job. No web content is stored here;
    for public synthetic inputs the payload may be kept locally, separate from
    canonical memory."""
    model_config = ConfigDict(extra="ignore")

    local_job_id: str
    remote_job_id: str | None = None
    capability_id: str
    node_id: str
    privacy_class: str
    status: LocalJobStatus
    attempt_count: int = 0
    max_attempts: int = 3
    created_at: float
    submitted_at: float | None = None
    updated_at: float
    result_available: bool = False
    result_digest: str | None = None
    error_detail: str | None = None
