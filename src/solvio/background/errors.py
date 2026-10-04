"""Errors for the Core background job subsystem (STEP 21.2G)."""
from __future__ import annotations


class BackgroundError(Exception):
    """Base for background-job errors."""


class JobNotFound(BackgroundError):
    pass


class SubmitUncertain(BackgroundError):
    """The node was unreachable during submit; the job stays LOCAL_PENDING_SUBMIT and
    is safe to resend with the same idempotency key (reconcile handles it)."""


class ResultNotReady(BackgroundError):
    pass


class ResultDigestMismatch(BackgroundError):
    """Retrieved payload digest != metadata digest (integrity/diagnostic signal)."""


class LocalStoreCorrupt(BackgroundError):
    """The local ledger failed its integrity check — never fabricate jobs."""
