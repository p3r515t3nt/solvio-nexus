"""Owner-selected routes through real temporary HTTPS, runtime and ledger.

Only model replies and research are doubles. No native turn, network research,
production state or account is used. The public owner path admits each task.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
import json
import os
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_agent_task_entry import world
from test_agent_public_research import PLAN, VERDICT, SOURCES
from solvio.agent_runtime import store as S, planner as PL, provider_switch as PS
from solvio.agent_runtime import specialists as SP, cost_dispatch as D, costs as C, checkpoint as CP
from solvio.agent_runtime.orchestrator import Orchestrator
from solvio.specialists.result import SpecialistResult


@contextmanager
def probe_measurements():
    """Fresh, bounded fake-probe workers; all work is joined before unpatching."""
    with ThreadPoolExecutor(max_workers=1) as pool, \
            patch.object(PS, "_PROBE_POOL", pool), \
            patch.object(PS, "_PROBE_FUTURES", {"worker": None, "mcp": None}), \
            patch.object(PS, "_WORKER_CANARY", None):
        yield


class Transport:
    timeout = 1

    def __init__(self, provider="codex", blocked="plan", calls=None, plan=None):
        self.provider, self.blocked = provider, blocked
        self.calls = calls if calls is not None else []
        self.plan = PLAN if plan is None else plan

    @property
    def route(self):
        return {"provider": self.provider, "billing_mode": "subscription"}

    async def __call__(self, payload):
        scope = D.current_scope()
        require(isinstance(scope, D.TaskCostScope), "selected route lost the physical cost context")
        self.calls.append((scope.phase, self.provider, scope.task_id, scope.run_id, payload))
        blocked = scope.phase == self.blocked
        verdict = VERDICT
        if (scope.phase == "assessment" and len(self.plan["schritte"]) > 1
                and sum(call[0] == "assessment" for call in self.calls) == 1):
            verdict = {"beantwortet": [], "offen": [], "fehlend": [],
                "unsicher": ["Die zweite unabhängige Quellenprüfung fehlt noch."], "weiterarbeit_noetig": True}
        return dict(ok=not blocked, reason="quota" if blocked else "", text=json.dumps(
            verdict if scope.phase == "assessment" else self.plan), provider=self.provider,
            billing_mode="subscription", dispatch_started=False, tokens=3, usage_reported=True)


@asynccontextmanager
async def paused(phase="plan", started=False, plan=None, fail_at=1):
    async with world() as w:
        source = Transport(blocked=phase, plan=plan)
        target = Transport("claude-code", blocked="")
        w.orch.planner = PL.Planner(subscription_transport=source)
        w.orch.researcher = object()
        w.research_calls = []

        async def research(request, **kwargs):
            scope = D.current_scope()
            w.research_calls.append((request, scope.task_id, scope.run_id))
            failed = phase == "specialist" and len(w.research_calls) == fail_at
            return SP.SpecialistRun(result=SpecialistResult(role="researcher", provider="codex",
                question=request.objective, ok=not failed, reason="quota" if failed else "",
                findings=["Die beiden Quellen belegen den lokalen Protokollnachweis vollständig."
                    + (f" Teilprüfung {len(w.research_calls)}." if fail_at > 1 else "")],
                evidence=SOURCES, recommended_path="Die beiden Quellen stimmen überein.", confidence="hoch"),
                provider="claude-code" if request.profile == "researcher/claude" else "codex",
                billing_mode="subscription", dispatch_started=started if failed else False,
                quota=failed, usage_reported=True)

        with patch.object(SP, "run_specialist", research), \
                patch("solvio.specialists.subscription.SubscriptionTransport", side_effect=lambda provider: target if provider == "claude-code" else source):
            response = await w.start()
            require_equal(response.status, 201, str(await response.json()))
            ids = await response.json()
            w.run_id, w.task_id = ids["run_id"], ids["task_id"]
            for _ in range(6):
                await w.orch.tick()
                if w.ledger.get_run(w.run_id).state == S.WAITING_USER:
                    break
            require_equal(w.ledger.get_run(w.run_id).state, S.WAITING_USER)
            w.source, w.target = source, target
            yield w


async def view(w):
    response = await w.client.get("/v1/agent/runs/" + w.run_id, headers=w.headers)
    require_equal(response.status, 200)
    return await response.json()


async def choose(w, body=None, headers=None, client=None):
    if body is None:
        boundary = (await view(w))["anbietergrenze"]
        body = {"provider": "claude-code", "boundary_ref": boundary["boundary_ref"]}
    return await (client or w.client).post("/v1/agent/runs/" + w.run_id + "/resume",
        json=body, headers=w.headers if headers is None else headers)


async def t_public_plan_switch_keeps_task_costs_and_does_not_touch_global_route():
    async with paused() as w:
        before_task = w.ledger.get_task(w.task_id)
        policy = w.orch.costs.view(w.task_id)
        boundary = (await view(w))["anbietergrenze"]
        require_equal([x["provider"] for x in boundary["wechseloptionen"]], ["claude-code"])
        require_equal((await choose(w)).status, 200)
        require_equal(w.source.calls[0][:4], ("plan", "codex", w.task_id, w.run_id))
        require_equal(w.target.calls, [], "HTTP selection dispatched a model")
        await w.orch.tick()
        require_equal(w.target.calls[0][:4], ("plan", "claude-code", w.task_id, w.run_id))
        require_equal(w.orch.planner.route["provider"], "codex", "global route mutated")
        require_equal(w.ledger.get_task(w.task_id).objective, before_task.objective)
        require_equal(w.orch.costs.view(w.task_id), policy)
        require_equal(len(w.ledger.runs_for_task(w.task_id)), 1)


async def t_public_assessment_switch_preserves_completed_research_and_requirements():
    async with paused("assessment") as w:
        old_steps = w.ledger.steps_for_run(w.run_id)
        requirements = w.ledger.get_task(w.task_id).requirements
        require_equal((await choose(w)).status, 200)
        await w.orch.tick()
        require_equal(len(w.research_calls), 1)
        require_equal(w.target.calls[0][:4], ("assessment", "claude-code", w.task_id, w.run_id))
        require_equal(w.ledger.get_task(w.task_id).requirements, requirements)
        for old in old_steps:
            if old.state == "succeeded":
                require_equal(w.ledger.get_step(old.step_id), old)


async def t_public_research_switch_survives_restart_with_same_step_and_full_context():
    async with paused("specialist") as w:
        before = w.ledger.get_run(w.run_id)
        steps = w.ledger.steps_for_run(w.run_id)
        expected = json.loads(before.plan_checkpoint)
        require_equal((await choose(w)).status, 200)
        after = w.ledger.get_run(w.run_id)
        actual = json.loads(after.plan_checkpoint)
        for step in expected["schritte"]:
            if step.get("profil") == "researcher/hermes":
                step["profil"] = "researcher/claude"
        require_equal(actual, expected, "switch changed anything except the route")
        restarted = Orchestrator(ledger=S.AgentRunLedger(w.ledger.path), router=w.orch.router,
            planner=w.orch.planner, researcher=object(), control_plane=w.cp)
        await restarted.reconcile()
        await restarted.tick()
        require_equal(w.research_calls[-1][0].profile, "researcher/claude")
        require_equal(w.research_calls[-1][1:], (w.task_id, w.run_id))
        require_equal([s.step_id for s in w.ledger.steps_for_run(w.run_id)], [s.step_id for s in steps])
        require_equal(w.ledger.get_run(w.run_id).planner_calls, before.planner_calls)


async def t_parallel_and_repeated_choice_credit_wait_and_route_once():
    async with paused() as w:
        boundary = (await view(w))["anbietergrenze"]
        body = {"provider": "claude-code", "boundary_ref": boundary["boundary_ref"]}
        results = await asyncio.gather(choose(w, body), choose(w, body))
        require_equal(sorted(r.status for r in results), [200, 409])
        seconds = w.ledger.get_run(w.run_id).provider_wait_seconds
        require_equal((await choose(w, body)).status, 409)
        require_equal(w.ledger.get_run(w.run_id).provider_wait_seconds, seconds)
        require_equal(len([e for e in w.ledger.events_for_run(w.run_id) if e.kind == "boundary_resumed"]), 1)
        require_equal(w.target.calls, [])


async def t_stale_boundary_and_foreign_or_untrusted_selection_have_no_effect():
    async with paused() as w:
        before = w.ledger.get_run(w.run_id)
        require_equal((await choose(w, {"provider": "claude-code", "boundary_ref": "0" * 64})).status, 409)
        require_equal((await choose(w, headers={})).status, 401)
        other = await w.new_client()
        headers = await w.login(other, "another-owner")
        require_equal((await choose(w, headers=headers, client=other)).status, 404)
        for body in ({"provider": "claude-code"}, {"provider": [], "boundary_ref": "0" * 64},
                     {"provider": "api", "boundary_ref": "0" * 64},
                     {"provider": "claude-code", "boundary_ref": "0" * 64, "budget": 99999}):
            require_equal((await choose(w, body)).status, 400)
        require_equal(w.ledger.get_run(w.run_id), before)
        require_equal(w.target.calls, [])


async def t_unsafe_phase_reason_started_claim_and_revocation_are_not_choices():
    for alteration in ({"phase": "development"}, {"phase": "action_interpret"},
                       {"reason": "cost_approval_required"}, {"reason": "cost_recovery_required"},
                       {"dispatch_started": True}, {"resume_allowed": False}):
        async with paused() as w:
            run = w.ledger.get_run(w.run_id)
            data = json.loads(run.boundary); data["provider_wait"].update(alteration)
            w.ledger.set_run_fields(w.run_id, boundary=json.dumps(data))
            require_equal((await view(w))["anbietergrenze"]["wechseloptionen"], [], str(alteration))
            require_equal((await choose(w)).status, 409)
    async with paused() as w:
        with w.ledger._open() as db:
            db.execute("UPDATE agent_task_grants SET revoked_at=1 WHERE task_id=?", (w.task_id,))
        require_equal((await choose(w)).status, 409)


async def t_unknown_or_reserved_task_cost_blocks_switch_even_before_dispatch():
    for state in ("reserved", "unknown"):
        async with paused() as w:
            reservation = w.orch.costs.reserve(w.task_id, "held-test-call", 1,
                evidence=C.CostEvidence("free_local", "test:bounded"), route="test")
            with w.ledger._open() as db:
                db.execute("UPDATE agent_cost_reservations SET state=? WHERE reservation_id=?", (state, reservation.reservation_id))
            require_equal((await view(w))["anbietergrenze"]["wechseloptionen"], [])
            require_equal((await choose(w)).status, 409)


async def t_started_research_requires_exact_terminal_and_settled_invocation():
    async with paused("specialist", started=True) as w:
        require_equal((await choose(w)).status, 409, "started without cost proof switched")
        reservation = w.orch.costs.reserve(w.task_id, "pc-" + "a" * 64, 0,
            evidence=C.CostEvidence("free_local", "test:readonly-quota"), route="codex")
        w.orch.costs.settle(reservation.reservation_id, 0, evidence=C.CostEvidence("free_local", "test:terminal"))
        with w.ledger._open() as db:
            db.execute("INSERT INTO agent_provider_invocations(reservation_id,invocation_id,subject_id,task_id,run_id,phase,operation_id,ordinal,provider,request_digest,process_owner,state,claimed_at,finished_at) VALUES(?,?,?,?,?,'specialist','test',1,'codex',?,'test','finished',1,2)",
                (reservation.reservation_id, "pc-" + "a" * 64, w.task_id, w.task_id, w.run_id, "b" * 64))
        run = w.ledger.get_run(w.run_id)
        data = json.loads(run.boundary); data["provider_wait"]["cost_invocation_id"] = "pc-" + "a" * 64
        w.ledger.set_run_fields(w.run_id, boundary=json.dumps(data))
        require_equal((await choose(w)).status, 200)


async def t_unknown_or_unfinished_physical_invocation_blocks_even_with_settled_money():
    for state, finished in (("unknown", 2), ("claimed", None), ("finished", None)):
        async with paused() as w:
            reservation = w.orch.costs.reserve(w.task_id, "pc-" + "a" * 64, 0,
                evidence=C.CostEvidence("free_local", "test:invocation"), route="codex")
            w.orch.costs.settle(reservation.reservation_id, 0, C.CostEvidence("free_local", "test:money-only"))
            with w.ledger._open() as db:
                db.execute("INSERT INTO agent_provider_invocations(reservation_id,invocation_id,subject_id,task_id,run_id,phase,operation_id,ordinal,provider,request_digest,process_owner,state,claimed_at,finished_at) VALUES(?,?,?,?,?,'plan','test',1,'codex',?,'test',?,1,?)",
                    (reservation.reservation_id, "pc-" + "a" * 64, w.task_id, w.task_id, w.run_id, "b" * 64, state, finished))
            require_equal((await choose(w)).status, 409)


async def t_old_actual_boundary_cannot_select_a_later_wait_even_with_same_clock():
    async with paused() as w:
        old = (await view(w))["anbietergrenze"]
        # Same-provider recovery, then another measured quota is a NEW decision.
        response = await w.client.post("/v1/agent/runs/" + w.run_id + "/resume", headers=w.headers)
        require_equal(response.status, 200)
        with patch("time.time", return_value=old["wartet_seit"]):
            await w.orch.tick()
        new = (await view(w))["anbietergrenze"]
        require(old["boundary_ref"] != new["boundary_ref"])
        require_equal((await choose(w, {"provider": "claude-code", "boundary_ref": old["boundary_ref"]})).status, 409)
        require_equal((await choose(w)).status, 200)


async def t_switch_crash_rolls_back_profile_selection_and_wait_together():
    async with paused("specialist") as w:
        before = w.ledger.get_run(w.run_id)
        steps = w.ledger.steps_for_run(w.run_id)
        connect = w.ledger._connect
        class Crash(BaseException): pass
        class Connection:
            def __init__(self): self.inner = connect()
            def __getattr__(self, name): return getattr(self.inner, name)
            def __enter__(self): self.inner.__enter__(); return self
            def __exit__(self, *args): return self.inner.__exit__(*args)
            def execute(self, sql, *args):
                if "UPDATE agent_runs SET state=?,boundary=?,provider_selection=" in sql:
                    raise Crash()
                return self.inner.execute(sql, *args)
        with patch.object(w.ledger, "_connect", Connection):
            try:
                await w.orch.resume(w.run_id, provider="claude-code", boundary_ref=PS.reference(before), principal="local-owner")
            except Crash:
                pass
            else:
                raise AssertionError("crash injection missed the durable switch")
        require_equal(w.ledger.get_run(w.run_id), before)
        require_equal(w.ledger.steps_for_run(w.run_id), steps)
        require_equal((await choose(w)).status, 200)


async def t_current_grant_expiry_or_binding_change_refuses_selection():
    for column, value in (("expires_at", 1), ("binding_digest", "0" * 64), ("task_fingerprint", "0" * 64)):
        async with paused() as w:
            with w.ledger._open() as db:
                db.execute("UPDATE agent_task_grants SET " + column + "=? WHERE run_id=?", (value, w.run_id))
            require_equal((await choose(w)).status, 409)


async def t_switch_preserves_completed_plan_prefix_and_sources_after_restart():
    import copy
    plan = copy.deepcopy(PLAN)
    second = copy.deepcopy(plan["schritte"][0]); second["auftrag"] += " Prüfe danach die unabhängige zweite Quelle."
    plan["schritte"].append(second)
    async with paused("specialist", plan=plan, fail_at=2) as w:
        before = w.ledger.get_run(w.run_id)
        checkpoint = json.loads(before.plan_checkpoint)
        completed = next(s for s in w.ledger.steps_for_run(w.run_id) if s.kind == "specialist" and s.state == "succeeded")
        require_equal(checkpoint["cursor"], 1)
        require(bool(checkpoint["quellen"]))
        require_equal((await choose(w)).status, 200)
        after = json.loads(w.ledger.get_run(w.run_id).plan_checkpoint)
        require_equal(after["schritte"][0], checkpoint["schritte"][0])
        require_equal(after["schritte"][1]["profil"], "researcher/claude")
        require_equal({k: v for k, v in after.items() if k != "schritte"},
                      {k: v for k, v in checkpoint.items() if k != "schritte"})
        restarted = Orchestrator(ledger=S.AgentRunLedger(w.ledger.path), router=w.orch.router,
            planner=w.orch.planner, researcher=object(), control_plane=w.cp)
        await restarted.reconcile()
        for _ in range(4): await restarted.tick()
        require_equal(len(w.research_calls), 3, "finished prefix executed twice")
        require_equal(w.ledger.get_step(completed.step_id), completed)
        require_equal(w.ledger.get_run(w.run_id).state, S.SUCCEEDED)


async def t_unconfigured_target_and_incompatible_work_never_offer_a_switch():
    async with paused() as w:
        with patch.object(PS, "configured", return_value=False):
            require_equal((await view(w))["anbietergrenze"]["wechseloptionen"], [])
            require_equal((await choose(w)).status, 409)
    for profile in ("builder/codex", "files/codex", "image/codex"):
        async with paused("specialist") as w:
            run = w.ledger.get_run(w.run_id); data = json.loads(run.boundary)
            with w.ledger._open() as db:
                db.execute("UPDATE agent_steps SET specialist_profile=? WHERE step_id=?", (profile, data["schritt"]))
            require_equal((await choose(w)).status, 409)
    async with paused("assessment") as w:
        with patch("solvio.agent_runtime.result_files.describe_files", return_value=([{"mime_type": "image/png"}], "")):
            require_equal((await choose(w)).status, 409)


async def t_research_choice_reports_distinct_tools_without_coupling_claude_to_codex_browser():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    # A missing or invalid Codex browser cannot disable the independent
    # installed Claude WebSearch/WebFetch route.
    with patch("solvio.specialists.launcher.resolve", return_value="fixture"), \
            patch.object(SP, "native_research_config", side_effect=ValueError("unavailable Codex browser")):
        require(PS.configured("claude-code", "specialist"))
        require(not PS.configured("codex", "specialist"))
    claude_tools = ["Websuche (WebSearch)", "Webseiten lesen (WebFetch)"]
    browser_tool = "Öffentlicher Hermes-Browser ohne Anmeldung"
    # Public research-boundary readback must not promise Claude's browser
    # even when the real independent Codex browser has all three paths.
    async with paused("specialist") as w:
        settings = SimpleNamespace(agent_runtime_hermes_browser_python="python",
            agent_runtime_hermes_browser_bin="browser", agent_runtime_hermes_browser_chrome="chrome")
        with patch("solvio.config.load_settings", return_value=settings):
            option = PS.offers(w.ledger, w.ledger.get_run(w.run_id))[0]
            require_equal(option["provider"], "claude-code")
            require_equal(option["werkzeuge"], claude_tools)
    # Projection checks cover both eligible providers and all boundary phases;
    # their admission and dispatch are proved by the separate public cases.
    ledger = MagicMock()
    for paths in (("", "", ""), ("python", "", ""), ("python", "browser", "chrome")):
        settings = SimpleNamespace(**dict(zip(("agent_runtime_hermes_browser_" + key
            for key in ("python", "bin", "chrome")), paths)))
        for phase in ("specialist", "plan", "assessment"):
            run = SimpleNamespace(task_id="task", boundary=json.dumps({"provider_wait": {"phase": phase}}))
            with patch.object(PS, "eligible", return_value=True), \
                    patch("solvio.config.load_settings", return_value=settings):
                options = {item["provider"]: item["werkzeuge"] for item in PS.offers(ledger, run)}
            if phase != "specialist":
                require_equal(options, {p: ["Textplanung und Ergebnisprüfung"] for p in PS.PROFILES})
            else:
                require_equal(options["claude-code"], claude_tools)
                require_equal(browser_tool in options["codex"], all(paths))


async def t_revision_two_uses_its_fresh_grant_not_the_revoked_previous_grant():
    from test_agent_task_followup_entry import parent, request, post
    async with world() as w:
        task_id, old_id, _ = await parent(w)
        response = await post(w, request(w, old_id))
        require_equal(response.status, 202, await response.text())
        accepted = await response.json()
        w.run_id, w.task_id = accepted["run_id"], task_id
        old = w.orch.task_authority.for_run(old_id)
        w.orch.task_authority.revoke(old.reference, "test:previous-revision-finished")
        before_old = w.ledger.get_run(old_id)
        before_current = w.orch.task_authority.for_run(w.run_id)
        w.orch.planner = PL.Planner(subscription_transport=Transport())
        for _ in range(3):
            await w.orch.tick()
            if w.ledger.get_run(w.run_id).state == S.WAITING_USER: break
        require_equal(w.ledger.get_run(w.run_id).state, S.WAITING_USER)
        require_equal((await choose(w)).status, 200)
        require_equal(w.orch.task_authority.for_run(w.run_id), before_current)
        require_equal(w.ledger.get_run(old_id), before_old)


# ---------------------------------------------------------------------------
# N8/C4 — the worker line: selections, gates, plan check, offer conditions.
# ---------------------------------------------------------------------------
from types import SimpleNamespace
from solvio.agent_runtime import native_tasks as NTASK


def _run(selection):
    return SimpleNamespace(provider_selection=json.dumps(selection), run_id="r", boundary="")


def t_worker_selection_is_a_third_durable_key_with_the_same_provider_set():
    ref = "a" * 64
    data = PS.selections(json.dumps({"worker": {"provider": "claude-code", "boundary_ref": ref},
                                     "research": {"provider": "codex", "boundary_ref": ref}}))
    require_equal(set(data), {"worker", "research"})
    require_equal(PS.selected(_run({"worker": {"provider": "claude-code", "boundary_ref": ref}}), "worker"), "claude-code")
    require_equal(PS.selected(_run({"worker": {"provider": "claude-code", "boundary_ref": ref}}), "specialist"), "")
    require_equal(PS.selected(_run({"research": {"provider": "claude-code", "boundary_ref": ref}}), "worker"), "")
    for bad in ({"builder": {"provider": "codex", "boundary_ref": ref}},
                {"worker": {"provider": "hermes", "boundary_ref": ref}},
                {"worker": {"provider": "claude-code", "boundary_ref": "x"}},
                {"worker": {"provider": "claude-code"}}):
        try:
            PS.selections(json.dumps(bad))
        except ValueError as exc:
            require_equal(str(exc), "invalid_provider_selection")
        else:
            raise AssertionError("accepted: " + json.dumps(bad))
    require(PS.profile_table("worker/claude") is PS.WORKER_PROFILES)
    require(PS.profile_table("worker/codex") is PS.WORKER_PROFILES)
    require(PS.profile_table("researcher/claude") is PS.PROFILES)
    require(PS.profile_table("builder/codex") is None)
    require_equal(PS.WORKER_PROFILES, {"codex": SP.TASK_PROFILE, "claude-code": SP.CLAUDE_TASK_PROFILE})


def t_worker_configured_is_red_on_each_missing_gate_and_green_only_with_all():
    from solvio.agent_runtime import native_costs as NC, isolation as ISO
    import solvio.specialists.launcher as LAUNCH
    green = dict(resolve=patch.object(LAUNCH, "resolve", return_value="/synthetic/claude"),
                 sandbox=patch.object(ISO, "available", return_value=True),
                 pre=patch.object(NC, "claude_worker_preconditions", return_value=(True, "", {})),
                 canary=patch.object(PS, "_WORKER_CANARY", ""))
    from contextlib import ExitStack
    with ExitStack() as stack:
        for item in green.values():
            stack.enter_context(item)
        require(PS.configured("claude-code", "specialist", worker=True))
        require(not PS.configured("claude-code", "plan", worker=True), "the worker exists only in the specialist phase")
        require(not PS.configured("hermes", "specialist", worker=True))
        with patch.object(ISO, "available", return_value=False):
            require(not PS.configured("claude-code", "specialist", worker=True), "no sandbox")
        with patch.object(NC, "claude_worker_preconditions", return_value=(False, "broker_not_running", {})):
            require(not PS.configured("claude-code", "specialist", worker=True), "no broker")
        with patch.object(NC, "claude_worker_preconditions", return_value=(False, "credential_scope", {})):
            require(not PS.configured("claude-code", "specialist", worker=True), "no worker capability")
        with patch.object(NC, "claude_worker_preconditions", return_value=(False, "pot_proof_missing", {})):
            require(not PS.configured("claude-code", "specialist", worker=True), "empty pot proof")
        for verdict in ("worker_canary_unavailable", "worker_canary_failed:leak", "cli_canary_failed:foreign_credential"):
            with patch.object(PS, "_WORKER_CANARY", verdict):
                require(not PS.configured("claude-code", "specialist", worker=True), verdict)
        with patch.dict(SP.BLOCKED_PROFILES, {SP.CLAUDE_TASK_PROFILE: "test"}):
            require(not PS.configured("claude-code", "specialist", worker=True), "blocked profile")
        from solvio.specialists.launcher import LauncherError
        with patch.object(LAUNCH, "resolve", side_effect=LauncherError("not_installed", "claude")):
            require(not PS.configured("claude-code", "specialist", worker=True), "CLI missing")
        with patch.object(SP, "native_research_configured", return_value=False):
            require(not PS.configured("codex", "specialist", worker=True))
        with patch.object(SP, "native_research_configured", return_value=True):
            require(PS.configured("codex", "specialist", worker=True))
    # Without a measured verdict the gate is a NAMED block, never a silent pass.
    from _native_worker_seams import claude_module_double
    with probe_measurements(), claude_module_double(SimpleNamespace()):
        require_equal(PS.claude_worker_canary(), "worker_canary_unavailable")
    for error in (RuntimeError, TimeoutError):
        with probe_measurements():
            calls = []

            def crash():
                calls.append(1)
                raise error("boom")

            with claude_module_double(SimpleNamespace(worker_canary_verdict=crash)):
                for _ in range(2):
                    require_equal(PS.claude_worker_canary(), "worker_canary_failed:" + error.__name__)
                require_equal(len(calls), 1, "failed probes also remain cached")
    with probe_measurements(), claude_module_double(SimpleNamespace(worker_canary_verdict=lambda: "")):
        require_equal(PS.claude_worker_canary(), "")
    with probe_measurements(), claude_module_double(SimpleNamespace(mcp_mode=lambda: "bridge")):
        require_equal(PS.claude_mcp_mode(), "bridge")
    with probe_measurements(), claude_module_double(SimpleNamespace(mcp_mode=lambda: "anything-else")):
        require_equal(PS.claude_mcp_mode(), "none")
    with probe_measurements(), claude_module_double(SimpleNamespace()):
        require_equal(PS.claude_mcp_mode(), "none")


async def t_worker_status_stays_responsive_during_two_second_canary_and_reuses_one_probe():
    from _native_worker_seams import claude_module_double
    from solvio.agent_runtime import native_costs as NC, isolation as ISO
    from solvio.capabilities.agent import AgentCapabilities
    calls, beats, latencies = [], [], []
    loop_thread = threading.get_ident()

    def canary():
        calls.append(threading.get_ident())
        time.sleep(2)
        return ""

    async with paused("specialist") as w:
        run = w.ledger.get_run(w.run_id)
        data, body = json.loads(run.boundary), CP.decode(run.plan_checkpoint)
        with w.ledger._open() as db:
            db.execute("UPDATE agent_steps SET specialist_profile=? WHERE step_id=?",
                       (SP.TASK_PROFILE, data["schritt"]))
        body["schritte"][0]["profil"] = SP.TASK_PROFILE
        data["provider_wait"].update(provider="codex", dispatch_started=False, resume_allowed=True)
        w.ledger.set_run_fields(w.run_id, boundary=json.dumps(data),
                                plan_checkpoint=json.dumps(body, ensure_ascii=False, separators=(",", ":")))
        module = SimpleNamespace(worker_canary_verdict=canary, mcp_mode=lambda: "none")
        with probe_measurements(), claude_module_double(module), \
                patch("solvio.specialists.launcher.resolve", return_value="/synthetic/claude"), \
                patch.object(ISO, "available", return_value=True), \
                patch.object(NC, "claude_worker_preconditions", return_value=(True, "", {})):
            stopped = asyncio.Event()
            capabilities = AgentCapabilities(w.orch)

            async def heartbeat():
                while not stopped.is_set():
                    beats.append(time.monotonic())
                    await asyncio.sleep(.05)

            ticker = asyncio.create_task(heartbeat())
            try:
                await asyncio.sleep(0)
                for _ in range(5):
                    began = time.monotonic()
                    # The voice status runs on the Core loop; the HTTP view
                    # already uses to_thread and may join the same Future.
                    status = await capabilities.status({"run_id": w.run_id})
                    latencies.append(time.monotonic() - began)
                    require_equal(status["anbietergrenze"]["wechseloptionen"], [], "pending probe cannot authorize a switch")
                    require_equal(PS.claude_worker_canary(), "worker_canary_pending")
                    require(PS._WORKER_CANARY is None, "pending must not poison the process verdict")
                    await asyncio.sleep(.05)
                # A dispatch-side, off-loop reader joins the same physical probe.
                require_equal(await asyncio.to_thread(PS.claude_worker_canary), "")
                status = await view(w)
                require_equal([o["provider"] for o in status["anbietergrenze"]["wechseloptionen"]], ["claude-code"])
                require_equal(len(calls), 1, "status polls and the off-loop reader share one probe")
                require(calls[0] != loop_thread)
                require(max(latencies) < .5, f"status blocked for {max(latencies):.3f}s")
                require(len(beats) >= 20, f"only {len(beats)} heartbeat ticks during a two-second probe")
                require(max(b - a for a, b in zip(beats, beats[1:])) < .5, "probe blocked the event loop")
            finally:
                stopped.set()
                await ticker


async def t_mcp_probe_is_pending_without_bridge_and_timeout_retries_share_the_same_worker():
    from _native_worker_seams import claude_module_double
    release = threading.Event()
    calls = []

    def mcp():
        calls.append(("mcp", threading.get_ident()))
        if not release.wait(2):
            raise RuntimeError("test did not release its probe")
        return "bridge"

    def worker():
        calls.append(("worker", threading.get_ident()))
        return ""

    with probe_measurements(), claude_module_double(SimpleNamespace(mcp_mode=mcp, worker_canary_verdict=worker)):
        try:
            with patch.object(PS, "_PROBE_WAIT_SECONDS", .02):
                require_equal(PS.claude_mcp_mode(), "none")
                for _ in range(3):
                    require_equal(await asyncio.to_thread(PS.claude_mcp_mode), "none", "bounded wait remains closed")
                    require_equal(PS.claude_mcp_mode(), "none")
                    require_equal(PS.claude_worker_canary(), "worker_canary_pending")
                require_equal(len(calls), 1, "timeouts cannot duplicate the probe or start a second worker thread")
        finally:
            release.set()
        require_equal(await asyncio.to_thread(PS.claude_mcp_mode), "bridge")
        require_equal(await asyncio.to_thread(PS.claude_worker_canary), "")
        require_equal(PS.claude_mcp_mode(), "bridge", "later status sees the finished verdict")
        require_equal([name for name, _ in calls], ["mcp", "worker"])
        require_equal(len({thread for _, thread in calls}), 1, "both fixed probe slots share one thread")


def t_check_plan_accepts_either_worker_profile_and_refuses_foreign_or_changed_plans():
    from solvio.agent_runtime import planner as PLN
    task = SimpleNamespace(scope=S.SCOPE_TASK, target_repo="", objective="Sortiere die CSV lokal.")
    for provider in ("codex", "claude-code"):
        NTASK.check_plan(task, NTASK.delegation(task, provider))
    for bad in (PLN.Plan(goal=task.objective, steps=(PLN.PlannedStep(kind="specialist", profile="researcher/hermes",
                                                                    instruction=task.objective),)),
                PLN.Plan(goal=task.objective, steps=(PLN.PlannedStep(kind="specialist", profile=SP.CLAUDE_TASK_PROFILE,
                                                                    instruction="Etwas anderes."),)),
                PLN.Plan(goal="Anderes Ziel", steps=NTASK.delegation(task, "codex").steps),
                PLN.Plan(goal=task.objective, steps=NTASK.delegation(task, "codex").steps
                         + NTASK.delegation(task, "claude-code").steps), None):
        try:
            NTASK.check_plan(task, bad)
        except PLN.PlanInvalid as exc:
            require_equal(exc.reason, "native_delegation_changed")
        else:
            raise AssertionError("accepted a foreign plan")
    try:
        NTASK.delegation(task, "hermes")
    except PLN.PlanInvalid as exc:
        require_equal(exc.reason, "native_worker_provider_invalid")
    else:
        raise AssertionError("delegation to a foreign provider")


def t_nonstart_is_proven_only_by_an_exclusive_429_book_without_output_tokens():
    base = {"rows": 3, "requests": 3, "input_tokens": 90, "output_tokens": 0, "tokens": 90,
            "outcomes": {"upstream_error": 2, "denied": 1}, "status_codes": {"429": 3}}
    require(NTASK.nonstart_proven(base))
    for label, changes in (("one answered row", dict(outcomes={"upstream_error": 2, "forwarded": 1},
                                                     status_codes={"429": 2, "200": 1})),
                           ("output tokens", dict(output_tokens=5)),
                           ("a 503 refusal", dict(status_codes={"429": 2, "503": 1})),
                           ("empty book", dict(rows=0, requests=0, outcomes={}, status_codes={})),
                           ("inconsistent counts", dict(rows=4)),
                           ("client aborted", dict(outcomes={"client_aborted": 3}))):
        require(not NTASK.nonstart_proven(dict(base, **changes)), label)
    require(not NTASK.nonstart_proven(None))


async def t_no_switch_offer_without_waiting_user_and_none_for_a_started_worker_turn():
    async with paused() as w:
        run = w.ledger.get_run(w.run_id)
        require(PS.offers(w.ledger, run), "the research pause offers a switch")
        w.ledger.transition(w.run_id, S.RUNNING)
        require_equal(PS.offers(w.ledger, w.ledger.get_run(w.run_id)), [], "no offer outside WAITING_USER")
    async with paused("specialist") as w:
        run = w.ledger.get_run(w.run_id); data = json.loads(run.boundary)
        with w.ledger._open() as db:
            db.execute("UPDATE agent_steps SET specialist_profile=? WHERE step_id=?",
                       (SP.CLAUDE_TASK_PROFILE, data["schritt"]))
        body = CP.decode(run.plan_checkpoint)
        body["schritte"][0]["profil"] = SP.CLAUDE_TASK_PROFILE
        data["provider_wait"].update(provider="claude-code", dispatch_started=True, resume_allowed=True)
        w.ledger.set_run_fields(w.run_id, boundary=json.dumps(data),
                                plan_checkpoint=json.dumps(body, ensure_ascii=False, separators=(",", ":")))
        with patch.object(PS, "configured", return_value=True):
            require_equal(PS.offers(w.ledger, w.ledger.get_run(w.run_id)), [],
                          "a started worker turn must never be offered a switch")
            data["provider_wait"].update(dispatch_started=False)
            w.ledger.set_run_fields(w.run_id, boundary=json.dumps(data))
            offers = PS.offers(w.ledger, w.ledger.get_run(w.run_id))
            require_equal([o["provider"] for o in offers], ["codex"], "the proven non-start offers the other worker")
            require_equal(offers[0]["werkzeuge"][0], "Lokale Datei- und Codearbeit im Auftragsordner")
            # An unsettled native turn of the task (state `started`, no
            # finished/settled invocation) blocks the switch even though the
            # boundary itself says resumable non-start.
            from solvio.agent_runtime import native_sessions as NS
            NS.NativeSessions(w.ledger)
            with w.ledger._open() as db:
                db.execute("INSERT INTO agent_native_sessions(session_id,task_id,provider,profile,policy_digest,"
                           "workspace,native_thread_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                           ("ns-test", w.task_id, "claude-code", SP.CLAUDE_TASK_PROFILE, "a" * 64, "/tmp", "", 1.0))
                db.execute("INSERT INTO agent_native_turns(invocation_id,session_id,run_id,revision,revision_digest,"
                           "reservation_id,request_digest,native_turn_id,state,terminal_status,requested_at,updated_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("pc-open", "ns-test", w.run_id, 1, "b" * 64, "res-open", "", "t/1", "started", "", 1.0, 1.0))
            require_equal(PS.offers(w.ledger, w.ledger.get_run(w.run_id)), [],
                          "an unsettled native turn must block the worker switch")
            with w.ledger._open() as db:
                db.execute("DELETE FROM agent_native_turns WHERE invocation_id='pc-open'")
            require_equal([o["provider"] for o in PS.offers(w.ledger, w.ledger.get_run(w.run_id))], ["codex"])
            data["provider_wait"].update(reason="logged_out")
            w.ledger.set_run_fields(w.run_id, boundary=json.dumps(data))
            require_equal(PS.offers(w.ledger, w.ledger.get_run(w.run_id)), [],
                          "only the quota non-start is a worker switch")


async def t_claude_worker_is_not_offered_for_core_tool_or_browser_or_image_orders():
    async with paused("specialist") as w:
        run = w.ledger.get_run(w.run_id); data = json.loads(run.boundary)
        with w.ledger._open() as db:
            db.execute("UPDATE agent_steps SET specialist_profile=? WHERE step_id=?", (SP.TASK_PROFILE, data["schritt"]))
        body = CP.decode(run.plan_checkpoint)
        body["schritte"][0]["profil"] = SP.TASK_PROFILE
        data["provider_wait"].update(provider="codex", dispatch_started=False, resume_allowed=True)
        w.ledger.set_run_fields(w.run_id, boundary=json.dumps(data),
                                plan_checkpoint=json.dumps(body, ensure_ascii=False, separators=(",", ":")))
        current = w.ledger.get_run(w.run_id)
        from solvio.agent_runtime import requirements as RQ, task_revisions as TR
        view = TR.task_view(w.ledger, w.run_id)
        def bind(text):
            payload = json.dumps(RQ.validate({"auskunft": [{"id": "a1", "text": text}]}, objective=view.objective))
            with w.ledger._open() as db:
                db.execute("UPDATE agent_tasks SET requirements=? WHERE task_id=?", (payload, w.task_id))
        with patch.object(PS, "configured", return_value=True):
            bind("Vergleiche zwei öffentliche Quellen zum Thema.")
            with patch.object(PS, "claude_mcp_mode", return_value="none"):
                require_equal([o["provider"] for o in PS.offers(w.ledger, current)], ["claude-code"])
                bind("Nenne die lokal konfigurierten Portale.")
                require_equal(PS.offers(w.ledger, current), [], "Core tool need without the bridge")
            with patch.object(PS, "claude_mcp_mode", return_value="bridge"):
                offers = PS.offers(w.ledger, current)
                require_equal([o["provider"] for o in offers], ["claude-code"])
                require("Core-Lesewege" in offers[0]["werkzeuge"])
                bind("Öffne die Seite im Browser und lies die Preise ab.")
                require_equal(PS.offers(w.ledger, current), [], "browser need")
                bind("Vergleiche zwei öffentliche Quellen zum Thema.")
                with patch("solvio.agent_runtime.result_files.describe_files", return_value=([{"mime_type": "image/png"}], "")):
                    require_equal(PS.offers(w.ledger, current), [], "image files")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
