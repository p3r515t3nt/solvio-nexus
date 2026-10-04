"""P1C/F4 — execution crash recovery and idempotency.

MEASURED AGAINST THE RELEASED P1B TREE (8353b1f), with real child processes and real
SIGKILLs, before any of this was built:

    A  crash before the claim            state=APPROVED,  0 effects, a later claim works
    B  crash after claim, before effect  state=EXECUTING, 0 effects
    C  effect succeeded, crash           state=EXECUTING, 1 effect

B and C produced a BYTE-IDENTICAL database state. SOLVIO could not tell "definitely did not
happen" from "may have happened", so after a restart it could only refuse forever. And a
fifth window was worse than refusing: an adapter that completed its side effect and then
raised was recorded as `FAILED` — a claim about the world nobody had established.

WHAT IS CLAIMED. Not exactly-once for arbitrary external effects; that is not achievable
across the gap between "the adapter was called" and "its answer was stored". What is claimed
is that the gap is RECORDED before it is entered, and that nothing is retried automatically
unless the capability's server-declared semantics make a retry provably safe.

OUT OF SCOPE: real Computer Agent, Gmail/Calendar/file actions, S2B, distributed execution,
background job scheduling.

ASSERTION POLICY: `require*` from `tests/_guard.py` are function calls and survive `-O`.

Direct: python tests/test_p1c_execution_recovery.py
"""
import asyncio
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

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
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
STORE_SRC = os.path.join(REPO, "src", "solvio", "security", "mobile_approval", "store.py")
BRIDGE_SRC = os.path.join(REPO, "src", "solvio", "security", "mobile_approval", "bridge.py")
ROUNDS = int(os.environ.get("SOLVIO_RACE_ROUNDS", "100"))

# P1C/§2: capabilities declare their semantics SERVER-SIDE. These test capabilities are
# registered at exactly the same place a real one would be — in code, never in a request —
# which is the point being demonstrated: nothing the model sends can set this.
X.CAPABILITY_SEMANTICS.setdefault("t_idempotent", X.IDEMPOTENT_WRITE)
X.CAPABILITY_SEMANTICS.setdefault("t_reconcilable", X.RECONCILABLE_WRITE)
X.CAPABILITY_SEMANTICS.setdefault("t_non_idempotent", X.NON_IDEMPOTENT_WRITE)
KIND_TO_TOOL = {"idempotent": "t_idempotent", "reconcilable": "t_reconcilable",
                "non_idempotent": "t_non_idempotent"}
# A crashed owner never releases its lease, so the tests use a short one rather than waiting
# out the production default. Ownership only — it never changes what recovery is ALLOWED to do.
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
    """The real production path: request -> verified iPhone APPROVE -> S1 confirmation."""
    st, cp, co = await _wire(tmp)
    ctx = await H.enroll_attested(cp)
    ws = os.path.join(tmp, "ws")
    os.makedirs(ws, exist_ok=True)
    aid = await cp.create_request(principal="local-owner", tool=KIND_TO_TOOL[kind],
                                  mode="modify", task="edit README", workspace=ws,
                                  human_summary="Codex will edit README")
    wire, _ = await cp.issue_challenge(approval_id=aid, device_id=ctx.device_id)
    res, status = await co.apply_mobile_decision(
        **H.sign_decision(ctx, P.b64d(wire["payload_b64"])))
    require_equal(status, "ok", f"setup decision: {status}")
    with open(os.path.join(tmp, "aid.txt"), "w", encoding="utf-8") as fh:
        fh.write(aid)
    return st, cp, co, ctx, aid


async def _reconfirm(co, st, aid):
    """Re-arm the one-shot S1 confirmation, as apply_mobile_decision does in production."""
    req = await st.get_request(aid)
    co.approver.confirm(aid, req["principal"], req["action_digest"])


def _rows(tmp, sql, args=()):
    con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _expire_leases(tmp):
    """A crashed owner never releases its lease, so recovery waits for it to expire.

    The tests do not sleep out that wait — they expire it explicitly. What a lease decides is
    OWNERSHIP; `t_a_lease_settles_ownership_not_whether_the_effect_happened` covers the part
    that actually matters, namely that expiry does NOT make an ambiguous effect retryable.
    """
    con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
    try:
        con.execute("UPDATE execution_attempts SET lease_expires_at=?", (time.time() - 1,))
    finally:
        con.close()


def _attempts(tmp):
    return _rows(tmp, "SELECT * FROM execution_attempts ORDER BY claimed_at")


def _events(tmp):
    return [r["event"] for r in _rows(tmp, "SELECT event FROM audit ORDER BY id")]


# ---------------------------------------------------------------------------
# The crash child: a real process, killed at a named point
# ---------------------------------------------------------------------------
CHILD = '''
import asyncio, os, signal, sys
REPO = %(repo)r
sys.path.insert(0, os.path.join(REPO, "src"))
sys.path.insert(0, os.path.join(REPO, "tests"))
import execution_test_services as ES
import mobile_attest_helper as H
from solvio.security.approval import ApprovalBroker
from solvio.security.mobile_approval import bridge as B, control as C, execution as X
from solvio.security.mobile_approval import identity, store as S

TMP, WHERE, KIND, SVC = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
FAIL = sys.argv[5] if len(sys.argv) > 5 else ""
MODE = sys.argv[6] if len(sys.argv) > 6 else "execute"

def die():
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)

async def go():
    st = S.ApprovalControlStore(os.path.join(TMP, "approval_control.sqlite3"))
    await st.open()
    cp = C.MobileApprovalControlPlane(
        st, identity.MacSigningKey.load_or_create(TMP),
        identity.load_or_create_core_instance_id(TMP),
        attest_verifier=H.fake_verifier(), app_id="APP",
        allowed_environments={"development"})
    approver = B.MobileApprover()
    co = B.MobileApprovalCoordinator(cp, ApprovalBroker(approver=approver), approver)
    # A separate SOLVIO process must be CONFIGURED like one: the capability declarations and
    # the lease length are server-side settings, not something a child inherits for free.
    X.CAPABILITY_SEMANTICS.setdefault("t_idempotent", X.IDEMPOTENT_WRITE)
    X.CAPABILITY_SEMANTICS.setdefault("t_reconcilable", X.RECONCILABLE_WRITE)
    X.CAPABILITY_SEMANTICS.setdefault("t_non_idempotent", X.NON_IDEMPOTENT_WRITE)
    co.claim_lease_seconds = 0.2
    co.recovery_lease_seconds = 5.0
    aid = open(os.path.join(TMP, "aid.txt")).read().strip()
    req = await st.get_request(aid)
    approver.confirm(aid, req["principal"], req["action_digest"])
    service = ES.make(KIND, SVC)

    if WHERE == "pre_claim":
        async def hook(*a, **k):
            die()
        st.claim_execution_attempt = hook
    elif WHERE == "post_claim":
        async def hook(*a, **k):
            die()                     # attempt is CLAIMED; the boundary is not crossed
        st.begin_external_execution = hook

    base = ES.adapter(service, fail=(FAIL or None))
    async def ex(action):
        if WHERE == "post_boundary":
            die()                     # boundary committed, adapter never reached
        out = await base(action)
        if WHERE == "post_effect":
            die()                     # the effect happened; the outcome is never stored
        return out

    if MODE == "recover":
        r = await co.recover_execution(aid, ex, reconciler=ES.reconciler(service))
    else:
        r = await co.execute_approved(aid, ex)
    if WHERE == "post_success":
        # HYGIENE/H7B: die durable Success-Transaktion ist committet; erst JETZT sterben.
        if r[1] != "ok":
            print("CHILD_RESULT " + str(r[1]))
            sys.stdout.flush()
            return
        die()
    print("CHILD_RESULT " + str(r[1]))
    sys.stdout.flush()
    await st.close()

asyncio.run(go())
'''


def _child(tmp, where, kind, svc, fail="", mode="execute"):
    script = os.path.join(tmp, "child.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(CHILD % {"repo": REPO})
    proc = subprocess.run([sys.executable, script, tmp, where, kind, svc, fail, mode],
                          capture_output=True, text=True, timeout=180)
    status = ""
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CHILD_RESULT "):
            status = line.split(" ", 1)[1]
    return proc, status


# =====================================================================
# Phase 2/3 — semantics and identity
# =====================================================================
async def t_semantics_are_declared_server_side_only():
    require_equal(X.semantics_for("codex_task"), X.NON_IDEMPOTENT_WRITE, "codex_task")
    require_equal(X.semantics_for("a_capability_nobody_classified"), X.NON_IDEMPOTENT_WRITE,
                  "an unclassified capability must get the FAIL-SAFE default")
    require_equal(X.semantics_for(""), X.NON_IDEMPOTENT_WRITE, "empty capability")
    require_equal(X.semantics_for(None), X.NON_IDEMPOTENT_WRITE, "missing capability")
    # The model must have no way to assert this: the semantics are not a request field.
    src = open(STORE_SRC, encoding="utf-8").read()
    require("semantics" in src, "the journal does not record semantics")
    ctl = open(os.path.join(REPO, "src", "solvio", "security", "mobile_approval",
                            "control.py"), encoding="utf-8").read()
    require("_X.semantics_for(" in ctl, "semantics are not resolved server-side")
    for field in ("semantics", "idempotency_key", "execution_id"):
        require(f'"{field}"' not in ctl.split("async def create_request")[-1][:2000]
                or "create_request" not in ctl,
                f"{field} looks reachable through create_request")


async def t_execution_identity_is_stable_and_unique():
    a = X.execution_id_for("core-1", "apr-1")
    require_equal(a, X.execution_id_for("core-1", "apr-1"), "not stable across calls")
    require(a != X.execution_id_for("core-1", "apr-2"), "two approvals share an identity")
    require(a != X.execution_id_for("core-2", "apr-1"), "two cores share an identity")
    key = X.idempotency_key_for(a, "codex_task")
    require_equal(key, X.idempotency_key_for(a, "codex_task"), "key not stable")
    require(key != X.idempotency_key_for(a, "other"), "key ignores the capability")
    for bad in (("", "apr"), ("core", "")):
        try:
            X.execution_id_for(*bad)
            require(False, f"{bad} was accepted")
        except ValueError:
            pass


async def t_execution_identity_survives_a_restart():
    """The identity is recomputed, not remembered — so a restart cannot mint a new one."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        first = X.execution_id_for(cp.core_instance_id, aid)
        await st.close()
        st2, cp2, co2 = await _wire(tmp)
        second = X.execution_id_for(cp2.core_instance_id, aid)
        require_equal(second, first, "the execution identity changed across a restart")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_the_execute_surface_exposes_no_identity_to_the_caller():
    params = set(inspect.signature(B.MobileApprovalCoordinator.execute_approved).parameters)
    require_equal(params, {"self", "approval_id", "executor"},
                  f"execute_approved grew a spoofable parameter: {sorted(params)}")
    src = open(BRIDGE_SRC, encoding="utf-8").read()
    require("X.execution_id_for(self.cp.core_instance_id" in src,
            "the execution identity is not derived from this core")


# =====================================================================
# Phase 4 — atomic claim, one owner
# =====================================================================
async def t_concurrent_claim_has_exactly_one_winner():
    for i in range(ROUNDS):
        tmp = os.path.realpath(tempfile.mkdtemp())
        try:
            st, cp, co, ctx, aid = await _approved(tmp)
            await st.close()
            svc = ES.service_path(tmp)
            ES.make("non_idempotent", svc)
            barrier = threading.Barrier(2)
            out = [None, None]

            def worker(k):
                async def go():
                    st2, cp2, co2 = await _wire(tmp)
                    try:
                        await _reconfirm(co2, st2, aid)
                        service = ES.make("non_idempotent", svc)
                        barrier.wait()
                        return await co2.execute_approved(aid, ES.adapter(service))
                    finally:
                        await st2.close()
                try:
                    out[k] = asyncio.run(go())
                except BaseException as exc:      # noqa: BLE001
                    out[k] = ("EXC", f"{type(exc).__name__}: {exc}")
            threads = [threading.Thread(target=worker, args=(k,)) for k in (0, 1)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=120)
            require(not any(t.is_alive() for t in threads), "a claim worker deadlocked")
            statuses = [o[1] for o in out]
            require_equal(statuses.count("ok"), 1, f"round {i}: {statuses}")
            require_equal(ES.make("non_idempotent", svc).count(), 1,
                          f"round {i}: {ES.make('non_idempotent', svc).count()} effects")
            require_equal(len([a for a in _attempts(tmp) if a["status"] == X.SUCCEEDED]), 1,
                          f"round {i}: attempts {[a['status'] for a in _attempts(tmp)]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


async def t_the_claim_and_the_journal_entry_commit_together():
    src = open(STORE_SRC, encoding="utf-8").read()
    start = src.index("def _claim_execution_attempt(")
    body = src[start:src.index("\n    async def ", start + 10)]
    require_equal(body.count('BEGIN IMMEDIATE'), 1, "the claim is not one transaction")
    for needed in ("_claim_execution(", "INSERT INTO execution_attempts"):
        require(needed in body, f"{needed} is outside the claim transaction")


# =====================================================================
# Phase 7/19 — the crash matrix, real processes
# =====================================================================
async def _crash_case(where, kind, *, fail="", expect_status, expect_effects,
                      expect_attempt):
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, kind)
        await st.close()
        svc = ES.service_path(tmp)
        ES.make(kind, svc)
        proc, status = _child(tmp, where, kind, svc, fail)
        service = ES.make(kind, svc)
        attempts = [a["status"] for a in _attempts(tmp)]
        require_equal(attempts, expect_attempt,
                      f"{where}: attempts {attempts}\n{proc.stdout[-400:]}{proc.stderr[-400:]}")
        require_equal(service.count(), expect_effects, f"{where}: {service.count()} effects")
        if expect_status is not None:
            require_equal(status, expect_status, f"{where}: child said {status!r}")
        return tmp, aid, svc, service
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


async def t_c1_crash_before_the_claim_leaves_nothing_claimed():
    tmp, aid, svc, service = await _crash_case(
        "pre_claim", "non_idempotent", expect_status="", expect_effects=0, expect_attempt=[])
    try:
        require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"],
                      S.APPROVED, "the request left APPROVED")
        # a later legitimate execution still works
        st, cp, co = await _wire(tmp)
        await _reconfirm(co, st, aid)
        res, status = await co.execute_approved(aid, ES.adapter(ES.make("non_idempotent", svc)))
        require_equal(status, "ok", f"the later legitimate claim failed: {status}")
        require_equal(ES.make("non_idempotent", svc).count(), 1, "effects after retry")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_c2_crash_after_claim_records_that_nothing_started():
    tmp, aid, svc, service = await _crash_case(
        "post_claim", "non_idempotent", expect_status="", expect_effects=0,
        expect_attempt=[X.CLAIMED])
    try:
        require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"],
                      S.EXECUTING, "the request is not EXECUTING")
        # CLAIMED is knowledge: the adapter was never called.
        require_equal(X.recovery_decision(X.CLAIMED, X.NON_IDEMPOTENT_WRITE),
                      X.CLOSED_NO_EFFECT, "CLAIMED must be closable without ambiguity")
        _expire_leases(tmp)
        st, cp, co = await _wire(tmp)
        out, status = await co.recover_execution(aid)
        require_equal(status, "closed_no_effect", f"recovery said {status}")
        require_equal([a["status"] for a in _attempts(tmp)], [X.ABANDONED], _attempts(tmp))
        require_equal(ES.make("non_idempotent", svc).count(), 0, "recovery caused an effect")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_c3_crash_after_the_boundary_is_treated_as_may_have_happened():
    """The adapter was never reached — and the record still says MAY. That is the point."""
    tmp, aid, svc, service = await _crash_case(
        "post_boundary", "non_idempotent", expect_status="", expect_effects=0,
        expect_attempt=[X.EXTERNAL_PENDING])
    try:
        _expire_leases(tmp)
        st, cp, co = await _wire(tmp)
        out, status = await co.recover_execution(aid)
        require_equal(status, "manual_recovery_required",
                      f"a non-idempotent ambiguous outcome was auto-resolved: {status}")
        require_equal(ES.make("non_idempotent", svc).count(), 0, "recovery caused an effect")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_c4_non_idempotent_post_effect_crash_is_never_auto_retried():
    """THE ambiguous-outcome case. One effect happened; a retry would make it two."""
    tmp, aid, svc, service = await _crash_case(
        "post_effect", "non_idempotent", expect_status="", expect_effects=1,
        expect_attempt=[X.EXTERNAL_PENDING])
    try:
        _expire_leases(tmp)
        st, cp, co = await _wire(tmp)
        out, status = await co.recover_execution(aid, ES.adapter(ES.make("non_idempotent", svc)))
        require_equal(status, "manual_recovery_required", f"recovery said {status}")
        require_equal(out["decision"], X.MANUAL_RECOVERY_REQUIRED, out)
        require_equal(ES.make("non_idempotent", svc).count(), 1,
                      "recovery produced a SECOND external effect")
        require_equal([a["status"] for a in _attempts(tmp)], [X.UNKNOWN], _attempts(tmp))
        # and it stays that way, however often recovery is attempted
        for _ in range(3):
            _, again = await co.recover_execution(aid, ES.adapter(ES.make("non_idempotent", svc)))
            require_equal(again, "manual_recovery_required", again)
        require_equal(ES.make("non_idempotent", svc).count(), 1, "repeated recovery duplicated")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_c4_idempotent_post_effect_crash_retries_with_the_same_key():
    tmp, aid, svc, service = await _crash_case(
        "post_effect", "idempotent", expect_status="", expect_effects=1,
        expect_attempt=[X.EXTERNAL_PENDING])
    try:
        before = ES.make("idempotent", svc).effects()
        _expire_leases(tmp)
        st, cp, co = await _wire(tmp)
        out, status = await co.recover_execution(aid, ES.adapter(ES.make("idempotent", svc)))
        require_equal(status, "retried_succeeded", f"recovery said {status}")
        after = ES.make("idempotent", svc).effects()
        require_equal(len(after), 1, f"the retry duplicated the effect: {after}")
        require_equal(after[0]["idempotency_key"], before[0]["idempotency_key"],
                      "the retry used a DIFFERENT idempotency key")
        require_equal([a["status"] for a in _attempts(tmp)], [X.SUCCEEDED], _attempts(tmp))
        require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"],
                      S.CONSUMED, "the request did not reach CONSUMED")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_c4_reconcilable_post_effect_crash_reconciles_instead_of_retrying():
    tmp, aid, svc, service = await _crash_case(
        "post_effect", "reconcilable", expect_status="", expect_effects=1,
        expect_attempt=[X.EXTERNAL_PENDING])
    try:
        _expire_leases(tmp)
        st, cp, co = await _wire(tmp)
        service2 = ES.make("reconcilable", svc)
        out, status = await co.recover_execution(
            aid, ES.adapter(service2), reconciler=ES.reconciler(service2))
        require_equal(status, "reconciled_succeeded", f"recovery said {status}")
        require_equal(out["reconciled"], True, out)
        require_equal(ES.make("reconcilable", svc).count(), 1,
                      "reconciliation caused a SECOND effect")
        require_equal([a["status"] for a in _attempts(tmp)], [X.SUCCEEDED], _attempts(tmp))
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_reconcilable_with_no_effect_is_closed_not_guessed():
    """Reconciliation that finds nothing must close the attempt, not silently retry."""
    tmp, aid, svc, service = await _crash_case(
        "post_boundary", "reconcilable", expect_status="", expect_effects=0,
        expect_attempt=[X.EXTERNAL_PENDING])
    try:
        _expire_leases(tmp)
        st, cp, co = await _wire(tmp)
        service2 = ES.make("reconcilable", svc)
        out, status = await co.recover_execution(
            aid, ES.adapter(service2), reconciler=ES.reconciler(service2))
        require_equal(status, "reconciled_not_executed", f"recovery said {status}")
        require_equal(out["reconciled"], False, out)
        require_equal(ES.make("reconcilable", svc).count(), 0, "reconciliation executed")
        require_equal([a["status"] for a in _attempts(tmp)], [X.ABANDONED], _attempts(tmp))
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_reconcilable_without_a_reconciler_refuses_to_guess():
    tmp, aid, svc, service = await _crash_case(
        "post_effect", "reconcilable", expect_status="", expect_effects=1,
        expect_attempt=[X.EXTERNAL_PENDING])
    try:
        _expire_leases(tmp)
        st, cp, co = await _wire(tmp)
        out, status = await co.recover_execution(aid, ES.adapter(ES.make("reconcilable", svc)))
        require_equal(status, "reconciler_required", f"recovery said {status}")
        require_equal(ES.make("reconcilable", svc).count(), 1, "it retried anyway")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_c5_a_safe_adapter_failure_is_recorded_as_safe():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        svc = ES.service_path(tmp)
        service = ES.make("non_idempotent", svc)
        res, status = await co.execute_approved(aid, ES.adapter(service, fail="safe"))
        require(res is None, "a safe failure reported success")
        require_equal(status, "failed_safe", status)
        require_equal([a["status"] for a in _attempts(tmp)], [X.FAILED_SAFE], _attempts(tmp))
        require_equal(service.count(), 0, "a 'safe' failure left an effect behind")
        require_equal(X.recovery_decision(X.FAILED_SAFE, X.NON_IDEMPOTENT_WRITE),
                      X.CLOSED_NO_EFFECT, "a safe failure should be closable")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_an_ambiguous_adapter_failure_is_never_recorded_as_failed():
    """The reproduced P1B defect: effect happened, adapter raised, store said FAILED."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        svc = ES.service_path(tmp)
        service = ES.make("non_idempotent", svc)
        res, status = await co.execute_approved(aid, ES.adapter(service, fail="ambiguous"))
        require(res is None, "an ambiguous outcome reported success")
        require_equal(status, "unknown_outcome", status)
        require_equal(service.count(), 1, "setup: the effect should have happened")
        require_equal([a["status"] for a in _attempts(tmp)], [X.UNKNOWN], _attempts(tmp))
        state = _rows(tmp, "SELECT state, error FROM approval_requests")[0]
        require(state["state"] != S.FAILED,
                "an effect that MAY have happened was recorded as FAILED")
        require_equal(state["state"], S.EXECUTING,
                      f"the request should stay in flight, not claim an outcome: {state}")
        require("execution_unknown" in _events(tmp), _events(tmp))
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_c6_a_committed_success_is_never_executed_again():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        svc = ES.service_path(tmp)
        service = ES.make("non_idempotent", svc)
        res, status = await co.execute_approved(aid, ES.adapter(service))
        require_equal(status, "ok", status)
        require_equal(service.count(), 1, "setup")
        await st.close()
        # restart, then try every door
        st2, cp2, co2 = await _wire(tmp)
        await _reconfirm(co2, st2, aid)
        _, again = await co2.execute_approved(aid, ES.adapter(ES.make("non_idempotent", svc)))
        require_equal(again, "not_approved", f"a second execute said {again}")
        out, rec = await co2.recover_execution(aid, ES.adapter(ES.make("non_idempotent", svc)))
        require_equal(rec, "already_succeeded", f"recovery after success said {rec}")
        require_equal(await cp2.claim_execution(aid, ctx.device_id), "already_succeeded",
                      "a direct claim after success was allowed")
        require_equal(ES.make("non_idempotent", svc).count(), 1,
                      "a restart produced a second external effect")
        require_equal(len([a for a in _attempts(tmp) if a["status"] == X.SUCCEEDED]), 1,
                      _attempts(tmp))
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_c7_success_survives_a_process_crash_immediately_after():
    """Crash right after the success commit: the restart must not re-execute."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        await st.close()
        svc = ES.service_path(tmp)
        ES.make("non_idempotent", svc)
        proc, status = _child(tmp, "none", "non_idempotent", svc)
        require_equal(status, "ok", f"{proc.stdout[-300:]}{proc.stderr[-300:]}")
        require_equal(ES.make("non_idempotent", svc).count(), 1, "setup")
        st2, cp2, co2 = await _wire(tmp)
        await _reconfirm(co2, st2, aid)
        _, again = await co2.execute_approved(aid, ES.adapter(ES.make("non_idempotent", svc)))
        require_equal(again, "not_approved", again)
        require_equal(ES.make("non_idempotent", svc).count(), 1, "restart duplicated")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_at_most_one_success_row_is_enforced_by_the_database():
    """Not a policy someone can forget: a partial unique index refuses the second row."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        svc = ES.service_path(tmp)
        require_equal((await co.execute_approved(aid, ES.adapter(ES.make("non_idempotent", svc))))[1],
                      "ok", "setup")
        row = _attempts(tmp)[0]
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:
            con.execute(
                "INSERT INTO execution_attempts (attempt_id, execution_id, approval_id, "
                "device_id, capability, semantics, idempotency_key, status, claimed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("att-forged", row["execution_id"], aid, row["device_id"], "codex_task",
                 X.NON_IDEMPOTENT_WRITE, "k", X.SUCCEEDED, time.time()))
            require(False, "a second SUCCEEDED row was accepted")
        except sqlite3.IntegrityError:
            pass
        finally:
            con.close()
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# Phase 12 — concurrent recovery
# =====================================================================
async def t_concurrent_recovery_has_at_most_one_owner():
    for i in range(ROUNDS):
        tmp = os.path.realpath(tempfile.mkdtemp())
        try:
            st, cp, co, ctx, aid = await _approved(tmp, "idempotent")
            await st.close()
            svc = ES.service_path(tmp)
            ES.make("idempotent", svc)
            _child(tmp, "post_effect", "idempotent", svc)
            require_equal([a["status"] for a in _attempts(tmp)], [X.EXTERNAL_PENDING],
                          f"round {i}: setup {_attempts(tmp)}")
            _expire_leases(tmp)
            barrier = threading.Barrier(2)
            out = [None, None]

            def worker(k):
                async def go():
                    st2, cp2, co2 = await _wire(tmp)
                    try:
                        service = ES.make("idempotent", svc)
                        barrier.wait()
                        return await co2.recover_execution(
                            aid, ES.adapter(service), owner=f"owner-{k}")
                    finally:
                        await st2.close()
                try:
                    out[k] = asyncio.run(go())
                except BaseException as exc:      # noqa: BLE001
                    out[k] = ("EXC", f"{type(exc).__name__}: {exc}")
            threads = [threading.Thread(target=worker, args=(k,)) for k in (0, 1)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=120)
            require(not any(t.is_alive() for t in threads), "a recovery worker deadlocked")
            statuses = sorted(o[1] for o in out)
            require(statuses.count("retried_succeeded") <= 1,
                    f"round {i}: two recovery owners succeeded — {statuses}")
            require("recovery_lease_held" in statuses or statuses.count("already_succeeded") == 1,
                    f"round {i}: the loser was not excluded — {statuses}")
            require_equal(ES.make("idempotent", svc).count(), 1,
                          f"round {i}: concurrent recovery duplicated the effect")
            require_equal(len([a for a in _attempts(tmp) if a["status"] == X.SUCCEEDED]), 1,
                          f"round {i}: {_attempts(tmp)}")
            require_equal(len([e for e in _events(tmp) if e == "execution_succeeded"]), 1,
                          f"round {i}: two success events")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


async def t_a_lease_settles_ownership_not_whether_the_effect_happened():
    """An expired lease must not turn UNKNOWN into 'safe to retry'."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        await st.close()
        svc = ES.service_path(tmp)
        ES.make("non_idempotent", svc)
        _child(tmp, "post_effect", "non_idempotent", svc)
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:                                        # expire every lease
            con.execute("UPDATE execution_attempts SET lease_expires_at=?", (time.time() - 1,))
        finally:
            con.close()
        st2, cp2, co2 = await _wire(tmp)
        out, status = await co2.recover_execution(
            aid, ES.adapter(ES.make("non_idempotent", svc)))
        require_equal(status, "manual_recovery_required",
                      f"an expired lease unlocked an ambiguous non-idempotent effect: {status}")
        require_equal(ES.make("non_idempotent", svc).count(), 1, "the effect was duplicated")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# Phase 10/14 — no new authority, revocation semantics
# =====================================================================
async def t_recovery_never_invents_authority():
    """Recovery works only on an approval that already exists and was already decided."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        out, status = await co.recover_execution("apr-does-not-exist")
        require(out is None, "recovery invented an execution for an unknown approval")
        require_equal(status, "no_open_attempt", status)
        require_equal(_attempts(tmp), [], "an attempt row was created out of nothing")
        src = open(BRIDGE_SRC, encoding="utf-8").read()
        rec = src[src.index("async def recover_execution"):]
        for forbidden in ("broker.approve(", "approver.confirm(", "submit_decision("):
            require(forbidden not in rec, f"recovery reaches {forbidden}")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_revocation_before_the_claim_blocks_execution():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        await cp.revoke_device(ctx.device_id, reason="stolen")
        svc = ES.service_path(tmp)
        service = ES.make("non_idempotent", svc)
        res, status = await co.execute_approved(aid, ES.adapter(service))
        require(res is None, "a revoked device executed")
        require_equal(status, "device_revoked", status)
        require_equal(service.count(), 0, "an external effect happened anyway")
        require_equal(_attempts(tmp), [], "an attempt was journalled for a revoked device")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_revocation_after_the_claim_stops_before_the_external_start():
    """Case B: the claim is valid, nothing external has begun — so nothing should begin."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        svc = ES.service_path(tmp)
        service = ES.make("non_idempotent", svc)
        st2, cp2, co2 = await _wire(tmp)             # an independent connection, like the CLI
        real = st.begin_external_execution
        fired = []

        async def interposed(**kw):
            if not fired:
                fired.append(1)
                await cp2.revoke_device(ctx.device_id, reason="operator")
            return await real(**kw)

        st.begin_external_execution = interposed
        res, status = await co.execute_approved(aid, ES.adapter(service))
        require(fired, "the racing revoke never ran — the probe is vacuous")
        require(res is None, "execution continued on revoked authority")
        require_equal(status, "device_revoked", status)
        require_equal(service.count(), 0, "an external effect happened after the revoke")
        require_equal([a["status"] for a in _attempts(tmp)], [X.ABANDONED], _attempts(tmp))
        require("execution_abandoned_revoked" in _events(tmp), _events(tmp))
        await st.close()
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_revocation_after_the_external_start_does_not_retry_or_undo():
    """Case C: the effect may already have happened. Revoking cannot unmake it."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        await st.close()
        svc = ES.service_path(tmp)
        ES.make("non_idempotent", svc)
        _child(tmp, "post_effect", "non_idempotent", svc)
        require_equal([a["status"] for a in _attempts(tmp)], [X.EXTERNAL_PENDING], "setup")
        _expire_leases(tmp)
        st2, cp2, co2 = await _wire(tmp)
        await cp2.revoke_device(ctx.device_id, reason="stolen")
        out, status = await co2.recover_execution(
            aid, ES.adapter(ES.make("non_idempotent", svc)))
        require_equal(status, "manual_recovery_required", status)
        require_equal(ES.make("non_idempotent", svc).count(), 1,
                      "revocation triggered a second effect")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# Phase 15/16 — audit consistency and no bypass writers
# =====================================================================
async def t_no_success_audit_without_a_committed_success():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        svc = ES.service_path(tmp)
        service = ES.make("non_idempotent", svc)
        real = st._cas_transition
        boom = RuntimeError("injected failure while committing the success")

        def exploding(*a, **kw):
            raise boom

        st._cas_transition = exploding
        try:
            await co.execute_approved(aid, ES.adapter(service))
            require(False, "the injected failure did not propagate")
        except RuntimeError as exc:
            require(exc is boom, f"wrong exception: {exc}")
        finally:
            st._cas_transition = real
        require("execution_succeeded" not in _events(tmp),
                f"a success audit survived a rolled-back success: {_events(tmp)}")
        require_equal(len([a for a in _attempts(tmp) if a["status"] == X.SUCCEEDED]), 0,
                      _attempts(tmp))
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_the_audit_tells_the_execution_story():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        svc = ES.service_path(tmp)
        require_equal((await co.execute_approved(
            aid, ES.adapter(ES.make("non_idempotent", svc))))[1], "ok", "setup")
        events = _events(tmp)
        for needed in ("execution_attempt_claimed", "execution_external_boundary",
                       "execution_succeeded", "state_" + S.EXECUTING, "state_" + S.CONSUMED):
            require(needed in events, f"{needed} missing from {events}")
        require_equal(len([e for e in events if e == "execution_succeeded"]), 1, events)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_no_writer_reaches_executing_without_the_journal():
    import re
    src = open(STORE_SRC, encoding="utf-8").read()
    hits = [m.start() for m in re.finditer(r"EXECUTING", src)]
    require(hits, "EXECUTING vanished from the store")
    # `_claim_execution` is the only statement that sets it, and its ONLY caller is the
    # journalled claim — so there is no path to EXECUTING without an attempt row.
    callers = re.findall(r"self\._claim_execution\(", src)
    require_equal(len(callers), 1,
                  f"{len(callers)} callers of the raw claim — expected only the journalled one")
    start = src.index("def _claim_execution_attempt(")
    require("self._claim_execution(" in src[start:src.index("\n    async def ", start + 10)],
            "the journalled claim no longer performs the claim")
    ctl = open(os.path.join(REPO, "src", "solvio", "security", "mobile_approval",
                            "control.py"), encoding="utf-8").read()
    require("self.store.claim_execution(" not in ctl,
            "the control plane still calls the un-journalled claim")
    br = open(BRIDGE_SRC, encoding="utf-8").read()
    require("claim_execution_attempt(" in br, "the coordinator bypasses the journal")


async def t_the_boundary_is_crossed_before_the_adapter_is_called():
    """Structural: nothing may call the executor before the boundary commits."""
    src = open(BRIDGE_SRC, encoding="utf-8").read()
    body = src[src.index("async def _run_attempt("):]
    body = body[:body.index("\n    async def ")]
    require(body.index("begin_external_execution") < body.index("await executor("),
            "the adapter is called before the durable boundary")
    require('status=X.UNKNOWN' in body, "an ambiguous outcome is not recorded as UNKNOWN")
    require("except Exception" in body and "X.SafeExecutionFailure" in body,
            "the safe/ambiguous distinction is gone")


# =====================================================================
# Phase 17 — migration
# =====================================================================
async def t_a_p1b_store_still_opens_and_gains_the_journal():
    """A database created before P1C keeps working; nothing historical is invented."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        script = os.path.join(tmp, "make_old.py")
        state = os.path.join(tmp, "state")
        os.makedirs(state, exist_ok=True)
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(
                "import asyncio, os, sys, time\n"
                f"sys.path.insert(0, {os.path.join(REPO, 'src')!r})\n"
                f"sys.path.insert(0, {os.path.join(REPO, 'tests')!r})\n"
                "import mobile_attest_helper as H\n"
                "from solvio.security.mobile_approval import control as C, identity\n"
                "from _public_source_fixture import load_module\n"
                "S = load_module('p1b_legacy/store.py', 'solvio.security.mobile_approval._public_p1b_store')\n"
                "async def go():\n"
                f"    st = S.ApprovalControlStore(os.path.join({state!r}, {DB!r}))\n"
                "    await st.open()\n"
                "    cp = C.MobileApprovalControlPlane(st,\n"
                f"        identity.MacSigningKey.load_or_create({state!r}),\n"
                f"        identity.load_or_create_core_instance_id({state!r}),\n"
                "        attest_verifier=H.fake_verifier(), app_id='APP',\n"
                "        allowed_environments={'development'})\n"
                "    await H.enroll_attested(cp)\n"
                "    await st.create_request(approval_id='old-1', principal='local-owner',\n"
                "        tool='codex_task', mode='modify', task='t', workspace='/w',\n"
                "        action_digest='d'*64, human_summary='s', expires_at=time.time()+600)\n"
                "    await st.transition('old-1', S.APPROVED, device_id='dev-1')\n"
                "    await st.close()\n"
                "    print('OLD_OK')\n"
                "asyncio.run(go())\n")
        out = subprocess.run([sys.executable, script], cwd=REPO, capture_output=True, text=True)
        require("OLD_OK" in out.stdout, f"could not build a P1B store: {out.stderr[-500:]}")

        st = S.ApprovalControlStore(os.path.join(state, DB))
        await st.open()
        req = await st.get_request("old-1")
        require_equal(req["state"], S.APPROVED, "the pre-P1C request did not survive")
        require_equal(await st.attempts_for(X.execution_id_for("c", "old-1")), [],
                      "migration invented an execution history that never existed")
        cols = {r["name"] for r in _rows(state, "PRAGMA table_info(execution_attempts)")}
        require({"execution_id", "semantics", "status"} <= cols, cols)
        await st.close()
        # read-only still works, and is still refused when a migration is pending
        ro = S.ApprovalControlStore(os.path.join(state, DB), read_only=True)
        await ro.open()
        require_equal(len(await ro.list_devices()), 1, "read-only lost the devices")
        await ro.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_a_legacy_executing_row_is_not_declared_safe():
    """An old EXECUTING row has no journal entry. Nobody may claim the effect did not run."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:                                    # exactly what a pre-P1C crash left behind
            con.execute("UPDATE approval_requests SET state=? WHERE approval_id=?",
                        (S.EXECUTING, aid))
        finally:
            con.close()
        out, status = await co.recover_execution(aid)
        require(out is None, f"a legacy EXECUTING row was resolved out of thin air: {out}")
        require_equal(status, "no_open_attempt", status)
        svc = ES.service_path(tmp)
        await _reconfirm(co, st, aid)
        _, again = await co.execute_approved(aid, ES.adapter(ES.make("non_idempotent", svc)))
        require_equal(again, "not_approved", f"a legacy EXECUTING row re-executed: {again}")
        require_equal(ES.make("non_idempotent", svc).count(), 0, "an effect was produced")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# Phase 9 — the recovery policy is total
# =====================================================================
async def t_the_recovery_policy_covers_every_combination():
    for status in (X.CLAIMED, X.EXTERNAL_PENDING, X.SUCCEEDED, X.FAILED_SAFE, X.UNKNOWN,
                   X.ABANDONED, "SOMETHING_NOBODY_DEFINED"):
        for sem in X.ALL_SEMANTICS + ("SOMETHING_NOBODY_DECLARED",):
            d = X.recovery_decision(status, sem)
            require(d in (X.RETRY_SAME_KEY, X.RECONCILE_FIRST, X.NO_ACTION_SUCCEEDED,
                          X.MANUAL_RECOVERY_REQUIRED, X.CLOSED_NO_EFFECT),
                    f"({status}, {sem}) -> {d}")
    # the fail-safe corners, named explicitly
    require_equal(X.recovery_decision(X.EXTERNAL_PENDING, X.NON_IDEMPOTENT_WRITE),
                  X.MANUAL_RECOVERY_REQUIRED, "non-idempotent ambiguity must be manual")
    require_equal(X.recovery_decision(X.UNKNOWN, X.NON_IDEMPOTENT_WRITE),
                  X.MANUAL_RECOVERY_REQUIRED, "UNKNOWN non-idempotent must be manual")
    require_equal(X.recovery_decision("SOMETHING_NOBODY_DEFINED", X.IDEMPOTENT_WRITE),
                  X.MANUAL_RECOVERY_REQUIRED, "an unknown status must fall through to manual")
    require_equal(X.recovery_decision(X.EXTERNAL_PENDING, "SOMETHING_NOBODY_DECLARED"),
                  X.MANUAL_RECOVERY_REQUIRED, "unknown semantics must fall through to manual")
    require(not X.may_auto_retry(X.EXTERNAL_PENDING, X.NON_IDEMPOTENT_WRITE), "auto retry")
    require(not X.may_auto_retry(X.EXTERNAL_PENDING, X.RECONCILABLE_WRITE),
            "reconcilable must reconcile first, not retry")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
