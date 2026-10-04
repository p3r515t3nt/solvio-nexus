"""N7 internal service cost boundary; real grants and temporary shared ledger.

No service, account or provider is contacted. Callback observations stand in
for the future Core service adapter; none of these tests grants public access.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import costs as C, cost_dispatch as D, store as S
from solvio.agent_runtime.task_authority import CapabilityGrant, TaskAuthority, VerifiedTaskReceipt
from solvio.specialists import launcher as L

FREE = D.CostQuote(0, C.CostEvidence("included_no_extra_charge", "test:bounded-local-service"))
BOUND = D.CostQuote(600, C.CostEvidence("enforceable_upper_bound", "test:600-cent-cap"))


def invocation(**changes):
    fields = dict(capability="calendar_get_event", version=1, service="calendar-test",
        operation="get_event", arguments={"title": "SYNTHETIC_PRIVATE_APPOINTMENT"},
        resources={"account": "local-test-account", "event_id": "event-one"})
    fields.update(changes)
    return D.ServiceInvocation.bind(**fields)


@contextmanager
def fixture(*, grant=True):
    with tempfile.TemporaryDirectory(prefix="solvio-service-cost-") as directory:
        ledger = S.AgentRunLedger(str(Path(directory) / "agent.sqlite3"))
        task = ledger.create_task(objective="Lies den konkret beauftragten Testtermin",
            scope="research", created_origin="trusted_interactive_app", created_principal="owner:device")
        run = ledger.create_run(task_id=task.task_id)
        ledger.transition(run.run_id, S.PLANNING)
        ledger.transition(run.run_id, S.RUNNING)
        costs = C.CostLedger(ledger)
        costs.configure(task.task_id)
        authority = TaskAuthority(ledger)
        issued = authority.issue(task.task_id, run.run_id,
            receipt=VerifiedTaskReceipt("app_session", "test:verified-task", "owner:device"),
            capabilities=(CapabilityGrant("calendar_get_event", 1),),
            expires_at=time.time() + 3600) if grant else None
        yield ledger, costs, task.task_id, run.run_id, authority, issued


def scope(ledger, task, run, *, operation="service:one", quote=FREE, phase="capability", **kwargs):
    return D.task_cost_scope(ledger, task_id=task, run_id=run, phase=phase,
        operation_id=operation, quote_adapter=lambda *_: quote, **kwargs)


async def completed(_):
    return D.ServiceOutcome("completed", True, receipt_ref="test:service-terminal", data={"found": True})


def t_descriptor_binds_exact_canonical_payload_and_never_holds_it():
    first = invocation(arguments={"b": [1, True], "a": "private-text"})
    same = invocation(arguments={"a": "private-text", "b": [1, True]})
    require_equal(first, same)
    require_equal(first.request_digest, same.request_digest)
    for changed in (
        invocation(arguments={"b": [1, 1], "a": "private-text"}),
        invocation(resources={"event_id": "other"}),
        invocation(version=2), invocation(service="different-service"),
        invocation(operation="search"), invocation(capability="calendar_list_events")):
        require(first.request_digest != changed.request_digest)
    require("private-text" not in repr(first))
    require(not first.matches(arguments={"a": "different"}, resources={}))
    for changed in ({"version": True}, {"arguments": {1: "wrong-key"}},
                    {"resources": {"depth": float("nan")}},
                    {"arguments": {"token": "sk-SYNTHETIC_NOT_A_REAL_KEY_123456789"}}):
        try:
            invocation(**changed)
        except (ValueError, PermissionError):
            pass
        else:
            raise AssertionError("unbounded/credential/noncanonical binding accepted")


async def t_no_scope_or_wrong_phase_or_missing_grant_holds_before_any_callback():
    async def forbidden(*_):
        raise AssertionError("callback without task authority")
    result = await D.dispatch_service(invocation(), forbidden)
    require_equal(result.outcome.reason, "cost_unbounded")
    require(not result.dispatch_started)
    for granted, phase in ((False, "capability"), (True, "plan"), (True, "adaptive_extract")):
        with fixture(grant=granted) as (ledger, costs, task, run, _, _):
            with D.task_cost_scope(ledger, task_id=task, run_id=run, phase=phase,
                    operation_id="blocked", quote_adapter=forbidden):
                result = await D.dispatch_service(invocation(), forbidden)
            require(not result.dispatch_started)
            require_equal(D.invocations(ledger, task), [])
            require_equal(costs.view(task)["counts"], {})


async def t_quote_claim_and_terminal_are_real_typed_service_records_without_cli():
    with fixture() as (ledger, costs, task, run, _, _):
        request = invocation()
        async def quote(service, descriptor):
            require_equal((service, descriptor), (request.service, request))
            require_equal(D.invocations(ledger, task), [])
            await asyncio.sleep(0)
            return FREE
        async def local(descriptor):
            rows = D.invocations(ledger, task)
            require_equal(len(rows), 1)
            require_equal(rows[0]["state"], "claimed")
            require_equal(rows[0]["request_digest"], descriptor.request_digest)
            require_equal(rows[0]["phase"], "capability")
            require_equal(rows[0]["provider"], "calendar-test")
            return await completed(descriptor)
        with D.task_cost_scope(ledger, task_id=task, run_id=run, phase="capability",
                operation_id="service:event", quote_adapter=quote):
            result = await D.dispatch_service(request, local)
        require(result.completed and result.dispatch_started and result.outcome.ok)
        require_equal(result.cost_status, "settled")
        require_equal(costs.view(task)["ai_tool"]["spent_cents"], 0)
        require_equal(D.invocations(ledger, task)[0]["state"], "finished")
        with ledger._open() as connection:
            dump = "\n".join(connection.iterdump())
        require("SYNTHETIC_PRIVATE_APPOINTMENT" not in dump)
        require("local-test-account" not in dump)
        require(not hasattr(result.outcome, "exit_code"))


async def t_unknown_quote_and_login_only_do_not_authorize_service_costs():
    with fixture() as (ledger, costs, task, run, _, _):
        for n, quote in enumerate((D.CostQuote(), D.CostQuote(0,
                C.CostEvidence("subscription_auth", "test:only-auth")))):
            with scope(ledger, task, run, quote=quote, operation=f"unknown:{n}"):
                result = await D.dispatch_service(invocation(), completed)
            require_equal(result.outcome.reason, "cost_unbounded")
            require(not result.dispatch_started)
        require_equal(D.invocations(ledger, task), [])
        require_equal(costs.view(task)["counts"], {"unbounded_cost": 2})


async def t_parallel_service_and_cli_share_the_same_task_budget():
    with fixture() as (ledger, costs, task, run, _, _):
        started, release = asyncio.Event(), asyncio.Event()
        async def running(descriptor):
            started.set()
            await release.wait()
            return await completed(descriptor)
        async def forbidden(*_):
            raise AssertionError("combined task budget bypassed")
        with scope(ledger, task, run, quote=BOUND):
            first = asyncio.create_task(D.dispatch_service(invocation(), running))
            await asyncio.wait_for(started.wait(), 2)
            try:
                second = await D.dispatch_service(invocation(), forbidden)
                require_equal(second.outcome.reason, "cost_approval_required")
                with scope(ledger, task, run, operation="planner:next", phase="plan", quote=BOUND):
                    cli = await D.dispatch("codex", L.Invocation("/never/run", (), timeout=1), "", forbidden)
                require_equal(cli.outcome.reason, "cost_approval_required")
            finally:
                release.set()
                await first
        require_equal(len(D.invocations(ledger, task)), 1)
        require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 600)


async def t_replayed_live_and_completed_claims_never_dispatch_again_after_reopen():
    with fixture() as (ledger, _, task, run, _, _):
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def running(descriptor):
            calls.append(descriptor)
            started.set()
            await release.wait()
            return await completed(descriptor)
        with scope(ledger, task, run):
            first = asyncio.create_task(D.dispatch_service(invocation(), running))
            await asyncio.wait_for(started.wait(), 2)
        try:
            for pending in (True, False):
                if not pending:
                    release.set()
                    await first
                with scope(S.AgentRunLedger(ledger.path), task, run):
                    replay = await D.dispatch_service(invocation(), running)
                require_equal(replay.outcome.reason, "cost_recovery_required")
                require(not replay.dispatch_started)
            require_equal(len(calls), 1)
        finally:
            release.set()
            await first


async def t_revoke_or_terminal_during_quote_is_rechecked_before_physical_claim():
    for action in ("revoke", "cancel", "task_changed"):
        with fixture() as (ledger, costs, task, run, authority, grant):
            async def quote(*_):
                await asyncio.sleep(0)
                if action == "revoke":
                    authority.revoke(grant.reference, "test:owner-revocation")
                elif action == "cancel":
                    ledger.transition(run, S.CANCELLED)
                else:
                    with ledger._open() as connection:
                        connection.execute("UPDATE agent_tasks SET objective=? WHERE task_id=?",
                                           ("Changed authenticated task", task))
                return BOUND
            async def forbidden(_):
                raise AssertionError("stale task authority dispatched")
            with D.task_cost_scope(ledger, task_id=task, run_id=run, phase="capability",
                    operation_id="service:race", quote_adapter=quote):
                result = await D.dispatch_service(invocation(), forbidden)
            require_equal(result.outcome.reason, "cost_recovery_required")
            require(not result.dispatch_started)
            require_equal(D.invocations(ledger, task), [])
            require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 0)


async def t_source_check_failure_is_not_an_unbooked_service_call():
    with fixture() as (ledger, costs, task, run, _, _):
        async def rejected_source():
            await asyncio.sleep(0)
            raise ValueError("owner_source_revoked")
        with scope(ledger, task, run) as current:
            current.source_check = rejected_source
            result = await D.dispatch_service(invocation(), completed)
        require_equal(result.outcome.reason, "cost_recovery_required")
        require_equal(D.invocations(ledger, task), [])
        require_equal(costs.view(task)["counts"], {})


async def t_expired_revoked_or_terminal_task_stops_even_quote_callback():
    for action in ("expired", "revoked", "terminal"):
        with fixture() as (ledger, costs, task, run, authority, grant):
            async def forbidden(*_):
                raise AssertionError("quote or service callback after authority ended")
            with D.task_cost_scope(ledger, task_id=task, run_id=run, phase="capability",
                    operation_id="invalid-task", quote_adapter=forbidden) as current:
                if action == "expired":
                    current.authority.clock = lambda: grant.expires_at
                elif action == "revoked":
                    authority.revoke(grant.reference, "test:revocation")
                else:
                    ledger.transition(run, S.CANCELLED)
                result = await D.dispatch_service(invocation(), forbidden)
            require_equal(result.outcome.reason, "cost_recovery_required")
            require_equal(D.invocations(ledger, task), [])
            require_equal(costs.view(task)["counts"], {})


async def t_unknown_exception_and_cancel_keep_reserves_and_block_new_operation():
    for failure in ("unknown", "exception", "cancel", "invalid_outcome"):
        with fixture() as (ledger, costs, task, run, _, _):
            started, release = asyncio.Event(), asyncio.Event()
            async def unconfirmed(_):
                started.set()
                if failure == "cancel":
                    await release.wait()
                if failure == "exception":
                    raise RuntimeError("test:lost-service-answer")
                if failure == "invalid_outcome":
                    return {"ok": True, "state": "completed"}
                return D.ServiceOutcome(reason="service_answer_missing")
            with scope(ledger, task, run, quote=BOUND):
                first = asyncio.create_task(D.dispatch_service(invocation(), unconfirmed))
                await asyncio.wait_for(started.wait(), 2)
                if failure == "cancel":
                    first.cancel()
                try:
                    result = await first
                except (asyncio.CancelledError, ValueError, RuntimeError):
                    require(failure != "unknown")
                else:
                    require_equal(failure, "unknown")
                    require_equal(result.cost_status, "unknown")
                    require(not result.completed)
            require_equal(D.invocations(ledger, task)[0]["state"], "unknown")
            require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 600)
            with scope(S.AgentRunLedger(ledger.path), task, run, operation="new-operation"):
                retry = await D.dispatch_service(invocation(), completed)
            require_equal(retry.outcome.reason, "cost_recovery_required")


async def t_lost_claim_ownership_is_not_a_new_service_attempt():
    with fixture() as (ledger, costs, task, run, _, _):
        request = invocation()
        with scope(ledger, task, run, quote=BOUND) as current:
            ordinal, ident = current.next_invocation()
            reserved = costs.reserve(task, ident, 600, route=request.service, evidence=BOUND.evidence)
            require(D._claim(current, reserved, ordinal, ident, request.service, request.request_digest))
        D._ACTIVE_CLAIMS.discard((ledger.path, reserved.reservation_id))
        with scope(S.AgentRunLedger(ledger.path), task, run, operation="after-restart"):
            result = await D.dispatch_service(request, completed)
        require_equal(result.outcome.reason, "cost_recovery_required")
        require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 600)
        require_equal(len(D.invocations(ledger, task)), 1)


async def t_proven_non_dispatch_releases_money_but_is_not_replay_authority():
    with fixture() as (ledger, costs, task, run, _, _):
        async def not_sent(_):
            return D.ServiceOutcome("not_dispatched", reason="account_missing",
                                    receipt_ref="test:request-never-sent")
        with scope(ledger, task, run, quote=BOUND):
            result = await D.dispatch_service(invocation(), not_sent)
        require(not result.dispatch_started and not result.completed)
        require_equal(result.cost_status, "released")
        require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 0)
        require_equal(D.invocations(ledger, task)[0]["state"], "not_dispatched")
        with scope(ledger, task, run, quote=BOUND):
            replay = await D.dispatch_service(invocation(), completed)
        require_equal(replay.outcome.reason, "cost_recovery_required")
        for state in ("not_dispatched", "completed"):
            try:
                D.ServiceOutcome(state)
            except ValueError:
                pass
            else:
                raise AssertionError("execution evidence was not required")


async def t_measured_settlement_and_failed_terminal_remain_separate_from_success():
    with fixture() as (ledger, costs, task, run, _, _):
        async def settlement(service, descriptor, outcome):
            require_equal(service, descriptor.service)
            require_equal(outcome.state, "completed")
            await asyncio.sleep(0)
            return D.CostSettlement(400, C.CostEvidence("actual_charge", "test:service-receipt"))
        async def declined(_):
            return D.ServiceOutcome("completed", reason="remote_rejected", receipt_ref="test:terminal-rejection")
        with scope(ledger, task, run, quote=BOUND, settlement_adapter=settlement):
            result = await D.dispatch_service(invocation(), declined)
        require(result.completed and result.dispatch_started)
        require(not result.outcome.ok)
        require_equal(result.cost_status, "settled")
        require_equal(costs.view(task)["ai_tool"]["spent_cents"], 400)
        with scope(ledger, task, run, quote=BOUND, operation="next-service"):
            second = await D.dispatch_service(invocation(), completed)
        require_equal(second.outcome.reason, "cost_approval_required")


async def t_invalid_accounting_keeps_bound_and_settlement_overrun_stops_followup():
    for actual, kind in ((1, "included_no_extra_charge"), (700, "actual_charge")):
        with fixture() as (ledger, costs, task, run, _, _):
            with scope(ledger, task, run, quote=BOUND,
                    settlement_adapter=lambda *_: D.CostSettlement(actual, C.CostEvidence(kind, "test:receipt"))):
                result = await D.dispatch_service(invocation(), completed)
            require(result.completed)
            if actual == 1:
                require_equal(result.cost_status, "reserved")
                require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 600)
            else:
                require_equal(costs.view(task)["ai_tool"]["spent_cents"], 700)
                with scope(ledger, task, run, operation="after-overrun"):
                    blocked = await D.dispatch_service(invocation(), completed)
                require(not blocked.dispatch_started)
                require_equal(blocked.outcome.reason, "cost_recovery_required")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
