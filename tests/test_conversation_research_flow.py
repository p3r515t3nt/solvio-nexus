"""Chat message -> native research -> result in the same authenticated chat.

Reuse the real HTTPS/chat and installed Hermes protocol worlds. Only the
classifier, planner/assessor and native model replies are local fixtures.
No production data, external model, web request or real device is involved.
"""
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_agent_public_research as R
import test_conversation_processing as T
from solvio.agent_runtime import requirements as RQ


class ChatResearchWorld(R.ResearchWorld, T.ProcWorld):
    def runtime(self):
        runtime = super().runtime()
        self.bag.agent_runtime = runtime
        self.bag.capabilities = self.router
        return runtime


@asynccontextmanager
async def world(**options):
    with patch.object(R, "ResearchWorld", ChatResearchWorld):
        async with R.world(**options) as w:
            yield w
            await w.processor.wait_idle(20)


async def start_chat_research(w, *, iphone=False, route="auftrag_recherche"):
    cid = await w.chat()
    # The classifier's paraphrase must never replace the actual user request.
    w.cli.assessments.append(T.assessment(route, "Andere erfundene Suche",
                                         auftragsprofil="spezialisiert"))
    native_quote = w.orch.cost_quote_adapter
    with patch.object(T.U, "text_invocation", return_value=T.L.Invocation(
            sys.executable, ("-c", "pass"), cwd=str(w.folder), timeout=10)), \
            patch.object(T.P, "codex_status", AsyncMock(return_value=T.AUTH)), \
            patch.object(w.orch, "cost_quote_adapter", lambda *_: T.FREE):
        if iphone:
            ctx, client, headers = await w.device()
            message = {"conversation_id": cid, "client_message_id": "research-iphone-001",
                       "text": w.objective}
            body = await w.app_proof(ctx, client, headers, message)
            response = await client.post(f"{T.CE.PREFIX}/{cid}/messages", json=body, headers=headers)
            require_equal(response.status, 202, await response.text())
            row = await w.settled(cid, (await response.json())["delivery_id"])
        else:
            row = await w.ask(cid, w.objective)
    require_equal(w.orch.cost_quote_adapter, native_quote)
    require_equal(row["status"], "completed", row["error_code"])
    task = w.ledger.get_task(row["task_id"])
    require_equal((task.scope, task.objective, task.conversation_ref),
                  ("research", w.objective, cid))
    require_equal(w.tasks(), 1)
    require_equal(len(w.cli.of("assess")), 1)
    require_equal(len(w.cli.of("answer")), 0)
    return cid, row


async def completed_chat_flow(*, iphone):
    async with world() as w:
        cid, row = await start_chat_research(w, iphone=iphone)
        other = await w.chat("research-other-chat")
        await w.detach()
        run = await w.tick_until(row["run_id"], R.S.SUCCEEDED)
        calls = w.calls()
        require_equal([c["kind"] for c in calls], ["plan", "assessment"])
        require_equal(len(R.method_rows(w.rpc, "turn/start")), 1)
        w.runtime()
        await w.orch.reconcile()
        reader, _ = await w.fresh_reader()
        response = await reader.get(f"{T.CE.PREFIX}/{cid}")
        require_equal(response.status, 200)
        chat = await response.json()
        require_equal(len(chat["auftraege"]), 1)
        view = chat["auftraege"][0]
        require_equal((view["id"], view["zustand_code"], view["auftrag"]),
                      (run.run_id, R.S.SUCCEEDED, w.objective))
        require(view["befunde"], "chat lost the research result")
        require(all(source in view["quellen"] for source in R.SOURCES),
                "chat lost the native sources")
        delivery = await (await reader.get(
            f"{T.CE.PREFIX}/{cid}/deliveries/{row['delivery_id']}")).json()
        require_equal(delivery["run_id"], run.run_id)
        other_view = await (await reader.get(f"{T.CE.PREFIX}/{other}")).json()
        require_equal(other_view["auftraege"], [])
        require_equal(w.calls(), calls, "reopening a chat dispatched fresh research")
        notices = [n for n in await w.proactive.unread() if n["lauf"] == run.run_id]
        require_equal(len(notices), 1)


async def t_browser_chat_research_keeps_user_goal_and_sources_after_logout_and_restart():
    await completed_chat_flow(iphone=False)


async def t_attested_iphone_chat_research_reaches_the_same_result_after_disconnect():
    await completed_chat_flow(iphone=True)


async def t_six_required_sources_reach_assessment_without_losing_the_contract():
    sources = [f"https://example.org/offer-{n}" for n in range(6)]
    objective = "Vergleiche drei Angebote mit je zwei verschiedenen Quellen."
    plan = {**R.PLAN, "anforderungen": {"auskunft": [{"id": "a1", "text": objective}],
        "handlungen": [], "unklar": [], "belege": {"mindestens": 6}}}
    answer = {"findings": ["Drei Angebote mit jeweils zwei Quellen verglichen."],
        "evidence": sources, "confidence": "mittel", "recommended_path": "Vergleich liegt vor.",
        "assumptions": [], "uncertainties": [], "risk_notes": [], "rejected_alternatives": []}
    verdict = {**R.VERDICT, "beantwortet": [{"id": "a1", "belege": sources}]}
    async with world(objective=objective, plan=plan, answer=answer, verdict=verdict) as w:
        cid, row = await start_chat_research(w)
        final = await w.tick_until(row["run_id"], R.S.SUCCEEDED)
        bound = json.loads(w.ledger.get_task(row["task_id"]).requirements)
        require_equal(bound["belege"]["mindestens"], 6)
        require_equal(final.assessment_calls, 1)
        require_equal([c["kind"] for c in w.calls()], ["plan", "assessment"])
        view = (await (await w.client.get(f"{T.CE.PREFIX}/{cid}")).json())["auftraege"][0]
        require(all(source in view["quellen"] for source in sources))


async def t_missing_research_contract_is_repaired_before_any_native_research():
    async with world(plan={"schritte": R.PLAN["schritte"]}, followup_plan=R.PLAN) as w:
        _, row = await start_chat_research(w)
        final = await w.tick_until(row["run_id"], R.S.SUCCEEDED)
        require_equal([c["kind"] for c in w.calls()], ["plan", "plan", "assessment"])
        require_equal(final.planner_calls, 2)
        require_equal(len(R.method_rows(w.rpc, "turn/start")), 1)
        require(w.ledger.get_task(row["task_id"]).requirements)


async def t_long_step_is_repaired_and_native_worker_gets_full_original_goal():
    objective = ("Vergleiche öffentliche Angebote. " * 45
                 + "Schlussvorgabe: Maße einschließlich Rollen und fester Griffe prüfen.")
    requirements = {**R.PLAN["anforderungen"], "auskunft": [
        {"id": "a1", "text": "Maße einschließlich Rollen und fester Griffe prüfen."}]}
    short = {**R.PLAN, "anforderungen": requirements}
    long = {**short, "schritte": [{"art": "specialist", "profil": "researcher/hermes",
                                  "auftrag": objective}]}
    async with world(objective=objective, plan=long, followup_plan=short) as w:
        _, row = await start_chat_research(w)
        final = await w.tick_until(row["run_id"], R.S.SUCCEEDED)
        require_equal(final.planner_calls, 2)
        turns = R.method_rows(w.rpc, "turn/start")
        require_equal(len(turns), 1)
        native = json.dumps(turns, ensure_ascii=False)
        require(objective in native, "native protocol lost the tail of the owner goal")
        require("gebundene_anforderungen" in native)
        require("Schlussvorgabe" in native)


async def t_unusable_research_contract_stops_after_one_repair_without_dispatch():
    for block in (None, {**R.PLAN["anforderungen"], "belege": {"mindestens": 13}}):
        plan = {**R.PLAN, "anforderungen": block}
        async with world(plan=plan) as w:
            _, row = await start_chat_research(w)
            final = await w.tick_until(row["run_id"], R.S.FAILED)
            require_equal(final.planner_calls, 2)
            require_equal(final.assessment_calls, 0)
            require_equal([c["kind"] for c in w.calls()], ["plan", "plan"])
            require_equal(R.method_rows(w.rpc, "turn/start"), [],
                          "research dispatched without usable completion criteria")
            require_equal(w.ledger.get_task(row["task_id"]).requirements, "")


async def t_unmet_shopping_constraint_stays_visible_in_chat_after_bounded_research():
    objective = "Finde einen weichen Koffer mit 46 x 31 x 78 cm, Preis und Angebotslink."
    gap = "Die Hoehe 78 cm ist beim gefundenen Angebot nicht belegt."
    finding = "Weicher Beispielkoffer, 59 Euro; Breite 46 cm und Tiefe 31 cm."
    answer = {"findings": [finding], "evidence": R.SOURCES, "confidence": "mittel",
              "recommended_path": "Vor einer Auswahl muss die Hoehe bestaetigt werden.",
              "assumptions": [], "uncertainties": [gap], "risk_notes": [],
              "rejected_alternatives": []}
    requirements = {"auskunft": [{"id": "a1", "text": objective}],
                    "handlungen": [], "unklar": [], "belege": {"mindestens": 2}}
    plan = {"schritte": [{"art": "specialist", "profil": "researcher/hermes",
                          "auftrag": objective}], "anforderungen": requirements}
    followup = {**plan, "schritte": [{"art": "specialist", "profil": "researcher/hermes",
                                     "auftrag": "Pruefe gezielt die fehlende Hoehe des Koffers."}]}
    alternative = {**plan, "schritte": [{"art": "specialist", "profil": "researcher/hermes",
        "auftrag": "Pruefe das oeffentliche Herstellerdatenblatt und alternative Anbieter zur fehlenden Kofferhoehe."}]}
    findings = [finding,
                "Direkte Angebotsseite nennt das Aussenmass ohne Rollen; die Gesamthoehe bleibt offen.",
                "Alternatives Herstellerdatenblatt nennt keine Gesamthoehe inklusive Rollen."]
    sources = [f"https://example.org/shopping-stage-{n}" for n in range(1, 4)]
    answers = [{**answer, "findings": [value], "evidence": [*R.SOURCES, source]}
               for value, source in zip(findings, sources)]
    verdict = {"beantwortet": [], "offen": ["a1"], "fehlend": [gap],
               "unsicher": [], "weiterarbeit_noetig": True}
    async with world(objective=objective, plan=plan, answer=answers[0],
                     verdict=verdict, followup_plan=followup, alternative_plan=alternative) as w:
        cid, row = await start_chat_research(w)
        # Each local native reply describes the actual distinct strategy. Move
        # the fixture only after the preceding research reached verification.
        for index in range(3):
            await w.tick_until(row["run_id"], R.S.VERIFYING)
            require_equal(len(R.method_rows(w.rpc, "turn/start")), index + 1)
            if index < 2:
                (w.rpc / "answer.json").write_text(json.dumps(answers[index + 1], ensure_ascii=False))
        final = await w.tick_until(row["run_id"], R.S.FAILED)
        require_equal(final.failure_category, "goal_unverified")
        require_equal(len(R.method_rows(w.rpc, "turn/start")), 3,
                      "a concrete missing criterion must reach direct and alternative source research")
        require_equal((final.plan_revision, final.planner_calls, final.assessment_calls), (2, 3, 3))
        calls = w.calls()
        require_equal([c["kind"] for c in calls], ["plan", "assessment"] * 3)
        assessed = next(c for c in reversed(calls) if c["kind"] == "assessment")["request"]
        require_equal(assessed["originalauftrag"], objective)
        require_equal(assessed["gebundene_anforderungen"], RQ.validate(requirements, objective=objective))
        require_equal(assessed["gebundene_anforderungen"], json.loads(w.ledger.get_task(row["task_id"]).requirements))
        require(all(value in assessed["ergebnis"] for value in [*findings, *sources, gap]),
                "the last assessment lost a research stage or its open constraint")
        view = (await (await w.client.get(f"{T.CE.PREFIX}/{cid}")).json())["auftraege"][0]
        require_equal(view["zustand_code"], R.S.FAILED)
        require(all(value in "\n".join(view["befunde"]) for value in findings),
                "one of the three usable partial findings disappeared")
        require(gap in json.dumps(view, ensure_ascii=False), "missing constraint disappeared")
        require_equal(view["auftrag"], objective)
        require(all(source in view["quellen"] for source in [*R.SOURCES, *sources]))
        require_equal(w.tasks(), 1)
        require_equal(w.calls(), calls, "reading the chat dispatched another research stage")


async def short_weather_flow(*, iphone):
    objective = "Wie ist das Wetter heute in Dietzenbach? Bitte aktuell nachsehen."
    finding = "Synthetische Wetterquelle: 18 Grad, Regenwahrscheinlichkeit 30 Prozent."
    plan = {"schritte": [{"art": "specialist", "profil": "researcher/hermes",
                          "auftrag": objective}],
            "anforderungen": {"auskunft": [{"id": "a1", "text": objective}],
                "handlungen": [], "unklar": [], "belege": {"mindestens": 2}}}
    answer = {"findings": [finding], "evidence": R.SOURCES, "confidence": "mittel",
              "recommended_path": finding, "assumptions": [], "uncertainties": [],
              "risk_notes": [], "rejected_alternatives": []}
    async with world(objective=objective, plan=plan, answer=answer,
                     assessment_required=(finding,)) as w:
        cid, row = await start_chat_research(w, iphone=iphone, route="kurzrecherche")
        require_equal(json.loads(row["dispatch"])["route"], "kurzrecherche")
        final = await w.tick_until(row["run_id"], R.S.SUCCEEDED)
        require_equal(len(R.method_rows(w.rpc, "turn/start")), 1,
                      "a current question must actually reach native research")
        view = (await (await w.client.get(f"{T.CE.PREFIX}/{cid}")).json())["auftraege"][0]
        require_equal(view["id"], final.run_id)
        require(finding in json.dumps(view, ensure_ascii=False))
        require(all(source in view["quellen"] for source in R.SOURCES))
        require_equal(len(w.cli.of("answer")), 0,
                      "do not send a research question to the toolless answer model")


async def t_short_weather_question_from_browser_reaches_research_and_same_chat_result():
    await short_weather_flow(iphone=False)


async def t_short_weather_question_from_iphone_reaches_research_and_same_chat_result():
    await short_weather_flow(iphone=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
