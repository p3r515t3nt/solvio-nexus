"""P1A.6 — deterministic test assurance, admin identity safety, and rotation-aware revoke.

A cold review against ebc8702 reproduced four things this suite pins:

H1  an `async` test in a suite whose `__main__` called tests synchronously produced an
    un-awaited coroutine, raised nothing, and was reported PASS — in `test_approval.py`,
    the suite guarding the S1 broker's digest binding.
H2  editing only a suite's enumerator line dropped ten tests, including every App-Attest
    canonicalisation and execution-revocation-predicate test, while the runner reported
    FAILED=0. A count check alone cannot tell removal from renaming.
M1  a READ-ONLY admin command (`devices`) minted a new signing key and a new
    core_instance_id when those files were absent — silently de-authorising every enrolled
    device (they then fail `wrong_core_instance`) while the listing still showed them
    ACTIVE/ATTESTED.
M2  a legitimate key rotation under one device_id orphaned the previous approval key:
    `revoke-device` revoked only the current binding, and the old key stayed enrollable.

The assurance contract is expected-vs-executed by IDENTITY: `tests/_inventory.py` derives
the expected set from the tracked source (AST), `tests/_harness.py` reports what actually
ran, and `scripts/run_tests.py` requires the two sets to match exactly.

ASSERTION POLICY: `require*` from `tests/_guard.py` are function calls and survive `-O`.

Direct: python tests/test_p1a_6_assurance_and_identity.py
"""
import asyncio
import contextlib
import importlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import _harness  # noqa: E402
import _inventory  # noqa: E402
import mobile_attest_helper as H  # noqa: E402
from _guard import require, require_equal, require_raises  # noqa: E402
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


async def _open(tmp):
    st = S.ApprovalControlStore(os.path.join(tmp, DB))
    await st.open()
    cp = C.MobileApprovalControlPlane(
        st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
        attest_verifier=H.fake_verifier(), app_id=APP_ID)
    return st, cp


async def _enroll(cp, ctx, device_id, cred="tc"):
    token, _ = await cp.create_enrollment_token("local-owner")
    res, s = await cp.begin_enrollment(
        enrollment_token=token, device_id=device_id,
        approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
        app_attest_key_id=ctx.aakid, transport_cred=cred)
    if s != "ok":
        return s
    _, s2 = await cp.complete_attestation(
        enrollment_id=res["enrollment_id"],
        attestation_b64=P.b64e(AA.fake_attestation(ctx.aakey_x963)))
    return s2


def _snapshot(path):
    out = {}
    for root, _d, files in os.walk(path):
        for f in files:
            p = os.path.join(root, f)
            try:
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, path)] = len(fh.read())
            except OSError:
                pass
    return out


# =====================================================================
# §1-§3 — the expected-vs-executed contract
# =====================================================================
def t_inventory_finds_every_test_form_present_in_this_repo():
    """The SOLL side must not miss a form. `tests/memory/` derives its cases from a local
    base class, which a naive `TestCase`-in-bases check misses — 101 real tests."""
    inv = _inventory.discover(TESTS_DIR)
    require(len(inv) >= 25, f"suite discovery looks wrong: {len(inv)}")
    memory = {p: v for p, v in inv.items() if os.sep + "memory" + os.sep in p}
    require(memory, "tests/memory/ not discovered")
    require(all(v for v in memory.values()),
            f"a memory suite reported zero expected tests: "
            f"{[os.path.basename(p) for p, v in memory.items() if not v]}")
    require(any("." in i for v in inv.values() for i in v),
            "no Class.method identity found — unittest suites are being missed")
    require(any(i.startswith("t_") for v in inv.values() for i in v),
            "no t_* identity found — security suites are being missed")
    empty = [os.path.basename(p) for p, v in inv.items() if not v]
    require_equal(empty, [], f"suites with no expected tests: {empty}")


def t_harness_awaits_coroutine_tests():
    ran = {}

    async def t_async_probe():
        ran["async"] = True

    def t_sync_probe():
        ran["sync"] = True

    ns = {"t_async_probe": t_async_probe, "t_sync_probe": t_sync_probe}
    for fn in (t_async_probe, t_sync_probe):
        fn.__module__ = "probe"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _harness.run_module(ns, "probe")
    require_equal(rc, 0, buf.getvalue())
    require(ran.get("async"), "the coroutine test was never awaited")
    require(ran.get("sync"), "the sync test never ran")


def t_harness_refuses_a_sync_test_that_returns_an_awaitable():
    """The exact H1 bug: a coroutine object must never count as a successful test."""
    async def _inner():
        raise AssertionError("must not be silently discarded")

    def t_returns_coroutine():
        return _inner()

    t_returns_coroutine.__module__ = "probe"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _harness.run_module({"t_returns_coroutine": t_returns_coroutine}, "probe")
    out = buf.getvalue()
    require(rc != 0, f"an un-awaited coroutine was reported as success:\n{out}")
    require("incomplete_async" in out or "awaitable" in out, out)


def t_harness_writes_the_result_to_the_runner_channel():
    """P1A.7/H4: the authoritative result goes to a file the RUNNER named, never to stdout.
    Nothing the suite prints can change it."""
    import json
    def t_a():
        pass

    def t_b():
        raise AssertionError("boom")

    for fn in (t_a, t_b):
        fn.__module__ = "probe"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _harness.run_module({"t_a": t_a, "t_b": t_b}, "probe")
    # P1A.8/H1: the result is HANDED BACK. There is no path and no environment variable
    # naming the channel any more, so nothing inside the suite process can address it.
    data = {"protocol": 2, "tests": _harness.last_results()}
    require(not hasattr(_harness, "RESULT_PATH_ENV"),
            "the addressable result path is back in the harness")
    require_equal(data.get("protocol"), 2, data)
    ids = {e["id"]: e for e in data["tests"]}
    require_equal(sorted(ids), ["t_a", "t_b"], ids)
    require_equal(ids["t_a"]["status"], "passed", ids["t_a"])
    require_equal(ids["t_b"]["status"], "failed", ids["t_b"])
    require(all(e["started"] and e["completed"] for e in data["tests"]), data)
    require(_harness.MANIFEST_PREFIX not in buf.getvalue(),
            "the authoritative result must not be printed to stdout")


def t_runner_requires_identity_set_equality():
    """Drive the real runner against a suite that hides a test from its own enumerator."""
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    runner = importlib.import_module("run_tests")
    tmp = tempfile.mkdtemp()
    suite = os.path.join(tmp, "test_probe_contract.py")
    with open(suite, "w", encoding="utf-8") as fh:
        fh.write(
            "import os, sys\n"
            f"sys.path.insert(0, {TESTS_DIR!r})\n"
            "from _guard import enforce_assertions\n"
            "enforce_assertions()\n"
            "def t_kept():\n    pass\n"
            "def t_hidden_security_test():\n    pass\n"
            'if __name__ == "__main__":\n'
            "    from _harness import run_module\n"
            "    ns = {k: v for k, v in globals().items() if k != 't_hidden_security_test'}\n"
            "    raise SystemExit(run_module(ns, __name__))\n")
    try:
        from pathlib import Path
        expected = _inventory.suite_expected(suite)
        require_equal(sorted(expected), ["t_hidden_security_test", "t_kept"], expected)
        res = runner.run_one(Path(suite), expected)
        require_equal(res.missing, ["t_hidden_security_test"], res.missing)
        require(res.failed >= 1, f"a dropped test was not a failure: {res.failures}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_runner_rejects_a_suite_without_a_result_channel():
    """A suite that does not use the shared harness cannot vouch for itself."""
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    runner = importlib.import_module("run_tests")
    tmp = tempfile.mkdtemp()
    suite = os.path.join(tmp, "test_probe_nomanifest.py")
    with open(suite, "w", encoding="utf-8") as fh:
        fh.write("import sys\n"
                 "def t_a():\n    pass\n"
                 'if __name__ == "__main__":\n'
                 '    print("\\n=== 1/1 bestanden ===")\n'
                 "    sys.exit(0)\n")
    try:
        from pathlib import Path
        res = runner.run_one(Path(suite), _inventory.suite_expected(suite))
        require(res.failed >= 1, "a self-reporting suite was accepted")
        require(any("no harness results" in f or "channel" in f for f in res.failures),
                res.failures)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_every_suite_uses_the_shared_harness():
    missing = []
    for path in sorted(_inventory.discover(TESTS_DIR)):
        src = open(path, encoding="utf-8").read()
        if "run_module(" not in src and "run_unittest(" not in src:
            missing.append(os.path.relpath(path, TESTS_DIR))
    require_equal(missing, [], f"suites not on the shared harness: {missing}")


# =====================================================================
# §6 — optimize safety must not regress
# =====================================================================
def t_optimize_guard_survives_the_new_harness():
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([os.path.join(REPO, "src"), TESTS_DIR])
    env.pop("PYTHONOPTIMIZE", None)
    bad = []
    for path in sorted(_inventory.discover(TESTS_DIR)):
        proc = subprocess.run([sys.executable, "-O", path], capture_output=True,
                              text=True, env=env, cwd=REPO, timeout=180)
        if proc.returncode == 0 or "optimized Python" not in (proc.stdout + proc.stderr):
            bad.append(os.path.relpath(path, TESTS_DIR))
    require_equal(bad, [], f"suites that no longer fail loudly under -O: {bad}")
    proc = subprocess.run([sys.executable, "-O", os.path.join(REPO, "scripts", "run_tests.py")],
                          capture_output=True, text=True, cwd=REPO)
    require(proc.returncode != 0, "the canonical runner ran under -O")


# =====================================================================
# §7-§8 — admin commands never mint identity
# =====================================================================
def t_admin_valid_state_works():
    tmp = tempfile.mkdtemp()
    try:
        asyncio.run(_setup(tmp))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = admin_cli.main(["--state-dir", tmp, "devices"])
        require_equal(rc, 0, buf.getvalue())
        require("iphone" in buf.getvalue(), buf.getvalue())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _setup(tmp):
    st, cp = await _open(tmp)
    await H.enroll_attested(cp, device_id="iphone", transport_cred="tc")
    await st.close()


def _admin_must_fail_closed(tmp, argv, label):
    before = _snapshot(tmp)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = admin_cli.main(argv)
    require(rc != 0, f"{label}: exited 0")
    require("gesperrt" not in out.getvalue(), f"{label}: printed a success line")
    require_equal(_snapshot(tmp), before, f"{label}: the state directory changed")


def t_admin_missing_identity_files_fail_closed():
    """The M1 finding: a read-only command must not create a core identity."""
    for victim in ("core_signing_key.pem", "core_instance_id"):
        tmp = tempfile.mkdtemp()
        try:
            asyncio.run(_setup(tmp))
            cid_before = open(os.path.join(tmp, "core_instance_id"), encoding="utf-8").read()
            os.remove(os.path.join(tmp, victim))
            for argv in (["--state-dir", tmp, "devices"],
                         ["--state-dir", tmp, "revoke-device", "iphone"]):
                _admin_must_fail_closed(tmp, argv, f"{victim}/{argv[-1]}")
            require(not os.path.exists(os.path.join(tmp, victim)),
                    f"{victim} was recreated by an admin command")
            if victim != "core_instance_id":
                require_equal(open(os.path.join(tmp, "core_instance_id"),
                                   encoding="utf-8").read(), cid_before,
                              "core_instance_id was rotated")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def t_admin_missing_database_fails_closed():
    tmp = tempfile.mkdtemp()
    try:
        asyncio.run(_setup(tmp))
        for suffix in ("", "-wal", "-shm"):
            p = os.path.join(tmp, DB + suffix)
            if os.path.exists(p):
                os.remove(p)
        _admin_must_fail_closed(tmp, ["--state-dir", tmp, "devices"], "no db")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_admin_wrong_state_dir_creates_nothing():
    tmp = tempfile.mkdtemp()
    typo = tmp + "-typo"
    try:
        asyncio.run(_setup(tmp))
        real = _snapshot(tmp)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = admin_cli.main(["--state-dir", typo, "revoke-device", "iphone"])
        require(rc != 0, "a typo state-dir succeeded")
        require("gesperrt" not in out.getvalue(), out.getvalue())
        require(not os.path.exists(typo), "a new state directory was created")
        require_equal(_snapshot(tmp), real, "the real state directory changed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(typo, ignore_errors=True)


def t_identity_load_helpers_never_create():
    tmp = tempfile.mkdtemp()
    try:
        require_raises(identity.MissingCoreIdentity, identity.load_core_instance_id, tmp)
        require_raises(identity.MissingCoreIdentity, identity.MacSigningKey.load, tmp)
        require_equal(_snapshot(tmp), {}, "a load-only helper created files")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# §10-§13 — device identity history and rotation-aware revoke
# =====================================================================
async def t_history_records_every_trusted_binding():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k1, k2 = H.new_device("dev-1"), H.new_device("dev-1")
        require_equal(await _enroll(cp, k1, "dev-1"), "ok", "first enrollment")
        require_equal(await _enroll(cp, k2, "dev-1"), "ok", "rotation")
        rows = await st._run(lambda: st._conn.execute(
            "SELECT * FROM device_identity_history WHERE device_id='dev-1'").fetchall())
        fps = {r["approval_key_sha256"] for r in rows}
        require(crypto.fingerprint(k1.appr_x963) in fps, "K1 not recorded")
        require(crypto.fingerprint(k2.appr_x963) in fps, "K2 not recorded")
        require_equal(len(rows), 2, f"expected two bindings, got {len(rows)}")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_history_is_append_only_and_idempotent():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k1 = H.new_device("dev-1")
        await _enroll(cp, k1, "dev-1")
        await _enroll(cp, k1, "dev-1")          # same material again
        rows = await st._run(lambda: st._conn.execute(
            "SELECT COUNT(*) c FROM device_identity_history").fetchone())
        require_equal(rows["c"], 1, "re-attesting the same binding duplicated history")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_revoke_device_covers_rotated_away_keys():
    """M2: a rotation used to orphan the previous approval key."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k1, k2 = H.new_device("dev-1"), H.new_device("dev-1")
        await _enroll(cp, k1, "dev-1")
        await _enroll(cp, k2, "dev-1")
        other = H.new_device("dev-other")
        await _enroll(cp, other, "dev-other", cred="to")

        res = await cp.revoke_device("dev-1", reason="compromised")
        for label, kind, value in (
                ("K1 approval", S.REVOKE_APPROVAL_KEY, crypto.fingerprint(k1.appr_x963)),
                ("K2 approval", S.REVOKE_APPROVAL_KEY, crypto.fingerprint(k2.appr_x963)),
                ("K1 attest", S.REVOKE_APP_ATTEST_KEY, k1.aakid),
                ("K2 attest", S.REVOKE_APP_ATTEST_KEY, k2.aakid)):
            require_equal(await st.is_revoked(kind, value), True, f"{label} not revoked")
        require((S.REVOKE_APPROVAL_KEY, crypto.fingerprint(k1.appr_x963)) in res.identities,
                f"the rotated-away key was not reported: {res}")

        require_equal(await _enroll(cp, k1, "dev-X"), "device_revoked", "K1 re-enrolled")
        require_equal(await _enroll(cp, k2, "dev-Y"), "device_revoked", "K2 re-enrolled")
        require_equal(await st.is_revoked(
            S.REVOKE_APPROVAL_KEY, crypto.fingerprint(other.appr_x963)), False,
            "an unrelated key was revoked")
        require_equal((await st.get_device("dev-other"))["status"], S.DEVICE_ACTIVE,
                      "an unrelated device was revoked")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_history_and_revoke_survive_restart():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k1, k2 = H.new_device("dev-1"), H.new_device("dev-1")
        await _enroll(cp, k1, "dev-1")
        await _enroll(cp, k2, "dev-1")
        await cp.revoke_device("dev-1", reason="compromised")
        await st.close()

        st2, cp2 = await _open(tmp)
        require_equal(await st2.is_revoked(
            S.REVOKE_APPROVAL_KEY, crypto.fingerprint(k1.appr_x963)), True, "K1 after restart")
        require_equal(await _enroll(cp2, k1, "dev-Z"), "device_revoked", "K1 re-enrolled")
        rows = await st2._run(lambda: st2._conn.execute(
            "SELECT COUNT(*) c FROM device_identity_history").fetchone())
        require_equal(rows["c"], 2, "history did not survive restart")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_pre_history_bindings_are_not_invented():
    """§12: a device row that predates the history table must not gain fabricated history."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k1 = H.new_device("dev-legacy")
        await _enroll(cp, k1, "dev-legacy")
        await st._run(lambda: st._conn.execute("DELETE FROM device_identity_history"))
        res = await cp.revoke_device("dev-legacy", reason="legacy")
        # only the CURRENT binding can be known — nothing is conjured for the missing past
        kinds = {k for k, _ in res.identities}
        require_equal(kinds, set(S.REVOKE_KINDS), f"unexpected scope: {res}")
        require_equal(len(res.identities), 3,
                      f"history was fabricated for a pre-history device: {res}")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# §14 — RevocationResult accuracy
# =====================================================================
async def t_revocation_result_reports_the_real_operation():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        a = await H.enroll_attested(cp, device_id="dev-a", transport_cred="ta")
        b = await H.enroll_attested(cp, device_id="dev-b", transport_cred="tb")
        c = await H.enroll_attested(cp, device_id="dev-c", transport_cred="tc")
        require_equal((await cp.revoke_device("dev-a")).operation, "revoke-device", "device")
        require_equal((await cp.revoke_approval_key(
            crypto.fingerprint(b.appr_x963))).operation, "revoke-approval-key", "approval")
        dev = await st.get_device("dev-c")
        require_equal((await cp.revoke_app_attest_key(
            dev["app_attest_key_id"])).operation, "revoke-app-attest-key", "app attest")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_no_committed_result_when_the_transaction_fails():
    """`committed` must never be True for something that rolled back — the result object is
    only ever constructed after COMMIT, and this proves it by failing the transaction."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")

        class Boom(RuntimeError):
            pass

        class FailConn:
            def __init__(self, real):
                self._real, self.begun = real, False

            def execute(self, sql, *args):
                head = sql.strip().upper()
                if head.startswith("BEGIN"):
                    self.begun = True
                if self.begun and head.startswith("INSERT INTO REVOCATIONS"):
                    raise Boom("injected")
                return self._real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._real, name)

        real = await st._run(lambda: st._conn)
        fc = FailConn(real)
        st._conn = fc
        got = None
        try:
            got = await cp.revoke_device("dev-1")
            raise AssertionError("the injected failure did not propagate")
        except Boom:
            pass
        finally:
            st._conn = real
        require(fc.begun, "the transaction never opened")
        require(got is None, f"a result was produced for a rolled-back revoke: {got}")
        require_equal([r for r in await st.list_revocations()], [], "a revocation committed")
        require_equal((await st.get_device("dev-1"))["status"], S.DEVICE_ACTIVE, "status")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_committed_result_matches_the_database():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        k1, k2 = H.new_device("dev-1"), H.new_device("dev-1")
        await _enroll(cp, k1, "dev-1")
        await _enroll(cp, k2, "dev-1")
        res = await cp.revoke_device("dev-1", reason="x")
        require(res.committed, "a committed transaction reported committed=False")
        recorded = {(r["kind"], r["value"]) for r in await st.list_revocations()}
        for entry in res.identities:
            require(entry in recorded, f"reported but not committed: {entry}")
        require_equal(len(recorded), len(res.identities),
                      f"committed rows and reported identities differ: {recorded} vs {res}")
        revoked = {d["device_id"] for d in await st.list_devices_full()
                   if d["status"] == S.DEVICE_REVOKED}
        require_equal(set(res.affected_devices), revoked, "affected devices differ from DB")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
