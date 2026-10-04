"""Authenticated action -> real runtime/native REST client -> durable result.

Only REST transport, device attestation and the native model protocol are
synthetic. All stores, HTTPS admission, grants, costs, orchestration, receipts,
detached continuation and fresh owner reads are real and temporary.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
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

from test_agent_public_execution import BuildWorld
from test_agent_cost_runtime import _cli
from test_agent_runtime_subscription_flow import ForbiddenBroker
from test_agent_action_services import native_fixture, calendar_action, gmail_action, ha_action, _gmail_message
from solvio.agent_runtime import action_contract as AC, planner as PL, store as S
from solvio.agent_runtime import cost_dispatch as D, costs as C
from solvio.agent_runtime.orchestrator import Orchestrator
from solvio.capabilities.agent import AgentCapabilities, SPECS
from solvio.capabilities.approval_gateway import CapabilityApprovals
from solvio.capabilities.router import CapabilityRouter
from solvio.capabilities.task_action import TaskServiceAction, SPEC
from solvio.proactive.store import ProactiveStore
from solvio.specialists import providers as P
from solvio.specialists.subscription import SubscriptionTransport


class ActionWorld(BuildWorld):
    def runtime(self):
        ledger = S.AgentRunLedger(self.ledger.path)
        router = CapabilityRouter(mobile=CapabilityApprovals(self.co, owner_principal="local-owner"))
        planner = PL.Planner(broker=self.broker, transport=self.api,
            subscription_transport=SubscriptionTransport("codex", timeout=10))

        def quote(provider, invocation):
            require_equal(provider, "codex")
            require_equal(invocation.executable, str(self.executable))
            return D.CostQuote(0, C.CostEvidence("free_local", "test:local-action-assessment"))

        orch = Orchestrator(ledger=ledger, router=router, planner=planner,
            control_plane=self.cp, proactive=self.proactive,
            require_task_authority=True, cost_quote_adapter=quote)
        router.register(SPECS["agent_task_action"], AgentCapabilities(orch).action)
        service = TaskServiceAction(ledger, calendar=self.native.calendar, gmail=self.native.gmail,
            ha=self.native.ha, exposure=self.native.exposure, portals=getattr(self, "portals", None))
        router.register(SPEC, service)
        orch.action_service = service
        self.ledger, self.router, self.orch = ledger, router, orch
        return orch

    def assessment(self, run_id):
        receipts = AC.completion_evidence(self.ledger, run_id)
        payload = json.loads((self.folder / "fixture.json").read_text())
        payload["assessment"] = [{"beantwortet": [
            {"id": r.requirement, "belege": [r.evidence]} for r in receipts],
            "offen": [], "fehlend": [], "unsicher": [], "weiterarbeit_noetig": False}]
        (self.folder / "fixture.json").write_text(json.dumps(payload))

    async def finish(self, run_id, expected=S.SUCCEEDED):
        for _ in range(16):
            run = self.ledger.get_run(run_id)
            if run.state == S.VERIFYING:
                self.assessment(run_id)
            if run.terminal:
                break
            await self.orch.tick()
        run = self.ledger.get_run(run_id)
        require_equal(run.state, expected, run.result_summary)
        return run


@asynccontextmanager
async def world(kind="calendar", *, expected_native=None):
    with tempfile.TemporaryDirectory(prefix="solvio-public-actions-") as directory:
        folder = Path(directory)
        with native_fixture(folder) as native:
            w = await ActionWorld().open(directory)
            try:
                w.folder, w.native = folder, native
                w.executable = _cli(folder, w.ledger.path)
                source = w.executable.read_text()
                source = source.replace("'pid':os.getpid()", "'pid':os.getpid(), 'request':request")
                marker = "    text = reply if isinstance(reply, str) else json.dumps(reply)"
                require_equal(source.count(marker), 1)
                expected = expected_native or {"calendar": "SYNTHETIC calendar appointment", "gmail": "Fixture body", "ha": "light.fixture"}[kind]
                source = source.replace(marker,
                    f"    if kind == 'assessment' and {expected!r} not in request['ergebnis']:\n"
                    "        reply = {'beantwortet':[], 'offen':['a1'], 'fehlend':['Native result missing'], "
                    "'unsicher':[], 'weiterarbeit_noetig':True}\n" + marker)
                w.executable.write_text(source)
                w.broker = ForbiddenBroker()
                w.api = AsyncMock(side_effect=AssertionError("No paid API fallback"))
                w.proactive = ProactiveStore(str(folder / "proactive.sqlite3"))
                with patch.object(P, "resolve", return_value=str(w.executable)):
                    w.runtime()
                    w.app["agent_runtime_provider"] = lambda: w.orch
                    action = (calendar_action(native.calendar) if kind == "calendar" else
                              ha_action(native.ha) if kind == "ha" else gmail_action(native.gmail))
                    w.body = {"scope": "action", "objective": (
                        "Lege den genau beschriebenen synthetischen Kalendereintrag an."
                        if kind == "calendar" else "Schalte genau light.fixture ein." if kind == "ha" else
                        "Erstelle diesen genau beschriebenen Mailentwurf, ohne ihn zu senden."),
                        "target_repo": "", "client_request_id": "public-action-001",
                        "action_request": {"actions": [action]}}
                    yield w
                require_equal(w.broker.calls, [])
                require_equal(w.api.await_count, 0)
            finally:
                await w.close()


async def admit(w):
    response = await w.start(w.body)
    result = await response.json()
    require_equal(response.status, 201, str(result))
    require_equal(w.native.transport.mutations, [])
    require_equal(await w.store.list_pending(), [])
    return result["run_id"]


async def t_calendar_finishes_after_browser_closes_and_fresh_runtime_reads_receipt():
    async with world() as w:
        run_id = await admit(w)
        await w.detach()
        final = await w.finish(run_id)
        require_equal(len(w.native.transport.mutations), 1)
        require_equal(final.planner_calls, 0, "Structured actions need no model reinterpretation")
        require_equal([c["kind"] for c in w.calls()], ["assessment"])
        require("SYNTHETIC calendar appointment" in w.calls()[0]["request"]["ergebnis"])
        before = AC.read_receipts(w.ledger, run_id)
        w.runtime()
        await w.orch.reconcile()
        await w.orch.tick()
        reader, _ = await w.fresh_reader()
        rows = await (await reader.get("/v1/agent/runs")).json()
        response = await reader.get("/v1/agent/runs/" + rows["laeufe"][0]["id"])
        view = await response.json()
        require_equal(view["zustand_code"], S.SUCCEEDED)
        require_equal(AC.read_receipts(w.ledger, run_id), before)
        require_equal(len(w.native.transport.mutations), 1)
        require_equal(w.orch.costs.view(final.task_id)["ask_threshold_cents"], 1000)
        require_equal(len(await w.proactive.unread()), 1)


async def t_mail_draft_returns_observed_content_without_sending():
    async with world("gmail") as w:
        run_id = await admit(w)
        await w.detach()
        await w.finish(run_id)
        require_equal(len(w.native.transport.mutations), 1)
        require(all(not call[1].endswith("/send") for call in w.native.transport.calls))
        require("Fixture body" in w.calls()[0]["request"]["ergebnis"])
        require_equal(len(AC.completion_evidence(w.ledger, run_id)), 1)


async def t_two_bound_services_complete_in_order_after_browser_detaches():
    async with world() as w:
        w.body["objective"] = "Lege den beschriebenen Termin und den beschriebenen Mailentwurf an."
        w.body["action_request"]["actions"].append(gmail_action(w.native.gmail, action_id="a2"))
        run_id = await admit(w)
        await w.detach()
        await w.finish(run_id)
        require_equal(len(w.native.transport.mutations), 2)
        require("/calendar/v3/" in w.native.transport.mutations[0][1])
        require(w.native.transport.mutations[1][1].endswith("/drafts"))
        require_equal({item.requirement for item in AC.completion_evidence(w.ledger, run_id)}, {"a1", "a2"})
        result = w.calls()[0]["request"]["ergebnis"]
        require("SYNTHETIC calendar appointment" in result and "Fixture body" in result)


async def t_five_explicit_actions_are_not_mistaken_for_one_repeated_attempt():
    async with world() as w:
        w.body["objective"] = "Lege genau diese fünf synthetischen Kalendertermine an."
        actions = [calendar_action(w.native.calendar, action_id="a" + str(i)) for i in range(1, 6)]
        for i, action in enumerate(actions, 1):
            action["payload"]["summary"] += " " + str(i)
        w.body["action_request"]["actions"] = actions
        run_id = await admit(w)
        await w.detach()
        await w.finish(run_id)
        require_equal(len(w.native.transport.mutations), 5)
        require_equal(len(w.native.transport.events), 5)
        require_equal(len(AC.completion_evidence(w.ledger, run_id)), 5)
        require_equal(len(w.calls()), 1)


async def t_a_structured_task_cannot_send_mail_adr_0041():
    """Bis 26.09.2026 sendete ein strukturierter Auftrag einen gebundenen Entwurf — auch
    nach einem Start ohne Face ID (Dashboard-Sitzung). ADR-0041: nicht mehr; der Start
    wird abgewiesen, und nichts verlaesst das Haus."""
    async with world("gmail") as w:
        w.native.transport.drafts["draft1"] = {"id": "draft1", "message": _gmail_message(
            "message1", "recipient@example.invalid", "SYNTHETIC message", "Fixture body")}
        w.body["objective"] = "Sende genau diesen synthetischen Entwurf an recipient@example.invalid."
        w.body["action_request"]["actions"] = [gmail_action(w.native.gmail, "send_draft")]
        response = await w.start(w.body)
        require(response.status != 201, "a mail send task was admitted")
        require_equal(w.native.transport.mutations, [])


async def t_assessment_at_ten_euros_waits_before_provider_call_and_does_not_repeat_action():
    from solvio.agent_runtime import inquiry as Q
    async with world() as w:
        run_id = await admit(w)
        w.orch.cost_quote_adapter = lambda *_: D.CostQuote(
            1000, C.CostEvidence("enforceable_upper_bound", "test:assessment-ten-euros"))
        for _ in range(6):
            await w.orch.tick()
            if w.ledger.get_run(run_id).state == S.WAITING_USER:
                break
        run = w.ledger.get_run(run_id)
        require_equal(run.state, S.WAITING_USER)
        view = Q.run_view(w.ledger, run)
        require_equal(view["anbietergrenze"]["grund"], "cost_approval_required")
        require_equal(w.calls(), [])
        require_equal(len(w.native.transport.mutations), 1)
        await w.orch.tick()
        require_equal(w.calls(), [])
        require_equal(len(w.native.transport.mutations), 1)


async def t_ha_detached_task_reads_actual_device_state_once():
    async with world("ha") as w:
        run_id = await admit(w)
        await w.detach()
        await w.finish(run_id)
        require_equal(w.native.transport.states["light.fixture"]["state"], "on")
        require_equal(len(w.native.transport.mutations), 1)
        observed = AC.read_receipts(w.ledger, run_id)[0]["native"]["observed"]
        require(observed["confirmed"])
        w.runtime()
        await w.orch.reconcile()
        await w.orch.tick()
        require_equal(len(w.native.transport.mutations), 1)


async def t_ha_no_longer_exposed_stops_without_effect_or_substitute_plan():
    async with world("ha") as w:
        run_id = await admit(w)
        w.native.transport.exposed = {}
        await w.finish(run_id, S.FAILED)
        require_equal(w.native.transport.mutations, [])
        require_equal(w.calls(), [])
        require_equal(w.ledger.get_run(run_id).planner_calls, 0)


async def t_portal_status_uses_bound_session_after_browser_closes_without_login_or_write():
    from test_agent_action_portal import portal_fixture, portal_action
    async with portal_fixture(owner="local-owner") as portal, world(expected_native="Synthetic account") as w:
        w.portals = portal.portals
        w.runtime()
        w.body["action_request"]["actions"] = [portal_action(portal)]
        w.body["objective"] = "Lies den Status genau dieser bereits geöffneten synthetischen Portalsitzung."
        run_id = await admit(w)
        await w.detach()
        await w.finish(run_id)
        require(set(portal.operations).issubset({"ping", "read"}), str(portal.operations))
        require("read" in portal.operations)
        require_equal(w.native.transport.mutations, [])
        require_equal(w.native.transport.calls, [])
        require_equal(len(AC.completion_evidence(w.ledger, run_id)), 1)


async def t_restart_before_assessment_restores_full_observed_body_beyond_checkpoint_limit():
    async with world("gmail") as w:
        tail = "FINAL_NATIVE_SENTENCE_BEYOND_CHECKPOINT"
        content = "Fixture body " + "additional detail " * 90 + tail
        w.body["action_request"]["actions"][0]["payload"]["body"] = content
        run_id = await admit(w)
        for _ in range(3):
            await w.orch.tick()
        require_equal(len(w.native.transport.mutations), 1)
        require_equal(w.ledger.steps_for_run(run_id)[0].state, "succeeded")
        w.runtime()
        await w.orch.reconcile()
        await w.finish(run_id)
        require(content in w.calls()[0]["request"]["ergebnis"],
                "The full native observation must survive checkpoint truncation")
        require_equal(len(w.native.transport.mutations), 1)


async def t_restart_after_native_receipt_before_step_finish_reuses_receipt_without_new_write():
    class AbruptProcessLoss(BaseException):
        pass
    async with world() as w:
        run_id = await admit(w)
        await w.orch.tick()
        await w.orch.tick()
        with patch.object(w.orch, "_settle_capability", side_effect=AbruptProcessLoss):
            try:
                await w.orch.tick()
            except AbruptProcessLoss:
                pass
            else:
                raise AssertionError("Crash injection did not reach post-native receipt window")
        require_equal(len(w.native.transport.mutations), 1)
        require_equal(w.ledger.steps_for_run(run_id)[0].state, "running")
        require_equal(AC.read_receipts(w.ledger, run_id)[0]["status"], "completed")
        w.runtime()
        await w.orch.reconcile()
        await w.finish(run_id)
        require_equal(len(w.native.transport.mutations), 1)
        require_equal(w.ledger.steps_for_run(run_id)[0].state, "succeeded")


async def t_login_nonstart_resumes_same_action_atomically_across_another_restart():
    async with world() as w:
        run_id = await admit(w)
        w.native.calendar._access_token = ""
        w.native.calendar._expires_at = 0
        w.native.transport.auth_error = "invalid_grant"
        for _ in range(3):
            await w.orch.tick()
        require_equal(w.ledger.get_run(run_id).state, S.WAITING_USER)
        require_equal(AC.read_receipts(w.ledger, run_id)[0]["status"], "not_dispatched")
        require_equal(w.native.transport.mutations, [])
        await w.detach()
        w.runtime()
        await w.orch.reconcile()
        # The temporary service now accepts the SAME configured account.
        # Credential/account rotation is a separate binding change, not waived.
        w.native.transport.auth_error = ""
        reader, headers = await w.fresh_reader()
        response = await reader.post("/v1/agent/runs/" + run_id + "/resume", json={}, headers=headers)
        require_equal(response.status, 200, str(await response.json()))
        require_equal(w.ledger.get_run(run_id).plan_revision, 1)
        w.runtime()  # crash immediately after resume, before the next tick
        await w.orch.reconcile()
        await w.finish(run_id)
        receipts = AC.read_receipts(w.ledger, run_id)
        require_equal([r["status"] for r in receipts], ["not_dispatched", "completed"])
        require_equal(len(w.native.transport.mutations), 1)
        require_equal(len(w.ledger.recent_runs()), 1)


async def t_lost_native_reply_is_unknown_and_never_repeated_after_restart():
    async with world() as w:
        run_id = await admit(w)
        w.native.transport.drop_after_write = True
        final = await w.finish(run_id, S.FAILED)
        require_equal(final.failure_category, "recovery_required")
        require_equal(len(w.native.transport.mutations), 1)
        require_equal(AC.completion_evidence(w.ledger, run_id), ())
        require_equal(w.calls(), [])
        w.runtime()
        await w.orch.reconcile()
        await w.orch.tick()
        require(not await w.orch.resume(run_id))
        require_equal(len(w.native.transport.mutations), 1)


async def t_owner_cancel_before_action_tick_never_reaches_native_service():
    async with world() as w:
        run_id = await admit(w)
        response = await w.client.post("/v1/agent/runs/" + run_id + "/cancel",
                                       json={}, headers=w.headers)
        require_equal(response.status, 200)
        await w.orch.tick()
        require_equal(w.ledger.get_run(run_id).state, S.CANCELLED)
        require_equal(w.native.transport.mutations, [])
        require_equal(w.calls(), [])


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
