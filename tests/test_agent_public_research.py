"""N3: authenticated HTTPS research through real Hermes native RPC and Core.

Browser authentication, task/grant/cost stores, Planner and assessment,
SubscriptionTransport, launcher, installed Hermes Session/Client, completion,
artifacts and inbox are real. Only the two Codex protocols are local programs:
one answers planning/assessment, the other stands in for Codex app-server.
No production stores, real credentials, provider inference or web access.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from test_agent_public_execution import BuildWorld
from test_agent_cost_runtime import _cli
from test_agent_runtime_subscription_flow import ForbiddenBroker
from test_hermes_native import HERMES_SOURCE, HERMES_PYTHON, RPC_PROGRAM, observed, method_rows
from solvio import config as CONFIG
from solvio.agent_runtime import planner as PL, store as S, costs as C, cost_dispatch as D, specialists as SP
from solvio.agent_runtime.orchestrator import Orchestrator
from solvio.capabilities.agent import AgentCapabilities, SPECS
from solvio.capabilities.approval_gateway import CapabilityApprovals
from solvio.capabilities.router import CapabilityRouter
from solvio.proactive.store import ProactiveStore
from solvio.realtime.control import CoreControl, ControlClient
from solvio.specialists import providers as P, hermes_native as N
from solvio.specialists.subscription import SubscriptionTransport

OBJECTIVE = "Recherchiere den lokalen Protokollnachweis und belege die Antwort mit zwei Quellen."
SOURCES = ["Native Websuche: https://example.org/source",
           "Native Websuche: https://example.org/result"]
PLAN = {"schritte": [{"art": "specialist", "profil": "researcher/hermes",
                       "auftrag": OBJECTIVE}],
        "anforderungen": {"auskunft": [{"id": "a1", "text": "Lokaler Protokollnachweis"}],
            "handlungen": [], "unklar": [], "belege": {"mindestens": 2}}}
VERDICT = {"beantwortet": [{"id": "a1", "belege": SOURCES}],
           "offen": [], "fehlend": [], "unsicher": [], "weiterarbeit_noetig": False}


class ResearchWorld(BuildWorld):
    """Reuse the browser lifecycle, while all research phases stay native."""

    def runtime(self):
        ledger = S.AgentRunLedger(self.ledger.path)
        router = CapabilityRouter(mobile=CapabilityApprovals(self.co, owner_principal="local-owner"))
        planner = PL.Planner(broker=self.broker, transport=self.api,
            subscription_transport=SubscriptionTransport("codex", timeout=10))

        def quote(provider, invocation):
            require_equal(provider, "codex")
            if invocation.executable == str(self.executable):
                evidence = "test:local-planner-assessor"
            else:
                require_equal(invocation.executable, self.native.hermes_python)
                argv = invocation.argv
                require_equal(argv[argv.index("--codex-bin") + 1], self.native.codex_bin)
                require_equal(argv[argv.index("--codex-home") + 1], self.native.codex_home)
                evidence = "test:local-hermes-rpc"
            return D.CostQuote(0, C.CostEvidence("free_local", evidence))

        orch = Orchestrator(ledger=ledger, router=router, control_plane=self.cp,
            planner=planner, proactive=self.proactive, require_task_authority=True,
            cost_quote_adapter=quote, researcher=None)
        caps = AgentCapabilities(orch)
        router.register(SPECS["agent_task_research"], caps.research)
        router.register(SPECS["agent_task_build"], caps.build)
        self.ledger, self.router, self.orch = ledger, router, orch
        return orch

    async def admit(self):
        response = await self.start({"scope": "research", "objective": self.objective,
            "target_repo": "", "client_request_id": "public-native-research-001"})
        require_equal(response.status, 201, str(await response.json()))
        accepted = await response.json()
        grant = self.orch.task_authority.for_run(accepted["run_id"])
        require(grant is not None)
        require_equal(grant.receipt_method, "dashboard_session")
        require_equal(grant.authorizer, "local-owner")
        require_equal(self.ledger.get_task(accepted["task_id"]).created_origin, "trusted_dashboard")
        require_equal(await self.store.list_pending(), [])
        require_equal(self.calls(), [])
        require_equal(observed(self.rpc), [], "HTTP acceptance must not itself dispatch")
        return accepted, grant

    def mode(self, mode):
        (self.rpc / "mode").write_text(mode)

    async def inquiry(self):
        # HTTP currently exposes the compact operator view. The complete
        # source/status inquiry is the existing owner-authenticated control
        # socket. A fresh client starts without any remembered task key.
        with tempfile.TemporaryDirectory(prefix="solvio-inquiry-", dir="/tmp") as directory:
            control = CoreControl(SimpleNamespace(agent_runtime=self.orch),
                                  socket_path=str(Path(directory) / "c.sock"))
            await control.start()
            try:
                found = await ControlClient(control.socket_path).task_status()
                require(found["ok"] and found["found"], str(found))
                return found["auftrag"]
            finally:
                await control.stop()


@asynccontextmanager
async def world(mode="ok", *, objective=OBJECTIVE, plan=None, verdict=None,
                answer=None, assessment_required=(), followup_plan=None, alternative_plan=None):
    require(Path(HERMES_SOURCE, "agent/transports/codex_app_server_session.py").is_file(),
            "This integration test requires the installed Hermes transport")
    with tempfile.TemporaryDirectory(prefix="solvio-public-research-") as directory:
        folder = Path(directory)
        with patch.dict(os.environ, {"SOLVIO_STATE_DIR": directory,
                                    "SOLVIO_AGENT_RUNS_DB": str(folder / "agent.sqlite3")}):
            w = await ResearchWorld().open(directory)
            try:
                w.folder = folder
                w.objective = objective
                w.rpc = folder / "rpc"
                w.rpc.mkdir()
                home = w.rpc / "native-home"
                home.mkdir(mode=0o700)
                model = "gpt-local-protocol-test"
                (home / "config.toml").write_text(N.native_config_text(model))
                codex = w.rpc / "codex-local"
                codex.write_text("#!" + sys.executable + "\n" + RPC_PROGRAM)
                codex.chmod(0o700)
                w.native = N.NativeResearchConfig(HERMES_PYTHON, HERMES_SOURCE,
                    str(codex), str(home), model)
                w.mode(mode)
                if answer is not None:
                    (w.rpc / "answer.json").write_text(json.dumps(answer, ensure_ascii=False))
                w.executable = _cli(folder, w.ledger.path,
                                    plans=[plan or PLAN] + ([followup_plan] if followup_plan else [])
                                          + ([alternative_plan] if alternative_plan else []),
                                    assessments=[verdict or VERDICT])
                # Preserve the existing CLI protocol fixture; record the actual
                # assessment input so original task and source binding are checked.
                source = w.executable.read_text()
                marker = "'pid':os.getpid()"
                require_equal(source.count(marker), 1)
                source = source.replace(marker, marker + ", 'request':request, "
                    "'instruction':'\\n'.join(m['content'] for m in messages if m.get('role') == 'system')")
                if assessment_required:
                    # This local assessor can only confirm the answer if its
                    # actual protocol input contains the required material.
                    # A fixed positive fixture hid the production handoff bug.
                    marker = "    text = reply if isinstance(reply, str) else json.dumps(reply)"
                    require_equal(source.count(marker), 1)
                    condition = repr(tuple(assessment_required))
                    source = source.replace(marker,
                        "    if kind == 'assessment' and not all(text in request['ergebnis'] "
                        f"for text in {condition}):\n"
                        "        reply = {'beantwortet':[], 'offen':['a1'], "
                        "'fehlend':['Empfehlung oder Einschraenkung fehlt.'], "
                        "'unsicher':[], 'weiterarbeit_noetig':True}\n" + marker)
                w.executable.write_text(source)
                w.broker = ForbiddenBroker()
                w.api = AsyncMock(side_effect=AssertionError("paid API dispatch forbidden"))
                w.proactive = ProactiveStore(str(folder / "proactive.sqlite3"))
                settings = SimpleNamespace(agent_runtime_research_provider='codex',
                    agent_runtime_hermes_python=HERMES_PYTHON,
                    agent_runtime_hermes_source=HERMES_SOURCE,
                    agent_runtime_hermes_codex_bin=str(codex),
                    agent_runtime_hermes_codex_home=str(home), agent_runtime_hermes_model=model,
                    agent_runtime_hermes_browser_python='', agent_runtime_hermes_browser_bin='',
                    agent_runtime_hermes_browser_chrome='')
                with patch.object(P, "resolve", return_value=str(w.executable)), \
                        patch.object(CONFIG, "load_settings", return_value=settings):
                    w.runtime()
                    w.app["agent_runtime_provider"] = lambda: w.orch
                    yield w
                require_equal(w.broker.calls, [])
                require_equal(w.api.await_count, 0)
                require(not (home / "auth.json").exists(), "no credential copying")
            finally:
                await w.close()


async def t_public_native_research_finishes_detached_and_fresh_inquiry_reads_actual_sources():
    async with world() as w:
        accepted, grant = await w.admit()
        await w.detach()
        final = await w.tick_until(accepted["run_id"], S.SUCCEEDED)
        require_equal(final.planner_calls, 1)
        require_equal(final.specialist_count, 1)
        require_equal(final.assessment_calls, 1)
        calls = w.calls()
        rpc = observed(w.rpc)
        require_equal([c["kind"] for c in calls], ["plan", "assessment"])
        require(all(c["claimed"] == 1 for c in calls))
        require_equal(len(method_rows(w.rpc, "turn/start")), 1)
        assessment = calls[-1]["request"]
        require_equal(assessment["originalauftrag"], OBJECTIVE)
        require(all(s in assessment["ergebnis"] for s in SOURCES))
        require_equal(w.orch.costs.view(final.task_id)["counts"], {"settled": 3})
        claims = D.invocations(w.ledger, final.task_id)
        require_equal([c["phase"] for c in claims], ["plan", "specialist", "assessment"])
        require(all(c["state"] == "finished" for c in claims))
        routes = [json.loads(e.ref) for e in w.ledger.events_for_run(final.run_id)
                  if e.kind == "provider_route"]
        native = [r for r in routes if r.get("runtime") == "hermes-codex-app-server"]
        require_equal(len(native), 1)
        require_equal(native[0]["native_thread_id"], "local-thread")
        require_equal(native[0]["native_turn_id"], "local-turn")
        require(native[0]["usage_reported"])
        notices = [n for n in await w.proactive.unread() if n["lauf"] == final.run_id]
        require_equal(len(notices), 1)
        require("Ergebnis lesen" in notices[0]["zusammenfassung"])
        require("Lokaler Protokollnachweis" in "\n".join(notices[0]["befunde"]))

        w.runtime()
        await w.orch.reconcile()
        await w.orch.tick()
        reader, _ = await w.fresh_reader()
        listed = await (await reader.get("/v1/agent/runs")).json()
        require_equal(len(listed["laeufe"]), 1)
        # Use the new authenticated reader's actual list, not a remembered ID.
        response = await reader.get("/v1/agent/runs/" + listed["laeufe"][0]["id"])
        require_equal(response.status, 200)
        view = await response.json()
        require_equal(view["id"], accepted["run_id"])
        require_equal(view["zustand_code"], S.SUCCEEDED)
        require_equal(view["auftrag"], OBJECTIVE)
        view = await w.inquiry()
        require_equal(view["kennung"], accepted["run_id"])
        require_equal(view["zustand_code"], S.SUCCEEDED)
        require("Lokaler Protokollnachweis" in view["befunde"])
        require(all(s in view["quellen"] for s in SOURCES))
        require(not any("access_token" in s for s in view["quellen"]))
        report = next(a for a in w.ledger.artifacts_for_run(final.run_id) if a.kind == "report")
        require_equal(json.loads(Path(report.path).read_text())["quellen"], view["quellen"])
        require_equal(w.orch.task_authority.for_run(final.run_id).reference, grant.reference)
        require_equal(len(w.ledger.runs_for_task(final.task_id)), 1)
        require_equal(await w.store.list_pending(), [])
        require_equal(w.calls(), calls)
        require_equal(observed(w.rpc), rpc)
        require_equal([n for n in await w.proactive.unread() if n["lauf"] == final.run_id], notices)


async def t_public_native_quota_survives_restart_and_only_explicit_resume_starts_work():
    async with world("quota_before") as w:
        accepted, grant = await w.admit()
        await w.detach()
        waiting = await w.tick_until(accepted["run_id"], S.WAITING_USER)
        require_equal([c["kind"] for c in w.calls()], ["plan"])
        require_equal(method_rows(w.rpc, "turn/start"), [])
        before = observed(w.rpc)
        w.runtime()
        await w.orch.reconcile()
        for _ in range(3):
            await w.orch.tick()
        require_equal(observed(w.rpc), before, "no quota polling or automatic provider retry")
        reader, headers = await w.fresh_reader()
        path = "/v1/agent/runs/" + waiting.run_id
        view = await (await reader.get(path)).json()
        require_equal(view["zustand_code"], S.WAITING_USER)
        require("Kontingent" in view["wartet_auf"]["grund"])
        view = await w.inquiry()
        require_equal(view["anbietergrenze"]["grund"], "quota")
        require_equal(view["anbietergrenze"]["phase"], "specialist")
        require(view["anbietergrenze"]["fortsetzbar"])
        require("Kontingent" in view["wartet_auf"]["grund"])
        w.mode("ok")
        require_equal((await reader.post(path + "/resume", json={}, headers=headers)).status, 200)
        require_equal((await reader.post(path + "/resume", json={}, headers=headers)).status, 409)
        final = await w.tick_until(waiting.run_id, S.SUCCEEDED)
        require_equal(len(method_rows(w.rpc, "turn/start")), 1)
        require_equal([c["kind"] for c in w.calls()], ["plan", "assessment"])
        require_equal(len(w.ledger.runs_for_task(final.task_id)), 1)
        require_equal(w.orch.task_authority.for_run(final.run_id).reference, grant.reference)
        require_equal(len([s for s in w.ledger.steps_for_run(final.run_id) if s.kind == "specialist"]), 1)
        require_equal((await (await reader.get(path)).json())["zustand_code"], S.SUCCEEDED)


async def t_public_native_login_loss_waits_before_rpc_and_resumes_same_task():
    async with world("login_out") as w:
        accepted, _ = await w.admit()
        waiting = await w.tick_until(accepted["run_id"], S.WAITING_USER)
        require_equal(len(D.invocations(w.ledger, waiting.task_id)), 1, "only planning dispatched")
        require_equal(method_rows(w.rpc, "initialize"), [])
        reader, headers = await w.fresh_reader()
        path = "/v1/agent/runs/" + waiting.run_id
        require_equal((await (await reader.get(path)).json())["zustand_code"], S.WAITING_USER)
        view = await w.inquiry()
        require_equal(view["anbietergrenze"]["grund"], "logged_out")
        w.mode("ok")
        require_equal((await reader.post(path + "/resume", json={}, headers=headers)).status, 200)
        final = await w.tick_until(waiting.run_id, S.SUCCEEDED)
        require_equal(final.planner_calls, 1)
        require_equal(len(method_rows(w.rpc, "turn/start")), 1)
        require_equal([c["kind"] for c in w.calls()], ["plan", "assessment"])


async def t_public_native_missing_model_waits_once_and_owner_resumes_the_same_step():
    async with world("model_unavailable") as w:
        accepted, grant = await w.admit()
        await w.detach()
        waiting = await w.tick_until(accepted["run_id"], S.WAITING_USER)
        require_equal(waiting.planner_calls, 1)
        require_equal(waiting.plan_revision, 0)
        require_equal(waiting.specialist_count, 1)
        require_equal([c["kind"] for c in w.calls()], ["plan"])
        require_equal(len(method_rows(w.rpc, "initialize")), 1, "only one native worker")
        require_equal(len(method_rows(w.rpc, "model/list")), 1)
        require_equal(method_rows(w.rpc, "thread/start"), [])
        require_equal(method_rows(w.rpc, "turn/start"), [])
        steps = w.ledger.steps_for_run(waiting.run_id)
        require_equal(len(steps), 1)
        step = steps[0]
        require_equal(step.state, "waiting")
        require_equal(step.outcome_reason, "provider_wait")
        require_equal(w.orch.costs.view(waiting.task_id)["counts"], {"settled": 2})
        require_equal([i["state"] for i in D.invocations(w.ledger, waiting.task_id)],
                      ["finished", "finished"])
        before_rpc, before_calls = observed(w.rpc), w.calls()
        before_notices = await w.proactive.unread()
        require_equal(len(before_notices), 1)

        for _ in range(3):
            await w.orch.tick()
        w.runtime()
        await w.orch.reconcile()
        for _ in range(3):
            await w.orch.tick()
        require_equal(observed(w.rpc), before_rpc, "no preflight or research polling")
        require_equal(w.calls(), before_calls, "no fresh planning for a setup failure")
        require_equal(await w.proactive.unread(), before_notices, "no repeated owner question")
        require_equal(w.ledger.get_run(waiting.run_id).state, S.WAITING_USER)

        reader, headers = await w.fresh_reader()
        path = "/v1/agent/runs/" + waiting.run_id
        for view in (await (await reader.get(path)).json(), await w.inquiry()):
            require_equal(view["zustand_code"], S.WAITING_USER)
            require_equal(view["anbietergrenze"]["grund"], "native_model_unavailable")
            require_equal(view["anbietergrenze"]["phase"], "specialist")
            require_equal(view["anbietergrenze"]["anbieter"], "codex")
            require(view["anbietergrenze"]["fortsetzbar"])
            require("Modell" in view["wartet_auf"]["grund"])
            require("nicht verfuegbar" in view["wartet_auf"]["grund"])
            require("Modell" in view["wartet_auf"]["handlung"])

        # Fixing the local catalog alone is not permission to dispatch.
        w.mode("ok")
        for _ in range(3):
            await w.orch.tick()
        require_equal(observed(w.rpc), before_rpc)
        require_equal(w.calls(), before_calls)
        require_equal((await reader.post(path + "/resume", json={}, headers=headers)).status, 200)
        require_equal((await reader.post(path + "/resume", json={}, headers=headers)).status, 409)
        final = await w.tick_until(waiting.run_id, S.SUCCEEDED)
        require_equal(final.planner_calls, 1)
        require_equal(final.plan_revision, 0)
        require_equal([c["kind"] for c in w.calls()], ["plan", "assessment"])
        require_equal(len(method_rows(w.rpc, "initialize")), 2)
        require_equal(len(method_rows(w.rpc, "turn/start")), 1)
        final_steps = [s for s in w.ledger.steps_for_run(final.run_id) if s.kind == "specialist"]
        require_equal([s.step_id for s in final_steps], [step.step_id])
        require_equal(final_steps[0].state, "succeeded")
        require_equal(final_steps[0].attempt, step.attempt)
        require_equal(len(w.ledger.runs_for_task(waiting.task_id)), 1)
        require_equal(w.orch.task_authority.for_run(final.run_id).reference, grant.reference)
        view = await (await reader.get(path)).json()
        require_equal(view["zustand_code"], S.SUCCEEDED)
        require(all(source in view["quellen"] for source in SOURCES))


async def t_public_native_disconnect_retains_unknown_claim_and_rejects_resume_after_restart():
    async with world("disconnect") as w:
        accepted, _ = await w.admit()
        await w.detach()
        waiting = await w.tick_until(accepted["run_id"], S.WAITING_USER)
        require_equal(len(method_rows(w.rpc, "turn/start")), 1)
        before = observed(w.rpc)
        calls = w.calls()
        require_equal([r["state"] for r in D.invocations(w.ledger, waiting.task_id)],
                      ["finished", "unknown"])
        require_equal(w.orch.costs.view(waiting.task_id)["counts"], {"settled": 1, "unknown": 1})
        w.runtime()
        await w.orch.reconcile()
        reader, headers = await w.fresh_reader()
        path = "/v1/agent/runs/" + waiting.run_id
        require_equal((await (await reader.get(path)).json())["zustand_code"], S.WAITING_USER)
        view = await w.inquiry()
        require_equal(view["anbietergrenze"]["grund"], "cost_recovery_required")
        require(not view["anbietergrenze"]["fortsetzbar"])
        w.mode("ok")
        require_equal((await reader.post(path + "/resume", json={}, headers=headers)).status, 409)
        for _ in range(3):
            await w.orch.tick()
        require_equal(w.ledger.get_run(waiting.run_id).state, S.WAITING_USER)
        require_equal(observed(w.rpc), before, "unknown result must never start another native turn")
        require_equal(w.calls(), calls, "no assessment or fresh planning over the unknown result")
        require_equal(w.ledger.get_run(waiting.run_id).assessment_calls, 0)
        require_equal(len(w.ledger.runs_for_task(waiting.task_id)), 1)


async def _recommendation_handoff(*, restart=False, missing=False):
    objective = "Vergleiche zwei Spazierwege mit Cafe und empfehle einen mit Quellen."
    recommendation = ("Ich empfehle Weg A mit Cafe am Fluss. "
                      + "Die kurze Route verbindet Wasser und Architektur. " * 13
                      + "Bei Hochwasser ist meine Empfehlung ungueltig.")
    material = {
        "recommended_path": recommendation,
        "assumptions": ["Die Anreise erfolgt mit dem oeffentlichen Nahverkehr."],
        "uncertainties": ["Die kurzfristige Oeffnung des Cafes wurde nicht bestaetigt."],
        "risk_notes": ["Bei Hochwasser ist der Uferweg gesperrt."],
        "rejected_alternatives": ["Weg B verlangt mehr Stufen und ist fuer diesen Ausflug die zweite Wahl."],
    }
    required = (recommendation, *(value[0] for key, value in material.items()
                                  if key != "recommended_path"))
    answer = {"findings": [f"Belegter Vergleichspunkt {n}: Weg A und Weg B haben ein Cafe."
                           for n in range(12)],
              "evidence": SOURCES, "confidence": "hoch", **material}
    if missing:
        answer["recommended_path"] = ""
    plan = {"schritte": [{"art": "specialist", "profil": "researcher/hermes",
                          "auftrag": objective}],
            "anforderungen": {"auskunft": [{"id": "a1", "text": objective}],
                "handlungen": [], "unklar": [], "belege": {"mindestens": 2}}}
    followup = {**plan, "schritte": [{"art": "specialist", "profil": "researcher/hermes",
        "auftrag": "Ergaenze eine begruendete Empfehlung fuer die bereits verglichenen Spazierwege."}]}
    alternative = {**plan, "schritte": [{"art": "specialist", "profil": "researcher/hermes",
        "auftrag": "Pruefe alternative oeffentliche Ortsbeschreibungen fuer die fehlende Spazierwegempfehlung."}]}
    async with world(objective=objective, plan=plan, answer=answer,
                     assessment_required=required, followup_plan=followup if missing else None,
                     alternative_plan=alternative if missing else None) as w:
        accepted, grant = await w.admit()
        await w.detach()
        if restart:
            await w.tick_until(accepted["run_id"], S.VERIFYING)
            require_equal([c["kind"] for c in w.calls()], ["plan"])
            w.runtime()
            await w.orch.reconcile()
        final = await w.tick_until(accepted["run_id"], S.FAILED if missing else S.SUCCEEDED)
        calls = w.calls()
        # A concrete quality gap gets two distinct bounded follow-ups through
        # the same real transports. This peer returns an identical result, so its
        # bound negative snapshot is reused instead of paying to judge it twice.
        require_equal([c["kind"] for c in calls], ["plan", "assessment"] + (["plan", "plan"] if missing else []))
        require_equal(len(method_rows(w.rpc, "turn/start")), 3 if missing else 1)
        require_equal(final.plan_revision, 2 if missing else 0)
        require_equal(final.assessment_calls, 1)
        require_equal(w.orch.task_authority.for_run(final.run_id).reference, grant.reference)
        assessment = json.loads(next(c for c in calls if c["kind"] == "assessment")["request"]["ergebnis"])
        assessed = "\n".join(assessment["befunde"])
        if missing:
            require_equal(final.failure_category, "goal_unverified")
            require(recommendation not in assessed, "missing content must never be manufactured")
            require(json.loads(final.completion_verdict)["weiterarbeit_noetig"])
        else:
            require(all(text in assessed for text in required), assessed)
            require(not any(text in "\n".join(assessment["quellen"]) for text in required),
                    "recommendations and caveats are not source-count evidence")
            report = next(a for a in w.ledger.artifacts_for_run(final.run_id) if a.kind == "report")
            report_body = json.loads(Path(report.path).read_text())
            require_equal(report_body["herkunft"], "untrusted_executor")
            require(all(text in "\n".join(report_body["befunde"]) for text in required))
            require(all(text in "\n".join(report_body["befunde"]) for text in answer["findings"]),
                    "preserving the recommendation must not displace the compared options")
            notices = [n for n in await w.proactive.unread() if n["lauf"] == final.run_id]
            require_equal(len(notices), 1)
            require("Ich empfehle Weg A" in json.dumps(notices, ensure_ascii=False))
            w.runtime()
            await w.orch.reconcile()
            await w.orch.tick()
            reader, _ = await w.fresh_reader()
            response = await reader.get("/v1/agent/runs/" + final.run_id)
            require_equal(response.status, 200)
            view = await response.json()
            require_equal(view["zustand_code"], S.SUCCEEDED)
            require(all(text in "\n".join(view["befunde"]) for text in required))
            require(all(text in "\n".join(view["befunde"]) for text in answer["findings"]))
            inquiry = await w.inquiry()
            require(all(text in "\n".join(inquiry["befunde"]) for text in required))
            require_equal(w.calls(), calls, "fresh readers must never dispatch another assessment")
            require_equal(len(method_rows(w.rpc, "turn/start")), 1)


async def t_public_recommendation_and_caveats_reach_assessment_report_and_fresh_reader():
    await _recommendation_handoff()


async def t_public_recommendation_and_caveats_survive_restart_before_assessment():
    await _recommendation_handoff(restart=True)


async def t_public_missing_recommendation_still_fails_completion():
    await _recommendation_handoff(missing=True)


async def _quality_criterion(*, fulfilled=False, weakened_requirement=False):
    """Real public contract; semantic judgements are explicitly local fixtures.

    This proves that a partial answer gets two bounded refinements and reaches
    the incomplete-result path with one final report. It cannot prove a model interprets the
    instruction correctly; that remains a separate semantic acceptance.
    """
    objective = ("Empfiehl mir einen Stadtspaziergang fuer einen freien Nachmittag. "
                 "Beruecksichtige meine vorhandenen Vorlieben und begruende die Wahl.")
    recommendation = ("Ich empfehle den ruhigen Gartenweg statt des Strassenfests: "
                      "Er passt zu deinem gespeicherten Wunsch nach ruhigen Ausfluegen."
                      if fulfilled else
                      "Ich empfehle als allgemeine Alternative den Gartenweg. "
                      "Die vorhandenen Vorlieben wurden nicht in die Auswahl einbezogen; "
                      "diese Empfehlung erfuellt den persoenlichen Teil des Auftrags nicht.")
    caveat = "Das Wetter am Ausflugstag steht noch nicht fest."
    answer = {"findings": ["Der Gartenweg und das Strassenfest sind zwei unterschiedliche Optionen."],
              "evidence": SOURCES, "recommended_path": recommendation,
              "uncertainties": [caveat], "confidence": "mittel",
              "assumptions": [], "rejected_alternatives": [], "risk_notes": []}
    criteria = [{"id": "a1", "text": "Einen Stadtspaziergang vorschlagen und begruenden."},
                {"id": "a2", "text": "Die Wahl an vorhandenen persoenlichen Vorlieben ausrichten."}]
    if weakened_requirement:
        criteria = [{"id": "a1", "text": (
            "Einen Stadtspaziergang vorschlagen und begruenden; "
            "derzeit sind keine Vorlieben verfuegbar, daher allgemein.")}]
    plan = {"schritte": [{"art": "specialist", "profil": "researcher/hermes",
                          "auftrag": objective}],
            "anforderungen": {"auskunft": criteria, "handlungen": [], "unklar": [],
                               "belege": {"mindestens": 2}}}
    open_point = "Die verlangte Ausrichtung an persoenlichen Vorlieben wurde nicht geliefert."
    cited_recommendation = "Empfehlung (Spezialist): " + recommendation
    verdict = {"beantwortet": [{"id": "a1", "belege": [cited_recommendation, *SOURCES]}],
               "offen": [], "fehlend": [], "unsicher": [], "weiterarbeit_noetig": False}
    if fulfilled:
        verdict["beantwortet"].append({"id": "a2", "belege": [cited_recommendation]})
    else:
        # A weakened original binding is caught by the ORIGINAL objective
        # coverage check; no unknown a2 is invented or stamped into its ledger.
        verdict["offen"] = [] if weakened_requirement else ["a2"]
        verdict["fehlend"] = [open_point]
        verdict["weiterarbeit_noetig"] = True
    expected = S.SUCCEEDED if fulfilled else S.FAILED
    followup = {**plan, "schritte": [{"art": "specialist", "profil": "researcher/hermes",
        "auftrag": "Pruefe gezielt, wie die vorhandenen Vorlieben die Wahl des Spazierwegs begruenden."}]}
    alternative = {**plan, "schritte": [{"art": "specialist", "profil": "researcher/hermes",
        "auftrag": "Pruefe alternative oeffentliche Routenbeschreibungen gegen die gebundenen Auswahlkriterien."}]}
    async with world(objective=objective, plan=plan, answer=answer, verdict=verdict,
                     followup_plan=None if fulfilled else followup,
                     alternative_plan=None if fulfilled else alternative) as w:
        accepted, grant = await w.admit()
        await w.detach()
        final = await w.tick_until(accepted["run_id"], expected)
        calls = w.calls()
        invocations = 1 if fulfilled else 3
        # The subsequent synthetic research deliberately adds no information.
        # Its unchanged snapshot keeps the original negative judgement.
        require_equal([call["kind"] for call in calls], ["plan", "assessment"] +
                      ([] if fulfilled else ["plan", "plan"]))
        require_equal(final.assessment_calls, 1)
        require_equal(final.plan_revision, invocations - 1)
        require_equal(len(method_rows(w.rpc, "turn/start")), invocations)
        require_equal(w.orch.task_authority.for_run(final.run_id).reference, grant.reference)
        assessment = next(call for call in calls if call["kind"] == "assessment")["request"]
        require_equal(assessment["originalauftrag"], objective)
        require_equal(assessment["gebundene_anforderungen"]["auskunft"], criteria)
        assessed = json.loads(assessment["ergebnis"])
        require(cited_recommendation in assessed["befunde"], "fixture must cite the actual snapshot line")
        require(caveat in "\n".join(assessed["befunde"]), "incidental caveat was discarded")
        persisted = json.loads(final.completion_verdict)
        require_equal(persisted["weiterarbeit_noetig"], not fulfilled)
        if not fulfilled:
            require_equal(final.failure_category, "goal_unverified")
            require(open_point in final.result_summary)
        report = next(a for a in w.ledger.artifacts_for_run(final.run_id) if a.kind == "report")
        report_body = json.loads(Path(report.path).read_text())
        require(recommendation in "\n".join(report_body["befunde"]), "partial result was lost")
        notices = [n for n in await w.proactive.unread() if n["lauf"] == final.run_id]
        require_equal(len(notices), 1)
        w.runtime()
        await w.orch.reconcile()
        await w.orch.tick()
        reader, _ = await w.fresh_reader()
        response = await reader.get("/v1/agent/runs/" + final.run_id)
        require_equal(response.status, 200)
        view = await response.json()
        require_equal(view["zustand_code"], expected)
        require(recommendation in "\n".join(view["befunde"]))
        inquiry = await w.inquiry()
        require(recommendation in "\n".join(inquiry["befunde"]))
        require_equal(w.ledger.get_task(final.task_id).requirements,
                      json.dumps(assessment["gebundene_anforderungen"], ensure_ascii=False,
                                 sort_keys=True))
        require_equal(w.ledger.get_run(final.run_id).completion_verdict, final.completion_verdict)
        require_equal(w.calls(), calls, "reading/restart repeated a semantic assessment")
        require_equal(len(method_rows(w.rpc, "turn/start")), invocations)
        require_equal(len(w.ledger.runs_for_task(final.task_id)), 1)


async def t_public_explaining_an_unmet_quality_criterion_preserves_partial_result_once():
    await _quality_criterion()
    await _variant_evidence("conflicting")
    await _variant_evidence("blocked")


async def t_public_context_weakened_requirement_does_not_replace_the_original_goal():
    await _quality_criterion(weakened_requirement=True)


async def t_public_fulfilled_quality_criteria_can_succeed_with_an_incidental_caveat():
    await _quality_criterion(fulfilled=True)
    await _variant_evidence("matched")


async def _variant_evidence(case):
    """Protocol/evidence/completion countercases, NOT a model-quality proof.

    The native research answer and semantic verdict are explicit local doubles.
    The public dispatch, transmitted prompts, assessment snapshot, bounded
    refinements, preserved partial report and final state are real Core paths.
    No shop or browser is contacted.
    """
    matched = case == "matched"
    variant = "https://shop.invalid/boxes/blue-40"
    other = "https://shop.invalid/boxes/red-60"
    objective = ("Finde eine konkret bestellbare blaue Aufbewahrungsbox mit den Aussenmassen "
                 "40 x 30 x 25 cm und einem Artikelpreis unter 30 EUR. Belege die Variante direkt.")
    finding = ("Variante Blau 40, Kennung B40, " + variant + ": " +
        ("Direkt geoeffnete Variantenseite nennt 40 x 30 x 25 cm, Artikelpreis 24 EUR."
         if matched else
         "Auswahllabel nennt 40 x 30 x 25 cm; dieselbe Seitentabelle nennt 60 x 40 x 30 cm."
         if case == "conflicting" else
         "Suchtreffer nennt 40 x 30 x 25 cm und 24 EUR; direkter Seitenzugriff ist blockiert."))
    limitation = ("Versandkosten sind noch offen; 24 EUR ist nur der Artikelpreis."
                  if matched else
                  "Die geforderten Masse sind wegen widerspruechlicher Angaben nicht bestaetigt."
                  if case == "conflicting" else
                  "Variante, Masse und aktueller Artikelpreis sind nicht direkt bestaetigt.")
    recommendation = ("Blau 40 erfuellt die verlangten Varianteneigenschaften; Versand gesondert pruefen."
                      if matched else
                      "Bedingter Hinweis auf Blau 40; kein verifizierter passender Fund. " + limitation)
    sources = ["Variantenseite: " + variant, "Andere Variante (kein Ersatzbeleg): " + other]
    answer = {"findings": [finding], "evidence": sources, "recommended_path": recommendation,
              "uncertainties": [limitation], "confidence": "mittel", "assumptions": [],
              "rejected_alternatives": ["Rot 60 hat andere Masse: " + other], "risk_notes": []}
    criteria = [{"id": "a1", "text": objective}]
    plan = {"schritte": [{"art": "specialist", "profil": "researcher/hermes", "auftrag": objective}],
            "anforderungen": {"auskunft": criteria, "handlungen": [], "unklar": [],
                               "belege": {"mindestens": 1}}}
    verdict = {"beantwortet": [{"id": "a1", "belege": [finding, sources[0]]}] if matched else [],
               "offen": [] if matched else ["a1"], "fehlend": [] if matched else [limitation],
               "unsicher": [], "weiterarbeit_noetig": not matched}
    followup = {**plan, "schritte": [{"art": "specialist", "profil": "researcher/hermes",
        "auftrag": "Pruefe die konkrete Variante und klaere genau die offene Eigenschaft: " + limitation}]}
    alternative = {**plan, "schritte": [{"art": "specialist", "profil": "researcher/hermes",
        "auftrag": "Pruefe alternative oeffentliche Anbieterbelege fuer die gebundene Produktvariante: " + limitation}]}
    async with world(objective=objective, plan=plan, answer=answer, verdict=verdict,
                     followup_plan=None if matched else followup,
                     alternative_plan=None if matched else alternative) as w:
        # Only the local RPC fixture emits these synthetic observations.
        executable = Path(w.native.codex_bin)
        source = executable.read_text()
        action = "{'type':'openPage','url':'https://example.org/source'}"
        require_equal(source.count(action), 1)
        replacement = ({"type": "search", "query": "Blau 40 Box"} if case == "blocked" else
                       {"type": "openPage", "url": variant})
        executable.write_text(source.replace(action, repr(replacement)))
        (w.rpc / "sources.json").write_text(json.dumps([variant, other]))
        accepted, grant = await w.admit()
        await w.detach()
        expected = S.SUCCEEDED if matched else S.FAILED
        final = await w.tick_until(accepted["run_id"], expected)
        turns = method_rows(w.rpc, "turn/start")
        require_equal(len(turns), 1 if matched else 3)
        for turn in turns:
            transmitted = json.dumps(turn["params"]["input"], ensure_ascii=False)
            require("konkrete Variante" in transmitted, "direct variant instruction missing at native transport")
            require("bedingten Hinweis" in transmitted, "blocked-source instruction missing at native transport")
        assessment = next(call for call in w.calls() if call["kind"] == "assessment")
        require("Andere Varianten" in assessment["instruction"], "variant distinction missing at assessor")
        require_equal(assessment["request"]["originalauftrag"], objective)
        snapshot = assessment["request"]["ergebnis"]
        require(all(item in snapshot for item in (variant, other, finding, limitation, recommendation)))
        require_equal(final.assessment_calls, 1, "unchanged refinement must not loop through assessments")
        require_equal(final.plan_revision, 0 if matched else 2)
        require_equal(w.orch.task_authority.for_run(final.run_id).reference, grant.reference)
        if not matched:
            require_equal(final.failure_category, "goal_unverified")
            require(limitation in final.result_summary)
        report = next(a for a in w.ledger.artifacts_for_run(final.run_id) if a.kind == "report")
        body = json.loads(Path(report.path).read_text())
        require(recommendation in "\n".join(body["befunde"]), "useful conditional result disappeared")
        reader, _ = await w.fresh_reader()
        view = await (await reader.get("/v1/agent/runs/" + final.run_id)).json()
        require_equal(view["zustand_code"], expected)
        require(recommendation in "\n".join(view["befunde"]))
    # The shared research instruction must not change a builder's contract.
    for profile in ("researcher/hermes", SP.CLAUDE_RESEARCH_PROFILE, "builder/codex"):
        prompt = SP.build_prompt(SP.profile(profile), SP.SpecialistRequest(profile, objective, ""))
        require_equal("konkrete Variante" in prompt, profile in SP.RESEARCH_PROFILES)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
