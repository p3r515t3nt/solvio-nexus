"""P1A.3 — every revocation writer is ONE atomic security state transition.

Measured before the fix (statement trace on the real connection):

    revoke_device          4 statements, NO transaction  -> 4 commit units
                           UPDATE(status) audit INSERT(revocations) audit
                           ... status was written BEFORE the durable revocation, so a crash
                           in between left a REVOKED device with no identity revocation.
    revoke_approval_key    7 statements, NO transaction  -> 6 commit units
                           INSERT(revocations) audit SELECT (UPDATE audit) x N
                           ... revocation durable, some bound devices still ACTIVE.
    revoke_app_attest_key  8 statements, ONE transaction (P1A.2)

All three now run through store.revoke_identity: BEGIN IMMEDIATE, then read + match +
write + audit + COMMIT. The lock is taken BEFORE the read on purpose — the approval-key
fingerprint is computed in Python from the stored public key rather than being a column, so
the match itself has to sit inside the transaction.

The two properties stay separate, as in P1A.2:

    revocations table  = durable identity-level authority blocklist
    devices.status     = device lifecycle / operator state

Nothing here may be read as "ACTIVE means authorized".

Direct: python test_p1a_3_transactional_revocation.py
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
from solvio.security.approval import ApprovalBroker
from solvio.security.mobile_approval import admin_cli
from solvio.security.mobile_approval import app_attest as AA
from solvio.security.mobile_approval import bridge as B
from solvio.security.mobile_approval import control as C
from solvio.security.mobile_approval import crypto
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
    return st, cp, B.MobileApprovalCoordinator(cp, ApprovalBroker(approver=approver),
                                               approver), approver


async def _enroll_same_approval_key(cp, ctx, device_id, *, transport_cred="tc"):
    """Bind ANOTHER device record to the SAME approval key (and the same app-attest key).
    Neither column carries a UNIQUE constraint, so this is the production path."""
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


async def _statements(st, coro_factory):
    """Trace the SQL a production call actually issues. The connection belongs to the store's
    worker thread, so the callback has to be installed from inside it."""
    seen = []
    await st._run(lambda: st._conn.set_trace_callback(
        lambda sql: seen.append(sql.strip().split()[0].upper())))
    try:
        await coro_factory()
    finally:
        await st._run(lambda: st._conn.set_trace_callback(None))
    return seen


class _Boom(RuntimeError):
    """Injected failure — distinguishable from a real bug."""


class _FailingConn:
    """Wraps the store's real connection and raises at a chosen point in the sequence.

    This is failure injection at a genuine seam: the production code runs unmodified and
    issues its own statements; only the connection underneath misbehaves, the way a disk
    error or a crashing process would. `st._conn` is a plain instance attribute, so no
    production test-hook is needed.
    """

    def __init__(self, real, *, fail_on, nth=1):
        self._real = real
        self._fail_on = fail_on.upper()
        self._nth = nth
        self._hits = 0
        self.fired = False

    def execute(self, sql, *args):
        head = sql.strip().upper()
        if head.startswith(self._fail_on):
            self._hits += 1
            if self._hits == self._nth:
                self.fired = True
                raise _Boom(f"injected failure on {self._fail_on} #{self._nth}")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def _inject(st, **kw):
    conn = await st._run(lambda: st._conn)
    failing = _FailingConn(conn, **kw)
    st._conn = failing
    return conn, failing


async def _restore(st, real):
    st._conn = real


# =====================================================================
# Phase 1 evidence — all three writers are ONE commit unit
# =====================================================================
async def t_all_three_revoke_paths_use_one_transaction():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        await _enroll_same_approval_key(cp, ctx, "dev-2")
        fp = crypto.fingerprint(ctx.appr_x963)

        for label, call in (
                ("device", lambda: cp.revoke_device("dev-1")),
                ("approval_key", lambda: cp.revoke_approval_key(fp)),
                ("app_attest_key", lambda: cp.revoke_app_attest_key(ctx.aakid))):
            seen = await _statements(st, call)
            assert "BEGIN" in seen, f"{label}: no explicit transaction: {seen}"
            assert seen[0] == "BEGIN" and seen[-1] == "COMMIT", f"{label}: {seen}"
            assert seen.count("BEGIN") == 1 and seen.count("COMMIT") == 1, f"{label}: {seen}"
            # P1A.4: no security-relevant read may sit outside the lock — including the read
            # that decides WHICH identities a device revoke covers.
            assert seen.index("BEGIN") < seen.index("SELECT"), f"{label}: read outside lock"
            assert "SELECT" not in seen[:seen.index("BEGIN")], f"{label}: pre-lock read: {seen}"
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# Phase 6 — approval key across several device records
# =====================================================================
async def t_approval_key_revoke_hits_every_bound_device():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        await _enroll_same_approval_key(cp, ctx, "dev-2")
        await _enroll_same_approval_key(cp, ctx, "dev-3")
        other = await H.enroll_attested(cp, device_id="dev-other", transport_cred="to")
        before_other = dict(await st.get_device("dev-other"))

        fp = crypto.fingerprint(ctx.appr_x963)
        affected = len((await cp.revoke_approval_key(fp, reason="stolen phone")).affected_devices)
        assert affected == 3, affected
        for d in ("dev-1", "dev-2", "dev-3"):
            assert (await st.get_device(d))["status"] == S.DEVICE_REVOKED, d
        assert dict(await st.get_device("dev-other")) == before_other
        assert await st.is_revoked(S.REVOKE_APPROVAL_KEY, fp) is True
        assert await st.is_revoked(
            S.REVOKE_APPROVAL_KEY, crypto.fingerprint(other.appr_x963)) is False
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_approval_key_state_survives_restart():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        await _enroll_same_approval_key(cp, ctx, "dev-2")
        fp = crypto.fingerprint(ctx.appr_x963)
        await cp.revoke_approval_key(fp, reason="stolen phone")
        await st.close()

        st2, cp2 = await _open(tmp)
        assert await st2.is_revoked(S.REVOKE_APPROVAL_KEY, fp) is True
        for d in ("dev-1", "dev-2"):
            assert (await st2.get_device(d))["status"] == S.DEVICE_REVOKED, d
        assert await cp2.verify_transport_cred("dev-1", "tc") is False
        token, _ = await cp2.create_enrollment_token("local-owner")
        res, s = await cp2.begin_enrollment(
            enrollment_token=token, device_id="dev-fresh",
            approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
            app_attest_key_id=H.new_device("other").aakid, transport_cred="tc")
        assert res is None and s == "device_revoked", s
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# Phase 7 — failure injection: no partially applied revocation
# =====================================================================
async def _assert_clean_after_failure(kind_label, injector_kw):
    """Run a real revoke with an injected failure and require NOTHING to be committed."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        await _enroll_same_approval_key(cp, ctx, "dev-2")
        await _enroll_same_approval_key(cp, ctx, "dev-3")
        fp = crypto.fingerprint(ctx.appr_x963)

        real, failing = await _inject(st, **injector_kw)
        try:
            await cp.revoke_approval_key(fp, reason="stolen phone")
            raise AssertionError(f"{kind_label}: injected failure did not propagate")
        except _Boom:
            pass
        finally:
            await _restore(st, real)
        assert failing.fired, f"{kind_label}: injection point never reached"

        # ROLLBACK must have undone everything — checked from an INDEPENDENT connection so
        # the store's own worker thread cannot be the reason this passes.
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            revs = con.execute("SELECT * FROM revocations WHERE kind=?",
                               (S.REVOKE_APPROVAL_KEY,)).fetchall()
            statuses = {r["device_id"]: r["status"] for r in
                        con.execute("SELECT device_id, status FROM devices").fetchall()}
        finally:
            con.close()
        assert revs == [], f"{kind_label}: revocation committed despite failure: {list(revs)}"
        assert all(v == S.DEVICE_ACTIVE for v in statuses.values()), \
            f"{kind_label}: partial device state committed: {statuses}"

        # the connection must still be usable — a leaked open transaction would break this
        affected = len((await cp.revoke_approval_key(fp, reason="second attempt")).affected_devices)
        assert affected == 3, affected
        for d in ("dev-1", "dev-2", "dev-3"):
            assert (await st.get_device(d))["status"] == S.DEVICE_REVOKED, d
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_failure_after_revocation_insert_rolls_everything_back():
    """A: the revocation row is written, then the first device UPDATE fails."""
    await _assert_clean_after_failure("A", {"fail_on": "UPDATE", "nth": 1})


async def t_failure_between_device_updates_rolls_everything_back():
    """B: the first device is already updated when the second UPDATE fails. This is exactly
    the state the old autocommit loop would have left half-written."""
    await _assert_clean_after_failure("B", {"fail_on": "UPDATE", "nth": 2})


async def t_failure_during_audit_rolls_everything_back():
    """C: audit is part of the security transaction, so a failing audit write must take the
    whole revocation with it — a revocation nobody can account for is not acceptable."""
    # INSERT #1 is the revocations row; #2 is the first audit entry.
    await _assert_clean_after_failure("C", {"fail_on": "INSERT", "nth": 2})


async def t_success_commits_everything_together():
    """D: the ordinary path — identity revocation, all device rows and the audit entries."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        await _enroll_same_approval_key(cp, ctx, "dev-2")
        fp = crypto.fingerprint(ctx.appr_x963)
        assert len((await cp.revoke_approval_key(fp, reason="stolen phone")).affected_devices) == 2

        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            assert con.execute("SELECT 1 FROM revocations WHERE kind=? AND value=?",
                               (S.REVOKE_APPROVAL_KEY, fp)).fetchone() is not None
            active = [r["device_id"] for r in con.execute(
                "SELECT device_id FROM devices WHERE status=?", (S.DEVICE_ACTIVE,)).fetchall()]
            events = {r["event"] for r in con.execute("SELECT event FROM audit").fetchall()}
        finally:
            con.close()
        assert active == [], f"revoked key, yet ACTIVE devices: {active}"
        assert "approval_key_revoked" in events, events
        assert "device_status" in events, events
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# Phase 8 — the write lock really covers read + match + write
# =====================================================================
class _RacingConn:
    """Fires a competing writer at a chosen statement INSIDE the live transaction.

    Same seam as _FailingConn: the production code runs untouched and issues its own SQL;
    the connection underneath calls out at a precise point. Patching the control plane's
    class instead would be global mutable state shared by every test in this file.
    """

    def __init__(self, real, *, on, hook):
        self._real = real
        self._on = on.upper()
        self._hook = hook
        self.fired = False

    def execute(self, sql, *args):
        if not self.fired and sql.strip().upper().startswith(self._on):
            self.fired = True
            self._hook()
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def t_concurrent_writer_cannot_slip_between_match_and_update():
    """Prove the lock spans the whole security sequence, not just the writes.

    A second connection tries to write WHILE the revoke transaction is mid-flight — fired on
    the SELECT that reads the device rows, i.e. after BEGIN IMMEDIATE and before any device
    update. That is precisely the interleaving that would let a bound device survive as
    ACTIVE. Synchronisation is the callback plus SQLite's own lock; no sleeps.
    """
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        await _enroll_same_approval_key(cp, ctx, "dev-2")
        fp = crypto.fingerprint(ctx.appr_x963)
        outcome = {}

        def competing_writer():
            # own connection, short timeout so it cannot simply wait the lock out
            con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None, timeout=0.4)
            try:
                con.execute("UPDATE devices SET status=? WHERE device_id=?",
                            (S.DEVICE_ACTIVE, "dev-2"))
                outcome["result"] = "wrote"
            except sqlite3.OperationalError as exc:
                outcome["result"] = f"blocked: {exc}"
            finally:
                con.close()

        real = await st._run(lambda: st._conn)
        racing = _RacingConn(real, on="SELECT", hook=competing_writer)
        st._conn = racing
        try:
            affected = len((await cp.revoke_approval_key(fp, reason="stolen phone")).affected_devices)
        finally:
            st._conn = real

        assert racing.fired, "the racing writer never ran"
        assert outcome["result"].startswith("blocked"), \
            f"a concurrent writer got in mid-transaction: {outcome['result']}"
        assert affected == 2, affected
        for d in ("dev-1", "dev-2"):
            assert (await st.get_device(d))["status"] == S.DEVICE_REVOKED, d
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_two_control_planes_revoking_concurrently_stay_consistent():
    """Two store objects on the same file, barrier-synchronised, both revoking. Whatever the
    order, the end state must be one revocation entry and every bound device REVOKED."""
    tmp = tempfile.mkdtemp()
    try:
        stA, cpA = await _open(tmp)
        stB, cpB = await _open(tmp)
        ctx = await H.enroll_attested(cpA, device_id="dev-1", transport_cred="tc")
        await _enroll_same_approval_key(cpA, ctx, "dev-2")
        fp = crypto.fingerprint(ctx.appr_x963)
        loop = asyncio.get_running_loop()
        barrier = threading.Barrier(2)

        async def revoke(cp, reason):
            await loop.run_in_executor(None, barrier.wait)
            return await cp.revoke_approval_key(fp, reason=reason)

        results = await asyncio.gather(revoke(cpA, "from A"), revoke(cpB, "from B"),
                                       return_exceptions=True)
        assert not any(isinstance(r, BaseException) for r in results), results
        rows = [r for r in await stA.list_revocations()
                if r["kind"] == S.REVOKE_APPROVAL_KEY]
        assert len(rows) == 1, rows
        for d in ("dev-1", "dev-2"):
            assert (await stA.get_device(d))["status"] == S.DEVICE_REVOKED, d
        # Exactly one caller may claim the first revocation; the other must report the
        # identity as already revoked. Together they account for both devices exactly once.
        assert sum(len(r.newly_revoked) for r in results) == 1, results
        assert sum(len(r.already_revoked) for r in results) == 1, results
        assert sum(len(r.affected_devices) for r in results) == 2, results
        await stA.close(); await stB.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# Phase 5 — idempotency, for every kind
# =====================================================================
async def t_repeated_revoke_is_safe_for_every_kind():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        await _enroll_same_approval_key(cp, ctx, "dev-2")
        fp = crypto.fingerprint(ctx.appr_x963)

        for label, first, again in (
                ("device", lambda: cp.revoke_device("dev-1", reason="first"),
                 lambda: cp.revoke_device("dev-1", reason="again")),
                ("approval_key", lambda: cp.revoke_approval_key(fp, reason="first"),
                 lambda: cp.revoke_approval_key(fp, reason="again")),
                ("app_attest_key", lambda: cp.revoke_app_attest_key(ctx.aakid, reason="first"),
                 lambda: cp.revoke_app_attest_key(ctx.aakid, reason="again"))):
            await first()
            for _ in range(3):
                res = await again()
                # P1A.5: a repeat revoke moves nothing and reports the identity as ALREADY
                # revoked rather than newly revoked — an honest, distinguishable result.
                assert res.affected_devices == (), f"{label}: {res}"
                assert res.newly_revoked == (), f"{label}: {res}"
                assert res.already_revoked, f"{label}: no already_revoked reported: {res}"
        rows = {r["kind"]: r["reason"] for r in await st.list_revocations()}
        assert set(rows) == set(S.REVOKE_KINDS), rows
        for kind, reason in rows.items():
            assert reason == "first", f"{kind}: security history overwritten -> {reason!r}"
        for d in ("dev-1", "dev-2"):
            assert (await st.get_device(d))["status"] == S.DEVICE_REVOKED, d
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# Phase 9 — authority stays closed, and stays table-driven
# =====================================================================
async def _authority_closed(revoke, ctx, st, cp, coord):
    ws = tempfile.mkdtemp()
    try:
        aid = await coord.request_codex_modify(
            principal="local-owner", task="edit README", workspace=ws,
            human_summary="Codex will edit README")
        wire, s = await cp.issue_challenge(approval_id=aid, device_id=ctx.device_id)
        assert s == "ok", s                       # challenge issued while still healthy
        ch_raw = P.b64d(wire["payload_b64"])

        await revoke()

        assert await cp.verify_transport_cred(ctx.device_id, "tc") is False
        aid2 = await coord.request_codex_modify(
            principal="local-owner", task="second", workspace=ws, human_summary="x")
        _, cs = await cp.issue_challenge(approval_id=aid2, device_id=ctx.device_id)
        assert cs != "ok", cs
        res, ds = await coord.apply_mobile_decision(**H.sign_decision(ctx, ch_raw))
        assert res is None and ds == "device_revoked", ds
    finally:
        shutil.rmtree(ws, ignore_errors=True)


async def t_authority_denied_after_device_revoke():
    tmp = tempfile.mkdtemp()
    try:
        st, cp, coord, _ = await _wire(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        await _authority_closed(lambda: cp.revoke_device(ctx.device_id), ctx, st, cp, coord)
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_authority_denied_after_approval_key_revoke():
    tmp = tempfile.mkdtemp()
    try:
        st, cp, coord, _ = await _wire(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        fp = crypto.fingerprint(ctx.appr_x963)
        await _authority_closed(lambda: cp.revoke_approval_key(fp), ctx, st, cp, coord)
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_authority_denied_after_app_attest_key_revoke():
    tmp = tempfile.mkdtemp()
    try:
        st, cp, coord, _ = await _wire(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        await _authority_closed(lambda: cp.revoke_app_attest_key(ctx.aakid),
                                ctx, st, cp, coord)
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_execution_blocked_after_each_revoke_kind():
    for which in ("device", "approval_key", "app_attest_key"):
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

            if which == "device":
                await cp.revoke_device(ctx.device_id)
            elif which == "approval_key":
                await cp.revoke_approval_key(crypto.fingerprint(ctx.appr_x963))
            else:
                await cp.revoke_app_attest_key(ctx.aakid)

            calls = []
            out, status = await coord.execute_approved(aid, _executor(calls))
            assert out is None and status == "device_revoked", f"{which}: {status}"
            assert calls == [], f"{which}: executor ran on revoked authority"
            assert (await st.get_request(aid))["state"] == S.APPROVED, which
            await st.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_revocations_table_stays_the_authority_source():
    """Phase 9 guard, for all three kinds. devices.status is tampered back to ACTIVE behind
    the control plane's back; authority must stay closed. Only the durable blocklist can
    deliver that — a check reduced to `status == REVOKED` would fall for this."""
    for which in ("device", "approval_key", "app_attest_key"):
        tmp = tempfile.mkdtemp()
        try:
            st, cp = await _open(tmp)
            ctx = await H.enroll_attested(cp, transport_cred="tc")
            if which == "device":
                await cp.revoke_device(ctx.device_id)
            elif which == "approval_key":
                await cp.revoke_approval_key(crypto.fingerprint(ctx.appr_x963))
            else:
                await cp.revoke_app_attest_key(ctx.aakid)

            con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
            try:
                con.execute("UPDATE devices SET status=? WHERE device_id=?",
                            (S.DEVICE_ACTIVE, ctx.device_id))
            finally:
                con.close()
            assert (await st.get_device(ctx.device_id))["status"] == S.DEVICE_ACTIVE

            assert await cp.verify_transport_cred(ctx.device_id, "tc") is False, which
            token, _ = await cp.create_enrollment_token("local-owner")
            res, s = await cp.begin_enrollment(
                enrollment_token=token, device_id=ctx.device_id,
                approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
                app_attest_key_id=ctx.aakid, transport_cred="tc")
            assert res is None and s == "device_revoked", f"{which}: {s}"
            await st.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# Phase 10 — the operator listing must never contradict the state
# =====================================================================
def t_admin_listing_consistent_for_every_revoke_kind():
    for which in ("revoke-device", "revoke-key", "revoke-app-attest-key"):
        tmp = tempfile.mkdtemp()
        try:
            async def setup():
                st, cp = await _open(tmp)
                ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
                await _enroll_same_approval_key(cp, ctx, "dev-2")
                await st.close()
                return ctx
            ctx = asyncio.run(setup())
            arg = {"revoke-device": "dev-1",
                   "revoke-key": crypto.fingerprint(ctx.appr_x963),
                   "revoke-app-attest-key": ctx.aakid}[which]
            assert admin_cli.main(["--state-dir", tmp, which, arg, "--reason", "x"]) == 0

            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                assert admin_cli.main(["--state-dir", tmp, "devices"]) == 0
            listing = buf.getvalue()
            revoked = {"revoke-device": ["dev-1"]}.get(which, ["dev-1", "dev-2"])
            for line in listing.splitlines():
                for d in revoked:
                    if line.startswith(d + " "):
                        assert S.DEVICE_ACTIVE not in line, f"{which}: {line}"
                        assert S.DEVICE_REVOKED in line, f"{which}: {line}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
