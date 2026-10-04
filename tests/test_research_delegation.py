"""Regression for the public retest: full assignment, bounded rework, clear history."""
import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_research_refinement as T
from solvio.agent_runtime import personal_context as PC, specialists as SP, planner as PL


def t_oversized_plan_instruction_is_rejected_instead_of_silently_losing_its_tail():
    instruction = "Prüfe öffentliche Angebote. " * 30 + "Rollen und Griffe einschließen."
    try:
        PL.validate({"schritte": [{"art": "specialist", "profil": "researcher/hermes",
            "auftrag": instruction}]}, scope="research", allowed_profiles={"researcher/hermes"},
            known_capabilities=set(), goal=T.GOAL)
    except PL.PlanInvalid as exc:
        require_equal(exc.reason, "instruction_too_long")
    else:
        raise AssertionError("accepted a truncated instruction")


def t_refinement_receives_complete_bound_task_criteria_evidence_and_gaps():
    with T.world() as w:
        final = asyncio.run(T.drive(w))
        require_equal(final.state, T.S.SUCCEEDED)
        require_equal(len(w.requests), 2)
        first, second = [json.loads(r.research_briefing) for r in w.requests]
        require_equal(first["originalauftrag"], T.GOAL)
        require_equal(second["originalauftrag"], T.GOAL)
        bound = json.loads(w.ledger.get_task(w.task.task_id).requirements)
        require_equal(second["gebundene_anforderungen"], bound)
        require_equal(first["bisherige_ergebnisse"], [])
        require("Erstes Angebot" in json.dumps(second["bisherige_ergebnisse"]))
        require(T.GAP in json.dumps(second["pruefhinweise"]))
        prompt = SP.build_prompt(SP.profile(w.requests[1].profile), w.requests[1])
        require(w.requests[1].research_briefing in prompt,
                "native worker prompt lost the complete assignment")
        require_equal((final.plan_revision, final.assessment_calls), (1, 2))


def t_public_only_request_skips_recall_before_reading_even_with_short_query():
    async def probe():
        for goal in ("Nur öffentliche Quellen verwenden.",
                     "Keine persönlichen Daten, Mails oder Kalender verwenden.",
                     "Bitte ohne mein Gedächtnis recherchieren.",
                     "Use only public sources.", "Do not use my personal data."):
            with patch.object(PC, "_read", AsyncMock(side_effect=AssertionError("private read"))) as read:
                result = await PC.for_call(object(), query="kurzer Suchtext",
                    fallback_query=goal, history="Öffentlicher Zwischenbefund")
                require_equal(read.await_count, 0)
                require_equal(result, "Öffentlicher Zwischenbefund")
        require(not PC.excludes_personal_context("Berücksichtige meine Vorlieben für Ausflüge."))
    asyncio.run(probe())


def t_public_only_chat_does_not_read_other_chats_or_personal_memories():
    from solvio.conversation.processing import ConversationProcessor
    from solvio.conversation import related_context as RC
    async def probe():
        with patch.object(RC, "collect", side_effect=AssertionError("related chat read")) as collect:
            # No usable runtime: any attempted context lookup must fail.
            result = await ConversationProcessor._related_context(
                object(), {}, "Nur öffentliche Quellen verwenden.", None)
            require_equal(collect.call_count, 0)
            require_equal(result.context, "")
    asyncio.run(probe())


def t_oversized_research_context_never_reaches_a_native_dispatch():
    with T.world() as w:
        with patch.object(T.RQ, "MAX_EVALUATION_CHARS", 100):
            final = asyncio.run(T.drive(w))
            require_equal(final.state, T.S.FAILED)
            require_equal(w.requests, [])


def t_latest_research_and_previous_findings_are_separate_without_erasing_uncertainty():
    with T.world(second_good=False) as w:
        final = asyncio.run(T.drive(w))
        require_equal(final.state, T.S.FAILED)
        require_equal(final.failure_category, "goal_unverified")
        require_equal([r.objective for r in w.requests], [T.FIRST, T.NEXT, T.THIRD])
        require_equal([r.research_strategy for r in w.requests], ["", "direct_sources", "alternative_sources"])
        require_equal((final.plan_revision, len(w.planner.plans), final.assessment_calls), (2, 3, 3))
        third = json.loads(w.requests[2].research_briefing)
        require_equal(third["originalauftrag"], T.GOAL)
        require_equal(third["gebundene_anforderungen"], json.loads(w.ledger.get_task(w.task.task_id).requirements))
        require_equal(len(third["bisherige_ergebnisse"]), 2)
        for section, label in zip(third["bisherige_ergebnisse"], ("Erstes Angebot", "Zweites Angebot")):
            require(label in "\n".join(section["findings"]))
            require(T.SOURCE in section["evidence"])
        require(T.GAP in json.dumps(third, ensure_ascii=False))
        assessment = w.planner.assessments[-1]["snapshot_body"]
        for value in ("Erstes Angebot", "Zweites Angebot", "Endpreis inklusive", T.SOURCE, T.GAP):
            require(value in assessment, "final assessment lost a prior or latest result")
        report = next(a for a in w.ledger.artifacts_for_run(final.run_id) if a.kind == "report")
        text = Path(report.path).read_text()
        require("Nachprüfung" in text)
        require("Frühere Recherche" in text)
        require(text.index("Endpreis inklusive") < text.index("Zweites Angebot") < text.index("Erstes Angebot"))
        require(T.GAP in text, "the prior qualification disappeared")
        require(T.SOURCE in text, "the source evidence disappeared")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
