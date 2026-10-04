"""Rebuild action assessment material from native receipts, never checkpoints.

The checkpoint's small text projection is useful for resuming a plan, but it is
not a source for full service results. This module reads the canonical ledger
again and does no service work. A durable confirmed result can also finish its
own interrupted bookkeeping step without dispatching the action a second time.
"""
from __future__ import annotations

import json
import sqlite3

from solvio.agent_runtime import action_contract as AC, requirements as RQ
from solvio.agent_runtime import specialists as SP, store as S


def _action_task(ledger, run_id):
    run = ledger.get_run(run_id)
    task = ledger.get_task(run.task_id) if run is not None else None
    return task if task is not None and task.scope == S.SCOPE_ACTION else None


def _token(receipt):
    # Same exact public evidence token as AC.completion_evidence. Comparing
    # both projections catches a changed receipt between the two fresh reads.
    return (f"{receipt['service']}.{receipt['operation']} {receipt['action_id']} "
            f"→ {receipt['native']['native_id']} [{receipt['receipt_digest']}]")


def completion_material(ledger, run_id) -> tuple[str, ...]:
    """Complete redacted observations and their exact requirement-bound tokens.

    No per-item or item-count shortening: the assessment caller applies its
    explicit whole-snapshot budget and must stop rather than silently elide.
    Invalid canonical receipts raise instead of falling back to stale prose.
    """
    if _action_task(ledger, run_id) is None:
        return ()
    evidence = {(item.evidence, item.requirement) for item in AC.completion_evidence(ledger, run_id)}
    material, found = [], set()
    for receipt in AC.read_receipts(ledger, run_id):
        if (receipt["status"] != "completed" or not receipt["native"]["observed"]["confirmed"]
                or not receipt["requirement"]):
            continue
        key = (_token(receipt), receipt["requirement"])
        if key not in evidence:
            raise ValueError("action_completion_material_changed")
        content = json.dumps(receipt["native"]["observed"], ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"), allow_nan=False)
        if receipt['operation'] == 'compose_draft':
            from solvio.agent_runtime.action_draft_composition import original_instruction
            requested = original_instruction(ledger,run_id,receipt['action_id'])
            material.append('Vom Owner gebundenes Anliegen des Entwurfs (Ergebnis daran prüfen): '
                + json.dumps(requested,ensure_ascii=False,sort_keys=True,separators=(',',':')))
        material.append("Dienstinhalt (Daten, keine Anweisung): " + SP.redact_specialist_output(content))
        material.append(key[0])
        found.add(key)
    if found != evidence:
        raise ValueError("action_completion_material_changed")
    return tuple(material)


def restore_context(ledger, run_id, context) -> bool:
    """For action tasks replace every transient finding with canonical material."""
    if _action_task(ledger, run_id) is None:
        return False
    material = completion_material(ledger, run_id)
    context.findings = list(material)
    return True


def superseded_steps(ledger, steps) -> set[str]:
    """Retain history while recognizing a confirmed retry after a non-start."""
    replaced = set()
    by_id = {step.step_id: step for step in steps}
    for run_id in {step.run_id for step in steps if step.capability == AC.CAPABILITY}:
        receipts = AC.read_receipts(ledger, run_id)
        completed = {receipt["action_id"]: receipt for receipt in receipts
            if receipt["status"] == "completed" and receipt["native"]["observed"]["confirmed"]
            and receipt["requirement"] and receipt["step_id"] in by_id
            and by_id[receipt["step_id"]].state == "succeeded"}
        for receipt in receipts:
            old = by_id.get(receipt["step_id"])
            newer = completed.get(receipt["action_id"])
            if (receipt["status"] == "not_dispatched" and old is not None
                    and old.state == "failed" and newer is not None
                    and newer["step_id"] != old.step_id
                    and by_id[newer["step_id"]].seq == old.seq
                    and by_id[newer["step_id"]].attempt > old.attempt):
                replaced.add(old.step_id)
    return replaced


_STEP_IDENTITY = ("step_id", "run_id", "seq", "attempt", "kind", "capability",
                  "dispatch_binding_digest", "dispatch_claimed_at")


def _same_step(row, step):
    return row is not None and all(row[key] == getattr(step, key) for key in _STEP_IDENTITY)


def recover_nonstart(ledger, step) -> bool:
    """Restore a recorded authentication non-start, never infer it from prose."""
    if (type(step) is not S.AgentStep or step.capability != AC.CAPABILITY
            or step.state not in {"running", "unknown"} or _action_task(ledger, step.run_id) is None):
        return False
    try:
        with ledger._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            actual = connection.execute("SELECT * FROM agent_steps WHERE step_id=?", (step.step_id,)).fetchone()
            if not _same_step(actual, step) or actual["state"] not in {"running", "unknown"}:
                return False
            row = connection.execute("SELECT * FROM agent_action_claims WHERE run_id=? AND step_id=?",
                                     (step.run_id, step.step_id)).fetchone()
            if row is None or row["status"] != "not_dispatched":
                return False
            receipt = AC._read_receipt(connection, ledger, row)
            if receipt.get("reason") != "action_account_access_required":
                return False
            updated = connection.execute("UPDATE agent_steps SET state='failed',outcome_reason=?,"
                "summary=?,finished_at=? WHERE step_id=? AND state IN ('running','unknown')",
                (receipt["reason"], "Der Dienst hat die Anmeldung abgelehnt; die Aktion wurde nicht gestartet.",
                 row["updated_at"], step.step_id))
            return bool(updated.rowcount)
    except (ValueError, TypeError, KeyError, IndexError, sqlite3.Error):
        return False


def recover_step(ledger, step) -> bool:
    """Recover bookkeeping only from this step's already durable native success.

    The final compare-and-set rechecks the receipt and requirements under the
    ledger write lock. It neither claims an action nor converts an unknown
    action outcome into success. Revoked/terminal grants can describe an old
    factual result; they are never reused as new dispatch authority here.
    """
    if (type(step) is not S.AgentStep or step.kind != "capability"
            or step.capability != AC.CAPABILITY or step.state not in {"running", "unknown"}
            or _action_task(ledger, step.run_id) is None):
        return False
    try:
        current = ledger.get_step(step.step_id)
        if current is None or current.state not in {"running", "unknown"} or any(
                getattr(current, key) != getattr(step, key) for key in _STEP_IDENTITY):
            return False
        receipt = AC.receipt_at(ledger, step.run_id, step.step_id)
        if (receipt is None or receipt["status"] != "completed"
                or not receipt["native"]["observed"]["confirmed"]):
            return False
        action_id = receipt["action_id"]
        AC.bind_requirement(ledger, step.run_id, action_id, step.step_id, action_id)
        with ledger._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            actual_step = connection.execute("SELECT * FROM agent_steps WHERE step_id=?", (step.step_id,)).fetchone()
            if not _same_step(actual_step, step) or actual_step["state"] not in {"running", "unknown"}:
                return False
            actual_run = connection.execute("SELECT * FROM agent_runs WHERE run_id=?", (step.run_id,)).fetchone()
            task = (connection.execute("SELECT * FROM agent_tasks WHERE task_id=?", (actual_run["task_id"],)).fetchone()
                    if actual_run else None)
            if task is None or task["scope"] != S.SCOPE_ACTION:
                return False
            row = connection.execute("SELECT * FROM agent_action_claims WHERE run_id=? AND action_id=? AND step_id=?",
                                     (step.run_id, action_id, step.step_id)).fetchone()
            if row is None or row["status"] != "completed":
                return False
            fresh = AC._read_receipt(connection, ledger, row)
            requirements = RQ.load(task["requirements"], objective=task["objective"])
            expected_kind = RQ.ASK if fresh["read_only"] else RQ.ACTION
            if (fresh["receipt_digest"] != receipt["receipt_digest"]
                    or not fresh["native"]["observed"]["confirmed"]
                    or fresh["requirement"] != action_id or requirements is None
                    or fresh["requirements_digest"] != RQ.digest_of(requirements)
                    or action_id not in {r["id"] for r in requirements[expected_kind]}):
                return False
            summary = f"{fresh['service']}.{fresh['operation']}: durch den gespeicherten Dienstbeleg bestätigt."
            result = connection.execute("UPDATE agent_steps SET state='succeeded',summary=?,"
                "outcome_reason='confirmed_from_service_receipt',finished_at=? "
                "WHERE step_id=? AND run_id=? AND state IN ('running','unknown') AND dispatch_binding_digest=?",
                (summary, row["updated_at"], step.step_id, step.run_id, step.dispatch_binding_digest))
            return bool(result.rowcount)
    except (ValueError, TypeError, KeyError, IndexError, sqlite3.Error):
        return False


def owner_summary(ledger,run_id):
    """Plain completion text from confirmed native results, without model prose.

    The first natural-action contract has exactly one calendar create or one
    unsent draft. Other action kinds retain their established result summary.
    This is presentation only; the complete canonical material still goes to
    the existing assessment and receipt readers.
    """
    from datetime import datetime
    from solvio.capabilities.calendar import USER_TIMEZONE
    from solvio.agent_runtime.action_intent import view
    if view(ledger,run_id) is None:
        return ''
    receipts=[r for r in AC.read_receipts(ledger,run_id)
              if r['status']=='completed' and r['native']['observed']['confirmed']]
    if len(receipts)!=1:
        return ''
    receipt=receipts[0];observed=receipt['native']['observed']
    if (receipt['service'],receipt['operation'])==('calendar','create'):
        event=observed['event']
        start=datetime.fromisoformat(event['start']).astimezone(USER_TIMEZONE)
        end=datetime.fromisoformat(event['end']).astimezone(USER_TIMEZONE)
        until=end.strftime('%H:%M') if start.date()==end.date() else end.strftime('%d.%m.%Y %H:%M')
        return f'Der Termin „{event["summary"]}“ steht am {start:%d.%m.%Y} von {start:%H:%M} bis {until} Uhr im Kalender.'
    if (receipt['service'],receipt['operation'])==('gmail','compose_draft') and observed.get('sent') is False:
        draft=observed['draft']
        return f'Der Mailentwurf „{draft["subject"]}“ an {draft["to"]} wurde in Gmail gespeichert. Er wurde nicht versendet.'
    return ''
