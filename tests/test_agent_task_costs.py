"""Auftragsweite Kosten: echte SQLite-Transaktionen, nur temporaere Ablagen."""
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
from solvio.agent_runtime import costs as C, store as S

BOUND = C.CostEvidence("enforceable_upper_bound", "adapter:bounded-call")
ZERO = C.CostEvidence("included_no_extra_charge", "provider:extra-charges-disabled-and-enforced")
LOCAL = C.CostEvidence("free_local", "core:local-only")
ACTUAL = C.CostEvidence("actual_charge", "provider:usage-receipt")
UNSENT = C.CostEvidence("not_dispatched", "launcher:pre-dispatch-refusal")


@contextmanager
def fixture(**configuration):
    with tempfile.TemporaryDirectory(prefix="solvio-task-costs-") as directory:
        ledger = S.AgentRunLedger(os.path.join(directory, "agent.sqlite3"))
        task = _task(ledger)
        costs = C.CostLedger(ledger)
        costs.configure(task.task_id, **configuration)
        yield costs, ledger, task.task_id


def _task(ledger):
    return ledger.create_task(objective="Bearbeite diesen begrenzten Auftrag",
        scope="research", created_origin="trusted_interactive_app",
        created_principal="owner:device")


def reserve(costs, task, invocation, amount, **kw):
    kw.setdefault("route", "codex")
    kw.setdefault("evidence", LOCAL if amount == 0 else BOUND)
    return costs.reserve(task, invocation, amount, **kw)


def raises(callback, error=ValueError):
    try:
        callback()
    except error:
        return
    raise AssertionError("expected exception")


def t_zero_999_and_exact_1000_are_decided_before_dispatch():
    with fixture() as (costs, _, task):
        require(reserve(costs, task, "local", 0).allowed)
        require(reserve(costs, task, "first", 999).allowed)
        boundary = reserve(costs, task, "second", 1)
        require_equal(boundary.status, "approval_required")
        require_equal(boundary.projected_cents, 1000)
        require_equal(costs.view(task)["ai_tool"]["total_cents"], 999)
    with fixture() as (costs, _, task):
        require_equal(reserve(costs, task, "one", 1000).status, "approval_required")


def t_subscription_auth_and_arbitrary_zero_do_not_prove_no_extra_usage():
    with fixture() as (costs, _, task):
        for index, evidence in enumerate((C.CostEvidence("subscription_auth", "auth:chatgpt"),
                C.CostEvidence("unknown"), C.CostEvidence("included_no_extra_charge"),
                C.CostEvidence("enforceable_upper_bound", "estimate:zero"))):
            require_equal(reserve(costs, task, "unknown-" + str(index), 0,
                                  evidence=evidence).status, "unbounded_cost")
        require_equal(reserve(costs, task, "unknown-amount", None).status, "unbounded_cost")
        require(reserve(costs, task, "proven-included", 0, evidence=ZERO).allowed)
        require_equal(costs.view(task)["counts"]["unbounded_cost"], 5)


def t_concurrent_physical_calls_cannot_both_consume_the_remaining_money():
    with fixture() as (costs, ledger, task):
        ready = threading.Barrier(2)
        first = C.CostLedger(S.AgentRunLedger(ledger.path))
        second = C.CostLedger(S.AgentRunLedger(ledger.path))

        def call(pair):
            service, key = pair
            ready.wait(timeout=5)
            return reserve(service, task, key, 600).status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(call, ((first, "one"), (second, "two"))))
        require_equal(sorted(results), ["approval_required", "reserved"])
        require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 600)


def t_same_invocation_replay_has_one_reservation_and_rejects_rebinding():
    with fixture() as (costs, _, task):
        first = reserve(costs, task, "same", 300)
        replay = reserve(costs, task, "same", 300)
        require(replay.replayed)
        require_equal(replay.reservation_id, first.reservation_id)
        for amount, kw in ((301, {}), (300, {"category": "purchase"}),
                           (300, {"route": "claude-code"}),
                           (300, {"evidence": C.CostEvidence("enforceable_upper_bound", "different:proof")})):
            denied = reserve(costs, task, "same", amount, **kw)
            require_equal(denied.reason, "invocation_binding_mismatch")
        require_equal(costs.view(task)["ai_tool"]["total_cents"], 300)


def t_simultaneous_duplicate_invocations_make_one_claim_and_one_replay():
    with fixture() as (costs, _, task):
        barrier = threading.Barrier(2)

        def call(_):
            barrier.wait(timeout=5)
            return reserve(costs, task, "same-physical-call", 600)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(call, range(2)))
        require_equal(len({result.reservation_id for result in results}), 1)
        require_equal(sorted(result.replayed for result in results), [False, True])
        require_equal(costs.view(task)["counts"], {"reserved": 1})
        require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 600)


def t_waiting_invocation_is_bound_and_uses_same_reservation_after_owner_decision():
    with fixture() as (costs, ledger, task):
        first = reserve(costs, task, "waiting", 2000)
        require_equal(first.status, "approval_required")
        require_equal(reserve(costs, task, "waiting", 1999).reason, "invocation_binding_mismatch")
        costs.approve(task, max_total_cents=2000, approval_ref="owner:decision-one")
        resumed = reserve(costs, task, "waiting", 2000)
        require(resumed.allowed)
        require_equal(resumed.reservation_id, first.reservation_id)
        require_equal(reserve(costs, task, "more", 1).status, "approval_required")
        costs.approve(task, max_total_cents=2000, approval_ref="owner:decision-one")
        raises(lambda: costs.approve(task, max_total_cents=2001, approval_ref="owner:decision-one"))
        raises(lambda: costs.approve(task, max_total_cents=2000, approval_ref="owner:decision-one",
                                    category="purchase"))
        other = _task(ledger).task_id
        costs.configure(other)
        raises(lambda: costs.approve(other, max_total_cents=2000, approval_ref="owner:decision-one"))
        require_equal(costs.view(other)["approved_ai_cap_cents"], None)


def t_restart_and_unknown_preserve_open_money_without_implying_zero_or_retry():
    with fixture() as (costs, ledger, task):
        first = reserve(costs, task, "sent", 900)
        require(costs.mark_unknown(first.reservation_id))
        reopened = C.CostLedger(S.AgentRunLedger(ledger.path))
        require_equal(reopened.view(task)["ai_tool"]["reserved_cents"], 900)
        require_equal(reserve(reopened, task, "next", 100).status, "approval_required")
        require_equal(reserve(reopened, task, "sent", 900).reason, "invocation_unknown")
        require_equal(reopened.settle(first.reservation_id, 600, ACTUAL).status, "settled")
        require_equal(reopened.view(task)["ai_tool"],
            {"spent_cents": 600, "reserved_cents": 0, "total_cents": 600, "overrun": False})
        require_equal(reopened.settle(first.reservation_id, 600, ACTUAL).reason, "already_settled")
        require_equal(reopened.settle(first.reservation_id, 599, ACTUAL).reason, "settlement_mismatch")
        require_equal(reserve(reopened, task, "sent", 900).reason, "invocation_settled")
        require(reserve(reopened, task, "remaining", 399).allowed)


def t_release_requires_proven_no_dispatch_and_does_not_resurrect_invocation():
    with fixture() as (costs, _, task):
        first = reserve(costs, task, "unsent", 900)
        for evidence in (C.CostEvidence("unknown"), ACTUAL,
                         C.CostEvidence("not_dispatched"), ZERO):
            raises(lambda: costs.release(first.reservation_id, evidence))
        require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 900)
        require(costs.release(first.reservation_id, UNSENT))
        require(not costs.release(first.reservation_id, UNSENT))
        require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 0)
        require_equal(reserve(costs, task, "unsent", 900).reason, "invocation_released")
        require(reserve(costs, task, "new-invocation", 900).allowed)


def t_actual_cost_overrun_is_recorded_and_halts_further_reservations():
    with fixture() as (costs, _, task):
        require(reserve(costs, task, "not-yet-dispatched", 600).allowed)
        first = reserve(costs, task, "bad-bound", 10)
        result = costs.settle(first.reservation_id, 1200, ACTUAL)
        require_equal(result.reason, "cost_bound_exceeded")
        require_equal(costs.view(task)["ai_tool"]["spent_cents"], 1200)
        require(costs.view(task)["ai_tool"]["overrun"])
        require_equal(reserve(costs, task, "more", 0).reason, "cost_bound_exceeded")
        require_equal(reserve(costs, task, "not-yet-dispatched", 600).reason, "cost_bound_exceeded")


def t_200_euro_purchase_budget_is_separate_from_ten_euro_ai_threshold():
    with fixture(purchase_cap_cents=20000, authority_ref="owner:buy-under-200") as (costs, _, task):
        require(reserve(costs, task, "purchase", 20000, category="purchase").allowed)
        require(reserve(costs, task, "analysis", 999).allowed)
        require_equal(reserve(costs, task, "analysis-more", 1).status, "approval_required")
        require_equal(reserve(costs, task, "purchase-more", 1, category="purchase").reason,
                      "purchase_budget_required")
        require_equal(costs.view(task)["purchase"]["reserved_cents"], 20000)
        require_equal(costs.view(task)["ai_tool"]["reserved_cents"], 999)
    with fixture() as (costs, _, task):
        require_equal(reserve(costs, task, "unauthorized-buy", 1, category="purchase").status,
                      "approval_required")
        costs.approve(task, max_total_cents=20000, approval_ref="owner:purchase", category="purchase")
        require(reserve(costs, task, "authorized-buy", 20000, category="purchase").allowed)
        require_equal(costs.view(task)["approved_ai_cap_cents"], None)


def t_default_is_durable_and_new_tasks_snapshot_it_without_mutating_old_tasks():
    with fixture() as (costs, ledger, old):
        costs.set_default_threshold(2500, "owner:settings")
        reopened = C.CostLedger(S.AgentRunLedger(ledger.path), default_threshold_cents=9000)
        require_equal(reopened.settings()["ask_threshold_cents"], 2500)
        new = _task(ledger).task_id
        reopened.configure(new)
        reopened.configure(old)
        require_equal(reopened.view(new)["ask_threshold_cents"], 2500)
        require_equal(reopened.view(old)["ask_threshold_cents"], 1000)
        require_equal(reserve(reopened, old, "old-boundary", 1000).status, "approval_required")
        require(reserve(reopened, new, "new-within", 1000).allowed)
        raises(lambda: reopened.configure(old, ask_threshold_cents=2500))
        require(ledger.permissions_ok())
        require(set(os.listdir(os.path.dirname(ledger.path))) <=
                {"agent.sqlite3", "agent.sqlite3-wal", "agent.sqlite3-shm"},
                "ein zweites Kostenbuch wurde angelegt")


def t_zero_threshold_asks_for_any_paid_call_and_allows_proven_free_work():
    with fixture(ask_threshold_cents=0) as (costs, _, task):
        require(reserve(costs, task, "free", 0).allowed)
        require_equal(reserve(costs, task, "paid", 1).status, "approval_required")


def t_late_settings_replay_keeps_newer_decision_across_restart():
    with fixture() as (costs, ledger, task):
        costs.set_default_threshold(1500, "owner:decision-A")
        later = costs.set_default_threshold(500, "owner:decision-B")
        reopened = C.CostLedger(S.AgentRunLedger(ledger.path))
        replay = reopened.set_default_threshold(1500, "owner:decision-A")
        require_equal(replay, later, "spaete Wiederholung A hat Entscheidung B zurueckgedreht")
        new = _task(ledger).task_id
        reopened.configure(new)
        require_equal(reopened.view(new)["ask_threshold_cents"], 500)
        require_equal(reopened.view(task)["ask_threshold_cents"], 1000)
        raises(lambda: reopened.set_default_threshold(1700, "owner:decision-A"))
        require_equal(reopened.settings(), later)


def t_concurrent_settings_rebinding_has_one_winner_and_one_durable_value():
    with fixture() as (costs, ledger, _):
        barrier = threading.Barrier(2)
        first = C.CostLedger(S.AgentRunLedger(ledger.path))
        second = C.CostLedger(S.AgentRunLedger(ledger.path))

        def set_value(pair):
            service, value = pair
            barrier.wait(timeout=5)
            try:
                return service.set_default_threshold(value, "owner:same-decision")["ask_threshold_cents"]
            except ValueError as exc:
                require_equal(str(exc), "cost_settings_binding_mismatch")
                return "refused"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(set_value, ((first, 500), (second, 1500))))
        require_equal(results.count("refused"), 1)
        winner = next(result for result in results if result != "refused")
        require_equal(costs.settings()["ask_threshold_cents"], winner)
        reopened = C.CostLedger(S.AgentRunLedger(ledger.path))
        require_equal(reopened.set_default_threshold(winner, "owner:same-decision")["ask_threshold_cents"], winner)


def t_every_physical_phase_and_run_of_one_task_shares_the_same_total():
    with fixture() as (costs, ledger, task):
        first_run = ledger.create_run(task_id=task)
        for key in (first_run.run_id + ":plan", first_run.run_id + ":format-repair",
                    first_run.run_id + ":specialist", first_run.run_id + ":assessment"):
            reservation = reserve(costs, task, key, 200)
            require(reservation.allowed)
            costs.settle(reservation.reservation_id, 200, ACTUAL)
        second_run = ledger.create_run(task_id=task)
        require_equal(reserve(costs, task, second_run.run_id + ":replan", 200).status,
                      "approval_required")
        other_task = _task(ledger).task_id
        costs.configure(other_task)
        require(reserve(costs, other_task, "other-plan", 999).allowed)


def t_validation_rejects_fake_money_untrusted_evidence_and_missing_authority():
    with fixture() as (costs, ledger, task):
        for value in (True, False, -1, 0.1, "100", C.MAX_CENTS + 1):
            raises(lambda: reserve(costs, task, "bad-value", value))
            raises(lambda: costs.set_default_threshold(value, "owner:settings"))
        for key in ("", "white space", "line\nbreak", "a" * 257):
            raises(lambda: reserve(costs, task, key, 1))
            raises(lambda: costs.approve(task, max_total_cents=2000, approval_ref=key))
        raises(lambda: costs.set_default_threshold(1000, ""))
        raises(lambda: C.CostEvidence("unknown-kind"))
        raises(lambda: C.CostEvidence("actual_charge", "receipt:one", "USD"))
        raises(lambda: reserve(costs, task, "bad-proof", 0, evidence={"kind": "free_local"}))
        new = _task(ledger).task_id
        raises(lambda: costs.configure(new, purchase_cap_cents=20000))
        require_equal(reserve(costs, new, "without-policy", 1).reason, "cost_policy_missing")
        raises(lambda: costs.configure("at-does-not-exist"))


def t_reservation_transaction_rolls_back_if_process_fails_before_commit():
    with fixture() as (costs, ledger, task):
        original = ledger._connect

        class Crash(Exception):
            pass

        class Connection:
            def __init__(self):
                self.inner = original()

            def __enter__(self):
                self.inner.__enter__()
                return self

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

            def close(self):
                self.inner.close()

            def execute(self, sql, *args):
                result = self.inner.execute(sql, *args)
                if sql.startswith("INSERT INTO agent_cost_reservations "):
                    raise Crash()
                return result

        with patch.object(ledger, "_connect", Connection):
            raises(lambda: reserve(costs, task, "interrupted", 900), Crash)
        reopened = C.CostLedger(S.AgentRunLedger(ledger.path))
        require_equal(reopened.view(task)["ai_tool"]["total_cents"], 0)
        require_equal(reopened.view(task)["counts"], {})
        require(reserve(reopened, task, "interrupted", 900).allowed)


def t_settlement_crash_keeps_prior_reservation_and_a_second_receipt_does_not_double_bill():
    with fixture() as (costs, ledger, task):
        reservation = reserve(costs, task, "one", 800)
        original = ledger._connect

        class Crash(Exception):
            pass

        class Connection:
            def __init__(self):
                self.inner = original()

            def __enter__(self):
                self.inner.__enter__()
                return self

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

            def close(self):
                self.inner.close()

            def execute(self, sql, *args):
                result = self.inner.execute(sql, *args)
                if sql.startswith("UPDATE agent_cost_reservations SET state='settled'"):
                    raise Crash()
                return result

        with patch.object(ledger, "_connect", Connection):
            raises(lambda: costs.settle(reservation.reservation_id, 400, ACTUAL), Crash)
        reopened = C.CostLedger(S.AgentRunLedger(ledger.path))
        require_equal(reopened.view(task)["ai_tool"]["reserved_cents"], 800)
        reopened.settle(reservation.reservation_id, 400, ACTUAL)
        reopened.settle(reservation.reservation_id, 400, ACTUAL)
        require_equal(reopened.view(task)["ai_tool"]["spent_cents"], 400)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
