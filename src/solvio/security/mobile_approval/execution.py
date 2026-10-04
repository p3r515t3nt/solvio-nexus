"""P1C/F4 — execution semantics, stable identity and the recovery decision.

WHY THIS EXISTS. Measured against the released P1B tree (8353b1f), a crash after the
executor's side effect and a crash before it produced a **byte-identical** database state:

    B  crash after claim, before the effect   -> state=EXECUTING, 0 external effects
    C  effect succeeded, crash before persist -> state=EXECUTING, 1 external effect

Both left `state_APPROVED, state_EXECUTING` in the audit and nothing else. SOLVIO could not
tell "definitely did not happen" from "may have happened", so the only safe behaviour was to
wedge: after a restart `execute_approved` refused with `not_approved` forever. And a fifth
window was worse than wedging — an executor that raised AFTER its side effect was recorded as
`FAILED`, which reads as "no effect happened". That is a false statement about the world.

WHAT IS AND IS NOT CLAIMED. SOLVIO does **not** claim exactly-once for arbitrary external
side effects. It cannot: between "the adapter was called" and "the adapter's answer was
durably stored" there is a window that no local transaction can close. What it claims is:

  * the truth of that window is durably RECORDED before the adapter is called, so after a
    crash the state is `external effect MAY have happened` rather than a guess; and
  * nothing is retried automatically unless the capability's DECLARED semantics say the far
    side deduplicates the repeat or can be asked about it. SOLVIO does not prove that; it
    relies on a claim about a system it does not control. If that claim is wrong, the retry
    is wrong — which is why anything unclassified defaults to NON_IDEMPOTENT_WRITE.

Fail-safe before convenience.
"""
from __future__ import annotations

import hashlib

# ---------------------------------------------------------------------------
# Execution semantics. Declared SERVER-SIDE per capability — never by the model, and never
# carried in the approval request, because a request is something the model can influence.
# ---------------------------------------------------------------------------
READ_ONLY = "READ_ONLY"
#: No external mutation at all. Re-running is safe by definition.

IDEMPOTENT_WRITE = "IDEMPOTENT_WRITE"
#: The same stable idempotency identity may be re-sent without a second logical effect.
#: Requires the remote side to deduplicate on that identity — SOLVIO asserting it is not
#: enough, which is why this is a property of the capability, not of the call.

RECONCILABLE_WRITE = "RECONCILABLE_WRITE"
#: After an ambiguous outcome SOLVIO can ASK whether the effect already happened, keyed by
#: the execution identity, and get a trustworthy answer.

NON_IDEMPOTENT_WRITE = "NON_IDEMPOTENT_WRITE"
#: A retry after an unknown outcome could duplicate the effect. There is no safe automatic
#: recovery. This is the DEFAULT for anything not explicitly declared otherwise.

ALL_SEMANTICS = (READ_ONLY, IDEMPOTENT_WRITE, RECONCILABLE_WRITE, NON_IDEMPOTENT_WRITE)

#: Capability -> semantics. Adding a capability without an entry gets the fail-safe default,
#: which is deliberate: forgetting to classify must cost safety, not silence.
CAPABILITY_SEMANTICS: dict[str, str] = {
    "codex_task": NON_IDEMPOTENT_WRITE,
    "codex_modify": NON_IDEMPOTENT_WRITE,
}


def semantics_for(capability: str) -> str:
    """The server's declaration for a capability. Unknown capability -> fail-safe."""
    return CAPABILITY_SEMANTICS.get(capability or "", NON_IDEMPOTENT_WRITE)


# ---------------------------------------------------------------------------
# Attempt status — the durable journal's state machine
# ---------------------------------------------------------------------------
CLAIMED = "CLAIMED"
#: The claim committed. The external adapter has NOT been called. This is knowledge, not a
#: guess: the boundary below is what makes the difference recordable.

EXTERNAL_PENDING = "EXTERNAL_PENDING"
#: The durable boundary was crossed. From here on, the honest statement is
#: "the external effect MAY have happened" — including if the adapter was never reached.

SUCCEEDED = "SUCCEEDED"
FAILED_SAFE = "FAILED_SAFE"
#: The adapter reported failure and asserted that NO external effect took place. Only an
#: adapter can know this, so it has to say so explicitly (`SafeExecutionFailure`).

UNKNOWN = "UNKNOWN"
#: Ambiguous outcome. Never auto-retried for NON_IDEMPOTENT_WRITE.

ABANDONED = "ABANDONED"
#: Recovery established that no effect happened (reconciliation, or a crash while still
#: CLAIMED), so this attempt is closed and a fresh attempt may be made.

TERMINAL_STATUSES = (SUCCEEDED, FAILED_SAFE, ABANDONED)
OPEN_STATUSES = (CLAIMED, EXTERNAL_PENDING, UNKNOWN)


class SafeExecutionFailure(Exception):
    """Raised by an adapter that KNOWS no external effect occurred.

    Every other exception is treated as an ambiguous outcome. That asymmetry is the point:
    before P1C an arbitrary exception was recorded as FAILED, which claimed more than anyone
    knew — reproduced with an adapter that completed its side effect and then raised.
    """


# ---------------------------------------------------------------------------
# Stable execution identity
# ---------------------------------------------------------------------------
def execution_id_for(core_instance_id: str, approval_id: str) -> str:
    """The stable execution identity of one approved logical action.

    A pure function of this core and the approval, so it survives a process restart, a new
    database connection and any number of retries WITHOUT being regenerated — a retry that
    minted a fresh identity would defeat remote deduplication exactly when it matters. Two
    different approvals can never share one, because the approval_id is in the digest.

    Callers do not supply it and cannot influence it: `approval_id` is server-generated and
    `core_instance_id` is this core's own identity file.
    """
    if not core_instance_id or not approval_id:
        raise ValueError("execution identity requires a core instance and an approval")
    digest = hashlib.sha256(
        f"solvio-execution-v1|{core_instance_id}|{approval_id}".encode("utf-8")).hexdigest()
    return "exec-" + digest[:32]


def idempotency_key_for(execution_id: str, capability: str) -> str:
    """What a remote service should deduplicate on. Derived, never supplied."""
    if not execution_id:
        raise ValueError("idempotency key requires an execution identity")
    return hashlib.sha256(
        f"solvio-idem-v1|{execution_id}|{capability or ''}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# THE recovery decision — one place, not scattered ifs
# ---------------------------------------------------------------------------
RETRY_SAME_KEY = "RETRY_SAME_KEY"
RECONCILE_FIRST = "RECONCILE_FIRST"
NO_ACTION_SUCCEEDED = "NO_ACTION_SUCCEEDED"
MANUAL_RECOVERY_REQUIRED = "MANUAL_RECOVERY_REQUIRED"
CLOSED_NO_EFFECT = "CLOSED_NO_EFFECT"


def recovery_decision(status: str, semantics: str) -> str:
    """What may be done with an attempt found after a restart.

    Deliberately total and deliberately boring: every (status, semantics) pair resolves here,
    and anything unrecognised falls through to manual recovery rather than to a retry.
    """
    if status == SUCCEEDED:
        return NO_ACTION_SUCCEEDED
    if status in (FAILED_SAFE, ABANDONED):
        # The adapter asserted no effect, or reconciliation established none. A NEW attempt
        # may be started by policy; this attempt is closed.
        return CLOSED_NO_EFFECT
    if status == CLAIMED:
        # The boundary was never crossed, so the adapter was never called. This is the one
        # crash window where "it did not happen" is knowledge rather than hope.
        return CLOSED_NO_EFFECT
    if status in (EXTERNAL_PENDING, UNKNOWN):
        if semantics == READ_ONLY:
            return RETRY_SAME_KEY
        if semantics == IDEMPOTENT_WRITE:
            return RETRY_SAME_KEY
        if semantics == RECONCILABLE_WRITE:
            return RECONCILE_FIRST
        return MANUAL_RECOVERY_REQUIRED       # NON_IDEMPOTENT_WRITE and anything unknown
    return MANUAL_RECOVERY_REQUIRED


def may_auto_retry(status: str, semantics: str) -> bool:
    """True only when the declared semantics permit a retry without asking anyone.

    Not a proof of harmlessness — see the module docstring. The guarantee is the far
    side's, and SOLVIO fails safe when none was declared.
    """
    return recovery_decision(status, semantics) in (RETRY_SAME_KEY, CLOSED_NO_EFFECT)
