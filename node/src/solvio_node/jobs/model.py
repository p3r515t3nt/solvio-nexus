"""Background job model + state machine (node execution plane,.

The node owns ONLY operational state: a durable queue, running execution, retry
state, a temporary result, and temporary metadata. This is EPHEMERAL OPERATIONAL
STATE — never memory, standing intent, user preference, authority, or canonical
personal state. The Mac Core is the authoritative control plane.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "QUEUED"                      # accepted, waiting for a worker
    RUNNING = "RUNNING"                    # a worker is executing it
    SUCCEEDED = "SUCCEEDED"               # done, result available (until TTL)
    FAILED = "FAILED"                     # terminal failure (attempts exhausted / permanent)
    CANCEL_REQUESTED = "CANCEL_REQUESTED"  # cancel asked while RUNNING
    CANCELLED = "CANCELLED"               # terminal, cancelled
    RESULT_EXPIRED = "RESULT_EXPIRED"     # succeeded but result payload past TTL


# Terminal node states (no further node-side execution).
TERMINAL: frozenset[JobStatus] = frozenset({
    JobStatus.SUCCEEDED, JobStatus.FAILED,
    JobStatus.CANCELLED, JobStatus.RESULT_EXPIRED,
})

# Explicit allowed transitions. No silent jumps.
_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset({
        JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.QUEUED,  # QUEUED = requeue on recovery/retry
        JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED,
    }),
    JobStatus.CANCEL_REQUESTED: frozenset({
        JobStatus.CANCELLED, JobStatus.SUCCEEDED, JobStatus.FAILED,
    }),
    JobStatus.SUCCEEDED: frozenset({JobStatus.RESULT_EXPIRED}),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.RESULT_EXPIRED: frozenset(),
}


def can_transition(current: JobStatus, nxt: JobStatus) -> bool:
    return nxt in _TRANSITIONS.get(current, frozenset())


def hash_idempotency_key(client_identity: str, capability_id: str, key: str) -> str:
    """Store idempotency keys only hashed, scoped to client + capability."""
    h = hashlib.sha256()
    h.update(client_identity.encode("utf-8"))
    h.update(b"\x00")
    h.update(capability_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(key.encode("utf-8"))
    return h.hexdigest()


@dataclass
class Job:
    job_id: str
    client_identity: str
    capability_id: str
    privacy_class: str
    status: JobStatus
    attempt_count: int
    max_attempts: int
    created_at: float
    updated_at: float
    payload_size: int
    submitted_at: float | None = None
    result_size: int | None = None
    result_digest: str | None = None
    error_code: str | None = None
    result_available: bool = False
    result_expires_at: float | None = None
    metadata_expires_at: float | None = None

    def public_dict(self) -> dict:
        """Metadata only — never includes payload or result content."""
        return {
            "job_id": self.job_id,
            "capability_id": self.capability_id,
            "privacy_class": self.privacy_class,
            "status": self.status.value,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "submitted_at": self.submitted_at,
            "payload_size": self.payload_size,
            "result_size": self.result_size,
            "result_digest": self.result_digest,
            "error_code": self.error_code,
            "result_available": self.result_available,
        }
