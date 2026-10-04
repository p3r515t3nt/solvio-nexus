"""Kostenstopps durch echte Runtime, Planer und lokale Anbieterprogramme.

Nur das externe CLI ist gestellt. Der Kostenvoranschlag bezeugt entweder
dieses lokale Programm oder einen synthetisch durchsetzbaren Testdeckel;
seine ChatGPT-Anmeldung ist ausdruecklich kein Null-Euro-Beweis. Die
Bewertungspruefung hier prueft die reale Bewertungsphase separat vom Build,
dessen regulaerer Abschluss deterministische Arbeitspruefung verwendet.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
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

import test_agent_runtime_subscription_flow as F
from solvio.agent_runtime import costs as C, cost_dispatch as D, inquiry as Q
from solvio.agent_runtime import planner as PL, requirements as RQ, specialists as SP, store as S
from solvio.agent_runtime.orchestrator import Orchestrator, _ProviderPause
from solvio.agent_runtime.workspace import WorkspaceManager
from solvio.capabilities.router import CapabilityRouter
from solvio.specialists import providers as P
from solvio.specialists.subscription import SubscriptionTransport


PLAN = {"schritte": [
    {"art": "specialist", "profil": "investigator/codex",
     "auftrag": "Untersuche die Kontingentbehandlung in README.md."},
    {"art": "specialist", "profil": "builder/codex",
     "auftrag": "Schreibe den begruendeten Bericht in ANALYSE.md."}]}
VERDICT = {"beantwortet": [{"id": "a1", "belege": ["README.md:2"]}],
           "offen": [], "fehlend": [], "unsicher": [], "weiterarbeit_noetig": False}


def _cli(folder: Path, ledger_path: str, *, plans=None, assessments=None, wait=False):
    executable = folder / "codex"
    (folder / "fixture.json").write_text(json.dumps({
        "ledger": ledger_path, "plan": plans or [PLAN],
        "assessment": assessments or [VERDICT], "wait": wait}), encoding="utf-8")
    executable.write_text(f"#!{sys.executable}\n" + '''
import json, os, sqlite3, sys, time
from pathlib import Path
folder = Path(__file__).parent
if sys.argv[1:3] == ['login', 'status']:
    with (folder/'auth.jsonl').open('a') as log:
        log.write('synthetic account status\\n')
    print('Logged in using ChatGPT', file=sys.stderr)
    raise SystemExit(0)
assert 'OPENAI_API_KEY' not in os.environ
fixture = json.loads((folder/'fixture.json').read_text())
prompt = sys.stdin.read()
assert '--json' in sys.argv or '--sandbox' in sys.argv
if '--json' in sys.argv:
    messages = json.loads(prompt.split('\\n', 1)[1])
    request = None
    for message in messages:
        try:
            candidate = json.loads(message['content'])
        except ValueError:
            continue
        if isinstance(candidate, dict) and ('ziel' in candidate or 'ergebnis' in candidate):
            request = candidate
    assert request is not None
    kind = 'assessment' if 'ergebnis' in request else 'plan'
else:
    kind = 'specialist'
old = [json.loads(line) for line in (folder/'calls.jsonl').read_text().splitlines()] \
    if (folder/'calls.jsonl').exists() else []
ordinal = len([call for call in old if call['kind'] == kind])
with sqlite3.connect('file:'+fixture['ledger']+'?mode=ro', uri=True) as db:
    claimed = db.execute("SELECT count(*) FROM agent_provider_invocations WHERE state='claimed'").fetchone()[0]
with (folder/'calls.jsonl').open('a') as log:
    log.write(json.dumps({'kind':kind, 'ordinal':ordinal, 'claimed':claimed, 'pid':os.getpid()})+'\\n')
if fixture['wait']:
    until = time.monotonic() + 5
    while not (folder/'release').exists() and time.monotonic() < until:
        time.sleep(0.01)
if kind == 'specialist':
    print(json.dumps({'findings':['README beschreibt Kontingente.'], 'evidence':['README.md:2'],
                     'recommended_path':'ANALYSE.md schreiben.', 'uncertainties':[]}))
else:
    choices = fixture[kind]
    reply = choices[min(ordinal, len(choices)-1)]
    if reply == '__EXIT_3__':
        print('stream error: transient', file=sys.stderr); sys.exit(3)
    text = reply if isinstance(reply, str) else json.dumps(reply)
    print(json.dumps({'type':'item.completed', 'item':{'type':'agent_message','text':text}}))
    print(json.dumps({'type':'turn.completed', 'usage':{'input_tokens':17,'output_tokens':11}}))
''', encoding="utf-8")
    executable.chmod(0o700)
    return executable


@contextmanager
def fixture(*, quote="free", plans=None, assessments=None, wait=False, timeout=3):
    with tempfile.TemporaryDirectory(prefix="solvio-cost-runtime-") as directory:
        folder = Path(directory)
        state = folder / "state"
        state.mkdir()
        repo = F._repository(folder)
        ledger_path = str(state / "agent.sqlite3")
        executable = _cli(folder, ledger_path, plans=plans, assessments=assessments, wait=wait)
        with patch.dict(os.environ, {"SOLVIO_STATE_DIR": str(state),
                                    "SOLVIO_AGENT_RUNS_DB": ledger_path, "HOME": str(folder)}), \
                patch.object(P, "resolve", return_value=str(executable)), \
                patch.object(SP, "resolve", return_value=str(executable)):
            ledger = S.AgentRunLedger(ledger_path)
            broker = F.ForbiddenBroker()
            api = AsyncMock(side_effect=AssertionError("unerlaubter API-Aufruf"))
            transport = SubscriptionTransport("codex", timeout=timeout)
            planner = PL.Planner(broker=broker, transport=api, subscription_transport=transport)

            def measured_quote(provider, invocation):
                require_equal(provider, "codex")
                require_equal(invocation.executable, str(executable))
                if quote == "free":
                    return D.CostQuote(0, C.CostEvidence("free_local", "fixture:local-cli"))
                return D.CostQuote(quote, C.CostEvidence("enforceable_upper_bound", "fixture:hard-cap"))

            orch = Orchestrator(ledger=ledger, planner=planner,
                router=CapabilityRouter(policy_mode="enforce"),
                workspaces=WorkspaceManager(allowed=(str(repo.resolve()),)),
                cost_quote_adapter=measured_quote if quote is not None else None)
            task, run = orch.create_task(objective=F.OBJECTIVE, scope="build",
                origin="trusted_interactive_app", principal="fixture-owner", target_repo=str(repo))
            case = SimpleNamespace(folder=folder, ledger=ledger, orch=orch, task=task, run=run,
                                   transport=transport, quote=measured_quote, costs=C.CostLedger(ledger))
            try:
                yield case
            finally:
                require_equal(broker.calls, [])
                require_equal(api.await_count, 0)


def calls(case):
    path = case.folder / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def wait_at_next_provider_call(case):
    path = case.folder / "fixture.json"
    configured = json.loads(path.read_text())
    configured["wait"] = True
    path.write_text(json.dumps(configured), encoding="utf-8")


async def drive_to(case, states, *, ticks=8):
    for _ in range(ticks):
        await case.orch.tick()
        current = case.ledger.get_run(case.run.run_id)
        if current.state in states or current.terminal:
            return current
    raise AssertionError("Zielzustand nicht erreicht: " + current.state)


def t_unknown_extra_costs_park_real_runtime_before_any_provider_inference():
    async def scenario():
        with fixture(quote=None) as case:
            current = await drive_to(case, {S.WAITING_USER})
            require_equal(current.state, S.WAITING_USER)
            view = Q.run_view(case.ledger, current)
            require_equal(view["anbietergrenze"]["grund"], "cost_unbounded")
            require_equal(view["anbietergrenze"]["phase"], "plan")
            require("Kosten" in view["wartet_auf"]["grund"], view["wartet_auf"])
            require_equal(calls(case), [])
            require((case.folder / "auth.jsonl").exists(), "echte Statuspruefung wurde umgangen")
            require_equal(D.invocations(case.ledger, case.task.task_id), [])
            require_equal(case.costs.view(case.task.task_id)["counts"], {"unbounded_cost": 1})
    asyncio.run(scenario())


def t_owner_cost_proof_after_predispatch_pause_can_start_a_new_bound_attempt():
    async def scenario():
        with fixture(quote=None) as case:
            current = await drive_to(case, {S.WAITING_USER})
            first_events = [e.ref for e in case.ledger.events_for_run(current.run_id)
                            if e.kind == "budget_event" and e.ref.startswith("costop-")]
            case.orch.cost_quote_adapter = lambda *_: D.CostQuote(
                0, C.CostEvidence("free_local", "fixture:verified-local-program"))
            require(await case.orch.resume(current.run_id))
            current = await drive_to(case, {S.RUNNING})
            require_equal(current.state, S.RUNNING, current.result_summary)
            require_equal([call["kind"] for call in calls(case)], ["plan"])
            records = D.invocations(case.ledger, case.task.task_id)
            require_equal(len(records), 1)
            require(records[0]["operation_id"] not in first_events)
            require_equal(case.costs.view(case.task.task_id)["counts"],
                          {"unbounded_cost": 1, "settled": 1})
    asyncio.run(scenario())


def t_plan_and_specialist_share_exact_ten_euro_threshold_before_second_dispatch():
    async def scenario():
        with fixture(quote=500) as case:
            current = await drive_to(case, {S.WAITING_USER})
            require_equal(current.state, S.WAITING_USER, current.result_summary)
            view = Q.run_view(case.ledger, current)
            require_equal(view["anbietergrenze"]["grund"], "cost_approval_required")
            require_equal(view["anbietergrenze"]["phase"], "specialist")
            require_equal([call["kind"] for call in calls(case)], ["plan"])
            records = D.invocations(case.ledger, case.task.task_id)
            require_equal(len(records), 1)
            require_equal(records[0]["phase"], "plan")
            cost = case.costs.view(case.task.task_id)
            require_equal(cost["ai_tool"]["reserved_cents"], 500)
            require_equal(cost["ai_tool"]["spent_cents"], 0)
            require_equal(cost["counts"], {"reserved": 1, "approval_required": 1})
    asyncio.run(scenario())


def t_real_plan_format_repair_and_assessment_repair_each_claim_and_settle_separately():
    async def scenario():
        with fixture(plans=["kein JSON", PLAN], assessments=[{"beantwortet": []}, VERDICT]) as case:
            current = await drive_to(case, {S.RUNNING})
            require_equal(current.state, S.RUNNING, current.result_summary)
            require_equal(current.planner_calls, 2)
            bound = RQ.validate({"auskunft": [{"id": "a1", "text": "Was sagt README?"}],
                                 "handlungen": [], "unklar": [], "belege": {"mindestens": 0}},
                                objective=case.task.objective)
            body = RQ.snapshot_body(["README beschreibt Kontingente."], ["README.md:2"])
            verdict = await case.orch._ask_assessment(current, case.task, bound, body,
                RQ.snapshot_digest(body), RQ.digest_of(bound))
            require_equal(verdict["beantwortet"], VERDICT["beantwortet"])
            require_equal(verdict["run_id"], current.run_id)
            require_equal(case.ledger.get_run(current.run_id).assessment_calls, 2)
            records = D.invocations(case.ledger, case.task.task_id)
            require_equal([record["phase"] for record in records], ["plan", "plan", "assessment", "assessment"])
            require_equal([record["ordinal"] for record in records], [1, 2, 1, 1])
            require_equal(records[0]["operation_id"], records[1]["operation_id"])
            require(records[2]["operation_id"] != records[3]["operation_id"])
            require_equal(len({record["invocation_id"] for record in records}), 4)
            require_equal([call["claimed"] for call in calls(case)], [1, 1, 1, 1])
            require_equal(case.costs.view(case.task.task_id)["counts"], {"settled": 4})
            require_equal(case.costs.view(case.task.task_id)["ai_tool"]["total_cents"], 0)
    asyncio.run(scenario())


def t_parallel_actual_cli_calls_share_runtime_scope_and_hold_exact_ten_euro_gate():
    async def scenario():
        with fixture(quote=500, wait=True) as case:
            case.ledger.transition(case.run.run_id, S.PLANNING)
            payload = {"input": [{"role": "user", "content": json.dumps({"ziel": F.OBJECTIVE})}]}
            with case.orch._cost_scope(case.ledger.get_run(case.run.run_id), "plan"):
                first = asyncio.create_task(case.transport(payload))
                try:
                    for _ in range(200):
                        if calls(case):
                            break
                        await asyncio.sleep(0.01)
                    require_equal(len(calls(case)), 1)
                    blocked = await case.transport(payload)
                    require_equal(blocked["reason"], "cost_approval_required")
                    require(not blocked["dispatch_started"])
                    require_equal(len(calls(case)), 1)
                finally:
                    (case.folder / "release").touch()
                    completed = await first
                require(completed["ok"])
            require_equal(len(D.invocations(case.ledger, case.task.task_id)), 1)
            require_equal(case.costs.view(case.task.task_id)["ai_tool"]["reserved_cents"], 500)
    asyncio.run(scenario())


def t_prior_actual_timeout_blocks_new_runtime_operation_and_cannot_resume():
    async def scenario():
        with fixture(quote=500, wait=True, timeout=0.2) as case:
            payload = {"input": [{"role": "user", "content": json.dumps({"ziel": F.OBJECTIVE})}]}
            # Ein echter abgebrochener Prozess hinterlaesst einen unklaren
            # Anspruch. Anschliessend versucht die echte Runtime mit neuem
            # Ereignis zu planen; die taskweite Sperre muss vorher greifen.
            with case.orch._cost_scope(case.run, "plan"):
                previous = await case.transport(payload)
            require(previous["dispatch_started"])
            require_equal(previous["cost_status"], "unknown")
            (case.folder / "release").touch()
            current = await drive_to(case, {S.WAITING_USER})
            require_equal(current.state, S.WAITING_USER)
            view = Q.run_view(case.ledger, current)
            require_equal(view["anbietergrenze"]["grund"], "cost_recovery_required")
            require(not view["anbietergrenze"]["fortsetzbar"])
            require(not await case.orch.resume(current.run_id))
            require_equal(len(calls(case)), 1)
            require_equal(case.costs.view(case.task.task_id)["counts"], {"unknown": 1})
            records = D.invocations(case.ledger, case.task.task_id)
            require_equal(len(records), 1)
            require_equal(records[0]["state"], "unknown")
            fresh = S.AgentRunLedger(case.ledger.path)
            require_equal(D.invocations(fresh, case.task.task_id), records)
    asyncio.run(scenario())


def t_runtime_first_timeout_already_reports_cost_recovery_without_unsafe_resume():
    async def scenario():
        with fixture(quote=500, wait=True, timeout=0.2) as case:
            current = await drive_to(case, {S.WAITING_USER})
            require_equal(current.state, S.WAITING_USER, current.result_summary)
            view = Q.run_view(case.ledger, current)
            require_equal(view["anbietergrenze"]["grund"], "cost_recovery_required")
            require(not view["anbietergrenze"]["fortsetzbar"])
            require(not await case.orch.resume(current.run_id))
            require_equal(len(calls(case)), 1)
            require_equal(case.costs.view(case.task.task_id)["counts"], {"unknown": 1})
    asyncio.run(scenario())


def t_assessment_first_actual_timeout_preserves_unknown_and_opens_nonresumable_boundary():
    async def scenario():
        with fixture(quote=400, timeout=0.2) as case:
            current = await drive_to(case, {S.RUNNING})
            require_equal(current.state, S.RUNNING, current.result_summary)
            wait_at_next_provider_call(case)
            bound = RQ.validate({"auskunft": [{"id": "a1", "text": "Was sagt README?"}],
                                 "handlungen": [], "unklar": [], "belege": {"mindestens": 0}},
                                objective=case.task.objective)
            body = RQ.snapshot_body(["README beschreibt Kontingente."], ["README.md:2"])
            # Die echte Bewertungsphase wird hier separat aufgerufen; ein
            # Build verlangt regulaer keine semantische Ergebnisbewertung.
            # Die Behandlung der geworfenen Grenze ist dieselbe wie in tick.
            try:
                await case.orch._ask_assessment(current, case.task, bound, body,
                    RQ.snapshot_digest(body), RQ.digest_of(bound))
            except _ProviderPause as pause:
                require_equal(pause.phase, "assessment")
                require_equal(pause.call.reason, "cost_recovery_required")
                require_equal(pause.call.cost_status, "unknown")
                require(pause.call.cost_reservation_id)
                await case.orch._open_provider_boundary(current.run_id, pause.call, pause.phase)
            else:
                raise AssertionError("unklare Bewertung wurde nicht als Kostengrenze gemeldet")
            view = Q.run_view(case.ledger, case.ledger.get_run(current.run_id))
            require_equal(view["anbietergrenze"]["grund"], "cost_recovery_required")
            require_equal(view["anbietergrenze"]["phase"], "assessment")
            require(not view["anbietergrenze"]["fortsetzbar"])
            require(not await case.orch.resume(current.run_id))
            require_equal([call["kind"] for call in calls(case)], ["plan", "assessment"])
            require_equal(case.costs.view(case.task.task_id)["counts"], {"reserved": 1, "unknown": 1})
            require_equal([row["state"] for row in D.invocations(case.ledger, case.task.task_id)],
                          ["finished", "unknown"])
    asyncio.run(scenario())


def t_specialist_first_actual_timeout_parks_original_step_without_replan_or_resume():
    async def scenario():
        with fixture(quote=400) as case:
            current = await drive_to(case, {S.RUNNING})
            require_equal(current.state, S.RUNNING, current.result_summary)
            wait_at_next_provider_call(case)
            invocation_for = SP._invocation_for

            def short_deadline(spec, request):
                # Nur die Frist wird fuer den echten lokalen Prozess kleiner.
                # Laufzeitadapter, Launcher und Timeoutbereinigung bleiben echt.
                return replace(invocation_for(spec, request), timeout=0.2)

            with patch.object(SP, "_invocation_for", short_deadline):
                current = await drive_to(case, {S.WAITING_USER})
            require_equal(current.state, S.WAITING_USER, current.result_summary)
            view = Q.run_view(case.ledger, current)
            require_equal(view["anbietergrenze"]["grund"], "cost_recovery_required")
            require_equal(view["anbietergrenze"]["phase"], "specialist")
            require(not view["anbietergrenze"]["fortsetzbar"])
            require(not await case.orch.resume(current.run_id))
            require_equal(current.planner_calls, 1, "unklarer Spezialist loeste Nachplanung aus")
            steps = [step for step in case.ledger.steps_for_run(current.run_id)
                     if step.kind == "specialist"]
            require_equal(len(steps), 1)
            require_equal(steps[0].state, "unknown")
            require_equal([call["kind"] for call in calls(case)], ["plan", "specialist"])
            require_equal(case.costs.view(case.task.task_id)["counts"], {"reserved": 1, "unknown": 1})
            require_equal([row["state"] for row in D.invocations(case.ledger, case.task.task_id)],
                          ["finished", "unknown"])
    asyncio.run(scenario())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
