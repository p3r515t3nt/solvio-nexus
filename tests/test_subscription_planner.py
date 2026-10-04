"""Reale Planner-/CLI-Umschlaege, Anbieter an der Startergrenze gestellt."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from solvio.agent_runtime import planner as PL, budget as BU, requirements as RQ
from solvio.agent_runtime import completion as CO
from solvio.specialists import launcher as L, providers as P
from solvio.specialists.subscription import SubscriptionTransport, decode


PLAN = {"schritte": [{"art": "specialist", "profil": "investigator/codex",
                        "auftrag": "Pruefe den Berechnungsweg im Repository."}]}


def codex_answer(reply, usage=None):
    return "\n".join(json.dumps(e) for e in (
        {"type": "thread.started", "thread_id": "synthetic"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": reply}},
        {"type": "turn.completed", "usage": usage}))


def outcome(text, provider="codex", **kwargs):
    return P.SubscriptionOutcome(True, text=text, exit_code=0,
                                 provider=provider, billing_mode="subscription",
                                 auth="chatgpt" if provider == "codex" else "claude.ai",
                                 dispatch_started=True, **kwargs)


def t_codex_counts_total_input_once_and_keeps_unknown_usage_unknown():
    result = decode("codex", outcome(codex_answer("Antwort", {
        "input_tokens": 100, "cached_input_tokens": 90, "output_tokens": 20,
        "reasoning_output_tokens": 10})))
    require(result["ok"])
    require_equal(result["tokens"], 120)
    require(result["usage_reported"])
    unknown = decode("codex", outcome(codex_answer("Antwort")))
    require(unknown["ok"])
    require(not unknown["usage_reported"])


def t_claude_cache_usage_is_additional_to_uncached_input():
    result = decode("claude-code", outcome(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "Antwort", "usage": {"input_tokens": 10, "output_tokens": 20,
        "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200}}),
        provider="claude-code"))
    require(result["ok"])
    require_equal(result["tokens"], 1230)
    require(result["usage_reported"])


def t_completed_quota_discussion_is_not_a_provider_failure():
    reply = 'Die Funktion prueft, ob das Kontingent (quota) erschoepft ist.'
    result = decode("codex", outcome(codex_answer(reply)))
    require(result["ok"])
    require_equal(result["text"], reply)


def t_incomplete_or_failed_turn_never_leaks_partial_success():
    partial = {"type": "item.completed", "item": {"type": "agent_message", "text": "fertig"}}
    for text in (json.dumps(partial), "not json", "[]", codex_answer("Antwort") + "\n" +
                 json.dumps({"type": "turn.failed", "error": {"message": "Usage limit reached"}})):
        result = decode("codex", outcome(text))
        require(not result["ok"])
        require_equal(result["text"], "")


def t_claude_zero_exit_error_is_quota_not_success():
    result = decode("claude-code", outcome(json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "You've reached your usage limit"}), provider="claude-code"))
    require(not result["ok"])
    require_equal(result["reason"], "quota")


def t_truncated_or_malformed_usage_never_claims_measured_zero():
    truncated = decode("codex", outcome(codex_answer("Antwort"), truncated=True))
    require(not truncated["ok"])
    for value in (-1, True, "10", None):
        result = decode("codex", outcome(codex_answer("Antwort", {
            "input_tokens": value, "output_tokens": 20})))
        require(not result["usage_reported"])


async def plan(planner, ledger=None):
    return await planner.plan(goal="Pruefe den Berechnungsweg im Repository.",
        scope="build", allowed_profiles={"investigator/codex"},
        known_capabilities=set(), run_id="temporary-run",
        ledger=ledger or BU.BudgetLedger(BU.DEFAULTS["build"]))


def t_real_planner_uses_subscription_for_plan_and_assessment_without_broker():
    calls = []
    async def runner(invocation, prompt):
        calls.append((invocation, prompt))
        return L.Outcome(True, text=codex_answer(json.dumps(PLAN), {
            "input_tokens": 10, "output_tokens": 20}), exit_code=0)
    status = P.ProviderStatus("codex", True, auth="chatgpt", billing_mode="subscription")
    transport = SubscriptionTransport(runner=runner)
    # Jede Beruehrung des alten Inferenzwegs bricht den Test ab.
    broker = SimpleNamespace(register_principal=lambda *a: (_ for _ in ()).throw(
        AssertionError("Broker wurde beruehrt")))
    planner = PL.Planner(broker=broker, subscription_transport=transport)
    async def scenario():
        with patch.object(P, "codex_status", AsyncMock(return_value=status)), \
                patch.object(P, "resolve", return_value="/synthetic/codex"):
            proposal, first = await plan(planner)
            assessed = await planner.assess(objective="Pruefe den Berechnungsweg im Repository.",
                bound={}, snapshot_body="ein Befund", run_id="temporary-run")
        require_equal(len(proposal.steps), 1)
        for call in (first, assessed):
            require(call.ok, call.reason)
            require_equal(call.provider, "codex")
            require_equal(call.billing_mode, "subscription")
            require_equal(call.tokens, 30)
            require(call.usage_reported)
            require_equal(call.lease_id, "")
    asyncio.run(scenario())
    require_equal(len(calls), 2)
    for invocation, prompt in calls:
        require("--model" not in invocation.argv)
        require("--json" in invocation.argv)
        require("features.shell_tool=false" in invocation.argv)
        require(not Path(invocation.cwd).exists(), "temporarer Arbeitsort blieb liegen")
        require("schritte" in prompt or "ergebnis" in prompt)


def t_requirement_prompt_separates_owner_criteria_from_missing_context():
    """Request contract only: no fixture pretends to test model judgement."""
    objective = "Vergleiche zwei Moeglichkeiten und beziehe meine vorhandenen Vorlieben ein."
    context = "Momentaufnahme: persoenliche Suchtreffer derzeit nicht verfuegbar."
    request = PL.build_request(goal=objective, scope="research", context=context,
        allowed_profiles={"researcher/hermes"}, known_capabilities=set())
    user = json.loads(request["input"][1]["content"])
    require_equal(user["ziel"], objective)
    require_equal(user["kontext"], context)
    instruction = request["input"][0]["content"]
    for clause in ("Anforderungen ausschliesslich aus dem ORIGINALAUFTRAG",
                   "Ergebnis- und Qualitaetskriterien einzeln",
                   "Anforderungen weder abschwaechen noch umdeuten oder streichen"):
        require(clause in instruction, "planning contract lost: " + clause)


def t_source_minimum_contract_reaches_the_actual_subscription_request():
    """The fake answers test delivery and preservation, not model semantics."""
    cases = (
        ("Lies das angehaengte Dokument vollstaendig und bewahre alle Textzeilen in ihrer Reihenfolge.", 1),
        ("Vergleiche die Angaben des Dokuments mit drei verschiedenen externen Quellen.", 3),
        ("Vergleiche drei Angebote mit je zwei verschiedenen Quellen.", 6),
        ("Pruefe die Angaben anhand von zwoelf verschiedenen Quellen.", 12),
    )
    async def scenario():
        for objective, minimum in cases:
            raw = {"schritte": [{"art": "specialist", "profil": "researcher/hermes",
                                  "auftrag": "Pruefe die verfuegbaren Quellen."}],
                "anforderungen": {"auskunft": [{"id": "a1", "text": objective}],
                    "handlungen": [], "unklar": [], "belege": {"mindestens": minimum}}}
            received = []
            async def runner(invocation, prompt):
                messages = json.loads(prompt.split("\n", 1)[1])
                received.append(messages)
                return L.Outcome(True, text=codex_answer(json.dumps(raw)), exit_code=0)
            planner = PL.Planner(subscription_transport=SubscriptionTransport(runner=runner))
            status = P.ProviderStatus("codex", True, auth="chatgpt", billing_mode="subscription")
            with patch.object(P, "codex_status", AsyncMock(return_value=status)), \
                    patch.object(P, "resolve", return_value="/synthetic/codex"):
                _, call = await planner.plan(goal=objective, scope="research",
                    allowed_profiles={"researcher/hermes"}, known_capabilities=set(),
                    run_id="temporary-source-contract", ledger=BU.BudgetLedger(BU.DEFAULTS["research"]))
            require_equal(len(received), 1)
            instruction = received[0][0]["content"]
            user = json.loads(received[0][1]["content"])
            require_equal(user["ziel"], objective)
            schema = user["schema"]["properties"]["anforderungen"]["properties"]["belege"]["properties"]["mindestens"]
            require_equal(schema["type"], "integer")
            require_equal((schema["minimum"], schema["maximum"]), (0, RQ.MAX_REFERENCES))
            require("anforderungen" in user["schema"]["required"])
            require("default" not in schema, "no synthetic source count may replace the task")
            for clause in ("Anzahl verschiedener Quellen", "nicht die Zahl der Anforderungen, Textzeilen",
                           "Ein bereitgestelltes Dokument ist eine Quelle", "externe Quellen und ihre Anzahl beibehalten"):
                require(clause in schema["description"], "missing schema meaning: " + clause)
            for clause in ("QUELLEN, nicht Anforderungen, Textzeilen",
                           "keine zusaetzliche Quellenzahl oder externe Recherchepflicht",
                           "Ausdruecklich verlangte externe Quellen und deren Anzahl bleiben erforderlich"):
                require(clause in instruction, "missing planning meaning: " + clause)
            bound = RQ.validate(PL.requirements_of(PL.call_payload(call)), objective=objective)
            require_equal(bound["belege"]["mindestens"], minimum,
                          "the planner rewrote the returned source minimum")
    asyncio.run(scenario())


def t_distinct_source_minimum_is_not_a_count_of_criteria_or_repeated_citations():
    """Existing deterministic count stays strict, including explicit multi-source work."""
    sources = [f"https://source.invalid/{n}" for n in range(12)]
    criteria = [{"id": "a1", "text": "Vollstaendiger Inhalt"},
                {"id": "a2", "text": "Reihenfolge"}, {"id": "a3", "text": "Alle Textzeilen"}]
    for minimum, used, expected in ((1, sources[:1], "goal_met"),
                                   (3, sources[:1] * 4, "not_enough_sources"),
                                   (3, sources[:3], "goal_met"),
                                   (6, sources[:5], "not_enough_sources"),
                                   (6, sources[:6], "goal_met"),
                                   (12, sources[:11], "not_enough_sources"),
                                   (12, sources, "goal_met")):
        objective = ("Lies das Dokument vollstaendig." if minimum == 1 else
                     f"Pruefe den Inhalt anhand von {minimum} verschiedenen Quellen.")
        bound = RQ.validate({"auskunft": criteria, "handlungen": [], "unklar": [],
                             "belege": {"mindestens": minimum}}, objective=objective)
        body = RQ.snapshot_body(["Der vollstaendige Inhalt mit allen Textzeilen in ihrer Reihenfolge."], sources)
        digest = RQ.snapshot_digest(body)
        judgement = {"v": RQ.VERSION, "task_id": "at-source-count", "run_id": "ar-source-count",
            "anforderungen_digest": RQ.digest_of(bound), "snapshot": digest,
            "beantwortet": [{"id": entry["id"], "belege": used} for entry in criteria],
            "offen": [], "fehlend": [], "unsicher": [], "weiterarbeit_noetig": False}
        verdict = CO.information(bound=bound, judgement=judgement,
            snapshot=json.loads(body), snapshot_digest=digest,
            requirements_digest=RQ.digest_of(bound), task_id="at-source-count", run_id="ar-source-count")
        require_equal(verdict.reason, expected)
        require_equal(verdict.satisfied, expected == "goal_met")
        request = PL.build_assessment_request(objective=objective, bound=bound, snapshot_body=body)
        instruction = request["input"][0]["content"]
        require("insgesamt verwendeten verschiedenen Eintraege aus `quellen`" in instruction)
        require("zaehlt insgesamt aber nur einmal" in instruction)
        require_equal(json.loads(request["input"][1]["content"])["gebundene_anforderungen"]["belege"]["mindestens"], minimum)


def t_assessment_schema_requires_every_runtime_check_and_preserves_original_goal():
    objective = "Vergleiche zwei Moeglichkeiten und beziehe meine vorhandenen Vorlieben ein."
    bound = RQ.validate({"auskunft": [{"id": "a1", "text": "Allgemeiner Vergleich; Hinweise fehlen."}],
        "handlungen": [], "unklar": [], "belege": {"mindestens": 0}}, objective=objective)
    snapshot = RQ.snapshot_body(["Eine allgemeine Alternative; das zweite Ziel bleibt offen."], [])
    request = PL.build_assessment_request(objective=objective, bound=bound,
                                         snapshot_body=snapshot)
    user = json.loads(request["input"][1]["content"])
    require_equal(user["originalauftrag"], objective)
    require_equal(user["gebundene_anforderungen"], bound)
    require_equal(user["ergebnis"], snapshot)
    schema = user["schema"]
    valid = {"beantwortet": [], "offen": ["a1"], "fehlend": [],
             "unsicher": [], "weiterarbeit_noetig": True}
    require_equal(set(schema["required"]), set(RQ.validate_judgement(valid)))
    for field in schema["required"]:
        missing = {key: value for key, value in valid.items() if key != field}
        try:
            RQ.validate_judgement(missing)
        except RQ.JudgementInvalid:
            pass
        else:
            raise AssertionError("schema requires a check the runtime does not: " + field)
    instruction = request["input"][0]["content"]
    for clause in ("ORIGINALAUFTRAG unabhaengig von den gebundenen Anforderungen",
                   "jede verlangte Handlung in der Welt",
                   "NICHT Ownerbestaetigung und NICHT Tatsachenquelle",
                   "Ordne erst danach dein Urteil den vorhandenen Kennungen zu",
                   "nur `beantwortet`, wenn ALLE verlangten Teile erfuellt sind",
                   "Fehlen und NICHT ihre Erfuellung",
                   "nebensaechliche Unsicherheit sperrt ein tatsaechlich erfuelltes Kriterium nicht pauschal"):
        require(clause in instruction, "assessment contract lost: " + clause)
        require(instruction.index(clause) < instruction.index("Ein BELEG"),
                "semantic coverage must precede citation selection: " + clause)


def t_real_planner_retains_pre_dispatch_auth_failure_without_format_retry():
    runner = AsyncMock(side_effect=AssertionError("unerlaubter Anbieterstart"))
    transport = SubscriptionTransport(runner=runner)
    planner = PL.Planner(subscription_transport=transport)
    status = P.ProviderStatus("codex", False, "subscription_required",
                              auth="api_key", billing_mode="metered_api")
    async def scenario():
        with patch.object(P, "codex_status", AsyncMock(return_value=status)), \
                patch.object(P, "resolve", return_value="/synthetic/codex"):
            try:
                await plan(planner)
            except PL.ProviderUnavailable as exc:
                require_equal(exc.detail, "subscription_required")
                require_equal(exc.call.billing_mode, "metered_api")
                require(not exc.call.dispatch_started)
            else:
                raise AssertionError("API-Anmeldung wurde ausgefuehrt")
    asyncio.run(scenario())
    require_equal(runner.await_count, 0)


def t_unknown_configured_provider_cannot_fall_back():
    planner = PL.planner_from_settings(SimpleNamespace(
        agent_runtime_subscription_provider="misspelled", agent_runtime_subscription_model=""))
    async def scenario():
        try:
            await plan(planner)
        except PL.ProviderUnavailable as exc:
            require_equal(exc.call.provider, "misspelled")
            require_equal(exc.call.reason, "provider_unavailable")
        else:
            raise AssertionError("unbekannter Anbieter bekam einen Rueckfall")
    asyncio.run(scenario())


def t_cancelling_launcher_reaps_the_real_process_group():
    with tempfile.TemporaryDirectory() as folder:
        marker = Path(folder) / "started"
        script = Path(folder) / "wait.py"
        script.write_text("import os,time,pathlib\npathlib.Path(" + repr(str(marker)) +
                          ").write_text(str(os.getpid()))\ntime.sleep(60)\n")
        async def scenario():
            invocation = L.Invocation(sys.executable, (str(script),), timeout=30,
                                      prompt_via_stdin=False, cwd=folder)
            task = asyncio.create_task(L.run(invocation, ""))
            for _ in range(300):
                if marker.exists():
                    break
                await asyncio.sleep(.01)
            require(marker.exists(), "Testprozess startete nicht")
            pid = int(marker.read_text())
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("Abbruch wurde verschluckt")
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            os.kill(pid, signal.SIGKILL)
            raise AssertionError("CLI blieb nach Auftragsabbruch am Leben")
        asyncio.run(scenario())



def t_real_timeout_keeps_cost_unknown_and_retains_content_free_diagnostic():
    """Real local child times out; replay cannot launch again or settle costs."""
    from solvio.specialists import subscription as U
    from solvio.agent_runtime import cost_dispatch as D
    from test_agent_interaction_costs import fixture, admit, scoped
    async def scenario():
        with fixture() as (ledger, _, _), tempfile.TemporaryDirectory() as folder:
            child = Path(folder) / "slow.py"
            child.write_text("import time\ntime.sleep(30)\n")
            binding = admit(ledger)
            calls = []
            async def runner(invocation, prompt):
                calls.append(invocation)
                local = L.Invocation(sys.executable, (str(child),), cwd=folder,
                                     timeout=.15, prompt_via_stdin=False)
                return await L.run(local, "")
            status = P.ProviderStatus("codex", True, auth="chatgpt", billing_mode="subscription")
            transport = SubscriptionTransport(runner=runner)
            with patch.object(P, "codex_status", AsyncMock(return_value=status)), \
                    patch.object(P, "resolve", return_value="/synthetic/codex"), \
                    patch.object(U.log, "warning") as warning:
                with scoped(ledger, binding):
                    first = await transport({"input": [{"role": "user", "content": "synthetic private prompt"}]})
                require_equal((first["ok"], first["reason"], first["text"], first["cost_status"]),
                              (False, "cost_recovery_required", "", "unknown"))
                events = [c for c in warning.call_args_list if c.args == ("subscription.call_unresolved",)]
                require_equal(len(events), 1, "timeout cause disappeared behind cost status")
                event = events[0].kwargs
                require_equal(event["cause"], "timeout")
                require_equal(event["invocation_id"], first["cost_invocation_id"])
                require(event["elapsed_ms"] >= 150)
                require("synthetic private prompt" not in str(event))
                with scoped(ledger, binding):
                    second = await transport({"input": []})
                require_equal(second["reason"], "cost_recovery_required")
            require_equal(len(calls), 1, "unknown predecessor caused another physical call")
            with ledger._open() as db:
                row = db.execute("SELECT state,actual_cents FROM agent_cost_reservations").fetchone()
                require_equal((row["state"], row["actual_cents"]), ("unknown", None))
    asyncio.run(scenario())


def t_unresolved_diagnostic_never_logs_arbitrary_outcome_fields():
    from solvio.specialists import subscription as U
    marker = "SYNTHETIC_PRIVATE_CONTENT"
    for elapsed in (float("nan"), float("inf"), -1, True, marker):
        raw = P.SubscriptionOutcome(False, text=marker, stderr_note=marker,
            reason=marker, provider=marker, elapsed=elapsed,
            cost_status="unknown", cost_invocation_id=marker, dispatch_started=True)
        with patch.object(U.log, "warning") as warning:
            result = decode(marker, raw)
        require_equal((result["ok"], result["reason"], result["text"]),
                      (False, "cost_recovery_required", ""))
        require_equal(warning.call_count, 1)
        fields = warning.call_args.kwargs
        require_equal(set(fields), {"provider", "cause", "invocation_id", "elapsed_ms"})
        require(marker not in str(warning.call_args))
        require_equal((fields["provider"], fields["cause"], fields["invocation_id"], fields["elapsed_ms"]),
                      ("unknown", "unclassified", "", None))


def t_assessment_uses_native_schema_while_retaining_negative_verdict_and_original_input():
    """The local CLI double checks transport, not real model correctness."""
    answer = {"beantwortet": [], "offen": ["a1"], "fehlend": ["Maße nicht belegt"],
              "unsicher": [], "weiterarbeit_noetig": True}
    received = []
    async def runner(invocation, prompt):
        require("--output-schema" in invocation.argv, "assessment has only a prompt schema")
        schema_path = Path(invocation.argv[invocation.argv.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text())
        require_equal(schema, PL.ASSESSMENT_SCHEMA)
        require_equal(schema["additionalProperties"], False)
        require_equal(schema["properties"]["beantwortet"]["items"]["additionalProperties"], False)
        messages = json.loads(prompt.split("\n", 1)[1])
        received.append(json.loads(messages[1]["content"]))
        return L.Outcome(True, text=codex_answer(json.dumps(answer)), exit_code=0)
    async def scenario():
        transport = SubscriptionTransport(runner=runner)
        planner = PL.Planner(subscription_transport=transport)
        objective = "Vergleiche die genauen Außenmaße, fehlende Belege bleiben offen."
        snapshot = RQ.snapshot_body(["Maße nicht belegt"], [])
        bound = RQ.validate({"auskunft": [{"id": "a1", "text": objective}],
            "handlungen": [], "unklar": [], "belege": {"mindestens": 0}}, objective=objective)
        with patch.object(P, "codex_status", AsyncMock(return_value=P.ProviderStatus(
                "codex", True, auth="chatgpt", billing_mode="subscription"))), \
                patch.object(P, "resolve", return_value="/synthetic/codex"):
            call = await planner.assess(objective=objective, bound=bound,
                                       snapshot_body=snapshot, run_id="ar-local-schema")
        require(call.ok, call.reason)
        require_equal(RQ.validate_judgement(PL.call_payload(call)), answer)
        require_equal(received[0]["originalauftrag"], objective)
        require_equal(received[0]["ergebnis"], snapshot)
        require_equal(received[0]["gebundene_anforderungen"], bound)
        # The explicit legacy API transport never receives the internal CLI key.
        api_requests = []
        async def api(payload, **kwargs):
            api_requests.append(payload)
            return {"output_text": json.dumps(answer)}
        api_planner = PL.Planner(transport=api)
        with patch.object(api_planner, "ensure_principal", return_value="synthetic"):
            await api_planner.assess(objective=objective, bound=bound,
                snapshot_body=snapshot, run_id="ar-local-schema")
        require_equal(set(api_requests[0]), {"model", "input"})
    asyncio.run(scenario())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
