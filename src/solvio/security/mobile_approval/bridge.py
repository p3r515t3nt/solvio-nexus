"""Wiring: durable mobile control-plane -> S1 ApprovalBroker (final execution gate).

Architecture rule (S2A): there is ONE approval truth — `approval_control.sqlite3`. The S1
`ApprovalBroker` remains the LAST execution boundary for Codex. Only AFTER a valid iPhone
decision does the control-plane feed a trusted approval INTO the broker; never the other
way round and never a second parallel 'approved' shortcut.

Flow:  iPhone signs decision -> control-plane VERIFIES + marks the stored request APPROVED
-> coordinator confirms exactly that approval to the S1 gate -> broker.approve (gated by
MobileApprover) -> executor claims exactly the stored action from the broker.

Invariant preserved: iPhone proves authority. Mac decides. The model executes no approval.
N2 task starts additionally accept an explicit authenticated dashboard decision.
This is recorded as dashboard_session, never disguised as a mobile proof; the same
S1 action gate, claim journal and durable external boundary still govern execution.
The `MobileDecisionVerifier` (submit_decision) NEVER runs Codex; execution only happens via
the S1 gate + an injected executor that receives the exact stored action.
"""
from __future__ import annotations

import os
import threading

from solvio.security.mobile_approval import execution as X
from solvio.security.mobile_approval import store as S


class MobileApprover:
    """S1 `Approver` backed by verified control-plane decisions.

    iPhone decisions retain both proofs. N2's explicit dashboard-session method is
    restricted by the store to two task starts. Confirmation binds the exact action;
    the durable claim independently rechecks the corresponding current authority.
    The language model has no path to confirm. There is no default transport trust.
    """

    def __init__(self) -> None:
        self._confirmed: dict[str, tuple[str, str]] = {}  # approval_id -> (principal, digest)

    def confirm(self, approval_id: str, principal: str, digest: str) -> None:
        self._confirmed[approval_id] = (principal, digest)

    def revoke_confirmation(self, approval_id: str) -> None:
        self._confirmed.pop(approval_id, None)

    def is_confirmed(self, approval_id: str) -> bool:
        return approval_id in self._confirmed

    def is_trusted(self, request, identity) -> bool:
        # `identity` is the approval_id the coordinator is executing; the request's action
        # must match the confirmed (principal, digest) exactly.
        entry = self._confirmed.get(identity)
        if entry is None:
            return False
        principal, digest = entry
        return request.principal == principal and request.digest == digest


class MobileApprovalCoordinator:
    #: P1C/§13: how long a claimed attempt holds its lease. A lease settles OWNERSHIP only.
    #: It must be long enough that a healthy executor is not overtaken mid-run, and short
    #: enough that a CRASHED owner does not block recovery for longer than an operator would
    #: tolerate — a dead process never releases its lease, so expiry is the only way back.
    #: Server-side configuration, deliberately not a per-call parameter.
    claim_lease_seconds: float = 120.0
    recovery_lease_seconds: float = 120.0

    def __init__(self, control_plane, s1_broker, mobile_approver: MobileApprover) -> None:
        self.cp = control_plane
        self.broker = s1_broker
        self.approver = mobile_approver

    async def request_codex_modify(self, *, principal: str, task: str, workspace: str,
                                   human_summary: str) -> str:
        """Model-initiated REQUEST only. Creates the durable pending request; returns the
        approval_id. No token/authority is returned to the model."""
        return await self.cp.create_request(
            principal=principal, tool="codex_task", mode="modify", task=task,
            workspace=workspace, human_summary=human_summary)

    async def apply_mobile_decision(self, *, payload_b64: str, signature_b64: str,
                                    key_id: str, assertion_b64: str | None = None):
        """Verify the iPhone decision (all crypto + semantic checks in the control-plane,
        including the App Attest assertion for APPROVE). On APPROVE, confirm exactly that
        approval to the S1 gate — nothing executes yet."""
        res, st = await self.cp.submit_decision(
            payload_b64=payload_b64, signature_b64=signature_b64, key_id=key_id,
            assertion_b64=assertion_b64)
        if st == "ok" and res["decision"] == "APPROVE":
            req = await self.cp.store.get_request(res["approval_id"])
            self.approver.confirm(res["approval_id"], req["principal"], req["action_digest"])
        return res, st

    async def apply_dashboard_decision(self, *, actor, approval_id: str,
                                       action_digest: str, decision: str):
        res, st = await self.cp.submit_dashboard_decision(
            actor=actor, approval_id=approval_id, action_digest=action_digest, decision=decision)
        if st == "ok" and res["decision"] == "APPROVE":
            # A session revoked after commit cannot authorize the eventual claim.
            req = await self.cp.store.get_request(approval_id)
            self.approver.confirm(approval_id, req["principal"], req["action_digest"])
        return res, st

    async def execute_approved(self, approval_id: str, executor):
        """Claim the EXACT stored action through the S1 gate and run it, with a durable
        boundary in front of the external call.

        `executor` is an async callable(action: dict) -> (ok, info). The action dict now also
        carries `execution_id` and `idempotency_key` so an adapter can deduplicate remotely.
        An adapter that KNOWS no side effect happened raises `X.SafeExecutionFailure`; every
        other exception is an AMBIGUOUS outcome, not a failure.

        P1C/F4: before this, an executor that raised AFTER its side effect was recorded as
        FAILED — a statement about the world that nobody had established. Reproduced.
        """
        req = await self.cp.store.get_request(approval_id)
        if req is None or req["state"] != S.APPROVED:
            return None, "not_approved"
        execution_id = X.execution_id_for(self.cp.core_instance_id, approval_id)
        capability = req["tool"]
        semantics = X.semantics_for(capability)
        idem = X.idempotency_key_for(execution_id, capability)
        # Derived, never a parameter: P1A/F3 keeps `execute_approved` down to an approval_id
        # so there is nothing in its signature a caller could use to nominate identity. The
        # thread id keeps two in-process executors distinct without widening that surface.
        owner = f"pid-{os.getpid()}-t{threading.get_ident()}"

        # Final execution gate = S1 ApprovalBroker.
        gate = self.broker.request(principal=req["principal"], tool=req["tool"],
                                   task=req["task"], workspace=req["workspace"],
                                   mode=req["mode"])
        _, ast_ = self.broker.approve(request_id=gate.request_id, identity=approval_id,
                                      presented_digest=gate.digest)
        if ast_ != "ok":
            return None, "s1_" + ast_  # fail-closed without a confirmed control-plane decision
        action, tst = self.broker.take_approved(request_id=gate.request_id, tool=req["tool"])
        if action is None:
            return None, "s1_take_" + tst

        # P1A.1/F3 + P1C: the claim, its TTL/device/revocation predicates and the journal
        # entry commit together. Nothing external has happened at this point, and the row
        # says so — that is the difference between "did not happen" and "may have happened".
        if req.get("decision_method") == S.MEMORY_COMMAND_METHOD:
            identities, why = await self.cp.store.browser_command_authorization(
                approval_id, self.cp.core_instance_id)
        elif req.get("decision_method") == S.DASHBOARD_METHOD:
            identities, why = await self.cp.store.dashboard_execution_authorization(
                approval_id, self.cp.core_instance_id)
        else:
            identities, why = await self.cp.execution_preflight(approval_id, req["decided_device"])
        if identities is None:
            self.approver.revoke_confirmation(approval_id)
            return None, why
        attempt_id, cst = await self.cp.store.claim_execution_attempt(
            approval_id=approval_id, device_id=req["decided_device"], identities=identities,
            execution_id=execution_id, capability=capability, semantics=semantics,
            idempotency_key=idem, owner=owner,
            lease_seconds=self.claim_lease_seconds,
            core_instance_id=self.cp.core_instance_id)
        # Either way the one-shot confirmation is spent — a refused claim must not leave a
        # usable confirmation behind for a second attempt.
        self.approver.revoke_confirmation(approval_id)
        if attempt_id is None:
            return None, cst
        return await self._run_attempt(attempt_id, approval_id, execution_id, identities,
                                       action, executor, semantics, idem)

    async def prepare_browser_memory_command(self, **command):
        approval_id, status = await self.cp.prepare_browser_memory_command(**command)
        if approval_id is not None:
            req = await self.cp.store.get_request(approval_id)
            if req["state"] == S.APPROVED:
                self.approver.confirm(approval_id, req["principal"], req["action_digest"])
        return approval_id, status

    async def _run_attempt(self, attempt_id, approval_id, execution_id, identities, action,
                           executor, semantics, idem):
        """Cross the durable boundary, call the adapter, record the outcome."""
        bst = await self.cp.store.begin_external_execution(
            attempt_id=attempt_id, identities=identities)
        if bst != "ok":
            # HYGIENE/H8: the store terminalises the attempt AND the request in the same
            # transaction that refuses the boundary. Calling finish again here used to hit
            # "attempt_already_final" and silently skip the request transition, leaving the
            # row EXECUTING for ever. Only close what is genuinely still open.
            if await self.cp.store.open_attempt_by_id(attempt_id) is not None:
                await self.cp.store.finish_execution_attempt(
                    attempt_id=attempt_id, status=X.ABANDONED, detail=bst,
                    request_state=S.FAILED)
            return None, bst

        payload = {"tool": action.tool, "mode": action.mode, "task": action.task,
                   "workspace": action.workspace, "action_digest": action.digest,
                   "execution_id": execution_id, "idempotency_key": idem,
                   "semantics": semantics}
        try:
            ok, info = await executor(payload)
        except X.SafeExecutionFailure as exc:
            # The adapter asserts NO external effect happened. Only it can know that.
            await self.cp.store.finish_execution_attempt(
                attempt_id=attempt_id, status=X.FAILED_SAFE,
                detail=f"safe_failure:{exc}", request_state=S.FAILED)
            return None, "failed_safe"
        except Exception as exc:  # noqa: BLE001
            # AMBIGUOUS. The boundary was crossed, so the effect MAY have happened. Recording
            # FAILED here would claim knowledge nobody has.
            await self.cp.store.finish_execution_attempt(
                attempt_id=attempt_id, status=X.UNKNOWN,
                detail=f"executor:{type(exc).__name__}", request_state=None)
            return None, "unknown_outcome"
        if ok:
            fst = await self.cp.store.finish_execution_attempt(
                attempt_id=attempt_id, status=X.SUCCEEDED, request_state=S.CONSUMED)
            if fst != "ok":
                return None, "outcome_" + fst
            return {"info": info, "execution_id": execution_id}, "ok"
        # A falsy result is a reported failure, but the adapter did not assert it was safe.
        await self.cp.store.finish_execution_attempt(
            attempt_id=attempt_id, status=X.UNKNOWN, detail=str(info)[:200],
            request_state=None)
        return None, "unknown_outcome"

    async def startup_recovery_scan(self):
        """FREEZE/F1: on start, look at what the journal still has open — and only look.

        Not a scheduler and not a background job. It runs once, classifies every open
        attempt through the SAME central policy the coordinator uses, and returns a summary.
        It performs **no external write of any kind**: a Mac rebooting is not a reason for
        anything to happen in the outside world.

        The only mutation it makes is closing an attempt that is *still* `CLAIMED`, which is
        bookkeeping rather than an action — `CLAIMED` means the adapter was provably never
        called. Doing even that safely needs two things the first version lacked:

          * the recovery LEASE, so a second process (or a live owner) cannot be overwritten;
          * an expected-status CAS, so the decision cannot be applied to a row that moved to
            `EXTERNAL_PENDING` after the snapshot was taken. That race is the dangerous one:
            it would durably record "no external effect took place" about an effect that may
            well have happened.

        Losing either compare is a normal outcome, not an error: the attempt is reported for
        attention instead of being closed.
        """
        rows = await self.cp.store.open_execution_attempts()
        summary = {"open": len(rows), "closed_no_effect": 0, "needs_attention": [],
                   "retryable": [], "reconcilable": [], "contended": []}
        owner = f"startup-pid-{os.getpid()}-t{threading.get_ident()}"
        for row in rows:
            decision = X.recovery_decision(row["status"], row["semantics"])
            entry = {"attempt_id": row["attempt_id"], "execution_id": row["execution_id"],
                     "approval_id": row["approval_id"], "capability": row["capability"],
                     "semantics": row["semantics"], "status": row["status"],
                     "decision": decision}
            if decision == X.RETRY_SAME_KEY:
                # Deliberately NOT retried here. A retry is an external write and needs a
                # fresh authority check plus an executor; startup only reports it.
                summary["retryable"].append(entry)
                continue
            if decision == X.RECONCILE_FIRST:
                summary["reconcilable"].append(entry)
                continue
            if decision != X.CLOSED_NO_EFFECT:
                summary["needs_attention"].append(entry)
                continue
            # CLOSED_NO_EFFECT — the only thing this scan may write, and only under a lease
            # and an expected-status compare.
            if await self.cp.store.acquire_recovery_lease(
                    attempt_id=row["attempt_id"], owner=owner,
                    lease_seconds=self.recovery_lease_seconds) != "ok":
                entry["detail"] = "recovery_lease_held"
                summary["contended"].append(entry)
                continue
            fresh = await self.cp.store.open_attempt_by_id(row["attempt_id"])
            if fresh is None:
                entry["detail"] = "closed_by_someone_else"
                summary["contended"].append(entry)
                continue
            if fresh["status"] != row["status"]:
                # It moved while we were deciding. Re-classify from what is true NOW.
                entry["status"] = fresh["status"]
                entry["decision"] = X.recovery_decision(fresh["status"], row["semantics"])
                entry["detail"] = f"moved {row['status']} -> {fresh['status']} during the scan"
                summary["needs_attention"].append(entry)
                continue
            st = await self.cp.store.finish_execution_attempt(
                attempt_id=row["attempt_id"], status=X.ABANDONED,
                detail="startup recovery: no external effect took place",
                request_state=S.FAILED, expected_status=row["status"])
            if st == "ok":
                summary["closed_no_effect"] += 1
            else:
                entry["detail"] = st
                summary["contended"].append(entry)
        await self.cp.store.audit(
            "startup_recovery_scan",
            reason=f"open={summary['open']} closed={summary['closed_no_effect']} "
                   f"attention={len(summary['needs_attention'])} "
                   f"retryable={len(summary['retryable'])} "
                   f"reconcilable={len(summary['reconcilable'])} "
                   f"contended={len(summary['contended'])}")
        return summary

    async def _authority_for_retry(self, attempt, out):
        """HYGIENE/H1: server-side re-check immediately before a recovery retry writes.

        Returns None when the retry may proceed, otherwise a refusal reason — and in that
        case the attempt is parked as UNKNOWN rather than closed, because a crashed
        EXTERNAL_PENDING attempt whose authority has since died is still ambiguous: the
        effect may have happened, and losing the authority does not answer that question.
        """
        rst = await self.cp.store.recheck_execution_authority(
            attempt_id=attempt["attempt_id"],
            allowed_environments=self.cp.allowed_environments,
            core_instance_id=self.cp.core_instance_id)
        if rst == "ok":
            return None
        if attempt["status"] != X.UNKNOWN:
            await self.cp.store.finish_execution_attempt(
                attempt_id=attempt["attempt_id"], status=X.UNKNOWN,
                detail=f"retry refused: {rst}", request_state=None)
        out["authority"] = rst
        return "retry_refused:" + rst

    async def recover_execution(self, approval_id: str, executor=None, *,
                                reconciler=None, owner=None):
        """P1C/§9: the ONE recovery decision for an interrupted execution.

        Called after a restart. It never invents authority: it can only continue the logical
        action the human already approved, under the identity that action already has.
        """
        execution_id = X.execution_id_for(self.cp.core_instance_id, approval_id)
        attempt = await self.cp.store.open_attempt_for(execution_id)
        if attempt is None:
            done = [a for a in await self.cp.store.attempts_for(execution_id)
                    if a["status"] == X.SUCCEEDED]
            return ({"execution_id": execution_id, "decision": X.NO_ACTION_SUCCEEDED},
                    "already_succeeded") if done else (None, "no_open_attempt")

        owner = owner or f"pid-{os.getpid()}-t{threading.get_ident()}"
        if await self.cp.store.acquire_recovery_lease(
                attempt_id=attempt["attempt_id"], owner=owner,
                lease_seconds=self.recovery_lease_seconds) != "ok":
            return None, "recovery_lease_held"

        decision = X.recovery_decision(attempt["status"], attempt["semantics"])
        out = {"execution_id": execution_id, "attempt_id": attempt["attempt_id"],
               "status": attempt["status"], "semantics": attempt["semantics"],
               "decision": decision}
        if decision == X.NO_ACTION_SUCCEEDED:
            return out, "already_succeeded"
        if decision == X.MANUAL_RECOVERY_REQUIRED:
            # Park it as UNKNOWN so the state names the uncertainty instead of implying one
            # of the two answers. The model cannot resolve this; a human must.
            if attempt["status"] != X.UNKNOWN:
                await self.cp.store.finish_execution_attempt(
                    attempt_id=attempt["attempt_id"], status=X.UNKNOWN,
                    detail="recovery: outcome unknown, manual recovery required",
                    request_state=None)
            return out, "manual_recovery_required"
        if decision == X.CLOSED_NO_EFFECT:
            await self.cp.store.finish_execution_attempt(
                attempt_id=attempt["attempt_id"], status=X.ABANDONED,
                detail="recovery: no external effect took place", request_state=S.FAILED)
            return out, "closed_no_effect"
        if decision == X.RECONCILE_FIRST:
            if reconciler is None:
                return out, "reconciler_required"
            happened = await reconciler(execution_id)
            if happened:
                await self.cp.store.finish_execution_attempt(
                    attempt_id=attempt["attempt_id"], status=X.SUCCEEDED,
                    detail="recovery: reconciled as already executed",
                    request_state=S.CONSUMED)
                out["reconciled"] = True
                return out, "reconciled_succeeded"
            # The observation is done and it is safe — but the retry it implies is a NEW
            # external write, so the attempt is closed here rather than silently retried.
            # Re-acquiring authority is a separate, explicit step.
            await self.cp.store.finish_execution_attempt(
                attempt_id=attempt["attempt_id"], status=X.ABANDONED,
                detail="recovery: reconciled as not executed", request_state=S.FAILED)
            out["reconciled"] = False
            return out, "reconciled_not_executed"
        # RETRY_SAME_KEY — only for semantics whose declared remote behaviour makes a repeat
        # harmless, and only ever with the SAME derived idempotency key.
        if executor is None:
            return out, "executor_required"
        # HYGIENE/H1: a retry is a NEW external write. That the authority held before the
        # crash says nothing about now.
        rst = await self._authority_for_retry(attempt, out)
        if rst is not None:
            return None, rst
        payload = {"tool": attempt["capability"], "execution_id": execution_id,
                   "idempotency_key": attempt["idempotency_key"],
                   "semantics": attempt["semantics"], "recovery": True}
        try:
            ok, info = await executor(payload)
        except X.SafeExecutionFailure as exc:
            await self.cp.store.finish_execution_attempt(
                attempt_id=attempt["attempt_id"], status=X.FAILED_SAFE,
                detail=f"safe_failure:{exc}", request_state=S.FAILED)
            return None, "failed_safe"
        except Exception as exc:  # noqa: BLE001
            await self.cp.store.finish_execution_attempt(
                attempt_id=attempt["attempt_id"], status=X.UNKNOWN,
                detail=f"retry:{type(exc).__name__}", request_state=None)
            return None, "unknown_outcome"
        if ok:
            await self.cp.store.finish_execution_attempt(
                attempt_id=attempt["attempt_id"], status=X.SUCCEEDED,
                detail="recovery retry", request_state=S.CONSUMED)
            out["info"] = info
            return out, "retried_succeeded"
        await self.cp.store.finish_execution_attempt(
            attempt_id=attempt["attempt_id"], status=X.UNKNOWN, detail=str(info)[:200],
            request_state=None)
        return None, "unknown_outcome"
