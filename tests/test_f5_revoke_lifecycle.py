"""S2A.1 F5 — REVOKED is terminal during the enrollment lifecycle.

Confirmed bug (independent review, reproduced): `revoke_device` wrote only the `status`
column, while `_set_device_attested` blindly SET `status='ACTIVE'`. Completing an
in-flight attestation therefore resurrected a REVOKED device to ACTIVE+ATTESTED and
transport auth started working again — a terminal security action was undone by the
later completion of an enrollment that was already in flight.

Invariant pinned here:

    REVOKED IS TERMINAL.

Neither `complete_attestation`, a retry, a stale/delayed completion, a process restart,
a concurrent completion, nor a fresh `begin_enrollment` for the same device_id may bring
a revoked device back to ACTIVE. Attestation completion may PRESUPPOSE an active device;
it must never CREATE one. Deliberate re-activation is a separate owner/admin flow and is
intentionally NOT implemented.

Direct: python test_f5_revoke_lifecycle.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import mobile_attest_helper as H
from solvio.security.mobile_approval import app_attest as AA
from solvio.security.mobile_approval import control as C
from solvio.security.mobile_approval import identity
from solvio.security.mobile_approval import protocol as P
from solvio.security.mobile_approval import store as S

DB = "c.sqlite3"


async def _open(tmp):
    st = S.ApprovalControlStore(os.path.join(tmp, DB))
    await st.open()
    cp = C.MobileApprovalControlPlane(
        st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
        attest_verifier=H.fake_verifier(), app_id="TEAM.bundle")
    return st, cp


async def _begin(cp, ctx, *, principal="local-owner", transport_cred="tc"):
    token, _ = await cp.create_enrollment_token(principal)
    return await cp.begin_enrollment(
        enrollment_token=token, device_id=ctx.device_id,
        approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
        app_attest_key_id=ctx.aakid, transport_cred=transport_cred)


async def _complete(cp, ctx, enrollment_id):
    return await cp.complete_attestation(
        enrollment_id=enrollment_id,
        attestation_b64=P.b64e(AA.fake_attestation(ctx.aakey_x963)))


# ---- 1-4: revoke during PENDING_ATTESTATION wins ------------------------
async def t_revoke_then_complete_is_rejected():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = H.new_device("dev-f5-a")
        res, s = await _begin(cp, ctx)
        assert s == "ok", s
        await cp.revoke_device(ctx.device_id)
        r, s2 = await _complete(cp, ctx, res["enrollment_id"])
        assert r is None and s2 == "device_revoked", s2
        dev = await st.get_device(ctx.device_id)
        assert dev["status"] == S.DEVICE_REVOKED
        assert dev["attestation_status"] != S.ATT_ATTESTED
        # transport auth stays denied
        assert await cp.verify_transport_cred(ctx.device_id, "tc") is False
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_revoke_then_retry_complete_stays_rejected():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = H.new_device("dev-f5-b")
        res, _ = await _begin(cp, ctx)
        await cp.revoke_device(ctx.device_id)
        for _ in range(3):
            r, s = await _complete(cp, ctx, res["enrollment_id"])
            assert r is None, "retry resurrected the device"
        dev = await st.get_device(ctx.device_id)
        assert dev["status"] == S.DEVICE_REVOKED
        assert await cp.verify_transport_cred(ctx.device_id, "tc") is False
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_complete_then_revoke_ends_revoked():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = H.new_device("dev-f5-c")
        res, _ = await _begin(cp, ctx)
        r, s = await _complete(cp, ctx, res["enrollment_id"])
        assert s == "ok", s
        assert await cp.verify_transport_cred(ctx.device_id, "tc") is True
        await cp.revoke_device(ctx.device_id)
        dev = await st.get_device(ctx.device_id)
        assert dev["status"] == S.DEVICE_REVOKED
        assert await cp.verify_transport_cred(ctx.device_id, "tc") is False
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_restart_then_stale_completion_stays_rejected():
    """Durable state must carry the revoke across a full process restart."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = H.new_device("dev-f5-d")
        res, _ = await _begin(cp, ctx)
        await cp.revoke_device(ctx.device_id)
        await st.close()                      # restart: drop all in-memory state
        st2, cp2 = await _open(tmp)
        r, s = await _complete(cp2, ctx, res["enrollment_id"])
        assert r is None, f"stale completion after restart resurrected the device ({s})"
        dev = await st2.get_device(ctx.device_id)
        assert dev["status"] == S.DEVICE_REVOKED
        assert await cp2.verify_transport_cred(ctx.device_id, "tc") is False
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- re-enrolling a revoked device_id must not reset it -----------------
async def t_reenrollment_of_revoked_device_is_rejected():
    """Without this, revoke -> begin -> complete would defeat the guarded completion."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = H.new_device("dev-f5-e")
        await _begin(cp, ctx)
        await cp.revoke_device(ctx.device_id)
        res, s = await _begin(cp, ctx)        # same device_id again
        assert res is None and s == "device_revoked", s
        dev = await st.get_device(ctx.device_id)
        assert dev["status"] == S.DEVICE_REVOKED
        assert dev["attestation_status"] != S.ATT_ATTESTED
        assert await cp.verify_transport_cred(ctx.device_id, "tc") is False
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- concurrency: never final ACTIVE after a committed revoke -----------
async def t_concurrent_revoke_and_complete_never_ends_active():
    """Race revoke against completion repeatedly. Either order is acceptable, but a
    device that was revoked must never end up ACTIVE."""
    for attempt in range(25):
        tmp = tempfile.mkdtemp()
        try:
            st, cp = await _open(tmp)
            ctx = H.new_device(f"dev-f5-race-{attempt}")
            res, _ = await _begin(cp, ctx)
            loop = asyncio.get_running_loop()
            barrier = threading.Barrier(2)

            async def do_revoke():
                await loop.run_in_executor(None, barrier.wait)
                await cp.revoke_device(ctx.device_id)

            async def do_complete():
                await loop.run_in_executor(None, barrier.wait)
                return await _complete(cp, ctx, res["enrollment_id"])

            done, _p = await asyncio.gather(do_complete(), do_revoke())
            dev = await st.get_device(ctx.device_id)
            # revoke was always issued -> the device must be REVOKED, whoever won
            assert dev["status"] == S.DEVICE_REVOKED, \
                f"attempt {attempt}: ended {dev['status']} after a committed revoke"
            assert await cp.verify_transport_cred(ctx.device_id, "tc") is False
            await st.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---- the normal path must be untouched ---------------------------------
async def t_normal_enrollment_without_revoke_still_works():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = H.new_device("dev-f5-ok")
        res, s = await _begin(cp, ctx)
        assert s == "ok", s
        r, s2 = await _complete(cp, ctx, res["enrollment_id"])
        assert s2 == "ok", s2
        dev = await st.get_device(ctx.device_id)
        assert dev["status"] == S.DEVICE_ACTIVE
        assert dev["attestation_status"] == S.ATT_ATTESTED
        assert dev["attested"] == 1 and dev["app_attest_public_key"]
        assert await cp.verify_transport_cred(ctx.device_id, "tc") is True
        # a second completion of the same (now consumed) enrollment is refused
        r2, _ = await _complete(cp, ctx, res["enrollment_id"])
        assert r2 is None
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
