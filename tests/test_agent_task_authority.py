"""Core-interne Grant-Bindung, keine vorgetaeuschte HTTP-Authentifizierung.

Die Receipt-Fixture steht fuer das bereits verifizierte Ergebnis des Eingangs.
App-Attest/Browserauth und die oeffentlichen Startwege werden separat geprueft.
Hier laufen echte Agenten-/Schrittzeilen und SQLite-Transaktionen temporaer.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import store as S, task_authority as A


def task_run(ledger):
    task = ledger.create_task(objective="Schreibe eine Notiz fuer meinen Fahrradtermin",
        scope="research", target_repo="", created_origin="room_voice",
        created_principal="pi-wohnzimmer")
    run = ledger.create_run(task_id=task.task_id)
    ledger.transition(run.run_id, S.PLANNING)
    ledger.transition(run.run_id, S.RUNNING)
    return task, run


@contextmanager
def fixture(*, constraints=None, expires_at=None, method="face_id"):
    with tempfile.TemporaryDirectory(prefix="solvio-task-authority-") as directory:
        ledger = S.AgentRunLedger(os.path.join(directory, "agent.sqlite3"))
        task, run = task_run(ledger)
        clock = [1000.0]
        service = A.TaskAuthority(ledger, clock=lambda: clock[0])
        receipt = A.VerifiedTaskReceipt(method, "verified:receipt-one", "owner:registered-device")
        rules = (A.CapabilityGrant("note_write", 1, constraints or {}),)
        grant = service.issue(task.task_id, run.run_id, receipt=receipt,
                              capabilities=rules, expires_at=expires_at)
        yield service, ledger, task.task_id, run.run_id, grant, clock


def verify(service, task, run, grant, arguments=None, capability="note_write", version=1):
    return service.verify(grant.reference, capability, arguments or {"text": "Fahrradtermin"},
                          version, task_id=task, run_id=run)


def step(ledger, run, *, seq=1, attempt=1, capability="note_write", state="running", kind="capability"):
    row = ledger.create_step(run_id=run, seq=seq, kind=kind, attempt=attempt, capability=capability)
    ledger.update_step(row.step_id, state=state, started=state == "running")
    return row.step_id


def claim(service, task, run, grant, step_id, arguments=None, capability="note_write", version=1):
    return service.claim_step(grant.reference, step_id, capability,
        arguments or {"text": "Fahrradtermin"}, version, task_id=task, run_id=run)


def raises(callback, error=A.GrantError):
    try:
        callback()
    except error:
        return
    raise AssertionError("expected exception")


def t_verified_receipt_methods_bind_without_changing_background_provenance():
    for method in A.RECEIPT_METHODS:
        with fixture(method=method) as (service, ledger, task, run, grant, _):
            require(verify(service, task, run, grant).allowed)
            require_equal(grant.receipt_method, method)
            require_equal(ledger.get_task(task).created_origin, "room_voice")
            require_equal(ledger.get_task(task).created_principal, "pi-wohnzimmer")
            require_equal(service.for_run(run).reference, grant.reference)
            require_equal(service.reference_for_run(run).reference, grant.reference)
            require_equal(ledger.steps_for_run(run), [], "verify erzeugte einen Schritt")


def t_reference_is_only_a_locator_and_does_not_grant_other_task_or_run():
    with fixture() as (service, ledger, task, run, grant, _):
        other_task, other_run = task_run(ledger)
        require_equal(verify(service, other_task.task_id, other_run.run_id, grant).reason,
                      "grant_task_run_mismatch")
        same_task_other_run = ledger.create_run(task_id=task)
        require_equal(verify(service, task, same_task_other_run.run_id, grant).reason,
                      "grant_task_run_mismatch")
        require_equal(service.verify("ag-unknown", "note_write", {}, 1,
                                     task_id=task, run_id=run).reason, "unknown_grant")


def t_scope_objective_repository_principal_and_origin_drift_invalidate_grant():
    for field, changed in (("objective", "Sende stattdessen eine andere Nachricht"),
            ("scope", "build"), ("target_repo", "/another/repository"),
            ("created_principal", "other-device"), ("created_origin", "trusted_interactive_app")):
        with fixture() as (service, ledger, task, run, grant, _):
            with ledger._open() as connection:
                connection.execute(f"UPDATE agent_tasks SET {field}=? WHERE task_id=?", (changed, task))
            require_equal(verify(service, task, run, grant).reason, "task_binding_changed", field)


def t_capability_and_version_are_explicit_no_future_names_or_versions():
    with fixture() as (service, _, task, run, grant, _):
        require(service.active(grant.reference, task_id=task, run_id=run).allowed)
        require_equal(verify(service, task, run, grant, capability=None).reason,
                      "invalid_grant_request", "Auftragsbindung ist keine namenlose Wirkungserlaubnis")
        require_equal(verify(service, task, run, grant, capability="gmail_send_draft").reason,
                      "capability_not_granted")
        require_equal(verify(service, task, run, grant, version=2).reason, "capability_not_granted")
        require_equal(verify(service, task, run, grant, version=True).reason, "invalid_grant_request")


def t_resource_constraints_are_exact_typed_and_can_only_leave_unbound_fields_free():
    constraints = {"target": "owner-notes", "enabled": True, "options": {"tags": ["private"]}}
    with fixture(constraints=constraints) as (service, _, task, run, grant, _):
        arguments = {**constraints, "text": "Beliebiger Text innerhalb der Aufgabe"}
        require(verify(service, task, run, grant, arguments).allowed)
        for changed in ({**arguments, "target": "other-notes"}, {**arguments, "enabled": 1},
                        {**arguments, "options": {"tags": ["private", "public"]}},
                        {key: value for key, value in arguments.items() if key != "target"}):
            require_equal(verify(service, task, run, grant, changed).reason, "resource_binding_changed")


def t_mutating_the_callers_rule_dictionary_does_not_expand_persisted_grant():
    with fixture(constraints={"target": "first"}) as (service, _, task, run, grant, _):
        grant.capabilities[0].constraints["target"] = "second"
        require_equal(verify(service, task, run, grant, {"target": "second"}).reason,
                      "resource_binding_changed")
        require(verify(service, task, run, grant, {"target": "first"}).allowed)


def t_expiry_is_optional_but_explicit_expiry_is_inclusive_at_boundary():
    with fixture(expires_at=1100) as (service, _, task, run, grant, clock):
        clock[0] = 1099.9
        require(verify(service, task, run, grant).allowed)
        clock[0] = 1100.0
        require_equal(verify(service, task, run, grant).reason, "grant_expired")
    with fixture() as (service, _, task, run, grant, clock):
        clock[0] = 10_000_000
        require(verify(service, task, run, grant).allowed)


def t_revocation_is_durable_and_neither_locator_nor_reissue_reactivates_it():
    with fixture() as (service, ledger, task, run, grant, _):
        require(service.revoke(grant.reference, "owner:revoked"))
        require(not service.revoke(grant.reference, "owner:again"))
        reopened = A.TaskAuthority(S.AgentRunLedger(ledger.path))
        require_equal(verify(reopened, task, run, grant).reason, "grant_revoked")
        raises(lambda: reopened.issue(task, run, receipt=A.VerifiedTaskReceipt(
            grant.receipt_method, grant.receipt_reference, grant.authorizer), capabilities=grant.capabilities))


def t_run_terminal_or_task_inactive_invalidates_even_an_unexpired_grant():
    for state in S.TERMINAL_STATES:
        with fixture() as (service, ledger, task, run, grant, _):
            ledger.transition(run, state)
            require_equal(verify(service, task, run, grant).reason, "run_terminal")
    with fixture() as (service, ledger, task, run, grant, _):
        ledger.set_task_state(task, S.TASK_CANCELLED)
        require_equal(verify(service, task, run, grant).reason, "task_inactive")


def t_issue_is_idempotent_and_refuses_receipt_reuse_or_capability_expansion():
    with fixture() as (service, ledger, task, run, grant, _):
        receipt = A.VerifiedTaskReceipt(grant.receipt_method, grant.receipt_reference, grant.authorizer)
        require_equal(service.issue(task, run, receipt=receipt, capabilities=grant.capabilities).reference,
                      grant.reference)
        raises(lambda: service.issue(task, run, receipt=receipt,
            capabilities=(*grant.capabilities, A.CapabilityGrant("gmail_send_draft", 1))))
        other_task, other_run = task_run(ledger)
        raises(lambda: service.issue(other_task.task_id, other_run.run_id, receipt=receipt,
                                     capabilities=grant.capabilities))
        raises(lambda: service.issue(task, run, receipt=A.VerifiedTaskReceipt(
            "dashboard_ok", "other:receipt", "owner:browser"), capabilities=grant.capabilities))


def t_simultaneous_claims_have_one_winner_and_store_the_binding_in_existing_step():
    with fixture() as (service, ledger, task, run, grant, _):
        sid = step(ledger, run)
        other = A.TaskAuthority(S.AgentRunLedger(ledger.path))
        barrier = threading.Barrier(2)

        def worker(authority):
            barrier.wait(timeout=5)
            return claim(authority, task, run, grant, sid)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(worker, (service, other)))
        require_equal(sorted(result.allowed for result in outcomes), [False, True])
        winner = next(result for result in outcomes if result.allowed)
        saved = ledger.get_step(sid)
        require_equal(saved.dispatch_binding_digest, winner.binding_digest)
        require(saved.dispatch_claimed_at is not None)
        require_equal(saved.state, "running", "Claim behauptet einen Ausgang")
        require_equal(saved.finished_at, None)


def t_same_changed_or_restarted_dispatch_is_never_claimed_twice():
    with fixture() as (service, ledger, task, run, grant, _):
        sid = step(ledger, run)
        require(claim(service, task, run, grant, sid).allowed)
        reopened = A.TaskAuthority(S.AgentRunLedger(ledger.path))
        require_equal(claim(reopened, task, run, grant, sid).reason, "step_already_claimed")
        require_equal(claim(reopened, task, run, grant, sid, {"text": "changed"}).reason,
                      "step_dispatch_binding_mismatch")
        ledger.update_step(sid, state="unknown")
        another_attempt = step(ledger, run, seq=1, attempt=2)
        require_equal(claim(reopened, task, run, grant, another_attempt).reason,
                      "step_attempt_already_claimed")


def t_step_wrong_run_kind_capability_or_inactive_state_cannot_claim():
    with fixture() as (service, ledger, task, run, grant, _):
        _, another_run = task_run(ledger)
        candidates = ((step(ledger, another_run.run_id), "step_binding_mismatch"),
            (step(ledger, run, seq=2, kind="specialist"), "step_binding_mismatch"),
            (step(ledger, run, seq=3, capability="other_capability"), "step_binding_mismatch"),
            (step(ledger, run, seq=4, state="pending"), "step_not_running"),
            (step(ledger, run, seq=5, state="succeeded"), "step_not_running"))
        for sid, reason in candidates:
            require_equal(claim(service, task, run, grant, sid).reason, reason)
            require_equal(ledger.get_step(sid).dispatch_binding_digest, "")
        paused = step(ledger, run, seq=6)
        ledger.transition(run, S.WAITING_USER)
        require_equal(claim(service, task, run, grant, paused).reason, "step_not_running")


def t_claim_rechecks_revocation_after_a_successful_read_verification():
    with fixture() as (service, ledger, task, run, grant, _):
        sid = step(ledger, run)
        require(verify(service, task, run, grant).allowed)
        service.revoke(grant.reference, "owner:stop")
        require_equal(claim(service, task, run, grant, sid).reason, "grant_revoked")
        require_equal(ledger.get_step(sid).dispatch_claimed_at, None)


def t_modified_stored_capabilities_do_not_match_the_bound_receipt():
    with fixture() as (service, ledger, task, run, grant, _):
        with ledger._open() as connection:
            connection.execute("UPDATE agent_task_grants SET capabilities=? WHERE reference=?",
                ('[{"name":"gmail_send_draft","version":1,"constraints":{}}]', grant.reference))
        require_equal(verify(service, task, run, grant, capability="gmail_send_draft").reason,
                      "grant_binding_invalid")


def t_invalid_receipts_versions_constraints_and_non_json_values_fail_closed():
    with fixture() as (service, ledger, _, _, _, _):
        task, run = task_run(ledger)
        for receipt in ({"method": "face_id", "reference": "from-body", "authorizer": "owner"},
                        "face_id", None):
            raises(lambda: service.issue(task.task_id, run.run_id, receipt=receipt, capabilities=()))
        for method in ("voice", "model", "transport_credential", ""):
            raises(lambda: A.VerifiedTaskReceipt(method, "receipt:one", "owner:one"))
        for value in (True, 0, -1, "1"):
            raises(lambda: A.CapabilityGrant("note_write", value))
        for value in ({1: "ambiguous-key"}, {"value": float("nan")}, {"value": object()}):
            raises(lambda: A.CapabilityGrant("note_write", 1, value))
        raises(lambda: A.VerifiedTaskReceipt("face_id", "", "owner:one"))
        raises(lambda: A.VerifiedTaskReceipt("face_id", "receipt:one", "has whitespace"))
        receipt = A.VerifiedTaskReceipt("face_id", "receipt:new", "owner:one")
        for expiry in (True, 1000.0, float("inf"), float("nan")):
            raises(lambda: service.issue(task.task_id, run.run_id, receipt=receipt,
                capabilities=(A.CapabilityGrant("note_write", 1),), expires_at=expiry))


def t_claim_transaction_crash_rolls_back_the_claim_and_never_changes_step_outcome():
    with fixture() as (service, ledger, task, run, grant, _):
        sid = step(ledger, run)
        connect = ledger._connect

        class Crash(Exception):
            pass

        class Connection:
            def __init__(self):
                self.inner = connect()

            def __enter__(self):
                self.inner.__enter__()
                return self

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

            def close(self):
                self.inner.close()

            def execute(self, sql, *args):
                result = self.inner.execute(sql, *args)
                if sql.startswith("UPDATE agent_steps SET dispatch_binding_digest="):
                    raise Crash()
                return result

        with patch.object(ledger, "_connect", Connection):
            raises(lambda: claim(service, task, run, grant, sid), Crash)
        reopened = S.AgentRunLedger(ledger.path)
        require_equal(reopened.get_step(sid).dispatch_binding_digest, "")
        require_equal(reopened.get_step(sid).state, "running")
        require(claim(A.TaskAuthority(reopened), task, run, grant, sid).allowed)


def t_existing_ledger_migrates_claim_columns_without_losing_steps_or_new_database():
    with tempfile.TemporaryDirectory(prefix="solvio-grant-migration-") as directory:
        path = os.path.join(directory, "agent.sqlite3")
        old_schema = S.SCHEMA.replace("    dispatch_binding_digest TEXT NOT NULL DEFAULT '',\n", "").replace(
            "    dispatch_claimed_at REAL,\n", "")
        old_columns = {**S._ADDED_COLUMNS, "agent_steps": {}}
        with patch.object(S, "SCHEMA", old_schema), patch.object(S, "_ADDED_COLUMNS", old_columns):
            ledger = S.AgentRunLedger(path)
            task, run = task_run(ledger)
            sid = step(ledger, run.run_id)
        reopened = S.AgentRunLedger(path)
        require_equal(reopened.get_step(sid).dispatch_binding_digest, "")
        require_equal(reopened.get_step(sid).dispatch_claimed_at, None)
        require_equal(reopened.get_task(task.task_id).objective, task.objective)
        A.TaskAuthority(reopened)
        require(reopened.permissions_ok())
        require(set(os.listdir(directory)) <= {"agent.sqlite3", "agent.sqlite3-wal", "agent.sqlite3-shm"})


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
