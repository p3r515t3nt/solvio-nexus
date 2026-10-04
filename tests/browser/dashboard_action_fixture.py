"""A2 browser fixture: real temporary HTTPS admission, no native dispatch.

Run with the existing work/python-core and an output directory. The companion
browser check uses the public dashboard; audit.json is read from the actual
ledger, never manufactured by a browser response stub.
"""
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_nexus_dashboard import world
from test_agent_action_services import native_fixture
from solvio.agent_runtime import action_contract as AC
from solvio.capabilities.agent import AgentCapabilities, SPECS
from solvio.capabilities.task_action import TaskServiceAction, SPEC


async def main(output):
    async with AsyncExitStack() as stack:
        w = await stack.enter_async_context(world())
        # Enter after world() constructs its genuine aiohttp client. The native
        # fixture routes all prospective service traffic into a local transport;
        # this proof additionally requires that transport to remain unused.
        native = stack.enter_context(native_fixture())
        service = TaskServiceAction(w.ledger, calendar=native.calendar, gmail=native.gmail)
        w.router.register(SPEC, service)
        w.orch.action_service = service
        caps = AgentCapabilities(w.orch)
        w.router.register(SPECS["agent_task_action"], caps.action)
        assert Path(w.ledger.path).is_relative_to(Path(w.folder))
        assert Path(w.folder).is_relative_to(Path(tempfile.gettempdir()))
        assert w.ledger.recent_runs() == []
        for prefix in ("n5-test-only-", "n8-action-relogin-"):
            with patch("solvio.security.mobile_approval.browser_sessions.secrets.token_urlsafe",
                       return_value=prefix.ljust(43, "0")):
                await w.sessions.issue_enrollment(principal="local-owner")

        stop = asyncio.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(signum, stop.set)

        async def audit():
            runs = []
            for run in w.ledger.recent_runs():
                task = w.ledger.get_task(run.task_id)
                bound = AC.for_run(w.ledger, run.run_id)
                grant = w.orch.task_authority.for_run(run.run_id)
                runs.append({"task_id": task.task_id, "run_id": run.run_id,
                    "scope": task.scope, "origin": task.created_origin,
                    "principal": task.created_principal, "objective": task.objective,
                    "state": run.state, "planner_calls": run.planner_calls,
                    "actions": list(bound.actions), "resource_id": bound.resource_id,
                    "contract_digest": bound.contract_digest,
                    "grant_method": grant.receipt_method})
            with w.ledger._open() as connection:
                counts = {name: connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                          for name in ("agent_action_contracts", "agent_action_claims")}
            value = {"state_root": w.folder, "services": service.accounts(),
                "runs": runs, "counts": counts,
                "pending_approvals": len(await w.store.list_pending()),
                "native_calls": len(native.transport.calls),
                "native_mutations": len(native.transport.mutations)}
            assert value["native_calls"] == value["native_mutations"] == 0
            assert value["pending_approvals"] == 0
            assert counts["agent_action_claims"] == 0
            assert all(run["state"] == "CREATED" and run["planner_calls"] == 0 for run in runs)
            temporary = output / "audit-next.json"
            temporary.write_text(json.dumps(value, indent=2))
            temporary.replace(output / "audit.json")

        await audit()
        (output / "dashboard-url.txt").write_text(w.origin + "/dashboard/")
        print("PREVIEW " + w.origin + "/dashboard/", flush=True)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass
            await audit()


if __name__ == "__main__":
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="solvio-dashboard-action-state-") as folder:
        with patch.dict(os.environ, {"SOLVIO_STATE_DIR": folder}):
            asyncio.run(main(output))
