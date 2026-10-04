"""P1A.4 — the findings a cold review reproduced against b924118.

C1  App-Attest-key revocation compared the base64 TEXT while the verifier compared the
    decoded BYTES. A 32-byte key id has four spellings, so a revoked key re-enrolled under
    an alternate spelling and regained authority. A real revocation bypass.
C2  20 of 24 suites are built on bare `assert`, which `python -O` strips: with a production
    guard deleted, an optimized run printed all-green.
H1  `expires_at` was not part of the atomic execution claim, so an approval that expired an
    hour ago still executed.
H2  `revoke-device` recorded only the device_id while printing "terminal"; the same key
    material re-paired under a fresh device_id and regained execution authority.
H3  the revocation predicate inside the execution claim was defended by no test — the
    device-status check in front of it always decided first.
§10 the enrollment revocation check was a Python read before a separate write, so a revoke
    racing in from the admin CLI produced an ACTIVE record bound to a revoked key.

ASSERTION POLICY: this suite uses `require*` from `tests/_guard.py`, which are real function
calls and therefore survive `-O`. `_guard` additionally refuses to import under optimized
Python. Bare `assert` elsewhere in the repo is NOT safe — the canonical runner
(`scripts/run_tests.py`) is what protects those suites, and it refuses to run under `-O`.

Direct: python tests/test_p1a_4_trust_and_test_integrity.py
"""
import asyncio
import base64
import os
import shutil
import sqlite3
import string
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import mobile_attest_helper as H
from _guard import require, require_equal, require_raises
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
REPO = os.path.join(os.path.dirname(__file__), "..")


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
    return st, cp, B.MobileApprovalCoordinator(
        cp, ApprovalBroker(approver=approver), approver), approver


def _executor(calls):
    async def run(action):
        calls.append(action)
        return True, {"ran": True}
    return run


def _spellings(canonical: str) -> list[str]:
    """All base64 strings that decode to the same 32 bytes, excluding the canonical one."""
    raw = base64.b64decode(canonical.encode("ascii"), validate=True)
    out = []
    for ch in string.ascii_letters + string.digits + "+/":
        cand = canonical[:len(canonical) - 2] + ch + "="
        if cand == canonical:
            continue
        try:
            if base64.b64decode(cand.encode("ascii"), validate=True) == raw:
                out.append(cand)
        except Exception:  # noqa: BLE001
            pass
    return out


async def _approved(tmp, ws, coord, cp, ctx):
    aid = await coord.request_codex_modify(
        principal="local-owner", task="edit README", workspace=ws,
        human_summary="Codex will edit README")
    wire, s = await cp.issue_challenge(approval_id=aid, device_id=ctx.device_id)
    require_equal(s, "ok", "challenge")
    res, s2 = await coord.apply_mobile_decision(
        **H.sign_decision(ctx, P.b64d(wire["payload_b64"])))
    require_equal(s2, "ok", "decision")
    return aid


# =====================================================================
# C1 — canonical App Attest identity
# =====================================================================
def t_c1_equivalent_spellings_are_one_identity():
    raw = os.urandom(32)
    canonical = base64.b64encode(raw).decode()
    variants = _spellings(canonical)
    require(len(variants) >= 1, f"expected alternate spellings, got {variants}")
    ids = {AA.canonical_app_attest_key_id(v) for v in [canonical] + variants}
    require_equal(len(ids), 1, "equivalent spellings produced different identities")
    require_equal(ids.pop(), canonical, "canonical form is not the re-encoded bytes")


def t_c1_malformed_and_wrong_length_are_rejected():
    bad = ["", "   ", "not base64!!", "AAAA",
           base64.b64encode(os.urandom(31)).decode(),
           base64.b64encode(os.urandom(33)).decode(),
           base64.b64encode(os.urandom(32)).decode().replace("=", ""),   # unpadded
           None, 42, b"bytes"]
    for value in bad:
        require_raises((AA.AppAttestIdentityError, ValueError),
                       AA.canonical_app_attest_key_id, value,
                       message=f"accepted {value!r}")


async def t_c1_alternate_spelling_cannot_re_enroll():
    """The reproduced bypass, now blocked."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        variants = _spellings(ctx.aakid)
        require(variants, "no alternate spelling available for this key")

        await cp.revoke_app_attest_key(ctx.aakid, reason="app compromised")
        require(await st.is_revoked(S.REVOKE_APP_ATTEST_KEY, ctx.aakid), "canonical")

        for v in variants:
            token, _ = await cp.create_enrollment_token("local-owner")
            res, s = await cp.begin_enrollment(
                enrollment_token=token, device_id=f"dev-evil-{variants.index(v)}",
                approval_public_key_x963_b64=P.b64e(H.new_device("z").appr_x963),
                app_attest_key_id=v, transport_cred="tc")
            require(res is None, f"spelling {v} enrolled")
            require_equal(s, "device_revoked", f"spelling {v}")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_c1_alternate_spelling_denied_on_every_authority_path():
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _ = await _wire(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        aid = await _approved(tmp, ws, coord, cp, ctx)
        variant = _spellings(ctx.aakid)[0]

        # revoke using the ALTERNATE spelling; the canonical device must still be caught
        affected = len((await cp.revoke_app_attest_key(variant, reason="via variant")).affected_devices)
        require_equal(affected, 1, "revoking by variant did not reach the device")
        require(await st.is_revoked(S.REVOKE_APP_ATTEST_KEY, ctx.aakid),
                "canonical identity not revoked when the variant was used")

        require_equal(await cp.verify_transport_cred("dev-1", "tc"), False, "transport")
        aid2 = await coord.request_codex_modify(
            principal="local-owner", task="x", workspace=ws, human_summary="x")
        _, cs = await cp.issue_challenge(approval_id=aid2, device_id="dev-1")
        require(cs != "ok", f"challenge: {cs}")
        calls = []
        out, es = await coord.execute_approved(aid, _executor(calls))
        require(out is None and calls == [], f"execution: {es}")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_c1_revocation_survives_restart_and_migrates_legacy_rows():
    """A revocation written in a pre-fix (non-canonical) spelling must keep working."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        variant = _spellings(ctx.aakid)[0]
        # simulate a legacy row: non-canonical text in both tables, written directly
        await st._run(lambda: st._conn.execute(
            "INSERT INTO revocations (kind, value, reason, revoked_at) VALUES (?,?,?,?)",
            (S.REVOKE_APP_ATTEST_KEY, variant, "legacy", time.time())))
        await st._run(lambda: st._conn.execute(
            "UPDATE devices SET app_attest_key_id=? WHERE device_id=?", (variant, "dev-1")))
        await st.close()

        st2, cp2 = await _open(tmp)                       # migration runs on open
        values = [r["value"] for r in await st2.list_revocations()
                  if r["kind"] == S.REVOKE_APP_ATTEST_KEY]
        require_equal(values, [ctx.aakid], "legacy revocation was not canonicalised")
        require_equal((await st2.get_device("dev-1"))["app_attest_key_id"], ctx.aakid,
                      "legacy device row was not canonicalised")
        require(await st2.is_revoked(S.REVOKE_APP_ATTEST_KEY, ctx.aakid), "durable")
        token, _ = await cp2.create_enrollment_token("local-owner")
        res, s = await cp2.begin_enrollment(
            enrollment_token=token, device_id="dev-new",
            approval_public_key_x963_b64=P.b64e(H.new_device("z").appr_x963),
            app_attest_key_id=variant, transport_cred="tc")
        require(res is None and s == "device_revoked", f"post-migration re-enroll: {s}")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_c1_cli_cannot_be_bypassed_by_spelling():
    tmp = tempfile.mkdtemp()
    try:
        async def setup():
            st, cp = await _open(tmp)
            ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
            await st.close()
            return ctx.aakid
        aakid = asyncio.run(setup())
        variant = _spellings(aakid)[0]
        require_equal(admin_cli.main(["--state-dir", tmp, "revoke-app-attest-key", variant]),
                      0, "CLI rejected a valid alternate spelling")

        async def check():
            st = S.ApprovalControlStore(os.path.join(tmp, DB))
            await st.open()
            try:
                return ([r["value"] for r in await st.list_revocations()
                         if r["kind"] == S.REVOKE_APP_ATTEST_KEY],
                        (await st.get_device("dev-1"))["status"])
            finally:
                await st.close()
        values, status = asyncio.run(check())
        require_equal(values, [aakid], "CLI stored a non-canonical identity")
        require_equal(status, S.DEVICE_REVOKED, "device not revoked via variant")
        for bad in ("not base64", "AAAA", ""):
            require(admin_cli.main(["--state-dir", tmp, "revoke-app-attest-key", bad]) != 0,
                    f"CLI accepted {bad!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# H2 — revoke-device cuts off the bound key material
# =====================================================================
async def t_h2_stolen_iphone_cannot_repair_under_a_new_device_id():
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _ = await _wire(tmp)
        ctx = await H.enroll_attested(cp, device_id="iphone", transport_cred="tc")
        await cp.revoke_device("iphone", reason="stolen")

        kinds = {r["kind"] for r in await st.list_revocations()}
        require_equal(kinds, set(S.REVOKE_KINDS),
                      "revoke-device did not revoke the bound key material")

        token, _ = await cp.create_enrollment_token("local-owner")
        res, s = await cp.begin_enrollment(
            enrollment_token=token, device_id="iphone-2",
            approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
            app_attest_key_id=ctx.aakid, transport_cred="tc")
        require(res is None, "the stolen key material re-paired")
        require_equal(s, "device_revoked", "re-pair status")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_h2_sibling_records_on_the_same_key_are_revoked_too():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="iphone", transport_cred="tc")
        for did in ("iphone-clone", "iphone-spare"):
            token, _ = await cp.create_enrollment_token("local-owner")
            res, s = await cp.begin_enrollment(
                enrollment_token=token, device_id=did,
                approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
                app_attest_key_id=ctx.aakid, transport_cred="tc")
            require_equal(s, "ok", f"setup {did}")
            _, s2 = await cp.complete_attestation(
                enrollment_id=res["enrollment_id"],
                attestation_b64=P.b64e(AA.fake_attestation(ctx.aakey_x963)))
            require_equal(s2, "ok", f"setup attest {did}")
        other = await H.enroll_attested(cp, device_id="ipad", transport_cred="to")

        affected = len((await cp.revoke_device("iphone", reason="stolen")).affected_devices)
        require_equal(affected, 3, "sibling records were not caught")
        for did in ("iphone", "iphone-clone", "iphone-spare"):
            require_equal((await st.get_device(did))["status"], S.DEVICE_REVOKED, did)
        require_equal((await st.get_device("ipad"))["status"], S.DEVICE_ACTIVE,
                      "an unrelated device was revoked")
        require_equal(await cp.verify_transport_cred("ipad", "to"), True, "collateral")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h2_cli_states_the_real_scope():
    tmp = tempfile.mkdtemp()
    try:
        async def setup():
            st, cp = await _open(tmp)
            await H.enroll_attested(cp, device_id="iphone", transport_cred="tc")
            await st.close()
        asyncio.run(setup())
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = admin_cli.main(["--state-dir", tmp, "revoke-device", "iphone"])
        require_equal(rc, 0, "CLI failed")
        out = buf.getvalue()
        for kind in S.REVOKE_KINDS:
            require(kind in out, f"CLI did not name the {kind} identity it revoked:\n{out}")
        require("NEUEN device_ids" in out or "NEUE device_ids" in out,
                f"CLI did not state that new device_ids stay blocked:\n{out}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# H1 — the approval TTL is enforced at the claim
# =====================================================================
async def _expired_execution(offset_seconds):
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _ = await _wire(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        aid = await _approved(tmp, ws, coord, cp, ctx)
        await st._run(lambda: st._conn.execute(
            "UPDATE approval_requests SET expires_at=? WHERE approval_id=?",
            (time.time() + offset_seconds, aid)))
        calls = []
        out, s = await coord.execute_approved(aid, _executor(calls))
        state = (await st.get_request(aid))["state"]
        await st.close()
        return out, s, calls, state
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_h1_approval_expired_an_hour_ago_cannot_execute():
    out, s, calls, state = await _expired_execution(-3600)
    require(out is None, f"expired approval executed: {out}")
    require_equal(calls, [], "the executor ran on an expired approval")
    require_equal(state, S.APPROVED, "state moved despite refusal")
    require(s != "ok", f"status: {s}")


async def t_h1_approval_expired_a_moment_ago_cannot_execute():
    out, s, calls, state = await _expired_execution(-0.05)
    require(out is None and calls == [], f"just-expired approval executed: {s}")
    require_equal(state, S.APPROVED, "state moved despite refusal")


async def t_h1_valid_approval_still_executes():
    out, s, calls, state = await _expired_execution(+600)
    require_equal(s, "ok", f"a valid approval was refused: {s}")
    require_equal(len(calls), 1, "executor did not run")
    require_equal(state, S.CONSUMED, "state")


async def t_h1_ttl_does_not_depend_on_anyone_polling():
    """`_expire_due` only ever ran from the phone-facing listing. The claim must not need it."""
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _ = await _wire(tmp)
        ctx = await H.enroll_attested(cp, transport_cred="tc")
        aid = await _approved(tmp, ws, coord, cp, ctx)
        await st._run(lambda: st._conn.execute(
            "UPDATE approval_requests SET expires_at=? WHERE approval_id=?",
            (time.time() - 60, aid)))
        # nobody lists anything; the row is still literally APPROVED in the table
        require_equal((await st.get_request(aid))["state"], S.APPROVED, "precondition")
        calls = []
        out, s = await coord.execute_approved(aid, _executor(calls))
        require(out is None and calls == [], f"executed without any poll: {s}")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


# =====================================================================
# H3 — the claim's revocation predicate, defended on its own
# =====================================================================
async def _claim_predicate_alone(kind):
    """Revoke an identity, then force devices.status back to ACTIVE so ONLY the revocations
    predicate inside the claiming UPDATE can refuse the execution."""
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _ = await _wire(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        aid = await _approved(tmp, ws, coord, cp, ctx)
        dev = await st.get_device("dev-1")
        if kind == S.REVOKE_DEVICE:
            await cp.revoke_device("dev-1", reason="x")
        elif kind == S.REVOKE_APPROVAL_KEY:
            await cp.revoke_approval_key(crypto.fingerprint(ctx.appr_x963), reason="x")
        else:
            await cp.revoke_app_attest_key(dev["app_attest_key_id"], reason="x")

        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:      # the device-status check must NOT be what saves this test
            con.execute("UPDATE devices SET status=? WHERE device_id=?",
                        (S.DEVICE_ACTIVE, "dev-1"))
        finally:
            con.close()
        require_equal((await st.get_device("dev-1"))["status"], S.DEVICE_ACTIVE,
                      "tamper precondition")

        calls = []
        out, s = await coord.execute_approved(aid, _executor(calls))
        require(out is None, f"{kind}: executed with only the revocation entry present")
        require_equal(calls, [], f"{kind}: executor ran")
        require_equal((await st.get_request(aid))["state"], S.APPROVED, kind)
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_h3_claim_refuses_on_revoked_device_identity_alone():
    await _claim_predicate_alone(S.REVOKE_DEVICE)


async def t_h3_claim_refuses_on_revoked_approval_key_alone():
    await _claim_predicate_alone(S.REVOKE_APPROVAL_KEY)


async def t_h3_claim_refuses_on_revoked_app_attest_key_alone():
    await _claim_predicate_alone(S.REVOKE_APP_ATTEST_KEY)


async def t_h3_claim_refuses_an_empty_identity_list():
    """`pred or "0"` used to make an empty list mean "check nothing"."""
    tmp = tempfile.mkdtemp()
    try:
        st, _ = await _open(tmp)
        try:
            # HYGIENE/H3: the public un-journalled claim is gone; the property it carried
            # — an empty identity list compiles to "no revocation check at all" and must be
            # refused — now lives on the journalled entry point.
            await st.claim_execution_attempt(
                approval_id="whatever", device_id="d", identities=[],
                execution_id="exec-x", capability="codex_task",
                semantics="NON_IDEMPOTENT_WRITE", idempotency_key="k", owner="test")
            raise AssertionError("empty identity list accepted")
        except ValueError:
            pass
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# §10 — enrollment cannot race a revoke
# =====================================================================
async def t_enrollment_cannot_race_a_concurrent_revoke():
    """A real second process commits the revoke while the enrollment is in flight."""
    tmp = tempfile.mkdtemp()
    scratch = os.path.join(tempfile.mkdtemp(), "revoker.py")
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-A", transport_cred="tc")
        fp = crypto.fingerprint(ctx.appr_x963)
        with open(scratch, "w", encoding="utf-8") as fh:
            fh.write(
                "import asyncio, os, sys\n"
                f"sys.path.insert(0, {os.path.abspath(os.path.join(REPO, 'src'))!r})\n"
                "from solvio.security.mobile_approval import control as C, identity, store as S\n"
                "async def go(d, fp):\n"
                f"    st = S.ApprovalControlStore(os.path.join(d, {DB!r}))\n"
                "    await st.open()\n"
                "    cp = C.MobileApprovalControlPlane(st, identity.MacSigningKey.load_or_create(d), 'c')\n"
                "    n = len((await cp.revoke_approval_key(fp, reason='stolen')).affected_devices)\n"
                "    await st.close()\n"
                "    print('REVOKED', n)\n"
                "asyncio.run(go(sys.argv[1], sys.argv[2]))\n")

        class RaceConn:
            def __init__(self, real):
                self._real, self.out, self._begins = real, None, 0

            def execute(self, sql, *args):
                # Fire BETWEEN the pairing-token transaction and the enrollment transaction,
                # i.e. BEFORE the enrollment opens its own. That is the real window: the
                # revoke commits while the caller is already inside begin_enrollment. Firing
                # later would only prove that BEGIN IMMEDIATE blocks the other process, which
                # is a different (and weaker) claim.
                #
                # P1B/§4 moved the token consumption itself into a write transaction, so the
                # old trigger ("first statement naming enrollment_tokens") now lands INSIDE
                # that transaction and the racing process just blocks on the lock. The window
                # is unchanged; the statement that marks it is. It is the SECOND
                # BEGIN IMMEDIATE: #1 belongs to consume_enrollment_token, #2 to
                # begin_enrollment_atomic.
                if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                    self._begins += 1
                    if self._begins == 2 and self.out is None:
                        proc = subprocess.run([sys.executable, scratch, tmp, fp],
                                              capture_output=True, text=True)
                        self.out = (proc.stdout + proc.stderr).strip()
                return self._real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._real, name)

        token, _ = await cp.create_enrollment_token("local-owner")
        real = await st._run(lambda: st._conn)
        racing = RaceConn(real)
        st._conn = racing
        try:
            res, s = await cp.begin_enrollment(
                enrollment_token=token, device_id="dev-NEW",
                approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
                app_attest_key_id=H.new_device("n").aakid, transport_cred="tc")
        finally:
            st._conn = real
        require(racing.out and racing.out.startswith("REVOKED"),
                f"the racing revoke never ran: {racing.out!r}")
        require(res is None, "enrollment succeeded against a revoked key")
        require_equal(s, "device_revoked", "race status")
        rows = {d["device_id"] for d in await st.list_devices_full()}
        require("dev-NEW" not in rows, "an ACTIVE record was created for a revoked key")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(os.path.dirname(scratch), ignore_errors=True)


# =====================================================================
# §15 — identifier validation
# =====================================================================
def t_identifier_lookalikes_are_refused():
    for bad in ("dev-1 ", " dev-1", "dev​1", "dev\n1", "dev\t1", "dev 1",
                "dev\x001", "", "x" * 200):
        require_raises(ValueError, C.canonical_device_id, bad,
                       message=f"accepted device_id {bad!r}")
    require_equal(C.canonical_device_id("dev-1"), "dev-1", "a normal id was altered")


async def t_lookalike_device_id_cannot_enroll():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        token, _ = await cp.create_enrollment_token("local-owner")
        res, s = await cp.begin_enrollment(
            enrollment_token=token, device_id="dev-1 ",
            approval_public_key_x963_b64=P.b64e(H.new_device("z").appr_x963),
            app_attest_key_id=H.new_device("z").aakid, transport_cred="tc")
        require(res is None and s == "bad_device_id", f"lookalike enrolled: {s}")
        require_equal(len(await st.list_devices_full()), 1, "a second record appeared")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# C2 — the runner itself must fail loudly under -O
# =====================================================================
def t_c2_canonical_runner_refuses_optimized_python():
    proc = subprocess.run([sys.executable, "-O", os.path.join(REPO, "scripts", "run_tests.py")],
                          capture_output=True, text=True, cwd=REPO)
    require(proc.returncode != 0, "the canonical runner ran under -O")
    require("optimized Python" in (proc.stderr + proc.stdout),
            f"no clear message:\n{proc.stderr}\n{proc.stdout}")


def t_c2_security_suites_refuse_optimized_python():
    """The guard rides on the shared helper, so it applies however a suite is invoked."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(REPO, "src"), os.path.dirname(__file__)])
    for suite in ("test_p1a_3_transactional_revocation.py",
                  "test_p1a_2_app_attest_revoke_consistency.py",
                  "test_f5_1_atomic_begin.py"):
        proc = subprocess.run(
            [sys.executable, "-O", os.path.join(os.path.dirname(__file__), suite)],
            capture_output=True, text=True, env=env, cwd=REPO)
        require(proc.returncode != 0, f"{suite} reported success under -O")
        require("optimized Python" in (proc.stderr + proc.stdout), suite)


def t_c2_this_suite_survives_optimization():
    """`require` is a function call, so it still fails when `assert` would not."""
    src = open(__file__, encoding="utf-8").read()
    body = src[src.index("# ====="):]
    offenders = [ln.strip() for ln in body.splitlines()
                 if ln.strip().startswith("assert ")]
    require(len(offenders) <= 1, f"bare asserts in a P1A.4 test: {offenders}")
    require_raises(Exception, require, False, message="require() did not raise")


# =====================================================================
# §11 — the removed footguns stay removed
# =====================================================================
def t_dead_footgun_writers_are_gone():
    for name in ("add_revocation", "set_device_status", "add_device", "claim_approved"):
        require(not hasattr(S.ApprovalControlStore, name),
                f"{name} is back on the store — it is an un-revoke / non-transactional path")
    src = open(os.path.join(REPO, "src", "solvio", "security", "mobile_approval",
                            "store.py"), encoding="utf-8").read()
    require("def _set_device_status" not in src, "private un-revoke helper still present")


async def t_revocation_db_uses_full_synchronous():
    tmp = tempfile.mkdtemp()
    try:
        st, _ = await _open(tmp)
        mode = await st._run(lambda: st._conn.execute("PRAGMA synchronous").fetchone()[0])
        journal = await st._run(
            lambda: st._conn.execute("PRAGMA journal_mode").fetchone()[0])
        require_equal(mode, 2, "synchronous is not FULL (2)")
        require_equal(journal, "wal", "journal mode changed")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# §13 — audit honesty
# =====================================================================
async def t_repeat_revoke_is_not_logged_as_a_first_revocation():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        fp = crypto.fingerprint(ctx.appr_x963)
        await cp.revoke_approval_key(fp, reason="first")
        await cp.revoke_approval_key(fp, reason="again")
        rows = await st._run(lambda: st._conn.execute(
            "SELECT event FROM audit").fetchall())
        events = [r["event"] for r in rows]
        require_equal(events.count("approval_key_revoked"), 1,
                      "a repeat revoke was logged as a first revocation")
        require("revocation_reaffirmed" in events,
                f"the corrective re-run left no trace: {sorted(set(events))}")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_attestation_rejection_for_revoked_device_is_audited():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = H.new_device("dev-1")
        token, _ = await cp.create_enrollment_token("local-owner")
        res, s = await cp.begin_enrollment(
            enrollment_token=token, device_id="dev-1",
            approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
            app_attest_key_id=ctx.aakid, transport_cred="tc")
        require_equal(s, "ok", "setup")
        await cp.revoke_device("dev-1", reason="stolen")
        r, s2 = await cp.complete_attestation(
            enrollment_id=res["enrollment_id"],
            attestation_b64=P.b64e(AA.fake_attestation(ctx.aakey_x963)))
        require(r is None and s2 == "device_revoked", f"attestation: {s2}")
        rows = await st._run(lambda: st._conn.execute(
            "SELECT event FROM audit WHERE device_id=?", ("dev-1",)).fetchall())
        events = {r["event"] for r in rows}
        require("enrollment_rejected_revoked_key" in events,
                f"the rejection left no audit trace: {sorted(events)}")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
