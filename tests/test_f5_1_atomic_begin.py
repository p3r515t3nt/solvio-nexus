"""S2A.1 F5.1 — the begin-enrollment revoke guard must be SQL-atomic, not executor-atomic.

The first F5 fix made attestation COMPLETION atomic but left enrollment BEGIN as a
check-then-act pair:

    SELECT status ...            <-- connection A sees "not revoked"
                                 <-- connection B commits REVOKED
    INSERT OR REPLACE ... ACTIVE <-- connection A clobbers the revoke

Under autocommit (isolation_level=None) that lost the revoke, and a subsequent
complete_attestation then legitimately produced ACTIVE + ATTESTED again. Reproduced
deterministically before the fix.

These tests pin the invariant at the SQL layer, using SEPARATE connections so the store's
ThreadPoolExecutor(max_workers=1) cannot be what makes them pass:

    REVOKED IS TERMINAL — no enrollment path may ever reactivate a REVOKED device_id.

Direct: python test_f5_1_atomic_begin.py
"""
import asyncio
import os
import shutil
import sqlite3
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


async def _open(tmp, name="c.sqlite3"):
    st = S.ApprovalControlStore(os.path.join(tmp, name))
    await st.open()
    cp = C.MobileApprovalControlPlane(
        st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
        attest_verifier=H.fake_verifier(), app_id="TEAM.bundle")
    return st, cp


async def _begin(cp, ctx, transport_cred="tc"):
    token, _ = await cp.create_enrollment_token("local-owner")
    return await cp.begin_enrollment(
        enrollment_token=token, device_id=ctx.device_id,
        approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
        app_attest_key_id=ctx.aakid, transport_cred=transport_cred)


def _raw(tmp):
    """A SECOND, independent connection to the same DB file — the store's single worker
    cannot serialize this one."""
    con = sqlite3.connect(os.path.join(tmp, "c.sqlite3"), isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


# ---- the exact interleaving that defeated the previous guard --------------
async def t_deterministic_revoke_between_read_and_write_is_not_clobbered():
    """The old guard read `status` in Python and then wrote. Recreate exactly that
    interleaving — read sees ACTIVE, a SECOND connection commits REVOKED, then the write
    runs — and require the write to refuse.

    P1A.1/F4: this drives the REAL production store method, rather than a copy of the
    production upsert pasted into the test body.

    P1A.6/§15 — HONEST SCOPE. This suite does NOT defend the SQL predicate
    `WHERE devices.status <> 'REVOKED'` on its own. Since P1A.4/§10 the enrollment
    transaction consults the `revocations` table first, and every path here revokes via
    `cp.revoke_device()`, which writes such a row — so the identity pre-check refuses before
    the upsert guard is ever reached, and deleting that guard leaves this suite 6/6 green
    (verified by mutation). The predicate's actual regression coverage is
    `test_p1a_5_blocker_remediation.py::t_h5_*`, which builds the legacy shape
    (status=REVOKED with NO revocations row) so nothing can mask it.
    """
    tmp = tempfile.mkdtemp()
    try:
        stA, cpA = await _open(tmp)
        stB, cpB = await _open(tmp)          # independent connection + own worker thread
        ctx = H.new_device("dev-f51-det")
        await _begin(cpA, ctx)

        # 1. the "check" half of the old check-then-act: connection A sees a live device
        assert (await stA.get_device(ctx.device_id))["status"] == S.DEVICE_ACTIVE

        # 2. connection B commits the revoke while A still believes what it read
        await cpB.revoke_device(ctx.device_id)

        # 3. the "act" half, through the SHIPPING code path. This is the write that used
        #    to clobber the revoke; it must now refuse on its own.
        wrote = await H.store_begin(stA, ctx)
        assert wrote != "ok", "production begin_enrollment_atomic overwrote a REVOKED row"

        for st in (stA, stB):                # both connections must agree, no caching alibi
            assert (await st.get_device(ctx.device_id))["status"] == S.DEVICE_REVOKED
        assert await cpA.verify_transport_cred(ctx.device_id, "tc") is False
        await stA.close(); await stB.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_cross_connection_begin_vs_revoke_never_reactivates():
    """Race a real begin_enrollment (store A) against a real revoke (store B) on the SAME
    database file, barrier-synchronised, many rounds. A committed revoke must always win."""
    tmp = tempfile.mkdtemp()
    try:
        stA, cpA = await _open(tmp)
        stB, cpB = await _open(tmp)          # independent connection + own worker thread
        loop = asyncio.get_running_loop()
        reactivated = 0
        for i in range(60):
            ctx = H.new_device(f"dev-f51-race-{i}")
            await _begin(cpA, ctx)
            token, _ = await cpA.create_enrollment_token("local-owner")
            barrier = threading.Barrier(2)

            async def do_begin():
                await loop.run_in_executor(None, barrier.wait)
                return await cpA.begin_enrollment(
                    enrollment_token=token, device_id=ctx.device_id,
                    approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
                    app_attest_key_id=ctx.aakid, transport_cred="tc")

            async def do_revoke():
                await loop.run_in_executor(None, barrier.wait)
                await cpB.revoke_device(ctx.device_id)

            await asyncio.gather(do_begin(), do_revoke(), return_exceptions=True)
            dev = await stA.get_device(ctx.device_id)
            if dev["status"] == S.DEVICE_ACTIVE:
                # A begin that won the race outright is fine ONLY if the revoke landed
                # first; re-assert by revoking again and confirming terminality.
                await cpB.revoke_device(ctx.device_id)
                if (await stA.get_device(ctx.device_id))["status"] != S.DEVICE_REVOKED:
                    reactivated += 1
        assert reactivated == 0, f"{reactivated} devices reactivated after a committed revoke"
        await stA.close(); await stB.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_revoked_then_begin_from_second_connection_is_rejected():
    """The public API, driven from a second store object on the same file."""
    tmp = tempfile.mkdtemp()
    try:
        stA, cpA = await _open(tmp)
        stB, cpB = await _open(tmp)
        ctx = H.new_device("dev-f51-2conn")
        await _begin(cpA, ctx)
        await cpA.revoke_device(ctx.device_id)
        res, s = await _begin(cpB, ctx)                     # other connection re-enrolls
        assert res is None and s == "device_revoked", s
        assert (await stB.get_device(ctx.device_id))["status"] == S.DEVICE_REVOKED
        assert await cpB.verify_transport_cred(ctx.device_id, "tc") is False
        await stA.close(); await stB.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- the completion guard must stay exactly as strong --------------------
async def t_completion_guard_still_atomic_across_connections():
    tmp = tempfile.mkdtemp()
    try:
        stA, cpA = await _open(tmp)
        stB, cpB = await _open(tmp)
        ctx = H.new_device("dev-f51-compl")
        res, _ = await _begin(cpA, ctx)
        await cpB.revoke_device(ctx.device_id)              # revoke from the other conn
        r, s = await cpA.complete_attestation(
            enrollment_id=res["enrollment_id"],
            attestation_b64=P.b64e(AA.fake_attestation(ctx.aakey_x963)))
        assert r is None and s == "device_revoked", s
        assert (await stA.get_device(ctx.device_id))["status"] == S.DEVICE_REVOKED
        await stA.close(); await stB.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- re-enrollment must not inherit stale attestation material -----------
async def t_reenrollment_clears_stale_attestation_state():
    """The old INSERT OR REPLACE reset these implicitly; the upsert must do it explicitly."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = H.new_device("dev-f51-reenroll")
        res, _ = await _begin(cp, ctx)
        r, s = await cp.complete_attestation(
            enrollment_id=res["enrollment_id"],
            attestation_b64=P.b64e(AA.fake_attestation(ctx.aakey_x963)))
        assert s == "ok", s
        before = await st.get_device(ctx.device_id)
        assert before["attestation_status"] == S.ATT_ATTESTED and before["app_attest_public_key"]
        # a NEW enrollment for the same (still ACTIVE) device must start clean
        res2, s2 = await _begin(cp, ctx)
        assert s2 == "ok", s2
        after = await st.get_device(ctx.device_id)
        assert after["attestation_status"] == S.ATT_PENDING
        assert after["attested"] == 0
        assert after["app_attest_public_key"] is None, "stale app-attest key survived"
        assert after["attested_at"] is None and after["app_id"] is None
        assert after["environment"] is None
        assert after["app_attest_counter"] == 0, "stale counter survived"
        assert await cp.verify_transport_cred(ctx.device_id, "tc") is False  # not attested yet
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_normal_enrollment_unaffected():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = H.new_device("dev-f51-ok")
        res, s = await _begin(cp, ctx)
        assert s == "ok", s
        r, s2 = await cp.complete_attestation(
            enrollment_id=res["enrollment_id"],
            attestation_b64=P.b64e(AA.fake_attestation(ctx.aakey_x963)))
        assert s2 == "ok", s2
        dev = await st.get_device(ctx.device_id)
        assert dev["status"] == S.DEVICE_ACTIVE and dev["attestation_status"] == S.ATT_ATTESTED
        assert await cp.verify_transport_cred(ctx.device_id, "tc") is True
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
