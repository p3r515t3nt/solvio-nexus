"""Approval Security V1 — the final hygiene block.

Each item here is a known finding from the P1B and P1C cold reviews. None of them was a
merge blocker; all of them are things that would become one the moment a real capability is
switched on. Nothing new is opened here.

  H1  a recovery retry performed a NEW external write with no authority re-check
  H2  two dormant authority writers with zero callers
  H3  dormant un-journalled execution writers
  H4  `_add_challenge` claimed a single-open invariant it did not enforce
  H5  `expose_to_llm` filtered the published schema list but not name resolution
  H6  structural guards that were too narrow
  H7  crash tests that did not prove the crash
  H8  a revoked-before-boundary request stayed EXECUTING for ever
  H9  no administrative view of what recovery still needs
  H10 nothing looked at the journal on start

OUT OF SCOPE, unchanged: product capabilities, production App Attest, S2B, remote approvals,
distributed execution, background schedulers.

ASSERTION POLICY: `require*` from `tests/_guard.py` are function calls and survive `-O`.

Direct: python tests/test_approval_v1_hygiene.py
"""
import ast as _ast
import asyncio
import inspect
import io
import contextlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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
from solvio.security.mobile_approval import admin_cli  # noqa: E402
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
CONTROL_SRC = os.path.join(SRC, "solvio", "security", "mobile_approval", "control.py")

# H12: test capabilities stay test capabilities. Registering them here — in code, at the same
# server-side declaration point a real one would use — is the mechanism being demonstrated.
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
    res, status = await co.apply_mobile_decision(
        **H.sign_decision(ctx, P.b64d(wire["payload_b64"])))
    require_equal(status, "ok", f"setup decision: {status}")
    with open(os.path.join(tmp, "aid.txt"), "w", encoding="utf-8") as fh:
        fh.write(aid)
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


async def _crash_after_effect(tmp, kind):
    """Reuse the P1C crash child: real process, real SIGKILL after the external effect."""
    import test_p1c_execution_recovery as P1C
    svc = ES.service_path(tmp)
    ES.make(kind, svc)
    proc, _ = P1C._child(tmp, "post_effect", kind, svc)
    # H7/A: prove the crash was a real SIGKILL, not a tidy exit that happened to look like one
    require_equal(proc.returncode, -9,
                  f"the child did not die by SIGKILL: rc={proc.returncode}")
    require_equal([a["status"] for a in _attempts(tmp)], [X.EXTERNAL_PENDING],
                  f"setup: {_attempts(tmp)}")
    require_equal(ES.make(kind, svc).count(), 1, "setup: the effect should have happened")
    return svc


# =====================================================================
# H1 — a recovery retry re-checks authority before writing again
# =====================================================================
async def t_h1_a_revoked_device_cannot_retry_an_idempotent_write():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "idempotent")
        await st.close()
        svc = await _crash_after_effect(tmp, "idempotent")
        _expire_leases(tmp)
        st2, cp2, co2 = await _wire(tmp)
        await cp2.revoke_device(ctx.device_id, reason="stolen")
        out, status = await co2.recover_execution(aid, ES.adapter(ES.make("idempotent", svc)))
        require(status.startswith("retry_refused:"), f"a revoked device retried: {status}")
        require("device_revoked" in status, status)
        require_equal(ES.make("idempotent", svc).count(), 1, "a new external write happened")
        require_equal([a["status"] for a in _attempts(tmp)], [X.UNKNOWN],
                      "the attempt should stay ambiguous, not be declared closed")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h1_an_expired_request_cannot_retry_an_idempotent_write():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "idempotent")
        await st.close()
        svc = await _crash_after_effect(tmp, "idempotent")
        _expire_leases(tmp)
        con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
        try:
            con.execute("UPDATE approval_requests SET expires_at=? WHERE approval_id=?",
                        (time.time() - 1, aid))
        finally:
            con.close()
        st2, cp2, co2 = await _wire(tmp)
        out, status = await co2.recover_execution(aid, ES.adapter(ES.make("idempotent", svc)))
        require(status.startswith("retry_refused:"), f"an expired request retried: {status}")
        require("request_expired" in status, status)
        require_equal(ES.make("idempotent", svc).count(), 1, "a new external write happened")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h1_every_authority_condition_is_rechecked():
    """Walk the conditions one by one, each on its own database."""
    cases = {
        "device revoked": ("UPDATE devices SET status=?", (S.DEVICE_REVOKED,),
                           "device_revoked"),
        "attestation lost": ("UPDATE devices SET attestation_status=?", (S.ATT_FAILED,),
                             "device_not_attested"),
        "enrolment gone": ("UPDATE devices SET current_enrollment_id=NULL", (),
                           "no_current_enrollment"),
        "principal changed": ("UPDATE devices SET principal=?", ("somebody-else",),
                              "principal_mismatch"),
        "environment lost": ("UPDATE devices SET environment=?", ("production",),
                             "device_environment_not_allowed"),
        "request expired": ("UPDATE approval_requests SET expires_at=?", (time.time() - 1,),
                            "request_expired"),
    }
    for label, (stmt, args, expected) in cases.items():
        tmp = os.path.realpath(tempfile.mkdtemp())
        try:
            st, cp, co, ctx, aid = await _approved(tmp, "idempotent")
            await st.close()
            svc = await _crash_after_effect(tmp, "idempotent")
            _expire_leases(tmp)
            con = sqlite3.connect(os.path.join(tmp, DB), isolation_level=None)
            try:
                con.execute(stmt, args)
            finally:
                con.close()
            st2, cp2, co2 = await _wire(tmp)
            out, status = await co2.recover_execution(
                aid, ES.adapter(ES.make("idempotent", svc)))
            require_equal(status, "retry_refused:" + expected, f"{label}: {status}")
            require_equal(ES.make("idempotent", svc).count(), 1,
                          f"{label}: a new external write happened")
            await st2.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


async def t_h1_a_revoked_reconcilable_may_still_observe_but_not_act():
    """Observation is not a write: an operator still needs to learn what happened."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "reconcilable")
        await st.close()
        svc = await _crash_after_effect(tmp, "reconcilable")
        _expire_leases(tmp)
        st2, cp2, co2 = await _wire(tmp)
        await cp2.revoke_device(ctx.device_id, reason="stolen")
        service = ES.make("reconcilable", svc)
        out, status = await co2.recover_execution(
            aid, ES.adapter(service), reconciler=ES.reconciler(service))
        require_equal(status, "reconciled_succeeded",
                      f"reconciliation of an existing effect was refused: {status}")
        require_equal(ES.make("reconcilable", svc).count(), 1,
                      "reconciliation produced a second effect")
        require_equal([a["status"] for a in _attempts(tmp)], [X.SUCCEEDED], _attempts(tmp))
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h1_a_revoked_reconcilable_with_no_effect_does_not_retry():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        import test_p1c_execution_recovery as P1C
        st, cp, co, ctx, aid = await _approved(tmp, "reconcilable")
        await st.close()
        svc = ES.service_path(tmp)
        ES.make("reconcilable", svc)
        proc, _ = P1C._child(tmp, "post_boundary", "reconcilable", svc)
        require_equal(proc.returncode, -9, f"no SIGKILL: {proc.returncode}")
        require_equal(ES.make("reconcilable", svc).count(), 0, "setup: no effect expected")
        _expire_leases(tmp)
        st2, cp2, co2 = await _wire(tmp)
        await cp2.revoke_device(ctx.device_id, reason="stolen")
        service = ES.make("reconcilable", svc)
        out, status = await co2.recover_execution(
            aid, ES.adapter(service), reconciler=ES.reconciler(service))
        require_equal(status, "reconciled_not_executed", f"recovery said {status}")
        require_equal(ES.make("reconcilable", svc).count(), 0,
                      "a revoked reconcilable attempt was retried anyway")
        require_equal([a["status"] for a in _attempts(tmp)], [X.ABANDONED], _attempts(tmp))
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h1_a_healthy_idempotent_retry_still_works():
    """The re-check must refuse the wrong cases, not every case."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "idempotent")
        await st.close()
        svc = await _crash_after_effect(tmp, "idempotent")
        _expire_leases(tmp)
        st2, cp2, co2 = await _wire(tmp)
        out, status = await co2.recover_execution(aid, ES.adapter(ES.make("idempotent", svc)))
        require_equal(status, "retried_succeeded", f"a healthy retry was refused: {status}")
        require_equal(ES.make("idempotent", svc).count(), 1, "the retry duplicated")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h1_the_decision_and_the_retry_share_one_predicate():
    """Two copies would drift. Asserted over the call graph, not over a literal."""
    src = open(STORE_SRC, encoding="utf-8").read()
    tree = _ast.parse(src)
    calls = {}
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            calls[node.name] = {c.func.attr for c in _ast.walk(node)
                                if isinstance(c, _ast.Call)
                                and isinstance(c.func, _ast.Attribute)}
    for caller in ("_commit_decision", "_recheck_execution_authority"):
        require(caller in calls, f"{caller} disappeared")
        require("_execution_authority" in calls[caller],
                f"{caller} no longer uses the shared authority predicate")
    body = src[src.index("def _execution_authority("):]
    body = body[:body.index("\n    async def ")]
    for needed in ("FROM revocations", "current_enrollment_id", "attestation_status",
                   "principal", "allowed_envs"):
        require(needed in body, f"the shared predicate lost {needed}")


# =====================================================================
# H2/H3 — dormant writers are gone and may not return
# =====================================================================
async def t_h2_h3_the_dormant_writers_are_gone():
    gone = {
        "store": (STORE_SRC, ["consume_attestation_challenge", "bump_app_attest_counter",
                              "_consume_att_challenge", "_bump_counter"]),
        "control": (CONTROL_SRC, ["mark_consumed", "mark_failed"]),
    }
    for label, (path, names) in gone.items():
        tree = _ast.parse(open(path, encoding="utf-8").read())
        defined = {n.name for n in _ast.walk(tree)
                   if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
        for name in names:
            require(name not in defined, f"{label}.{name} came back")
    require(not hasattr(S.ApprovalControlStore, "consume_attestation_challenge"), "store")
    require(not hasattr(S.ApprovalControlStore, "bump_app_attest_counter"), "store")
    require(not hasattr(S.ApprovalControlStore, "claim_execution"),
            "the public un-journalled claim came back")
    require(not hasattr(C.MobileApprovalControlPlane, "mark_consumed"), "control")
    require(not hasattr(C.MobileApprovalControlPlane, "mark_failed"), "control")
    # and the private statement that survives is reachable ONLY from the journalled claim
    src = open(STORE_SRC, encoding="utf-8").read()
    callers = [n.name for n in _ast.walk(_ast.parse(src))
               if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
               and any(isinstance(c, _ast.Call) and isinstance(c.func, _ast.Attribute)
                       and c.func.attr == "_claim_execution" for c in _ast.walk(n))]
    require_equal(callers, ["_claim_execution_attempt"],
                  f"the raw claim gained callers: {callers}")


async def t_h3_every_executing_writer_is_journalled():
    """Behavioural: there is no way to reach EXECUTING that leaves no attempt row."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        reason = await cp.claim_execution(aid, ctx.device_id)
        require(reason is None, f"the surviving claim entry point refused: {reason}")
        require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"],
                      S.EXECUTING, "the state did not move")
        require_equal(len(_attempts(tmp)), 1,
                      f"EXECUTING was reached without a journal entry: {_attempts(tmp)}")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# H4 — _add_challenge is atomic
# =====================================================================
async def t_h4_add_challenge_is_one_transaction():
    src = open(STORE_SRC, encoding="utf-8").read()
    body = src[src.index("def _add_challenge("):]
    body = body[:body.index("\n    async def ")]
    require_equal(body.count("BEGIN IMMEDIATE"), 1, "the challenge write is not atomic")
    require_equal(body.count('self._conn.execute("COMMIT")'), 1, "more than one commit point")


async def _pending(tmp):
    """A request with an OPEN challenge — the decision would burn it, so do not decide."""
    st, cp, co = await _wire(tmp)
    ctx = await H.enroll_attested(cp)
    ws = os.path.join(tmp, "ws")
    os.makedirs(ws, exist_ok=True)
    aid = await cp.create_request(principal="local-owner", tool="t_non_idempotent",
                                  mode="modify", task="edit README", workspace=ws,
                                  human_summary="Codex will edit README")
    wire, status = await cp.issue_challenge(approval_id=aid, device_id=ctx.device_id)
    require_equal(status, "ok", status)
    return st, cp, co, ctx, aid


async def t_h4_a_failed_challenge_write_invalidates_nothing():
    """Roll back mid-write: the previous open challenge must survive intact."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _pending(tmp)
        first = _rows(tmp, "SELECT * FROM challenges WHERE consumed=0")
        require_equal(len(first), 1, f"setup: {first}")
        real = st._audit
        boom = RuntimeError("injected failure between invalidate and insert")

        def exploding(*a, **kw):
            raise boom

        st._audit = exploding
        try:
            await cp.issue_challenge(approval_id=aid, device_id=ctx.device_id)
            require(False, "the injected failure did not propagate")
        except RuntimeError as exc:
            require(exc is boom, f"wrong exception: {exc}")
        finally:
            st._audit = real
        after = _rows(tmp, "SELECT * FROM challenges WHERE consumed=0")
        require_equal(after, first,
                      "a rolled-back issuance still invalidated the open challenge")
        require_equal(len(_rows(tmp, "SELECT * FROM challenges")), 1,
                      "a partial challenge row survived")
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h4_concurrent_issuance_leaves_exactly_one_open_challenge():
    import threading
    for i in range(20):
        tmp = os.path.realpath(tempfile.mkdtemp())
        try:
            st, cp, co, ctx, aid = await _pending(tmp)
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
                except BaseException as exc:      # noqa: BLE001
                    out[k] = ("EXC", f"{type(exc).__name__}: {exc}")
            threads = [threading.Thread(target=worker, args=(k,)) for k in (0, 1)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
            require(not any(t.is_alive() for t in threads), "an issuance worker deadlocked")
            require(not any(o[0] == "EXC" for o in out if isinstance(o, tuple)), out)
            open_rows = _rows(tmp, "SELECT * FROM challenges WHERE consumed=0")
            require_equal(len(open_rows), 1,
                          f"round {i}: {len(open_rows)} open challenges — {open_rows}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# H5 — expose_to_llm is enforced at resolution, not only in the schema list
# =====================================================================
def _dispatcher():
    from solvio.tools.dispatcher import ToolDispatcher
    from solvio.tools.base import RiskLevel

    class Exposed:
        name = "t_exposed"
        risk_level = RiskLevel.HARMLESS
        expose_to_llm = True

        def schema(self):
            return {"type": "function", "name": self.name}

        async def run(self, args):
            from solvio.tools.base import ToolResult
            return ToolResult(True, data={"ran": True})

    class Hidden(Exposed):
        name = "t_hidden"
        expose_to_llm = False

    d = ToolDispatcher()
    d.register(Exposed())
    d.register(Hidden())
    return d


async def t_h5_an_exposed_tool_still_works():
    res = await _dispatcher().dispatch("t_exposed", {})
    require(res.get("success"), f"an exposed tool was refused: {res}")


async def t_h5_a_hidden_tool_is_refused_by_name():
    res = await _dispatcher().dispatch("t_hidden", {})
    require(not res.get("success"), "a non-exposed tool ran through the LLM dispatch")
    require_equal(res.get("error"), "not_exposed_to_llm:t_hidden", res)


async def t_h5_schema_hiding_is_not_the_mechanism():
    d = _dispatcher()
    names = [t["name"] for t in d.openai_tools()]
    require("t_hidden" not in names, "setup: the schema should already be hidden")
    require("t_hidden" in d.names(), "setup: the tool is still registered")
    res = await d.dispatch("t_hidden", {})
    require(not res.get("success"),
            "the tool was registered and reachable — hiding the schema was the only guard")


async def t_h5_an_executing_tool_is_not_reachable_from_llm_dispatch():
    from _approval_fixture_tool import (CONFIRM_TOOL, FixtureConfirmTool,
                                        FixtureExecutor)
    from solvio.tools.dispatcher import ToolDispatcher

    d = ToolDispatcher()
    d.register(FixtureConfirmTool(FixtureExecutor(), ApprovalBroker()))
    require(CONFIRM_TOOL not in [t["name"] for t in d.openai_tools()], "schema")
    res = await d.dispatch(CONFIRM_TOOL, {"request_id": "anything"})
    require(not res.get("success"), f"{CONFIRM_TOOL} ran through the LLM dispatch")
    require_equal(res.get("error"), f"not_exposed_to_llm:{CONFIRM_TOOL}", res)


async def t_h5_the_trusted_route_still_reaches_it():
    """The control-plane channel keeps its explicitly intended route — and its own gate."""
    from _approval_fixture_tool import (CONFIRM_TOOL, FixtureConfirmTool,
                                        FixtureExecutor)
    from solvio.tools.dispatcher import ToolDispatcher

    d = ToolDispatcher()
    d.register(FixtureConfirmTool(FixtureExecutor(), ApprovalBroker()))
    res = await d.dispatch_trusted(CONFIRM_TOOL, {"request_id": "not-approved"})
    require(not res.get("success"), "an unapproved request executed")
    require(res.get("error") != f"not_exposed_to_llm:{CONFIRM_TOOL}",
            "the trusted route was blocked as if it were the LLM")


# =====================================================================
# H6 — authority guard sweep over src/ and scripts/
# =====================================================================
def _python_files(*roots):
    out = []
    for root in roots:
        for base, _dirs, files in os.walk(root):
            if "__pycache__" in base:
                continue
            out += [os.path.join(base, f) for f in sorted(files) if f.endswith(".py")]
    return sorted(out)


def _syntax_context(tree, node):
    """Actual lexical owners, so an allowed filename cannot hide a second caller."""
    parents = {child: parent for parent in _ast.walk(tree)
               for child in _ast.iter_child_nodes(parent)}
    owner = []
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (_ast.ClassDef, _ast.FunctionDef, _ast.AsyncFunctionDef)):
            owner.append(current.name)
    return tuple(reversed(owner)), parents.get(node)


async def t_h6_no_authority_entry_point_outside_the_package():
    pkg = os.path.join(SRC, "solvio", "security", "mobile_approval")
    authority = ("submit_decision", "commit_decision", "revoke_identities", "revoke_device",
                 "claim_execution_attempt", "begin_external_execution",
                 "finish_execution_attempt", "recover_execution", "consume_challenge",
                 "consume_enrollment_token", "recheck_execution_authority")
    leaks, app_nonces, proof_classes = [], [], []
    # There is still one nonce CAS. These exact proof types inherit the
    # same registered-device verification with separate purpose domains. The
    # follow-up authorizes a new run of the same task, never a reused start or
    # answer assertion; it adds no verifier/CAS implementation of its own.
    expected_classes = {
        ("src/solvio/agent_runtime/task_start_proof.py", "TaskStartProofService"): {
            "body_digest": "staticmethod(request_digest)",
            "assertion_hash": "staticmethod(client_data_hash)",
            "challenge_prefix": "_CHALLENGE_PREFIX",
            "binding_type": "TYPE_TASK_START_BINDING",
            "audit_prefix": "'app_task_start'"},
        ("src/solvio/agent_runtime/action_intent_endpoint.py", "TaskAnswerProofService"): {
            "body_digest": "staticmethod(answer_digest)",
            "assertion_hash": "staticmethod(client_data_hash)",
            "challenge_prefix": "'app-action-answer:'",
            "binding_type": "TYPE_ACTION_ANSWER",
            "audit_prefix": "'app_action_answer'"},
        ("src/solvio/agent_runtime/task_followup_endpoint.py", "TaskFollowupProofService"): {
            "body_digest": "staticmethod(request_digest)",
            "assertion_hash": "staticmethod(client_data_hash)",
            "challenge_prefix": "'app-task-followup:'",
            "binding_type": "TYPE_TASK_FOLLOWUP",
            "audit_prefix": "'app_task_followup'"},
        # N8/C3: one chat message from the app. Same registered-device
        # verification, own purpose domain and nonce namespace; it authorizes
        # the acceptance of exactly one message body, never a task or answer.
        ("src/solvio/agent_runtime/conversation_message_proof.py", "ConversationMessageProofService"): {
            "body_digest": "staticmethod(request_digest)",
            "assertion_hash": "staticmethod(client_data_hash)",
            "challenge_prefix": "'app-conversation-message:'",
            "binding_type": "TYPE_CONVERSATION_MESSAGE_BINDING",
            "audit_prefix": "'app_conversation_message'"},
        # Exact app cost cap, not a task start/resume or biometric decision.
        ("src/solvio/agent_runtime/task_cost_approval_proof.py", "TaskCostApprovalProofService"): {
            "body_digest": "staticmethod(request_digest)",
            "assertion_hash": "staticmethod(client_data_hash)",
            "challenge_prefix": "'app-task-cost-approval:'",
            "binding_type": "TYPE_TASK_COST_APPROVAL",
            "audit_prefix": "'app_task_cost_approval'"},
    }
    proof_names = {name for _path, name in expected_classes}
    sealed = {"challenge_prefix", "binding_type", "assertion_hash", "body_digest", "audit_prefix"}
    for path in _python_files(SRC, os.path.join(REPO, "scripts")):
        if path.startswith(pkg):
            continue
        rel = os.path.relpath(path, REPO)
        tree = _ast.parse(open(path, encoding="utf-8").read())
        for cls in (n for n in _ast.walk(tree) if isinstance(n, _ast.ClassDef)):
            key = (rel, cls.name)
            bases = [_ast.unparse(base).rsplit(".", 1)[-1] for base in cls.bases]
            if key in expected_classes or any(base in proof_names for base in bases):
                require(key in expected_classes, "unreviewed app-proof subclass")
                require_equal(bases, [] if cls.name == "TaskStartProofService" else ["TaskStartProofService"])
                assignments = [n for n in cls.body if isinstance(n, _ast.Assign)
                    and len(n.targets) == 1 and isinstance(n.targets[0], _ast.Name)
                    and n.targets[0].id in sealed]
                require_equal(len(assignments), len(sealed), "duplicate app-proof domain definition")
                attrs = {n.targets[0].id: _ast.unparse(n.value) for n in assignments}
                require_equal(attrs, expected_classes[key])
                if cls.name in {"TaskAnswerProofService", "TaskFollowupProofService",
                                "ConversationMessageProofService", "TaskCostApprovalProofService"}:
                    require_equal([n for n in cls.body if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))], [])
                    domain, binding, expected_domain, expected_binding = {
                        "TaskAnswerProofService": ("DOMAIN_ACTION_ANSWER", "TYPE_ACTION_ANSWER",
                                                   b"SOLVIO_APP_ACTION_ANSWER_V1", "app_action_answer_binding"),
                        "TaskFollowupProofService": ("DOMAIN_TASK_FOLLOWUP", "TYPE_TASK_FOLLOWUP",
                                                     b"SOLVIO_APP_TASK_FOLLOWUP_V1", "app_task_followup_binding"),
                        "ConversationMessageProofService": (
                            "DOMAIN_CONVERSATION_MESSAGE", "TYPE_CONVERSATION_MESSAGE_BINDING",
                            b"SOLVIO_APP_CONVERSATION_MESSAGE_V1", "app_conversation_message_binding"),
                        "TaskCostApprovalProofService": (
                            "DOMAIN_TASK_COST_APPROVAL", "TYPE_TASK_COST_APPROVAL",
                            b"SOLVIO_APP_TASK_COST_APPROVAL_V1", "app_task_cost_approval_binding"),
                    }[cls.name]
                    constants = {n.targets[0].id: _ast.literal_eval(n.value)
                        for n in tree.body if isinstance(n, _ast.Assign) and len(n.targets) == 1
                        and isinstance(n.targets[0], _ast.Name)
                        and n.targets[0].id in {domain, binding}}
                    require_equal(constants, {domain: expected_domain, binding: expected_binding})
                    hasher = next(n for n in tree.body if isinstance(n, _ast.FunctionDef) and n.name == "client_data_hash")
                    require_equal(_ast.unparse(hasher.body[0].value),
                        "hashlib.sha256(" + domain + " + b'\\x00' + raw).digest()")
                proof_classes.append(key)
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.Name, _ast.Attribute)) and isinstance(node.ctx, (_ast.Store, _ast.Del)):
                name = node.id if isinstance(node, _ast.Name) else node.attr
                if name in sealed:
                    owner, parent = _syntax_context(tree, node)
                    require(isinstance(node, _ast.Name) and len(owner) == 1
                        and (rel, owner[0]) in expected_classes
                        and isinstance(parent, _ast.Assign) and parent.targets == [node]
                        and _ast.unparse(parent.value) == expected_classes[(rel, owner[0])][name],
                        "app-proof domain overwritten outside its exact definition")
            if isinstance(node, _ast.Call) and _ast.unparse(node.func).rsplit(".", 1)[-1] in {"setattr", "delattr"}:
                require(not any(isinstance(arg, _ast.Constant) and arg.value in sealed
                    for arg in node.args if isinstance(arg, _ast.Constant) and isinstance(arg.value, str)),
                    "dynamic app-proof domain overwrite")
            name = (node.attr if isinstance(node, _ast.Attribute) else
                    node.id if isinstance(node, _ast.Name) else
                    node.name if isinstance(node, (_ast.alias, _ast.FunctionDef,
                                                   _ast.AsyncFunctionDef)) else
                    node.value if isinstance(node, _ast.Constant)
                    and isinstance(node.value, str) else "")
            if not any(sym in name for sym in authority):
                continue
            owner, parent = _syntax_context(tree, node)
            # N2 authenticates one app task; it does not decide a Face-ID request.
            # The exception is this exact call/domain, never the whole module.
            if (rel == "src/solvio/agent_runtime/task_start_proof.py"
                    and owner == ("TaskStartProofService", "_verify_and_consume")
                    and _ast.unparse(node) == "self.store._cas_consume_challenge"
                    and isinstance(parent, _ast.Call) and parent.func is node):
                require_equal([_ast.unparse(a) for a in parent.args],
                    ["nonce", "self.challenge_prefix + nonce", "device_id",
                     "row['principal']", "digest", "raw_hash"])
                require_equal(parent.keywords, [])
                constants = {n.targets[0].id: _ast.literal_eval(n.value)
                    for n in tree.body if isinstance(n, _ast.Assign)
                    and len(n.targets) == 1 and isinstance(n.targets[0], _ast.Name)
                    and n.targets[0].id in {"_CHALLENGE_PREFIX", "DOMAIN_TASK_START"}}
                require_equal(constants, {"_CHALLENGE_PREFIX": "app-task-start:",
                    "DOMAIN_TASK_START": b"SOLVIO_APP_TASK_START_V1"})
                hasher = next(n for n in tree.body
                    if isinstance(n, _ast.FunctionDef) and n.name == "client_data_hash")
                require_equal(_ast.unparse(hasher.body[0].value),
                    "hashlib.sha256(DOMAIN_TASK_START + b'\\x00' + binding_raw).digest()")
                app_nonces.append((rel, owner))
            else:
                leaks.append(f"{rel}:{node.lineno}::{name}")
    require_equal(leaks, [], f"authority reachable outside mobile_approval: {leaks}")
    require_equal(len(app_nonces), 1, "the one namespaced app-task nonce seam changed")
    require_equal(sorted(proof_classes), sorted(expected_classes), "app-proof classes changed")


async def t_h6_s1_approval_has_exactly_one_caller():
    callers, cost_callers = [], []
    for path in _python_files(SRC, os.path.join(REPO, "scripts")):
        if path.endswith(os.path.join("security", "approval.py")):
            continue
        tree = _ast.parse(open(path, encoding="utf-8").read())
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute) \
                    and node.func.attr == "approve":
                rel = os.path.relpath(path, REPO)
                owner, _ = _syntax_context(tree, node)
                receiver = _ast.unparse(node.func.value)
                if (rel == "src/solvio/agent_runtime/task_endpoint.py"
                        and owner == ("attach", "approve_cost") and receiver == "orch.costs"):
                    # A per-task cost ceiling is not an S1 action approval.
                    require_equal([_ast.unparse(a) for a in node.args], ["task.task_id"])
                    require_equal({k.arg for k in node.keywords},
                                  {"max_total_cents", "approval_ref"})
                    cost_callers.append((rel, owner, receiver))
                else:
                    callers.append((rel, owner, receiver))
    require_equal(callers, [("src/solvio/security/mobile_approval/bridge.py",
                  ("MobileApprovalCoordinator", "execute_approved"), "self.broker")],
                  f"S1 approval callers changed: {callers}")
    require_equal(len(cost_callers), 1, "only the authenticated task cost handler may set a ceiling")


async def t_h6_no_new_direct_writer_on_critical_state():
    """AST over string literals: only the known modules may name these UPDATEs at all."""
    critical = ("UPDATE approval_requests SET state=", "UPDATE devices SET status=",
                "UPDATE execution_attempts SET status=", "UPDATE challenges SET consumed=",
                "UPDATE enrollment_tokens SET consumed=")
    allowed = {os.path.join("src", "solvio", "security", "mobile_approval", "store.py")}
    found = {}
    for path in _python_files(SRC, os.path.join(REPO, "scripts")):
        rel = os.path.relpath(path, REPO)
        text = open(path, encoding="utf-8").read()
        for node in _ast.walk(_ast.parse(text)):
            if isinstance(node, _ast.Constant) and isinstance(node.value, str):
                for frag in critical:
                    if frag in node.value:
                        found.setdefault(rel, set()).add(frag)
    require_equal(set(found) - allowed, set(),
                  f"a module outside the store writes critical state: {found}")


async def t_h6_no_llm_reachable_tool_touches_approval_authority():
    """Every tool the model can see must be free of authority calls."""
    from solvio.tools import registry  # noqa: F401  (import proves it loads)
    tools_dir = os.path.join(SRC, "solvio", "tools")
    banned = ("submit_decision", "commit_decision", "revoke_", "claim_execution",
              "recover_execution", "finish_execution_attempt")
    hits = []
    for path in _python_files(tools_dir):
        text = open(path, encoding="utf-8").read()
        for sym in banned:
            if sym in text:
                hits.append(f"{os.path.relpath(path, REPO)}::{sym}")
    require_equal(hits, [], f"a tool module references approval authority: {hits}")


# =====================================================================
# H7 — crash-test hygiene
# =====================================================================
async def t_h7_the_crash_children_really_die_by_signal():
    import test_p1c_execution_recovery as P1C
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "non_idempotent")
        await st.close()
        svc = ES.service_path(tmp)
        ES.make("non_idempotent", svc)
        for where in ("pre_claim", "post_claim", "post_boundary", "post_effect"):
            # each needs its own database; rebuild per case
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp, exist_ok=True)
            st, cp, co, ctx, aid = await _approved(tmp, "non_idempotent")
            await st.close()
            svc = ES.service_path(tmp)
            ES.make("non_idempotent", svc)
            proc, _ = P1C._child(tmp, where, "non_idempotent", svc)
            require_equal(proc.returncode, -9,
                          f"{where}: expected SIGKILL, got rc={proc.returncode}\n"
                          f"{proc.stdout[-300:]}{proc.stderr[-300:]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h7_a_crash_after_durable_success_really_is_after_success():
    """The child must reach SUCCEEDED and then die — not die on the way there."""
    import test_p1c_execution_recovery as P1C
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "non_idempotent")
        await st.close()
        svc = ES.service_path(tmp)
        ES.make("non_idempotent", svc)
        proc, status = P1C._child(tmp, "post_success", "non_idempotent", svc)
        require_equal(proc.returncode, -9,
                      f"expected SIGKILL after success, rc={proc.returncode}\n"
                      f"{proc.stdout[-300:]}{proc.stderr[-300:]}")
        require_equal([a["status"] for a in _attempts(tmp)], [X.SUCCEEDED],
                      f"the success was not durable before the crash: {_attempts(tmp)}")
        require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"],
                      S.CONSUMED, "the request did not reach CONSUMED before the crash")
        require_equal(ES.make("non_idempotent", svc).count(), 1, "setup")
        # restart: nothing may run again
        st2, cp2, co2 = await _wire(tmp)
        out, rec = await co2.recover_execution(aid, ES.adapter(ES.make("non_idempotent", svc)))
        require_equal(rec, "already_succeeded", rec)
        require_equal(ES.make("non_idempotent", svc).count(), 1, "restart duplicated")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h7_a_crash_during_recovery_leaves_a_safe_state():
    import test_p1c_execution_recovery as P1C
    for kind, expected in (("idempotent", X.EXTERNAL_PENDING),
                           ("reconcilable", X.EXTERNAL_PENDING),
                           ("non_idempotent", X.EXTERNAL_PENDING)):
        tmp = os.path.realpath(tempfile.mkdtemp())
        try:
            st, cp, co, ctx, aid = await _approved(tmp, kind)
            await st.close()
            svc = ES.service_path(tmp)
            ES.make(kind, svc)
            P1C._child(tmp, "post_effect", kind, svc)
            _expire_leases(tmp)
            # now crash INSIDE recovery
            proc, _ = P1C._child(tmp, "post_effect", kind, svc, "", "recover")
            # RECONCILABLE never reaches the executor hook — it reconciles and finishes — so
            # the assertion is the INVARIANT, not a particular status: whatever the crash
            # left behind must be a state recovery can still handle safely, and the external
            # effect must still be exactly one.
            left = [a["status"] for a in _attempts(tmp)]
            require(left in ([expected], [X.SUCCEEDED], [X.UNKNOWN]),
                    f"{kind}: a crash during recovery left {left}")
            require_equal(ES.make(kind, svc).count(), 1,
                          f"{kind}: the crash during recovery duplicated the effect")
            _expire_leases(tmp)
            st2, cp2, co2 = await _wire(tmp)
            service = ES.make(kind, svc)
            out, status = await co2.recover_execution(
                aid, ES.adapter(service), reconciler=ES.reconciler(service)
                if kind == "reconcilable" else None)
            require(status in ("retried_succeeded", "reconciled_succeeded",
                               "manual_recovery_required", "already_succeeded",
                               "no_open_attempt"),
                    f"{kind}: recovery after a recovery crash said {status}")
            require_equal(service.count(), 1, f"{kind}: the effect was duplicated")
            await st2.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


async def t_h7_the_stable_key_assertion_compares_two_observations():
    """H7/D: the key must be compared against one taken BEFORE the retry, not itself."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "idempotent")
        await st.close()
        svc = await _crash_after_effect(tmp, "idempotent")
        before_service = ES.make("idempotent", svc).effects()[0]["idempotency_key"]
        before_journal = _attempts(tmp)[0]["idempotency_key"]
        require_equal(before_service, before_journal,
                      "the effect was not written under the journalled key")
        _expire_leases(tmp)
        st2, cp2, co2 = await _wire(tmp)
        out, status = await co2.recover_execution(aid, ES.adapter(ES.make("idempotent", svc)))
        require_equal(status, "retried_succeeded", status)
        after = ES.make("idempotent", svc).effects()
        require_equal(len(after), 1, f"the retry duplicated: {after}")
        require_equal(after[0]["idempotency_key"], before_service,
                      "the retry used a different key than the pre-crash observation")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# H8 — revoked before the boundary terminalises the request
# =====================================================================
async def t_h8_revoked_before_boundary_leaves_no_dangling_executing():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp)
        svc = ES.service_path(tmp)
        service = ES.make("non_idempotent", svc)
        st2, cp2, co2 = await _wire(tmp)
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
        require_equal(status, "device_revoked", status)
        require_equal(service.count(), 0, "an external effect happened")
        require_equal([a["status"] for a in _attempts(tmp)], [X.ABANDONED], _attempts(tmp))
        state = _rows(tmp, "SELECT state, error FROM approval_requests")[0]
        require(state["state"] != S.EXECUTING,
                f"the request was left dangling in EXECUTING: {state}")
        require_equal(state["state"], S.FAILED, state)
        require("revoked_before_external_start" in (state["error"] or ""), state)
        require("execution_abandoned_revoked" in _events(tmp), _events(tmp))
        # and nothing may re-enter through the abandoned attempt
        out, rec = await co.recover_execution(aid)
        require_equal(rec, "no_open_attempt", f"an abandoned attempt was reopened: {rec}")
        await st.close()
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# H9 — administrative recovery surface
# =====================================================================
async def t_h9_the_admin_view_lists_what_needs_attention():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "non_idempotent")
        await st.close()
        await _crash_after_effect(tmp, "non_idempotent")
        st2, _, _ = await _wire(tmp)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = await admin_cli._cmd_executions(None, st2, None)
        out = buf.getvalue()
        require_equal(rc, 0, out)
        require(X.EXTERNAL_PENDING in out, out)
        require(X.NON_IDEMPOTENT_WRITE in out, out)
        require(X.MANUAL_RECOVERY_REQUIRED in out, out)
        require("KANN eingetreten sein" in out,
                "the ambiguity is not spelled out for the operator")
        require(_attempts(tmp)[0]["execution_id"] in out, "no execution identity shown")
        require(_attempts(tmp)[0]["attempt_id"] in out, "no attempt identity shown")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h9_the_admin_surface_cannot_fabricate_a_success():
    src = open(os.path.join(SRC, "solvio", "security", "mobile_approval", "admin_cli.py"),
               encoding="utf-8").read()
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Constant) and isinstance(node.value, str):
            require("UPDATE execution_attempts" not in node.value,
                    "the admin CLI writes the journal directly")
            require("SUCCEEDED" not in node.value or "status" not in node.value,
                    f"the admin CLI names a success status literally: {node.value[:80]}")
    require("finish_execution_attempt" not in src,
            "the admin CLI can set an outcome without the central policy")
    require("recover_execution" in src, "the admin CLI does not use the central recovery")


async def t_h9_admin_recovery_uses_the_central_policy():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "non_idempotent")
        await st.close()
        svc = await _crash_after_effect(tmp, "non_idempotent")
        _expire_leases(tmp)
        st2, cp2, _ = await _wire(tmp)

        class _Args:
            approval_id = aid

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = await admin_cli._cmd_recover_execution(cp2, st2, _Args())
        out = buf.getvalue()
        require_equal(rc, 0, out)
        require("manual_recovery_required" in out, out)
        require_equal(ES.make("non_idempotent", svc).count(), 1,
                      "the admin command produced an external effect")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# H10 — startup recovery wiring
# =====================================================================
async def t_h10_startup_closes_only_what_is_known_safe():
    import test_p1c_execution_recovery as P1C
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "non_idempotent")
        await st.close()
        svc = ES.service_path(tmp)
        ES.make("non_idempotent", svc)
        P1C._child(tmp, "post_claim", "non_idempotent", svc)   # attempt stays CLAIMED
        require_equal([a["status"] for a in _attempts(tmp)], [X.CLAIMED], _attempts(tmp))
        # FREEZE/F1A: the crashed owner's CLAIM lease outlives it, and the scan now respects
        # leases — so it must NOT close the attempt while that lease is still valid. Prove
        # that first, then let the lease lapse the way a real restart eventually does.
        st_live, cp_live, co_live = await _wire(tmp)
        live = await co_live.startup_recovery_scan()
        require_equal(live["closed_no_effect"], 0,
                      f"the scan closed an attempt whose lease is still held: {live}")
        require_equal(len(live["contended"]), 1, live)
        require_equal(live["contended"][0]["detail"], "recovery_lease_held", live)
        require_equal([a["status"] for a in _attempts(tmp)], [X.CLAIMED],
                      "a leased attempt was mutated anyway")
        await st_live.close()
        _expire_leases(tmp)
        st2, cp2, co2 = await _wire(tmp)
        summary = await co2.startup_recovery_scan()
        require_equal(summary["closed_no_effect"], 1, summary)
        require_equal(summary["needs_attention"], [], summary)
        require_equal([a["status"] for a in _attempts(tmp)], [X.ABANDONED], _attempts(tmp))
        require_equal(_rows(tmp, "SELECT state FROM approval_requests")[0]["state"],
                      S.FAILED, "the request stayed EXECUTING after startup recovery")
        require_equal(ES.make("non_idempotent", svc).count(), 0, "startup executed something")
        require("startup_recovery_scan" in _events(tmp), _events(tmp))
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h10_startup_never_retries_an_ambiguous_effect():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "non_idempotent")
        await st.close()
        svc = await _crash_after_effect(tmp, "non_idempotent")
        st2, cp2, co2 = await _wire(tmp)
        summary = await co2.startup_recovery_scan()
        require_equal(summary["closed_no_effect"], 0, summary)
        require_equal(len(summary["needs_attention"]), 1, summary)
        require_equal(summary["needs_attention"][0]["decision"], X.MANUAL_RECOVERY_REQUIRED,
                      summary)
        require_equal(ES.make("non_idempotent", svc).count(), 1,
                      "a restart produced a second external effect")
        require_equal([a["status"] for a in _attempts(tmp)], [X.EXTERNAL_PENDING],
                      "startup resolved an ambiguity it cannot resolve")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h10_startup_surfaces_but_does_not_run_a_retryable_attempt():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        st, cp, co, ctx, aid = await _approved(tmp, "idempotent")
        await st.close()
        svc = await _crash_after_effect(tmp, "idempotent")
        st2, cp2, co2 = await _wire(tmp)
        summary = await co2.startup_recovery_scan()
        require_equal(len(summary["retryable"]), 1, summary)
        require_equal(ES.make("idempotent", svc).count(), 1,
                      "startup performed the retry itself")
        require_equal([a["status"] for a in _attempts(tmp)], [X.EXTERNAL_PENDING],
                      "startup consumed the attempt")
        await st2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_h10_startup_performs_no_external_write_at_all():
    """Structural companion: the scan must not be able to call an executor."""
    src = open(BRIDGE_SRC, encoding="utf-8").read()
    node = next(n for n in _ast.walk(_ast.parse(src))
                if isinstance(n, _ast.AsyncFunctionDef)
                and n.name == "startup_recovery_scan")
    # Names it may CALL — a comment mentioning "executor" is prose, not a call.
    called = {c.func.id for c in _ast.walk(node)
              if isinstance(c, _ast.Call) and isinstance(c.func, _ast.Name)}
    for forbidden in ("executor", "reconciler"):
        require(forbidden not in called, f"the startup scan calls {forbidden}")
    names = {n.id for n in _ast.walk(node) if isinstance(n, _ast.Name)}
    require_equal(names & {"executor", "reconciler"}, set(),
                  "the startup scan binds an executor or reconciler")
    body = _ast.get_source_segment(src, node) or ""
    require("recovery_decision(" in body, "the startup scan does not use the central policy")
    params = set(inspect.signature(
        B.MobileApprovalCoordinator.startup_recovery_scan).parameters)
    require_equal(params, {"self"}, f"the startup scan takes arguments: {sorted(params)}")


# =====================================================================
# H12 — no product capability was activated
# =====================================================================
async def t_h12_no_production_capability_became_retryable():
    production = {k: v for k, v in X.CAPABILITY_SEMANTICS.items()
                  if not k.startswith("t_")}
    require(production, "the capability registry is empty")
    for name, sem in production.items():
        require_equal(sem, X.NON_IDEMPOTENT_WRITE,
                      f"production capability {name} was reclassified to {sem}")
    # Substring matching would be wrong here: NON_IDEMPOTENT_WRITE contains
    # IDEMPOTENT_WRITE. The shipped registry is read from the AST instead.
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


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
