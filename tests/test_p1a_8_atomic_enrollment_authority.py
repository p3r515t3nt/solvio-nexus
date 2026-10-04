"""P1A.8 — atomic enrollment authority.

C1, reproduced against 84b797f before the fix. `begin_enrollment` wrote the device binding
in one transaction and inserted the attestation challenge in a SECOND one. Two concurrent
begins for the same device_id therefore interleaved as

    A.device_write | B.device_write | A.challenge_insert | B.challenge_insert

and A's challenge was never superseded, because the supersede sweep ran while A's challenge
did not yet exist. The device row then held B's approval key while A's challenge was still
live — and `finalize_attestation` never compared the challenge's approval key against the
device row at all. The measured result on the pre-fix tree:

    victim challenge superseded=0 consumed=0
    device holds approval key of: ATTACKER
    finalize -> ok
    APPROVAL KEY NOW TRUSTED = ATTACKER
    app_attest_public_key is the VICTIM's = True

i.e. the victim's own honest App Attest attestation promoted the ATTACKER's approval key —
the key that carries human authority — to ACTIVE/ATTESTED.

The fix is two-part and both parts are tested here:

  * `begin_enrollment_atomic` — revocation check, supersede, device binding and challenge in
    ONE `BEGIN IMMEDIATE`, with a server-generated `devices.current_enrollment_id` naming the
    single generation that owns the device;
  * `finalize_attestation` — reads the challenge AND the device row itself, computes both
    identities from key material, and refuses any enrollment_id that is not the current one.
    No caller-supplied security-check values cross that boundary any more.

ASSERTION POLICY: `require*` from `tests/_guard.py` are function calls and survive `-O`.

Direct: python tests/test_p1a_8_atomic_enrollment_authority.py
"""
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import mobile_attest_helper as H  # noqa: E402
from _guard import require, require_equal  # noqa: E402
from solvio.security.mobile_approval import app_attest as AA  # noqa: E402
from solvio.security.mobile_approval import attest_protocol as AP  # noqa: E402
from solvio.security.mobile_approval import control as C  # noqa: E402
from solvio.security.mobile_approval import crypto  # noqa: E402
from solvio.security.mobile_approval import identity  # noqa: E402
from solvio.security.mobile_approval import protocol as P  # noqa: E402
from solvio.security.mobile_approval import store as S  # noqa: E402

APP_ID = "WQ8CG7R53R.de.solvio.approvals"
DB = "approval_control.sqlite3"
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STORE_SRC = os.path.join(REPO, "src", "solvio", "security", "mobile_approval", "store.py")


async def _open(tmp, **kw):
    st = S.ApprovalControlStore(os.path.join(tmp, DB), **kw)
    await st.open()
    cp = C.MobileApprovalControlPlane(
        st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
        attest_verifier=H.fake_verifier(), app_id=APP_ID)
    return st, cp


async def _begin(cp, ctx, device_id, *, aakid=None):
    token, _ = await cp.create_enrollment_token("local-owner")
    return await cp.begin_enrollment(
        enrollment_token=token, device_id=device_id,
        approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
        app_attest_key_id=aakid or ctx.aakid, transport_cred="tc")


def _rows(tmp, sql, args=()):
    con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# C1 — the attack itself
# ---------------------------------------------------------------------------
async def t_c1_victim_attestation_cannot_promote_a_foreign_approval_key():
    """THE regression. Verbatim the pre-fix attack, through the shipping control path."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        victim, attacker = H.new_device("dev-race"), H.new_device("dev-race")
        vres, vst = await _begin(cp, victim, "dev-race")
        require_equal(vst, "ok", "victim begin")
        # the attacker claims the VICTIM's app-attest key id — at begin it is only a string
        ares, ast_ = await _begin(cp, attacker, "dev-race", aakid=victim.aakid)
        require_equal(ast_, "ok", "attacker begin")

        dev = await st.get_device("dev-race")
        require_equal(dev["key_id"], crypto.key_id(attacker.appr_x963),
                      "setup: the later begin should hold the device row")

        # the victim now attests HONESTLY with its own real App Attest key
        out, status = await cp.complete_attestation(
            enrollment_id=vres["enrollment_id"],
            attestation_b64=P.b64e(AA.fake_attestation(victim.aakey_x963)))
        require(out is None, f"the victim's attestation was accepted: {out}")
        require_equal(status, "challenge_superseded", f"wrong refusal reason: {status}")

        dev = await st.get_device("dev-race")
        require_equal(dev["attestation_status"], S.ATT_PENDING,
                      "a foreign approval key reached ATTESTED")
        require_equal(dev["attested"], 0, "attested flag was set")
        require(dev["app_attest_public_key"] is None,
                "the victim's verified key was written onto the attacker's binding")
        # and nothing was recorded as ever having been trusted
        require_equal(_rows(tmp, "SELECT * FROM device_identity_history"), [],
                      "a history row was written for a binding that never attested")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_c1_the_generation_guard_holds_without_the_supersede_flag():
    """Defence in depth: `superseded` and `current_enrollment_id` are independent.

    The atomic begin marks the loser superseded, so control refuses before any crypto runs.
    That flag must not be the only thing standing between an old challenge and a trusted
    transition — clear it and the store must still refuse, in SQL, under the write lock.
    """
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        victim, attacker = H.new_device("dev-gen2"), H.new_device("dev-gen2")
        vres, _ = await _begin(cp, victim, "dev-gen2")
        await _begin(cp, attacker, "dev-gen2", aakid=victim.aakid)
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:
            con.execute("UPDATE attestation_challenges SET superseded=0 WHERE enrollment_id=?",
                        (vres["enrollment_id"],))
        finally:
            con.close()
        out, status = await cp.complete_attestation(
            enrollment_id=vres["enrollment_id"],
            attestation_b64=P.b64e(AA.fake_attestation(victim.aakey_x963)))
        require(out is None, "an un-superseded stale challenge attested")
        require_equal(status, "enrollment_superseded", f"wrong reason: {status}")
        dev = await st.get_device("dev-gen2")
        require_equal(dev["attestation_status"], S.ATT_PENDING, "the device became attested")
        require(dev["app_attest_public_key"] is None, "a verified key was written")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_c1_only_one_enrollment_generation_owns_a_device():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k1, k2, k3 = (H.new_device("dev-gen") for _ in range(3))
        r1, _ = await _begin(cp, k1, "dev-gen")
        r2, _ = await _begin(cp, k2, "dev-gen")
        r3, _ = await _begin(cp, k3, "dev-gen")

        dev = await st.get_device("dev-gen")
        require_equal(dev["current_enrollment_id"], r3["enrollment_id"],
                      "the device row does not name the newest generation")
        live = [r["enrollment_id"] for r in _rows(
            tmp, "SELECT enrollment_id FROM attestation_challenges "
                 "WHERE device_id=? AND consumed=0 AND superseded=0", ("dev-gen",))]
        require_equal(live, [r3["enrollment_id"]],
                      f"more than one challenge stayed live: {live}")
        # every loser is refused, and refused as SUPERSEDED rather than by accident
        for r, ctx in ((r1, k1), (r2, k2)):
            out, status = await cp.complete_attestation(
                enrollment_id=r["enrollment_id"],
                attestation_b64=P.b64e(AA.fake_attestation(ctx.aakey_x963)))
            require(out is None, f"a superseded enrollment attested: {r['enrollment_id']}")
            require_equal(status, "challenge_superseded", f"wrong reason: {status}")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_c1_the_winner_can_still_complete_normally():
    """The fix must not break the ordinary path — a real enrollment still attests."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        old, cur = H.new_device("dev-ok"), H.new_device("dev-ok")
        await _begin(cp, old, "dev-ok")
        res, _ = await _begin(cp, cur, "dev-ok")
        out, status = await cp.complete_attestation(
            enrollment_id=res["enrollment_id"],
            attestation_b64=P.b64e(AA.fake_attestation(cur.aakey_x963)))
        require_equal(status, "ok", f"the current enrollment was refused: {status}")
        dev = await st.get_device("dev-ok")
        require_equal(dev["attestation_status"], S.ATT_ATTESTED, "not attested")
        require_equal(dev["key_id"], crypto.key_id(cur.appr_x963), "wrong approval key")
        require_equal(dev["app_attest_public_key"], cur.aakey_x963.hex(),
                      "the stored key is not the one that was verified")
        hist = _rows(tmp, "SELECT * FROM device_identity_history WHERE device_id=?", ("dev-ok",))
        require_equal(len(hist), 1, f"history rows: {hist}")
        require_equal(hist[0]["approval_key_sha256"], crypto.fingerprint(cur.appr_x963),
                      "history recorded a different approval key")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# C1 — real concurrency, separate connections, no sleeps
# ---------------------------------------------------------------------------
async def t_c1_concurrent_begins_from_separate_connections_leave_one_winner():
    """Eight begins across eight OWN connections, released together by a barrier.

    No sleeps: the barrier creates the contention and SQLite's write lock serialises it.
    The assertion is the invariant, not a schedule — whatever order the writers commit in,
    exactly one challenge may survive and it must be the one the device row names.
    """
    tmp = tempfile.mkdtemp()
    try:
        st0, cp0 = await _open(tmp)           # creates the identity files and the schema
        await st0.close()
        n = 8
        barrier = threading.Barrier(n)
        results: list = [None] * n
        errors: list = [None] * n

        def worker(i: int) -> None:
            async def run():
                st, cp = await _open(tmp)
                try:
                    ctx = H.new_device("dev-conc")
                    token, _ = await cp.create_enrollment_token("local-owner")
                    barrier.wait()
                    return await cp.begin_enrollment(
                        enrollment_token=token, device_id="dev-conc",
                        approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
                        app_attest_key_id=ctx.aakid, transport_cred="tc")
                finally:
                    await st.close()
            try:
                results[i] = asyncio.run(run())
            except BaseException as exc:       # noqa: BLE001 - reported, never swallowed
                errors[i] = f"{type(exc).__name__}: {exc}"
                try:
                    barrier.abort()
                except threading.BrokenBarrierError:
                    pass

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        require(not any(t.is_alive() for t in threads), "a begin worker deadlocked")
        require_equal([e for e in errors if e], [], f"begin raised: {errors}")

        ok = [r[0]["enrollment_id"] for r in results if r and r[1] == "ok"]
        require_equal(len(ok), n, f"only {len(ok)}/{n} begins succeeded")
        live = [r["enrollment_id"] for r in _rows(
            tmp, "SELECT enrollment_id FROM attestation_challenges "
                 "WHERE device_id=? AND consumed=0 AND superseded=0", ("dev-conc",))]
        require_equal(len(live), 1, f"{len(live)} challenges survived concurrent begins: {live}")
        dev = _rows(tmp, "SELECT * FROM devices WHERE device_id=?", ("dev-conc",))[0]
        require_equal(dev["current_enrollment_id"], live[0],
                      "the surviving challenge is not the generation the device row names")
        # the winner's binding is the one on the device row, field by field
        ch = _rows(tmp, "SELECT * FROM attestation_challenges WHERE enrollment_id=?",
                   (live[0],))[0]
        require_equal(ch["approval_key_id"], dev["key_id"], "torn approval key")
        require_equal(ch["approval_pubkey_sha256"],
                      crypto.fingerprint(bytes.fromhex(dev["public_key_x963"])),
                      "the device key is not the one the surviving challenge was issued for")
        require_equal(ch["app_attest_key_id"], dev["app_attest_key_id"], "torn app-attest id")
        require_equal(ch["principal"], dev["principal"], "torn principal")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_c1_a_device_row_is_never_visible_without_its_challenge():
    """No torn intermediate state: a reader must never see a generation with no challenge.

    This is what the two-transaction begin exposed — between the device write and the
    challenge insert the device row named a binding that had no challenge at all.
    """
    tmp = tempfile.mkdtemp()
    try:
        st0, _ = await _open(tmp)
        await st0.close()
        stop = threading.Event()
        torn: list = []

        def reader() -> None:
            con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
            con.row_factory = sqlite3.Row
            try:
                while not stop.is_set():
                    for r in con.execute(
                            "SELECT d.device_id, d.current_enrollment_id FROM devices d "
                            "WHERE d.current_enrollment_id IS NOT NULL AND NOT EXISTS ("
                            "  SELECT 1 FROM attestation_challenges c "
                            "  WHERE c.enrollment_id = d.current_enrollment_id)").fetchall():
                        torn.append(dict(r))
            finally:
                con.close()

        rd = threading.Thread(target=reader, daemon=True)
        rd.start()
        try:
            st, cp = await _open(tmp)
            for _ in range(25):
                await _begin(cp, H.new_device("dev-torn"), "dev-torn")
            await st.close()
        finally:
            stop.set()
            rd.join(timeout=30)
        require_equal(torn, [], f"a device row was visible without its challenge: {torn[:3]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# The store checks two independent sources — and no caller-supplied values
# ---------------------------------------------------------------------------
async def t_finalize_takes_no_caller_supplied_security_values():
    """Structural: the boundary must only carry VERIFIED facts.

    Passing the challenge's own fields back made the store's comparisons tautologies.
    A future caller must not be able to reintroduce that by keyword.
    """
    import inspect
    sig = inspect.signature(S.ApprovalControlStore.finalize_attestation)
    params = set(sig.parameters) - {"self"}
    require_equal(params, {"enrollment_id", "app_attest_public_key_x963", "app_attest_counter",
                           "environment", "app_id", "core_instance_id"},
                  f"the finalize boundary changed shape: {sorted(params)}")
    for banned in ("device_id", "approval_pubkey_sha256", "app_attest_key_id"):
        require(banned not in params,
                f"{banned} is a security-check value and must not be caller-supplied")


async def t_finalize_compares_the_device_row_not_only_the_challenge():
    """The device row's approval key was never compared. Break it directly and see."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx, other = H.new_device("dev-cmp"), H.new_device("dev-cmp")
        res, _ = await _begin(cp, ctx, "dev-cmp")
        # swap the device row's approval key behind the control plane's back
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:
            con.execute("UPDATE devices SET key_id=?, public_key_x963=? WHERE device_id=?",
                        (crypto.key_id(other.appr_x963), other.appr_x963.hex(), "dev-cmp"))
        finally:
            con.close()
        out, status = await cp.complete_attestation(
            enrollment_id=res["enrollment_id"],
            attestation_b64=P.b64e(AA.fake_attestation(ctx.aakey_x963)))
        require(out is None, "a device row bound to a different approval key attested")
        require_equal(status, "approval_key_mismatch", f"wrong reason: {status}")
        require_equal((await st.get_device("dev-cmp"))["attestation_status"], S.ATT_PENDING,
                      "the device became attested anyway")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_finalize_compares_the_public_key_itself_not_only_its_label():
    """Isolates the fingerprint check: key_id stays consistent, only the KEY is swapped.

    `key_id` is a label the client supplies; `public_key_x963` is the material the approval
    signature is verified against. Swapping only the key leaves every label agreeing, so the
    fingerprint computed from the device row is the only thing that can catch it.
    """
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx, other = H.new_device("dev-pk"), H.new_device("dev-pk")
        res, _ = await _begin(cp, ctx, "dev-pk")
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:
            con.execute("UPDATE devices SET public_key_x963=? WHERE device_id=?",
                        (other.appr_x963.hex(), "dev-pk"))       # key_id left untouched
        finally:
            con.close()
        dev = await st.get_device("dev-pk")
        require_equal(dev["key_id"], crypto.key_id(ctx.appr_x963),
                      "setup: the label must still match the challenge")
        out, status = await cp.complete_attestation(
            enrollment_id=res["enrollment_id"],
            attestation_b64=P.b64e(AA.fake_attestation(ctx.aakey_x963)))
        require(out is None, "a device holding a foreign approval key attested")
        require_equal(status, "approval_key_mismatch", f"wrong reason: {status}")
        require_equal((await st.get_device("dev-pk"))["attestation_status"], S.ATT_PENDING,
                      "the device became attested anyway")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_finalize_compares_the_principal_from_both_sources():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = H.new_device("dev-princ")
        res, _ = await _begin(cp, ctx, "dev-princ")
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:
            con.execute("UPDATE devices SET principal=? WHERE device_id=?",
                        ("someone-else", "dev-princ"))
        finally:
            con.close()
        out, status = await cp.complete_attestation(
            enrollment_id=res["enrollment_id"],
            attestation_b64=P.b64e(AA.fake_attestation(ctx.aakey_x963)))
        require(out is None, "a re-principalled device attested")
        require_equal(status, "principal_mismatch", f"wrong reason: {status}")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_finalize_rejects_a_challenge_from_another_core_instance():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = H.new_device("dev-core")
        res, _ = await _begin(cp, ctx, "dev-core")
        out, status = await st.finalize_attestation(
            enrollment_id=res["enrollment_id"],
            app_attest_public_key_x963=ctx.aakey_x963, app_attest_counter=0,
            environment="development", app_id=APP_ID,
            core_instance_id="core-somebody-else"), None
        require_equal(out, "wrong_core_instance", f"foreign core accepted: {out}")
        require_equal((await st.get_device("dev-core"))["attestation_status"], S.ATT_PENDING,
                      "the device became attested anyway")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_finalize_refuses_an_empty_environment():
    """An empty environment is outside every policy, so it is never recorded as trusted."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = H.new_device("dev-env")
        res, _ = await _begin(cp, ctx, "dev-env")
        out = await st.finalize_attestation(
            enrollment_id=res["enrollment_id"],
            app_attest_public_key_x963=ctx.aakey_x963, app_attest_counter=0,
            environment="", app_id=APP_ID, core_instance_id=cp.core_instance_id)
        require_equal(out, "environment_unknown", f"empty environment accepted: {out}")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# §7 / §8 — the remaining unguarded writers
# ---------------------------------------------------------------------------
async def t_a_stale_enrollment_cannot_fail_the_current_one():
    """A losing generation must not be able to lock the legitimate device out."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        old, cur = H.new_device("dev-fail"), H.new_device("dev-fail")
        r_old, _ = await _begin(cp, old, "dev-fail")
        r_cur, _ = await _begin(cp, cur, "dev-fail")
        await st.set_device_attestation_failed(
            enrollment_id=r_old["enrollment_id"], device_id="dev-fail", reason="stale")
        dev = await st.get_device("dev-fail")
        require_equal(dev["attestation_status"], S.ATT_PENDING,
                      "a superseded enrollment marked the current device FAILED")
        events = [r["event"] for r in _rows(tmp, "SELECT event FROM audit")]
        require("device_attestation_failed_stale" in events,
                f"the stale attempt was not audited: {events}")
        require("device_attestation_failed" not in events,
                "the stale attempt was audited as an authoritative failure")
        # the CURRENT generation still can
        await st.set_device_attestation_failed(
            enrollment_id=r_cur["enrollment_id"], device_id="dev-fail", reason="real")
        require_equal((await st.get_device("dev-fail"))["attestation_status"], S.ATT_FAILED,
                      "the owning enrollment could not record its own failure")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_the_unguarded_attested_writer_is_gone():
    """`set_device_attested` was a second, weaker way to mark a device trusted."""
    require(not hasattr(S.ApprovalControlStore, "set_device_attested"),
            "set_device_attested is back — it bypasses every finalize check")
    src = open(STORE_SRC, encoding="utf-8").read()
    require("def _set_device_attested(" not in src, "the private helper survived")
    require("def set_device_attested(" not in src, "the public writer survived")


async def t_only_one_enrollment_writer_exists():
    """The split begin API is the C1 window; it must not come back."""
    src = open(STORE_SRC, encoding="utf-8").read()
    for gone in ("async def begin_device_enrollment(", "async def add_attestation_challenge("):
        require(gone not in src, f"the split enrollment API is back: {gone}")
    require("async def begin_enrollment_atomic(" in src, "the atomic writer is missing")
    ctl = open(os.path.join(REPO, "src", "solvio", "security", "mobile_approval",
                            "control.py"), encoding="utf-8").read()
    require("begin_enrollment_atomic(" in ctl, "control does not use the atomic writer")


async def t_begin_is_a_single_transaction():
    """Structural: exactly one BEGIN IMMEDIATE covers the whole enrollment."""
    src = open(STORE_SRC, encoding="utf-8").read()
    start = src.index("def _begin_enrollment_atomic(")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    require_equal(body.count('BEGIN IMMEDIATE'), 1,
                  "the enrollment is not one transaction")
    for needed in ("FROM revocations", "SET superseded=1",
                   "_begin_device_enrollment_write", "INSERT INTO attestation_challenges"):
        require(needed in body, f"{needed} is outside the enrollment transaction")
    require_equal(body.count('self._conn.execute("COMMIT")'), 3,
                  "the transaction has more exits than its three outcomes")


async def t_revocation_still_blocks_the_atomic_begin():
    """P1A.4/§10 must survive the merge into one transaction."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = H.new_device("dev-rev")
        fp = crypto.fingerprint(ctx.appr_x963)
        await st.revoke_identities(
            entries=[(S.REVOKE_APPROVAL_KEY, fp)], reason="test",
            operation="revoke-approval-key",
            match=lambda row: cp._approval_fingerprint(row) == fp)
        res, status = await _begin(cp, ctx, "dev-rev")
        require(res is None, "a revoked approval key enrolled")
        require_equal(status, "device_revoked", f"wrong reason: {status}")
        require_equal(_rows(tmp, "SELECT * FROM attestation_challenges"), [],
                      "a challenge was written for a rejected enrollment")
        require_equal(_rows(tmp, "SELECT * FROM devices WHERE device_id=?", ("dev-rev",)), [],
                      "a device row was written for a rejected enrollment")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
