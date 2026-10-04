"""Real temporary ledger contracts; no network, credentials or service dispatch."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import action_contract as C, store as S, requirements as RQ
from solvio.agent_runtime.task_authority import TaskAuthority, VerifiedTaskReceipt


def event(action_id="meeting"):
    return {"action_id": action_id, "service": "calendar", "operation": "create", "account": "default",
        "target": {"calendar_id": "primary"}, "payload": {"summary": "Fahrradwerkstatt",
        "start": "2026-09-15T09:00:00+02:00", "end": "2026-09-15T10:00:00+02:00",
        "all_day": False, "description": "Fahrrad abholen", "location": "Werkstatt"}}


def raises(callback, error=ValueError):
    try:
        callback()
    except error:
        return
    raise AssertionError("expected refusal")


@contextmanager
def world(actions=None):
    with tempfile.TemporaryDirectory(prefix="solvio-action-contract-") as directory:
        ledger = S.AgentRunLedger(os.path.join(directory, "agent.sqlite3"))
        # This unit seam intentionally exercises the existing ledger vocabulary;
        # public action-scope authentication is tested by the integration suite.
        task = ledger.create_task(objective="Lege den Fahrradtermin im angegebenen Kalender an.",
            scope="research", created_origin="trusted_dashboard", created_principal="owner")
        run = ledger.create_run(task_id=task.task_id)
        authority = TaskAuthority(ledger)
        C.initialize(ledger)
        prepared = C.prepare(C.from_payload({"actions": actions or [event()]}), task_id=task.task_id, run_id=run.run_id)
        with ledger._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            C.record_prepared(connection, prepared, now=1.0)
        authority.issue(task.task_id, run.run_id, receipt=VerifiedTaskReceipt("dashboard_session", "browser:test-action", "owner"),
                        capabilities=(prepared.capability_grant,))
        ledger.transition(run.run_id, S.PLANNING)
        ledger.transition(run.run_id, S.RUNNING)
        bound = C.for_run(ledger, run.run_id)
        yield ledger, authority, task, run, bound


def router_claim(ledger, authority, bound, *, action_id="meeting", seq=1):
    step = ledger.create_step(run_id=bound.run_id, seq=seq, kind="capability", capability=C.CAPABILITY)
    ledger.update_step(step.step_id, state="running", started=True)
    allowed = authority.claim_step(bound.grant_reference, step.step_id, C.CAPABILITY,
        bound.action_arguments(action_id), C.VERSION, task_id=bound.task_id, run_id=bound.run_id)
    require(allowed.allowed)
    return step.step_id


def complete(ledger, authority, bound, *, confirmed=True, seq=1, action_id="meeting"):
    sid = router_claim(ledger, authority, bound, action_id=action_id, seq=seq)
    require(C.claim_action(ledger, bound, action_id, sid))
    receipt = {"native_id": "event123", "observed": {"confirmed": confirmed, "summary": "Fahrradwerkstatt"}}
    result = C.record_outcome(ledger, bound, action_id, sid, status="completed", receipt=receipt)
    return sid, receipt, result


def requirements(ledger, task, *, read=False, ids=("meeting",)):
    raw = {RQ.ASK: [{"id": i, "text": "Kalender lesen"} for i in ids] if read else [],
           RQ.ACTION: [] if read else [{"id": i, "text": "Termin anlegen"} for i in ids], RQ.UNCLEAR: [],
           "belege": {"mindestens": 0}}
    bound = RQ.validate(raw, objective=task.objective)
    require(ledger.bind_requirements(task.task_id, json.dumps(bound)))


def t_wire_has_exact_keys_bounded_actions_and_immutable_copies():
    payload = {"actions": [event()]}
    request = C.from_payload(payload)
    payload["actions"][0]["payload"]["summary"] = "Mutated"
    extracted = request.actions
    extracted[0]["target"]["calendar_id"] = "other"
    require_equal(request.actions[0], event())
    require_equal(C.validate_request(request), request)
    require_equal(C.canonical_request(request.descriptor), {"actions": [event()]})
    for bad in ({}, {"actions": []}, {"actions": [event() for _ in range(6)]},
                {"actions": [event(), event()]}, {"actions": [event()], "authority": True}):
        raises(lambda bad=bad: C.from_payload(bad))
    for changes in ({"action_id": "has-dash"}, {"action_id": "x" * 17}, {"account": "https://foreign.example"},
                    {"service": "shell"}, {"operation": "arbitrary"}, {"grant": "invented"}):
        raises(lambda changes=changes: C.from_payload({"actions": [dict(event(), **changes)]}))


def t_calendar_requires_explicit_resources_absolute_times_and_exact_payload():
    for change in ({"start": "tomorrow"}, {"start": "2026-09-15T09:00:00"},
                   {"end": "2026-09-14T10:00:00+02:00"}, {"all_day": True},
                   {"all_day": 1}, {"url": "https://foreign.example"}):
        action = event()
        action["payload"].update(change)
        raises(lambda: C.from_payload({"actions": [action]}))
    for target in ({}, {"calendar_id": ""}, {"calendar_id": "primary", "event_id": "not-for-create"}):
        raises(lambda target=target: C.from_payload({"actions": [dict(event(), target=target)]}))
    action = event()
    action["payload"].update(start="2026-09-15T00:00:00+02:00", end="2026-09-16T00:00:00+02:00", all_day=True)
    require_equal(C.from_payload({"actions": [action]}).actions[0], action)


def t_all_advertised_service_operations_have_closed_forms():
    base = event()
    samples = [base, dict(base, operation="update", target={"calendar_id": "primary", "event_id": "evt1"}),
        dict(base, operation="delete", target={"calendar_id": "primary", "event_id": "evt1"}, payload={}),
        dict(base, operation="list", payload={"start": base["payload"]["start"], "end": base["payload"]["end"]}),
        dict(base, service="gmail", operation="create_draft", target={"mailbox": "me", "to": "friend@example.invalid", "reply_to_message": ""},
             payload={"subject": "Termin", "body": "Bis morgen.", "thread_id": "", "in_reply_to": ""}),
        dict(base, service="gmail", operation="send_draft", target={"mailbox": "me", "draft_id": "draft1"},
             payload={"to": "friend@example.invalid", "subject": "Termin", "body": "Bis morgen."}),
        dict(base, service="gmail", operation="search", target={"mailbox": "me"}, payload={"query": "Fahrrad", "limit": 3}),
        dict(base, service="ha", operation="set_state", target={"entity_id": "light.room"}, payload={"state": "on"}),
        dict(base, service="ha", operation="set_brightness", target={"entity_id": "light.room"}, payload={"brightness_pct": 45}),
        dict(base, service="portal", operation="status", target={"portal_id": "studio", "session_id": "session1"}, payload={})]
    for sample in samples:
        require_equal(C.from_payload({"actions": [sample]}).actions[0], sample)
        changed = deepcopy(sample)
        changed["payload"]["secret"] = "do-not-accept-extra-fields"
        raises(lambda changed=changed: C.from_payload({"actions": [changed]}))
    bad = deepcopy(samples[4])
    bad["target"]["to"] = "friend@example.invalid\nBcc:other@example.invalid"
    raises(lambda: C.from_payload({"actions": [bad]}))
    bad = deepcopy(samples[8])
    bad["payload"]["brightness_pct"] = True
    raises(lambda: C.from_payload({"actions": [bad]}))


def t_preparation_is_idempotent_same_ledger_and_binds_content_to_task_run():
    with world() as (ledger, authority, task, run, bound):
        again = C.prepare(C.from_payload({"actions": [event()]}), task_id=task.task_id, run_id=run.run_id)
        require_equal(replace(bound, grant_reference=""), again)
        with ledger._open() as connection:
            C.record_prepared(connection, again, now=2.0)
            require_equal(connection.execute("SELECT count(*) FROM agent_action_contracts").fetchone()[0], 1)
        checked = C.check_prepared(ledger, task_id=task.task_id, run_id=run.run_id, entries=(bound.capability_grant,))
        require_equal(checked, again)
        changed = event()
        changed["payload"]["summary"] = "Other meeting"
        other = C.prepare(C.from_payload({"actions": [changed]}), task_id=task.task_id, run_id=run.run_id)
        require(other.contract_digest != bound.contract_digest)
        with ledger._open() as connection:
            raises(lambda: C.record_prepared(connection, other, now=2.0))
        require_equal(C.get_action(ledger, run.run_id, arguments=bound.action_arguments("meeting")), event())


def t_record_prepared_joins_callers_transaction_and_rolls_back():
    with world() as (ledger, authority, task, run, bound):
        other_run = ledger.create_run(task_id=task.task_id)
        other_task = ledger.create_task(objective="Zweiter Kalenderauftrag ohne Wirkung.", scope="research",
            created_origin="trusted_dashboard", created_principal="owner")
        other_run = ledger.create_run(task_id=other_task.task_id)
        other = C.prepare(C.from_payload({"actions": [event()]}), task_id=other_task.task_id, run_id=other_run.run_id)
        try:
            with ledger._open() as connection:
                connection.execute("BEGIN IMMEDIATE")
                C.record_prepared(connection, other, now=2.0)
                raise RuntimeError("synthetic crash")
        except RuntimeError:
            pass
        with ledger._open() as connection:
            require_equal(connection.execute("SELECT count(*) FROM agent_action_contracts").fetchone()[0], 1)


def t_changed_resource_or_extra_arguments_cannot_reach_an_action():
    with world() as (ledger, authority, task, run, bound):
        for args in (dict(bound.action_arguments("meeting"), target="another"),
                     dict(bound.action_arguments("meeting"), action_id="unknown"),
                     dict(bound.action_arguments("meeting"), contract_digest="0" * 64)):
            raises(lambda args=args: C.get_action(ledger, run.run_id, arguments=args))
        with ledger._open() as connection:
            changed = C.from_payload({"actions": [dict(event(), account="other")]})
            connection.execute("UPDATE agent_action_contracts SET request_json=?", (changed._payload_json,))
        raises(lambda: C.for_run(ledger, run.run_id))


def t_effect_claim_requires_current_grant_and_real_router_claim():
    with world() as (ledger, authority, task, run, bound):
        step = ledger.create_step(run_id=run.run_id, seq=1, kind="capability", capability=C.CAPABILITY)
        ledger.update_step(step.step_id, state="running", started=True)
        raises(lambda: C.claim_action(ledger, bound, "meeting", step.step_id))
        sid = router_claim(ledger, authority, bound, seq=2)
        authority.revoke(bound.grant_reference, "owner:withdrawn")
        raises(lambda: C.claim_action(ledger, bound, "meeting", sid))
        with ledger._open() as connection:
            require_equal(connection.execute("SELECT count(*) FROM agent_action_claims").fetchone()[0], 0)


def t_concurrent_claims_have_one_winner_even_with_distinct_plan_steps():
    with world() as (ledger, authority, task, run, bound):
        ids = [router_claim(ledger, authority, bound, seq=seq) for seq in (1, 2)]
        barrier = threading.Barrier(2)
        def claim(sid):
            fresh = S.AgentRunLedger(ledger.path)
            barrier.wait()
            return C.claim_action(fresh, bound, "meeting", sid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ids))
        require_equal(sorted(results), [False, True])


def t_complete_and_unknown_never_grant_second_action_attempt():
    for status in ("completed", "unknown"):
        with world() as (ledger, authority, task, run, bound):
            sid = router_claim(ledger, authority, bound)
            require(C.claim_action(ledger, bound, "meeting", sid))
            receipt = {"native_id": "event123", "observed": {"confirmed": True}} if status == "completed" else None
            first = C.record_outcome(ledger, bound, "meeting", sid, status=status, receipt=receipt)
            require_equal(C.record_outcome(ledger, bound, "meeting", sid, status=status, receipt=receipt), first)
            next_sid = router_claim(ledger, authority, bound, seq=8)
            require(not C.claim_action(S.AgentRunLedger(ledger.path), bound, "meeting", next_sid))
            raises(lambda: C.record_outcome(ledger, bound, "meeting", sid, status="completed",
                receipt={"native_id": "different", "observed": {"confirmed": True}}))


def may_retry(ledger, bound, step_id, *, arguments=None, task_id=None, run_id=None,
              capability=C.CAPABILITY, version=C.VERSION):
    with ledger._open() as connection:
        connection.execute("BEGIN")
        previous = connection.execute("SELECT * FROM agent_steps WHERE step_id=?", (step_id,)).fetchone()
        return C.can_retry_step(connection, ledger, previous, task_id=task_id or bound.task_id,
            run_id=run_id or bound.run_id, capability=capability,
            arguments=arguments or bound.action_arguments("meeting"), version=version)


def non_dispatch(ledger, authority, bound, *, seq=1):
    sid = router_claim(ledger, authority, bound, seq=seq)
    require(C.claim_action(ledger, bound, "meeting", sid))
    receipt = C.record_outcome(ledger, bound, "meeting", sid, status="not_dispatched")
    return sid, receipt


def t_auth_non_dispatch_retries_same_action_and_preserves_previous_receipt():
    with world() as (ledger, authority, task, run, bound):
        first_sid, first_receipt = non_dispatch(ledger, authority, bound)
        original = C.receipt_at(ledger, run.run_id, first_sid)
        require(may_retry(ledger, bound, first_sid))
        require(not C.claim_action(ledger, bound, "meeting", first_sid), "same dispatch was reused")
        second_sid = router_claim(ledger, authority, bound, seq=2)
        require(C.claim_action(ledger, bound, "meeting", second_sid))
        require_equal(C.receipt_at(ledger, run.run_id, first_sid), original)
        require(not may_retry(ledger, bound, first_sid), "past non-dispatch reopened a running attempt")
        C.record_outcome(ledger, bound, "meeting", second_sid, status="unknown")
        require_equal([r["status"] for r in C.read_receipts(ledger, run.run_id)], ["not_dispatched", "unknown"])
        require(not may_retry(ledger, bound, first_sid))
        require(not may_retry(ledger, bound, second_sid))
        third_sid = router_claim(ledger, authority, bound, seq=3)
        require(not C.claim_action(ledger, bound, "meeting", third_sid))
        require_equal(C.receipt_at(S.AgentRunLedger(ledger.path), run.run_id, first_sid), original)


def t_repeated_proven_non_dispatch_preserves_all_attempts_then_success_blocks():
    with world() as (ledger, authority, task, run, bound):
        first_sid, _ = non_dispatch(ledger, authority, bound)
        second_sid, _ = non_dispatch(ledger, authority, bound, seq=2)
        require(may_retry(ledger, bound, first_sid), "canonical archived non-dispatch cannot be checked")
        require(may_retry(ledger, bound, second_sid))
        third_sid, _, _ = complete(ledger, authority, bound, seq=3)
        require_equal([r["step_id"] for r in C.read_receipts(ledger, run.run_id)], [first_sid, second_sid, third_sid])
        require(not may_retry(ledger, bound, first_sid))
        require(not may_retry(ledger, bound, second_sid))
        require(not may_retry(ledger, bound, third_sid))


def t_non_dispatch_exception_requires_exact_action_request_previous_step_and_receipt():
    with world([event(), event("other")]) as (ledger, authority, task, run, bound):
        first_sid, _ = non_dispatch(ledger, authority, bound)
        require(may_retry(ledger, bound, first_sid))
        for changes in ({"arguments": bound.action_arguments("other")},
                        {"arguments": dict(bound.action_arguments("meeting"), contract_digest="0" * 64)},
                        {"arguments": dict(bound.action_arguments("meeting"), extra="not-bound")},
                        {"task_id": "at-" + "f" * 16}, {"run_id": "ar-" + "f" * 16},
                        {"capability": "calendar_create"}, {"version": 2}, {"version": True}):
            require(not may_retry(ledger, bound, first_sid, **changes))
        unclaimed_sid = router_claim(ledger, authority, bound, seq=2)
        require(not may_retry(ledger, bound, unclaimed_sid), "receiptless router claim opened retry")
        with ledger._open() as connection:
            row = connection.execute("SELECT * FROM agent_steps WHERE step_id=?", (first_sid,)).fetchone()
            fake = dict(row, seq=900)
            require(not C.can_retry_step(connection, ledger, fake, task_id=bound.task_id, run_id=bound.run_id,
                capability=C.CAPABILITY, arguments=bound.action_arguments("meeting"), version=C.VERSION))
            connection.execute("UPDATE agent_action_claims SET receipt_digest=?", ("0" * 64,))
        require(not may_retry(ledger, bound, first_sid), "noncanonical receipt opened retry")
        require(not C.claim_action(ledger, bound, "meeting", unclaimed_sid))


def t_concurrent_non_dispatch_reattempts_have_one_winner_and_one_archive():
    with world() as (ledger, authority, task, run, bound):
        original_sid, _ = non_dispatch(ledger, authority, bound)
        ids = [router_claim(ledger, authority, bound, seq=seq) for seq in (2, 3)]
        barrier = threading.Barrier(2)
        def claim(sid):
            fresh = S.AgentRunLedger(ledger.path)
            barrier.wait()
            return C.claim_action(fresh, bound, "meeting", sid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            require_equal(sorted(pool.map(claim, ids)), [False, True])
        with ledger._open() as connection:
            require_equal(connection.execute("SELECT count(*) FROM agent_action_attempt_receipts").fetchone()[0], 1)
        require_equal(C.read_receipts(ledger, run.run_id)[0]["step_id"], original_sid)


def t_non_dispatch_retry_rechecks_revocation_and_archive_corruption():
    with world() as (ledger, authority, task, run, bound):
        sid, _ = non_dispatch(ledger, authority, bound)
        authority.revoke(bound.grant_reference, "owner:withdrawn")
        require(not may_retry(ledger, bound, sid))
    with world() as (ledger, authority, task, run, bound):
        first_sid, _ = non_dispatch(ledger, authority, bound)
        second_sid, _ = non_dispatch(ledger, authority, bound, seq=2)
        with ledger._open() as connection:
            connection.execute("UPDATE agent_action_attempt_receipts SET receipt_json='{}'")
        require(not may_retry(ledger, bound, first_sid))
        require(not may_retry(ledger, bound, second_sid))
        raises(lambda: C.read_receipts(ledger, run.run_id))


def t_late_cancel_records_unknown_and_historical_receipt_reads_after_terminal():
    with world() as (ledger, authority, task, run, bound):
        sid = router_claim(ledger, authority, bound)
        require(C.claim_action(ledger, bound, "meeting", sid))
        ledger.transition(run.run_id, S.CANCELLED)
        C.record_outcome(ledger, bound, "meeting", sid, status="unknown")
        require_equal(C.receipt_at(ledger, run.run_id, sid)["status"], "unknown")
        raises(lambda: C.get_action(ledger, run.run_id, arguments=bound.action_arguments("meeting")))


def t_only_confirmed_write_receipt_bound_to_exact_requirement_proves_completion():
    for confirmed in (False, True):
        with world() as (ledger, authority, task, run, bound):
            requirements(ledger, task)
            sid, native, result = complete(ledger, authority, bound, confirmed=confirmed)
            require_equal(C.completion_evidence(ledger, run.run_id), ())
            C.bind_requirement(ledger, run.run_id, "meeting", sid, "meeting")
            C.bind_requirement(ledger, run.run_id, "meeting", sid, "meeting")
            evidence = C.completion_evidence(ledger, run.run_id)
            require_equal(len(evidence), 1 if confirmed else 0)
            if confirmed:
                require_equal(evidence[0].requirement, "meeting")
                require(result["receipt_digest"] in evidence[0].evidence)
            raises(lambda: C.bind_requirement(ledger, run.run_id, "meeting", sid, "other"))


def t_read_operation_receipts_are_never_external_effect_proof():
    action = dict(event(), operation="list", payload={"start": event()["payload"]["start"], "end": event()["payload"]["end"]})
    with world([action]) as (ledger, authority, task, run, bound):
        requirements(ledger, task, read=True)
        sid, _, _ = complete(ledger, authority, bound)
        C.bind_requirement(ledger, run.run_id, "meeting", sid, "meeting")
        require(C.receipt_at(ledger, run.run_id, sid)["read_only"])
        result = C.completion_evidence(ledger, run.run_id)
        require_equal(len(result), 1)
        require(result[0].requirement in {r["id"] for r in RQ.load(ledger.get_task(task.task_id).requirements,
            objective=task.objective)[RQ.ASK]})


def t_persisted_receipt_and_requirements_tampering_are_detected():
    for mutation in ("receipt", "requirements", "step", "requirement_id"):
        with world() as (ledger, authority, task, run, bound):
            requirements(ledger, task)
            sid, native, result = complete(ledger, authority, bound)
            C.bind_requirement(ledger, run.run_id, "meeting", sid, "meeting")
            with ledger._open() as connection:
                if mutation == "receipt":
                    row = connection.execute("SELECT receipt_json FROM agent_action_claims").fetchone()
                    body = json.loads(row[0]); body["native"]["native_id"] = "changed"
                    connection.execute("UPDATE agent_action_claims SET receipt_json=?", (json.dumps(body),))
                elif mutation == "step":
                    connection.execute("UPDATE agent_steps SET dispatch_binding_digest='changed' WHERE step_id=?", (sid,))
                elif mutation == "requirement_id":
                    connection.execute("UPDATE agent_action_claims SET requirement_id='another'")
                else:
                    row = connection.execute("SELECT requirements FROM agent_tasks WHERE task_id=?", (task.task_id,)).fetchone()
                    body = json.loads(row[0]); body[RQ.ACTION][0]["text"] = "Andere Wirkung"
                    connection.execute("UPDATE agent_tasks SET requirements=? WHERE task_id=?", (json.dumps(body), task.task_id))
            raises(lambda: C.completion_evidence(ledger, run.run_id))


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
