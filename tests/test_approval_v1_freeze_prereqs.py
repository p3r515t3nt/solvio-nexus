"""Approval Security V1 — the six freeze prerequisites.

Every item here is a known finding from the hygiene cold review. None was a merge blocker;
each becomes one the moment a real capability is switched on. Nothing new is opened.

  F1  startup_recovery_scan had no production caller — and could not have been wired
      safely, because it terminalised attempts from a snapshot with no lease and no
      expected-status compare
  F2  the recovery re-check asked "is this device healthy today", not "is this still the
      authority this execution was claimed under"
  F3  the generic `store.transition` could represent APPROVED -> EXECUTING outside the
      journal
  F4  nothing pinned the realtime tool-call loop to the LLM-safe dispatcher
  F5  test gaps behind honest-sounding names
  F6  documentation drift

OUT OF SCOPE, unchanged: product capabilities, S2B, remote approvals, new architecture.

ASSERTION POLICY: `require*` from `tests/_guard.py` are function calls and survive `-O`.

Direct: python tests/test_approval_v1_freeze_prereqs.py
"""
import ast as _ast
import asyncio
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

import execution_test_services as ES  # noqa: E402
import mobile_attest_helper as H  # noqa: E402
from _guard import require, require_equal  # noqa: E402
from solvio.security.approval import ApprovalBroker  # noqa: E402
from solvio.security.mobile_approval import bridge as B  # noqa: E402
from solvio.security.mobile_approval import control as C  # noqa: E402
from solvio.security.mobile_approval import execution as X  # noqa: E402
from solvio.security.mobile_approval import identity  # noqa: E402
from solvio.security.mobile_approval import protocol as P  # noqa: E402
from solvio.security.mobile_approval import store as S  # noqa: E402

APP_ID = "WQ8CG7R53R.de.solvio.approvals"
DB = "approval_control.sqlite3"
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(REPO, "src")
STORE_SRC = os.path.join(SRC, "solvio", "security", "mobile_approval", "store.py")
BRIDGE_SRC = os.path.join(SRC, "solvio", "security", "mobile_approval", "bridge.py")
GATEWAY_RUNNER = os.path.join(REPO, "scripts", "run_approval_gateway.py")
CORE_SERVER = os.path.join(SRC, "solvio", "realtime", "core_server.py")

X.CAPABILITY_SEMANTICS.setdefault("t_idempotent", X.IDEMPOTENT_WRITE)
X.CAPABILITY_SEMANTICS.setdefault("t_reconcilable", X.RECONCILABLE_WRITE)
X.CAPABILITY_SEMANTICS.setdefault("t_non_idempotent", X.NON_IDEMPOTENT_WRITE)
KIND_TO_TOOL = {"idempotent": "t_idempotent", "reconcilable": "t_reconcilable",
                "non_idempotent": "t_non_idempotent"}
B.MobileApprovalCoordinator.claim_lease_seconds = 0.2
B.MobileApprovalCoordinator.recovery_lease_seconds = 5.0


async def _wire(tmp):
    st = S.ApprovalControlStore(os.path.join(tmp, DB))
    await st.open()
    cp = C.MobileApprovalControlPlane(
        st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
        attest_verifier=H.fake_verifier(), app_id=APP_ID,
        allowed_environments={"development"})
    approver = B.MobileApprover()
    return st, cp, B.MobileApprovalCoordinator(cp, ApprovalBroker(approver=approver), approver)


async def _approved(tmp, kind="non_idempotent"):
    st, cp, co = await _wire(tmp)
    ctx = await H.enroll_attested(cp)
    ws = os.path.join(tmp, "ws")
    os.makedirs(ws, exist_ok=True)
    aid = await cp.create_request(principal="local-owner", tool=KIND_TO_TOOL[kind],
                                  mode="modify", task="edit README", workspace=ws,
                                  human_summary="Codex will edit README")
    wire, _ = await cp.issue_challenge(approval_id=aid, device_id=ctx.device_id)
    _res, status = await co.apply_mobile_decision(
        **H.sign_decision(ctx, P.b64d(wire["payload_b64"])))
    require_equal(status, "ok", f"setup decision: {status}")
    with open(os.path.join(tmp, "aid.txt"), "w", encoding="utf-8") as fh:
        fh.write(aid)                      # the crash child reads it from here
    return st, cp, co, ctx, aid


def _rows(tmp, sql, args=()):
    con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _attempts(tmp):
    return _rows(tmp, "SELECT * FROM execution_attempts ORDER BY claimed_at")


def _events(tmp):
    return [r["event"] for r in _rows(tmp, "SELECT event FROM audit ORDER BY id")]


def _expire_leases(tmp):
    con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
    try:
        con.execute("UPDATE execution_attempts SET lease_expires_at=?", (time.time() - 1,))
    finally:
        con.close()


async def _crashed_claimed(tmp, kind="non_idempotent"):
    """A real crashed child that leaves the attempt CLAIMED, lease lapsed."""
    import test_p1c_execution_recovery as P1C
    svc = ES.service_path(tmp)
    ES.make(kind, svc)
    proc, _ = P1C._child(tmp, "post_claim", kind, svc)
    require_equal(proc.returncode, -9, f"child did not die by SIGKILL: {proc.returncode}")
    require_equal([a["status"] for a in _attempts(tmp)], [X.CLAIMED], _attempts(tmp))
    _expire_leases(tmp)
    return svc


async def _crashed_pending(tmp, kind):
    """A real crashed child AFTER the external effect: EXTERNAL_PENDING, effect count 1."""
    import test_p1c_execution_recovery as P1C
    svc = ES.service_path(tmp)
    ES.make(kind, svc)
    proc, _ = P1C._child(tmp, "post_effect", kind, svc)
    require_equal(proc.returncode, -9, f"child did not die by SIGKILL: {proc.returncode}")
    require_equal([a["status"] for a in _attempts(tmp)], [X.EXTERNAL_PENDING], _attempts(tmp))
    require_equal(ES.make(kind, svc).count(), 1, "setup: the effect should have happened")
    _expire_leases(tmp)
    return svc


# =====================================================================
# F1 — the startup scan is wired, and safe to be wired
# =====================================================================
async def t_f1_the_startup_scan_has_a_production_caller():
    """It is no longer merely present. Asserted over the call graph, not a grep."""
    tree = _ast.parse(open(GATEWAY_RUNNER, encoding="utf-8").read())
    calls = [n for n in _ast.walk(tree)
             if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
             and n.func.attr == "startup_recovery_scan"]
    require(calls, "the gateway startup does not call startup_recovery_scan")
    awaited = [n for n in _ast.walk(tree)
               if isinstance(n, _ast.Await) and isinstance(n.value, _ast.Call)
               and isinstance(n.value.func, _ast.Attribute)
               and n.value.func.attr == "startup_recovery_scan"]
    require(awaited, "the scan is called but never awaited")


async def t_f1_startup_respects_a_live_recovery_lease():
    """T2 shape, single process: a valid lease means someone may still be working."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        await st.close()
        import test_p1c_execution_recovery as P1C
        svc = ES.service_path(tmp)
        ES.make("non_idempotent", svc)
        P1C._child(tmp, "post_claim", "non_idempotent", svc)      # leaves a LIVE claim lease
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:                                                       # make it unambiguously live
            con.execute("UPDATE execution_attempts SET lease_owner='someone-else', "
                        "lease_expires_at=?", (time.time() + 300,))
        finally:
            con.close()
        st2, cp2, co2 = await _wire(tmp)
        summary = await co2.startup_recovery_scan()
        require_equal(summary["closed_no_effect"], 0,
                      f"a leased attempt was closed anyway: {summary}")
        require_equal(len(summary["contended"]), 1, summary)
        require_equal(summary["contended"][0]["detail"], "recovery_lease_held", summary)
        require_equal([a["status"] for a in _attempts(tmp)], [X.CLAIMED],
                      "the leased attempt was mutated")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_f1_startup_closes_a_lapsed_claimed_attempt():
    """The other direction: once the dead owner's lease lapses, closing IS correct."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        await st.close()
        svc = await _crashed_claimed(tmp)
        st2, cp2, co2 = await _wire(tmp)
        summary = await co2.startup_recovery_scan()
        require_equal(summary["closed_no_effect"], 1, summary)
        require_equal([a["status"] for a in _attempts(tmp)], [X.ABANDONED], _attempts(tmp))
        require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"], S.FAILED,
                      "the request was left EXECUTING")
        require_equal(ES.make("non_idempotent", svc).count(), 0, "startup executed something")
        require("startup_recovery_scan" in _events(tmp), _events(tmp))
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_f1_t1_a_stale_claimed_snapshot_cannot_close_an_external_pending_attempt():
    """T1 — THE race, made deterministic.

    The scan reads CLAIMED, and between that read and the write the owning process crosses
    the durable boundary. Closing the attempt now would durably assert 'no external effect
    took place' about an effect that may well have happened. The interposition below moves
    the row at exactly that instant — no sleeps, no timing luck.
    """
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        await st.close()
        await _crashed_claimed(tmp)
        st2, cp2, co2 = await _wire(tmp)
        fired = []
        real_lease = st2.acquire_recovery_lease

        async def interposed(**kw):
            out = await real_lease(**kw)
            if not fired:                       # exactly at the decision point
                fired.append(1)
                con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
                try:
                    con.execute("UPDATE execution_attempts SET status=?, boundary_at=? "
                                "WHERE status=?",
                                (X.EXTERNAL_PENDING, time.time(), X.CLAIMED))
                finally:
                    con.close()
            return out

        st2.acquire_recovery_lease = interposed
        summary = await co2.startup_recovery_scan()
        require(fired, "the racing transition never ran — the probe is vacuous")
        require_equal(summary["closed_no_effect"], 0,
                      f"a stale CLAIMED snapshot closed an EXTERNAL_PENDING attempt: {summary}")
        require_equal([a["status"] for a in _attempts(tmp)], [X.EXTERNAL_PENDING],
                      "the ambiguous attempt was terminalised from a stale snapshot")
        reported = summary["needs_attention"] + summary["contended"]
        require_equal(len(reported), 1, f"the race was not surfaced: {summary}")
        require(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"] != S.FAILED,
                "the request was failed on the strength of a stale read")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_f1_the_expected_status_compare_is_load_bearing():
    """The CAS alone, without the re-read: an outcome may not be applied to a moved row."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        await st.close()
        await _crashed_claimed(tmp)
        st2, cp2, co2 = await _wire(tmp)
        att = _attempts(tmp)[0]["attempt_id"]
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:
            con.execute("UPDATE execution_attempts SET status=? WHERE attempt_id=?",
                        (X.EXTERNAL_PENDING, att))
        finally:
            con.close()
        out = await st2.finish_execution_attempt(
            attempt_id=att, status=X.ABANDONED, detail="stale", request_state=S.FAILED,
            expected_status=X.CLAIMED)
        require(out.startswith("expected_status_mismatch"),
                f"a stale expected status was accepted: {out}")
        require_equal([a["status"] for a in _attempts(tmp)], [X.EXTERNAL_PENDING], _attempts(tmp))
        # and the same call with the truthful expectation still works
        out2 = await st2.finish_execution_attempt(
            attempt_id=att, status=X.UNKNOWN, detail="honest", request_state=None,
            expected_status=X.EXTERNAL_PENDING)
        require_equal(out2, "ok", out2)
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_f1_t2_two_concurrent_startup_scans_produce_one_mutating_owner():
    for rnd in range(5):
        tmp = os.path.realpath(tempfile.mkdtemp())
        try:
            st, cp, co, ctx, aid = await _approved(tmp)
            await st.close()
            await _crashed_claimed(tmp)
            barrier = threading.Barrier(2)
            out = [None, None]

            def worker(k):
                async def go():
                    st2, cp2, co2 = await _wire(tmp)
                    try:
                        barrier.wait()
                        return await co2.startup_recovery_scan()
                    finally:
                        await st2.close()
                try:
                    out[k] = asyncio.run(go())
                except BaseException as exc:            # noqa: BLE001
                    out[k] = {"EXC": f"{type(exc).__name__}: {exc}"}

            threads = [threading.Thread(target=worker, args=(k,)) for k in (0, 1)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=90)
            require(not any(t.is_alive() for t in threads), "a startup scan deadlocked")
            require(not any("EXC" in (o or {}) for o in out), out)
            closed = sum(o["closed_no_effect"] for o in out)
            require_equal(closed, 1, f"round {rnd}: {closed} scans closed the same attempt")
            require_equal([a["status"] for a in _attempts(tmp)], [X.ABANDONED], _attempts(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


async def t_f1_t3_repeated_restart_never_retries_a_non_idempotent_unknown():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "non_idempotent")
        await st.close()
        svc = await _crashed_pending(tmp, "non_idempotent")
        for restart in range(4):
            st2, cp2, co2 = await _wire(tmp)
            summary = await co2.startup_recovery_scan()
            await st2.close()
            require_equal(summary["closed_no_effect"], 0, f"restart {restart}: {summary}")
            require_equal(len(summary["needs_attention"]), 1, summary)
            require_equal(summary["needs_attention"][0]["decision"],
                          X.MANUAL_RECOVERY_REQUIRED, summary)
            require_equal(ES.make("non_idempotent", svc).count(), 1,
                          f"restart {restart} produced a second external effect")
            require_equal([a["status"] for a in _attempts(tmp)], [X.EXTERNAL_PENDING],
                          "startup resolved an ambiguity it cannot resolve")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# F2 — claim-time authority snapshot, and the recovery re-check bound to it
# =====================================================================
async def t_f2_the_claim_records_the_authority_it_won_under():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "idempotent")
        await st.close()
        await _crashed_pending(tmp, "idempotent")
        att = _attempts(tmp)[0]
        dev = _rows(tmp, "SELECT * FROM devices")[0]
        require_equal(att["claim_identity_bound"], 1, "the claim recorded no snapshot")
        require_equal(att["claim_enrollment_id"], dev["current_enrollment_id"], att)
        require_equal(att["claim_app_attest_key_id"], dev["app_attest_key_id"], att)
        require_equal(att["claim_principal"], "local-owner", att)
        require_equal(att["claim_environment"], dev["environment"], att)
        require(att["claim_core_instance_id"], "no core instance recorded")
        require(att["claim_approval_key_sha256"], "no approval key recorded")
        from solvio.security.mobile_approval import crypto as _crypto
        require_equal(att["claim_approval_key_sha256"],
                      _crypto.fingerprint(bytes.fromhex(dev["public_key_x963"])),
                      "the recorded approval key is not the device's")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_f2_t4_a_re_enrolled_device_cannot_retry_an_old_claim():
    """T4 — a REAL re-enrolment: same device_id, new approval key, new App-Attest key,
    new enrollment generation. Every 'is it healthy today' check passes."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "idempotent")
        await st.close()
        svc = await _crashed_pending(tmp, "idempotent")
        before = _rows(tmp, "SELECT * FROM devices")[0]
        st2, cp2, co2 = await _wire(tmp)
        ctx2 = await H.enroll_attested(cp2, device_id=ctx.device_id)   # real re-enrolment
        after = _rows(tmp, "SELECT * FROM devices")[0]
        require(after["current_enrollment_id"] != before["current_enrollment_id"],
                "setup: the re-enrolment did not change the generation")
        require(after["public_key_x963"] != before["public_key_x963"],
                "setup: the re-enrolment did not rotate the approval key")
        require_equal(after["status"], S.DEVICE_ACTIVE, "setup: device should be healthy")
        require_equal(after["attestation_status"], S.ATT_ATTESTED, "setup")
        out, status = await co2.recover_execution(aid, ES.adapter(ES.make("idempotent", svc)))
        require(status.startswith("retry_refused:claim_authority_changed"),
                f"a re-enrolled device retried an old claim: {status}")
        require_equal(ES.make("idempotent", svc).count(), 1, "a new external write happened")
        require_equal([a["status"] for a in _attempts(tmp)], [X.UNKNOWN],
                      "the attempt should stay ambiguous, not be declared closed")
        require("execution_rejected_claim_rebound" in _events(tmp), _events(tmp))
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_f2_every_snapshot_field_is_load_bearing():
    """Change one recorded identity at a time; each must refuse the retry by NAME."""
    cases = {
        "enrollment": ("UPDATE devices SET current_enrollment_id=?", ("enr-other",)),
        "approval_key": ("UPDATE execution_attempts SET claim_approval_key_sha256=?",
                         ("0" * 64,)),
        "app_attest_key": ("UPDATE execution_attempts SET claim_app_attest_key_id=?",
                           ("other-aakid",)),
        "principal": ("UPDATE execution_attempts SET claim_principal=?", ("somebody-else",)),
        "environment": ("UPDATE execution_attempts SET claim_environment=?", ("production",)),
        "core_instance": ("UPDATE execution_attempts SET claim_core_instance_id=?",
                          ("core-other",)),
    }
    for label, (stmt, args) in cases.items():
        tmp = os.path.realpath(tempfile.mkdtemp())
        try:
            st, cp, co, ctx, aid = await _approved(tmp, "idempotent")
            await st.close()
            svc = await _crashed_pending(tmp, "idempotent")
            con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
            try:
                con.execute(stmt, args)
            finally:
                con.close()
            st2, cp2, co2 = await _wire(tmp)
            out, status = await co2.recover_execution(
                aid, ES.adapter(ES.make("idempotent", svc)))
            require_equal(status, "retry_refused:claim_authority_changed:" + label,
                          f"{label}: {status}")
            require_equal(ES.make("idempotent", svc).count(), 1,
                          f"{label}: a new external write happened")
            await st2.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


async def t_f2_t5_a_legacy_attempt_without_a_snapshot_is_never_auto_retried():
    """T5 — rows claimed before this migration carry no trustworthy identity."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "idempotent")
        await st.close()
        svc = await _crashed_pending(tmp, "idempotent")
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:        # exactly what a pre-migration row looks like
            con.execute("UPDATE execution_attempts SET claim_identity_bound=0, "
                        "claim_core_instance_id=NULL, claim_principal=NULL, "
                        "claim_environment=NULL, claim_enrollment_id=NULL, "
                        "claim_approval_key_sha256=NULL, claim_app_attest_key_id=NULL")
        finally:
            con.close()
        st2, cp2, co2 = await _wire(tmp)
        out, status = await co2.recover_execution(aid, ES.adapter(ES.make("idempotent", svc)))
        require_equal(status, "retry_refused:claim_identity_unbound", status)
        require_equal(ES.make("idempotent", svc).count(), 1,
                      "a legacy attempt drove a new external write")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_f2_a_healthy_retry_still_works():
    """The binding must refuse the wrong cases, not every case."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "idempotent")
        await st.close()
        svc = await _crashed_pending(tmp, "idempotent")
        st2, cp2, co2 = await _wire(tmp)
        out, status = await co2.recover_execution(aid, ES.adapter(ES.make("idempotent", svc)))
        require_equal(status, "retried_succeeded", f"a healthy retry was refused: {status}")
        require_equal(ES.make("idempotent", svc).count(), 1, "the retry duplicated")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_f2_observation_after_re_enrolment_is_still_allowed():
    """C — reconcile is not a write. An operator must still learn what happened."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "reconcilable")
        await st.close()
        svc = await _crashed_pending(tmp, "reconcilable")
        st2, cp2, co2 = await _wire(tmp)
        await H.enroll_attested(cp2, device_id=ctx.device_id)      # authority replaced
        service = ES.make("reconcilable", svc)
        out, status = await co2.recover_execution(
            aid, ES.adapter(service), reconciler=ES.reconciler(service))
        require_equal(status, "reconciled_succeeded",
                      f"observing an existing effect was refused: {status}")
        require_equal(ES.make("reconcilable", svc).count(), 1, "reconcile wrote something")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_f2_migration_is_versioned_and_idempotent():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        await st.close()
        ver = _rows(tmp, "SELECT value FROM schema_meta WHERE key='schema_version'")
        require_equal(int(ver[0]["value"]), S.ApprovalControlStore.SCHEMA_VERSION, ver)
        before = len([e for e in _events(tmp) if e == "schema_migrated"])
        for _ in range(3):                       # re-open must not migrate again
            st2 = S.ApprovalControlStore(os.path.join(tmp, DB))
            await st2.open()
            await st2.close()
        require_equal(len([e for e in _events(tmp) if e == "schema_migrated"]), before,
                      "the migration ran more than once")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# F3 — EXECUTING only through the journalled claim
# =====================================================================
async def t_f3_t6_the_generic_transition_cannot_enter_executing():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"],
                      S.APPROVED, "setup")
        try:
            await st.transition(aid, S.EXECUTING)
            require(False, "store.transition drove APPROVED -> EXECUTING outside the journal")
        except S.ExecutionBypassError:
            pass
        require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"],
                      S.APPROVED, "the state moved anyway")
        require_equal(len(_attempts(tmp)), 0, "an attempt appeared from nowhere")
        # the journalled path still works and DOES leave a row
        reason = await cp.claim_execution(aid, ctx.device_id)
        require(reason is None, f"the journalled claim refused: {reason}")
        require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"],
                      S.EXECUTING, "the journalled claim did not move the state")
        require_equal(len(_attempts(tmp)), 1, "EXECUTING without a journal entry")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_f3_only_the_journalled_claim_writes_executing():
    """AST: exactly one function can PRODUCE EXECUTING.

    A function that merely compares against EXECUTING is a guard, not a writer — counting
    it would make this test complain about the very defence it is supposed to protect. So
    every use of the name is classified: inside a comparison it is a check, anywhere else
    it is a value the function can write.
    """
    src = open(STORE_SRC, encoding="utf-8").read()
    tree = _ast.parse(src)
    writers, guards = [], []
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        uses = [n for n in _ast.walk(node)
                if isinstance(n, _ast.Name) and n.id == "EXECUTING"]
        if not uses:
            continue
        compared = set()
        for cmp_node in _ast.walk(node):
            if isinstance(cmp_node, _ast.Compare):
                for n in _ast.walk(cmp_node):
                    if isinstance(n, _ast.Name) and n.id == "EXECUTING":
                        compared.add(id(n))
        (guards if all(id(u) in compared for u in uses) else writers).append(node.name)
    require_equal(sorted(writers), ["_claim_execution"],
                  f"a second function can produce EXECUTING: {sorted(writers)}")
    require("_cas_transition" in guards,
            f"the generic transition no longer guards EXECUTING (guards={sorted(guards)})")


# =====================================================================
# F4 — the realtime tool-call loop is pinned to the LLM-safe dispatcher
# =====================================================================
def _realtime_toolcall_dispatches():
    """Every dispatcher call reachable from the realtime tool-call entry point."""
    src = open(CORE_SERVER, encoding="utf-8").read()
    tree = _ast.parse(src)
    funcs = {n.name: n for n in _ast.walk(tree)
             if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
    require("_handle_tool_calls" in funcs,
            "the realtime tool-call entry point was renamed — re-pin this guard")
    seen, stack, found = set(), ["_handle_tool_calls"], []
    while stack:
        name = stack.pop()
        if name in seen or name not in funcs:
            continue
        seen.add(name)
        for call in _ast.walk(funcs[name]):
            if isinstance(call, _ast.Call) and isinstance(call.func, _ast.Attribute):
                attr = call.func.attr
                if "dispatch" in attr:
                    found.append(attr)
                elif attr in funcs:
                    stack.append(attr)
    return found


async def t_f4_t7_the_realtime_loop_uses_only_the_llm_safe_dispatch():
    found = _realtime_toolcall_dispatches()
    require(found, "the realtime tool-call loop dispatches nothing — guard is vacuous")
    require_equal(sorted(set(found)), ["dispatch"],
                  f"the realtime tool-call path reaches {sorted(set(found))}")
    for banned in ("dispatch_trusted", "_dispatch"):
        require(banned not in found,
                f"the model-facing loop can reach {banned}")


async def t_f4_the_trusted_channel_has_no_realtime_caller():
    """dispatch_trusted may exist; it may not be reachable from anything the model drives."""
    realtime_dir = os.path.join(SRC, "solvio", "realtime")
    hits = []
    for base, _dirs, files in os.walk(realtime_dir):
        if "__pycache__" in base:
            continue
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(base, fname)
            for node in _ast.walk(_ast.parse(open(path, encoding="utf-8").read())):
                if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute) \
                        and node.func.attr in ("dispatch_trusted", "_dispatch"):
                    hits.append(f"{fname}::{node.func.attr}")
    require_equal(hits, [], f"the realtime package reaches a trusted dispatch route: {hits}")


async def t_f4_the_llm_dispatch_still_refuses_a_hidden_tool():
    """Behavioural companion: the pin is worthless if dispatch() stopped enforcing."""
    from _approval_fixture_tool import (CONFIRM_TOOL, FixtureConfirmTool,
                                        FixtureExecutor)
    from solvio.tools.dispatcher import ToolDispatcher

    d = ToolDispatcher()
    d.register(FixtureConfirmTool(FixtureExecutor(), ApprovalBroker()))
    res = await d.dispatch(CONFIRM_TOOL, {"request_id": "anything"})
    require(not res.get("success"), f"{CONFIRM_TOOL} ran through the LLM dispatch")
    require_equal(res.get("error"), f"not_exposed_to_llm:{CONFIRM_TOOL}", res)


# =====================================================================
# F5 — the named test gaps
# =====================================================================
async def t_f5_recovery_recheck_covers_every_condition_it_claims():
    """The hygiene test walked 6 conditions under a name promising all of them."""
    cases = {
        "device_revoked": ("UPDATE devices SET status=?", (S.DEVICE_REVOKED,)),
        "device_not_attested": ("UPDATE devices SET attestation_status=?", (S.ATT_FAILED,)),
        "no_current_enrollment": ("UPDATE devices SET current_enrollment_id=NULL", ()),
        "principal_mismatch": ("UPDATE devices SET principal=?", ("somebody-else",)),
        "device_environment_not_allowed": ("UPDATE devices SET environment=?",
                                           ("production",)),
        "request_expired": ("UPDATE approval_requests SET expires_at=?", (time.time() - 1,)),
        "device_binding_mismatch": ("UPDATE approval_requests SET decided_device=?",
                                    ("dev-someone-else",)),
        "request_not_executable": ("UPDATE approval_requests SET state=?", (S.CONSUMED,)),
        "device_key_unreadable": ("UPDATE devices SET public_key_x963=?", ("zz",)),
    }
    for expected, (stmt, args) in cases.items():
        tmp = os.path.realpath(tempfile.mkdtemp())
        try:
            st, cp, co, ctx, aid = await _approved(tmp, "idempotent")
            await st.close()
            svc = await _crashed_pending(tmp, "idempotent")
            con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
            try:
                con.execute(stmt, args)
            finally:
                con.close()
            st2, cp2, co2 = await _wire(tmp)
            out, status = await co2.recover_execution(
                aid, ES.adapter(ES.make("idempotent", svc)))
            require(status.startswith("retry_refused:" + expected),
                    f"{expected}: got {status}")
            require_equal(ES.make("idempotent", svc).count(), 1,
                          f"{expected}: a new external write happened")
            await st2.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


async def t_f5_the_recheck_pins_the_core_instance():
    """`wrong_core_instance` cannot be reached through `recover_execution` — that lookup is
    itself keyed on the execution id, so a foreign core simply finds no attempt. It is
    defence in depth on the store contract, so it is tested where it actually lives."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "idempotent")
        await st.close()
        await _crashed_pending(tmp, "idempotent")
        att = _attempts(tmp)[0]["attempt_id"]
        st2, cp2, co2 = await _wire(tmp)
        foreign = await st2.recheck_execution_authority(
            attempt_id=att, allowed_environments={"development"},
            core_instance_id="core-" + "0" * 32)
        require_equal(foreign, "wrong_core_instance", foreign)
        # the honest control: this core still passes
        mine = await st2.recheck_execution_authority(
            attempt_id=att, allowed_environments={"development"},
            core_instance_id=cp2.core_instance_id)
        require_equal(mine, "ok", mine)
        require_equal([a["status"] for a in _attempts(tmp)], [X.EXTERNAL_PENDING],
                      "the re-check mutated the attempt")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_f5_a_recovery_crash_child_really_dies_by_signal():
    """The hygiene recovery-crash test never asserted the child's return code."""
    import test_p1c_execution_recovery as P1C
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "idempotent")
        await st.close()
        svc = ES.service_path(tmp)
        ES.make("idempotent", svc)
        proc, _ = P1C._child(tmp, "post_effect", "idempotent", svc)
        require_equal(proc.returncode, -9, f"setup child: rc={proc.returncode}")
        _expire_leases(tmp)
        proc2, _ = P1C._child(tmp, "post_effect", "idempotent", svc, "", "recover")
        require_equal(proc2.returncode, -9,
                      f"the RECOVERY child did not die by SIGKILL: rc={proc2.returncode}\n"
                      f"{proc2.stdout[-300:]}{proc2.stderr[-300:]}")
        require_equal(ES.make("idempotent", svc).count(), 1,
                      "the crash during recovery duplicated the effect")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_f5_challenge_concurrency_proves_positive_work():
    """The hygiene race test never asserted that either worker actually issued."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co = await _wire(tmp)
        ctx = await H.enroll_attested(cp)
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws, exist_ok=True)
        aid = await cp.create_request(principal="local-owner", tool="t_non_idempotent",
                                      mode="modify", task="edit README", workspace=ws,
                                      human_summary="x")
        wire, status = await cp.issue_challenge(approval_id=aid, device_id=ctx.device_id)
        require_equal(status, "ok", status)
        first = _rows(tmp, "SELECT challenge_nonce FROM challenges")[0]["challenge_nonce"]
        await st.close()
        barrier = threading.Barrier(2)
        out = [None, None]

        def worker(k):
            async def go():
                st2, cp2, _ = await _wire(tmp)
                try:
                    barrier.wait()
                    return await cp2.issue_challenge(approval_id=aid,
                                                     device_id=ctx.device_id)
                finally:
                    await st2.close()
            try:
                out[k] = asyncio.run(go())
            except BaseException as exc:                # noqa: BLE001
                out[k] = ("EXC", f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(k,)) for k in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        require(not any(t.is_alive() for t in threads), "an issuance worker deadlocked")
        issued = [o for o in out if isinstance(o, tuple) and o[1] == "ok"]
        require(issued, f"NEITHER worker issued a challenge — the race never ran: {out}")
        rows = _rows(tmp, "SELECT * FROM challenges WHERE consumed=0")
        require_equal(len(rows), 1, f"{len(rows)} open challenges — {rows}")
        require(rows[0]["challenge_nonce"] != first,
                "the surviving challenge is the pre-race one — no worker did any work")
        require_equal(len(_rows(tmp, "SELECT * FROM challenges")), 3,
                      "the two issuances did not both leave a row")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# Non-regression: no product capability, production posture unchanged
# =====================================================================
async def t_no_production_capability_became_retryable():
    production = {k: v for k, v in X.CAPABILITY_SEMANTICS.items() if not k.startswith("t_")}
    require(production, "the capability registry is empty")
    for name, sem in production.items():
        require_equal(sem, X.NON_IDEMPOTENT_WRITE,
                      f"production capability {name} was reclassified to {sem}")
    src = open(os.path.join(SRC, "solvio", "security", "mobile_approval", "execution.py"),
               encoding="utf-8").read()
    shipped = {}
    for node in _ast.walk(_ast.parse(src)):
        if isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name) \
                and node.target.id == "CAPABILITY_SEMANTICS":
            for k, v in zip(node.value.keys, node.value.values):
                shipped[k.value] = v.id if isinstance(v, _ast.Name) else v.value
    require(shipped, "the shipped registry could not be read")
    for name, sem in shipped.items():
        require_equal(sem, "NON_IDEMPOTENT_WRITE",
                      f"the shipped registry declares {name} as {sem}")


async def t_production_runtime_is_still_fail_closed():
    """Non-regression for the released Production App Attest posture. No enrolment."""
    from solvio.security.mobile_approval import app_attest as AA
    require_equal(tuple(AA.allowed_environments_for_mode("production")),
                  (AA.ENV_PRODUCTION,), "production runtime widened")
    for bad in ("", "prod", "Development ", "both"):
        try:
            AA.allowed_environments_for_mode(bad)
            require(False, f"runtime mode {bad!r} was accepted")
        except AA.RuntimeModeError:
            pass
    gw = open(GATEWAY_RUNNER, encoding="utf-8").read()
    require('if getattr(verifier, "is_fake", False):' in gw and "raise SystemExit" in gw,
            "the gateway lost its fake-verifier guard")
    require("RuntimeModeError(" in gw, "the gateway lost its explicit runtime mode demand")
    require(getattr(AA.FakeAppAttestVerifier(), "is_fake", False),
            "the fake verifier is no longer flagged")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
