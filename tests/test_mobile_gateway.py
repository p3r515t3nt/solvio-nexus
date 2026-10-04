"""Approval gateway handler tests (STEP S2A, attested S2A.1), in-process aiohttp TestClient
(no TLS). Two-step enrollment (begin + App Attest complete); APPROVE carries both proofs.
Direct: python test_mobile_gateway.py."""
import asyncio
import hashlib
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import mobile_attest_helper as H
from aiohttp.test_utils import TestClient, TestServer

from solvio.security.approval import ApprovalBroker
from solvio.security.mobile_approval import app_attest as AA
from solvio.security.mobile_approval import bridge as B
from solvio.security.mobile_approval import control as C
from solvio.security.mobile_approval import crypto
from solvio.security.mobile_approval import gateway as G
from solvio.security.mobile_approval import identity
from solvio.security.mobile_approval import protocol as P
from solvio.security.mobile_approval import store as S

TCRED = "transport-cred-123"
APP_ID = "WQ8CG7R53R.de.solvio.approvals"


async def _setup(tmp):
    st = S.ApprovalControlStore(os.path.join(tmp, "approval_control.sqlite3"))
    await st.open()
    cp = C.MobileApprovalControlPlane(st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
                                      attest_verifier=H.fake_verifier(), app_id=APP_ID)
    approver = B.MobileApprover()
    coord = B.MobileApprovalCoordinator(cp, ApprovalBroker(approver=approver), approver)
    client = TestClient(TestServer(G.build_app(control_plane=cp, coordinator=coord)))
    await client.start_server()
    return st, cp, coord, client


async def _enroll(cp, client, device_id="dev-1", principal="local-owner"):
    """Two-step HTTP enrollment: begin, then App Attest complete (fake attestation)."""
    token, _ = await cp.create_enrollment_token(principal)
    ctx = H.new_device(device_id)
    r = await client.post("/v1/enroll/begin", json={
        "enrollment_token": token, "device_id": device_id,
        "approval_public_key_x963_b64": P.b64e(ctx.appr_x963),
        "app_attest_key_id": ctx.aakid, "transport_cred": TCRED})
    assert r.status == 200, await r.text()
    enrollment_id = (await r.json())["enrollment_id"]
    r2 = await client.post("/v1/enroll/complete", json={
        "enrollment_id": enrollment_id,
        "attestation_b64": P.b64e(AA.fake_attestation(ctx.aakey_x963))})
    assert r2.status == 200, await r2.text()
    return ctx


def _hdr(device_id="dev-1", cred=TCRED):
    return {"X-Device-Id": device_id, "X-Transport-Cred": cred}


async def t_enroll_and_authed_read():
    tmp = tempfile.mkdtemp()
    try:
        st, cp, coord, client = await _setup(tmp)
        await _enroll(cp, client)
        r = await client.get("/v1/approvals")
        assert r.status == 401
        r = await client.get("/v1/approvals", headers=_hdr(cred="wrong"))
        assert r.status == 401
        await coord.request_codex_modify(principal="local-owner", task="edit README",
                                         workspace="/tmp/ws", human_summary="s")
        r = await client.get("/v1/approvals", headers=_hdr())
        assert r.status == 200
        body = await r.json()
        assert len(body["approvals"]) == 1
        assert "signature" not in str(body) and "nonce" not in str(body)
        await client.close(); await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_full_http_flow_challenge_decision_status():
    tmp = tempfile.mkdtemp()
    try:
        st, cp, coord, client = await _setup(tmp)
        ctx = await _enroll(cp, client)
        approval_id = await coord.request_codex_modify(
            principal="local-owner", task="edit README", workspace="/tmp/ws", human_summary="s")
        r = await client.post(f"/v1/approvals/{approval_id}/challenge", headers=_hdr())
        assert r.status == 200
        ch_raw = P.b64d((await r.json())["payload_b64"])
        wire = H.sign_decision(ctx, ch_raw)  # Face-ID signature + App Attest assertion
        r = await client.post(f"/v1/approvals/{approval_id}/decision", json=wire)
        assert r.status == 200 and (await r.json())["decision"] == "APPROVE", await r.text()
        r = await client.get(f"/v1/approvals/{approval_id}/status", headers=_hdr())
        assert r.status == 200 and (await r.json())["state"] == S.APPROVED
        await client.close(); await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_forged_decision_rejected_over_http():
    tmp = tempfile.mkdtemp()
    try:
        st, cp, coord, client = await _setup(tmp)
        ctx = await _enroll(cp, client)
        approval_id = await coord.request_codex_modify(
            principal="local-owner", task="x", workspace="/tmp/ws", human_summary="s")
        r = await client.post(f"/v1/approvals/{approval_id}/challenge", headers=_hdr())
        ch_raw2 = P.b64d((await r.json())["payload_b64"])
        ch = P.strict_parse(ch_raw2)
        evil = crypto.generate_private_key()  # attacker signs but claims the enrolled key_id
        payload = P.build_decision_payload(
            core_instance_id=ch["core_instance_id"], approval_id=ch["approval_id"],
            action_digest=ch["action_digest"], principal_id=ch["principal_id"],
            device_id="dev-1", key_id=ctx.key_id, challenge_nonce=ch["challenge_nonce"],
            challenge_payload_sha256=hashlib.sha256(ch_raw2).hexdigest(),
            decision=P.DECISION_APPROVE, issued_at=int(time.time()),
            challenge_expires_at=ch["expires_at"])
        raw = P.canonical_bytes(payload)
        r = await client.post(f"/v1/approvals/{approval_id}/decision", json={
            "payload_b64": P.b64e(raw), "signature_b64": P.b64e(crypto.sign(evil, raw)),
            "key_id": ctx.key_id})
        assert r.status == 403 and (await r.json())["error"] == "bad_signature"
        r = await client.get(f"/v1/approvals/{approval_id}/status", headers=_hdr())
        assert (await r.json())["state"] == S.PENDING
        await client.close(); await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
