"""S2A.1 P1A — authority hygiene + revocation hardening.

Pins the P1A invariants:

  * Revocation is DURABLE and TERMINAL, and is keyed on identities that survive a device
    record: device_id, approval-key fingerprint, App-Attest key id. Revoking the approval
    KEY is what stops a physical phone re-pairing under a fresh device_id — the gap that
    device-level revocation alone left open.
  * Every authority path consults revocation: enrollment, attestation completion,
    transport auth, challenge issue AND decision submit — so a challenge minted BEFORE the
    revoke is still refused when it comes back.
  * App Attest environment policy is explicit per runtime mode; a production runtime never
    accepts a development attestation, and there is no implicit mixed default.
  * The removed footguns stay removed: `claim_approved` (S1 bypass) and legacy
    `add_device` (blind status='ACTIVE').

Direct: python test_p1a_revocation.py
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
from solvio.security.mobile_approval import admin_cli
from solvio.security.mobile_approval import app_attest as AA
from solvio.security.mobile_approval import control as C
from solvio.security.mobile_approval import crypto
from solvio.security.mobile_approval import identity
from solvio.security.mobile_approval import protocol as P
from solvio.security.mobile_approval import store as S

WS = "/tmp/p1a-ws"


async def _open(tmp, *, envs=None):
    st = S.ApprovalControlStore(os.path.join(tmp, "approval_control.sqlite3"))
    await st.open()
    cp = C.MobileApprovalControlPlane(
        st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
        attest_verifier=H.fake_verifier(), app_id="TEAM.bundle",
        allowed_environments=envs)
    return st, cp


async def _request(cp):
    return await cp.create_request(principal="local-owner", tool="codex_task", mode="modify",
                                   task="edit README", workspace=WS, human_summary="s")


def _fp(ctx):
    return crypto.fingerprint(ctx.appr_x963)


# ---- 1-3: device revoke kills every authority path ----------------------
async def t_device_revoke_denies_transport_challenge_and_decision():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        ap = await _request(cp)
        wire, s = await cp.issue_challenge(approval_id=ap, device_id=ctx.device_id)
        assert s == "ok"
        await cp.revoke_device(ctx.device_id, reason="test")
        assert await cp.verify_transport_cred(ctx.device_id, "tc") is False        # (1)
        _, cs = await cp.issue_challenge(approval_id=ap, device_id=ctx.device_id)  # (2)
        assert cs != "ok", cs
        r, ds = await cp.submit_decision(                                          # (3)
            **H.sign_decision(ctx, P.b64d(wire["payload_b64"])))
        assert r is None and ds == "device_revoked", ds
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 4,7: approval-key revoke reaches an existing device + stale challenge
async def t_key_revoke_denies_existing_device_and_stale_challenge():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        ap = await _request(cp)
        wire, s = await cp.issue_challenge(approval_id=ap, device_id=ctx.device_id)
        assert s == "ok"
        affected = len((await cp.revoke_approval_key(_fp(ctx), reason="stolen")).affected_devices)   # (4)
        assert affected == 1
        assert await cp.verify_transport_cred(ctx.device_id, "tc") is False
        r, ds = await cp.submit_decision(                                    # (7) stale challenge
            **H.sign_decision(ctx, P.b64d(wire["payload_b64"])))
        assert r is None and ds == "device_revoked", ds
        assert (await st.get_request(ap))["state"] == S.PENDING
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 5,6: the gap device-revocation alone left open ----------------------
async def t_revoked_key_cannot_re_enroll_under_new_device_id():
    """The physical phone mints a NEW device_id on every pairing and reuses its Secure
    Enclave approval key. Before P1A that regained full authority with a fresh QR."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-old", transport_cred="tc")
        await cp.revoke_approval_key(_fp(ctx), reason="lost phone")

        # (5)+(6) same approval key, BRAND-NEW device_id, FRESH pairing token
        token, _ = await cp.create_enrollment_token("local-owner")
        res, s = await cp.begin_enrollment(
            enrollment_token=token, device_id="dev-new-id",
            approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
            app_attest_key_id=ctx.aakid, transport_cred="tc2")
        assert res is None and s == "device_revoked", s
        assert await st.get_device("dev-new-id") is None
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_revoked_app_attest_key_cannot_re_enroll():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = H.new_device("dev-aa")
        await cp.revoke_app_attest_key(ctx.aakid, reason="compromised app instance")
        token, _ = await cp.create_enrollment_token("local-owner")
        res, s = await cp.begin_enrollment(
            enrollment_token=token, device_id="dev-aa",
            approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
            app_attest_key_id=ctx.aakid, transport_cred="tc")
        assert res is None and s == "device_revoked", s
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 8: a second legitimate device is unaffected -------------------------
async def t_second_device_unaffected_by_revocation():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        a = await H.enroll_attested(cp, device_id="dev-a", transport_cred="ta")
        b = await H.enroll_attested(cp, device_id="dev-b", transport_cred="tb")
        await cp.revoke_approval_key(_fp(a), reason="only a")
        assert await cp.verify_transport_cred("dev-a", "ta") is False
        assert await cp.verify_transport_cred("dev-b", "tb") is True
        ap = await _request(cp)
        _, s = await cp.issue_challenge(approval_id=ap, device_id="dev-b")
        assert s == "ok", s
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 9: revocation survives a restart ------------------------------------
async def t_key_revocation_survives_restart():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        fp = _fp(ctx)
        await cp.revoke_approval_key(fp, reason="durable?")
        await st.close()
        st2, cp2 = await _open(tmp)                                   # restart
        assert await st2.is_revoked(S.REVOKE_APPROVAL_KEY, fp) is True
        assert await cp2.verify_transport_cred(ctx.device_id, "tc") is False
        token, _ = await cp2.create_enrollment_token("local-owner")
        res, s = await cp2.begin_enrollment(
            enrollment_token=token, device_id="dev-after-restart",
            approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
            app_attest_key_id=ctx.aakid, transport_cred="tc")
        assert res is None and s == "device_revoked", s
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 10,11: the admin CLI persists, and is not an LLM tool ---------------
def t_admin_cli_revoke_persists():
    """(10) Sync on purpose: admin_cli.main() owns its own event loop, exactly as a real
    operator invocation does."""
    tmp = tempfile.mkdtemp()
    try:
        async def _setup():
            st, cp = await _open(tmp)
            ctx = await H.enroll_attested(cp, device_id="dev-cli", transport_cred="tc")
            fp = _fp(ctx)
            await st.close()
            return fp
        fp = asyncio.run(_setup())

        assert admin_cli.main(["--state-dir", tmp, "revoke-key", fp,
                               "--reason", "cli-test"]) == 0
        assert admin_cli.main(["--state-dir", tmp, "devices"]) == 0
        assert admin_cli.main(["--state-dir", tmp, "revocations"]) == 0

        async def _verify():
            st2, cp2 = await _open(tmp)
            assert await st2.is_revoked(S.REVOKE_APPROVAL_KEY, fp) is True
            assert (await st2.get_device("dev-cli"))["status"] == S.DEVICE_REVOKED
            assert await cp2.verify_transport_cred("dev-cli", "tc") is False
            await st2.close()
        asyncio.run(_verify())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_admin_cli_not_exposed_to_llm():
    """(11) The admin surface must not be reachable as a model tool."""
    import solvio.security.mobile_approval.admin_cli as m
    assert not hasattr(m, "schema"), "admin CLI must not look like a tool"
    assert not hasattr(m, "expose_to_llm")
    tools_dir = os.path.join(os.path.dirname(__file__), "..", "src", "solvio", "tools")
    joined = ""
    for fn in os.listdir(tools_dir):
        if fn.endswith(".py"):
            with open(os.path.join(tools_dir, fn), encoding="utf-8") as f:
                joined += f.read()
    for forbidden in ("admin_cli", "revoke_device", "revoke_approval_key",
                      "revoke_app_attest_key", "add_revocation"):
        assert forbidden not in joined, f"{forbidden} leaked into the LLM tool surface"


# ---- 12-14: App Attest environment policy --------------------------------
def t_runtime_mode_policy_is_explicit_and_fail_closed():
    assert AA.allowed_environments_for_mode(AA.RUNTIME_PRODUCTION) == (AA.ENV_PRODUCTION,)
    assert AA.allowed_environments_for_mode(AA.RUNTIME_DEVELOPMENT) == (AA.ENV_DEVELOPMENT,)
    for bad in ("", "prod", "PRODUCTION ", None, "mixed"):              # (14)
        try:
            AA.allowed_environments_for_mode(bad)
            raise AssertionError(f"accepted ambiguous mode {bad!r}")
        except AA.RuntimeModeError:
            pass
    # the permissive default is gone: the caller MUST state the policy
    try:
        AA.AppleAppAttestVerifier(team_id="T", bundle_id="b")
        raise AssertionError("verifier built without an environment policy")
    except TypeError:
        pass
    try:
        AA.AppleAppAttestVerifier(team_id="T", bundle_id="b", allowed_environments=())
        raise AssertionError("verifier accepted an empty policy")
    except ValueError:
        pass


async def t_production_runtime_rejects_development_attested_device():
    """(12)+(13) A device attested under development keeps its stored value — we never
    rewrite DB state to fake a promotion — but loses authority under a production runtime."""
    tmp = tempfile.mkdtemp()
    try:
        # enrolled while running in development mode
        st, cp = await _open(tmp, envs=(AA.ENV_DEVELOPMENT,))
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        dev = await st.get_device(ctx.device_id)
        assert dev["environment"] == AA.ENV_DEVELOPMENT
        assert await cp.verify_transport_cred(ctx.device_id, "tc") is True   # (13) dev ok
        ap = await _request(cp)
        _, s = await cp.issue_challenge(approval_id=ap, device_id=ctx.device_id)
        assert s == "ok"
        await st.close()

        # same store, now served by a PRODUCTION runtime
        st2, cp2 = await _open(tmp, envs=(AA.ENV_PRODUCTION,))                # (12)
        row = await st2.get_device(ctx.device_id)
        assert row["environment"] == AA.ENV_DEVELOPMENT, "stored state must NOT be rewritten"
        assert row["attestation_status"] == S.ATT_ATTESTED
        assert await cp2.verify_transport_cred(ctx.device_id, "tc") is False
        ap2 = await _request(cp2)
        _, s2 = await cp2.issue_challenge(approval_id=ap2, device_id=ctx.device_id)
        assert s2 == "device_environment_not_allowed", s2
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 15: the removed footguns stay removed -------------------------------
def t_claim_approved_and_add_device_are_gone():
    assert not hasattr(C.MobileApprovalControlPlane, "claim_approved"), \
        "claim_approved must not exist: it bypassed the S1 gate"
    assert not hasattr(S.ApprovalControlStore, "add_device"), \
        "legacy add_device must not exist: blind status='ACTIVE'"
    src = os.path.join(os.path.dirname(__file__), "..", "src", "solvio", "security",
                       "mobile_approval")
    for fn, forbidden in (("store.py", "INSERT OR REPLACE INTO devices"),):
        with open(os.path.join(src, fn), encoding="utf-8") as f:
            body = f.read()
        assert forbidden not in body, f"{forbidden} still present in {fn}"



# ---- 11: admin CLI revoke-device persists --------------------------------
def t_admin_cli_revoke_device_persists():
    tmp = tempfile.mkdtemp()
    try:
        async def _setup():
            st, cp = await _open(tmp)
            await H.enroll_attested(cp, device_id="dev-cli-d", transport_cred="tc")
            await st.close()
        asyncio.run(_setup())

        assert admin_cli.main(["--state-dir", tmp, "revoke-device", "dev-cli-d",
                               "--reason", "cli-device"]) == 0
        assert admin_cli.main(["--state-dir", tmp, "list-devices"]) == 0      # alias
        assert admin_cli.main(["--state-dir", tmp, "list-revocations"]) == 0  # alias

        async def _verify():
            st2, cp2 = await _open(tmp)
            assert await st2.is_revoked(S.REVOKE_DEVICE, "dev-cli-d") is True
            assert (await st2.get_device("dev-cli-d"))["status"] == S.DEVICE_REVOKED
            assert await cp2.verify_transport_cred("dev-cli-d", "tc") is False
            await st2.close()
        asyncio.run(_verify())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- PHASE 13: explicit security-boundary proofs -------------------------
def t_boundary_llm_cannot_reach_revocation_or_admin():
    """The model's tool surface must contain no revocation/admin capability at all, and the
    gateway must expose no revoke route (no remote/web-content authority either)."""
    root = os.path.join(os.path.dirname(__file__), "..", "src", "solvio")
    tools_src = ""
    tools_dir = os.path.join(root, "tools")
    for fn in os.listdir(tools_dir):
        if fn.endswith(".py"):
            with open(os.path.join(tools_dir, fn), encoding="utf-8") as f:
                tools_src += f.read()
    for forbidden in ("revoke_device", "revoke_approval_key", "revoke_app_attest_key",
                      "add_revocation", "admin_cli", "mobile_approval"):
        assert forbidden not in tools_src, f"{forbidden} reachable from the LLM tool surface"

    with open(os.path.join(root, "security", "mobile_approval", "gateway.py"),
              encoding="utf-8") as f:
        gw = f.read()
    assert "revoke" not in gw, "gateway must expose no revoke route"

    # the admin module must not look like a registrable tool
    import solvio.security.mobile_approval.admin_cli as m
    for attr in ("schema", "expose_to_llm", "run"):
        assert not hasattr(m, attr), f"admin CLI exposes tool-like attribute {attr!r}"


async def t_boundary_revoked_device_cannot_be_reactivated_or_reauthorized():
    """Neither a revoked DEVICE nor a revoked KEY can be brought back by anything the model
    or a re-pairing flow could drive: new device_id, fresh pairing token, new record."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-boundary", transport_cred="tc")
        await cp.revoke_approval_key(_fp(ctx), reason="boundary")

        # every re-entry attempt with the same approval key is refused
        for did in ("dev-boundary", "dev-fresh-1", "dev-fresh-2"):
            token, _ = await cp.create_enrollment_token("local-owner")
            res, s = await cp.begin_enrollment(
                enrollment_token=token, device_id=did,
                approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
                app_attest_key_id=ctx.aakid, transport_cred="tc")
            assert res is None and s == "device_revoked", (did, s)

        # and the revoked record itself stays revoked
        assert (await st.get_device("dev-boundary"))["status"] == S.DEVICE_REVOKED
        assert await cp.verify_transport_cred("dev-boundary", "tc") is False
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_boundary_transport_alone_and_appattest_alone_are_not_authority():
    """Transport credential = read only. App Attest = app integrity, never user authority.
    Approval still requires the Face-ID/Secure-Enclave signature (PROOF A)."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        ap = await _request(cp)

        # transport cred is valid ... but grants no approval
        assert await cp.verify_transport_cred(ctx.device_id, "tc") is True
        wire, s = await cp.issue_challenge(approval_id=ap, device_id=ctx.device_id)
        assert s == "ok"
        assert (await st.get_request(ap))["state"] == S.PENDING, \
            "holding a transport credential must not approve anything"

        # a decision signed by a DIFFERENT key (i.e. no valid PROOF A) is refused even
        # though a correct App Attest assertion is attached
        evil = H.new_device(ctx.device_id)
        evil.key_id = ctx.key_id                      # claim the enrolled key id
        wire2 = H.sign_decision(evil, P.b64d(wire["payload_b64"]))
        r, ds = await cp.submit_decision(**wire2)
        assert r is None and ds in ("bad_signature", "key_mismatch"), ds
        assert (await st.get_request(ap))["state"] == S.PENDING
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
