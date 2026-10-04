"""N7 nonexecuting need: real document admission, plan, checkpoint and run book.

Synthetic RTF and temporary SQLite/files only. No Orchestrator, provider,
builder, converter, activation or capability handler is run by this suite.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import budget as BU, capability_need as CN, checkpoint as CP
from solvio.agent_runtime import costs as C, cost_dispatch as CD, document_contract as DC
from solvio.agent_runtime import planner as PL, store as S
from solvio.agent_runtime.task_authority import TaskAuthority, VerifiedTaskReceipt
from solvio.agent_runtime.task_start_service import TaskStartService

RTF = b"{\\rtf1\\ansi Synthetic document for the bound need.}"
ALLOWED = {DC.CONTRACT: DC.RESOURCE}


def raw_step(**changes):
    return {"art": "capability_need", "vertrag": DC.CONTRACT,
            "ressource": DC.RESOURCE, "erfuellt": "h1", **changes}


def validate(step=None, *, allowed=ALLOWED):
    return PL.validate({"schritte": [step or raw_step()]}, goal="Lies mein gebundenes Dokument.",
        scope="research", allowed_profiles=set(), known_capabilities=set(), allowed_needs=allowed)


def checkpoint(plan=None, *, revision=0, cursor=0):
    return CP.encode(plan=plan or validate(), revision=revision, cursor=cursor, goal_met="",
        approval_attempts=0, pending_step_id="", notes=["Die Eingabe ist bereits gebunden."],
        findings=["Noch kein Adapter ausgefuehrt."], sources=["core:task-input"],
        invalid_signatures={"seen-format"}, attempts={"a" * 20: 2}, low_value={"b" * 20: 1})


@contextmanager
def world(*, document=True):
    with tempfile.TemporaryDirectory(prefix="solvio-capability-need-") as directory:
        root = Path(directory).resolve()
        with patch.dict(os.environ, {"SOLVIO_STATE_DIR": str(root)}):
            ledger = S.AgentRunLedger(str(root / "agent.sqlite3"))
            grants = TaskAuthority(ledger)
            costs = C.CostLedger(ledger)
            starts = TaskStartService(ledger, grants=grants, costs=costs)
            task, run = starts.create(objective="Lies das beigefuegte RTF-Dokument.",
                scope="research", origin="trusted_dashboard", principal="local-owner",
                receipt=VerifiedTaskReceipt("dashboard_session", "test:need-owner", "local-owner"),
                request_id="need-original-0001", document_request=DC.DocumentRequest(RTF) if document else None)
            require(starts.ready(run.run_id))
            ledger.transition(run.run_id, S.PLANNING)
            ledger.transition(run.run_id, S.RUNNING)
            yield SimpleNamespace(root=root, ledger=ledger, grants=grants, costs=costs, starts=starts,
                task=task, run=run, bound=DC.for_run(ledger, run.run_id) if document else None)


def park(w, **changes):
    kwargs = dict(run_id=w.run.run_id, seq=1, attempt=1, checkpoint=checkpoint())
    kwargs.update(changes)
    return CN.park(w.ledger, **kwargs)


def unparked(w):
    run = w.ledger.get_run(w.run.run_id)
    require_equal(run.state, S.RUNNING)
    require_equal(run.development_ref, "")
    require_equal(run.plan_checkpoint, "")
    require_equal(w.ledger.steps_for_run(w.run.run_id), [])
    require_equal(CD.invocations(w.ledger, w.task.task_id), [])


def rejects(function, message):
    try:
        function()
    except (ValueError, PL.PlanInvalid):
        return
    raise AssertionError(message)


async def t_real_planner_offers_only_server_bound_need_without_executable_arguments():
    with world() as w:
        calls = []
        async def transport(payload, **_):
            calls.append(payload)
            return {"ok": True, "text": json.dumps({"schritte": [raw_step()],
                "anforderungen": {"auskunft": [], "handlungen": [
                    {"id": "h1", "text": w.task.objective}], "unklar": [],
                    "belege": {"mindestens": 1}}}), "tokens": 1}
        planner = PL.Planner(transport=transport)
        budget = BU.BudgetLedger(BU.DEFAULTS["research"])
        plan, call = await planner.plan(goal=w.task.objective, scope=w.task.scope,
            allowed_profiles=set(), known_capabilities=set(), allowed_needs=ALLOWED,
            ledger=budget, run_id=w.run.run_id)
        require(call.ok)
        require_equal(len(calls), 1)
        catalog = json.loads(calls[0]["input"][1]["content"])["auswahl"]
        require_equal(catalog["gebundene_bedarfe"], ALLOWED)
        require_equal(catalog["faehigkeiten"], [])
        require_equal(plan.steps[0].kind, "capability_need")
        require_equal((plan.steps[0].contract, plan.steps[0].resource), (DC.CONTRACT, DC.RESOURCE))
        require_equal((plan.steps[0].capability, plan.steps[0].profile, plan.steps[0].arguments), ("", "", {}))
        unparked(w)


def t_a_model_cannot_invent_a_need_resource_or_hide_execution_in_it():
    for changed in (raw_step(vertrag="build-anything"), raw_step(ressource="another-document"),
                    raw_step(faehigkeit="gmail_send_draft"), raw_step(profil="builder/codex"),
                    raw_step(argumente={"origin": "trusted_dashboard"}),
                    raw_step(auftrag="Run arbitrary code"), raw_step(verzichtbar=True)):
        rejects(lambda: validate(changed), "unbound/executable need accepted")
    rejects(lambda: validate(allowed={}), "an absent owner resource became an available need")
    require_equal(validate().steps[0].requirement, "h1")


def t_checkpoint_roundtrip_retains_need_and_existing_findings_without_creating_authority():
    with world() as w:
        raw = checkpoint()
        restored, reason = CP.restore(raw, goal=w.task.objective, scope=w.task.scope,
            allowed_profiles=set(), known_capabilities=set(), allowed_needs=ALLOWED)
        require_equal(reason, "restored")
        step = restored.plan.steps[0]
        require_equal((step.kind, step.contract, step.resource, step.requirement),
                      ("capability_need", DC.CONTRACT, DC.RESOURCE, "h1"))
        require_equal(restored.plan.goal, w.task.objective)
        require_equal(restored.findings, ["Noch kein Adapter ausgefuehrt."])
        require_equal(restored.attempts, {"a" * 20: 2})
        for allowed in ({}, {DC.CONTRACT: "foreign-input"}):
            denied, reason = CP.restore(raw, goal=w.task.objective, scope=w.task.scope,
                allowed_profiles=set(), known_capabilities=set(), allowed_needs=allowed)
            require_equal((denied, reason), (None, "rejected"))
        unparked(w)


def t_need_intent_checkpoint_and_wait_state_commit_together_without_any_claim():
    with world() as w:
        raw = checkpoint()
        step_id, reference = park(w, checkpoint=raw)
        reopened = S.AgentRunLedger(w.ledger.path)
        run = reopened.get_run(w.run.run_id)
        require_equal((run.state, run.development_ref, run.plan_checkpoint),
                      (S.WAITING_CAPABILITY, CN.milestone_id(w.run.run_id), raw))
        require_equal(reference, run.development_ref)
        step = CN.pending(reopened, w.run.run_id)
        require_equal((step.step_id, step.kind, step.state, step.capability),
                      (step_id, "capability_need", "waiting", DC.CAPABILITY))
        require_equal(step.dispatch_claimed_at, None)
        require_equal(step.dispatch_binding_digest, "")
        require_equal((run.planner_calls, run.specialist_count), (0, 0))
        require_equal(CD.invocations(reopened, w.task.task_id), [])
        require_equal(w.costs.view(w.task.task_id)["counts"], {})
        require_equal(DC.read_for_run(reopened, w.run.run_id, arguments=w.bound.arguments), RTF)


def t_failed_run_update_rolls_back_the_need_step_and_all_intent_fields():
    with world() as w:
        with w.ledger._open() as connection:
            connection.execute("CREATE TRIGGER test_reject_need BEFORE UPDATE OF plan_checkpoint "
                "ON agent_runs WHEN NEW.development_ref <> '' BEGIN "
                "SELECT RAISE(ABORT, 'synthetic need commit failure'); END")
        try:
            park(w)
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("the test did not reach the real SQLite failure boundary")
        unparked(w)
        with w.ledger._open() as connection:
            connection.execute("DROP TRIGGER test_reject_need")
        park(w)
        require_equal(w.ledger.get_run(w.run.run_id).state, S.WAITING_CAPABILITY)
        require_equal(len(w.ledger.steps_for_run(w.run.run_id)), 1)


def t_parallel_and_reopened_need_replay_is_one_step_one_intent():
    with world() as w:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: park(w), range(2)))
        require_equal(results[0], results[1])
        fresh = S.AgentRunLedger(w.ledger.path)
        third = CN.park(fresh, run_id=w.run.run_id, seq=1, attempt=1, checkpoint=checkpoint())
        require_equal(third, results[0])
        require_equal(len(fresh.steps_for_run(w.run.run_id)), 1)
        require_equal(CD.invocations(fresh, w.task.task_id), [])


def t_lost_revoked_foreign_or_changed_grant_never_parks_the_run():
    for change in ("missing", "revoked", "foreign", "arguments", "objective"):
        with world() as w:
            foreign = (w.ledger.create_task(objective="Ein anderer unabhaengiger Auftrag.", scope="research",
                created_origin="trusted_dashboard", created_principal="another-owner")
                if change == "foreign" else None)
            if change == "revoked":
                w.grants.revoke(w.bound.grant_reference, "test:owner-revocation")
            else:
                with w.ledger._open() as connection:
                    if change == "missing":
                        connection.execute("DELETE FROM agent_task_grants WHERE run_id=?", (w.run.run_id,))
                    elif change == "foreign":
                        connection.execute("UPDATE agent_task_grants SET task_id=? WHERE run_id=?",
                                           (foreign.task_id, w.run.run_id))
                    elif change == "arguments":
                        original = w.grants.for_run(w.run.run_id)
                        entries = [{"name": c.name, "version": c.version, "constraints": c.constraints}
                                   for c in original.capabilities]
                        entries[0]["constraints"]["source_sha256"] = "0" * 64
                        connection.execute("UPDATE agent_task_grants SET capabilities=? WHERE run_id=?",
                                           (json.dumps(entries), w.run.run_id))
                    else:
                        connection.execute("UPDATE agent_tasks SET objective=? WHERE task_id=?",
                                           ("A different owner task", w.task.task_id))
            rejects(lambda: park(w), "invalid " + change + " authority became development intent")
            unparked(w)


def t_missing_document_and_changed_input_are_not_development_authority():
    with world(document=False) as w:
        rejects(lambda: park(w), "task without document parked as a document request")
        unparked(w)
    with world() as w:
        artifact = w.ledger.artifacts_for_run(w.run.run_id)[0]
        source = Path(artifact.path)
        source.chmod(0o600)
        source.write_bytes(RTF.replace(b"Synthetic", b"Different"))
        source.chmod(0o400)
        rejects(lambda: park(w), "changed source document was accepted")
        unparked(w)


def t_wrong_need_generation_cursor_or_contract_is_refused_before_writing():
    for changes in ({"seq": 0}, {"seq": 2}, {"attempt": 2}, {"checkpoint": ""},
                    {"checkpoint": checkpoint(revision=1)}, {"checkpoint": checkpoint(cursor=1)}):
        with world() as w:
            rejects(lambda: park(w, **changes), "unbound checkpoint position accepted")
            unparked(w)
    for changes in ({"vertrag": "foreign-contract"}, {"ressource": "foreign-resource"},
                    {"art": "capability"}):
        with world() as w:
            raw = json.loads(checkpoint())
            raw["schritte"][0].update(changes)
            rejects(lambda: park(w, checkpoint=json.dumps(raw)), "wrong need accepted")
            unparked(w)


def t_need_cannot_relabel_an_existing_physically_claimed_capability_step():
    with world() as w:
        step = w.ledger.create_step(run_id=w.run.run_id, seq=1, kind="capability", capability=DC.CAPABILITY)
        w.ledger.update_step(step.step_id, state="running")
        claimed = w.grants.claim_step(w.bound.grant_reference, step.step_id, DC.CAPABILITY,
            w.bound.arguments, DC.VERSION, task_id=w.task.task_id, run_id=w.run.run_id)
        require(claimed.allowed)
        rejects(lambda: park(w), "claimed execution was relabeled as an untouched need")
        current = w.ledger.get_step(step.step_id)
        require_equal(current.kind, "capability")
        require_equal(current.dispatch_binding_digest, claimed.binding_digest)
        require_equal(w.ledger.get_run(w.run.run_id).development_ref, "")


def t_raw_need_checkpoint_may_not_smuggle_execution_arguments_past_the_planner():
    with world() as w:
        raw = json.loads(checkpoint())
        raw["schritte"][0]["argumente"] = {"path": "/unrelated/document.rtf"}
        rejects(lambda: park(w, checkpoint=json.dumps(raw)), "park bypassed the planner's nonexecution contract")
        unparked(w)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
