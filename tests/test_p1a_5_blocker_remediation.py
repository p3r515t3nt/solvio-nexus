"""P1A.5 — the five HIGH blockers a cold review reproduced against 138b04e, plus the
assurance defects that came with them.

H1  the admin CLI printed a revocation scope it had read BEFORE the transaction; the
    transaction determined its own. A re-enrollment in the window made the CLI announce
    identities it had not revoked, while the real key material stayed enrollable.
H2  `python -O` still produced false greens: the optimize guard only reached suites that
    happened to import the attest helper (11 of 25). With the S1 broker's digest binding —
    the last execution boundary — deleted, `python -O tests/test_approval.py` printed 17/17
    where normal Python printed 16/17.
H3  the canonical runner reported a suite collecting ZERO tests as green, contradicting its
    own docstring.
H4  `canonical_device_id` was a blocklist and missed ten invisible characters, including the
    bidi overrides. Ten identifiers all rendered as `dev-1`.
H5  the F5.1 terminality guard was defended by no effective test; deleting it left every
    suite green.
M1  `--reason` displaced the revoked identity in the audit row.
M2  a mistyped `--state-dir` created a fresh store and printed "gesperrt (terminal)", exit 0.
M3  production security policy enforced by a bare `assert`.
M4  `attestation_status` was not part of the execution claim — REPRODUCED before fixing:
    a device whose attestation had failed still executed an earlier approval.

ASSERTION POLICY: `require*` from `tests/_guard.py` are function calls and survive `-O`;
`enforce_assertions()` is called explicitly at import. Bare `assert` elsewhere in the repo
is still stripped by `-O` — what protects those suites is the same explicit bootstrap, now
present in every standalone entry point, plus the canonical runner.

Direct: python tests/test_p1a_5_blocker_remediation.py
"""
import asyncio
import contextlib
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

import mobile_attest_helper as H  # noqa: E402
from _guard import require, require_equal, require_raises  # noqa: E402
from solvio.security.approval import ApprovalBroker  # noqa: E402
from solvio.security.mobile_approval import admin_cli  # noqa: E402
from solvio.security.mobile_approval import app_attest as AA  # noqa: E402
from solvio.security.mobile_approval import bridge as B  # noqa: E402
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


async def _enroll(cp, ctx, device_id, cred="tc"):
    token, _ = await cp.create_enrollment_token("local-owner")
    res, s = await cp.begin_enrollment(
        enrollment_token=token, device_id=device_id,
        approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
        app_attest_key_id=ctx.aakid, transport_cred=cred)
    require_equal(s, "ok", f"enroll {device_id}")
    _, s2 = await cp.complete_attestation(
        enrollment_id=res["enrollment_id"],
        attestation_b64=P.b64e(AA.fake_attestation(ctx.aakey_x963)))
    require_equal(s2, "ok", f"attest {device_id}")


async def _revocations(st):
    return {(r["kind"], r["value"]) for r in await st.list_revocations()}


# =====================================================================
# H1 — the reported scope is the committed scope
# =====================================================================
class _HookConn:
    """Fires a callback at a chosen statement of the live production path."""

    def __init__(self, real, *, on, hook):
        self._real, self._on, self._hook = real, on.upper(), hook
        self.fired = False

    def execute(self, sql, *args):
        if not self.fired and sql.strip().upper().startswith(self._on):
            self.fired = True
            self._hook()
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def t_h1_reported_scope_equals_committed_scope_under_race():
    """The exact race that broke the old CLI: the bound key material changes between the
    moment a pre-read would have run and the moment the transaction takes the lock."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        old = H.new_device("iphone")
        await _enroll(cp, old, "iphone")
        old_fp = crypto.fingerprint(old.appr_x963)
        new = H.new_device("iphone")
        loop = asyncio.get_running_loop()
        done = {}

        def swap_material():
            # runs immediately BEFORE the revoke transaction takes its write lock
            def work():
                async def go():
                    st2, cp2 = await _open(tmp)
                    try:
                        await _enroll(cp2, new, "iphone")
                        done["ok"] = True
                    finally:
                        await st2.close()
                asyncio.run(go())
            import threading
            t = threading.Thread(target=work)
            t.start()
            t.join()

        real = await st._run(lambda: st._conn)
        hooked = _HookConn(real, on="BEGIN", hook=swap_material)
        st._conn = hooked
        try:
            res = await cp.revoke_device("iphone", reason="stolen")
        finally:
            st._conn = real
        require(hooked.fired and done.get("ok"), "the racing re-enrollment never ran")

        recorded = await _revocations(st)
        # Everything the result reports must actually be in the durable blocklist.
        for kind, value in res.identities:
            require((kind, value) in recorded,
                    f"reported identity not in revocations: {kind}={value}")
        # And it must name the material the transaction really saw — the NEW one.
        new_fp = crypto.fingerprint(new.appr_x963)
        reported = {v for _, v in res.identities}
        require(new_fp in reported, f"committed material not reported: {res}")
        # P1A.6/§11: with the device-identity history in place the PREVIOUS binding is
        # legitimately revoked too, so the invariant is not "never name pre-lock material"
        # but the stronger "name exactly what was committed" enforced above.
        require_equal(await st.is_revoked(S.REVOKE_APPROVAL_KEY, new_fp), True, "new fp")
        if old_fp in reported:
            require_equal(await st.is_revoked(S.REVOKE_APPROVAL_KEY, old_fp), True,
                          "reported the old key without revoking it")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h1_cli_prints_only_identities_that_are_really_revoked():
    tmp = tempfile.mkdtemp()
    try:
        async def setup():
            st, cp = await _open(tmp)
            ctx = await H.enroll_attested(cp, device_id="iphone", transport_cred="tc")
            await st.close()
            return ctx
        ctx = asyncio.run(setup())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = admin_cli.main(["--state-dir", tmp, "revoke-device", "iphone",
                                 "--reason", "stolen"])
        require_equal(rc, 0, "CLI failed")
        out = buf.getvalue()

        async def check():
            st = S.ApprovalControlStore(os.path.join(tmp, DB))
            await st.open()
            try:
                return await _revocations(st), await st.list_devices_full()
            finally:
                await st.close()
        recorded, devices = asyncio.run(check())
        values = {v for _, v in recorded}
        printed = [ln.split(":", 1)[1].strip() for ln in out.splitlines()
                   if ln.strip().startswith(("gesperrt ", "bereits gesperrt"))]
        require(printed, f"CLI printed no identities:\n{out}")
        for token in printed:
            value = token.split()[-1]
            require(value in values,
                    f"CLI printed an identity that is NOT revoked: {value}\n{out}")
        require(str(len([d for d in devices if d["status"] == S.DEVICE_REVOKED])) in out,
                f"affected count not reported truthfully:\n{out}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h1_cli_output_is_truthful_under_a_real_race():
    """The decisive H1 test: race the CLI itself.

    A structural "no pre-read" check and a race-free output check both pass against a
    reintroduced pre-read — verified by mutation. Only an actual interleaving distinguishes
    a report derived from the committed transaction from one derived from an earlier read.
    The bound key material is swapped the moment the revoke transaction takes its lock.
    """
    tmp = tempfile.mkdtemp()
    try:
        old = H.new_device("iphone")
        new = H.new_device("iphone")

        async def setup():
            st, cp = await _open(tmp)
            await _enroll(cp, old, "iphone")
            await st.close()
        asyncio.run(setup())

        def swap():
            async def go():
                st2, cp2 = await _open(tmp)
                try:
                    await _enroll(cp2, new, "iphone")
                finally:
                    await st2.close()
            import threading
            t = threading.Thread(target=lambda: asyncio.run(go()))
            t.start(); t.join()

        real_open = admin_cli._open
        state = {}

        async def hooked_open(state_dir, **kw):
            st, cp = await real_open(state_dir, **kw)
            conn = await st._run(lambda: st._conn)
            hook = _HookConn(conn, on="BEGIN", hook=swap)
            st._conn = hook
            state["hook"] = hook
            return st, cp

        admin_cli._open = hooked_open
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = admin_cli.main(["--state-dir", tmp, "revoke-device", "iphone"])
        finally:
            admin_cli._open = real_open
        require_equal(rc, 0, "CLI failed")
        require(state.get("hook") and state["hook"].fired, "the race never fired")
        out = buf.getvalue()

        async def check():
            st = S.ApprovalControlStore(os.path.join(tmp, DB))
            await st.open()
            try:
                return await _revocations(st)
            finally:
                await st.close()
        recorded = {v for _, v in asyncio.run(check())}
        printed = [ln.split()[-1] for ln in out.splitlines()
                   if ln.strip().startswith(("gesperrt ", "bereits gesperrt"))]
        require(printed, f"CLI printed no identities:\n{out}")
        for value in printed:
            require(value in recorded,
                    f"CLI announced an identity that is NOT revoked: {value}\n{out}")
        # P1A.6/§11: the previous binding is now legitimately revoked via the device
        # identity history, so it MAY appear — but only if it really is revoked. That is
        # already enforced by the loop above; what must never happen is an announcement
        # without a matching committed row.
        old_fp = crypto.fingerprint(old.appr_x963)
        if old_fp in printed:
            require(old_fp in recorded,
                    f"CLI announced the previous key without revoking it:\n{out}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h1_cli_has_no_pre_transaction_scope_read():
    src = open(os.path.join(REPO, "src", "solvio", "security", "mobile_approval",
                            "admin_cli.py"), encoding="utf-8").read()
    require("revoke_device_scope" not in src,
            "the CLI still computes a scope outside the transaction")
    ctrl = open(os.path.join(REPO, "src", "solvio", "security", "mobile_approval",
                             "control.py"), encoding="utf-8").read()
    require("def revoke_device_scope" not in ctrl,
            "the pre-read helper is still available to be misused")


async def t_h1_failed_revoke_prints_no_success():
    """A rollback must produce no success output and no revocation."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        await H.enroll_attested(cp, device_id="iphone", transport_cred="tc")

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
        try:
            await cp.revoke_device("iphone", reason="x")
            raise AssertionError("injected failure did not propagate")
        except Boom:
            pass
        finally:
            st._conn = real
        require(fc.begun, "the transaction never opened")
        require_equal(await _revocations(st), set(), "a revocation was committed")
        require_equal((await st.get_device("iphone"))["status"], S.DEVICE_ACTIVE, "status")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# M2 — a wrong --state-dir must fail closed
# =====================================================================
def t_m2_wrong_state_dir_fails_closed():
    tmp = tempfile.mkdtemp()
    typo = tmp + "-typo"
    try:
        async def setup():
            st, cp = await _open(tmp)
            await H.enroll_attested(cp, device_id="iphone", transport_cred="tc")
            await st.close()
        asyncio.run(setup())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = admin_cli.main(["--state-dir", typo, "revoke-device", "iphone"])
        require(rc != 0, f"a mistyped state-dir succeeded (rc={rc})")
        require("gesperrt" not in out.getvalue(),
                f"a success line was printed for a wrong state-dir:\n{out.getvalue()}")
        require(not os.path.exists(typo), "a new security store was created silently")

        async def check():
            st = S.ApprovalControlStore(os.path.join(tmp, DB))
            await st.open()
            try:
                return (await st.get_device("iphone"))["status"], await _revocations(st)
            finally:
                await st.close()
        status, rev = asyncio.run(check())
        require_equal(status, S.DEVICE_ACTIVE, "the real store was modified")
        require_equal(rev, set(), "the real store gained a revocation")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(typo, ignore_errors=True)


def t_m2_empty_or_foreign_directory_fails_closed():
    for make in (lambda d: None,                      # empty directory
                 lambda d: open(os.path.join(d, DB), "wb").write(b"not a database"),
                 lambda d: sqlite3.connect(os.path.join(d, DB)).close()):  # wrong schema
        tmp = tempfile.mkdtemp()
        try:
            make(tmp)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = admin_cli.main(["--state-dir", tmp, "revoke-device", "x"])
            require(rc != 0, f"accepted an invalid state-dir (rc={rc})")
            require("gesperrt" not in out.getvalue(), out.getvalue())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# M1 — the audit keeps the identity even with --reason
# =====================================================================
async def t_m1_audit_keeps_identity_with_and_without_reason():
    for reason in (None, "lost phone"):
        tmp = tempfile.mkdtemp()
        try:
            st, cp = await _open(tmp)
            ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
            fp = crypto.fingerprint(ctx.appr_x963)
            await cp.revoke_approval_key(fp, reason=reason)
            rows = await st._run(lambda: st._conn.execute(
                "SELECT event, reason, identity FROM audit "
                "WHERE event='approval_key_revoked'").fetchall())
            require_equal(len(rows), 1, "expected one revocation event")
            row = dict(rows[0])
            require(fp in (row["identity"] or ""),
                    f"the revoked identity is not in the audit row: {row}")
            require_equal(row["reason"], reason, "operator reason not preserved")
            await st.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


async def t_m1_repeat_revoke_is_audited_honestly():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        fp = crypto.fingerprint(ctx.appr_x963)
        await cp.revoke_approval_key(fp, reason="first")
        res = await cp.revoke_approval_key(fp, reason="again")
        require_equal(res.newly_revoked, (), "a repeat claimed a first revocation")
        require(res.already_revoked, "a repeat did not report already-revoked")
        rows = await st._run(lambda: st._conn.execute(
            "SELECT event, identity FROM audit").fetchall())
        events = [r["event"] for r in rows]
        require_equal(events.count("approval_key_revoked"), 1,
                      "a repeat was logged as a first revocation")
        reaffirm = [dict(r) for r in rows if r["event"] == "revocation_reaffirmed"]
        require(reaffirm, "the corrective re-run left no trace")
        require(fp in (reaffirm[0]["identity"] or ""),
                f"reaffirm event lost the identity: {reaffirm[0]}")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# H2 / M3 — optimize safety and production asserts
# =====================================================================
def _all_suites():
    out = []
    for root, _, files in os.walk(TESTS_DIR):
        for f in sorted(files):
            if f.startswith("test_") and f.endswith(".py"):
                out.append(os.path.join(root, f))
    return sorted(out)


def t_h2_every_standalone_suite_refuses_optimized_python():
    """Structural, not a spot check: EVERY discoverable suite must fail loudly under -O."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([os.path.join(REPO, "src"), TESTS_DIR])
    env.pop("PYTHONOPTIMIZE", None)
    suites = _all_suites()
    require(len(suites) >= 20, f"suite discovery looks wrong: {len(suites)}")
    bad = []
    for path in suites:
        proc = subprocess.run([sys.executable, "-O", path], capture_output=True,
                              text=True, env=env, cwd=REPO, timeout=120)
        blob = proc.stdout + proc.stderr
        if proc.returncode == 0 or "optimized Python" not in blob:
            bad.append(f"{os.path.relpath(path, TESTS_DIR)} (rc={proc.returncode})")
    require_equal(bad, [], f"suites that do not fail loudly under -O: {bad}")


def t_h2_every_suite_calls_the_shared_bootstrap():
    missing = [os.path.relpath(p, TESTS_DIR) for p in _all_suites()
               if "enforce_assertions()" not in open(p, encoding="utf-8").read()]
    require_equal(missing, [], f"suites without the explicit optimize bootstrap: {missing}")


def t_h2_canonical_runner_refuses_optimized_python():
    proc = subprocess.run([sys.executable, "-O", os.path.join(REPO, "scripts", "run_tests.py")],
                          capture_output=True, text=True, cwd=REPO)
    require(proc.returncode != 0, "the canonical runner ran under -O")
    require("optimized Python" in (proc.stdout + proc.stderr), proc.stderr)


def t_m3_no_bare_assert_enforces_production_security():
    """A security policy must not depend on `assert`; -O removes it."""
    offenders = []
    roots = [os.path.join(REPO, "src", "solvio", "security"),
             os.path.join(REPO, "scripts")]
    for root in roots:
        for dirpath, _, files in os.walk(root):
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(dirpath, f)
                if os.path.basename(path) == "run_tests.py":
                    continue
                for n, line in enumerate(open(path, encoding="utf-8"), 1):
                    if line.lstrip().startswith("assert ") and "research_live" not in path:
                        offenders.append(f"{os.path.relpath(path, REPO)}:{n}")
    require_equal(offenders, [],
                  f"production security code enforced by bare assert: {offenders}")


# =====================================================================
# H3 — a suite that collects nothing is a failure
# =====================================================================
def t_h3_zero_test_suite_fails_the_runner():
    tmp = tempfile.mkdtemp()
    suite = os.path.join(tmp, "test_empty_probe.py")
    with open(suite, "w", encoding="utf-8") as fh:
        fh.write("import sys\n"
                 "def check_nothing():\n    pass\n"
                 'if __name__ == "__main__":\n'
                 '    tests = [v for k, v in sorted(globals().items()) if k.startswith("t_")]\n'
                 '    print(f"\\n=== {len(tests)}/{len(tests)} bestanden ===")\n'
                 "    sys.exit(0)\n")
    try:
        sys.path.insert(0, os.path.join(REPO, "scripts"))
        import importlib
        runner = importlib.import_module("run_tests")
        res = runner.run_one(__import__("pathlib").Path(suite))
        require_equal(res.collected, 0, "probe suite should collect nothing")
        require(res.failed >= 1, f"a zero-test suite was not a failure: {res.failures}")
        require(any("zero tests" in f for f in res.failures),
                f"no clear zero-test message: {res.failures}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h3_runner_docstring_matches_behaviour():
    src = open(os.path.join(REPO, "scripts", "run_tests.py"), encoding="utf-8").read()
    require("_enforce_non_empty" in src, "the zero-test guard is gone")
    require("zero tests collected" in src, "no explicit zero-test failure message")


# =====================================================================
# H4 — identifier policy
# =====================================================================
_INVISIBLE = [0x00AD, 0x0080, 0x009F, 0x0085, 0x180E, 0x2060, 0xFEFF, 0x202A, 0x202B,
              0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0x061C, 0x200B,
              0x200C, 0x200D, 0x200E, 0x200F, 0x00A0, 0x3000, 0x0009, 0x000A, 0x000D]


def t_h4_all_invisible_and_bidi_identifiers_are_refused():
    bad = []
    for cp in _INVISIBLE:
        for candidate in (f"dev-1{chr(cp)}", f"{chr(cp)}dev-1", f"dev{chr(cp)}1"):
            try:
                C.canonical_device_id(candidate)
                bad.append(f"U+{cp:04X}")
            except ValueError:
                pass
    require_equal(sorted(set(bad)), [], f"accepted invisible identifiers: {sorted(set(bad))}")


def t_h4_legitimate_identifiers_still_work():
    for good in ("dev-1", "dev-a2d63af3-8b1c-4e5f-9a7d-1234567890ab", "A_b.c:d-1",
                 "x" * 128, "0", "iPhone.15:Pro-2024"):
        require_equal(C.canonical_device_id(good), good, f"altered or rejected {good!r}")


def t_h4_identifier_is_never_silently_normalised():
    for value in ("dev-1 ", " dev-1", "DEV-1 ", "dev-1\t"):
        require_raises(ValueError, C.canonical_device_id, value,
                       message=f"silently normalised {value!r}")
    require_equal(C.canonical_device_id("DEV-1"), "DEV-1", "case was folded")


async def t_h4_lookalikes_cannot_enroll():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _open(tmp)
        await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        for cp_ord in (0x202E, 0xFEFF, 0x00AD, 0x2060):
            other = H.new_device("z")
            token, _ = await cp.create_enrollment_token("local-owner")
            res, s = await cp.begin_enrollment(
                enrollment_token=token, device_id=f"dev-1{chr(cp_ord)}",
                approval_public_key_x963_b64=P.b64e(other.appr_x963),
                app_attest_key_id=other.aakid, transport_cred="tc")
            require(res is None and s == "bad_device_id", f"U+{cp_ord:04X}: {s}")
        require_equal(len(await st.list_devices_full()), 1, "a lookalike was enrolled")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# H5 — the F5.1 terminality guard, defended on its own
# =====================================================================
async def t_h5_revoked_device_row_without_revocation_entry_stays_terminal():
    """The legacy shape the old revoke_device produced: devices.status=REVOKED with NO row
    in `revocations`. Only the upsert's own guard can refuse this — the P1A.4 revocation
    pre-check finds nothing, so it cannot mask the result."""
    tmp = tempfile.mkdtemp()
    try:
        st, _ = await _open(tmp)
        ctx = H.new_device("dev-legacy")
        ids = [(S.REVOKE_DEVICE, "dev-legacy"),
               (S.REVOKE_APPROVAL_KEY, crypto.fingerprint(ctx.appr_x963)),
               (S.REVOKE_APP_ATTEST_KEY, ctx.aakid)]
        first = await H.store_begin(st, ctx, device_id="dev-legacy", identities=ids)
        require_equal(first, "ok", "setup enrollment")

        # legacy state, produced directly: terminal status, no durable revocation
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:
            con.execute("UPDATE devices SET status=? WHERE device_id=?",
                        (S.DEVICE_REVOKED, "dev-legacy"))
        finally:
            con.close()
        require_equal(await _revocations(st), set(),
                      "precondition: no revocation row may exist, or the guard is masked")

        again = await H.store_begin(st, ctx, device_id="dev-legacy", identities=ids)
        require_equal(again, "device_revoked",
                      "the F5.1 terminality guard let a REVOKED row be reactivated")
        require_equal((await st.get_device("dev-legacy"))["status"], S.DEVICE_REVOKED,
                      "the row was reactivated")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h5_guard_holds_across_a_second_connection():
    tmp = tempfile.mkdtemp()
    try:
        stA, _ = await _open(tmp)
        stB, _ = await _open(tmp)
        ctx = H.new_device("dev-legacy2")
        ids = [(S.REVOKE_DEVICE, "dev-legacy2"),
               (S.REVOKE_APPROVAL_KEY, crypto.fingerprint(ctx.appr_x963)),
               (S.REVOKE_APP_ATTEST_KEY, ctx.aakid)]
        await H.store_begin(stA, ctx, device_id="dev-legacy2", identities=ids)
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:
            con.execute("UPDATE devices SET status=? WHERE device_id=?",
                        (S.DEVICE_REVOKED, "dev-legacy2"))
        finally:
            con.close()
        out = await H.store_begin(stB, ctx, device_id="dev-legacy2", identities=ids)
        require_equal(out, "device_revoked", "another connection reactivated a REVOKED row")
        await stA.close(); await stB.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# M4 — attestation status at the execution claim
# =====================================================================
async def _execute_with_attestation(att_status):
    tmp = tempfile.mkdtemp(); ws = tempfile.mkdtemp()
    try:
        st, cp, coord, _ = await _wire(tmp)
        ctx = await H.enroll_attested(cp, device_id="dev-1", transport_cred="tc")
        aid = await coord.request_codex_modify(
            principal="local-owner", task="edit", workspace=ws, human_summary="s")
        wire, s = await cp.issue_challenge(approval_id=aid, device_id="dev-1")
        require_equal(s, "ok", "challenge")
        _, s2 = await coord.apply_mobile_decision(
            **H.sign_decision(ctx, P.b64d(wire["payload_b64"])))
        require_equal(s2, "ok", "decision")
        if att_status is not None:
            con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
            try:
                con.execute("UPDATE devices SET attestation_status=? WHERE device_id=?",
                            (att_status, "dev-1"))
            finally:
                con.close()
            require_equal((await st.get_device("dev-1"))["status"], S.DEVICE_ACTIVE,
                          "precondition: the device must otherwise still be healthy")
        calls = []
        out, status = await coord.execute_approved(aid, _executor(calls))
        state = (await st.get_request(aid))["state"]
        await st.close()
        return out, status, calls, state
    finally:
        shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(ws, ignore_errors=True)


async def t_m4_unattested_device_cannot_execute():
    for att in ("ATTESTATION_FAILED", "PENDING_ATTESTATION", "UNATTESTED", "ATTESTED_"):
        out, status, calls, state = await _execute_with_attestation(att)
        require(out is None, f"{att}: executed")
        require_equal(calls, [], f"{att}: the executor ran without a valid attestation")
        require_equal(state, S.APPROVED, f"{att}: state moved")


async def t_m4_attested_device_still_executes():
    out, status, calls, state = await _execute_with_attestation(None)
    require_equal(status, "ok", f"a healthy attested device was refused: {status}")
    require_equal(len(calls), 1, "executor did not run")
    require_equal(state, S.CONSUMED, "state")


# =====================================================================
# Structural inventory of the test population
# =====================================================================
def t_inventory_every_tracked_suite_is_discovered_and_non_empty():
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import importlib
    runner = importlib.import_module("run_tests")
    discovered = {os.path.abspath(str(p)) for p in runner.discover([])}
    on_disk = {os.path.abspath(p) for p in _all_suites()}
    require_equal(sorted(on_disk - discovered), [],
                  "suites on disk the canonical runner does not discover")
    memory = [p for p in discovered if os.sep + "memory" + os.sep in p]
    require(memory, "the tests/memory suites are not discovered")
    security = [p for p in discovered if "p1a" in os.path.basename(p).lower()]
    require(len(security) >= 5, f"security suites missing from discovery: {len(security)}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
