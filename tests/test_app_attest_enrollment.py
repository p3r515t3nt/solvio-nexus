"""App Attest enrollment + two-proof decision control-plane tests (STEP S2A.1).

The Apple crypto itself is covered end-to-end by test_app_attest.py (real verifier vs
synthetic attestations). Here we drive the CONTROL-PLANE state machine + policy with the
FakeAppAttestVerifier (real ECDSA binding to clientDataHash + counter, no Apple chain):
two-step enrollment, attestation gating, the App Attest assertion as a mandatory 2nd proof
for APPROVE, counter anti-replay, and fail-closed behaviour. Direct: python <file>.
"""
import asyncio
import base64
import hashlib
import os
import shutil
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from solvio.security.approval import ApprovalBroker
from solvio.security.mobile_approval import app_attest as AA
from solvio.security.mobile_approval import bridge as B
from solvio.security.mobile_approval import attest_protocol as AP
from solvio.security.mobile_approval import control as C
from solvio.security.mobile_approval import crypto
from solvio.security.mobile_approval import identity
from solvio.security.mobile_approval import protocol as P
from solvio.security.mobile_approval import store as S

APP_ID = "WQ8CG7R53R.de.solvio.approvals"


def _aa_key():
    k = ec.generate_private_key(ec.SECP256R1())
    x963 = k.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    key_id_b64 = base64.b64encode(__import__("hashlib").sha256(x963).digest()).decode()
    return k, x963, key_id_b64


async def _mk_cp(tmp, *, attest_ttl=300.0):
    st = S.ApprovalControlStore(os.path.join(tmp, "approval_control.sqlite3"))
    await st.open()
    cp = C.MobileApprovalControlPlane(
        st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
        attest_verifier=AA.FakeAppAttestVerifier(environment=AA.ENV_DEVELOPMENT),
        app_id=APP_ID, attest_ttl=attest_ttl)
    return st, cp


async def _enroll_attest(cp, *, device_id="dev-1", principal="local-owner", attest=True,
                         fail_attest=False):
    token, _ = await cp.create_enrollment_token(principal)
    appr = crypto.generate_private_key()
    appr_x963 = crypto.public_key_x963(appr)
    aakey, aakey_x963, aakid = _aa_key()
    res, st = await cp.begin_enrollment(
        enrollment_token=token, device_id=device_id,
        approval_public_key_x963_b64=P.b64e(appr_x963), app_attest_key_id=aakid,
        transport_cred="tcred")
    assert st == "ok", st
    ctx = types.SimpleNamespace(appr=appr, appr_x963=appr_x963, aakey=aakey,
                                aakey_x963=aakey_x963, aakid=aakid, device_id=device_id,
                                enrollment_id=res["enrollment_id"], binding_b64=res["binding_b64"])
    if attest:
        att = AA.fake_attestation(aakey_x963, fail=fail_attest)
        r2, s2 = await cp.complete_attestation(enrollment_id=res["enrollment_id"],
                                               attestation_b64=P.b64e(att))
        ctx.attest_result = (r2, s2)
    return ctx


async def _request(cp, *, principal="local-owner", task="edit main.py"):
    return await cp.create_request(principal=principal, tool="codex_task", mode="modify",
                                   task=task, workspace="/Users/solvio/solvio-core",
                                   human_summary="modify main.py")


async def _decide(cp, ctx, approval_id, *, approve=True, counter=1, tamper_nonce=False,
                  key_override=None):
    wire, st = await cp.issue_challenge(approval_id=approval_id, device_id=ctx.device_id)
    if wire is None:
        return None, "challenge_" + st
    ch = P.strict_parse(P.b64d(wire["payload_b64"]))
    kid = key_override or crypto.key_id(ctx.appr_x963)
    decision = P.build_decision_payload(
        core_instance_id=ch["core_instance_id"], approval_id=ch["approval_id"],
        action_digest=ch["action_digest"], principal_id=ch["principal_id"],
        device_id=ctx.device_id, key_id=kid, challenge_nonce=ch["challenge_nonce"],
        challenge_payload_sha256=hashlib.sha256(P.canonical_bytes(ch)).hexdigest(),
        decision=P.DECISION_APPROVE if approve else P.DECISION_DENY,
        issued_at=int(time.time()), challenge_expires_at=ch["expires_at"])
    raw = P.canonical_bytes(decision)
    sig = crypto.sign(ctx.appr, raw)
    assertion_b64 = None
    if approve:
        nonce = "deadbeef" if tamper_nonce else ch["challenge_nonce"]
        db = AP.build_decision_binding(
            core_instance_id=ch["core_instance_id"], approval_id=ch["approval_id"],
            device_id=ctx.device_id, decision_sha256=AP.sha256_hex(raw),
            challenge_nonce=nonce, approval_public_key_sha256=crypto.fingerprint(ctx.appr_x963))
        cdh = AP.decision_client_data_hash(P.canonical_bytes(db))
        assertion_b64 = P.b64e(AA.fake_assertion(ctx.aakey, cdh, counter))
    return await cp.submit_decision(payload_b64=P.b64e(raw), signature_b64=P.b64e(sig),
                                    key_id=kid, assertion_b64=assertion_b64)


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ tests --
def test_two_proof_approve_happy_path():
    async def go():
        d = tempfile.mkdtemp()
        try:
            st, cp = await _mk_cp(d)
            ctx = await _enroll_attest(cp)
            assert ctx.attest_result[1] == "ok"
            dev = await st.get_device(ctx.device_id)
            assert dev["attestation_status"] == S.ATT_ATTESTED and dev["environment"] == "development"
            ap = await _request(cp)
            res, s = await _decide(cp, ctx, ap, approve=True, counter=1)
            assert s == "ok" and res["decision"] == "APPROVE", (res, s)
            assert (await st.get_request(ap))["state"] == S.APPROVED
            # P1A/F3: execution only via the S1-gated coordinator.
            req = await st.get_request(ap)
            assert req["tool"] == "codex_task" and req["mode"] == "modify"
        finally:
            await st.close(); shutil.rmtree(d, ignore_errors=True)
    _run(go())


def test_deny_needs_no_assertion():
    async def go():
        d = tempfile.mkdtemp()
        try:
            st, cp = await _mk_cp(d)
            ctx = await _enroll_attest(cp)
            ap = await _request(cp)
            res, s = await _decide(cp, ctx, ap, approve=False)
            assert s == "ok" and res["decision"] == "DENY"
            assert (await st.get_request(ap))["state"] == S.DENIED
        finally:
            await st.close(); shutil.rmtree(d, ignore_errors=True)
    _run(go())


def test_unattested_device_cannot_approve():
    async def go():
        d = tempfile.mkdtemp()
        try:
            st, cp = await _mk_cp(d)
            ctx = await _enroll_attest(cp, attest=False)     # PENDING_ATTESTATION only
            ap = await _request(cp)
            res, s = await _decide(cp, ctx, ap, approve=True)
            assert res is None and s == "challenge_device_inactive", s
        finally:
            await st.close(); shutil.rmtree(d, ignore_errors=True)
    _run(go())


def test_invalid_attestation_marks_failed():
    async def go():
        d = tempfile.mkdtemp()
        try:
            st, cp = await _mk_cp(d)
            ctx = await _enroll_attest(cp, fail_attest=True)
            assert ctx.attest_result == (None, "attestation_failed")
            dev = await st.get_device(ctx.device_id)
            assert dev["attestation_status"] == S.ATT_FAILED
        finally:
            await st.close(); shutil.rmtree(d, ignore_errors=True)
    _run(go())


def test_approve_without_assertion_reject():
    async def go():
        d = tempfile.mkdtemp()
        try:
            st, cp = await _mk_cp(d)
            ctx = await _enroll_attest(cp)
            ap = await _request(cp)
            wire, _ = await cp.issue_challenge(approval_id=ap, device_id=ctx.device_id)
            ch = P.strict_parse(P.b64d(wire["payload_b64"]))
            decision = P.build_decision_payload(
                core_instance_id=ch["core_instance_id"], approval_id=ap,
                action_digest=ch["action_digest"], principal_id=ch["principal_id"],
                device_id=ctx.device_id, key_id=crypto.key_id(ctx.appr_x963),
                challenge_nonce=ch["challenge_nonce"],
                challenge_payload_sha256=hashlib.sha256(P.canonical_bytes(ch)).hexdigest(),
                decision=P.DECISION_APPROVE,
                issued_at=int(time.time()), challenge_expires_at=ch["expires_at"])
            raw = P.canonical_bytes(decision)
            res, s = await cp.submit_decision(payload_b64=P.b64e(raw),
                signature_b64=P.b64e(crypto.sign(ctx.appr, raw)),
                key_id=crypto.key_id(ctx.appr_x963), assertion_b64=None)
            assert res is None and s == "assertion_required", s
        finally:
            await st.close(); shutil.rmtree(d, ignore_errors=True)
    _run(go())


def test_bad_assertion_reject():
    async def go():
        d = tempfile.mkdtemp()
        try:
            st, cp = await _mk_cp(d)
            ctx = await _enroll_attest(cp)
            ap = await _request(cp)
            res, s = await _decide(cp, ctx, ap, approve=True, tamper_nonce=True)
            assert res is None and s == "bad_assertion", s
            assert (await st.get_request(ap))["state"] == S.PENDING   # challenge not burned
        finally:
            await st.close(); shutil.rmtree(d, ignore_errors=True)
    _run(go())


def test_assertion_counter_replay_reject():
    async def go():
        d = tempfile.mkdtemp()
        try:
            st, cp = await _mk_cp(d)
            ctx = await _enroll_attest(cp)
            ap1 = await _request(cp)
            r1, s1 = await _decide(cp, ctx, ap1, approve=True, counter=1)
            assert s1 == "ok"
            ap2 = await _request(cp)
            r2, s2 = await _decide(cp, ctx, ap2, approve=True, counter=1)  # counter not > 1
            assert r2 is None and s2 == "bad_assertion", s2
        finally:
            await st.close(); shutil.rmtree(d, ignore_errors=True)
    _run(go())


def test_wrong_principal_reject():
    async def go():
        d = tempfile.mkdtemp()
        try:
            st, cp = await _mk_cp(d)
            ctx = await _enroll_attest(cp, device_id="dev-A", principal="owner-A")
            ap = await cp.create_request(principal="owner-B", tool="codex_task", mode="modify",
                                         task="t", workspace="/w", human_summary="h")
            res, s = await _decide(cp, ctx, ap, approve=True)
            assert res is None and s == "challenge_principal_mismatch", s
        finally:
            await st.close(); shutil.rmtree(d, ignore_errors=True)
    _run(go())


def test_expired_attestation_challenge_reject():
    async def go():
        d = tempfile.mkdtemp()
        try:
            st, cp = await _mk_cp(d, attest_ttl=-1.0)     # already expired
            ctx = await _enroll_attest(cp, attest=False)
            att = AA.fake_attestation(ctx.aakey_x963)
            r, s = await cp.complete_attestation(enrollment_id=ctx.enrollment_id,
                                                 attestation_b64=P.b64e(att))
            assert r is None and s == "challenge_expired", s
        finally:
            await st.close(); shutil.rmtree(d, ignore_errors=True)
    _run(go())


def test_replayed_enrollment_token_reject():
    async def go():
        d = tempfile.mkdtemp()
        try:
            st, cp = await _mk_cp(d)
            token, _ = await cp.create_enrollment_token("local-owner")
            appr_x963 = crypto.public_key_x963(crypto.generate_private_key())
            _, _, aakid = _aa_key()
            r1, s1 = await cp.begin_enrollment(enrollment_token=token, device_id="d1",
                approval_public_key_x963_b64=P.b64e(appr_x963), app_attest_key_id=aakid)
            r2, s2 = await cp.begin_enrollment(enrollment_token=token, device_id="d2",
                approval_public_key_x963_b64=P.b64e(appr_x963), app_attest_key_id=aakid)
            assert s1 == "ok" and r2 is None and s2 == "already_consumed", (s1, s2)
        finally:
            await st.close(); shutil.rmtree(d, ignore_errors=True)
    _run(go())


def test_revoked_device_cannot_approve():
    async def go():
        d = tempfile.mkdtemp()
        try:
            st, cp = await _mk_cp(d)
            ctx = await _enroll_attest(cp)
            await cp.revoke_device(ctx.device_id)
            ap = await _request(cp)
            res, s = await _decide(cp, ctx, ap, approve=True)
            assert res is None and s == "challenge_device_inactive", s
        finally:
            await st.close(); shutil.rmtree(d, ignore_errors=True)
    _run(go())


def test_re_enroll_resets_to_pending_then_attests():
    async def go():
        d = tempfile.mkdtemp()
        try:
            st, cp = await _mk_cp(d)
            ctx = await _enroll_attest(cp, device_id="dev-R")
            assert (await st.get_device("dev-R"))["attestation_status"] == S.ATT_ATTESTED
            ctx2 = await _enroll_attest(cp, device_id="dev-R")   # new token, re-enroll
            assert ctx2.attest_result[1] == "ok"
            assert (await st.get_device("dev-R"))["attestation_status"] == S.ATT_ATTESTED
        finally:
            await st.close(); shutil.rmtree(d, ignore_errors=True)
    _run(go())


def test_transport_cred_requires_attested():
    async def go():
        d = tempfile.mkdtemp()
        try:
            st, cp = await _mk_cp(d)
            ctx = await _enroll_attest(cp, attest=False)
            assert await cp.verify_transport_cred(ctx.device_id, "tcred") is False  # PENDING
            att = AA.fake_attestation(ctx.aakey_x963)
            await cp.complete_attestation(enrollment_id=ctx.enrollment_id,
                                          attestation_b64=P.b64e(att))
            assert await cp.verify_transport_cred(ctx.device_id, "tcred") is True    # ATTESTED
            assert await cp.verify_transport_cred(ctx.device_id, "wrong") is False
        finally:
            await st.close(); shutil.rmtree(d, ignore_errors=True)
    _run(go())


def test_no_attest_verifier_fail_closed():
    async def go():
        d = tempfile.mkdtemp()
        try:
            st = S.ApprovalControlStore(os.path.join(d, "approval_control.sqlite3"))
            await st.open()
            cp = C.MobileApprovalControlPlane(st, identity.MacSigningKey.load_or_create(d),
        identity.load_or_create_core_instance_id(d), attest_verifier=None, app_id=APP_ID)
            r, s = await cp.complete_attestation(enrollment_id="enr-x", attestation_b64="AA==")
            assert r is None and s == "no_attest_verifier", s
        finally:
            await st.close(); shutil.rmtree(d, ignore_errors=True)
    _run(go())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
