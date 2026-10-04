"""Wiring tests: mobile control-plane -> S1 broker -> executor (STEP S2A, attested S2A.1).

Proves the production path AND the invariant: iPhone proves authority (Face-ID approval
signature + Apple App Attest assertion), Mac decides, the model executes no approval; the S1
broker is the final execution gate; the executor runs exactly the stored action. Fake
executor + temp workspace (no real codex modify). Direct: python test_mobile_wiring.py."""
import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import mobile_attest_helper as H
from solvio.security.approval import ApprovalBroker
from solvio.security.mobile_approval import bridge as B
from solvio.security.mobile_approval import control as C
from solvio.security.mobile_approval import identity
from solvio.security.mobile_approval import protocol as P
from solvio.security.mobile_approval import store as S

APP_ID = "WQ8CG7R53R.de.solvio.approvals"


async def _wire(tmp):
    st = S.ApprovalControlStore(os.path.join(tmp, "approval_control.sqlite3"))
    await st.open()
    cp = C.MobileApprovalControlPlane(st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
                                      attest_verifier=H.fake_verifier(), app_id=APP_ID)
    approver = B.MobileApprover()
    broker = ApprovalBroker(approver=approver)  # S1 gate wired to the mobile approver
    coord = B.MobileApprovalCoordinator(cp, broker, approver)
    return st, cp, coord, approver


async def _decision(cp, ctx, approval_id, *, decision=P.DECISION_APPROVE):
    wire, s = await cp.issue_challenge(approval_id=approval_id, device_id=ctx.device_id)
    assert s == "ok", s
    return H.sign_decision(ctx, P.b64d(wire["payload_b64"]), decision=decision)


def _executor(calls):
    async def run(action):
        calls.append(action)
        with open(os.path.join(action["workspace"], "codex_ran.txt"), "w", encoding="utf-8") as f:
            f.write(action["action_digest"])
        return True, {"changed_files": ["codex_ran.txt"]}
    return run


async def t_full_production_path_exact_action():
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, approver = await _wire(tmp)
        ctx = await H.enroll_attested(cp)
        approval_id = await coord.request_codex_modify(
            principal="local-owner", task="edit README", workspace=ws,
            human_summary="Codex will edit README")
        assert (await st.get_request(approval_id))["state"] == S.PENDING
        marker = os.path.join(ws, "codex_ran.txt")
        assert not os.path.exists(marker)
        res, s = await coord.apply_mobile_decision(**(await _decision(cp, ctx, approval_id)))
        assert s == "ok" and res["decision"] == "APPROVE"
        assert (await st.get_request(approval_id))["state"] == S.APPROVED
        calls = []
        out, s2 = await coord.execute_approved(approval_id, _executor(calls))
        assert s2 == "ok"
        assert os.path.exists(marker)
        assert len(calls) == 1 and calls[0]["task"] == "edit README" and calls[0]["workspace"] == ws
        assert (await st.get_request(approval_id))["state"] == S.CONSUMED
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_s1_gate_fail_closed_without_confirmed_decision():
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, approver = await _wire(tmp)
        approval_id = await coord.request_codex_modify(
            principal="local-owner", task="x", workspace=ws, human_summary="x")
        await st.transition(approval_id, S.APPROVED)  # durable APPROVED WITHOUT a verified iPhone decision
        assert approver.is_confirmed(approval_id) is False
        calls = []
        out, s = await coord.execute_approved(approval_id, _executor(calls))
        assert out is None and s == "s1_no_trusted_approver"
        assert calls == [] and not os.path.exists(os.path.join(ws, "codex_ran.txt"))
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_no_execution_before_approval():
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, approver = await _wire(tmp)
        approval_id = await coord.request_codex_modify(
            principal="local-owner", task="x", workspace=ws, human_summary="x")
        calls = []
        out, s = await coord.execute_approved(approval_id, _executor(calls))
        assert out is None and s == "not_approved" and calls == []
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_replay_blocked_after_consume():
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, approver = await _wire(tmp)
        ctx = await H.enroll_attested(cp)
        approval_id = await coord.request_codex_modify(
            principal="local-owner", task="edit README", workspace=ws, human_summary="s")
        wire = await _decision(cp, ctx, approval_id)
        await coord.apply_mobile_decision(**wire)
        out, s = await coord.execute_approved(approval_id, _executor([]))
        assert s == "ok"
        out2, s2 = await coord.execute_approved(approval_id, _executor([]))
        assert out2 is None and s2 == "not_approved"
        r3, s3 = await coord.apply_mobile_decision(**wire)
        assert r3 is None and s3 == "not_pending"
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_deny_does_not_execute():
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, approver = await _wire(tmp)
        ctx = await H.enroll_attested(cp)
        approval_id = await coord.request_codex_modify(
            principal="local-owner", task="x", workspace=ws, human_summary="x")
        res, s = await coord.apply_mobile_decision(
            **(await _decision(cp, ctx, approval_id, decision=P.DECISION_DENY)))
        assert s == "ok" and res["decision"] == "DENY"
        assert (await st.get_request(approval_id))["state"] == S.DENIED
        assert approver.is_confirmed(approval_id) is False
        out, s2 = await coord.execute_approved(approval_id, _executor([]))
        assert out is None and s2 == "not_approved"
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
