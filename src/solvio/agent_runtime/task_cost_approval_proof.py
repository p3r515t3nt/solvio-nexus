"""One app-authenticated cost-cap decision in the existing task cost ledger.

The registered App Attest key proves the exact request. This is not a biometric
decision, task start or permission to resume. Nonces and device/enrollment
freshness remain owned by the existing TaskStartProofService.
"""
from __future__ import annotations

import hashlib
import re

from solvio.agent_runtime.costs import MAX_CENTS
from solvio.agent_runtime.task_start_proof import TaskStartProofService
from solvio.agent_runtime.task_start_service import request_identifier
from solvio.security.mobile_approval import protocol as P

DOMAIN_TASK_COST_APPROVAL = b"SOLVIO_APP_TASK_COST_APPROVAL_V1"
TYPE_TASK_COST_APPROVAL = "app_task_cost_approval_binding"


def canonical_cost_approval(value):
    if type(value) is not dict or set(value) != {"task_id", "max_total_cents", "client_request_id"}:
        raise ValueError("invalid_cost_approval")
    if type(value["task_id"]) is not str or re.fullmatch(r"at-[0-9a-f]{16}", value["task_id"]) is None:
        raise ValueError("invalid_task_id")
    amount = value["max_total_cents"]
    if type(amount) is not int or not 0 <= amount <= MAX_CENTS:
        raise ValueError("invalid_approved_cap")
    request_identifier(value["client_request_id"])
    return dict(value)


def request_digest(value):
    return hashlib.sha256(P.canonical_bytes(canonical_cost_approval(value))).hexdigest()


def client_data_hash(raw):
    return hashlib.sha256(DOMAIN_TASK_COST_APPROVAL + b"\0" + raw).digest()


class TaskCostApprovalProofService(TaskStartProofService):
    body_digest = staticmethod(request_digest)
    assertion_hash = staticmethod(client_data_hash)
    challenge_prefix = "app-task-cost-approval:"
    binding_type = TYPE_TASK_COST_APPROVAL
    audit_prefix = "app_task_cost_approval"


def approval_reference(principal, client_request_id):
    # Nonce, enrollment, task and amount deliberately do not distinguish a
    # retry. The existing cost authorization row checks task/category/amount
    # and refuses a changed request, including after a fresh nonce or restart.
    if type(principal) is not str or not principal:
        raise ValueError("invalid_principal")
    value = {"principal": principal, "client_request_id": request_identifier(client_request_id)}
    return "app-task-cost-approval:" + hashlib.sha256(P.canonical_bytes(value)).hexdigest()
