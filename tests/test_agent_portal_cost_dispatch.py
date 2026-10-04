"""N7: actual Step -> Router -> registered local portal handler and cost books.

Only temporary agent/vault paths. The original vault sees an absent local file;
no credentials, keychain, portal worker, provider or account are used.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import costs as C, cost_dispatch as D, store as S, steps as ST
from solvio.agent_runtime.task_authority import CapabilityGrant, TaskAuthority, VerifiedTaskReceipt
from solvio.agent_runtime.task_start_service import TaskStepAuthority
from solvio.capabilities import portal as P
from solvio.capabilities import router as R
from solvio.capabilities.envelope import CapabilityOutcome as OUT
from solvio.capabilities.policy import OriginClass
from solvio.portal.vault import PortalVault
from solvio.secret_vault.context import current as secret_context


class NoPortalClient:
    def __getattr__(self, name):
        raise AssertionError("portal_list reached a portal worker: " + name)


@contextmanager
def world(*, configure=True, register=True, granted=True):
    # The state dir is THIS world, also when a suite runs outside scripts/run_tests.py
    # (which isolates per suite): measured 19.09.2026 — direct suite runs wrote 101
    # orphan run folders (observation/helper artifacts of the fake worker) into the
    # owner's ~/.solvio/agent_runs through artifact_root() (DEBT-0278 class).
    with tempfile.TemporaryDirectory(prefix="solvio-portal-cost-") as directory, \
            patch.dict(os.environ, {"SOLVIO_STATE_DIR": directory}):
        ledger = S.AgentRunLedger(str(Path(directory) / "agent.sqlite3"))
        task = ledger.create_task(objective="Nenne die lokal konfigurierten Portale.",
            scope="research", created_origin="trusted_interactive_app", created_principal="owner:device")
        run = ledger.create_run(task_id=task.task_id)
        ledger.transition(run.run_id, S.PLANNING)
        ledger.transition(run.run_id, S.RUNNING)
        costs = C.CostLedger(ledger)
        if configure:
            costs.configure(task.task_id)
        authority = TaskAuthority(ledger)
        grant = authority.issue(task.task_id, run.run_id,
            receipt=VerifiedTaskReceipt("app_session", "test:verified-portal-task", "owner:device"),
            capabilities=(CapabilityGrant("portal_list", 1),) if granted else (),
            expires_at=time.time() + 3600)
        step = ledger.create_step(run_id=run.run_id, seq=1, kind="capability", capability="portal_list")
        ledger.update_step(step.step_id, state="running")
        events = []
        router = R.CapabilityRouter(recorder=events.append)
        router._task_authority = authority
        vault = PortalVault(str(Path(directory) / "empty-vault"))
        capabilities = P.PortalCapabilities(NoPortalClient(), vault)
        if register:
            P.register(router, capabilities)
        w = SimpleNamespace(ledger=ledger, costs=costs, task=task.task_id, run=run.run_id,
            step=step.step_id, router=router, authority=authority, grant=grant, vault=vault,
            capabilities=capabilities, events=events, accesses=[])
        w.binding = TaskStepAuthority(grant.reference, w.task, w.run, w.step)
        original_exists = os.path.exists
        def observed_exists(path):
            if path == vault.path:
                claims = D.invocations(ledger, w.task)
                current = ledger.steps_for_run(w.run)[0]
                w.accesses.append((claims, current.dispatch_claimed_at, secret_context().origin))
            return original_exists(path)
        with patch("solvio.portal.vault.os.path.exists", observed_exists):
            yield w


async def execute(w, *, task_step=True, arguments=None, cancel_token=None):
    return await w.router.execute("portal_list", arguments if arguments is not None else {},
        trust=ST.agent_trust(w.task, "2026-09-10"), origin=OriginClass.BACKGROUND_AUTOMATION,
        principal=ST.agent_principal(w.run), commanded=True,
        task_step=w.binding if task_step else None, cancel_token=cancel_token)


def scoped(w, **kwargs):
    return D.task_cost_scope(w.ledger, task_id=w.task, run_id=w.run,
        phase="capability", operation_id=w.step, **kwargs)


@contextmanager
def quote_gate(change):
    """Pause the actual dispatcher's quote await; preserve its registration quote."""
    dispatch = D.dispatch_service
    async def intercepted(invocation, runner, *, quote_adapter=None):
        async def delayed(service, descriptor):
            quote = quote_adapter(service, descriptor)
            await change()
            return quote
        return await dispatch(invocation, runner, quote_adapter=delayed)
    with patch.object(D, "dispatch_service", intercepted):
        yield


def require_unstarted(w):
    require_equal(w.accesses, [], "the canonical vault must not have been touched")
    require_equal(w.ledger.steps_for_run(w.run)[0].dispatch_claimed_at, None)


async def t_real_step_router_local_handler_share_existing_claim_and_cost_books():
    with world() as w:
        outcome = await ST.execute_capability(w.router, name="portal_list", arguments={}, sources={},
            run_id=w.run, task_id=w.task, when="2026-09-10", task_step=w.binding)
        require_equal(outcome.state, "succeeded")
        require_equal(len(outcome.data["portale"]), len(P.BINDINGS))
        require(w.accesses, "the real handler must reach the real local vault")
        for claims, step_claimed_at, origin in w.accesses:
            require_equal(len(claims), 1)
            require_equal(claims[0]["state"], "claimed")
            require_equal((claims[0]["task_id"], claims[0]["run_id"], claims[0]["operation_id"]),
                          (w.task, w.run, w.step))
            require(step_claimed_at is not None, "cost and effect claims must precede local I/O")
            require_equal(origin, OriginClass.BACKGROUND_AUTOMATION)
        claims = D.invocations(S.AgentRunLedger(w.ledger.path), w.task)
        require_equal(claims[0]["provider"], "local.portal-catalog")
        require_equal(claims[0]["state"], "finished")
        require_equal(w.costs.view(w.task)["counts"], {"settled": 1})
        require_equal(w.costs.view(w.task)["ai_tool"]["spent_cents"], 0)
        require_equal([e.phase for e in w.events], ["requested", "started", "finished"])
        with w.ledger._open() as connection:
            text = "\n".join(connection.iterdump())
        require(w.vault.path not in text, "costs persist only resource digests")
        require(outcome.call_id)
        require("solvio:portal-list-local:v1" in text)


async def t_a_same_name_handler_or_injected_vault_does_not_inherit_zero_cost():
    for replacement in ("handler", "vault", "subclass", "vault_method"):
        with world(register=False) as w:
            def forbidden(*_):
                raise AssertionError("unpriced implementation ran")
            if replacement == "handler":
                w.router.register(P.SPECS["portal_list"], forbidden)
            else:
                if replacement == "vault":
                    w.capabilities.vault = SimpleNamespace(has=forbidden)
                elif replacement == "subclass":
                    class OtherPortal(P.PortalCapabilities):
                        pass
                    w.capabilities = OtherPortal(NoPortalClient(), w.vault)
                else:
                    w.vault.has = forbidden
                P.register(w.router, w.capabilities)
            result = await execute(w)
            require_equal((result.outcome, result.reason), (OUT.REJECTED_BY_POLICY, "cost_unbounded"))
            require_unstarted(w)
            require_equal(D.invocations(w.ledger, w.task), [])


async def t_unpriced_classification_is_stopped_before_its_first_await():
    with world() as w:
        calls = []
        async def classify(_):
            calls.append("service-classification")
            raise AssertionError("service read before its price claim")
        w.router._classifiers["portal_list"] = classify
        result = await execute(w)
        require_equal(result.reason, "cost_unbounded")
        require_equal(calls, [])
        require_unstarted(w)
        require_equal(D.invocations(w.ledger, w.task), [])


async def t_even_the_resource_resolver_and_price_methods_must_be_original():
    for name in ("resources", "quote", "execute"):
        with world() as w:
            service = w.router._handlers["portal_list"]
            calls = []
            def replacement(*_):
                calls.append(name)
                raise AssertionError("unreviewed registration method ran before a claim")
            object.__setattr__(service, name, replacement)
            result = await execute(w)
            require_equal(result.reason, "cost_unbounded")
            require_equal(calls, [])
            require_unstarted(w)
            require_equal(D.invocations(w.ledger, w.task), [])


async def t_resources_registration_and_grant_are_rechecked_after_quote_await():
    for change in ("path", "handler", "vault", "classify", "grant"):
        with world() as w:
            async def mutate():
                await asyncio.sleep(0)
                require_unstarted(w)
                if change == "path":
                    w.vault.path += ".changed"
                elif change == "handler":
                    w.router._handlers["portal_list"] = lambda _: {"forged": True}
                elif change == "vault":
                    w.capabilities.vault = PortalVault(w.vault.base_dir + "-other")
                elif change == "classify":
                    w.router._classifiers["portal_list"] = lambda _: None
                else:
                    w.authority.revoke(w.grant.reference, "test:owner-revocation")
            with quote_gate(mutate):
                result = await execute(w)
            require_equal(result.outcome, OUT.REJECTED_BY_POLICY)
            require(result.reason in {"service_binding_drift", "cost_recovery_required"})
            require_unstarted(w)
            require_equal(w.costs.view(w.task)["ai_tool"]["reserved_cents"], 0)
            for row in D.invocations(w.ledger, w.task):
                require_equal(row["state"], "not_dispatched")


async def t_mutating_original_arguments_cannot_change_the_frozen_handler_payload():
    with world() as w:
        arguments = {}
        async def change():
            arguments["portal"] = "injected-after-signature"
        with quote_gate(change):
            result = await execute(w, arguments=arguments)
        require_equal(result.outcome, OUT.SUCCESS)
        require_equal(arguments, {"portal": "injected-after-signature"})
        require(w.accesses)


async def t_source_check_is_preserved_and_revalidated_before_effect_claim():
    with world() as w:
        async def revoke_source():
            await asyncio.sleep(0)
            w.authority.revoke(w.grant.reference, "test:source-revoked")
        with scoped(w) as scope:
            scope.source_check = revoke_source
            result = await execute(w)
        require_equal((result.outcome, result.reason), (OUT.REJECTED_BY_POLICY, "cost_recovery_required"))
        require_unstarted(w)
        require_equal(D.invocations(w.ledger, w.task), [])


async def t_ungranted_or_unconfigured_call_never_consumes_the_effect_claim():
    for configured, granted in ((False, True), (True, False)):
        with world(configure=configured, granted=granted) as w:
            result = await execute(w)
            require_equal(result.outcome, OUT.REJECTED_BY_POLICY)
            require_unstarted(w)
            require_equal(D.invocations(w.ledger, w.task), [])


async def t_static_policy_denial_stays_non_dispatch_without_unknown_record():
    with world() as w:
        with patch.object(R.P, "decide", return_value=SimpleNamespace(
                decision=R.P.Decision.DENY, origin=OriginClass.BACKGROUND_AUTOMATION,
                action_class=R.P.ActionClass.READ_ONLY, reason_code="test:deny", preauthorization_id="")):
            result = await execute(w)
        require_equal((result.outcome, result.reason), (OUT.REJECTED_BY_POLICY, "policy_denied"))
        require_unstarted(w)
        require_equal(D.invocations(w.ledger, w.task), [])


async def t_simultaneous_and_reopened_repeat_never_runs_the_handler_again():
    with world() as w:
        first, second = await asyncio.gather(execute(w), execute(w))
        require_equal(sum(r.outcome is OUT.SUCCESS for r in (first, second)), 1)
        require_equal(len(w.accesses), len(P.BINDINGS))
        w.router._task_authority = TaskAuthority(S.AgentRunLedger(w.ledger.path))
        replay = await execute(w)
        require(replay.outcome is not OUT.SUCCESS)
        require_equal(len(w.accesses), len(P.BINDINGS))
        require_equal(len(D.invocations(w.ledger, w.task)), 1)


async def t_scope_without_step_or_wrong_step_operation_cannot_use_legacy_bypass():
    with world() as w:
        with scoped(w):
            no_step = await execute(w, task_step=False)
        with D.task_cost_scope(w.ledger, task_id=w.task, run_id=w.run,
                phase="capability", operation_id="unrelated-step"):
            wrong_step = await execute(w)
        require_equal((no_step.reason, wrong_step.reason), ("cost_unbounded", "cost_unbounded"))
        require_unstarted(w)
        require_equal(D.invocations(w.ledger, w.task), [])


async def t_legacy_grant_free_handler_keeps_its_old_route_without_new_costs():
    with world(register=False) as w:
        called = []
        async def old(arguments):
            called.append(arguments)
            return {"legacy": True}
        w.router.register(P.SPECS["portal_list"], old)
        result = await execute(w, task_step=False)
        require_equal(result.outcome, OUT.SUCCESS)
        require_equal(called, [{}])
        require_unstarted(w)
        require_equal(D.invocations(w.ledger, w.task), [])


async def t_cancel_during_quote_proves_no_local_dispatch_and_no_effect_claim():
    with world() as w:
        cancel = asyncio.Event()
        async def stop():
            cancel.set()
        with quote_gate(stop):
            result = await execute(w, cancel_token=cancel)
        require_equal(result.outcome, OUT.CANCELLED)
        require_unstarted(w)
        require_equal(w.costs.view(w.task)["ai_tool"]["reserved_cents"], 0)
        require_equal([row["state"] for row in D.invocations(w.ledger, w.task)], ["not_dispatched"])


async def t_exception_after_real_handler_entry_is_unknown_and_cannot_repeat():
    with world() as w:
        accesses = []
        original_exists = os.path.exists
        def unavailable(path):
            if path == w.vault.path:
                accesses.append(path)
                require_equal(D.invocations(w.ledger, w.task)[0]["state"], "claimed")
                require(w.ledger.steps_for_run(w.run)[0].dispatch_claimed_at is not None)
                raise OSError("synthetic filesystem outcome")
            return original_exists(path)
        with patch("solvio.portal.vault.os.path.exists", unavailable):
            result = await execute(w)
            replay = await execute(w)
        require_equal((result.outcome, result.reason), (OUT.RECOVERY_REQUIRED, "service_execution_unknown"))
        require_equal(replay.reason, "cost_recovery_required")
        require_equal(accesses, [w.vault.path])
        require_equal([row["state"] for row in D.invocations(w.ledger, w.task)], ["unknown"])
        require_equal(w.costs.view(w.task)["counts"], {"unknown": 1})


async def t_service_task_cleanup_survives_owner_cancel_and_shutdown_cancel():
    entered, cleanup, release, finished = (asyncio.Event() for _ in range(4))
    children = []
    async def service():
        children.append(asyncio.current_task())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()
            finished.set()
    task = asyncio.create_task(R._run_task_cancellable(service, 5, None))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.wait_for(cleanup.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        require(not task.done(), "a second cancellation must not abandon cleanup")
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("outer cancellation was swallowed")
        require(finished.is_set())
        require(all(child.done() for child in children))
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
