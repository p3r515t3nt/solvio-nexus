"""Physische Aufrufe am echten Abo-Dispatch; Anbieter nur lokale Testprozesse."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import costs as C, cost_dispatch as D, store as S
from solvio.specialists import launcher as L, providers as P, subscription as U

FREE = D.CostQuote(0, C.CostEvidence("free_local", "test:local-executable"))
BOUND = D.CostQuote(600, C.CostEvidence("enforceable_upper_bound", "test:hard-cap"))
AUTH = P.ProviderStatus("codex", True, auth="chatgpt", billing_mode="subscription")
INVOCATION = L.Invocation("/synthetic/codex", (), cwd="/", timeout=1)


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory(prefix="solvio-cost-dispatch-") as directory:
        ledger = S.AgentRunLedger(os.path.join(directory, "agent.sqlite3"))
        task = ledger.create_task(objective="Untersuche und bearbeite diesen Auftrag",
            scope="research", created_origin="trusted_interactive_app", created_principal="owner:device")
        run = ledger.create_run(task_id=task.task_id)
        ledger.transition(run.run_id, S.PLANNING)
        ledger.transition(run.run_id, S.RUNNING)
        costs = C.CostLedger(ledger)
        costs.configure(task.task_id)
        yield ledger, costs, task.task_id, run.run_id, Path(directory)


def scope(ledger, task, run, *, phase="plan", operation_id="plan:one", quote=FREE, **kw):
    return D.task_cost_scope(ledger, task_id=task, run_id=run, phase=phase,
        operation_id=operation_id, quote_adapter=(lambda *_: quote) if quote else None, **kw)


async def completed(invocation, prompt):
    return L.Outcome(True, text="Antwort", exit_code=0, process_started=True)


def t_auth_refusal_creates_no_cost_reservation_and_never_starts_provider():
    async def go():
        with fixture() as (ledger, costs, task, run, _):
            denied = P.ProviderStatus("codex", False, "logged_out")
            runner = AsyncMock(side_effect=AssertionError("unexpected provider"))
            with scope(ledger, task, run), patch.object(P, "codex_status", AsyncMock(return_value=denied)):
                result = await P.run_subscription("codex", INVOCATION, "Plan", runner=runner)
            require_equal(result.reason, "logged_out")
            require_equal(costs.view(task)["counts"], {})
            require_equal(D.invocations(ledger, task), [])
            require(not result.dispatch_started)
    asyncio.run(go())


def t_default_unknown_and_subscription_login_alone_stop_before_real_dispatch():
    async def go():
        with fixture() as (ledger, costs, task, run, _):
            runner = AsyncMock(side_effect=AssertionError("provider started without cost proof"))
            quotes = (None, D.CostQuote(0, C.CostEvidence("subscription_auth", "auth:chatgpt")))
            with patch.object(P, "codex_status", AsyncMock(return_value=AUTH)):
                for index, quote in enumerate(quotes):
                    with scope(ledger, task, run, operation_id="attempt:" + str(index), quote=quote):
                        result = await P.run_subscription("codex", INVOCATION, "Plan", runner=runner)
                    require_equal(result.reason, "cost_unbounded")
                    require(not result.dispatch_started)
            require_equal(costs.view(task)["counts"], {"unbounded_cost": 2})
            require_equal(D.invocations(ledger, task), [])
    asyncio.run(go())


def t_each_physical_plan_repair_assessment_and_specialist_is_separately_recorded():
    async def go():
        with fixture() as (ledger, costs, task, run, _):
            with patch.object(P, "codex_status", AsyncMock(return_value=AUTH)):
                with scope(ledger, task, run, phase="plan", operation_id="plan:event-one"):
                    await P.run_subscription("codex", INVOCATION, "Plan", runner=completed)
                    await P.run_subscription("codex", INVOCATION, "Repariere das JSON", runner=completed)
                for phase in ("assessment", "specialist"):
                    with scope(ledger, task, run, phase=phase, operation_id=phase + ":event-one"):
                        await P.run_subscription("codex", INVOCATION, phase, runner=completed)
            records = D.invocations(ledger, task)
            require_equal(len(records), 4)
            require_equal(len({row["invocation_id"] for row in records}), 4)
            require_equal(sorted(row["ordinal"] for row in records if row["phase"] == "plan"), [1, 2])
            require_equal({row["phase"] for row in records}, {"plan", "assessment", "specialist"})
            require_equal(costs.view(task)["counts"], {"settled": 4})
            require("Repariere" not in json.dumps(records), "Prompt im Kostenbuch")
            require(all(row["state"] == "finished" for row in records))
    asyncio.run(go())


def t_parallel_physical_calls_share_ten_euro_cap_and_preserve_live_claim():
    async def go():
        with fixture() as (ledger, costs, task, run, _):
            began, release = asyncio.Event(), asyncio.Event()

            async def running(*_):
                began.set()
                await release.wait()
                return await completed(None, None)

            with patch.object(P, "codex_status", AsyncMock(return_value=AUTH)):
                with scope(ledger, task, run, quote=BOUND):
                    first = asyncio.create_task(P.run_subscription("codex", INVOCATION, "one", runner=running))
                    await asyncio.wait_for(began.wait(), 2)
                    second = await P.run_subscription("codex", INVOCATION, "two", runner=completed)
                    require_equal(second.reason, "cost_approval_required")
                    require(not second.dispatch_started)
                    release.set()
                    require((await first).ok)
            require_equal(len(D.invocations(ledger, task)), 1)
            require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 600)
    asyncio.run(go())


def t_async_quote_and_measured_settlement_replace_bound_and_exact_ten_euros_asks():
    async def go():
        with fixture() as (ledger, costs, task, run, _):
            async def quote(*_):
                return D.CostQuote(999, C.CostEvidence("enforceable_upper_bound", "test:999-cent-cap"))

            async def settlement(*_):
                return D.CostSettlement(400, C.CostEvidence("actual_charge", "test:receipt"))

            with patch.object(P, "codex_status", AsyncMock(return_value=AUTH)):
                with D.task_cost_scope(ledger, task_id=task, run_id=run, phase="plan", operation_id="first",
                                       quote_adapter=quote, settlement_adapter=settlement):
                    first = await P.run_subscription("codex", INVOCATION, "Plan", runner=completed)
                require_equal(first.cost_status, "settled")
                with scope(ledger, task, run, phase="assessment", operation_id="second", quote=BOUND):
                    second = await P.run_subscription("codex", INVOCATION, "Pruefe", runner=completed)
                require_equal(second.reason, "cost_approval_required")
            require_equal(costs.view(task)["ai_tool"]["spent_cents"], 400)
    asyncio.run(go())


def t_a_replayed_reservation_does_not_start_the_same_physical_call_again():
    async def go():
        with fixture() as (ledger, _, task, run, _):
            runner = AsyncMock(side_effect=completed)
            with patch.object(P, "codex_status", AsyncMock(return_value=AUTH)):
                with scope(ledger, task, run):
                    first = await P.run_subscription("codex", INVOCATION, "Plan", runner=runner)
                with scope(S.AgentRunLedger(ledger.path), task, run):
                    second = await P.run_subscription("codex", INVOCATION, "Plan", runner=runner)
            require(first.ok)
            require_equal(second.reason, "cost_recovery_required")
            require_equal(runner.await_count, 1)
    asyncio.run(go())


def t_an_open_replayed_reservation_cannot_dispatch_while_first_call_is_still_running():
    async def go():
        with fixture() as (ledger, costs, task, run, _):
            began, release = asyncio.Event(), asyncio.Event()

            async def running(*_):
                began.set()
                await release.wait()
                return await completed(None, None)

            forbidden = AsyncMock(side_effect=AssertionError("offene Reserve wurde erneut ausgefuehrt"))
            with patch.object(P, "codex_status", AsyncMock(return_value=AUTH)):
                with scope(ledger, task, run):
                    first = asyncio.create_task(P.run_subscription("codex", INVOCATION, "Plan", runner=running))
                    await asyncio.wait_for(began.wait(), 2)
                    try:
                        with scope(ledger, task, run):
                            replay = await P.run_subscription("codex", INVOCATION, "Plan", runner=forbidden)
                        require_equal(replay.reason, "cost_recovery_required")
                        require_equal(costs.view(task)["counts"], {"reserved": 1})
                    finally:
                        release.set()
                        await first
            require_equal(len(D.invocations(ledger, task)), 1)
    asyncio.run(go())


def t_a_new_operation_after_predispatch_unknown_cost_can_use_new_measured_proof():
    async def go():
        with fixture() as (ledger, _, task, run, _):
            with patch.object(P, "codex_status", AsyncMock(return_value=AUTH)):
                with scope(ledger, task, run, quote=None, operation_id="before-owner-settings"):
                    first = await P.run_subscription("codex", INVOCATION, "Plan", runner=completed)
                with scope(ledger, task, run, operation_id="after-owner-settings"):
                    second = await P.run_subscription("codex", INVOCATION, "Plan", runner=completed)
            require_equal(first.reason, "cost_unbounded")
            require(second.ok)
            require_equal(len(D.invocations(ledger, task)), 1)
    asyncio.run(go())


def t_unknown_provider_outcome_blocks_new_operation_ids_and_retains_the_reserve():
    async def go():
        with fixture() as (ledger, costs, task, run, _):
            timeout = AsyncMock(return_value=L.Outcome(False, reason="timeout", process_started=True))
            with patch.object(P, "codex_status", AsyncMock(return_value=AUTH)):
                with scope(ledger, task, run, quote=BOUND):
                    result = await P.run_subscription("codex", INVOCATION, "Plan", runner=timeout)
                with scope(S.AgentRunLedger(ledger.path), task, run, operation_id="new-id", quote=FREE):
                    repeat = await P.run_subscription("codex", INVOCATION, "Plan", runner=completed)
            require(result.dispatch_started)
            require_equal(result.cost_status, "unknown")
            require_equal(repeat.reason, "cost_recovery_required")
            require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 600)
            require_equal(D.invocations(ledger, task)[0]["state"], "unknown")
    asyncio.run(go())


def t_orphaned_claim_after_restart_blocks_even_a_new_scope():
    async def go():
        with fixture() as (ledger, costs, task, run, _):
            with scope(ledger, task, run, quote=BOUND) as current:
                ordinal, invocation_id = current.next_invocation()
                decision = costs.reserve(task, invocation_id, 600, route="codex", evidence=BOUND.evidence)
                require(D._claim(current, decision, ordinal, invocation_id, "codex", "test-request-digest"))
            D._ACTIVE_CLAIMS.discard((ledger.path, decision.reservation_id))
            with scope(S.AgentRunLedger(ledger.path), task, run, operation_id="restart-attempt"), \
                    patch.object(P, "codex_status", AsyncMock(return_value=AUTH)):
                result = await P.run_subscription("codex", INVOCATION, "Plan", runner=completed)
            require_equal(result.reason, "cost_recovery_required")
            require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 600)
    asyncio.run(go())


def t_native_spawn_failure_is_the_only_proven_nonstart_and_releases_reserve():
    async def go():
        with fixture() as (ledger, costs, task, run, directory):
            missing = L.Invocation(str(directory / "not-installed"), (), cwd=str(directory), timeout=1)
            with scope(ledger, task, run, quote=BOUND), patch.object(P, "codex_status", AsyncMock(return_value=AUTH)):
                result = await P.run_subscription("codex", missing, "Plan")
            require_equal(result.reason, "spawn_failed")
            require(not result.dispatch_started)
            require_equal(result.cost_status, "released")
            require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 0)
            require_equal(D.invocations(ledger, task)[0]["state"], "not_dispatched")
    asyncio.run(go())


def t_failure_word_without_measured_nonstart_does_not_release_money():
    async def go():
        with fixture() as (ledger, costs, task, run, _):
            ambiguous = AsyncMock(return_value=L.Outcome(False, reason="spawn_failed"))
            with scope(ledger, task, run, quote=BOUND), patch.object(P, "codex_status", AsyncMock(return_value=AUTH)):
                result = await P.run_subscription("codex", INVOCATION, "Plan", runner=ambiguous)
            require(result.dispatch_started)
            require_equal(result.cost_status, "unknown")
            require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 600)
    asyncio.run(go())


def t_cancellation_terminates_real_local_child_and_keeps_unknown_cost_claim():
    async def go():
        with fixture() as (ledger, costs, task, run, directory):
            started = directory / "pid"
            script = directory / "fake_provider.py"
            script.write_text("import os,time\nfrom pathlib import Path\nPath(" + repr(str(started)) +
                              ").write_text(str(os.getpid()))\ntime.sleep(30)\n")
            invocation = L.Invocation(sys.executable, (str(script),), cwd=str(directory), timeout=5)
            with scope(ledger, task, run, quote=BOUND), patch.object(P, "codex_status", AsyncMock(return_value=AUTH)):
                running = asyncio.create_task(P.run_subscription("codex", invocation, "Plan"))
                for _ in range(200):
                    if started.exists():
                        break
                    await asyncio.sleep(.01)
                require(started.exists(), "Testprozess startete nicht")
                pid = int(started.read_text())
                running.cancel()
                try:
                    await running
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("Cancellation wurde verschluckt")
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise AssertionError("Testprozess laeuft nach Cancellation weiter")
            require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 600)
            require_equal(D.invocations(ledger, task)[0]["state"], "unknown")
    asyncio.run(go())


def t_real_subscription_transport_propagates_cost_receipt_from_local_executable():
    async def go():
        with fixture() as (ledger, costs, task, run, directory):
            script = directory / "cli.py"
            reply = '\n'.join(json.dumps(item) for item in (
                {"type": "item.completed", "item": {"type": "agent_message", "text": "Antwort"}},
                {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}}))
            script.write_text("import sys\nsys.stdin.read()\nprint(" + repr(reply) + ")\n")
            invocation = L.Invocation(sys.executable, (str(script), "-"), cwd=str(directory), timeout=2)
            transport = U.SubscriptionTransport("codex")
            with scope(ledger, task, run), patch.object(P, "codex_status", AsyncMock(return_value=AUTH)), \
                    patch.object(P, "codex_invocation", return_value=invocation):
                result = await transport({"input": [{"role": "user", "content": "Plan"}]})
            require(result["ok"])
            require_equal(result["tokens"], 7)
            require_equal(result["cost_status"], "settled")
            require(result["cost_reservation_id"])
            require_equal(costs.view(task)["counts"], {"settled": 1})
    asyncio.run(go())


def t_fast_mode_is_disabled_in_child_even_when_parent_explicitly_enables_it():
    with patch.dict(os.environ, {"CLAUDE_CODE_DISABLE_FAST_MODE": "0", "DISABLE_AUTOUPDATER": "0"}):
        require_equal(L.child_environment()["CLAUDE_CODE_DISABLE_FAST_MODE"], "1")
        require_equal(L.child_environment()["DISABLE_AUTOUPDATER"], "1")
        require_equal(os.environ["CLAUDE_CODE_DISABLE_FAST_MODE"], "0")
        require_equal(os.environ["DISABLE_AUTOUPDATER"], "0")


def t_no_task_scope_preserves_existing_non_task_cli_path():
    async def go():
        require_equal(D.current_scope(), None)
        with patch.object(P, "codex_status", AsyncMock(return_value=AUTH)):
            result = await P.run_subscription("codex", INVOCATION, "Beratung", runner=completed)
        require(result.ok)
        require_equal(result.cost_reservation_id, "")
    asyncio.run(go())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
