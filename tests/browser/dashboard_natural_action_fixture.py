"""Real natural-action HTTPS fixture, no external model or native account.

Uses the canonical public ActionWorld and its local subscription-protocol
executable/REST transport. Test control advances explicit stages only; the
browser alone performs task admission and answers the persisted question.
"""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from test_agent_action_intent import intent_world, proposal
from solvio.agent_runtime import endpoint, action_intent as AI, action_contract as AC
from solvio.dashboard import endpoint as D
from solvio.realtime.audio_observations import AudioObservations
from solvio.realtime.satellite_health import SatelliteHealthRegistry

OBJECTIVE = "Trage morgen den Termin Fahrradwerkstatt um 09:00 ein."


async def main(output):
    holder = []
    voice = SimpleNamespace(audio_observations=AudioObservations(), satellite_health=SatelliteHealthRegistry())
    original = endpoint.attach

    def attach(app, *args, **kwargs):
        result = original(app, *args, **kwargs)
        D.attach(app, owner_principal="local-owner", environment="isolated_test",
                 voice_server_provider=lambda: voice, router_provider=lambda: holder[0].router)
        return result

    with patch.object(endpoint, "attach", attach):
        async with intent_world(OBJECTIVE, reply=proposal(OBJECTIVE, title="Fahrradwerkstatt", when="morgen", time="09:00")) as w:
            holder.append(w)
            assert Path(w.ledger.path).is_relative_to(Path(w.folder))
            assert Path(w.folder).is_relative_to(Path(tempfile.gettempdir()))
            assert w.ledger.recent_runs() == []
            for prefix in ("n8-natural-browser-", "n8-natural-fresh-"):
                with patch("solvio.security.mobile_approval.browser_sessions.secrets.token_urlsafe", return_value=prefix.ljust(43, "0")):
                    await w.sessions.issue_enrollment(principal="local-owner")
            stop = asyncio.Event()
            for signum in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(signum, stop.set)
            phase = "ready"

            async def audit():
                rows = []
                for run in w.ledger.recent_runs():
                    task = w.ledger.get_task(run.task_id)
                    with w.ledger._open() as connection:
                        row = connection.execute("SELECT * FROM agent_action_contracts WHERE run_id=?", (run.run_id,)).fetchone()
                        bound = AC._with_grant(connection, w.ledger, AC._bound_row(row), active=False) if row else None
                        original_binding = AI._read(connection, run.run_id)[1]
                    grant = w.orch.task_authority.for_run(run.run_id)
                    rows.append({"run_id": run.run_id, "task_id": task.task_id, "state": run.state,
                        "objective": task.objective, "origin": task.created_origin, "principal": task.created_principal,
                        "intent": AI.view(w.ledger, run.run_id), "binding": original_binding,
                        "actions": list(bound.actions) if bound else [], "grant": grant.reference if grant else None,
                        "receipt_method": grant.receipt_method if grant else None,
                        "receipts": AC.read_receipts(w.ledger, run.run_id), "costs": w.orch.costs.view(task.task_id)})
                with w.ledger._open() as connection:
                    counts = {}
                    for table in ("agent_tasks", "agent_runs", "agent_task_grants", "agent_action_contracts",
                                  "agent_action_claims", "agent_action_intents", "agent_action_intent_answers"):
                        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                        counts[table] = connection.execute("SELECT count(*) FROM " + table).fetchone()[0] if exists else 0
                value = {"phase": phase, "state_root": str(w.folder), "runs": rows, "counts": counts,
                    "local_protocol_calls": w.calls(), "native_calls": w.native.transport.calls,
                    "native_mutations": len(w.native.transport.mutations), "native_writes": w.native.transport.mutations,
                    "pending_approvals": len(await w.store.list_pending())}
                assert len(rows) <= 1 and value["native_mutations"] <= 1
                assert value["pending_approvals"] == 0
                assert w.broker.calls == [] and w.api.await_count == 0
                path = output / "audit-next.json"
                path.write_text(json.dumps(value, indent=2))
                path.replace(output / "audit.json")

            await audit()
            (output / "dashboard-url.txt").write_text(w.origin + "/dashboard/")
            print("PREVIEW " + w.origin + "/dashboard/", flush=True)
            while not stop.is_set():
                control = output / "fixture-control.json"
                if control.exists():
                    command = json.loads(control.read_text())
                    assert set(command) == {"stage"} and command["stage"] in {"interpret", "resolve", "execute"}
                    run_id = w.ledger.recent_runs()[0].run_id
                    if command["stage"] == "interpret" and phase == "ready":
                        assert w.orch.task_authority.for_run(run_id) is None
                        await w.orch.tick()
                        assert w.ledger.get_run(run_id).state == "WAITING_USER"
                        assert AI.view(w.ledger, run_id)["question"]["field"] == "duration"
                        assert w.native.transport.mutations == []
                        phase = "question_ready"
                    elif command["stage"] == "resolve" and phase == "question_ready":
                        assert AI.view(w.ledger, run_id)["question"] is None
                        await w.orch.tick()
                        assert AI.view(w.ledger, run_id)["status"] == "resolved"
                        assert AC.for_run(w.ledger, run_id) is not None
                        assert w.native.transport.mutations == []
                        phase = "bound"
                    elif command["stage"] == "execute" and phase == "bound":
                        w.runtime()
                        await w.orch.reconcile()
                        await w.finish(run_id)
                        receipts = AC.read_receipts(w.ledger, run_id)
                        assert len(receipts) == 1 and receipts[0]["native"]["observed"]["confirmed"] is True
                        assert len(w.native.transport.mutations) == 1
                        w.runtime()
                        await w.orch.reconcile()
                        await w.orch.tick()
                        assert w.ledger.get_run(run_id).state == "SUCCEEDED"
                        assert AC.read_receipts(w.ledger, run_id) == receipts
                        assert len(w.native.transport.mutations) == 1
                        assert [c["kind"] for c in w.calls()] == ["interpret", "assessment"]
                        phase = "completed"
                await audit()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass


if __name__ == "__main__":
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="solvio-natural-browser-state-") as folder:
        with patch.dict(os.environ, {"SOLVIO_STATE_DIR": folder}):
            asyncio.run(main(output))
