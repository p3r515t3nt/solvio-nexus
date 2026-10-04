"""Full specialist conclusions survive the existing bounded checkpoint."""
import copy
import json
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

_TEMP = tempfile.TemporaryDirectory(prefix="solvio-result-checkpoint-")
os.environ["SOLVIO_STATE_DIR"] = _TEMP.name
os.environ["SOLVIO_AGENT_RUNS_DB"] = os.path.join(_TEMP.name, "agent.sqlite3")

from solvio.agent_runtime import checkpoint as CP, planner as PL
from solvio.agent_runtime import orchestrator as O, budget as BU, requirements as RQ


def section():
    return {"step_id": "as-local-result", "confidence": "mittel",
        "recommended_path": "Empfohlen: HafenCity. " + "Begruendung. " * 55 + "Nur bei trockenem Wetter.",
        "findings": [f"Befund {i}" for i in range(12)],
        "evidence": ["https://example.test/offiziell"],
        "assumptions": ["Ein Spaziergang zu Fuss ist moeglich."],
        "uncertainties": ["Oeffnungszeiten sind nicht abschliessend bestaetigt."],
        "rejected_alternatives": ["Blankenese: viele Treppen."],
        "risk_notes": ["Bei Sturm nicht am Wasser entlanggehen."]}


def encode(sections=None, **extra):
    value = section()
    args = dict(plan=PL.Plan(goal="Vergleiche zwei Viertel und empfehle eines.",
                    steps=(PL.PlannedStep(kind="specialist", profile="researcher/hermes",
                                         instruction="Vergleiche und empfehle."),)),
        revision=0, cursor=1, goal_met="", approval_attempts=0, pending_step_id="",
        notes=[], findings=value["findings"], sources=value["evidence"],
        invalid_signatures=set(), attempts={}, low_value={}, research_result_material=True)
    if sections is not None:
        args["result_sections"] = sections
    args.update(extra)
    return CP.encode(**args)


def restore(raw):
    restored, reason = CP.restore(raw, goal="Vergleiche zwei Viertel und empfehle eines.",
        scope="research", allowed_profiles={"researcher/hermes"}, known_capabilities=set())
    require_equal(reason, "restored")
    return restored


def t_complete_conclusions_survive_over_six_hundred_chars_and_all_twelve_findings():
    expected = section()
    expected["evidence"] = [f"Modellbeleg {i}" for i in range(12)] + [
        f"{prefix}https://example.test/{i}?date=2026-10-05"
        for prefix in ("Native Websuche: ", "Nativer Browser: ") for i in range(12)]
    raw = encode([expected])
    path = os.path.join(_TEMP.name, "checkpoint.json")
    with open(path, "w") as stream:
        stream.write(raw)
    with open(path) as stream:
        rebuilt = restore(stream.read())
    require(rebuilt.result_sections_complete)
    require_equal(rebuilt.result_sections, [expected])
    require(len(rebuilt.result_sections[0]["recommended_path"]) > CP.MAX_FINDING_TEXT)
    require(rebuilt.result_sections[0]["recommended_path"].endswith("Nur bei trockenem Wetter."))
    require_equal(len(rebuilt.result_sections[0]["findings"]), 12)
    require_equal(len(rebuilt.findings), 8, "legacy short view stays compatible")
    too_many = copy.deepcopy(expected)
    too_many["evidence"].append("https://example.test/over-evidence-cap")
    require(not restore(encode([too_many])).result_sections_complete)


def t_legacy_checkpoint_without_new_fields_stays_readable():
    raw = encode()
    require("result_sections" not in json.loads(raw))
    old = restore(raw)
    require_equal(old.result_sections, [])
    require(old.result_sections_complete)


def t_oversize_never_returns_a_complete_prefix_of_conclusions():
    original = section()
    for name in CP.RESULT_SECTION_LISTS:
        original[name] = ["x" * 600 for _ in range(12)]
    values = [dict(copy.deepcopy(original), step_id=f"as-{n}") for n in range(3)]
    raw = encode(values)
    require(len(raw) <= CP.MAX_CHECKPOINT)
    rebuilt = restore(raw)
    require(not rebuilt.result_sections_complete)
    require_equal(rebuilt.result_sections, [], "no silently shortened qualification")
    require_equal(len(rebuilt.plan.steps), 1, "the plan remains reconstructable")


def t_incomplete_marker_cannot_be_reset_by_another_checkpoint():
    first = restore(encode([section()], result_sections_complete=False))
    second = restore(encode(first.result_sections,
                            result_sections_complete=first.result_sections_complete))
    require(not second.result_sections_complete)


def t_malformed_new_field_is_not_an_implicit_complete_empty_result():
    body = json.loads(encode([section()]))
    body["result_sections"][0]["confidence"] = []
    damaged = restore(json.dumps(body))
    require_equal(damaged.result_sections, [])
    require(not damaged.result_sections_complete)
    body["result_sections"] = [{"recommended_path": "Nur Erfolg behaupten."}]
    require(not restore(json.dumps(body)).result_sections_complete)


def t_synthetic_secret_in_conclusions_uses_existing_redaction():
    value = section()
    secret = "sk-" + "a" * 40
    value["recommended_path"] = "Empfehlung " + secret
    value["risk_notes"] = ["Nicht weitergeben: " + secret]
    raw = encode([value])
    require(secret not in raw)
    rebuilt = restore(raw)
    require(secret not in json.dumps(rebuilt.result_sections))


def runtime_restore(raw):
    orch = object.__new__(O.Orchestrator)
    def allowed_profiles(task, run_id):
        # Recovery selects the persisted route of this run, not global settings.
        require_equal(run_id, "ar-local", "profile selection must bind the restored run")
        return {"researcher/hermes"}
    orch._allowed_profiles = allowed_profiles
    orch._known_capabilities = lambda run_id: set()
    orch._allowed_needs = lambda run_id: {}
    context = O.RunContext(run_id="ar-local", task_id="at-local", scope="research",
                          ledger=BU.BudgetLedger(BU.Budget.from_dict(None)))
    task = SimpleNamespace(scope="research", objective="Vergleiche zwei Viertel und empfehle eines.")
    # Use the real persisted run model, including its empty provider selection.
    run = O.S.AgentRun(run_id="ar-local", task_id="at-local", plan_checkpoint=raw, plan_revision=0)
    orch._restore_plan(run, task, context, [])
    require_equal(context.plan_state, "restored")
    return orch, context


def t_equal_short_prefixes_restore_in_original_order_and_keep_snapshot_identity():
    for finding_prefix, source_prefix in (("f" * 600, "https://example.test/" + "s" * 300),
                                          ("Wort " * 120, "Quelle " * 42 + "sechs ")):
        value = section()
        value["findings"] = [finding_prefix + " A", "Zwischenbefund", finding_prefix + " C"]
        value["evidence"] = [source_prefix + "A", "https://example.test/b", source_prefix + "C"]
        before = O.RunContext(run_id="ar-local", task_id="at-local", scope="research",
                              ledger=BU.BudgetLedger(BU.Budget.from_dict(None)),
                              findings=list(value["findings"]), sources=list(value["evidence"]),
                              result_sections=[value])
        raw = encode([value], findings=value["findings"], sources=value["evidence"])
        orch, after = runtime_restore(raw)
        require_equal(after.findings, before.findings)
        require_equal(after.sources, before.sources)
        require_equal(RQ.snapshot_body(orch._result_findings(after), after.sources),
                      RQ.snapshot_body(orch._result_findings(before), before.sources),
                      "restart must not make a valid snapshot/verdict stale")


def t_dropped_sections_remain_explicit_in_public_output_after_restart():
    value = section()
    for name in CP.RESULT_SECTION_LISTS:
        value[name] = ["x" * 600 for _ in range(12)]
    values = [dict(copy.deepcopy(value), step_id=f"as-{n}") for n in range(3)]
    orch, after = runtime_restore(encode(values))
    require(not after.result_sections_complete)
    require_equal(after.findings, [])
    require_equal(after.sources, [])
    orch._contexts = {"ar-local": after}
    # Accessing a fallback would lose the explicit incompleteness marker.
    orch.ledger = None
    findings, sources = orch._collect_findings("ar-local")
    require_equal(sources, [])
    require(any("unvollstaendig erhalten" in finding for finding in findings))


def near_cap_sections():
    """Three complete results fit; their duplicate short views do not."""
    def text(prefix, size, end):
        filler = "Oeffentlich gelesener kuenstlicher Vergleich. "
        return prefix + (filler * size)[:size - len(prefix) - len(end)] + end
    values = []
    for n in range(3):
        values.append({"step_id": f"as-local-research-{n}", "confidence": "niedrig",
            "recommended_path": text(f"Empfehlung {n}: ", 350, "Kein bestaetigter Sieger."),
            "findings": [text(f"Befund {n}/{i}: ", 920 if i == 0 else 230,
                "Ein buchbarer Endpreis ist NICHT bestaetigt.") for i in range(12)],
            "evidence": [text(f"https://example.test/research/{n}/{i} ",
                650 if i == 0 else 130, "Kein Checkoutbeleg.") for i in range(20)],
            "assumptions": [text(f"Annahme {n}/{i}: ", 70, "Nur Annahme.") for i in range(4)],
            "uncertainties": [text(f"Unsicherheit {n}/{i}: ", 330,
                "Buchbarkeit bleibt unbekannt.") for i in range(4)],
            "rejected_alternatives": [text(f"Verworfen {n}/{i}: ", 150,
                "Kein passender Einwegflug.") for i in range(4)],
            "risk_notes": [text(f"Risiko {n}/{i}: ", 150,
                "Keinen Endpreis behaupten.") for i in range(4)]})
    return values


def all_material(values):
    return ([text for value in values for text in value["findings"]],
            [text for value in values for text in value["evidence"]])


def t_three_complete_results_survive_shedding_only_duplicate_short_views():
    values = near_cap_sections()
    findings, sources = all_material(values)
    notes = ["Nur fluechtiger Planungskontext. " * 30 for _ in range(5)]
    raw = encode(values, findings=findings, sources=sources, notes=notes)
    body = json.loads(raw)
    require(len(raw) <= CP.MAX_CHECKPOINT)
    require_equal(body["result_sections"], values, "all three complete results must fit")
    duplicate = copy.deepcopy(body)
    duplicate["befunde"] = [text[:CP.MAX_FINDING_TEXT] for text in findings[:CP.MAX_FINDINGS]]
    duplicate["quellen"] = [text[:CP.MAX_SOURCE_TEXT] for text in sources[:CP.MAX_SOURCES]]
    require(len(json.dumps(duplicate, ensure_ascii=False, sort_keys=True)) > CP.MAX_CHECKPOINT,
            "fixture must overflow even without context notes")
    require_equal(body["notizen"], [], "context notes are not assessment findings")
    require_equal(body["befunde"], [])
    require_equal(body["quellen"], [])
    after = restore(raw)
    require(after.result_sections_complete,
            "dropping duplicate projections must not disqualify complete results")
    require_equal(after.result_sections, values)
    _orch, context = runtime_restore(raw)
    require_equal(context.findings, findings)
    require_equal(context.sources, sources)
    require(context.findings[0].endswith("Ein buchbarer Endpreis ist NICHT bestaetigt."))
    require(context.sources[0].endswith("Kein Checkoutbeleg."))
    require(len(context.findings[0]) > CP.MAX_FINDING_TEXT)
    require(len(context.sources[0]) > CP.MAX_SOURCE_TEXT)
    sticky = restore(encode(values, findings=findings, sources=sources, notes=notes,
                            result_sections_complete=False))
    require(not sticky.result_sections_complete, "a prior incomplete result stays incomplete")


def t_uncovered_result_material_stays_incomplete_when_short_views_are_shed():
    values = near_cap_sections()
    findings, sources = all_material(values)
    for name in ("findings", "sources"):
        material = {"findings": list(findings), "sources": list(sources)}
        # A shared short prefix is not the complete field: the final
        # qualification of this extra material is absent from every section.
        material[name].insert(0, material[name][0][:-30] + "Anderer Schluss: NICHT bestaetigt.")
        raw = encode(values, **material)
        require(len(raw) <= CP.MAX_CHECKPOINT)
        rebuilt = restore(raw)
        require_equal(rebuilt.result_sections, values)
        require(not rebuilt.result_sections_complete,
                f"uncovered {name} must not disappear behind a positive marker")


def t_uncovered_result_material_cannot_disappear_inside_initial_short_view_caps():
    value = section()
    for name, cap in (("findings", CP.MAX_FINDING_TEXT), ("sources", CP.MAX_SOURCE_TEXT)):
        extra = "Ungedecktes Ergebnis. " * 50 + "Am Ende: NICHT bestaetigt."
        require(len(extra) > cap)
        material = {"findings": list(value["findings"]), "sources": list(value["evidence"])}
        material[name].insert(0, extra)
        rebuilt = restore(encode([value], **material))
        require_equal(rebuilt.result_sections, [value])
        require(not rebuilt.result_sections_complete,
                f"a prefix of uncovered {name} cannot represent its full material")


def t_native_journal_previews_and_research_material_have_distinct_retention():
    args = dict(plan=PL.Plan(goal="Vergleiche zwei Viertel und empfehle eines.",
                    steps=(PL.PlannedStep(kind="specialist", profile="researcher/hermes",
                                         instruction="Vergleiche und empfehle."),)),
        revision=0, cursor=1, goal_met="", approval_attempts=0, pending_step_id="",
        notes=[], invalid_signatures=set(), attempts={}, low_value={}, result_sections=[])
    cases = (
        {"findings": ["Vollstaendiger Dienstbeleg. " * 35 + "Abschluss im nativen Journal."], "sources": []},
        {"findings": [f"Dienstbeleg {n}" for n in range(9)], "sources": []},
        {"findings": [], "sources": ["Oeffentliche Quellenangabe. " * 20 + "Abschliessende Bedingung."]},
    )
    for material in cases:
        raw = CP.encode(**args, **material)
        require(len(raw) < CP.MAX_CHECKPOINT)
        require(restore(raw).result_sections_complete,
                "bounded native previews do not replace the canonical journal assessment")
        strict = CP.encode(**args, **material, research_result_material=True)
        require(not restore(strict).result_sections_complete,
                "research has no canonical action journal to recover uncovered material")
        journal = {"journal_findings": material["findings"], "journal_sources": material["sources"]}
        recovered = CP.encode(**args, **material, research_result_material=True, **journal)
        require(restore(recovered).result_sections_complete,
                "only exact canonical journal material can replace its short research preview")
        altered = {key: [value + " Geaenderte Schlussbedingung." for value in values]
                   for key, values in journal.items()}
        require(not restore(CP.encode(**args, **material, research_result_material=True,
                                      **altered)).result_sections_complete,
                "a different journal string cannot cover original material")
        for research in (False, True):
            prior_false = CP.encode(**args, **material, research_result_material=research,
                                    result_sections_complete=False, **journal)
            require(not restore(prior_false).result_sections_complete)
            malformed = dict(args, result_sections=[{"recommended_path": "Unbelegt."}])
            require(not restore(CP.encode(**malformed, **material,
                                          research_result_material=research)).result_sections_complete)
    values = near_cap_sections()
    findings, sources = all_material(values)
    for research in (False, True):
        overflowing = dict(args, result_sections=values,
                           notes=["Fluechtiger Planungskontext. " * 35 for _ in range(5)])
        raw = CP.encode(**overflowing, findings=findings, sources=sources,
                        research_result_material=research)
        require(len(raw) <= CP.MAX_CHECKPOINT)
        require_equal(restore(raw).result_sections_complete, research,
                      "only research permits shedding exact duplicate views below the total cap")
    material = cases[0]
    orch = object.__new__(O.Orchestrator)
    orch.ledger = SimpleNamespace(artifacts_for_run=lambda run_id: [SimpleNamespace(kind="file_work_receipt")])
    delivery = SimpleNamespace(finding=material["findings"][0], evidence="", sources=())
    def context(*, extra=(), complete=True):
        return O.RunContext(run_id="ar-native-file", task_id="at-native-file", scope="research",
            ledger=BU.BudgetLedger(BU.Budget.from_dict(None)), plan=args["plan"],
            findings=[*material["findings"], *extra], result_sections_complete=complete)
    with patch("solvio.agent_runtime.file_results.completion_evidence", return_value=(delivery,)) as read:
        require(CP.decode(orch._checkpoint_blob(context()))["result_sections_complete"])
        read.assert_called_once_with(orch.ledger, "ar-native-file")
        uncovered = context(extra=(material["findings"][0] + " Unbestaetigter Rechercheschluss.",))
        require(not CP.decode(orch._checkpoint_blob(uncovered))["result_sections_complete"],
                "a file receipt cannot exempt unrelated research material")
        require(not CP.decode(orch._checkpoint_blob(context(complete=False)))["result_sections_complete"])
    with patch("solvio.agent_runtime.file_results.completion_evidence", side_effect=ValueError("file_result_changed")):
        invalid = context()
        require(not CP.decode(orch._checkpoint_blob(invalid))["result_sections_complete"])
        require(not invalid.result_sections_complete, "invalid native readback remains sticky")


def t_only_bounded_native_browser_observation_can_be_thirty_seventh_evidence():
    value = section()
    value["evidence"] = [f"https://example.test/flight/{i}" for i in range(36)]
    value["evidence"][0] = ("Oeffentliche Quelle. " * 40)[:700] + "Kein Checkoutbeleg."
    require(len(value["evidence"][0]) > 600, "the original 720-char evidence bound stays valid")
    observation = CP.NATIVE_BROWSER_OBSERVATION_PREFIX + "Browseraufrufe unbekannt; keine bestätigten Seitenbelege."
    value["evidence"].append(observation)
    rebuilt = restore(encode([value], findings=value["findings"], sources=value["evidence"]))
    require(rebuilt.result_sections_complete)
    require_equal(rebuilt.result_sections, [value], "all 36 evidence entries and observation survive")
    for kind in ("thirty_eighth", "wrong_position", "oversize_observation"):
        invalid = copy.deepcopy(value)
        if kind == "thirty_eighth":
            invalid["evidence"].append(observation)
        elif kind == "wrong_position":
            invalid["evidence"][0], invalid["evidence"][-1] = invalid["evidence"][-1], invalid["evidence"][0]
        else:
            invalid["evidence"][-1] = CP.NATIVE_BROWSER_OBSERVATION_PREFIX + "Beobachtung. " * 50
            require(len(invalid["evidence"][-1]) > 600)
        rejected = restore(encode([invalid], findings=invalid["findings"], sources=invalid["evidence"]))
        require(not rejected.result_sections_complete, kind)
        require_equal(rejected.result_sections, [], kind)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
