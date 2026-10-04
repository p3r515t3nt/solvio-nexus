"""P1A.7 — attestation binding, upgrade revocation, and assurance isolation.

Four blockers a cold review reproduced against fc6afa4:

H1  `complete_attestation` applied a verified attestation to whatever the devices row held
    at that moment. A second `begin_enrollment` overwrote it, so an older challenge marked a
    binding ATTESTED that its attestation had never proved — leaving `app_attest_key_id` and
    `app_attest_public_key` describing DIFFERENT keys, so revoking the key that actually
    signs every assertion matched nothing.
H2  core main (3233e99) had no `revocations` table; `revoke_device` was
    `set_device_status(REVOKED)`. After the upgrade that legacy row still blocked its own
    device_id, but the key material re-enrolled under a fresh device_id with full authority.
H3  the device update and the identity-history insert were two autocommits, so a failure
    between them left a trusted device with no history row — and a later rotation then
    orphaned that binding.
H4  the runner parsed a result manifest out of the suite's own stdout/stderr and took the
    LAST one, so a suite could print a forged manifest and turn a failing test green.

ASSERTION POLICY: `require*` from `tests/_guard.py` are function calls and survive `-O`.

Direct: python tests/test_p1a_7_attestation_upgrade_assurance.py
"""
import asyncio
import contextlib
import importlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import _harness  # noqa: E402
import _inventory  # noqa: E402
import mobile_attest_helper as H  # noqa: E402
from _guard import require, require_equal  # noqa: E402
from solvio.security.mobile_approval import admin_cli  # noqa: E402
from solvio.security.mobile_approval import app_attest as AA  # noqa: E402
from solvio.security.mobile_approval import control as C  # noqa: E402
from solvio.security.mobile_approval import crypto  # noqa: E402
from solvio.security.mobile_approval import identity  # noqa: E402
from solvio.security.mobile_approval import protocol as P  # noqa: E402
from solvio.security.mobile_approval import store as S  # noqa: E402

APP_ID = "WQ8CG7R53R.de.solvio.approvals"
DB = "approval_control.sqlite3"
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


async def _open(tmp, **kw):
    st = S.ApprovalControlStore(os.path.join(tmp, DB), **kw)
    await st.open()
    cp = C.MobileApprovalControlPlane(
        st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
        attest_verifier=H.fake_verifier(), app_id=APP_ID)
    return st, cp


async def _begin(cp, ctx, device_id, cred="tc"):
    token, _ = await cp.create_enrollment_token("local-owner")
    return await cp.begin_enrollment(
        enrollment_token=token, device_id=device_id,
        approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
        app_attest_key_id=ctx.aakid, transport_cred=cred)


async def _enroll(cp, ctx, device_id, cred="tc"):
    res, s = await _begin(cp, ctx, device_id, cred)
    if s != "ok":
        return s
    _, s2 = await cp.complete_attestation(
        enrollment_id=res["enrollment_id"],
        attestation_b64=P.b64e(AA.fake_attestation(ctx.aakey_x963)))
    return s2


# =====================================================================
# H1 — the challenge is authoritative for attestation
# =====================================================================
async def t_h1_old_challenge_cannot_attest_a_new_binding():
    """A: the reproduced attack. K1's challenge must not attest a row now holding K2."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k1, k2 = H.new_device("dev-D"), H.new_device("dev-D")
        r1, s1 = await _begin(cp, k1, "dev-D")
        require_equal(s1, "ok", "first begin")
        _, s2 = await _begin(cp, k2, "dev-D")
        require_equal(s2, "ok", "second begin")

        res, s3 = await cp.complete_attestation(
            enrollment_id=r1["enrollment_id"],
            attestation_b64=P.b64e(AA.fake_attestation(k1.aakey_x963)))
        require(res is None, f"the stale challenge attested a new binding: {s3}")
        require_equal(s3, "challenge_superseded", f"status: {s3}")
        dev = await st.get_device("dev-D")
        require_equal(dev["attestation_status"], S.ATT_PENDING,
                      "the device became attested through a superseded challenge")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h1_attestation_for_a1_can_never_mark_a2_attested():
    """B: even reaching the store directly, the challenge binding refuses."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k1, k2 = H.new_device("dev-D"), H.new_device("dev-D")
        r1, _ = await _begin(cp, k1, "dev-D")
        await _begin(cp, k2, "dev-D")
        out = await st.finalize_attestation(
            enrollment_id=r1["enrollment_id"],
            app_attest_public_key_x963=k1.aakey_x963,
            app_attest_counter=0, environment="development", app_id=APP_ID,
            core_instance_id=cp.core_instance_id)
        require(out != "ok", f"A1 attested a row bound to A2: {out}")
        dev = await st.get_device("dev-D")
        require_equal(dev["attestation_status"], S.ATT_PENDING, "device became attested")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h1_stored_identity_matches_the_verified_public_key():
    """C: app_attest_key_id must be the identity of the key that was actually verified."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k = H.new_device("dev-1")
        require_equal(await _enroll(cp, k, "dev-1"), "ok", "enrollment")
        dev = await st.get_device("dev-1")
        derived = AA.app_attest_identity_from_public_key(
            bytes.fromhex(dev["app_attest_public_key"]))
        require_equal(dev["app_attest_key_id"], derived,
                      "stored identity does not match the stored verified public key")
        require_equal(dev["app_attest_key_id"], k.aakid, "identity drifted from the key")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h1_derived_identity_mismatch_fails_closed():
    """The derivation must be enforced, not merely computed."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k, other = H.new_device("dev-1"), H.new_device("other")
        r, _ = await _begin(cp, k, "dev-1")
        # a verified key whose identity is NOT the challenge-bound one
        out = await st.finalize_attestation(
            enrollment_id=r["enrollment_id"],
            app_attest_public_key_x963=other.aakey_x963,
            app_attest_counter=0, environment="development", app_id=APP_ID,
            core_instance_id=cp.core_instance_id)
        require_equal(out, "app_attest_key_mismatch", f"mismatch accepted: {out}")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h1_verifier_returning_a_foreign_key_is_refused():
    """The derived-identity check, exercised through the REAL control path.

    Since P1A.8 the store derives this identity itself, but the control path must refuse
    too: `complete_attestation` derives before it commits anything, so a verifier whose
    returned public key does not match the challenge is rejected without a write. Without
    this the control-level check is not defended at all
    (found by mutation).
    """
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k, foreign = H.new_device("dev-1"), H.new_device("foreign")
        base = cp.attest_verifier

        class ForeignKeyVerifier:
            allowed_environments = getattr(base, "allowed_environments", None)
            is_fake = True

            def verify_attestation(self, **kw):
                att = base.verify_attestation(**kw)
                return att.__class__(**{**att.__dict__,
                                        "public_key_x963": foreign.aakey_x963})

        cp.attest_verifier = ForeignKeyVerifier()
        r, _ = await _begin(cp, k, "dev-1")
        res, s = await cp.complete_attestation(
            enrollment_id=r["enrollment_id"],
            attestation_b64=P.b64e(AA.fake_attestation(k.aakey_x963)))
        require(res is None, f"a foreign verified key was accepted: {s}")
        require_equal(s, "app_attest_key_mismatch", f"status: {s}")
        dev = await st.get_device("dev-1")
        require(dev["attestation_status"] != S.ATT_ATTESTED,
                "the device became attested on a key the challenge never named")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h1_correct_challenge_still_succeeds():
    """E: the fix must not break the working path."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k = H.new_device("dev-1")
        require_equal(await _enroll(cp, k, "dev-1"), "ok", "enrollment")
        dev = await st.get_device("dev-1")
        require_equal(dev["attestation_status"], S.ATT_ATTESTED, "not attested")
        require_equal(await cp.verify_transport_cred("dev-1", "tc"), True, "transport")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h1_revoking_the_attesting_key_reaches_the_device():
    """F: the key that actually attested must be revocable."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k = H.new_device("dev-1")
        await _enroll(cp, k, "dev-1")
        res = await cp.revoke_app_attest_key(k.aakid, reason="compromised app")
        require_equal(len(res.affected_devices), 1, f"revoke reached nothing: {res}")
        require_equal((await st.get_device("dev-1"))["status"], S.DEVICE_REVOKED, "status")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h1_superseded_state_survives_restart():
    """G."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k1, k2 = H.new_device("dev-D"), H.new_device("dev-D")
        r1, _ = await _begin(cp, k1, "dev-D")
        await _begin(cp, k2, "dev-D")
        await st.close()

        st2, cp2 = await _open(tmp)
        res, s = await cp2.complete_attestation(
            enrollment_id=r1["enrollment_id"],
            attestation_b64=P.b64e(AA.fake_attestation(k1.aakey_x963)))
        require(res is None and s == "challenge_superseded", f"after restart: {s}")
        rows = await st2._run(lambda: st2._conn.execute(
            "SELECT superseded FROM attestation_challenges WHERE enrollment_id=?",
            (r1["enrollment_id"],)).fetchone())
        require_equal(rows["superseded"], 1, "superseded flag not durable")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# H3 — attestation and history commit together
# =====================================================================
class _Boom(RuntimeError):
    pass


class _FailAt:
    """Fails at a chosen statement inside the live transaction."""

    def __init__(self, real, needle):
        self._real, self._needle, self.begun = real, needle.upper(), False
        self.fired = False

    def execute(self, sql, *args):
        head = sql.strip().upper()
        if head.startswith("BEGIN"):
            self.begun = True
        if self.begun and self._needle in head:
            self.fired = True
            raise _Boom(f"injected at {self._needle}")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def _attest_with_injection(needle):
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k = H.new_device("dev-1")
        r, _ = await _begin(cp, k, "dev-1")
        real = await st._run(lambda: st._conn)
        fc = _FailAt(real, needle)
        st._conn = fc
        raised = False
        try:
            await cp.complete_attestation(
                enrollment_id=r["enrollment_id"],
                attestation_b64=P.b64e(AA.fake_attestation(k.aakey_x963)))
        except _Boom:
            raised = True
        finally:
            st._conn = real
        require(fc.begun, f"{needle}: the transaction never opened")
        require(fc.fired and raised, f"{needle}: the injected failure did not propagate")

        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            dev = con.execute("SELECT * FROM devices WHERE device_id='dev-1'").fetchone()
            hist = con.execute(
                "SELECT COUNT(*) c FROM device_identity_history").fetchone()["c"]
            ch = con.execute(
                "SELECT consumed FROM attestation_challenges").fetchone()["consumed"]
        finally:
            con.close()
        require_equal(dev["attestation_status"], S.ATT_PENDING,
                      f"{needle}: device committed as attested despite rollback")
        require_equal(hist, 0, f"{needle}: a history row survived the rollback")
        require_equal(ch, 0, f"{needle}: the challenge was consumed despite rollback")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h3_failure_after_device_update_rolls_back():
    await _attest_with_injection("INSERT INTO DEVICE_IDENTITY_HISTORY")


async def t_h3_failure_at_challenge_finalisation_rolls_back():
    await _attest_with_injection("UPDATE ATTESTATION_CHALLENGES")


async def t_h3_failure_at_audit_rolls_back():
    await _attest_with_injection("INSERT INTO AUDIT")


async def t_h3_success_commits_device_history_and_challenge_together():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k = H.new_device("dev-1")
        require_equal(await _enroll(cp, k, "dev-1"), "ok", "enrollment")
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            dev = con.execute("SELECT * FROM devices WHERE device_id='dev-1'").fetchone()
            hist = con.execute(
                "SELECT * FROM device_identity_history WHERE device_id='dev-1'").fetchall()
            ch = con.execute(
                "SELECT consumed FROM attestation_challenges").fetchone()["consumed"]
        finally:
            con.close()
        require_equal(dev["attestation_status"], S.ATT_ATTESTED, "device")
        require_equal(len(hist), 1, f"history rows: {len(hist)}")
        require_equal(hist[0]["app_attest_key_id"], k.aakid, "history identity")
        require_equal(ch, 1, "challenge not finalised")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h3_no_attested_device_without_history():
    """The invariant, stated directly and checked over the whole store."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        for i in range(3):
            await _enroll(cp, H.new_device(f"dev-{i}"), f"dev-{i}")
        rows = await st._run(lambda: st._conn.execute(
            "SELECT d.device_id FROM devices d WHERE d.attestation_status=? "
            "AND NOT EXISTS (SELECT 1 FROM device_identity_history h "
            "                WHERE h.device_id=d.device_id)", (S.ATT_ATTESTED,)).fetchall())
        require_equal([r["device_id"] for r in rows], [],
                      "an ATTESTED device exists with no history row")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# H2 — legacy revocations survive the upgrade
# =====================================================================
async def _legacy_store(tmp):
    """Build a store, then force it into the exact pre-P1A shape: devices.status=REVOKED
    with no revocations row, no history and no schema version."""
    st, cp = await _open(tmp)
    k = H.new_device("dev-legacy")
    require_equal(await _enroll(cp, k, "dev-legacy"), "ok", "setup enrollment")
    for sql in ("UPDATE devices SET status='REVOKED' WHERE device_id='dev-legacy'",
                "DELETE FROM revocations", "DELETE FROM device_identity_history",
                "DELETE FROM schema_meta"):
        await st._run(lambda sql=sql: st._conn.execute(sql))
    await st.close()
    return k


async def t_h2_legacy_revoked_device_is_backfilled_on_upgrade():
    tmp = tempfile.mkdtemp()
    try:
        k = await _legacy_store(tmp)
        st, cp = await _open(tmp)                      # upgrade open runs the migration
        kinds = {r["kind"] for r in await st.list_revocations()}
        require_equal(kinds, set(S.REVOKE_KINDS), f"backfilled kinds: {kinds}")
        require_equal(await st.is_revoked(
            S.REVOKE_APPROVAL_KEY, crypto.fingerprint(k.appr_x963)), True, "approval key")
        require_equal(await st.is_revoked(S.REVOKE_APP_ATTEST_KEY, k.aakid), True, "attest")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h2_legacy_key_material_cannot_re_enroll_under_a_new_device_id():
    """D: the reproduced upgrade hole."""
    tmp = tempfile.mkdtemp()
    try:
        k = await _legacy_store(tmp)
        st, cp = await _open(tmp)
        require_equal(await _enroll(cp, k, "dev-new"), "device_revoked",
                      "legacy key material re-enrolled after upgrade")
        require_equal(await _enroll(cp, k, "dev-legacy"), "device_revoked", "same id")
        await st.close()

        st2, cp2 = await _open(tmp)                    # restart
        require_equal(await _enroll(cp2, k, "dev-new2"), "device_revoked", "after restart")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h2_migration_is_idempotent_and_versioned():
    tmp = tempfile.mkdtemp()
    try:
        await _legacy_store(tmp)
        st, _ = await _open(tmp)
        first = {(r["kind"], r["value"]) for r in await st.list_revocations()}
        ver = await st._run(lambda: st._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone())
        require_equal(int(ver["value"]), S.ApprovalControlStore.SCHEMA_VERSION, "version")
        before = await st._run(lambda: st._conn.execute(
            "SELECT COUNT(*) c FROM audit WHERE event='schema_migrated'").fetchone()["c"])
        await st.close()
        st2, _ = await _open(tmp)
        second = {(r["kind"], r["value"]) for r in await st2.list_revocations()}
        after = await st2._run(lambda: st2._conn.execute(
            "SELECT COUNT(*) c FROM audit WHERE event='schema_migrated'").fetchone()["c"])
        require_equal(second, first, "a second open changed the revocation set")
        # Idempotence is "a further open adds nothing", not an absolute event count: this
        # store legitimately migrated twice (initial creation, then the simulated downgrade).
        require_equal(after, before, f"the migration ran again: {before} -> {after}")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h2_legacy_row_without_key_material_still_revokes_the_device():
    """Honest limitation: a row that lost its key material contributes only its device
    identity — nothing historical is invented."""
    tmp = tempfile.mkdtemp()
    try:
        await _legacy_store(tmp)
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:
            con.execute("UPDATE devices SET app_attest_key_id=NULL WHERE device_id=?",
                        ("dev-legacy",))
        finally:
            con.close()
        st, _ = await _open(tmp)
        kinds = {r["kind"] for r in await st.list_revocations()}
        require(S.REVOKE_DEVICE in kinds, "the device identity was not revoked")
        require(S.REVOKE_APP_ATTEST_KEY not in kinds,
                "an app-attest identity was invented for a row that no longer carries one")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# H4 — the runner owns the result channel
# =====================================================================
def _runner():
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    return importlib.import_module("run_tests")


def _probe_suite(tmp, body):
    path = os.path.join(tmp, "test_probe_channel.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"import os, sys\nsys.path.insert(0, {TESTS_DIR!r})\n"
                 "from _guard import enforce_assertions\nenforce_assertions()\n" + body)
    return path


def t_h4_forged_stdout_manifest_cannot_turn_a_failure_green():
    """The reproduced spoof: a real failure plus a later all-green manifest on stdout."""
    from pathlib import Path
    runner = _runner()
    tmp = tempfile.mkdtemp()
    try:
        suite = _probe_suite(tmp, (
            "def t_real_failure():\n    raise AssertionError('genuinely broken')\n"
            'if __name__ == "__main__":\n'
            "    from _harness import run_module, MANIFEST_PREFIX\n"
            "    import json\n"
            "    run_module(globals(), __name__)\n"
            "    fake = {'protocol': 1, 'tests': [{'id': 't_real_failure', 'started': True,\n"
            "        'completed': True, 'status': 'passed', 'detail': ''}]}\n"
            "    print(MANIFEST_PREFIX + json.dumps(fake))\n"
            "    sys.stderr.write(MANIFEST_PREFIX + json.dumps(fake) + '\\n')\n"
            "    raise SystemExit(0)\n"))
        res = runner.run_one(Path(suite), _inventory.suite_expected(suite))
        require(res.failed >= 1, f"a forged manifest turned a failure green: {res.failures}")
        require_equal(res.passed, 0, f"the forged result was believed: passed={res.passed}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h4_missing_result_channel_fails():
    from pathlib import Path
    runner = _runner()
    tmp = tempfile.mkdtemp()
    try:
        suite = _probe_suite(tmp, (
            "def t_a():\n    pass\n"
            'if __name__ == "__main__":\n'
            "    print('=== 1/1 bestanden ===')\n    raise SystemExit(0)\n"))
        res = runner.run_one(Path(suite), _inventory.suite_expected(suite))
        require(res.failed >= 1, "a suite with no result channel was accepted")
        # P1A.8/H1: the WORKER always frames a message, so the runner learns the suite
        # produced no harness results rather than merely finding nothing.
        require(any("no harness results" in f or "channel" in f for f in res.failures),
                res.failures)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h4_malformed_result_channel_fails():
    from pathlib import Path
    runner = _runner()
    tmp = tempfile.mkdtemp()
    try:
        # P1A.8/H1: there is no result PATH to corrupt any more. The nearest a suite can
        # get is the write end it inherits from the worker (its number is in the worker's
        # argv) — so write junk there and require the framing to refuse it.
        suite = _probe_suite(tmp, (
            "def t_a():\n    pass\n"
            'if __name__ == "__main__":\n'
            "    os.write(int(sys.argv[2]), b'{not json')\n"
            "    raise SystemExit(0)\n"))
        res = runner.run_one(Path(suite), _inventory.suite_expected(suite))
        require(res.failed >= 1, "a malformed result channel was accepted")
        require(res.protocol_error, "protocol error not flagged")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h4_wrong_protocol_version_fails():
    from pathlib import Path
    runner = _runner()
    tmp = tempfile.mkdtemp()
    try:
        # A forged frame with an unknown protocol version, written to the inherited fd.
        # The worker's own frame follows it, so this is ALSO the duplicate-message case.
        suite = _probe_suite(tmp, (
            "import json\n"
            "def t_a():\n    pass\n"
            'if __name__ == "__main__":\n'
            "    b = json.dumps({'protocol': 99, 'tests': []}).encode()\n"
            "    os.write(int(sys.argv[2]),\n"
            "             b'SOLVIO-RESULT-1 ' + str(len(b)).encode() + b'\\n' + b)\n"
            "    raise SystemExit(0)\n"))
        res = runner.run_one(Path(suite), _inventory.suite_expected(suite))
        require(res.failed >= 1, "an unknown protocol version was accepted")
        require(res.protocol_error, "a second result message was not refused")
        # and the contract itself refuses an unknown version outright
        probe = runner.Result(Path(suite))
        probe.channel = {"protocol": 99, "tests": [], "ran": True}
        runner._enforce_contract(probe, ["t_a"])
        require(probe.failed >= 1, "protocol 99 was accepted by the contract")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h4_worker_crash_fails():
    from pathlib import Path
    runner = _runner()
    tmp = tempfile.mkdtemp()
    try:
        suite = _probe_suite(tmp, (
            "def t_a():\n    pass\n"
            'if __name__ == "__main__":\n'
            "    os._exit(3)\n"))
        res = runner.run_one(Path(suite), _inventory.suite_expected(suite))
        require(res.failed >= 1, "a crashed worker was accepted")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h4_hung_suite_times_out():
    from pathlib import Path
    runner = _runner()
    tmp = tempfile.mkdtemp()
    try:
        suite = _probe_suite(tmp, (
            "import time\n"
            "def t_a():\n    pass\n"
            'if __name__ == "__main__":\n'
            "    time.sleep(120)\n"))
        res = runner.run_one(Path(suite), _inventory.suite_expected(suite), timeout=3)
        require(res.timed_out, "a hanging suite did not time out")
        require(res.failed >= 1, "a timed-out suite was not a failure")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# §13 — unittest semantics
# =====================================================================
def _harness_result(namespace, module_name, unittest_mode=False):
    """P1A.8/H1: the harness no longer writes anywhere — it hands the results back."""
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        rc = (_harness.run_unittest if unittest_mode else _harness.run_module)(
            namespace, module_name)
    return rc, {"protocol": 2, "tests": _harness.last_results(), "ran": True}


def t_unittest_failing_subtest_fails_its_parent():
    class TestSub(unittest.TestCase):
        def test_subtests_that_fail(self):
            for i in (1, 2):
                with self.subTest(i=i):
                    self.assertEqual(i, 0)

        def test_ok(self):
            pass

    TestSub.__module__ = "probe_ut"
    rc, data = _harness_result({"TestSub": TestSub}, "probe_ut", unittest_mode=True)
    ids = {e["id"]: e for e in data["tests"]}
    require_equal(ids["TestSub.test_subtests_that_fail"]["status"], "failed",
                  f"a failing subTest was laundered into a pass: {ids}")
    require_equal(ids["TestSub.test_ok"]["status"], "passed", ids)
    require(rc != 0, "the suite exited 0 despite a failing subTest")


def t_unittest_unexpected_success_is_a_failure():
    class TestUX(unittest.TestCase):
        @unittest.expectedFailure
        def test_passes_unexpectedly(self):
            pass

    TestUX.__module__ = "probe_ux"
    rc, data = _harness_result({"TestUX": TestUX}, "probe_ux", unittest_mode=True)
    ids = {e["id"]: e for e in data["tests"]}
    require_equal(ids["TestUX.test_passes_unexpectedly"]["status"], "failed",
                  f"an unexpected success passed: {ids}")
    require(rc != 0, "the suite exited 0 despite an unexpected success")


def t_unittest_expected_failure_and_skip_still_work():
    class TestNormal(unittest.TestCase):
        @unittest.expectedFailure
        def test_expected_failure(self):
            raise AssertionError("as expected")

        @unittest.skip("deliberate")
        def test_skipped(self):
            pass

    TestNormal.__module__ = "probe_norm"
    rc, data = _harness_result({"TestNormal": TestNormal}, "probe_norm", unittest_mode=True)
    ids = {e["id"]: e["status"] for e in data["tests"]}
    require_equal(ids["TestNormal.test_expected_failure"], "passed", ids)
    require_equal(ids["TestNormal.test_skipped"], "skipped", ids)
    require_equal(rc, 0, "a normal expected-failure/skip suite failed")


# =====================================================================
# §16 — read-only admin never mutates
# =====================================================================
def t_readonly_admin_does_not_mutate_the_database():
    tmp = tempfile.mkdtemp()
    try:
        asyncio.run(_setup_store(tmp))
        db = os.path.join(tmp, DB)
        import hashlib
        before = hashlib.sha256(open(db, "rb").read()).hexdigest()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = admin_cli.main(["--state-dir", tmp, "devices"])
        require_equal(rc, 0, buf.getvalue())
        after = hashlib.sha256(open(db, "rb").read()).hexdigest()
        require_equal(after, before, "a read-only listing changed the database")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _setup_store(tmp):
    st, cp = await _open(tmp)
    await H.enroll_attested(cp, device_id="iphone", transport_cred="tc")
    await st.close()


def t_readonly_admin_refuses_when_a_migration_is_pending():
    """A read-only path reports pending work; it never applies it."""
    tmp = tempfile.mkdtemp()
    try:
        asyncio.run(_setup_store(tmp))
        db = os.path.join(tmp, DB)
        con = sqlite3.connect(db, isolation_level=None)
        try:
            con.execute("DELETE FROM schema_meta")
        finally:
            con.close()
        import hashlib
        before = hashlib.sha256(open(db, "rb").read()).hexdigest()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = admin_cli.main(["--state-dir", tmp, "devices"])
        require(rc != 0, "a pending migration was silently applied")
        require("migration" in (out.getvalue() + err.getvalue()).lower(),
                out.getvalue() + err.getvalue())
        after = hashlib.sha256(open(db, "rb").read()).hexdigest()
        require_equal(after, before, "the read-only path migrated the database")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# §18 — the dead single-kind writer is gone
# =====================================================================
def t_dead_single_kind_revoke_writer_is_removed():
    require(not hasattr(S.ApprovalControlStore, "revoke_identity"),
            "revoke_identity is back — it mislabelled the result's operation")
    src = open(os.path.join(REPO, "src", "solvio", "security", "mobile_approval",
                            "store.py"), encoding="utf-8").read()
    require("def _revoke_identity(" not in src, "the private helper survived")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
