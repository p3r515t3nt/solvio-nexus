"""A3 device browser fixture: real HTTPS and native HA reads, synthetic transport only.

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
from unittest.mock import AsyncMock, patch
from types import MethodType

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_nexus_dashboard import world
from test_agent_action_services import native_fixture
from test_agent_action_resources import change_credential, add
from test_agent_action_execution import ActionWorld
from test_agent_cost_runtime import _cli
from test_agent_runtime_subscription_flow import ForbiddenBroker
from solvio.specialists import providers as P
from solvio.agent_runtime import action_contract as AC
from solvio.capabilities.agent import AgentCapabilities, SPECS
from solvio.capabilities.task_action import TaskServiceAction, SPEC


async def main(output):
    async with AsyncExitStack() as stack:
        w = await stack.enter_async_context(world())
        # Enter after world() constructs its genuine aiohttp client. The native
        # fixture routes all prospective service traffic into a local transport;
        # the execution proof uses only this local transport.
        native = stack.enter_context(native_fixture())
        w.native = native
        change_credential(w)
        add(w, 'switch.fixture', name='Fixture switch')
        add(w, 'switch.garage', device_class='garage', name='Restricted garage')
        add(w, 'light.hidden', exposed=False, name='Hidden fixture')
        add(w, 'sensor.temperature', name='Read-only sensor')
        service = TaskServiceAction(w.ledger, ha=native.ha, exposure=native.exposure)
        w.router.register(SPEC, service)
        w.orch.action_service = service
        caps = AgentCapabilities(w.orch)
        w.router.register(SPECS["agent_task_action"], caps.action)
        # Use the same actual runtime as the canonical public action tests.
        # Only its model executable and native HA transport are synthetic.
        w.folder = Path(w.folder)
        w.executable = _cli(w.folder, w.ledger.path)
        w.broker = ForbiddenBroker()
        w.api = AsyncMock(side_effect=AssertionError('No paid API'))
        w.proactive = w.center.dispatcher.proactive_store
        w.runtime = MethodType(ActionWorld.runtime, w)
        w.assessment = MethodType(ActionWorld.assessment, w)
        w.finish = MethodType(ActionWorld.finish, w)
        stack.enter_context(patch.object(P, 'resolve', return_value=str(w.executable)))
        w.runtime()
        w.app['agent_runtime_provider'] = lambda: w.orch
        service = w.orch.action_service
        execution_checked = False
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
                with w.ledger._open() as connection:
                    row = connection.execute('SELECT * FROM agent_action_contracts WHERE run_id=?', (run.run_id,)).fetchone()
                    bound = AC._with_grant(connection, w.ledger, AC._bound_row(row), active=False)
                grant = w.orch.task_authority.for_run(run.run_id)
                runs.append({"task_id": task.task_id, "run_id": run.run_id,
                    "scope": task.scope, "origin": task.created_origin,
                    "principal": task.created_principal, "objective": task.objective,
                    "state": run.state, "planner_calls": run.planner_calls,
                    "actions": list(bound.actions), "resource_id": bound.resource_id,
                    "contract_digest": bound.contract_digest,
                    "grant_method": grant.receipt_method, "receipts": AC.read_receipts(w.ledger, run.run_id)})
            with w.ledger._open() as connection:
                counts = {name: connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                          for name in ("agent_action_contracts", "agent_action_claims")}
            value = {"state_root": str(w.folder), "services": service.accounts(),
                "runs": runs, "counts": counts,
                "pending_approvals": len(await w.store.list_pending()),
                "native_calls": list(native.transport.calls),
                "light_hidden": not native.transport.exposed["light.fixture"]["conversation"],
                "native_mutations": len(native.transport.mutations),
                "native_writes": list(native.transport.mutations), "execution_checked": execution_checked,
                "fixture_state": native.transport.states['light.fixture'],
                "provider_calls": [json.loads(line) for line in (w.folder/'calls.jsonl').read_text().splitlines()]
                    if (w.folder/'calls.jsonl').exists() else []}
            assert value["native_mutations"] <= 1
            assert all(url.startswith("http://127.0.0.1:8123/api/") for method, url in value["native_calls"])
            assert value["pending_approvals"] == 0
            assert counts["agent_action_claims"] <= 1
            assert w.broker.calls == [] and w.api.await_count == 0
            assert len(runs) <= 1
            temporary = output / "audit-next.json"
            temporary.write_text(json.dumps(value, indent=2))
            temporary.replace(output / "audit.json")

        await audit()
        (output / "dashboard-url.txt").write_text(w.origin + "/dashboard/")
        print("PREVIEW " + w.origin + "/dashboard/", flush=True)
        while not stop.is_set():
            control = output / 'fixture-control.json'
            if control.exists():
                instruction = json.loads(control.read_text())
                assert instruction == {'execute': True}
                if not execution_checked:
                    runs = w.ledger.recent_runs()
                    assert len(runs) == 1
                    run_id = runs[0].run_id
                    w.runtime()
                    await w.orch.reconcile()
                    await w.finish(run_id)
                    assert len(native.transport.mutations) == 1
                    receipts = AC.read_receipts(w.ledger,run_id)
                    assert len(receipts) == 1 and receipts[0]['status'] == 'completed'
                    assert receipts[0]['native']['observed']['confirmed'] is True
                    w.runtime()
                    await w.orch.reconcile()
                    await w.orch.tick()
                    assert w.ledger.get_run(run_id).state == 'SUCCEEDED'
                    assert AC.read_receipts(w.ledger,run_id) == receipts
                    assert len(native.transport.mutations) == 1
                    execution_checked = True
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
