"""N5: one explicit owner command through the existing S1/execution journal.

This is a closed memory adapter, not a general dashboard permission. The model
cannot supply its proof through JSON. Memory handlers stay shared with iPhone.
"""
import asyncio
from dataclasses import dataclass, replace
import hashlib
import json
import re

from solvio.security.mobile_approval import store as S, execution as X
from solvio.security.mobile_approval.browser_sessions import BrowserActor
from . import policy as P
from .envelope import CapabilityOutcome as O, CapabilityResult


def valid_arguments(name, arguments):
    if name not in S.MEMORY_COMMAND_TOOLS or type(arguments) is not dict:
        return False
    key = "candidate_id" if name in {"memory_confirm_candidate", "memory_decline_candidate"} else "memory_id"
    fields = {key, "statement"} if name == "memory_correct" else {key}
    if set(arguments) != fields:
        return False
    if not isinstance(arguments[key], str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", arguments[key]):
        return False
    return "statement" not in fields or (isinstance(arguments["statement"], str)
        and bool(arguments["statement"].strip()) and len(arguments["statement"]) <= 4000)


def _digest(name, arguments):
    return hashlib.sha256(json.dumps([name, 1, arguments], sort_keys=True,
                                    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class BrowserMemoryCommand:
    actor: BrowserActor
    client_request_id: str
    arguments_digest: str

    @classmethod
    def bind(cls, actor, client_request_id, name, arguments):
        if type(actor) is not BrowserActor or not valid_arguments(name, arguments):
            raise ValueError("invalid_memory_command")
        return cls(actor, client_request_id, _digest(name, arguments))

    def matches(self, spec, arguments, principal, origin):
        return (type(self.actor) is BrowserActor and self.actor.principal == principal
            and origin is P.OriginClass.TRUSTED_DASHBOARD and spec.version == 1
            and spec.execution_class.value == "controlled" and spec.semantics == X.NON_IDEMPOTENT_WRITE
            and valid_arguments(spec.name, arguments)
            and self.arguments_digest == _digest(spec.name, arguments))


async def read_status(control_plane, command_id, principal):
    """Read only. Never recover, approve, refresh a TTL or call a handler."""
    snapshot = await control_plane.store.browser_memory_status_snapshot(
        command_id, principal, control_plane.core_instance_id)
    if snapshot is None:
        return None
    req = snapshot["request"]
    execution_id = X.execution_id_for(control_plane.core_instance_id, command_id)
    attempts = snapshot["attempts"]
    codes = {a["status"] for a in attempts}
    reason = ""
    if snapshot["succeeded"]:
        state = "succeeded"
    elif X.UNKNOWN in codes or X.EXTERNAL_PENDING in codes:
        state = "outcome_unconfirmed"
    elif req["state"] in {S.FAILED, S.DENIED, S.EXPIRED}:
        state = "not_executed"
    elif req["state"] == S.APPROVED and req["expires_at"] <= snapshot["observed_at"]:
        state = "expired"
    elif req["state"] == S.APPROVED and not attempts:
        reason = snapshot["reason"]
        state = "pending" if snapshot["authorized"] else "not_executed"
    elif X.CLAIMED in codes:
        state = "pending"
    else:
        state = "outcome_unconfirmed"
    return {"command_id": command_id, "capability": req["tool"], "state": state,
            "requested_at": req["created_at"], "description": req["task"],
            "reason": "memory_target_changed" if any(a.get("detail") == "safe_failure:memory_target_changed" for a in attempts) else reason or "",
            "execution_id": execution_id, "request_state": req["state"],
            "attempt_states": sorted(codes)}


async def execute(command, mobile, spec, arguments, handler, call_id):
    from .approval_gateway import approval_mode, render_action
    from .router import _call
    from solvio.capabilities import execution_identity as EI
    from solvio.secret_vault import context as SC

    if mobile is None or mobile.owner_principal != command.actor.principal:
        return CapabilityResult(O.REJECTED_BY_POLICY, call_id, spec.name,
                                reason="owner_command_unavailable")
    co = mobile.coordinator
    command_id, status = await co.prepare_browser_memory_command(actor=command.actor,
        client_request_id=command.client_request_id, tool=spec.name, mode=approval_mode(spec),
        task=render_action(spec, arguments, P.origin_label(P.OriginClass.TRUSTED_DASHBOARD)))
    if command_id is None:
        return CapabilityResult(O.REJECTED_BY_POLICY, call_id, spec.name, reason=status)

    async def invoke(payload):
        identity = EI.ExecutionIdentity(execution_id=payload["execution_id"],
            idempotency_key=payload["idempotency_key"], approval_id=command_id,
            capability=spec.name, semantics=payload["semantics"], action_digest=payload["action_digest"])
        with EI.bound(identity), SC.bound(replace(SC.current(),
                execution_id=identity.execution_id, approval_id=command_id)):
            data = await asyncio.wait_for(_call(handler, dict(arguments)), timeout=spec.timeout)
        # A false handler result is never success; it may follow partial writes.
        ok = type(data) is dict and data.get("ok") is True
        return ok, {"reason": "" if ok else "memory_handler_did_not_confirm"}

    await co.execute_approved(command_id, invoke)
    data = await read_status(co.cp, command_id, command.actor.principal)
    state = (data or {}).get("state")
    outcome = O.SUCCESS if state == "succeeded" else O.CAPABILITY_FAILED if state in {"not_executed", "expired"} else O.RECOVERY_REQUIRED
    return CapabilityResult(outcome, call_id, spec.name, data=data,
        reason="" if state == "succeeded" else "memory_" + str(state),
        human_message="Änderung bestätigt." if state == "succeeded" else "Der Abschluss ist nicht bestätigt. Bitte den Zustand nachsehen; nicht erneut ändern.")
