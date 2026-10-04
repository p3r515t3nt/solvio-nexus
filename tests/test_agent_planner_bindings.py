"""Fixed planning/evaluation context, real validators, synthetic model transport.

No provider call, artifact producer or judgement of model semantics. The
native failure motivating this suite is preserved separately in Native3.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal, require_raises
enforce_assertions()
from solvio.agent_runtime import planner as PL, requirements as RQ, budget as BU
from solvio.agent_runtime import completion as CO, store as S

GOAL = "Analysiere die Tabelle und liefere Excel, Diagramm und Bericht mit Kennzahlen."


def binding(goal=GOAL, ask=1, actions=5, unclear=0):
    return RQ.validate({
        RQ.ASK: [{"id": f"a{i}", "text": f"Auskunft {i}."} for i in range(1, ask + 1)],
        RQ.ACTION: [{"id": f"h{i}", "text": f"Dateieigenschaft {i}."} for i in range(1, actions + 1)],
        RQ.UNCLEAR: [{"id": f"u{i}", "text": f"Unklare Forderung {i}."} for i in range(1, unclear + 1)],
        "belege": {"mindestens": 1}}, objective=goal)


def proposal(bound):
    return {"anforderungen": copy.deepcopy(bound), "schritte": [{"art": "capability",
        "faehigkeit": "file_process", "argumente": {},
        "erfuellt": [entry["id"] for entry in bound[RQ.ACTION]]}]}


class Transport:
    provider = "codex"
    timeout = 1
    route = {"provider": "codex", "billing_mode": "subscription"}

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def __call__(self, payload):
        self.calls.append(copy.deepcopy(payload))
        return {"ok": True, "text": json.dumps(self.replies.pop(0)),
                "provider": "codex", "billing_mode": "subscription"}


def planning(planner, ledger, bound, goal=GOAL, **extra):
    return planner.plan(goal=goal, scope="research", allowed_profiles=set(),
        known_capabilities={"file_process"}, ledger=ledger, run_id="ar-test",
        bound_requirements=bound, **extra)


def t_replan_preserves_all_five_actions_and_full_original_through_existing_repair():
    goal = "Tabelle. " + "Originalangabe. " * 160 + "Auch den Bericht vollstaendig liefern."
    require(2000 < len(goal) <= S.MAX_OBJECTIVE)
    bound = binding(goal)
    changed = proposal(bound)
    changed["anforderungen"][RQ.ACTION].pop()
    transport = Transport([changed, proposal(bound)])
    ledger = BU.BudgetLedger(BU.DEFAULTS["research"])
    plan, call = asyncio.run(planning(PL.Planner(subscription_transport=transport), ledger,
                                     bound, goal, context="x" * 3000))
    require(call.ok)
    require_equal(plan.steps[0].requirements, ("h1", "h2", "h3", "h4", "h5"))
    require_equal(ledger.planner_calls, 2)
    require_equal(len(transport.calls), 2)
    for payload in transport.calls:
        body = json.loads(payload["input"][1]["content"])
        require_equal(body["ziel"], goal)
        require_equal(body["gebundene_anforderungen"], bound)
        require_equal(len(body["kontext"]), 2000)
    require("bound_requirements_changed" in transport.calls[1]["input"][-1]["content"])


def t_changed_missing_or_weakened_binding_never_gets_a_third_format_attempt():
    bound = binding()
    mutations = []
    for kind in ("missing", "id", "text", "count", "kind", "invalid"):
        raw = proposal(bound)
        if kind == "missing": raw.pop("anforderungen")
        elif kind == "id": raw["anforderungen"][RQ.ACTION][-1]["id"] = "replacement"
        elif kind == "text": raw["anforderungen"][RQ.ACTION][-1]["text"] = "Optional."
        elif kind == "count": raw["anforderungen"]["belege"]["mindestens"] = 0
        elif kind == "kind": raw["anforderungen"][RQ.ASK] += [raw["anforderungen"][RQ.ACTION].pop()]
        else: raw["anforderungen"] = {"handlungen": "not a list"}
        mutations.append(raw)
    for changed in mutations:
        transport = Transport([changed, changed, proposal(bound)])
        ledger = BU.BudgetLedger(BU.DEFAULTS["research"])
        async def run():
            try: await planning(PL.Planner(subscription_transport=transport), ledger, bound)
            except PL.PlanInvalid as exc: require_equal(exc.reason, "bound_requirements_changed")
            else: raise AssertionError("changed requirement set accepted")
        asyncio.run(run())
        require_equal(len(transport.calls), 2)
        require_equal(ledger.planner_calls, 2)


def t_invalid_caller_binding_or_oversized_goal_never_dispatches():
    bound = binding()
    variants = [dict(bound, ziel_digest="f" * 64), dict(bound, v=2), {"handlungen": []}]
    for value, goal in [(v, GOAL) for v in variants] + [(bound, "x" * (S.MAX_OBJECTIVE + 1))]:
        transport = Transport([proposal(bound)])
        ledger = BU.BudgetLedger(BU.DEFAULTS["research"])
        async def run():
            try: await planning(PL.Planner(subscription_transport=transport), ledger, value, goal)
            except ValueError: pass
            else: raise AssertionError("invalid binding dispatched")
        asyncio.run(run())
        require_equal(transport.calls, [])
        require_equal(ledger.planner_calls, 0)


def t_escalation_fallback_keeps_same_bound_requirements():
    bound = binding()
    payloads = []
    principals = []
    async def transport(payload, **_):
        payloads.append(payload)
        return {"ok": True, "text": json.dumps(proposal(bound))}
    def open_lease(principal, **_):
        principals.append(principal)
        if principal != PL.BROKER_PRINCIPAL: raise ValueError("fixture cap")
        return "fixture-lease"
    broker = SimpleNamespace(register_principal=lambda _: "fixture",
        open_lease=open_lease, close_lease=lambda _: None)
    planner = PL.Planner(broker=broker, transport=transport)
    result = asyncio.run(planner._call(goal=GOAL, scope="research", run_id="ar-fallback",
        allowed_profiles=set(), known_capabilities={"file_process"}, context="",
        repair=False, hint="", tier="large", bound_requirements=bound))
    require(result.ok)
    require_equal(len(payloads), 1)
    require_equal(len(principals), 2)
    require(principals[0] != PL.BROKER_PRINCIPAL)
    require_equal(principals[1], PL.BROKER_PRINCIPAL)
    require_equal(json.loads(payloads[0]["input"][1]["content"])["gebundene_anforderungen"], bound)


def evidence_case(ask=1, actions=5, unclear=0):
    bound = binding(ask=ask, actions=actions, unclear=unclear)
    effects = {f"Core-Ausfuehrungsbeleg fuer h{i}; sha256:{str(i) * 64}": f"h{i}"
               for i in range(1, actions + 1)}
    generic = "Core-Dateibeleg: Datei vorhanden."
    source = "Eingabedatei: originale Tabelle, gepruefte Bytes."
    body = RQ.snapshot_body([generic, "Untrusted PDF-Inhalt: Berichttext.", *effects], [source])
    judgement = {"beantwortet": [{"id": f"a{i}", "belege": [source]}
                                  for i in range(1, ask + 1)] +
        [{"id": rid, "belege": [ev, source]} for ev, rid in effects.items()],
        "offen": [], "fehlend": [], "unsicher": [], "weiterarbeit_noetig": False}
    return bound, body, effects, judgement


def verdict(bound, body, effects, judgement):
    stamped = dict(RQ.validate_judgement(judgement), v=RQ.VERSION,
        task_id="at-test", run_id="ar-test", anforderungen_digest=RQ.digest_of(bound),
        snapshot=RQ.snapshot_digest(body))
    return CO.information(bound=bound, judgement=stamped, snapshot=json.loads(body),
        snapshot_digest=RQ.snapshot_digest(body), requirements_digest=RQ.digest_of(bound),
        task_id="at-test", run_id="ar-test", verified_effects=effects)


def t_actual_assessment_transport_receives_exact_snapshot_indices_without_duplicate_text():
    bound, body, effects, judgement = evidence_case()
    transport = Transport([judgement])
    call = asyncio.run(PL.Planner(subscription_transport=transport).assess(objective=GOAL,
        bound=bound, snapshot_body=body, verified_effects=effects, run_id="ar-test"))
    require(call.ok)
    request = transport.calls[0]
    user = json.loads(request["input"][1]["content"])
    require_equal(user["ergebnis"], body)
    require_equal(user["gebundene_anforderungen"], bound)
    catalogue = user["gepruefte_handlungsbelege"]
    require_equal(len(catalogue["eintraege"]), 5)
    snapshot = json.loads(body)
    rebuilt = {snapshot[entry["feld"]][entry["index"]]: entry["id"]
               for entry in catalogue["eintraege"]}
    require_equal(rebuilt, effects)
    for evidence in effects: require(evidence not in json.dumps(catalogue), "duplicated receipt")
    instruction = request["input"][0]["content"]
    for clause in ("nullbasierte", "keinen anforderungsgebundenen Ausfuehrungsbeleg",
                   "KEINE fachliche Zielerfuellung", "Anzahl verschiedener Quellen bleibt unveraendert"):
        require(clause in instruction, clause)


def t_foreign_missing_or_mutated_effect_catalogue_is_refused_before_transport():
    bound, body, effects, judgement = evidence_case()
    first = next(iter(effects))
    variants = [{first: "unknown"}, {first + " changed": "h1"}, {1: "h1"},
                {first: 1}, {"": "h1"}, [first]]
    for invalid in variants:
        transport = Transport([judgement])
        result = asyncio.run(PL.Planner(subscription_transport=transport).assess(objective=GOAL,
            bound=bound, snapshot_body=body, verified_effects=invalid, run_id="ar-test"))
        require(not result.ok)
        require_equal(result.reason, "assessment_input_refused")
        require_equal(transport.calls, [])
    require_raises(ValueError, PL.build_assessment_request, objective=GOAL, bound=bound,
        snapshot_body=body.replace(first, "removed"), verified_effects=effects)


def t_catalogue_counts_towards_existing_evaluation_limit_without_silent_cutoff():
    bound, body, effects, judgement = evidence_case()
    base = PL.assessment_input_size(objective=GOAL, bound=bound, snapshot_body=body)
    full = PL.assessment_input_size(objective=GOAL, bound=bound, snapshot_body=body, verified_effects=effects)
    require(full > base)
    snapshot = json.loads(body)
    snapshot["befunde"][1] += "x" * (RQ.MAX_EVALUATION_CHARS - full)
    limit_body = RQ.snapshot_body(snapshot["befunde"], snapshot["quellen"])
    require_equal(PL.assessment_input_size(objective=GOAL, bound=bound, snapshot_body=limit_body,
                                          verified_effects=effects), RQ.MAX_EVALUATION_CHARS)
    exact = PL.build_assessment_request(objective=GOAL, bound=bound, snapshot_body=limit_body,
                                        verified_effects=effects)
    require_equal(json.loads(exact["input"][1]["content"])["ergebnis"], limit_body)
    too_long = limit_body.replace('Untrusted PDF-Inhalt:', 'XUntrusted PDF-Inhalt:')
    require(PL.assessment_input_size(objective=GOAL, bound=bound, snapshot_body=too_long)
            < RQ.MAX_EVALUATION_CHARS, "only the new catalogue must push this over")
    transport = Transport([judgement])
    result = asyncio.run(PL.Planner(subscription_transport=transport).assess(objective=GOAL,
        bound=bound, snapshot_body=too_long, verified_effects=effects, run_id="ar-test"))
    require_equal(result.reason, "assessment_input_refused")
    require_equal(transport.calls, [])


def t_six_mixed_ids_are_assessable_but_generic_file_evidence_cannot_replace_any_action():
    bound, body, effects, judgement = evidence_case()
    require_equal(len(judgement["beantwortet"]), 6)
    require(verdict(bound, body, effects, judgement).satisfied)
    generic = json.loads(body)["befunde"][0]
    wrong = copy.deepcopy(judgement)
    wrong["beantwortet"][-1]["belege"] = [generic]
    require_equal(verdict(bound, body, effects, wrong).reason, "action_not_verified")
    wrong["beantwortet"][-1]["belege"] = [next(iter(effects))]
    require_equal(verdict(bound, body, effects, wrong).reason, "action_not_verified")
    missing = copy.deepcopy(judgement)
    missing["beantwortet"].pop()
    require_equal(verdict(bound, body, effects, missing).reason, "open_external_action")
    foreign = copy.deepcopy(judgement)
    foreign["beantwortet"][-1]["id"] = "foreign"
    require_equal(verdict(bound, body, effects, foreign).reason, "verdict_unknown_requirement")
    foreign = copy.deepcopy(judgement)
    foreign["offen"] = ["foreign"]
    require_equal(verdict(bound, body, effects, foreign).reason, "verdict_unknown_requirement")


def t_full_union_fits_both_id_lists_but_neither_unclarity_nor_text_limits_are_relaxed():
    bound, body, effects, judgement = evidence_case(ask=5, actions=5, unclear=5)
    all_ids = sorted(RQ.requirement_ids(bound))
    require_equal(len(all_ids), 15)
    judgement["beantwortet"] += [{"id": f"u{i}", "belege": [next(iter(effects))]} for i in range(1, 6)]
    require_equal(len(RQ.validate_judgement(judgement)["beantwortet"]), 15)
    require_equal(verdict(bound, body, effects, judgement).reason, "requirement_unclear")
    all_open = dict(judgement, beantwortet=[], offen=all_ids)
    require_equal(RQ.validate_judgement(all_open)["offen"], all_ids)
    for field in ("beantwortet", "offen"):
        excessive = copy.deepcopy(judgement if field == "beantwortet" else all_open)
        excessive[field].append(excessive[field][0])
        require_raises(RQ.JudgementInvalid, RQ.validate_judgement, excessive)
        require_equal(PL.ASSESSMENT_SCHEMA["properties"][field]["maxItems"], 15)
    for field in ("fehlend", "unsicher"):
        excessive = dict(judgement, **{field: ["offen"] * 6})
        require_raises(RQ.JudgementInvalid, RQ.validate_judgement, excessive)
        require_equal(PL.ASSESSMENT_SCHEMA["properties"][field]["maxItems"], 5)


def t_image_prompt_keeps_visual_citations_distinct_from_action_proof():
    from solvio.agent_runtime import cost_dispatch as D, image_inputs
    from test_agent_task_revisions import world
    bound, body, effects, judgement = evidence_case()
    transport = Transport([judgement])
    # The actual task scope now also reads the authenticated revision chain.
    # Keep only image selection synthetic, with a real temporary task ledger.
    with world(content=None, objective=GOAL) as w:
        with D.task_cost_scope(w.ledger, task_id=w.task_id, run_id=w.run_id,
                phase="assessment", operation_id="fixture-image-assessment"), \
                patch.object(image_inputs, "artifact_ids", return_value=["aa-fixture"]):
            result = asyncio.run(PL.Planner(subscription_transport=transport).assess(objective=GOAL,
                bound=bound, snapshot_body=body, verified_effects=effects, run_id=w.run_id))
    require(result.ok)
    payload = transport.calls[0]
    require_equal(payload["core_image_artifacts"], ["aa-fixture"])
    visual = payload["input"][1]["content"]
    require("Zitiere für eine visuelle Feststellung den zugehörigen Core-Dateibeleg" in visual)
    require("ein visueller Dateibeleg ersetzt ihn nicht" in visual)
    require("ein Erfolg ist nicht vorgegeben" in visual)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
