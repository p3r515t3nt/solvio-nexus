"""Reconciliation decisions for the Core control plane (STEP 21.2G).

After a Core restart the manager finds its non-terminal local jobs and re-establishes
the remote truth. The per-job decision is a pure function of the local status:

  * LOCAL_PENDING_SUBMIT  -> resend with the SAME idempotency key (the node
    deduplicates, so an uncertain submit never double-executes).
  * anything else non-terminal -> refresh the remote status (which maps a missing
    remote job to REMOTE_UNKNOWN — surfaced, never silently deleted).
"""
from __future__ import annotations

from solvio.background.models import LocalJobStatus


def next_reconcile_step(status: LocalJobStatus) -> str:
    """Return "resend" or "refresh" for a non-terminal local job."""
    if status == LocalJobStatus.LOCAL_PENDING_SUBMIT:
        return "resend"
    return "refresh"
