"""P1A.1 — the four findings that blocked the P1A merge.

F1  admin CLI resolved key identifiers by PREFIX with first-match-wins, so a short input
    could revoke a DIFFERENT key than the operator meant while printing success.
F2  the environment policy failed OPEN when no policy was configured.
F3  execute_approved never re-checked revocation, so a revoke that landed after the
    approval but before execution was ignored.
F4  the F5.1 regression test asserted against its own copy of the production SQL.

Everything here drives production code paths. Direct: python test_p1a_1_remediation.py
"""
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
from solvio.security.mobile_approval import admin_cli
from solvio.security.mobile_approval import app_attest as AA
from solvio.security.mobile_approval import bridge as B
from solvio.security.mobile_approval import control as C
from solvio.security.mobile_approval import crypto
from solvio.security.mobile_approval import identity
from solvio.security.mobile_approval import protocol as P
from solvio.security.mobile_approval import store as S

APP_ID = "WQ8CG7R53R.de.solvio.approvals"
DB = "approval_control.sqlite3"          # the filename the admin CLI opens


async def _open(tmp, *, allowed=None):
    st = S.ApprovalControlStore(os.path.join(tmp, DB))
    await st.open()
    cp = C.MobileApprovalControlPlane(
        st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
        attest_verifier=H.fake_verifier(), app_id=APP_ID,
        allowed_environments=allowed)
    return st, cp


async def _wire(tmp, **kw):
    st, cp = await _open(tmp, **kw)
    approver = B.MobileApprover()
    broker = ApprovalBroker(approver=approver)
    return st, cp, B.MobileApprovalCoordinator(cp, broker, approver), approver


def _executor(calls):
    async def run(action):
        calls.append(action)
        return True, {"changed_files": []}
    return run


# =====================================================================
# F1 — CLI identifier resolution: exact only, fail closed
# =====================================================================
async def _two_devices(tmp):
    st, cp = await _open(tmp)
    a = await H.enroll_attested(cp, device_id="dev-A", transport_cred="ta")
    b = await H.enroll_attested(cp, device_id="dev-B", transport_cred="tb")
    await st.close()
    return (crypto.fingerprint(a.appr_x963), a.key_id,
            crypto.fingerprint(b.appr_x963), b.key_id)


async def _status(tmp, device_id):
    st = S.ApprovalControlStore(os.path.join(tmp, DB))
    await st.open()
    try:
        return (await st.get_device(device_id))["status"]
    finally:
        await st.close()


async def _revocations(tmp):
    st = S.ApprovalControlStore(os.path.join(tmp, DB))
    await st.open()
    try:
        return await st.list_revocations()
    finally:
        await st.close()


def t_f1_short_prefix_is_rejected():
    """The original bug: a 6-char prefix silently resolved to whatever device came first."""
    tmp = tempfile.mkdtemp()
    try:
        fa, _, _, _ = asyncio.run(_two_devices(tmp))
        rc = admin_cli.main(["--state-dir", tmp, "revoke-key", fa[:6]])
        assert rc != 0, "a prefix was accepted"
        assert asyncio.run(_revocations(tmp)) == [], "a prefix wrote a revocation"
        assert asyncio.run(_status(tmp, "dev-A")) == S.DEVICE_ACTIVE
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_f1_long_prefix_is_rejected():
    """Half a fingerprint still is not a fingerprint — near-misses must fail closed too."""
    tmp = tempfile.mkdtemp()
    try:
        fa, _, _, _ = asyncio.run(_two_devices(tmp))
        assert admin_cli.main(["--state-dir", tmp, "revoke-key", fa[:32]]) != 0
        assert asyncio.run(_revocations(tmp)) == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_f1_prefix_never_revokes_the_wrong_device():
    """The security property, stated directly: an input that is a prefix of device B's key
    must never take device B (or anything else) down."""
    tmp = tempfile.mkdtemp()
    try:
        _, _, fb, _ = asyncio.run(_two_devices(tmp))
        # NB: n=16 is deliberately absent. key_id is sha256(x963)[:16] and the fingerprint
        # is sha256(x963), so the 16-hex "prefix" IS that key's key_id — it resolves to the
        # SAME key, never a foreign one. That coincidence is pinned separately below.
        for n in (2, 8, 15, 17, 40, 63):
            assert admin_cli.main(["--state-dir", tmp, "revoke-key", fb[:n]]) != 0, n
        assert asyncio.run(_status(tmp, "dev-A")) == S.DEVICE_ACTIVE
        assert asyncio.run(_status(tmp, "dev-B")) == S.DEVICE_ACTIVE
        assert asyncio.run(_revocations(tmp)) == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_f1_the_two_accepted_formats_denote_the_same_key():
    """The only reason accepting two formats is safe: key_id is sha256(x963)[:16] and the
    fingerprint is sha256(x963), so the short form can never denote a different key than
    the long form. If that ever stops holding, the CLI would need a single format."""
    tmp = tempfile.mkdtemp()
    try:
        fa, kid_a, fb, kid_b = asyncio.run(_two_devices(tmp))
        assert fa[:16] == kid_a and fb[:16] == kid_b
        assert admin_cli.main(["--state-dir", tmp, "revoke-key", fa[:16]]) == 0
        assert asyncio.run(_status(tmp, "dev-A")) == S.DEVICE_REVOKED
        assert asyncio.run(_status(tmp, "dev-B")) == S.DEVICE_ACTIVE
        assert [r["value"] for r in asyncio.run(_revocations(tmp))] == [fa]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_f1_malformed_identifiers_are_rejected():
    tmp = tempfile.mkdtemp()
    try:
        asyncio.run(_two_devices(tmp))
        for bad in ("", "   ", "zz" * 32, "dev-A", "0x" + "a" * 62, "a" * 65, "a" * 63):
            assert admin_cli.main(["--state-dir", tmp, "revoke-key", bad]) != 0, bad
        assert asyncio.run(_revocations(tmp)) == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_f1_exact_fingerprint_and_key_id_both_work():
    """Both documented formats must actually work — a fail-closed tool nobody can use is
    not a working revoke path."""
    tmp = tempfile.mkdtemp()
    try:
        fa, _, fb, kid_b = asyncio.run(_two_devices(tmp))
        assert admin_cli.main(["--state-dir", tmp, "revoke-key", fa]) == 0
        assert asyncio.run(_status(tmp, "dev-A")) == S.DEVICE_REVOKED
        assert asyncio.run(_status(tmp, "dev-B")) == S.DEVICE_ACTIVE   # blast radius = 1
        assert admin_cli.main(["--state-dir", tmp, "revoke-key", kid_b]) == 0
        assert asyncio.run(_status(tmp, "dev-B")) == S.DEVICE_REVOKED
        vals = {r["value"] for r in asyncio.run(_revocations(tmp))}
        assert vals == {fa, fb}, vals
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_f1_uppercase_and_whitespace_normalise_not_widen():
    """Normalisation may only change case/padding — it must never turn a non-match into a
    match."""
    tmp = tempfile.mkdtemp()
    try:
        fa, _, _, _ = asyncio.run(_two_devices(tmp))
        assert admin_cli.main(["--state-dir", tmp, "revoke-key", "  " + fa.upper() + " "]) == 0
        assert asyncio.run(_status(tmp, "dev-A")) == S.DEVICE_REVOKED
        assert asyncio.run(_status(tmp, "dev-B")) == S.DEVICE_ACTIVE
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_f1_ambiguous_key_id_fails_closed():
    """Two devices sharing a key_id but holding DIFFERENT approval keys: the tool must
    refuse rather than pick one."""
    tmp = tempfile.mkdtemp()
    try:
        async def build():
            st, cp = await _open(tmp)
            a = await H.enroll_attested(cp, device_id="dev-A", transport_cred="ta")
            b = await H.enroll_attested(cp, device_id="dev-B", transport_cred="tb")
            # force the collision directly in the store: same key_id, different keys
            await st._run(lambda: st._conn.execute(
                "UPDATE devices SET key_id=? WHERE device_id=?", (a.key_id, "dev-B")))
            await st.close()
            return a.key_id, crypto.fingerprint(a.appr_x963), crypto.fingerprint(b.appr_x963)
        kid, fa, fb = asyncio.run(build())
        assert admin_cli.main(["--state-dir", tmp, "revoke-key", kid]) != 0, "ambiguity accepted"
        assert asyncio.run(_revocations(tmp)) == [], "an ambiguous input wrote a revocation"
        assert asyncio.run(_status(tmp, "dev-A")) == S.DEVICE_ACTIVE
        assert asyncio.run(_status(tmp, "dev-B")) == S.DEVICE_ACTIVE
        # the documented escape hatch resolves it unambiguously
        assert admin_cli.main(["--state-dir", tmp, "revoke-key", fb]) == 0
        assert asyncio.run(_status(tmp, "dev-B")) == S.DEVICE_REVOKED
        assert asyncio.run(_status(tmp, "dev-A")) == S.DEVICE_ACTIVE
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_f1_help_names_the_exact_accepted_formats():
    txt = admin_cli.build_parser().format_help() + admin_cli.__doc__
    for sub in ("revoke-key",):
        assert sub in txt
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            admin_cli.build_parser().parse_args(["revoke-key", "--help"])
        except SystemExit:
            pass
    h = buf.getvalue()
    assert "64" in h and "16" in h, h            # both accepted lengths are stated
    assert "Präfix" in h or "prefix" in h, h     # and that prefixes are not accepted


# =====================================================================
# F2 — environment policy fails CLOSED in both directions
# =====================================================================
def _blocked(policy, env):
    """Drive the production predicate with a synthetic device row."""
    cp = C.MobileApprovalControlPlane.__new__(C.MobileApprovalControlPlane)
    cp.allowed_environments = policy
    return cp._environment_blocked({"environment": env, "device_id": "d"})


def t_f2_no_policy_denies_everything():
    """The finding: "no policy" used to mean "allow anything"."""
    for policy in (None, frozenset(), set(), ()):
        for env in (AA.ENV_PRODUCTION, AA.ENV_DEVELOPMENT, None, "", "weird"):
            assert _blocked(policy, env) is True, (policy, env)


def t_f2_null_or_empty_stored_environment_is_denied():
    """An unattested/legacy row carries environment=NULL; that is not a pass."""
    for policy in (frozenset({AA.ENV_PRODUCTION}), frozenset({AA.ENV_DEVELOPMENT}),
                   frozenset({AA.ENV_PRODUCTION, AA.ENV_DEVELOPMENT})):
        assert _blocked(policy, None) is True
        assert _blocked(policy, "") is True


def t_f2_mismatch_denied_match_allowed():
    prod, dev = AA.ENV_PRODUCTION, AA.ENV_DEVELOPMENT
    assert _blocked(frozenset({prod}), dev) is True        # prod policy + dev device
    assert _blocked(frozenset({dev}), prod) is True        # dev policy + prod device
    assert _blocked(frozenset({prod}), prod) is False      # prod + prod
    assert _blocked(frozenset({dev}), dev) is False        # dev + dev


def t_f2_unknown_environment_is_denied():
    for env in ("weird", "PRODUCTION", "prod", "sandbox", "development "):
        assert _blocked(frozenset({AA.ENV_PRODUCTION, AA.ENV_DEVELOPMENT}), env) is True, env


def t_f2_verifier_and_control_plane_share_the_policy_semantics():
    """The reason "no policy" had to mean "allow all" was that the fake verifier carried no
    policy at all. It must now expose the same attribute as the real one."""
    fake = AA.FakeAppAttestVerifier(environment=AA.ENV_DEVELOPMENT)
    assert fake.allowed_environments == {AA.ENV_DEVELOPMENT}
    fake_p = AA.FakeAppAttestVerifier(environment=AA.ENV_PRODUCTION)
    assert fake_p.allowed_environments == {AA.ENV_PRODUCTION}


def t_f2_policy_survives_restart_and_still_denies():
    """Policy is config, not state: reopening the store must not silently widen it."""
    tmp = tempfile.mkdtemp()
    try:
        async def go():
            st, cp = await _open(tmp, allowed=frozenset({AA.ENV_DEVELOPMENT}))
            ctx = await H.enroll_attested(cp, transport_cred="tc")
            assert await cp.verify_transport_cred(ctx.device_id, "tc") is True
            await st.close()
            # restart with a production-only policy: the same stored device is now out
            st2, cp2 = await _open(tmp, allowed=frozenset({AA.ENV_PRODUCTION}))
            assert await cp2.verify_transport_cred(ctx.device_id, "tc") is False
            await st2.close()
            # an EXPLICITLY empty policy must fail closed, not open
            st3, cp3 = await _open(tmp, allowed=frozenset())
            assert cp3.allowed_environments in (None, frozenset())
            assert await cp3.verify_transport_cred(ctx.device_id, "tc") is False
            await st3.close()
            # and `allowed=None` is NOT "no policy": it inherits the verifier's real one,
            # which is exactly the shared-semantics property F2 asks for.
            st4, cp4 = await _open(tmp, allowed=None)
            assert cp4.allowed_environments == frozenset({AA.ENV_DEVELOPMENT})
            assert await cp4.verify_transport_cred(ctx.device_id, "tc") is True
            await st4.close()
        asyncio.run(go())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# F3 — revocation must beat an already-APPROVED request
# =====================================================================
async def _approved(tmp, ws, **kw):
    st, cp, coord, approver = await _wire(tmp, **kw)
    ctx = await H.enroll_attested(cp, transport_cred="tc")
    approval_id = await coord.request_codex_modify(
        principal="local-owner", task="edit README", workspace=ws,
        human_summary="Codex will edit README")
    # route the decision through the BRIDGE, not the control plane directly — only
    # apply_mobile_decision confirms the S1 gate, and S1 is the real execution gate.
    wire, cs = await cp.issue_challenge(approval_id=approval_id, device_id=ctx.device_id)
    assert cs == "ok", cs
    res, s = await coord.apply_mobile_decision(
        **H.sign_decision(ctx, P.b64d(wire["payload_b64"])))
    assert s == "ok" and res["decision"] == P.DECISION_APPROVE, s
    req = await st.get_request(approval_id)
    assert req["state"] == S.APPROVED
    return st, cp, coord, approver, ctx, approval_id


async def _revoked_after_approval_blocks(kind):
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _, ctx, aid = await _approved(tmp, ws)
        dev = await st.get_device(ctx.device_id)
        if kind == "device":
            await cp.revoke_device(ctx.device_id, reason="stolen")
        elif kind == "approval_key":
            await cp.revoke_approval_key(crypto.fingerprint(ctx.appr_x963), reason="stolen")
        else:
            await cp.revoke_app_attest_key(dev["app_attest_key_id"], reason="stolen")

        calls = []
        out, status = await coord.execute_approved(aid, _executor(calls))
        assert out is None, out
        assert status == "device_revoked", status
        assert calls == [], "the executor ran on revoked authority"
        assert (await st.get_request(aid))["state"] == S.APPROVED, "row moved to EXECUTING"
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


def t_f3_revoked_device_blocks_execution():
    asyncio.run(_revoked_after_approval_blocks("device"))


def t_f3_revoked_approval_key_blocks_execution():
    asyncio.run(_revoked_after_approval_blocks("approval_key"))


def t_f3_revoked_app_attest_key_blocks_execution():
    """P1A.1/F4 also asks for this one explicitly: app-attest-key revocation is a real
    revocation kind and must block execution like the other two."""
    asyncio.run(_revoked_after_approval_blocks("app_attest_key"))


async def t_f3_revoke_from_a_second_connection_still_wins():
    """The admin CLI is a SEPARATE PROCESS with its own SQLite connection, so a
    Python-side check-then-act would leave a window. Revoke through a second store object
    on the same file and require the execution claim to lose."""
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _, ctx, aid = await _approved(tmp, ws)
        stB, cpB = await _open(tmp)                      # independent connection
        await cpB.revoke_device(ctx.device_id, reason="cli")
        calls = []
        out, status = await coord.execute_approved(aid, _executor(calls))
        assert out is None and status == "device_revoked", status
        assert calls == []
        assert (await st.get_request(aid))["state"] == S.APPROVED
        await stB.close(); await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_f3_identity_comes_from_the_stored_row_not_the_caller():
    """execute_approved takes only an approval_id — the device identity it checks is read
    from the stored, already-bound approval context. Enrolling a second, healthy device
    must not launder a revoked one."""
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _, ctx, aid = await _approved(tmp, ws)
        await H.enroll_attested(cp, device_id="dev-healthy", transport_cred="th")
        await cp.revoke_device(ctx.device_id, reason="stolen")
        import inspect
        params = set(inspect.signature(coord.execute_approved).parameters)
        assert params == {"approval_id", "executor"}, params   # no device_id to spoof
        calls = []
        out, status = await coord.execute_approved(aid, _executor(calls))
        assert out is None and status == "device_revoked", status
        assert calls == []
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_f3_null_decided_device_fails_closed():
    """A legacy/damaged row with no bound device must not execute. The reviewer question
    this answers: what does the gate do when there is nothing to check?"""
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _, ctx, aid = await _approved(tmp, ws)
        await st._run(lambda: st._conn.execute(
            "UPDATE approval_requests SET decided_device=NULL WHERE approval_id=?", (aid,)))
        calls = []
        out, status = await coord.execute_approved(aid, _executor(calls))
        assert out is None and status == "unknown_device", status
        assert calls == []
        assert (await st.get_request(aid))["state"] == S.APPROVED
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_f3_refused_claim_does_not_leave_a_usable_confirmation():
    """A refused claim must spend the one-shot S1 confirmation, so a retry cannot slip
    through on the authority of the first (already revoked) decision."""
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, approver, ctx, aid = await _approved(tmp, ws)
        await cp.revoke_device(ctx.device_id, reason="stolen")
        calls = []
        out, s1 = await coord.execute_approved(aid, _executor(calls))
        assert out is None and s1 == "device_revoked", s1
        out2, s2 = await coord.execute_approved(aid, _executor(calls))
        assert out2 is None, out2
        assert calls == [], "a retry executed after a refused claim"
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_f3_rejection_is_audited():
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _, ctx, aid = await _approved(tmp, ws)
        await cp.revoke_device(ctx.device_id, reason="stolen")
        await coord.execute_approved(aid, _executor([]))
        rows = await st._run(lambda: st._conn.execute(
            "SELECT event, reason FROM audit WHERE approval_id=?", (aid,)).fetchall())
        events = {r["event"] for r in rows}
        assert any("reject" in e or "revok" in e for e in events), events
        assert S.EXECUTING not in {e.replace("state_", "") for e in events}, events
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_f3_healthy_device_still_executes():
    """The gate must not break the working path."""
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _, ctx, aid = await _approved(tmp, ws)
        calls = []
        out, status = await coord.execute_approved(aid, _executor(calls))
        assert status == "ok", status
        assert len(calls) == 1 and calls[0]["task"] == "edit README"
        assert (await st.get_request(aid))["state"] == S.CONSUMED
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_f3_boundary_revoke_during_execution_does_not_unwind():
    """HONEST BOUNDARY. The gate closes at the APPROVED->EXECUTING claim. A revoke that
    lands while the executor is already running does NOT undo the side effect — that is
    F4 (idempotency / reconciliation) and is out of scope here. This test pins the real
    behaviour so the documentation cannot drift away from it."""
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _, ctx, aid = await _approved(tmp, ws)
        started = asyncio.Event()
        calls = []

        async def slow(action):
            calls.append(action)
            started.set()
            await asyncio.sleep(0.05)          # revoke lands in here
            return True, {"changed_files": []}

        task = asyncio.create_task(coord.execute_approved(aid, slow))
        # never block forever: an early return would otherwise never set `started`
        await asyncio.wait_for(started.wait(), timeout=5)
        await cp.revoke_device(ctx.device_id, reason="too late")
        out, status = await task
        assert status == "ok", status                     # the side effect completed
        assert len(calls) == 1
        # ... and the device is nevertheless dead for everything that comes next
        assert (await st.get_device(ctx.device_id))["status"] == S.DEVICE_REVOKED
        aid2 = await coord.request_codex_modify(
            principal="local-owner", task="second edit", workspace=ws,
            human_summary="another one")
        _, s2 = await cp.issue_challenge(approval_id=aid2, device_id=ctx.device_id)
        assert s2 != "ok", s2
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_f3_revoked_key_blocks_reenrollment_and_execution_together():
    """The end-to-end operator story: revoke the KEY, and neither a fresh device_id nor the
    already-approved request gets anywhere."""
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _, ctx, aid = await _approved(tmp, ws)
        fp = crypto.fingerprint(ctx.appr_x963)
        await cp.revoke_approval_key(fp, reason="stolen phone")
        token, _ = await cp.create_enrollment_token("local-owner")
        res, s = await cp.begin_enrollment(
            enrollment_token=token, device_id="dev-fresh-id",
            approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
            app_attest_key_id=ctx.aakid, transport_cred="tc")
        assert res is None and s == "device_revoked", s
        calls = []
        out, status = await coord.execute_approved(aid, _executor(calls))
        assert out is None and status == "device_revoked", status
        assert calls == []
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
