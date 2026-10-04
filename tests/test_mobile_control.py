"""Mobile approval control-plane tests (STEP S2A, attested for S2A.1). MAC side, no Secure
Enclave hardware (software P-256 keys stand in for the iPhone SE approval key + Apple App
Attest key; the Apple chain is faked, see mobile_attest_helper). Direct: python <file>."""
import asyncio
import os
import shutil
import sys
import tempfile
import hashlib
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import mobile_attest_helper as H
from solvio.security.approval import ApprovalBroker
from solvio.security.mobile_approval import bridge as B
from solvio.security.mobile_approval import control as C
from solvio.security.mobile_approval import crypto
from solvio.security.mobile_approval import identity
from solvio.security.mobile_approval import protocol as P
from solvio.security.mobile_approval import store as S

WS = "/tmp/solvio-approval-testws"
APP_ID = "WQ8CG7R53R.de.solvio.approvals"


async def _fresh(tmp):
    st = S.ApprovalControlStore(os.path.join(tmp, "approval_control.sqlite3"))
    await st.open()
    cp = C.MobileApprovalControlPlane(st, identity.MacSigningKey.load_or_create(tmp),
                                      "core-test-1", attest_verifier=H.fake_verifier(),
                                      app_id=APP_ID)
    return st, cp


def _coord(cp):
    """S1-gated execution path — the ONLY supported way to claim an approved action
    (claim_approved was removed in P1A/F3)."""
    approver = B.MobileApprover()
    return B.MobileApprovalCoordinator(cp, ApprovalBroker(approver=approver), approver)


async def _enroll(cp, principal="local-owner", device_id="dev-1"):
    return await H.enroll_attested(cp, device_id=device_id, principal=principal,
                                   transport_cred="tcred-1")


def _sign_decision(dev_key, ch_payload, *, decision=P.DECISION_APPROVE, device_id="dev-1",
                   action_digest=None, principal_id=None, core_instance_id=None, key_id=None):
    """Manual decision (NO assertion) for the pre-assertion negative checks."""
    kid = key_id or crypto.key_id(dev_key)
    payload = P.build_decision_payload(
        core_instance_id=core_instance_id or ch_payload["core_instance_id"],
        approval_id=ch_payload["approval_id"],
        action_digest=action_digest or ch_payload["action_digest"],
        principal_id=principal_id or ch_payload["principal_id"], device_id=device_id,
        key_id=kid, challenge_nonce=ch_payload["challenge_nonce"],
        challenge_payload_sha256=hashlib.sha256(P.canonical_bytes(ch_payload)).hexdigest(),
        decision=decision, issued_at=int(time.time()),
        challenge_expires_at=ch_payload["expires_at"])
    raw = P.canonical_bytes(payload)
    return {"payload_b64": P.b64e(raw), "signature_b64": P.b64e(crypto.sign(dev_key, raw)),
            "key_id": kid}


async def _challenge(cp, ctx):
    approval_id = await cp.create_request(principal="local-owner", tool="codex_task",
                                          mode="modify", task="edit README", workspace=WS,
                                          human_summary="Codex will edit README")
    wire, st = await cp.issue_challenge(approval_id=approval_id, device_id=ctx.device_id)
    assert st == "ok", st
    ch_raw = P.b64d(wire["payload_b64"])
    ch = P.strict_parse(ch_raw)
    assert crypto.verify(crypto.public_key_from_x963(cp.mac_key.public_key_x963()),
                         P.b64d(wire["signature_b64"]), ch_raw)
    return approval_id, ch, ch_raw


# ---------------- tests ----------------
async def t_enrollment_one_time():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _fresh(tmp)
        token, _ = await cp.create_enrollment_token("local-owner")
        d = H.new_device("d1")
        args = dict(approval_public_key_x963_b64=P.b64e(d.appr_x963), app_attest_key_id=d.aakid)
        r1, s1 = await cp.begin_enrollment(enrollment_token=token, device_id="d1", **args)
        assert s1 == "ok"
        r2, s2 = await cp.begin_enrollment(enrollment_token=token, device_id="d2", **args)
        assert r2 is None and s2 == "already_consumed"  # one-time
        rbad, sbad = await cp.begin_enrollment(enrollment_token="nope", device_id="d3", **args)
        assert rbad is None and sbad == "unknown"
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_happy_path_and_claim_exact_action():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _fresh(tmp)
        ctx = await _enroll(cp)
        approval_id, ch, ch_raw = await _challenge(cp, ctx)
        coord = _coord(cp)
        res, s = await coord.apply_mobile_decision(**H.sign_decision(ctx, ch_raw))
        assert s == "ok" and res["decision"] == "APPROVE"
        claimed = []

        async def _exec(action):
            claimed.append(action)
            return True, {"ok": True}

        out, s2 = await coord.execute_approved(approval_id, _exec)
        assert s2 == "ok" and len(claimed) == 1
        assert claimed[0]["task"] == "edit README" and claimed[0]["workspace"] == WS
        assert (await st.get_request(approval_id))["state"] == S.CONSUMED
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_negatives():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _fresh(tmp)
        ctx = await _enroll(cp)
        approval_id, ch, ch_raw = await _challenge(cp, ctx)
        _, s = await cp.submit_decision(**_sign_decision(ctx.appr, ch, core_instance_id="core-EVIL"))
        assert s == "wrong_core_instance"
        _, s = await cp.submit_decision(**_sign_decision(ctx.appr, ch, action_digest="0" * 64))
        assert s == "action_digest_mismatch"
        _, s = await cp.submit_decision(**_sign_decision(ctx.appr, ch, principal_id="someone-else"))
        assert s == "principal_mismatch"
        evil = crypto.generate_private_key()
        _, s = await cp.submit_decision(**_sign_decision(evil, ch, key_id=ctx.key_id))
        assert s == "bad_signature"
        _, s = await cp.submit_decision(**_sign_decision(evil, ch))
        assert s == "key_mismatch"
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_nonce_replay_and_decision_replay():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _fresh(tmp)
        ctx = await _enroll(cp)
        approval_id, ch, ch_raw = await _challenge(cp, ctx)
        wire = H.sign_decision(ctx, ch_raw)
        r, s = await cp.submit_decision(**wire)
        assert s == "ok"
        r2, s2 = await cp.submit_decision(**wire)
        assert r2 is None and s2 == "not_pending"
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_revoked_device_rejected():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _fresh(tmp)
        ctx = await _enroll(cp)
        await cp.revoke_device(ctx.device_id)
        approval_id = await cp.create_request(principal="local-owner", tool="codex_task",
                                              mode="modify", task="x", workspace=WS,
                                              human_summary="x")
        wire, s = await cp.issue_challenge(approval_id=approval_id, device_id=ctx.device_id)
        assert wire is None and s == "device_inactive"
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_transport_cred_cannot_approve():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _fresh(tmp)
        await _enroll(cp)
        assert await cp.verify_transport_cred("dev-1", "tcred-1") is True
        assert await cp.verify_transport_cred("dev-1", "wrong") is False
        assert not hasattr(cp, "approve_with_transport_cred")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_durability_restart():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _fresh(tmp)
        ctx = await _enroll(cp)
        approval_id = await cp.create_request(principal="local-owner", tool="codex_task",
                                              mode="modify", task="edit README", workspace=WS,
                                              human_summary="Codex will edit README")
        await st.close()  # simulate control-plane restart
        st2 = S.ApprovalControlStore(os.path.join(tmp, "approval_control.sqlite3"))
        await st2.open()
        cp2 = C.MobileApprovalControlPlane(st2, identity.MacSigningKey.load_or_create(tmp),
                                           "core-test-1", attest_verifier=H.fake_verifier(),
                                           app_id=APP_ID)
        req = await st2.get_request(approval_id)
        assert req is not None and req["state"] == S.PENDING  # survived restart
        dev = await st2.get_device(ctx.device_id)
        assert dev["attestation_status"] == S.ATT_ATTESTED  # attestation persisted
        wire, s = await cp2.issue_challenge(approval_id=approval_id, device_id=ctx.device_id)
        assert s == "ok"
        res, s2 = await cp2.submit_decision(
            **H.sign_decision(ctx, P.b64d(wire["payload_b64"])))
        assert s2 == "ok" and res["decision"] == "APPROVE"
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_state_machine_illegal_transition():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _fresh(tmp)
        approval_id = await cp.create_request(principal="local-owner", tool="codex_task",
                                              mode="modify", task="x", workspace=WS, human_summary="x")
        async def _never(action):  # must never run
            raise AssertionError("executor ran without an approval")

        act, s = await _coord(cp).execute_approved(approval_id, _never)
        assert act is None and s == "not_approved"
        raised = False
        try:
            await st.transition(approval_id, S.CONSUMED)
        except S.IllegalTransition:
            raised = True
        assert raised
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_no_model_facing_authority():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _fresh(tmp)
        for bad in ("approve", "force_approve", "set_approved", "mark_approved"):
            assert not hasattr(cp, bad)
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
