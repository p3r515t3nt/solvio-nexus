"""Three bounded research results remain assessable without duplicating sources."""
import asyncio
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_research_refinement import world, drive, SP, PL, RQ, S, O, CP, SpecialistResult


def qualified_result(n, objective):
    result = SpecialistResult("scout", "codex", objective, ok=True,
        findings=[f"Recherche {n}, Fund {i}: " + "Datum und Bedingungen bleiben zu prüfen. " * 22
                  + "Der frühere Preis ist NICHT bestätigt." for i in range(6)],
        evidence=[f"https://example.org/recherche-{n}/quelle-{i}/" + "public-route-" * 9
                  for i in range(20)],
        assumptions=[f"Recherche {n}: " + "Ein Reisender, nur Hinflug. " * 5],
        uncertainties=[f"Recherche {n}: " + "Gepäck und Gebühren bleiben offen. " * 5],
        rejected_alternatives=[f"Recherche {n}: " + "Anderen Flughafen getrennt vergleichen. " * 4],
        risk_notes=[f"Recherche {n}: " + "Keine Buchung ohne bestätigten Endpreis. " * 4],
        recommended_path=f"Recherche {n}: Ein vollständiger Vergleich benötigt bestätigte Bedingungen.")
    # Specialist field validation strips outer whitespace before retention.
    for name in ("findings", "evidence", "assumptions", "uncertainties",
                 "rejected_alternatives", "risk_notes"):
        setattr(result, name, [value.strip() for value in getattr(result, name)])
    return result


def t_three_result_assessments_fit_without_losing_history_or_qualifications():
    with world(second_good=False) as w:
        results = []
        hook_errors = []
        assessment_sizes = []
        real_assess = O.Orchestrator._assess

        async def observe_size(orch, run, context):
            public = RQ.snapshot_body(orch._result_findings(context), context.sources)
            compact = RQ.snapshot_body(orch._result_findings(context, assessment=True),
                                       context.sources)
            assessment_sizes.append((len(context.result_sections), context.result_sections_complete,
                                     len(orch._checkpoint_blob(context)), len(public), len(compact)))
            return await real_assess(orch, run, context)

        async def research(request, **kw):
            w.planner.capture_scope("specialist")
            w.requests.append(request)
            result = qualified_result(len(w.requests), request.objective)
            results.append(result)
            return SP.SpecialistRun(result=result, provider="codex",
                                    billing_mode="subscription", auth="chatgpt")

        async def check_assessment(n):
            # The same actual request builder enforces the unchanged 36k cap.
            call = w.planner.assessments[-1]
            PL.build_assessment_request(objective=call["objective"], bound=call["bound"],
                                        snapshot_body=call["snapshot_body"])
            body = json.loads(call["snapshot_body"])
            text = "\n".join(body["befunde"])
            for result in results:
                for field in ("findings", "assumptions", "uncertainties",
                              "rejected_alternatives", "risk_notes"):
                    for value in getattr(result, field):
                        require(value in text, field + " was lost")
                for value in result.evidence:
                    require_equal(body["quellen"].count(value), 1,
                                  "each complete source must remain exactly once")
            if n == 3:
                require(len(call["snapshot_body"]) < RQ.MAX_EVALUATION_CHARS)

        async def observed_assessment(n):
            try:
                await check_assessment(n)
            except Exception as exc:
                hook_errors.append(type(exc).__name__ + ": " + str(exc))
                raise
        w.planner.assessment_hook = observed_assessment
        with patch.object(SP, "run_specialist", research), patch.object(O.Orchestrator, "_assess", observe_size):
            final = asyncio.run(drive(w))
        require_equal((len(w.requests), final.assessment_calls, final.plan_revision), (3, 3, 2),
                      repr((final.failure_category, hook_errors, assessment_sizes)))
        third = next(size for size in assessment_sizes if size[0] == 3)
        require(third[1] and third[2] <= CP.MAX_CHECKPOINT, "complete material must fit checkpoint")
        require(third[3] > RQ.MAX_EVALUATION_CHARS,
                "counterfactual duplicate projection must cross the existing cap")
        require_equal((final.state, final.failure_category), (S.FAILED, "goal_unverified"),
                      "being assessable never confirms unchecked prices")
        require_equal(len(w.planner.assessments), 3)
        # The public output retains the source-to-step association and every
        # complete qualification, even though the assessment uses references.
        report = next(a for a in w.ledger.artifacts_for_run(final.run_id) if a.kind == "report")
        with open(report.path, encoding="utf-8") as stream:
            public = json.load(stream)
        text = "\n".join(public["befunde"])
        for result in results:
            for value in result.evidence + result.findings + result.risk_notes:
                require(value in text, "public result lost complete history")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
