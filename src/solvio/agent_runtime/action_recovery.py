"""Atomically resume the same action only after a proven native non-start."""
import time

from solvio.agent_runtime import action_contract as AC, checkpoint as CP, store as S


def resume_boundary(ledger, run, boundary, checkpoint, revision):
    """The existing run owns state, cursor and attempt; commit them together.

    A resume click changes no resource and cannot reconcile an unknown effect.
    Missing/rotated account bindings are rechecked by the service afterwards.
    """
    if not boundary or not boundary.repeat_step or boundary.seq < 1:
        return False
    blob = S._safe_json_record(checkpoint, S.MAX_PLAN_CHECKPOINT, where="agent_run.plan_checkpoint")
    if blob != checkpoint:
        return False
    decoded = CP.decode(blob)
    if not decoded or decoded.get("revision") != revision or decoded.get("cursor") != boundary.seq - 1:
        return False
    with ledger._open() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("SELECT * FROM agent_runs WHERE run_id=?", (run.run_id,)).fetchone()
        task = connection.execute("SELECT * FROM agent_tasks WHERE task_id=?", (run.task_id,)).fetchone()
        step = connection.execute("SELECT * FROM agent_steps WHERE step_id=?", (boundary.step_id,)).fetchone()
        if (current is None or task is None or task["scope"] != S.SCOPE_ACTION
                or current["state"] != S.WAITING_USER or current["boundary"] != run.boundary
                or current["plan_revision"] != run.plan_revision or revision != run.plan_revision + 1
                or step is None or step["seq"] != boundary.seq):
            return False
        row = connection.execute("SELECT * FROM agent_action_contracts WHERE run_id=?", (run.run_id,)).fetchone()
        if row is None:
            return False
        bound = AC._with_grant(connection, ledger, AC._bound_row(row), active=True)
        action = bound.actions[boundary.seq - 1] if boundary.seq <= len(bound.actions) else None
        if action is None or not AC.can_retry_step(connection, ledger, step,
                task_id=run.task_id, run_id=run.run_id, capability=AC.CAPABILITY,
                arguments=bound.action_arguments(action["action_id"]), version=AC.VERSION):
            return False
        cursor = connection.execute("UPDATE agent_runs SET state=?,boundary='',plan_checkpoint=?,"
            "plan_revision=?,updated_at=? WHERE run_id=? AND state=? AND boundary=? AND plan_revision=?",
            (S.RUNNING, blob, revision, time.time(), run.run_id, S.WAITING_USER,
             run.boundary, run.plan_revision))
        return bool(cursor.rowcount)
