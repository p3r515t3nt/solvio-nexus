"""P1B/F9 — approval, pairing and nonce lifecycle under real concurrency.

Every race below was REPRODUCED against the released P1A tree (58c53c6) before it was fixed,
with separate SQLite connections released by a `threading.Barrier` — no sleeps.

  transition APPROVE || DENY   both read PENDING, both passed the Python check, both wrote.
                               Final state = last writer; BOTH callers were told they had
                               succeeded; the audit carried `state_APPROVED` AND `state_DENIED`
                               for one request. Two successes for a once-only operation.
  nonce      consume || consume both returned "ok" for the same challenge nonce.
  pairing    redeem  || redeem  both returned ("local-owner", "ok") for one token, so a single
                               pairing token could bind two devices.
  revoke     vs decision       a `revoke-device` committed from a second connection after the
                               revocation check and before the transition still produced an
                               APPROVED request bound to a REVOKED device.

The full `submit_decision` replay behaved differently and that difference is recorded
honestly: in 100/100 runs it did NOT double-succeed, because the crypto work in front of the
transition skews the two threads far past the ~0.1 ms window. What it DID do, 100/100 times,
was raise `IllegalTransition` out of `submit_decision` — an uncaught exception whose loser had
already burned the nonce, so an honest retry then failed as a replay. Timing skew is not a
security property; the window was real and is now closed in SQL.

OUT OF SCOPE, deliberately: P1C/F4 execution recovery, S2B, remote approvals.

ASSERTION POLICY: `require*` from `tests/_guard.py` are function calls and survive `-O`.

Direct: python tests/test_p1b_approval_concurrency.py
"""
import asyncio
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import mobile_attest_helper as H  # noqa: E402
from _guard import require, require_equal  # noqa: E402
from solvio.security.mobile_approval import control as C  # noqa: E402
from solvio.security.mobile_approval import crypto  # noqa: E402
from solvio.security.mobile_approval import identity  # noqa: E402
from solvio.security.mobile_approval import protocol as P  # noqa: E402
from solvio.security.mobile_approval import store as S  # noqa: E402

APP_ID = "WQ8CG7R53R.de.solvio.approvals"
DB = "approval_control.sqlite3"
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STORE_SRC = os.path.join(REPO, "src", "solvio", "security", "mobile_approval", "store.py")
CONTROL_SRC = os.path.join(REPO, "src", "solvio", "security", "mobile_approval", "control.py")
# Security-critical races are repeated; keep the count high enough to be meaningful and low
# enough that the suite stays inside the gate's budget.
ROUNDS = int(os.environ.get("SOLVIO_RACE_ROUNDS", "100"))
ENVS = frozenset({"development"})


async def _open(tmp):
    st = S.ApprovalControlStore(os.path.join(tmp, DB))
    await st.open()
    cp = C.MobileApprovalControlPlane(
        st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
        attest_verifier=H.fake_verifier(), app_id=APP_ID, allowed_environments=ENVS)
    return st, cp


async def _request(st, approval_id="apr-1", *, principal="local-owner", ttl=600.0):
    await st.create_request(approval_id=approval_id, principal=principal, tool="t", mode="m",
                            task="task", workspace="/w", action_digest="d" * 64,
                            human_summary="s", expires_at=time.time() + ttl)
    return approval_id


def _rows(tmp, sql, args=()):
    con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _race(coros):
    """Run each coroutine factory in its OWN thread, event loop and store connection.

    Synchronisation is a barrier, never a sleep: every worker does all of its setup, then
    blocks until the last one arrives, and only then touches the contended row.
    """
    n = len(coros)
    barrier = threading.Barrier(n)
    out = [None] * n

    def worker(i, factory):
        try:
            out[i] = asyncio.run(factory(barrier))
        except BaseException as exc:                # noqa: BLE001 - reported, never swallowed
            out[i] = ("EXC", f"{type(exc).__name__}: {exc}")
            try:
                barrier.abort()
            except threading.BrokenBarrierError:
                pass

    threads = [threading.Thread(target=worker, args=(i, f)) for i, f in enumerate(coros)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    require(not any(t.is_alive() for t in threads), "a race worker deadlocked")
    return out


def _success_audits(tmp, approval_id):
    return [r["event"] for r in _rows(
        tmp, "SELECT event FROM audit WHERE approval_id=? ORDER BY id", (approval_id,))
        if r["event"].startswith("state_")]


# =====================================================================
# A — an approval request is decided terminally exactly once
# =====================================================================
async def _transition_race(tmp, states):
    async def attempt(barrier, state):
        st, _ = await _open(tmp)
        try:
            barrier.wait()
            try:
                return ("ok", await st.transition("apr-1", state, device_id="dev-1"))
            except (S.IllegalTransition, S.ConcurrentTransition) as exc:
                return ("lost", type(exc).__name__)
        finally:
            await st.close()
    return _race([(lambda b, s=s_: attempt(b, s)) for s_ in states])


async def _one_winner(states, label):
    winners = []
    for i in range(ROUNDS):
        tmp = os.path.realpath(tempfile.mkdtemp())
        try:
            st, cp = await _open(tmp)
            await _request(st)
            await st.close()
            out = await _transition_race(tmp, states)
            require(not any(isinstance(o, tuple) and o[0] == "EXC" for o in out),
                    f"{label} round {i}: unexpected exception {out}")
            ok = [o for o in out if o[0] == "ok"]
            require_equal(len(ok), 1, f"{label} round {i}: {len(ok)} winners — {out}")
            audits = _success_audits(tmp, "apr-1")
            require_equal(len(audits), 1,
                          f"{label} round {i}: {len(audits)} success audits — {audits}")
            final = _rows(tmp, "SELECT state FROM approval_requests")[0]["state"]
            require(final in states, f"{label} round {i}: final state {final}")
            winners.append(final)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return winners


async def t_a_concurrent_approve_vs_approve_has_one_winner():
    await _one_winner([S.APPROVED, S.APPROVED], "approve||approve")


async def t_a_concurrent_approve_vs_reject_has_one_winner():
    winners = await _one_winner([S.APPROVED, S.DENIED], "approve||deny")
    require(set(winners) <= {S.APPROVED, S.DENIED}, winners)


async def t_a_concurrent_reject_vs_reject_has_one_winner():
    await _one_winner([S.DENIED, S.DENIED], "deny||deny")


async def t_a_the_loser_never_overwrites_the_committed_state():
    """Sequential and explicit: once terminal, the state is frozen for every other actor."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        await _request(st)
        await st.transition("apr-1", S.APPROVED, device_id="dev-1")
        # APPROVED -> EXPIRED is a LEGAL edge (the TTL sweeper), so it is not listed:
        # the invariant is that no second DECISION may land, not that the row is immutable.
        for bad in (S.DENIED, S.APPROVED, S.CONSUMED):
            try:
                await st.transition("apr-1", bad, device_id="dev-2")
                require(False, f"APPROVED -> {bad} was accepted")
            except S.IllegalTransition:
                pass
        require_equal((await st.get_request("apr-1"))["state"], S.APPROVED, "state moved")
        require_equal(_success_audits(tmp, "apr-1"), ["state_APPROVED"],
                      "a refused transition wrote a success audit")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# Phase 7 — the transition graph, asserted rather than assumed
# =====================================================================
async def t_transition_graph_forbids_every_terminal_reversal():
    terminal = [S.DENIED, S.CONSUMED, S.EXPIRED, S.FAILED]
    for state in terminal:
        require_equal(S._ALLOWED.get(state, set()), set(),
                      f"{state} is terminal but has outgoing transitions")
    require_equal(S._ALLOWED[S.PENDING], {S.APPROVED, S.DENIED, S.EXPIRED}, S._ALLOWED)
    require_equal(S._ALLOWED[S.APPROVED], {S.EXECUTING, S.EXPIRED}, S._ALLOWED)
    require_equal(S._ALLOWED[S.EXECUTING], {S.CONSUMED, S.FAILED}, S._ALLOWED)
    for src in (S.APPROVED, S.DENIED, S.EXPIRED, S.CONSUMED):
        require(S.APPROVED not in S._ALLOWED.get(src, set()) or src == S.PENDING,
                f"{src} -> APPROVED is reachable")


async def t_transition_graph_is_enforced_against_the_database():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, _ = await _open(tmp)
        for i, (start, target) in enumerate((
                (S.DENIED, S.APPROVED), (S.EXPIRED, S.APPROVED),
                (S.CONSUMED, S.APPROVED), (S.APPROVED, S.DENIED))):
            aid = await _request(st, f"apr-g{i}")
            con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
            try:
                con.execute("UPDATE approval_requests SET state=? WHERE approval_id=?",
                            (start, aid))
            finally:
                con.close()
            try:
                await st.transition(aid, target)
                require(False, f"{start} -> {target} was accepted")
            except S.IllegalTransition:
                pass
            require_equal((await st.get_request(aid))["state"], start, "state moved anyway")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# B — pairing token single use
# =====================================================================
async def t_b_concurrent_pairing_redemption_has_one_winner():
    for i in range(ROUNDS):
        tmp = os.path.realpath(tempfile.mkdtemp())
        try:
            st, cp = await _open(tmp)
            token, _ = await cp.create_enrollment_token("local-owner")
            digest = hashlib.sha256(token.encode()).hexdigest()
            await st.close()

            async def attempt(barrier):
                st2, _ = await _open(tmp)
                try:
                    barrier.wait()
                    return await st2.consume_enrollment_token(digest)
                finally:
                    await st2.close()

            out = _race([attempt, attempt])
            ok = [o for o in out if isinstance(o, tuple) and o[1] == "ok"]
            require_equal(len(ok), 1, f"round {i}: {len(ok)} redemptions succeeded — {out}")
            require_equal(ok[0][0], "local-owner", ok)
            losers = [o for o in out if o not in ok]
            require_equal(losers[0][1], "already_consumed", f"loser said {losers[0]}")
            # third replay, and a fourth through a brand-new connection
            st3, _ = await _open(tmp)
            require_equal((await st3.consume_enrollment_token(digest))[1], "already_consumed",
                          "a third redemption succeeded")
            await st3.close()
            require_equal(_rows(tmp, "SELECT consumed FROM enrollment_tokens")[0]["consumed"],
                          1, "token not terminal")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


async def t_b_only_one_device_binding_results_from_one_token():
    """End to end: two concurrent enrolments with the SAME token bind exactly one device."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        token, _ = await cp.create_enrollment_token("local-owner")
        await st.close()
        ctxs = [H.new_device("dev-x"), H.new_device("dev-y")]

        async def attempt(barrier, ctx):
            st2, cp2 = await _open(tmp)
            try:
                barrier.wait()
                return await cp2.begin_enrollment(
                    enrollment_token=token, device_id=ctx.device_id,
                    approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
                    app_attest_key_id=ctx.aakid, transport_cred="tc")
            finally:
                await st2.close()

        out = _race([(lambda b, c=c: attempt(b, c)) for c in ctxs])
        ok = [o for o in out if isinstance(o, tuple) and o[1] == "ok"]
        require_equal(len(ok), 1, f"{len(ok)} enrolments succeeded from one token: {out}")
        require_equal(len(_rows(tmp, "SELECT device_id FROM devices")), 1,
                      "one pairing token produced more than one device row")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_b_expired_token_stays_fail_closed():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, _ = await _open(tmp)
        digest = hashlib.sha256(b"tok").hexdigest()
        await st.add_enrollment_token(digest, "local-owner", time.time() - 1)
        require_equal((await st.consume_enrollment_token(digest))[1], "expired", "not expired")
        require_equal(_rows(tmp, "SELECT consumed FROM enrollment_tokens")[0]["consumed"], 0,
                      "an expired token was burned, which hides the reason on retry")
        require_equal((await st.consume_enrollment_token("deadbeef"))[1], "unknown", "unknown")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# C/D — decision nonce single use and binding
# =====================================================================
async def _issue(cp, st, ctx, approval_id):
    wire, status = await cp.issue_challenge(approval_id=approval_id, device_id=ctx.device_id)
    require_equal(status, "ok", f"issue_challenge: {status}")
    raw = P.b64d(wire["payload_b64"])
    return P.strict_parse(raw), hashlib.sha256(raw).hexdigest(), raw


async def t_c_concurrent_nonce_consumption_has_one_winner():
    for i in range(ROUNDS):
        tmp = os.path.realpath(tempfile.mkdtemp())
        try:
            st, cp = await _open(tmp)
            ctx = await H.enroll_attested(cp)
            await _request(st)
            ch, sha, _ = await _issue(cp, st, ctx, "apr-1")
            await st.close()

            async def attempt(barrier):
                st2, _ = await _open(tmp)
                try:
                    barrier.wait()
                    return await st2.consume_challenge(
                        challenge_nonce=ch["challenge_nonce"], approval_id="apr-1",
                        device_id=ctx.device_id, principal="local-owner",
                        action_digest="d" * 64, payload_sha256=sha)
                finally:
                    await st2.close()

            out = _race([attempt, attempt])
            require_equal(out.count("ok"), 1, f"round {i}: {out}")
            require("replay" in out, f"round {i}: loser said {out}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


async def t_c_concurrent_signed_replay_yields_one_accept():
    """The same signed decision payload, submitted twice at once, through the real path."""
    for i in range(ROUNDS):
        tmp = os.path.realpath(tempfile.mkdtemp())
        try:
            st, cp = await _open(tmp)
            ctx = await H.enroll_attested(cp)
            await _request(st)
            _, _, raw = await _issue(cp, st, ctx, "apr-1")
            signed = H.sign_decision(ctx, raw)
            await st.close()

            async def attempt(barrier):
                st2, cp2 = await _open(tmp)
                try:
                    barrier.wait()
                    return await cp2.submit_decision(**signed)
                finally:
                    await st2.close()

            out = _race([attempt, attempt])
            require(not any(isinstance(o, tuple) and o[0] == "EXC" for o in out),
                    f"round {i}: an exception escaped submit_decision — {out}")
            statuses = [o[1] for o in out]
            require_equal(statuses.count("ok"), 1, f"round {i}: {statuses}")
            require_equal(len(_success_audits(tmp, "apr-1")), 1,
                          f"round {i}: {_success_audits(tmp, 'apr-1')}")
            require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"],
                          S.APPROVED, "final state")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


async def t_c_replay_after_a_fresh_connection_stays_rejected():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp)
        await _request(st)
        _, _, raw = await _issue(cp, st, ctx, "apr-1")
        signed = H.sign_decision(ctx, raw)
        res, status = await cp.submit_decision(**signed)
        require_equal(status, "ok", status)
        await st.close()
        # brand new process-like connection, as after a restart
        st2, cp2 = await _open(tmp)
        res2, status2 = await cp2.submit_decision(**signed)
        require(res2 is None, "a replay succeeded after a restart")
        require_equal(status2, "not_pending", f"replay said {status2}")
        require_equal(len(_success_audits(tmp, "apr-1")), 1, _success_audits(tmp, "apr-1"))
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_d_a_nonce_cannot_be_moved_to_another_request():
    """The nonce is bound to the request it was issued for, server-side."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp)
        await _request(st, "apr-1")
        await _request(st, "apr-2")
        ch, sha, _ = await _issue(cp, st, ctx, "apr-1")
        out = await st.consume_challenge(
            challenge_nonce=ch["challenge_nonce"], approval_id="apr-2",
            device_id=ctx.device_id, principal="local-owner", action_digest="d" * 64,
            payload_sha256=sha)
        require_equal(out, "mismatch", f"a foreign request consumed the nonce: {out}")
        require_equal(_rows(tmp, "SELECT consumed FROM challenges")[0]["consumed"], 0,
                      "a rejected cross-request attempt still burned the nonce")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_d_cross_request_replay_through_the_real_decision_path():
    """A compromised approver app holds a legitimate key — it must not move a nonce.

    Both fields are inside the SIGNED payload, so the device itself is the only party that
    can build this. The two requests deliberately share an action digest, so the control
    plane's digest check cannot be what saves us: the refusal has to come from the challenge
    binding inside the atomic decision.
    """
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp)
        await _request(st, "apr-1")
        await _request(st, "apr-2")            # same action_digest by construction
        _, _, raw = await _issue(cp, st, ctx, "apr-1")
        stolen = P.strict_parse(raw)
        forged = dict(stolen, approval_id="apr-2")
        signed = H.sign_decision(ctx, P.canonical_bytes(forged))
        res, status = await cp.submit_decision(**signed)
        require(res is None, f"a nonce issued for apr-1 decided apr-2: {res}")
        require_equal(status, "nonce_mismatch", f"refusal reason: {status}")
        states = {r["approval_id"]: r["state"] for r in
                  _rows(tmp, "SELECT approval_id, state FROM approval_requests")}
        require_equal(states, {"apr-1": S.PENDING, "apr-2": S.PENDING}, states)
        require_equal(_rows(tmp, "SELECT consumed FROM challenges")[0]["consumed"], 0,
                      "the cross-request attempt burned the nonce")
        require_equal(_success_audits(tmp, "apr-2"), [], "a success audit was written")
        # the honest decision for apr-1 still works afterwards
        require_equal((await cp.submit_decision(**H.sign_decision(ctx, raw)))[1], "ok",
                      "the legitimate decision was collateral damage")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_g_a_lost_compare_and_swap_writes_no_success_audit():
    """Make the CAS actually LOSE, and require the audit to stay silent.

    The row is moved out from under the transition on its OWN connection, between the state
    read and the swap — the one interleaving that a write transaction cannot prevent. This is
    the case that decides whether the audit is written before or after rowcount is checked.
    """
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, _ = await _open(tmp)
        await _request(st)
        real_conn = st._conn
        fired = []

        class Interposer:
            def execute(self, sql, *args):
                out = real_conn.execute(sql, *args)
                if (not fired) and sql.startswith("SELECT state FROM approval_requests"):
                    fired.append(1)
                    # same connection, same transaction: `cur` is now stale
                    real_conn.execute(
                        "UPDATE approval_requests SET state=? WHERE approval_id=?",
                        (S.DENIED, "apr-1"))
                return out

            def __getattr__(self, name):
                return getattr(real_conn, name)

        st._conn = Interposer()
        try:
            await st.transition("apr-1", S.APPROVED, device_id="dev-1")
            require(False, "a transition whose row had moved reported success")
        except S.ConcurrentTransition:
            pass
        finally:
            st._conn = real_conn
        require(fired, "the interposer never fired — the probe is vacuous")
        require_equal(_success_audits(tmp, "apr-1"), [],
                      f"a success audit survived a lost CAS: {_success_audits(tmp, 'apr-1')}")
        require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"], S.PENDING,
                      "the rollback did not restore the row")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_d_a_nonce_is_bound_to_device_principal_and_display():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp)
        await _request(st)
        ch, sha, _ = await _issue(cp, st, ctx, "apr-1")
        base = dict(challenge_nonce=ch["challenge_nonce"], approval_id="apr-1",
                    device_id=ctx.device_id, principal="local-owner",
                    action_digest="d" * 64, payload_sha256=sha)
        for field, value, expected in (("device_id", "dev-other", "mismatch"),
                                       ("principal", "someone-else", "mismatch"),
                                       ("action_digest", "e" * 64, "mismatch"),
                                       ("payload_sha256", "f" * 64, "display_mismatch")):
            out = await st.consume_challenge(**{**base, field: value})
            require_equal(out, expected, f"{field} rebinding said {out}")
            require_equal(_rows(tmp, "SELECT consumed FROM challenges")[0]["consumed"], 0,
                          f"{field}: a rejected attempt burned the nonce")
        require_equal(await st.consume_challenge(**base), "ok", "the honest consume failed")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_d_a_nonce_from_another_core_instance_is_refused():
    """The decision payload names the core; a challenge minted elsewhere cannot be used."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp)
        await _request(st)
        _, _, raw = await _issue(cp, st, ctx, "apr-1")
        parsed = P.strict_parse(raw)
        forged = dict(parsed, core_instance_id="core-somebody-else")
        signed = H.sign_decision(ctx, P.canonical_bytes(forged))
        res, status = await cp.submit_decision(**signed)
        require(res is None, "a foreign core instance decided")
        require_equal(status, "wrong_core_instance", status)
        require_equal(_rows(tmp, "SELECT consumed FROM challenges")[0]["consumed"], 0,
                      "the nonce was burned by a foreign-core attempt")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_c_expired_nonce_is_refused_and_not_burned():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp)
        await _request(st)
        ch, sha, _ = await _issue(cp, st, ctx, "apr-1")
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:
            con.execute("UPDATE challenges SET expires_at=? WHERE challenge_nonce=?",
                        (time.time() - 1, ch["challenge_nonce"]))
        finally:
            con.close()
        out = await st.consume_challenge(
            challenge_nonce=ch["challenge_nonce"], approval_id="apr-1",
            device_id=ctx.device_id, principal="local-owner", action_digest="d" * 64,
            payload_sha256=sha)
        require_equal(out, "expired", out)
        require_equal(await st.consume_challenge(
            challenge_nonce="nope", approval_id="apr-1", device_id=ctx.device_id,
            principal="local-owner", action_digest="d" * 64, payload_sha256=sha), "unknown",
            "unknown nonce")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# E/F — nonce and transition are one operation; revocation cannot be raced
# =====================================================================
async def t_e_nonce_and_transition_commit_together():
    """Fail the transition inside the operation: the nonce must NOT stay burned."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp)
        await _request(st)
        _, _, raw = await _issue(cp, st, ctx, "apr-1")
        signed = H.sign_decision(ctx, raw)

        boom = RuntimeError("injected failure after the nonce was burned")
        real = st._cas_transition

        def exploding(*a, **kw):
            raise boom

        st._cas_transition = exploding
        try:
            await cp.submit_decision(**signed)
            require(False, "the injected failure did not propagate")
        except RuntimeError as exc:
            require(exc is boom, f"wrong exception: {exc}")
        finally:
            st._cas_transition = real

        # read back from an INDEPENDENT connection: nothing may have been committed
        require_equal(_rows(tmp, "SELECT consumed FROM challenges")[0]["consumed"], 0,
                      "the nonce stayed burned after the decision rolled back")
        require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"], S.PENDING,
                      "the request moved")
        require_equal(_success_audits(tmp, "apr-1"), [], "a success audit survived a rollback")
        # and the honest decision still works afterwards
        res, status = await cp.submit_decision(**signed)
        require_equal(status, "ok", f"the retry after a rollback failed: {status}")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_f_a_revoke_committed_before_the_decision_wins():
    """The reproduced TOCTOU: a second connection revokes inside the decision's flight."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp)
        await _request(st)
        _, _, raw = await _issue(cp, st, ctx, "apr-1")
        signed = H.sign_decision(ctx, raw)

        st2, cp2 = await _open(tmp)                 # an independent connection, like the CLI
        real = st.commit_decision
        fired = []

        async def interposed(**kw):
            if not fired:
                fired.append(1)
                await cp2.revoke_device(ctx.device_id, reason="operator")
            return await real(**kw)

        st.commit_decision = interposed
        res, status = await cp.submit_decision(**signed)
        require(fired, "the racing revoke never ran — the probe is vacuous")
        require(res is None, "a revoked device decided")
        require_equal(status, "device_revoked", f"refusal reason: {status}")
        require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"], S.PENDING,
                      "an approval was committed for a revoked device")
        require_equal(_success_audits(tmp, "apr-1"), [], "a success audit was written")
        await st.close()
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_f_expiry_racing_the_decision_is_fail_closed():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp)
        await _request(st)
        _, _, raw = await _issue(cp, st, ctx, "apr-1")
        signed = H.sign_decision(ctx, raw)
        real = st.commit_decision
        fired = []

        async def interposed(**kw):
            if not fired:
                fired.append(1)
                con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
                try:
                    con.execute("UPDATE approval_requests SET expires_at=? WHERE approval_id=?",
                                (time.time() - 1, "apr-1"))
                finally:
                    con.close()
            return await real(**kw)

        st.commit_decision = interposed
        res, status = await cp.submit_decision(**signed)
        require(fired, "the expiry never landed — the probe is vacuous")
        require(res is None, "an expired request was decided")
        require_equal(status, "request_expired", status)
        require_equal(_rows(tmp, "SELECT consumed FROM challenges")[0]["consumed"], 0,
                      "the nonce was burned by a refused decision")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_f_a_reenrolled_device_cannot_use_the_old_key():
    """The signature is verified before the transaction; the row is checked inside it."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp)
        await _request(st)
        _, _, raw = await _issue(cp, st, ctx, "apr-1")
        signed = H.sign_decision(ctx, raw)
        other = H.new_device(ctx.device_id)
        real = st.commit_decision
        fired = []

        async def interposed(**kw):
            if not fired:
                fired.append(1)
                con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
                try:
                    con.execute("UPDATE devices SET public_key_x963=?, key_id=? "
                                "WHERE device_id=?",
                                (other.appr_x963.hex(), other.key_id, ctx.device_id))
                finally:
                    con.close()
            return await real(**kw)

        st.commit_decision = interposed
        res, status = await cp.submit_decision(**signed)
        require(fired, "the rebinding never landed — the probe is vacuous")
        require(res is None, "a decision signed with a superseded key was committed")
        require_equal(status, "device_rebound", status)
        require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"], S.PENDING,
                      "the request moved")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# G — the audit records at most one success
# =====================================================================
async def t_g_a_refused_decision_never_writes_a_success_event():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp)
        await _request(st)
        _, _, raw = await _issue(cp, st, ctx, "apr-1")
        signed = H.sign_decision(ctx, raw)
        require_equal((await cp.submit_decision(**signed))[1], "ok", "setup")
        require_equal((await cp.submit_decision(**signed))[1], "not_pending", "replay")
        require_equal(_success_audits(tmp, "apr-1"), ["state_APPROVED"],
                      _success_audits(tmp, "apr-1"))
        # Honest limit: a replay refused by the control plane's early state check writes NO
        # audit row at all. That satisfies invariant G ("losers MAY be auditable, but must
        # not look like a second success") and is deliberately not widened here — auditing
        # every authentication failure is an explicitly deferred item, and doing it on an
        # unauthenticated path would hand an attacker an audit-flooding lever.
        require_equal([r["event"] for r in _rows(
            tmp, "SELECT event FROM audit WHERE approval_id=? AND event LIKE 'state_%'",
            ("apr-1",))], ["state_APPROVED"], "a second success event was written")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# Phase 9 — failure semantics leave nothing half-trusted
# =====================================================================
async def t_lock_contention_fails_closed_and_commits_nothing():
    """Another writer holds the lock: the decision must fail, not half-apply.

    busy_timeout is lowered on the deciding connection only, so the test costs milliseconds
    instead of five seconds. What is being asserted is the OUTCOME of losing the lock, not
    how long SQLite waits for it.
    """
    tmp = os.path.realpath(tempfile.mkdtemp())
    blocker = None
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp)
        await _request(st)
        _, _, raw = await _issue(cp, st, ctx, "apr-1")
        signed = H.sign_decision(ctx, raw)
        await st._run(lambda: st._conn.execute("PRAGMA busy_timeout=150"))

        blocker = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE approval_requests SET error='holding' WHERE approval_id=?",
                        ("apr-1",))
        try:
            await cp.submit_decision(**signed)
            require(False, "the decision succeeded while another writer held the lock")
        except sqlite3.OperationalError as exc:
            require("locked" in str(exc) or "busy" in str(exc), f"unexpected error: {exc}")
        blocker.execute("ROLLBACK")
        blocker.close()
        blocker = None

        require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"], S.PENDING,
                      "a lock failure left the request moved")
        require_equal(_rows(tmp, "SELECT consumed FROM challenges")[0]["consumed"], 0,
                      "a lock failure left the nonce burned")
        require_equal(_success_audits(tmp, "apr-1"), [], "a lock failure wrote a success")
        # and the honest decision still works once the lock is gone
        require_equal((await cp.submit_decision(**signed))[1], "ok",
                      "the retry after lock contention failed")
        await st.close()
    finally:
        if blocker is not None:
            blocker.close()
        shutil.rmtree(tmp, ignore_errors=True)


async def t_every_refusal_reason_leaves_the_request_untouched():
    """Walk the refusal paths and require the same thing of all of them: nothing moved."""
    cases = []

    async def check(label, setup):
        tmp = os.path.realpath(tempfile.mkdtemp())
        try:
            st, cp = await _open(tmp)
            ctx = await H.enroll_attested(cp)
            await _request(st)
            _, _, raw = await _issue(cp, st, ctx, "apr-1")
            signed = H.sign_decision(ctx, raw)
            await setup(st, cp, ctx, tmp)
            before = _rows(tmp, "SELECT state FROM approval_requests")[0]["state"]
            burned = _rows(tmp, "SELECT consumed FROM challenges")[0]["consumed"]
            res, status = await cp.submit_decision(**signed)
            require(res is None, f"{label}: accepted")
            require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"],
                          before, f"{label}: the refusal still moved the request")
            require_equal(_rows(tmp, "SELECT consumed FROM challenges")[0]["consumed"],
                          burned, f"{label}: the refusal changed the nonce")
            require_equal(_success_audits(tmp, "apr-1"), [], f"{label}: success audited")
            cases.append((label, status))
            await st.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def sql(stmt, args):
        async def _apply(st, cp, ctx, tmp):
            con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
            try:
                con.execute(stmt, args)
            finally:
                con.close()
        return _apply

    async def revoke(st, cp, ctx, tmp):
        await cp.revoke_device(ctx.device_id, reason="operator")

    await check("revoked device", revoke)
    await check("stale request", sql("UPDATE approval_requests SET state=? WHERE approval_id=?",
                                     (S.DENIED, "apr-1")))
    await check("expired request", sql(
        "UPDATE approval_requests SET expires_at=? WHERE approval_id=?",
        (time.time() - 1, "apr-1")))
    await check("stale nonce", sql("UPDATE challenges SET consumed=1", ()))
    await check("attestation failed", sql(
        "UPDATE devices SET attestation_status=?", (S.ATT_FAILED,)))
    await check("no current enrollment", sql(
        "UPDATE devices SET current_enrollment_id=NULL", ()))
    reasons = dict(cases)
    require_equal(reasons["revoked device"], "device_revoked", reasons)
    require_equal(reasons["expired request"], "request_expired", reasons)
    require_equal(reasons["no current enrollment"], "no_current_enrollment", reasons)
    require_equal(len(set(reasons.values())), len(reasons),
                  f"refusal reasons are conflated: {reasons}")


async def t_an_invalid_signature_never_reaches_the_decision():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp)
        await _request(st)
        _, _, raw = await _issue(cp, st, ctx, "apr-1")
        signed = dict(H.sign_decision(ctx, raw))
        bad = bytearray(P.b64d(signed["signature_b64"]))
        bad[-1] ^= 0xFF
        signed["signature_b64"] = P.b64e(bytes(bad))
        res, status = await cp.submit_decision(**signed)
        require(res is None, "a forged signature decided")
        require_equal(status, "bad_signature", status)
        require_equal(_rows(tmp, "SELECT consumed FROM challenges")[0]["consumed"], 0,
                      "a forged signature burned the nonce")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_no_schema_change_was_required():
    """P1B is transaction discipline, not a new data model. Migration surface: none."""
    src = open(STORE_SRC, encoding="utf-8").read()
    schema = src[src.index("_SCHEMA = "):src.index("class ApprovalControlStore")]
    for table in ("approval_requests", "challenges", "enrollment_tokens"):
        require(f"CREATE TABLE IF NOT EXISTS {table}" in schema, f"{table} vanished")
    require("p1b" not in schema.lower(), "P1B added schema — it was not supposed to need any")


# =====================================================================
# Independence — the hardening must not serialise unrelated work
# =====================================================================
async def t_two_different_requests_can_both_succeed_concurrently():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp)
        await _request(st, "apr-A")
        await _request(st, "apr-B")
        # Challenges must be issued before the race: issuing one invalidates older OPEN
        # challenges for the SAME approval_id, which is per-request and does not interfere.
        signed = {}
        for aid in ("apr-A", "apr-B"):
            _, _, raw = await _issue(cp, st, ctx, aid)
            signed[aid] = H.sign_decision(ctx, raw)
        await st.close()

        async def attempt(barrier, aid):
            st2, cp2 = await _open(tmp)
            try:
                barrier.wait()
                return await cp2.submit_decision(**signed[aid])
            finally:
                await st2.close()

        out = _race([(lambda b, a=a: attempt(b, a)) for a in ("apr-A", "apr-B")])
        statuses = [o[1] for o in out]
        require_equal(statuses, ["ok", "ok"],
                      f"independent requests interfered with each other: {out}")
        states = {r["approval_id"]: r["state"] for r in
                  _rows(tmp, "SELECT approval_id, state FROM approval_requests")}
        require_equal(states, {"apr-A": S.APPROVED, "apr-B": S.APPROVED}, states)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_the_same_device_can_decide_two_requests():
    """No accidental global device lock: one approver, two legitimate decisions."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp)
        for n, aid in enumerate(("apr-1", "apr-2"), start=1):
            await _request(st, aid)
            _, _, raw = await _issue(cp, st, ctx, aid)
            res, status = await cp.submit_decision(**H.sign_decision(ctx, raw, counter=n))
            require_equal(status, "ok", f"{aid}: {status}")
        states = {r["approval_id"]: r["state"] for r in
                  _rows(tmp, "SELECT approval_id, state FROM approval_requests")}
        require_equal(states, {"apr-1": S.APPROVED, "apr-2": S.APPROVED}, states)
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# Phase 2 — MODEL MAY REQUEST. MODEL MAY NOT APPROVE.
# =====================================================================
async def t_no_llm_path_reaches_approval_or_revocation_authority():
    """P1B must not have opened a second door. Asserted over the tracked source.

    The claim is narrow and checkable: the authority entry points live in
    `security/mobile_approval/`; N2 adds one named task-proof nonce consumer.
    Task/Voice/Face-ID domain separation and nonce non-interchangeability are
    exercised in test_app_task_start_proof. This exception does not authorize
    decisions, enrollment consumption or revocation from the task endpoint. It is NOT
    a claim that the process is sandboxed against arbitrary Python.
    """
    import pathlib
    pkg = pathlib.Path(REPO) / "src" / "solvio" / "security" / "mobile_approval"
    root = pathlib.Path(REPO) / "src" / "solvio"
    authority = ("submit_decision", "commit_decision", "revoke_identities", "revoke_device",
                 "claim_execution", "consume_enrollment_token", "consume_challenge")
    leaks = []
    for path in sorted(root.rglob("*.py")):
        if pkg in path.parents or path.parent == pkg:
            continue
        text = path.read_text(encoding="utf-8")
        for sym in authority:
            if path == root / "agent_runtime" / "task_start_proof.py" and sym == "consume_challenge":
                require_equal(text.count("self.store._cas_consume_challenge("), 1)
                require('_CHALLENGE_PREFIX = "app-task-start:"' in text)
                require('DOMAIN_TASK_START = b"SOLVIO_APP_TASK_START_V1"' in text)
                continue
            if sym in text:
                leaks.append(f"{path.relative_to(root)}::{sym}")
    require_equal(leaks, [], f"authority reachable outside mobile_approval: {leaks}")

    broker = (root / "security" / "approval.py").read_text(encoding="utf-8")
    require("def approve(" in broker, "the S1 broker lost its approve entry point")
    callers = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "approval.py":
            continue
        text = path.read_text(encoding="utf-8")
        if ".approve(" in text:
            if path == root / "agent_runtime" / "task_endpoint.py":
                # N2: Kostenobergrenze ist keine S1-Aktionsfreigabe. Der
                # authentifizierte Owner-Handler darf genau CostLedger.approve
                # rufen; jeder weitere approve-Aufruf bleibt ein Fund.
                import ast
                calls = [n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute) and n.func.attr == "approve"]
                require_equal(len(calls), 1)
                require_equal(ast.unparse(calls[0].func.value), "orch.costs")
                continue
            callers.append(str(path.relative_to(root)))
    require_equal(callers, ["security/mobile_approval/bridge.py"],
                  f"S1 approval is reachable from {callers} — only the mobile bridge may")


# =====================================================================
# Phase 10 — no alternative writer may reach the same authority state
# =====================================================================
async def t_no_bypass_writer_reaches_the_request_state():
    """Every production write to approval_requests.state must go through the CAS."""
    import re
    src = open(STORE_SRC, encoding="utf-8").read()
    writes = [m.start() for m in re.finditer(r"UPDATE approval_requests SET state=", src)]
    require_equal(len(writes), 3,
                  f"{len(writes)} writers of approval_requests.state — expected the CAS "
                  f"transition, the P1A execution claim and the guarded TTL sweeper")
    # The invariant is not the COUNT but the shape: every one of them must carry a state
    # predicate, so none can resurrect or create authority regardless of what it is called.
    for pos in writes:
        stmt = src[pos:pos + 800]
        require("state=?" in stmt.split("WHERE", 1)[-1] or "state IN (?,?)" in stmt
                or "ar.state=?" in stmt,
                f"a state write without a state predicate near offset {pos}")
    require("def _cas_transition(" in src, "the central transition primitive is gone")
    ctl = open(CONTROL_SRC, encoding="utf-8").read()
    require("UPDATE approval_requests" not in ctl, "the control plane writes state directly")
    require("commit_decision(" in ctl, "the control plane no longer uses the atomic decision")
    for gone in ("self.store.consume_challenge(", "self.store.bump_app_attest_counter("):
        require(gone not in ctl,
                f"{gone} is still a separate step outside the atomic decision")


async def t_the_decision_boundary_carries_no_expected_identity():
    import inspect
    params = set(inspect.signature(
        S.ApprovalControlStore.commit_decision).parameters) - {"self"}
    require_equal(params, {"approval_id", "device_id", "new_state", "challenge_nonce",
                           "challenge_payload_sha256", "verified_public_key_x963",
                           "verified_app_attest_public_key", "app_attest_counter",
                           "allowed_environments"},
                  f"the decision boundary changed shape: {sorted(params)}")
    for banned in ("principal", "action_digest", "approval_pubkey_sha256", "expected_state"):
        require(banned not in params,
                f"{banned} must come from the stored request, not from the caller")


def _transitive_body(src, name, depth=3):
    """Source of `name` plus, transitively, every `self._x(...)` helper it calls.

    HYGIENE/H7: the previous version grepped for literals inside ONE function, so extracting
    a helper broke it even though the property was untouched. What matters is that the work
    happens inside the transaction — including work the method delegates to — so the check
    follows the call graph instead of assuming everything is inlined.
    """
    import ast as _ast
    tree = _ast.parse(src)
    funcs = {}

    class _Collect(_ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            funcs.setdefault(node.name, node)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node):
            funcs.setdefault(node.name, node)
            self.generic_visit(node)

    _Collect().visit(tree)
    seen, out, stack = set(), [], [(name, depth)]
    while stack:
        fn, left = stack.pop()
        if fn in seen or fn not in funcs or left < 0:
            continue
        seen.add(fn)
        node = funcs[fn]
        out.append(_ast.get_source_segment(src, node) or "")
        for call in _ast.walk(node):
            if isinstance(call, _ast.Call) and isinstance(call.func, _ast.Attribute) \
                    and isinstance(call.func.value, _ast.Name) \
                    and call.func.value.id == "self":
                stack.append((call.func.attr, left - 1))
    return "\n".join(out), seen


async def t_the_decision_is_one_transaction():
    src = open(STORE_SRC, encoding="utf-8").read()
    own = src[src.index("def _commit_decision("):]
    own = own[:own.index("\n    def ")]
    require_equal(own.count('BEGIN IMMEDIATE'), 1, "the decision is not one transaction")
    require_equal(own.count('self._conn.execute("COMMIT")'), 1,
                  "the decision transaction has more than one commit point")
    body, called = _transitive_body(src, "_commit_decision")
    for needed in ("FROM revocations", "_cas_consume_challenge", "_cas_transition",
                   "current_enrollment_id", "app_attest_counter"):
        require(needed in body,
                f"{needed} is not reachable inside the decision transaction "
                f"(followed: {sorted(called)})")
    # Nothing it delegates to may open a transaction of its own.
    helpers = body.replace(own, "")
    require_equal(helpers.count("BEGIN IMMEDIATE"), 0,
                  "a helper called inside the decision opens its own transaction")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
