"""N2: dashboard and Face ID decide one task row; real temporary stores/S1 gate.

Browser enrollment/authentication and mobile signatures are real components.
Only the Apple attestation chain and final task executor are supplied by the test.
No productive state, provider request, real device or network is used.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import os
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import mobile_attest_helper as H
from solvio.security.approval import ApprovalBroker
from solvio.security.mobile_approval import bridge as B, control as C, identity, protocol as P, store as S
from solvio.security.mobile_approval.browser_sessions import BrowserActor, BrowserSessionService


async def _wire(path, approver=None):
    store = S.ApprovalControlStore(os.path.join(path, "approval.sqlite3"))
    await store.open()
    cp = C.MobileApprovalControlPlane(store, identity.MacSigningKey.load_or_create(path),
        identity.load_or_create_core_instance_id(path), attest_verifier=H.fake_verifier(),
        app_id="WQ8CG7R53R.de.solvio.approvals")
    approver = approver or B.MobileApprover()
    coordinator = B.MobileApprovalCoordinator(cp, ApprovalBroker(approver=approver), approver)
    sessions = BrowserSessionService(store, core_instance_id=cp.core_instance_id)
    return store, cp, coordinator, sessions


@asynccontextmanager
async def _world():
    with tempfile.TemporaryDirectory(prefix="solvio-dashboard-approval-") as path:
        store, cp, co, sessions = await _wire(path)
        enrollment = await sessions.issue_enrollment(principal="local-owner")
        session = await sessions.redeem(enrollment.token)
        actor = await sessions.authenticate(session.token, csrf_token=session.csrf_token)
        require(actor is not None)
        try:
            yield path, store, cp, co, sessions, session, actor
        finally:
            await store.close()


async def _request(cp, tool="agent_task_research", principal="local-owner"):
    key = await cp.create_request(principal=principal, tool=tool, mode="execute",
        task='{"objective":"Vergleiche drei Hotels mit belegten Quellen"}',
        workspace="", human_summary="Drei Hotels vergleichen")
    return key, (await cp.store.get_request(key))["action_digest"]


def _executor(calls):
    async def execute(action):
        calls.append(action)
        return True, {"task_id": "temporary-task"}
    return execute


async def t_dashboard_approval_uses_the_same_gate_and_exact_execution_journal():
    for tool in sorted(S.DASHBOARD_TASK_TOOLS):
        async with _world() as (_, store, cp, co, sessions, session, actor):
            key, digest = await _request(cp, tool)
            res, status = await co.apply_dashboard_decision(actor=actor, approval_id=key,
                action_digest=digest, decision="APPROVE")
            require_equal(status, "ok")
            require_equal(res["method"], S.DASHBOARD_METHOD)
            request = await store.get_request(key)
            require_equal(request["state"], S.APPROVED)
            require_equal(request["decided_device"], None)
            require_equal(request["decided_session"], actor.session_id)
            calls = []
            result, status = await co.execute_approved(key, _executor(calls))
            require_equal(status, "ok")
            require_equal(calls[0]["action_digest"], digest)
            require_equal(calls[0]["tool"], tool)
            require_equal((await store.get_request(key))["state"], S.CONSUMED)
            attempt = (await store.attempts_for(result["execution_id"]))[0]
            require_equal(attempt["claim_authorization_method"], S.DASHBOARD_METHOD)
            require_equal(attempt["device_id"], None)
            require_equal(attempt["claim_browser_session_id"], actor.session_id)
            require_equal(attempt["claim_action_digest"], digest)
            require_equal(attempt["claim_app_attest_key_id"], None)
            require_equal(attempt["status"], "SUCCEEDED")
            require_equal((await co.execute_approved(key, _executor(calls)))[1], "not_approved")
            require_equal(len(calls), 1)


async def t_wrong_principal_digest_tool_core_and_unverified_actor_are_rejected():
    async with _world() as (_, store, cp, co, sessions, session, actor):
        key, digest = await _request(cp)
        for candidate, sent_digest, expected in (
            (replace(actor, principal="somebody-else"), digest, "principal_mismatch"),
            (BrowserActor("local-owner", "no-session"), digest, "unknown_browser_session"),
            (actor, "f" * 64, "action_digest_mismatch"),
            ({"principal": actor.principal, "session_id": actor.session_id}, digest, "browser_actor_required")):
            require_equal((await co.apply_dashboard_decision(actor=candidate, approval_id=key,
                action_digest=sent_digest, decision="APPROVE"))[1], expected)
        for tool in ("note_write", "codex_task", "payment_execute", "agent_run_resume"):
            blocked, dg = await _request(cp, tool)
            require_equal((await co.apply_dashboard_decision(actor=actor, approval_id=blocked,
                action_digest=dg, decision="APPROVE"))[1], "dashboard_tool_forbidden")
            require_equal((await store.get_request(blocked))["state"], S.PENDING)
        other = C.MobileApprovalControlPlane(store, cp.mac_key, "another-core")
        require_equal((await other.submit_dashboard_decision(actor=actor, approval_id=key,
            action_digest=digest, decision="APPROVE"))[1], "wrong_core_instance")
        require_equal((await store.get_request(key))["state"], S.PENDING)
        require_equal(await store.list_devices(), [])


async def t_revoked_expired_and_foreign_sessions_cannot_decide():
    for case in ("revoked", "expired", "principal", "core"):
        async with _world() as (_, store, cp, co, sessions, session, actor):
            key, digest = await _request(cp)
            if case == "revoked":
                await sessions.revoke(actor.session_id, principal=actor.principal)
                expected = "browser_session_revoked"
            else:
                column, value, expected = {
                    "expired": ("expires_at", time.time() - 1, "browser_session_expired"),
                    "principal": ("principal", "different-owner", "principal_mismatch"),
                    "core": ("core_instance_id", "different-core", "wrong_core_instance"),
                }[case]
                # Expiry stays above created_at to respect the real table contract.
                if case == "expired":
                    await store._run(lambda: store._conn.execute(
                        "UPDATE browser_sessions SET created_at=? WHERE session_id=?",
                        (value - 100, actor.session_id)))
                await store._run(lambda: store._conn.execute(
                    f"UPDATE browser_sessions SET {column}=? WHERE session_id=?", (value, actor.session_id)))
            require_equal((await co.apply_dashboard_decision(actor=actor, approval_id=key,
                action_digest=digest, decision="APPROVE"))[1], expected)
            require_equal((await store.get_request(key))["state"], S.PENDING)


async def t_request_expiry_tampered_action_and_legacy_core_binding_fail_closed():
    for field, value, expected in (
        ("expires_at", 1.0, "request_expired"),
        ("task", "different action", "action_digest_mismatch"),
        ("request_core_instance_id", "", "wrong_core_instance")):
        async with _world() as (_, store, cp, co, sessions, session, actor):
            key, digest = await _request(cp)
            await store._run(lambda: store._conn.execute(
                f"UPDATE approval_requests SET {field}=? WHERE approval_id=?", (value, key)))
            require_equal((await co.apply_dashboard_decision(actor=actor, approval_id=key,
                action_digest=digest, decision="APPROVE"))[1], expected)
            require_equal((await co.execute_approved(key, _executor([])))[1], "not_approved")


async def t_dashboard_denial_is_terminal_even_for_a_previously_issued_faceid_challenge():
    async with _world() as (_, store, cp, co, sessions, session, actor):
        device = await H.enroll_attested(cp)
        key, digest = await _request(cp)
        challenge, status = await cp.issue_challenge(approval_id=key, device_id=device.device_id)
        require_equal(status, "ok")
        signed = H.sign_decision(device, P.b64d(challenge["payload_b64"]))
        require_equal((await co.apply_dashboard_decision(actor=actor, approval_id=key,
            action_digest=digest, decision="DENY"))[1], "ok")
        require_equal((await co.apply_mobile_decision(**signed))[1], "not_pending")
        require_equal((await co.apply_dashboard_decision(actor=actor, approval_id=key,
            action_digest=digest, decision="APPROVE"))[1], "not_pending")
        require_equal((await store.get_request(key))["state"], S.DENIED)
        calls = []
        require_equal((await co.execute_approved(key, _executor(calls)))[1], "not_approved")
        require_equal(calls, [])


async def t_faceid_and_dashboard_have_one_atomic_decision_winner():
    for browser_decision, mobile_decision in (("APPROVE", "APPROVE"), ("DENY", "APPROVE"),
                                               ("APPROVE", "DENY")):
        for _ in range(4):
            async with _world() as (path, store, cp, co, sessions, session, actor):
                second, cp2, co2, _sessions = await _wire(path, co.approver)
                try:
                    device = await H.enroll_attested(cp)
                    key, digest = await _request(cp)
                    ch, _status = await cp.issue_challenge(approval_id=key, device_id=device.device_id)
                    signed = H.sign_decision(device, P.b64d(ch["payload_b64"]), decision=mobile_decision)
                    barrier = threading.Barrier(2)
                    dashboard_commit = store._commit_dashboard_decision
                    mobile_commit = second._commit_decision
                    def dashboard(*args):
                        barrier.wait(timeout=10)
                        return dashboard_commit(*args)
                    def mobile(*args):
                        barrier.wait(timeout=10)
                        return mobile_commit(*args)
                    with patch.object(store, "_commit_dashboard_decision", dashboard), \
                            patch.object(second, "_commit_decision", mobile):
                        results = await asyncio.gather(co.apply_dashboard_decision(actor=actor,
                            approval_id=key, action_digest=digest, decision=browser_decision),
                            co2.apply_mobile_decision(**signed))
                    require_equal(sum(status == "ok" for _, status in results), 1)
                    require(all(status in {"ok", "not_pending"} for _, status in results), str(results))
                    winner = next(result for result, status in results if status == "ok")
                    row = await store.get_request(key)
                    require_equal(row["state"], S.APPROVED if winner["decision"] == "APPROVE" else S.DENIED)
                    events = [e for e in await store.audit_events() if e["approval_id"] == key
                              and e["event"] in {"state_APPROVED", "state_DENIED"}]
                    require_equal(len(events), 1)
                    calls = []
                    await co.execute_approved(key, _executor(calls))
                    await co2.execute_approved(key, _executor(calls))
                    require_equal(len(calls), int(winner["decision"] == "APPROVE"))
                finally:
                    await second.close()


async def t_revocation_after_approval_and_after_claim_prevents_execution():
    for when in ("approved", "claimed"):
        async with _world() as (_, store, cp, co, sessions, session, actor):
            key, digest = await _request(cp)
            await co.apply_dashboard_decision(actor=actor, approval_id=key,
                action_digest=digest, decision="APPROVE")
            begin = store.begin_external_execution
            async def revoke_then_begin(**kwargs):
                await sessions.revoke(actor.session_id, principal=actor.principal)
                return await begin(**kwargs)
            calls = []
            if when == "approved":
                await sessions.revoke(actor.session_id, principal=actor.principal)
                result, status = await co.execute_approved(key, _executor(calls))
            else:
                with patch.object(store, "begin_external_execution", revoke_then_begin):
                    result, status = await co.execute_approved(key, _executor(calls))
                require_equal((await store.get_request(key))["state"], S.FAILED)
            require_equal(status, "browser_session_revoked")
            require_equal(calls, [])


async def t_fabricated_or_stale_typed_authorization_cannot_claim_or_cross_boundary():
    async with _world() as (_, store, cp, co, sessions, session, actor):
        from solvio.security.mobile_approval import execution as X
        key, digest = await _request(cp)
        await co.apply_dashboard_decision(actor=actor, approval_id=key,
            action_digest=digest, decision="APPROVE")
        auth, why = await store.dashboard_execution_authorization(key, cp.core_instance_id)
        require_equal(why, None)
        eid = X.execution_id_for(cp.core_instance_id, key)
        kwargs = dict(approval_id=key, device_id=None, execution_id=eid,
            capability="agent_task_research", semantics=X.semantics_for("agent_task_research"),
            idempotency_key=X.idempotency_key_for(eid, "agent_task_research"), owner="test",
            core_instance_id=cp.core_instance_id)
        for changed in (replace(auth, principal="other"), replace(auth, session_binding="fake"),
                        replace(auth, action_digest="0" * 64)):
            require_equal((await store.claim_execution_attempt(identities=changed, **kwargs))[0], None)
        aid, status = await store.claim_execution_attempt(identities=auth, **kwargs)
        require_equal(status, "ok")
        await sessions.revoke(actor.session_id, principal=actor.principal)
        require_equal(await store.begin_external_execution(attempt_id=aid, identities=auth),
                      "browser_session_revoked")
        require_equal((await store.attempts_for(eid))[0]["status"], X.ABANDONED)


async def t_simultaneous_execution_and_restart_never_duplicate_the_approved_task():
    async with _world() as (path, store, cp, co, sessions, session, actor):
        key, digest = await _request(cp)
        await co.apply_dashboard_decision(actor=actor, approval_id=key,
            action_digest=digest, decision="APPROVE")
        calls = []
        await asyncio.gather(co.execute_approved(key, _executor(calls)),
                             co.execute_approved(key, _executor(calls)))
        require_equal(len(calls), 1)
        second, _cp, reopened, _sessions = await _wire(path)
        try:
            require_equal((await reopened.execute_approved(key, _executor(calls)))[1], "not_approved")
            require_equal(len(calls), 1)
        finally:
            await second.close()


async def t_claim_rechecks_session_and_boundary_rechecks_expiry():
    from solvio.security.mobile_approval import execution as X
    async with _world() as (_, store, cp, co, sessions, session, actor):
        key, digest = await _request(cp)
        await co.apply_dashboard_decision(actor=actor, approval_id=key,
            action_digest=digest, decision="APPROVE")
        auth, _ = await store.dashboard_execution_authorization(key, cp.core_instance_id)
        eid = X.execution_id_for(cp.core_instance_id, key)
        kwargs = dict(approval_id=key, device_id=None, execution_id=eid,
            capability=auth.tool, semantics=X.semantics_for(auth.tool),
            idempotency_key=X.idempotency_key_for(eid, auth.tool), owner="test",
            core_instance_id=cp.core_instance_id, identities=auth)
        for changes in ({"capability": "note_write"}, {"execution_id": "invented"},
                        {"idempotency_key": "invented"}, {"semantics": X.READ_ONLY}):
            require_equal((await store.claim_execution_attempt(**dict(kwargs, **changes)))[0], None)
        await sessions.revoke(actor.session_id, principal=actor.principal)
        require_equal((await store.claim_execution_attempt(**kwargs))[1], "browser_session_revoked")
        require_equal(await store.attempts_for(eid), [])
    for expire in ("request", "session"):
        async with _world() as (_, store, cp, co, sessions, session, actor):
            key, digest = await _request(cp)
            await co.apply_dashboard_decision(actor=actor, approval_id=key,
                action_digest=digest, decision="APPROVE")
            begin = store.begin_external_execution
            async def expire_then_begin(**kwargs):
                if expire == "request":
                    await store._run(lambda: store._conn.execute(
                        "UPDATE approval_requests SET expires_at=1 WHERE approval_id=?", (key,)))
                else:
                    await store._run(lambda: store._conn.execute(
                        "UPDATE browser_sessions SET created_at=1,expires_at=2 WHERE session_id=?",
                        (actor.session_id,)))
                return await begin(**kwargs)
            calls = []
            with patch.object(store, "begin_external_execution", expire_then_begin):
                require_equal((await co.execute_approved(key, _executor(calls)))[1],
                              "request_expired" if expire == "request" else "browser_session_expired")
            require_equal(calls, [])
            require_equal((await store.get_request(key))["state"], S.FAILED)


async def t_receipt_is_historical_verified_task_proof_not_a_new_authorization():
    for method in (S.DASHBOARD_METHOD, "face_id"):
        async with _world() as (_, store, cp, co, sessions, session, actor):
            key, digest = await _request(cp)
            require_equal((await cp.task_authorization_receipt(key))[0], None)
            if method == S.DASHBOARD_METHOD:
                await co.apply_dashboard_decision(actor=actor, approval_id=key,
                    action_digest=digest, decision="APPROVE")
                reference = actor.session_id
            else:
                device = await H.enroll_attested(cp)
                ch, _ = await cp.issue_challenge(approval_id=key, device_id=device.device_id)
                signed = H.sign_decision(device, P.b64d(ch["payload_b64"]))
                await co.apply_mobile_decision(**signed)
                reference = device.device_id
            require_equal((await cp.task_authorization_receipt(key))[0], None)
            await co.execute_approved(key, _executor([]))
            receipt, status = await cp.task_authorization_receipt(key)
            require_equal(status, "ok")
            require_equal(receipt["method"], method)
            require_equal(receipt["reference"], "approval:" + key)
            require_equal(receipt["session_id"] if method == S.DASHBOARD_METHOD else receipt["device_id"], reference)
            require_equal(receipt["authorizer"], "local-owner")
            require_equal(receipt["action_digest"], digest)
            require_equal(receipt["status"], "SUCCEEDED")
            await sessions.revoke(actor.session_id, principal=actor.principal)
            require_equal((await cp.task_authorization_receipt(key))[0], receipt)
            require_equal((await store.task_authorization_receipt(key, "wrong-core"))[1],
                          "wrong_core_instance")
            await store._run(lambda: store._conn.execute(
                "UPDATE execution_attempts SET claim_action_digest=NULL WHERE approval_id=?", (key,)))
            require_equal((await cp.task_authorization_receipt(key))[0], None)


async def t_task_start_receipt_requires_current_authority_and_is_not_a_success_receipt():
    from solvio.security.mobile_approval import execution as X
    for method in (S.DASHBOARD_METHOD, "face_id"):
        async with _world() as (_, store, cp, co, sessions, session, actor):
            key, digest = await _request(cp)
            eid = X.execution_id_for(cp.core_instance_id, key)
            require_equal((await cp.task_start_claim_receipt(key, eid))[0], None)
            if method == S.DASHBOARD_METHOD:
                await co.apply_dashboard_decision(actor=actor, approval_id=key,
                    action_digest=digest, decision="APPROVE")
            else:
                device = await H.enroll_attested(cp)
                ch, _ = await cp.issue_challenge(approval_id=key, device_id=device.device_id)
                await co.apply_mobile_decision(**H.sign_decision(device, P.b64d(ch["payload_b64"])))
            require_equal((await cp.task_start_claim_receipt(key, eid))[0], None)
            collected = []
            async def execute(action):
                receipt, status = await cp.task_start_claim_receipt(key, action["execution_id"])
                require_equal(status, "ok")
                require_equal(receipt["purpose"], "task_start_authorization")
                require_equal(receipt["status"], X.EXTERNAL_PENDING)
                require_equal(receipt["executed_at"], None)
                require_equal(receipt["reference"], "approval:" + key)
                require_equal(receipt["method"], method)
                require_equal(receipt["action_digest"], digest)
                require_equal((await cp.task_authorization_receipt(key))[0], None)
                require_equal((await cp.task_start_claim_receipt(key, "wrong-execution"))[0], None)
                collected.append(receipt)
                if method == S.DASHBOARD_METHOD:
                    await sessions.revoke(actor.session_id, principal=actor.principal)
                else:
                    await cp.revoke_device(device.device_id, reason="isolated test")
                require_equal((await cp.task_start_claim_receipt(key, eid))[0], None,
                              "stale authority still authorized task creation")
                return True, {"test_only": True}
            require_equal((await co.execute_approved(key, execute))[1], "ok")
            require_equal(len(collected), 1)
            require_equal((await cp.task_start_claim_receipt(key, eid))[0], None)
            require_equal((await cp.task_authorization_receipt(key))[1], "ok")


async def t_partial_schema_is_rejected_readonly_and_migration_does_not_invent_authority():
    import sqlite3
    with tempfile.TemporaryDirectory(prefix="solvio-dashboard-schema-") as path:
        store, cp, _co, _sessions = await _wire(path)
        key, _digest = await _request(cp)
        db = store.path
        await store.close()
        with sqlite3.connect(db) as connection:
            connection.execute("ALTER TABLE approval_requests DROP COLUMN request_core_instance_id")
            connection.execute("ALTER TABLE execution_attempts DROP COLUMN claim_browser_session_binding")
        readonly = S.ApprovalControlStore(db, read_only=True)
        try:
            try:
                await readonly.open()
            except S.StateMigrationRequired:
                pass
            else:
                raise AssertionError("partial authority schema accepted read-only")
        finally:
            await readonly.close()
        migrated = S.ApprovalControlStore(db)
        await migrated.open()
        try:
            req = await migrated.get_request(key)
            require_equal(req["request_core_instance_id"], "", "migration invented request authority")
            require_equal(req["decision_method"], "")
            require_equal(req["state"], S.PENDING)
        finally:
            await migrated.close()


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
