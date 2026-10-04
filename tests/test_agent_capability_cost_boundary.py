"""N2: READ_ONLY is not proof that a generic capability costs no money.

Actual HTTPS task admission, grant, orchestrator, router and temporary stores.
The registered research_quick uses its real production specification; only its
executor and the planner's returned text are supplied by the test. No provider
or paid service is called, even when reproducing the pre-fix failure.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from test_agent_task_entry import world
import mobile_attest_helper as H
from solvio.agent_runtime import planner as PL, store as S
from solvio.agent_runtime.orchestrator import Orchestrator
from solvio.capabilities.research_quick import SPECS
from solvio.security.mobile_approval import protocol as P

QUESTION = "Welche drei Hotels in Hamburg sind heute verfuegbar?"
PROPOSAL = {"schritte": [{"art": "capability", "faehigkeit": "research_quick",
                            "argumente": {"question": QUESTION}}]}


async def admit(w):
    calls = []

    async def chargeable_executor(arguments):
        calls.append(dict(arguments))
        return {"answer": "A real executor would have called the paid native search."}

    w.router.register(SPECS["research_quick"], chargeable_executor)
    require(SPECS["research_quick"].is_read_only(), "this is the production READ_ONLY spec")
    response = await w.start()
    require_equal(response.status, 201)
    data = await response.json()
    run = w.ledger.get_run(data["run_id"])
    grant = w.orch.task_authority.for_run(run.run_id)
    require(grant is not None, "the actual HTTP task must own its persistent grant")
    return calls, run, grant


async def t_explicit_preexisting_plan_cannot_dispatch_unpriced_research_quick():
    async with world() as w:
        calls, run, grant = await admit(w)
        # A model-proposed/persisted plan is not authority. Deliberately supply
        # the step directly here so catalog filtering cannot hide a broken
        # execution gate. Real _advance still owns all subsequent dispatch.
        context = w.orch._contexts[run.run_id]
        context.plan = PL.Plan(goal=w.ledger.get_task(run.task_id).objective,
            steps=(PL.PlannedStep(kind="capability", capability="research_quick",
                                  arguments={"question": QUESTION}),))
        w.ledger.transition(run.run_id, S.PLANNING)
        w.ledger.transition(run.run_id, S.RUNNING)
        await w.orch._advance(w.ledger.get_run(run.run_id))
        require_equal(calls, [], "READ_ONLY must not spend through a generic task capability")
        require_equal(await w.store.list_pending(), [], "no fallback approval may disguise this missing cost contract")
        steps = w.ledger.steps_for_run(run.run_id)
        require_equal(len(steps), 1)
        require_equal(steps[0].state, "failed")
        require_equal(steps[0].outcome_reason, "rejected_by_policy:capability_not_granted")
        require("research_quick" not in {c.name for c in grant.capabilities})


async def t_actual_planner_catalog_and_validation_exclude_unpriced_capability():
    async with world() as w:
        calls, run, _ = await admit(w)
        model_requests = []

        async def proposed_plan(payload, **kwargs):
            model_requests.append(payload)
            # Insist on the forbidden capability even after a format repair.
            return {"ok": True, "text": json.dumps(PROPOSAL), "tokens": 1}

        w.orch.planner = PL.Planner(transport=proposed_plan)
        await w.orch.tick()  # CREATED -> PLANNING
        await w.orch.tick()  # Actual planner, catalog and validation.
        current = w.ledger.get_run(run.run_id)
        require_equal(current.state, S.FAILED, "an explicit ungranted capability must not form an accepted plan")
        require_equal(current.failure_category, "plan_invalid")
        require(model_requests, "the test must reach the actual model-plan validation")
        for payload in model_requests:
            catalog = json.loads(payload["input"][1]["content"])["auswahl"]
            require("research_quick" not in catalog["faehigkeiten"])
            require("research_quick" not in catalog["eingabevertraege"])
        require_equal(calls, [])
        require_equal(await w.store.list_pending(), [])
        require_equal(w.ledger.steps_for_run(run.run_id), [])


async def t_production_mode_stops_legacy_runs_without_inventing_task_authority():
    for state in (S.CREATED, S.RUNNING, S.WAITING_APPROVAL):
        async with world() as w:
            calls = []

            async def capability(arguments):
                calls.append("capability")
                return {"answer": "Would have reached native paid search."}

            async def model(payload, **kwargs):
                calls.append("model")
                return {"ok": True, "text": json.dumps(PROPOSAL), "tokens": 1}

            w.router.register(SPECS["research_quick"], capability)
            production = Orchestrator(ledger=w.ledger, router=w.router, control_plane=w.cp,
                planner=PL.Planner(transport=model), require_task_authority=True)
            # Represent an actual pre-N2 row. This trusted test/service seam
            # deliberately creates no source receipt or migrated grant.
            task, run = production.create_task(objective="Vergleiche drei Hotels mit Quellen.",
                scope="research", origin="local_owner", principal="local-owner")
            context = production._contexts[run.run_id]
            context.plan = PL.Plan(goal=task.objective, steps=(PL.PlannedStep(
                kind="capability", capability="research_quick", arguments={"question": QUESTION}),))
            if state != S.CREATED:
                w.ledger.transition(run.run_id, S.PLANNING)
                w.ledger.transition(run.run_id, S.RUNNING)
            if state == S.WAITING_APPROVAL:
                key = await w.router._mobile.request(SPECS["research_quick"],
                    {"question": QUESTION}, requested_by="legacy-task")
                device = await H.enroll_attested(w.cp)
                challenge, status = await w.cp.issue_challenge(approval_id=key, device_id=device.device_id)
                require_equal(status, "ok")
                _, status = await w.co.apply_mobile_decision(
                    **H.sign_decision(device, P.b64d(challenge["payload_b64"])))
                require_equal(status, "ok")
                step = w.ledger.create_step(run_id=run.run_id, seq=1, kind="capability",
                                            capability="research_quick")
                w.ledger.update_step(step.step_id, state="waiting", approval_id=key)
                context.pending_step_id = step.step_id
                w.ledger.transition(run.run_id, S.WAITING_APPROVAL)
            await production.tick()
            await production.tick()
            require_equal(calls, [], f"legacy {state} escaped the production authority gate")
            final = w.ledger.get_run(run.run_id)
            require_equal(final.state, S.FAILED, state)
            require_equal(final.failure_category, "policy_denied", state)
            require_equal(production.task_authority.for_run(run.run_id), None)
            with w.ledger._open() as connection:
                require_equal(connection.execute("SELECT COUNT(*) FROM agent_task_sources").fetchone()[0], 0)
                require_equal(connection.execute("SELECT COUNT(*) FROM agent_cost_policies").fetchone()[0], 0)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
