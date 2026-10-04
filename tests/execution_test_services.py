"""P1C/F4 test adapters — external services that persist ACROSS PROCESSES.

Real crash tests need the external effect to survive the SIGKILL that ends the process which
caused it. In-memory doubles cannot show that. Each service therefore keeps its effects in
its own SQLite file, entirely separate from the SOLVIO store, so a restarted process sees
exactly what a real remote service would have retained.

No real email, file or Home Assistant action is ever performed here.
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS effects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT, execution_id TEXT NOT NULL, note TEXT);
"""


class _Service:
    def __init__(self, path: str) -> None:
        self.path = path
        con = self._con()
        try:
            con.executescript(SCHEMA)
        finally:
            con.close()

    def _con(self):
        con = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def effects(self) -> list[dict]:
        con = self._con()
        try:
            return [dict(r) for r in con.execute(
                "SELECT * FROM effects ORDER BY id").fetchall()]
        finally:
            con.close()

    def count(self, execution_id: str | None = None) -> int:
        rows = self.effects()
        if execution_id is None:
            return len(rows)
        return len([r for r in rows if r["execution_id"] == execution_id])


class IdempotentService(_Service):
    """Deduplicates on the idempotency key, the way a well-behaved remote API would."""

    def send(self, execution_id: str, idempotency_key: str, note: str = "") -> str:
        con = self._con()
        try:
            con.execute("BEGIN IMMEDIATE")
            hit = con.execute("SELECT id FROM effects WHERE idempotency_key=?",
                              (idempotency_key,)).fetchone()
            if hit:
                con.execute("COMMIT")
                return "duplicate_ignored"
            con.execute("INSERT INTO effects (idempotency_key, execution_id, note) "
                        "VALUES (?,?,?)", (idempotency_key, execution_id, note))
            con.execute("COMMIT")
            return "sent"
        finally:
            con.close()


class NonIdempotentService(_Service):
    """Every invocation is a new external effect. No key, no deduplication, no way back."""

    def send(self, execution_id: str, idempotency_key: str, note: str = "") -> str:
        con = self._con()
        try:
            con.execute("INSERT INTO effects (idempotency_key, execution_id, note) "
                        "VALUES (?,?,?)", (None, execution_id, note))
        finally:
            con.close()
        return "sent"


class ReconcilableService(NonIdempotentService):
    """Duplicates like the non-idempotent one, but can be ASKED what it already did."""

    def has_effect(self, execution_id: str) -> bool:
        return self.count(execution_id) > 0


def make(kind: str, path: str):
    return {"idempotent": IdempotentService,
            "non_idempotent": NonIdempotentService,
            "reconcilable": ReconcilableService}[kind](path)


def adapter(service, *, fail=None):
    """An async executor over one of the services above.

    `fail` selects a deliberate failure mode: "safe" raises SafeExecutionFailure BEFORE any
    effect (the adapter knows nothing happened), "ambiguous" performs the effect and then
    raises an ordinary exception (nobody can know).
    """
    from solvio.security.mobile_approval import execution as X

    async def run(action):
        if fail == "safe":
            raise X.SafeExecutionFailure("refused before contacting the service")
        out = service.send(action["execution_id"], action["idempotency_key"],
                           action.get("task", ""))
        if fail == "ambiguous":
            raise RuntimeError("connection dropped after the request was sent")
        return True, {"service": out}
    return run


def reconciler(service):
    async def check(execution_id):
        return service.has_effect(execution_id)
    return check


def service_path(root: str, name: str = "external") -> str:
    return os.path.join(root, f"{name}_service.sqlite3")
