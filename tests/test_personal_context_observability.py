"""Content-free personal-context diagnostics; temporary stores, no model calls."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from structlog.testing import capture_logs

import test_agent_personal_context as T
from _guard import enforce_assertions, require, require_equal
from solvio.agent_runtime import personal_context as PC

enforce_assertions()
EVENT = "agent.personal_context_read"
PRIVATE = "PRIVATE_SENTINEL_NEVER_LOG_THIS"


def observations(logs):
    rows = [row for row in logs if row.get("event") == EVENT]
    require(rows, "personal context outcome has no structured diagnostic")
    for row in rows:
        require_equal(set(row), {"event", "log_level", "status", "reason", "purpose",
            "elapsed_ms", "selected_records", "returned_records", "truncated"})
        require(row["elapsed_ms"] is None or
                type(row["elapsed_ms"]) is int and row["elapsed_ms"] >= 0)
        require(PRIVATE not in json.dumps(row))
    return rows


def t_current_unavailable_causes_are_distinguished_without_exception_text():
    async def probe():
        async with T.world() as w:
            for failure in (RuntimeError(PRIVATE), TimeoutError(PRIVATE)):
                async def broken(*args, **kwargs):
                    raise failure
                with capture_logs() as logs, patch.object(w.memory, "search", broken):
                    result = await PC.for_call(w.memory, query=PRIVATE)
                row, = observations(logs)
                require_equal(T.data(result)["status"], "unavailable")
                require_equal((row["status"], row["reason"]), ("unavailable", "lookup_error"))
                require(PRIVATE not in result)
            await w.memory.close()
            with capture_logs() as logs:
                result = await PC.for_call(w.memory, query=PRIVATE)
            row, = observations(logs)
            require_equal((row["status"], row["reason"]), ("unavailable", "store_unavailable"))
    asyncio.run(probe())


def t_deadline_logs_elapsed_and_drains_without_swallowing_caller_cancel():
    async def probe():
        for cancelled in (False, True):
            async with T.world() as w:
                entered, drained = asyncio.Event(), asyncio.Event()
                async def blocked(*args, **kwargs):
                    entered.set()
                    try:
                        await asyncio.Future()
                    finally:
                        drained.set()
                with capture_logs() as logs, patch.object(w.memory, "search", blocked), \
                     patch.object(PC, "SEARCH_TIMEOUT_SECONDS", .02):
                    pending = asyncio.create_task(PC.for_call(w.memory, query=PRIVATE))
                    await asyncio.wait_for(entered.wait(), 1)
                    if cancelled:
                        pending.cancel()
                        try:
                            await pending
                        except asyncio.CancelledError:
                            pass
                        else:
                            raise AssertionError("caller cancellation swallowed")
                    else:
                        require_equal(T.data(await pending)["status"], "unavailable")
                require(drained.is_set(), "lookup worker not drained")
                row, = observations(logs)
                require_equal((row["status"], row["reason"]),
                    ("cancelled", "cancelled") if cancelled else ("unavailable", "deadline"))
                if not cancelled:
                    require(row["elapsed_ms"] >= 10)
        async with T.world() as w:
            async def self_cancelled(*args, **kwargs):
                raise asyncio.CancelledError()
            with capture_logs() as logs, patch.object(w.memory, "search", self_cancelled):
                try:
                    await PC.for_call(w.memory, query=PRIVATE)
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("lookup cancellation swallowed")
            row, = observations(logs)
            require_equal((row["status"], row["reason"]), ("cancelled", "cancelled"))
    asyncio.run(probe())


def t_success_empty_and_output_trimming_log_counts_without_record_material():
    async def probe():
        async with T.world() as w:
            rid = await w.memory.semantic.remember(T.record(content=T.OLD + " " + PRIVATE))
            for limit in (2000, 700):
                with capture_logs() as logs:
                    result = await PC.for_call(w.memory, query=T.GOAL, history=PRIVATE, max_chars=limit)
                row, = observations(logs)
                require_equal((row["status"], row["reason"]), ("available", "completed"))
                require_equal(row["selected_records"], 1)
                require_equal(row["returned_records"], len(T.data(result)["treffer"]))
                require_equal(row["truncated"], limit == 700)
                require(rid not in json.dumps(row) and T.OLD not in json.dumps(row))
            require(await w.memory.semantic.forget(rid, reason="user_request"))
            with capture_logs() as logs:
                result = await PC.for_call(w.memory, query=T.GOAL)
            row, = observations(logs)
            require_equal((row["status"], row["returned_records"]), ("available", 0))
    asyncio.run(probe())


def t_degraded_retrieval_stays_distinct_from_empty_or_unavailable():
    async def probe():
        async with T.world() as w:
            await w.memory.semantic.remember(T.record())
            hits = await w.memory.search(T.GOAL)
            async def degraded(*args, **kwargs):
                w.memory.model_load_error = PRIVATE
                return hits
            with capture_logs() as logs, patch.object(w.memory, "search", degraded):
                result = await PC.for_call(w.memory, query=T.GOAL)
            row, = observations(logs)
            require_equal((row["status"], row["reason"], row["returned_records"]),
                          ("degraded", "completed", 1))
            require_equal(T.data(result)["status"], "degraded")
            require(PRIVATE not in result)
    asyncio.run(probe())


def t_no_attached_memory_preserves_history_and_has_no_search():
    async def probe():
        with capture_logs() as logs:
            require_equal(await PC.for_call(None, query=PRIVATE, history=PRIVATE), PRIVATE)
        row, = observations(logs)
        require_equal((row["status"], row["reason"]), ("skipped", "not_attached"))
    asyncio.run(probe())


def t_real_planner_and_specialist_seams_supply_only_static_purposes():
    async def probe():
        async with T.world() as w:
            await w.memory.semantic.remember(T.record())
            with capture_logs() as logs:
                orch, _, _ = await w.planned()
                await orch.tick()
            rows = observations(logs)
            require_equal([row["purpose"] for row in rows], ["task_planner", "task_specialist"])
            require(all(row["returned_records"] == 1 for row in rows))
            with capture_logs() as logs:
                await PC.for_call(w.memory, query=T.GOAL, purpose=PRIVATE)
            row, = observations(logs)
            require_equal(row["purpose"], "unspecified")
    asyncio.run(probe())


def t_diagnostic_clock_and_logger_failures_do_not_change_success():
    async def probe():
        async with T.world() as w:
            rid = await w.memory.semantic.remember(T.record())
            with capture_logs() as logs, patch.object(PC, "monotonic", side_effect=RuntimeError(PRIVATE)):
                result = await PC.for_call(w.memory, query=T.GOAL)
            row, = observations(logs)
            require_equal(row["elapsed_ms"], None)
            require_equal([r["id"] for r in T.data(result)["treffer"]], [rid])
            with patch.object(PC.log, "info", side_effect=RuntimeError(PRIVATE)):
                result = await PC.for_call(w.memory, query=T.GOAL)
            require_equal([r["id"] for r in T.data(result)["treffer"]], [rid])
    asyncio.run(probe())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
