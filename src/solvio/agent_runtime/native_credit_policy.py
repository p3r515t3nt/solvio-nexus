"""Account-bound credit consent in the EXISTING Face-ID execution journal.

No second authority store. A completed, bound mobile approval is the setting;
pending/denied/unknown executions never enable it. This is not a cash cap or a
provider billing-setting monitor. SOLVIO exposes no purchase/reload operation.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
from pathlib import Path

from solvio.capabilities.approval_gateway import approval_digest, approval_mode, render_action
from solvio.capabilities.contract import CapabilitySpec, ExecutionClass
from solvio.nodes.models import DataClass
from solvio.security.mobile_approval.execution import NON_IDEMPOTENT_WRITE, execution_id_for
from solvio.tools.base import RiskLevel

NAME = "native_credit_consent"
ORIGIN = "SOLVIO-Einstellungen"
TERMS = ("Vorhandene ChatGPT-Credits für SOLVIO nutzen. Verbrauch je Auftrag wird nicht "
         "gemessen. Ein laufender Auftrag kann das Guthaben ins Minus bringen; spätere "
         "Aufladungen gleichen das zuerst aus. SOLVIO kauft keine Credits und aktiviert "
         "kein automatisches Nachladen. Die Nachladeeinstellung bei OpenAI liegt beim Nutzer.")
SPEC = CapabilitySpec(name=NAME, version=1, execution_class=ExecutionClass.CONTROLLED,
    base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
    data_class=DataClass.HOME_ONLY, description="ChatGPT-Credits für SOLVIO erlauben oder sperren",
    input_schema={"type": "object", "additionalProperties": False,
                  "properties": {k: {"type": "string"} for k in ("konto", "nutzung", "bedingungen")},
                  "required": ["konto", "nutzung", "bedingungen"]})


def arguments(account: str, enabled: bool) -> dict:
    if not isinstance(account, str) or not re.fullmatch(r"[a-f0-9]{64}", account):
        raise ValueError("credit_account_unconfirmed")
    if type(enabled) is not bool:
        raise ValueError("invalid_credit_choice")
    return {"konto": "ChatGPT-Kontobindung " + account,
            "nutzung": "Erlauben" if enabled else "Sperren", "bedingungen": TERMS}


class NativeCreditPolicy:
    def __init__(self, approvals):
        self.approvals = approvals
        self.jobs = set()

    def grant(self, account: str) -> str:
        """Read-only projection, re-read immediately before physical dispatch.

        Today's device binding/revocations must still match the consumed claim.
        A completed later Sperren supersedes Erlauben. No historical request is
        re-executed, and no approval state is written by this reader.
        """
        if self.approvals is None:
            return ""
        cp = self.approvals.control_plane
        allow, deny = arguments(account, True), arguments(account, False)
        try:
            with sqlite3.connect(Path(cp.store.path).resolve().as_uri() + "?mode=ro", uri=True,
                                 timeout=1) as db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN")
                req = db.execute("SELECT * FROM approval_requests WHERE tool=? AND principal=? "
                    "AND request_core_instance_id=? AND state='CONSUMED' AND task IN (?,?) "
                    "ORDER BY decided_at DESC,created_at DESC LIMIT 1", (NAME,
                    self.approvals.owner_principal, cp.core_instance_id,
                    render_action(SPEC, allow, ORIGIN), render_action(SPEC, deny, ORIGIN))).fetchone()
                if (not req or req["task"] != render_action(SPEC, allow, ORIGIN)
                        or req["mode"] != approval_mode(SPEC)
                        or req["decision_method"] != "face_id" or req["workspace"]
                        or req["action_digest"] != approval_digest(SPEC, allow, ORIGIN)):
                    return ""
                att = db.execute("SELECT * FROM execution_attempts WHERE approval_id=? "
                    "AND execution_id=? AND status='SUCCEEDED'", (req["approval_id"],
                    execution_id_for(cp.core_instance_id, req["approval_id"]))).fetchone()
                if (not att or not att["claim_identity_bound"] or not att["boundary_at"]
                        or not att["finished_at"] or att["capability"] != NAME
                        or att["claim_authorization_method"] != "face_id"
                        or att["claim_action_digest"] != req["action_digest"]
                        or att["device_id"] != req["decided_device"]):
                    return ""
                dev = db.execute("SELECT * FROM devices WHERE device_id=?", (att["device_id"],)).fetchone()
                if (not dev or dev["status"] != "ACTIVE" or dev["attestation_status"] != "ATTESTED"
                        or not dev["current_enrollment_id"] or dev["principal"] != req["principal"]
                        or dev["environment"] not in (cp.allowed_environments or ())):
                    return ""
                fingerprint = hashlib.sha256(bytes.fromhex(dev["public_key_x963"])).hexdigest()
                pairs = (("core_instance_id", cp.core_instance_id), ("principal", dev["principal"]),
                         ("environment", dev["environment"]), ("enrollment_id", dev["current_enrollment_id"]),
                         ("approval_key_sha256", fingerprint), ("app_attest_key_id", dev["app_attest_key_id"]))
                if any(att["claim_" + key] != value for key, value in pairs):
                    return ""
                if db.execute("SELECT 1 FROM revocations WHERE (kind='device' AND value=?) "
                    "OR (kind='approval_key' AND value=?) OR (kind='app_attest_key' AND value=?)",
                    (dev["device_id"], fingerprint, dev["app_attest_key_id"])).fetchone():
                    return ""
                return "approval:" + req["approval_id"]
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            return ""

    async def request(self, account, enabled, *, account_check):
        args = arguments(account, enabled)
        # The existing control plane performs Face ID; browser sessions cannot
        # approve this capability. The journal is the ONLY durable policy.
        approval_id = await self.approvals.request(SPEC, args,
            requested_by=self.approvals.owner_principal, origin_label=ORIGIN)

        async def consume():
            async def activate(_args):
                if not await account_check(account):
                    from solvio.security.mobile_approval.execution import SafeExecutionFailure
                    raise SafeExecutionFailure("credit_account_changed")
                return {"credit_permission": enabled, "consumption_measured": False}
            for _ in range(900):
                row = await self.approvals.control_plane.store.get_request(approval_id)
                if not row or row["state"] != "PENDING":
                    if row and row["state"] == "APPROVED":
                        await self.approvals.resume(approval_id, SPEC, args, activate, ORIGIN)
                    return
                await asyncio.sleep(1)
            await self.approvals.abandon(approval_id, reason="credit_consent_expired")

        job = asyncio.create_task(consume())
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)
        return approval_id

    async def close(self):
        for job in self.jobs:
            job.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
