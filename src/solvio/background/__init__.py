"""Core background-job control plane (STEP 21.2G).

MAC OWNS INTENT. NODE OWNS EXECUTION. This package holds the authoritative durable
record of background jobs, crash-safe submission, status reconciliation, and result
retrieval with digest verification. Trust classification stays with the Core
(research.* results are UNTRUSTED_WEB). It is NOT wired into Voice/Realtime, there is
no scheduler and no always-on poller — refresh()/reconcile() are explicit.
"""
from solvio.background.errors import (
    BackgroundError,
    JobNotFound,
    LocalStoreCorrupt,
    ResultDigestMismatch,
    ResultNotReady,
    SubmitUncertain,
)
from solvio.background.manager import BackgroundManager
from solvio.background.models import (
    LOCAL_TERMINAL,
    NODE_TO_LOCAL,
    BackgroundJobSpec,
    LocalJobStatus,
)
from solvio.background.reconciliation import next_reconcile_step
from solvio.background.store import LocalJobStore

__all__ = [
    "BackgroundError", "JobNotFound", "LocalStoreCorrupt", "ResultDigestMismatch",
    "ResultNotReady", "SubmitUncertain",
    "BackgroundManager", "LocalJobStore",
    "BackgroundJobSpec", "LocalJobStatus", "LOCAL_TERMINAL", "NODE_TO_LOCAL",
    "next_reconcile_step",
]
