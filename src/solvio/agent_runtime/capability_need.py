"""Nonexecuting, crash-safe development intent in the existing run ledger."""
from __future__ import annotations

import hashlib
import json
import time

from solvio.agent_runtime import store as S, document_contract as DC, file_inputs as FI
from solvio.agent_runtime.task_authority import TaskAuthority


def bound_for_run(ledger, run_id):
    document = DC.for_run(ledger, run_id)
    if document is not None:
        return document
    from solvio.agent_runtime import table_report_contract as TC
    return TC.for_run(ledger, run_id)


def capability(bound):
    return getattr(bound, "capability", DC.CAPABILITY)


def resource(bound):
    return getattr(bound, "resource", DC.RESOURCE)


def milestone_id(run_id: str, contract_digest=DC.CONTRACT_DIGEST) -> str:
    return "adapter-" + hashlib.sha256((run_id + "\0" + contract_digest).encode()).hexdigest()[:32]


def pending(ledger, run_id):
    return next((step for step in ledger.steps_for_run(run_id)
                 if step.kind == "capability_need" and step.state == "waiting"
                 and step.capability in {DC.CAPABILITY, FI.CAPABILITY}), None)


def replan_pending(ledger, run_id):
    """A resolved need is still nonexecuting until its follow-up plan is durable."""
    return next((step for step in ledger.steps_for_run(run_id)
                 if step.kind == "capability_need" and step.state == "skipped"
                 and step.capability in {DC.CAPABILITY, FI.CAPABILITY}
                 and step.outcome_reason == "implementation_ready"), None)


def resolve(ledger, *, run_id, step_id, checkpoint):
    """Atomically consume the need and charge ONE still-pending plan revision.

    Keeping the prior plan is intentional: it retains all findings and budget
    digests. The step marker forces planning, even after reconcile changes the
    run to INTERRUPTED; it never authorizes a capability or proves an effect.
    """
    from solvio.agent_runtime import budget as BU, checkpoint as CP
    bound = bound_for_run(ledger, run_id)
    if bound is None:
        raise ValueError("document_contract_not_authorized")
    from solvio.agent_runtime.task_revisions import task_view
    task = task_view(ledger, run_id)
    body = CP.decode(checkpoint)
    if body is None:
        raise ValueError("need_checkpoint_required")
    with ledger._open() as connection:
        connection.execute("BEGIN IMMEDIATE")
        allowed = TaskAuthority(ledger)._verify(connection, bound.grant_reference,
            capability(bound), bound.arguments, 1, task_id=bound.task_id, run_id=run_id)
        if not allowed.allowed:
            raise ValueError(allowed.reason)
        run = connection.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        step = connection.execute("SELECT * FROM agent_steps WHERE step_id=? AND run_id=?",
                                  (step_id, run_id)).fetchone()
        if (run["state"] != S.WAITING_CAPABILITY
                or run["development_ref"] != milestone_id(run_id, bound.contract_digest)
                or body["revision"] != run["plan_revision"] or step is None
                or step["kind"] != "capability_need" or step["capability"] != capability(bound)
                or step["state"] != "waiting" or step["dispatch_claimed_at"] is not None
                or step["attempt"] != run["plan_revision"] + 1
                or not 0 < step["seq"] <= len(body["schritte"])):
            raise ValueError("need_resolution_changed")
        proposed = body["schritte"][step["seq"] - 1]
        if (proposed.get("art"), proposed.get("vertrag"), proposed.get("ressource")) != (
                "capability_need", bound.contract, resource(bound)):
            raise ValueError("need_binding_changed")
        BU.BudgetLedger(BU.Budget.from_dict(task.budget),
                        plan_revisions=run["plan_revision"]).check_revision()
        body["revision"] += 1
        body["notizen"] = (body.get("notizen", []) + [
            "[core] Das gepruefte Werkzeug ist jetzt verfuegbar. "
            "Verarbeite die gebundene Eingabe."])[-CP.MAX_NOTES:]
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        if len(raw) > S.MAX_PLAN_CHECKPOINT:
            raise ValueError("need_checkpoint_too_large")
        raw = S._safe_json_record(raw, S.MAX_PLAN_CHECKPOINT, where="agent_run.plan_checkpoint")
        now = time.time()
        connection.execute("UPDATE agent_steps SET state='skipped',finished_at=?,"
            "summary=?,outcome_reason='implementation_ready' WHERE step_id=?", (now,
            "Das gepruefte Werkzeug ist bereit; die Eingabe wurde noch nicht verarbeitet.", step_id))
        connection.execute("UPDATE agent_runs SET state=?,plan_revision=?,plan_checkpoint=?,"
            "updated_at=? WHERE run_id=?", (S.RUNNING, body["revision"], raw, now, run_id))


def finish_replan(ledger, *, run_id, checkpoint):
    """Clear the obligation only in the transaction saving its actual new plan."""
    from solvio.agent_runtime import checkpoint as CP
    bound = bound_for_run(ledger, run_id)
    if bound is None:
        raise ValueError("document_contract_not_authorized")
    body = CP.decode(checkpoint)
    if body is None:
        raise ValueError("need_checkpoint_required")
    raw = S._safe_json_record(checkpoint, S.MAX_PLAN_CHECKPOINT, where="agent_run.plan_checkpoint")
    with ledger._open() as connection:
        connection.execute("BEGIN IMMEDIATE")
        run = connection.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        steps = connection.execute("SELECT * FROM agent_steps WHERE run_id=? AND kind='capability_need' "
            "AND state='skipped' AND capability=? AND outcome_reason='implementation_ready'",
            (run_id, capability(bound))).fetchall()
        if (run is None or run["state"] not in {S.PLANNING, S.RUNNING, S.INTERRUPTED}
                or run["development_ref"] != milestone_id(run_id, bound.contract_digest)
                or body["revision"] != run["plan_revision"] or len(steps) != 1
                or steps[0]["attempt"] != run["plan_revision"]):
            raise ValueError("need_replan_changed")
        connection.execute("UPDATE agent_runs SET plan_checkpoint=?,updated_at=? WHERE run_id=?",
                           (raw, time.time(), run_id))
        connection.execute("UPDATE agent_steps SET outcome_reason='implementation_replanned' WHERE step_id=?",
                           (steps[0]["step_id"],))


def park(ledger, *, run_id, seq, attempt, checkpoint):
    """Need step + checkpoint + intent + WAITING_CAPABILITY are one transaction.

    No approval, authority claim, handler or provider is invoked here. The
    existing poller reconciles this intent with the Autopilot ledger afterward.
    """
    bound = bound_for_run(ledger, run_id)
    if bound is None:
        raise ValueError("document_contract_not_authorized")
    from solvio.agent_runtime import checkpoint as CP
    body = CP.decode(checkpoint)
    if (body is None or body.get("revision") != attempt - 1
            or body.get("cursor") != seq - 1 or seq < 1
            or seq > len(body["schritte"])):
        raise ValueError("need_checkpoint_required")
    proposed = body["schritte"][seq - 1]
    if (proposed.get("art") != "capability_need" or proposed.get("vertrag") != bound.contract
            or proposed.get("ressource") != resource(bound)):
        raise ValueError("need_binding_changed")
    from solvio.agent_runtime import planner as PL
    from solvio.agent_runtime.task_revisions import task_view
    task = task_view(ledger, run_id)
    PL.validate({"schritte": [proposed]}, scope=task.scope, goal=task.objective,
        allowed_profiles=set(), known_capabilities=set(),
        allowed_needs={bound.contract: resource(bound)})
    checkpoint = S._safe_json_record(checkpoint, S.MAX_PLAN_CHECKPOINT,
                                      where="agent_run.plan_checkpoint")
    authority = TaskAuthority(ledger)
    reference = milestone_id(run_id, bound.contract_digest)
    now = time.time()
    with ledger._open() as connection:
        connection.execute("BEGIN IMMEDIATE")
        allowed = authority._verify(connection, bound.grant_reference, capability(bound),
            bound.arguments, 1, task_id=bound.task_id, run_id=run_id)
        if not allowed.allowed:
            raise ValueError(allowed.reason)
        run = connection.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if (run["state"] not in {S.RUNNING, S.WAITING_CAPABILITY}
                or run["plan_revision"] != attempt - 1
                or run["development_ref"] not in {"", reference}):
            raise ValueError("need_run_changed")
        prior = connection.execute("SELECT * FROM agent_steps WHERE run_id=? AND seq=? AND attempt=?",
                                   (run_id, seq, attempt)).fetchone()
        if prior:
            if (prior["kind"] != "capability_need" or prior["state"] != "waiting"
                    or prior["capability"] != capability(bound) or prior["dispatch_claimed_at"] is not None):
                raise ValueError("need_step_changed")
            step_id = prior["step_id"]
        else:
            step_id = S.new_step_id()
            connection.execute("INSERT INTO agent_steps (step_id,run_id,seq,kind,state,attempt,"
                "capability,summary,outcome_reason) VALUES (?,?,?,'capability_need','waiting',?,?,?,?)",
                (step_id, run_id, seq, attempt, capability(bound),
                 "Wartet auf das gepruefte Werkzeug.", "implementation_missing"))
        connection.execute("UPDATE agent_runs SET state=?,development_ref=?,plan_checkpoint=?,"
            "updated_at=? WHERE run_id=?", (S.WAITING_CAPABILITY, reference, checkpoint, now, run_id))
    return step_id, reference
