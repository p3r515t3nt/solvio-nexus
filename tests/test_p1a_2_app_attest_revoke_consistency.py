"""P1A.2 — app-attest-key revocation must leave a CONSISTENT state.

Found during the final P1A.1 verification: revoke_device and revoke_approval_key both wrote
the durable revocation AND moved the affected device records to REVOKED. revoke_app_attest_key
wrote only the revocation row. Authority was still blocked everywhere — every authority path
consults the `revocations` table — but the device records kept reporting ACTIVE.

That is not a hole, it is a contradiction: the operator listing said "fine" about a device
that could no longer approve anything. Same class of false assurance as the P1A.1/F1 prefix
bug, and exactly the sort of thing that gets trusted during an incident.

Two properties are wanted, and these tests keep them separate on purpose:

    revocations table  = canonical, durable identity blocklist  (authority)
    devices.status     = device lifecycle / operator state      (consistency)

Nothing here may be read as "ACTIVE means authorized" — authority is decided by the
revocation checks, not by the status column.

Direct: python test_p1a_2_app_attest_revoke_consistency.py
"""
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import mobile_attest_helper as H
from solvio.security.approval import ApprovalBroker
from solvio.security.mobile_approval import admin_cli
from solvio.security.mobile_approval import app_attest as AA
from solvio.security.mobile_approval import bridge as B
from solvio.security.mobile_approval import control as C
from solvio.security.mobile_approval import identity
from solvio.security.mobile_approval import protocol as P
from solvio.security.mobile_approval import store as S

APP_ID = "WQ8CG7R53R.de.solvio.approvals"
DB = "approval_control.sqlite3"


async def _open(tmp):
    st = S.ApprovalControlStore(os.path.join(tmp, DB))
    await st.open()
    cp = C.MobileApprovalControlPlane(
        st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
        attest_verifier=H.fake_verifier(), app_id=APP_ID)
    return st, cp


async def _wire(tmp):
    st, cp = await _open(tmp)
    approver = B.MobileApprover()
    broker = ApprovalBroker(approver=approver)
    return st, cp, B.MobileApprovalCoordinator(cp, broker, approver), approver


async def _enroll_sharing_key(cp, ctx, device_id, *, transport_cred="tc"):
    """Enroll ANOTHER device record bound to the SAME app-attest key. Phase 1 established
    that app_attest_key_id carries no UNIQUE constraint, so this is reachable in production
    — not a synthetic row poked into the table."""
    token, _ = await cp.create_enrollment_token("local-owner")
    res, s = await cp.begin_enrollment(
        enrollment_token=token, device_id=device_id,
        approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
        app_attest_key_id=ctx.aakid, transport_cred=transport_cred)
    assert s == "ok", s
    _, s2 = await cp.complete_attestation(
        enrollment_id=res["enrollment_id"],
        attestation_b64=P.b64e(AA.fake_attestation(ctx.aakey_x963)))
    assert s2 == "ok", s2


def _executor(calls):
    async def run(action):
        calls.append(action)
        return True, {"changed_files": []}
    return run


# ---- 1 + 2: durable revocation AND device lifecycle state ----------------
async def t_revoke_writes_durable_revocation_entry():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        await cp.revoke_app_attest_key(ctx.aakid, reason="compromised app")
        rows = [r for r in await st.list_revocations()
                if r["kind"] == S.REVOKE_APP_ATTEST_KEY]
        assert len(rows) == 1 and rows[0]["value"] == ctx.aakid, rows
        assert rows[0]["reason"] == "compromised app"
        # the canonical blocklist is what authority consults — assert it directly
        assert await st.is_revoked(S.REVOKE_APP_ATTEST_KEY, ctx.aakid) is True
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_revoke_sets_bound_device_to_revoked():
    """The actual finding: this used to stay ACTIVE."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        assert (await st.get_device(ctx.device_id))["status"] == S.DEVICE_ACTIVE
        affected = len((await cp.revoke_app_attest_key(ctx.aakid, reason="compromised app")).affected_devices)
        assert affected == 1, affected
        assert (await st.get_device(ctx.device_id))["status"] == S.DEVICE_REVOKED
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 3: several device records may share one app-attest key -------------
async def t_all_devices_bound_to_the_key_are_revoked():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        await _enroll_sharing_key(cp, ctx, "dev-2")
        await _enroll_sharing_key(cp, ctx, "dev-3")
        for d in ("dev-1", "dev-2", "dev-3"):
            assert (await st.get_device(d))["status"] == S.DEVICE_ACTIVE

        affected = len((await cp.revoke_app_attest_key(ctx.aakid, reason="compromised app")).affected_devices)
        assert affected == 3, affected
        for d in ("dev-1", "dev-2", "dev-3"):
            assert (await st.get_device(d))["status"] == S.DEVICE_REVOKED, d
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 4: blast radius --------------------------------------------------
async def t_unrelated_device_is_untouched():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        a = await H.enroll_attested(cp, device_id="dev-A", transport_cred="ta")
        b = await H.enroll_attested(cp, device_id="dev-B", transport_cred="tb")
        assert a.aakid != b.aakid
        before = dict(await st.get_device("dev-B"))

        await cp.revoke_app_attest_key(a.aakid, reason="only A")
        assert (await st.get_device("dev-A"))["status"] == S.DEVICE_REVOKED
        after = dict(await st.get_device("dev-B"))
        assert after == before, "an unrelated device record changed"
        assert await cp.verify_transport_cred("dev-B", "tb") is True
        assert await st.is_revoked(S.REVOKE_APP_ATTEST_KEY, b.aakid) is False
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 5-7: authority paths stay closed ----------------------------------
async def t_transport_after_revoke_is_denied():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        assert await cp.verify_transport_cred(ctx.device_id, "tc") is True
        await cp.revoke_app_attest_key(ctx.aakid)
        assert await cp.verify_transport_cred(ctx.device_id, "tc") is False
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_issue_challenge_after_revoke_is_denied():
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _ = await _wire(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        aid = await coord.request_codex_modify(
            principal="local-owner", task="edit README", workspace=ws,
            human_summary="Codex will edit README")
        await cp.revoke_app_attest_key(ctx.aakid)
        wire, s = await cp.issue_challenge(approval_id=aid, device_id=ctx.device_id)
        # The client-facing status stays deliberately vague: a possibly-stolen device must
        # not learn WHY it was cut off. The server-side audit is what has to be precise.
        assert wire is None and s == "device_inactive", s
        rows = await st._run(lambda: st._conn.execute(
            "SELECT event, reason FROM audit WHERE device_id=?", (ctx.device_id,)).fetchall())
        events = {r["event"] for r in rows}
        assert "challenge_rejected_revoked_key" in events, events
        reasons = {r["reason"] for r in rows if r["event"] == "challenge_rejected_revoked_key"}
        assert S.REVOKE_APP_ATTEST_KEY in reasons, reasons
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_stale_challenge_decision_after_revoke_is_denied():
    """A challenge issued BEFORE the revoke must still be refused when it comes back."""
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _ = await _wire(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        aid = await coord.request_codex_modify(
            principal="local-owner", task="edit README", workspace=ws,
            human_summary="Codex will edit README")
        wire, s = await cp.issue_challenge(approval_id=aid, device_id=ctx.device_id)
        assert s == "ok", s                       # issued while still healthy
        ch_raw = P.b64d(wire["payload_b64"])

        await cp.revoke_app_attest_key(ctx.aakid)  # revoke lands in between

        res, s2 = await coord.apply_mobile_decision(**H.sign_decision(ctx, ch_raw))
        assert res is None and s2 == "device_revoked", s2
        assert (await st.get_request(aid))["state"] == S.PENDING
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


# ---- 8: already approved, then revoked, then executed ------------------
async def t_execution_after_revoke_is_blocked():
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _ = await _wire(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        aid = await coord.request_codex_modify(
            principal="local-owner", task="edit README", workspace=ws,
            human_summary="Codex will edit README")
        wire, s = await cp.issue_challenge(approval_id=aid, device_id=ctx.device_id)
        assert s == "ok", s
        res, s2 = await coord.apply_mobile_decision(
            **H.sign_decision(ctx, P.b64d(wire["payload_b64"])))
        assert s2 == "ok" and res["decision"] == P.DECISION_APPROVE, s2
        assert (await st.get_request(aid))["state"] == S.APPROVED

        await cp.revoke_app_attest_key(ctx.aakid, reason="stolen")

        calls = []
        out, status = await coord.execute_approved(aid, _executor(calls))
        assert out is None and status == "device_revoked", status
        assert calls == [], "the executor ran on revoked authority"
        assert (await st.get_request(aid))["state"] == S.APPROVED
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


# ---- 9: durability across restart -------------------------------------
async def t_revocation_and_device_state_survive_restart():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        await _enroll_sharing_key(cp, ctx, "dev-2")
        await cp.revoke_app_attest_key(ctx.aakid, reason="compromised app")
        await st.close()

        st2, cp2 = await _open(tmp)                       # fresh process-like open
        assert await st2.is_revoked(S.REVOKE_APP_ATTEST_KEY, ctx.aakid) is True
        for d in ("dev-1", "dev-2"):
            assert (await st2.get_device(d))["status"] == S.DEVICE_REVOKED, d
        assert await cp2.verify_transport_cred("dev-1", "tc") is False
        # and re-enrolling a fresh device_id with the same key stays blocked
        token, _ = await cp2.create_enrollment_token("local-owner")
        res, s = await cp2.begin_enrollment(
            enrollment_token=token, device_id="dev-fresh",
            approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
            app_attest_key_id=ctx.aakid, transport_cred="tc")
        assert res is None and s == "device_revoked", s
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 10: idempotency ---------------------------------------------------
async def t_repeated_revoke_is_idempotent():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        await _enroll_sharing_key(cp, ctx, "dev-2")

        first = len((await cp.revoke_app_attest_key(ctx.aakid, reason="first")).affected_devices)
        assert first == 2, first
        for _ in range(3):
            again = len((await cp.revoke_app_attest_key(ctx.aakid, reason="again")).affected_devices)
            assert again == 0, again          # nothing left to move, and no error
        rows = [r for r in await st.list_revocations()
                if r["kind"] == S.REVOKE_APP_ATTEST_KEY]
        assert len(rows) == 1, rows           # still exactly one entry
        assert rows[0]["reason"] == "first", "a repeat revoke overwrote the original reason"
        for d in ("dev-1", "dev-2"):
            assert (await st.get_device(d))["status"] == S.DEVICE_REVOKED
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 11: the operator listing must not contradict the security state ---
def t_admin_listing_never_shows_a_revoked_device_as_active():
    """This is the test that would have caught the original finding, because the finding was
    ONLY visible through the operator's eyes."""
    tmp = tempfile.mkdtemp()
    try:
        async def setup():
            st, cp = await _open(tmp)
            ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
            await _enroll_sharing_key(cp, ctx, "dev-2")
            await st.close()
            return ctx.aakid
        aakid = asyncio.run(setup())

        import contextlib
        import io
        rc = admin_cli.main(["--state-dir", tmp, "revoke-app-attest-key", aakid,
                             "--reason", "compromised app"])
        assert rc == 0, rc

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert admin_cli.main(["--state-dir", tmp, "devices"]) == 0
        listing = buf.getvalue()
        assert "dev-1" in listing and "dev-2" in listing, listing
        for line in listing.splitlines():
            if "dev-1" in line or "dev-2" in line:
                assert S.DEVICE_ACTIVE not in line, f"listing still calls it ACTIVE: {line}"
                assert S.DEVICE_REVOKED in line, line

        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            assert admin_cli.main(["--state-dir", tmp, "revocations"]) == 0
        assert aakid in buf2.getvalue()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- transactionality: no half-applied security promise ----------------
async def t_revocation_and_status_land_in_one_transaction():
    """A second connection must never observe "revocation present, device still ACTIVE".
    Checked from an INDEPENDENT connection so the store's single worker thread cannot be
    what makes it pass."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        await _enroll_sharing_key(cp, ctx, "dev-2")
        await cp.revoke_app_attest_key(ctx.aakid, reason="compromised app")

        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            revoked = con.execute(
                "SELECT 1 FROM revocations WHERE kind=? AND value=?",
                (S.REVOKE_APP_ATTEST_KEY, ctx.aakid)).fetchone() is not None
            still_active = [r["device_id"] for r in con.execute(
                "SELECT device_id FROM devices WHERE app_attest_key_id=? AND status=?",
                (ctx.aakid, S.DEVICE_ACTIVE)).fetchall()]
        finally:
            con.close()
        assert revoked, "revocation not visible to a second connection"
        assert still_active == [], f"revoked key, yet ACTIVE devices: {still_active}"
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _Boom(RuntimeError):
    """Injected failure, distinguishable from a real bug."""


class _FailAfterBegin:
    """Fails at a chosen statement AFTER BEGIN IMMEDIATE.

    P1A.4/§14: the earlier version of this test passed ""/None, which raises ValueError
    BEFORE the transaction is ever opened — so it proved input validation, not rollback,
    while its name claimed rollback. The injection now happens inside the transaction.
    """

    def __init__(self, real, *, fail_on, nth=1):
        self._real, self._on, self._nth, self._hits = real, fail_on.upper(), nth, 0
        self.begun = False
        self.fired = False

    def execute(self, sql, *args):
        head = sql.strip().upper()
        if head.startswith("BEGIN"):
            self.begun = True
        if self.begun and head.startswith(self._on):
            self._hits += 1
            if self._hits == self._nth:
                self.fired = True
                raise _Boom(f"injected on {self._on} #{self._nth}")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def t_failed_revoke_leaves_no_partial_state():
    """A failure INSIDE the transaction must commit nothing — verified through an
    independent connection, and the store must still be usable afterwards."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        await _enroll_sharing_key(cp, ctx, "dev-2")

        real = await st._run(lambda: st._conn)
        failing = _FailAfterBegin(real, fail_on="UPDATE", nth=1)
        st._conn = failing
        try:
            await cp.revoke_app_attest_key(ctx.aakid, reason="will fail")
            raise AssertionError("injected failure did not propagate")
        except _Boom:
            pass
        finally:
            st._conn = real
        assert failing.begun, "the transaction was never opened — nothing was tested"
        assert failing.fired, "the injection point was never reached"

        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            revs = con.execute("SELECT * FROM revocations WHERE kind=?",
                               (S.REVOKE_APP_ATTEST_KEY,)).fetchall()
            statuses = {r["device_id"]: r["status"] for r in
                        con.execute("SELECT device_id, status FROM devices").fetchall()}
        finally:
            con.close()
        assert list(revs) == [], f"revocation committed despite failure: {list(revs)}"
        assert all(v == S.DEVICE_ACTIVE for v in statuses.values()), statuses

        # input validation is a separate property — assert it separately, honestly
        for bad in ("", None, "not base64", "AAAA"):
            try:
                await cp.revoke_app_attest_key(bad)
                raise AssertionError(f"bad key accepted: {bad!r}")
            except ValueError:
                pass
        affected = len((await cp.revoke_app_attest_key(ctx.aakid, reason="real one")).affected_devices)
        assert affected == 2, affected
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- the two properties must stay SEPARATE ----------------------------
async def t_revocations_table_stays_the_authority_source_not_the_status():
    """Phase 3 guard. Authority must NOT be reduced to `status == REVOKED`. Force a device
    row back to ACTIVE behind the control plane's back and require authority to stay closed
    — only the durable blocklist can deliver that."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        await cp.revoke_app_attest_key(ctx.aakid, reason="compromised app")

        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:      # tamper: exactly what a status-only check would fall for
            con.execute("UPDATE devices SET status=? WHERE device_id=?",
                        (S.DEVICE_ACTIVE, ctx.device_id))
        finally:
            con.close()
        assert (await st.get_device(ctx.device_id))["status"] == S.DEVICE_ACTIVE

        assert await cp.verify_transport_cred(ctx.device_id, "tc") is False
        token, _ = await cp.create_enrollment_token("local-owner")
        res, s = await cp.begin_enrollment(
            enrollment_token=token, device_id="dev-fresh",
            approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
            app_attest_key_id=ctx.aakid, transport_cred="tc")
        assert res is None and s == "device_revoked", s
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
