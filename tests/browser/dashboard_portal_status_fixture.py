"""Actual HTTPS dashboard and existing owned Portal reader; synthetic page only.

No Portal open/login/execute operation, secret access, model or speech. This
fixture only admits one explicit status task; it never runs an agent tick.
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
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from test_nexus_dashboard import world
from test_agent_action_portal import portal_fixture
from solvio.agent_runtime import action_contract as AC
from solvio.capabilities.agent import AgentCapabilities, SPECS
from solvio.capabilities.task_action import TaskServiceAction, SPEC

async def main(output):
    async with AsyncExitStack() as stack:
        w = await stack.enter_async_context(world())
        p = await stack.enter_async_context(portal_fixture(owner='local-owner'))
        adapter = TaskServiceAction(w.ledger, portals=p.portals)
        w.router.register(SPEC, adapter); w.orch.action_service = adapter
        w.router.register(SPECS['agent_task_action'], AgentCapabilities(w.orch).action)
        with patch('solvio.security.mobile_approval.browser_sessions.secrets.token_urlsafe',
                   return_value='n8-portal-status-'.ljust(43, '0')):
            await w.sessions.issue_enrollment(principal='local-owner')
        stop = asyncio.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(signum, stop.set)
        async def audit():
            runs = []
            for run in w.ledger.recent_runs():
                task = w.ledger.get_task(run.task_id); bound = AC.for_run(w.ledger, run.run_id)
                grant = w.orch.task_authority.for_run(run.run_id)
                runs.append({'run_id':run.run_id, 'state':run.state, 'objective':task.objective,
                    'actions':list(bound.actions), 'receipt_method':grant.receipt_method,
                    'planner_calls':run.planner_calls})
            with w.ledger._open() as c:
                claims = c.execute('SELECT COUNT(*) FROM agent_action_claims').fetchone()[0]
            assert claims == 0 and all(r['state'] == 'CREATED' and r['planner_calls'] == 0 for r in runs)
            assert set(p.operations) <= {'ping', 'list_sessions'}
            assert all(a['service']=='portal' and a['operation']=='status' for r in runs for a in r['actions'])
            value={'runs':runs,'claims':claims,'portal_operations':p.operations,'cdp_read_count':len(p.cdp.calls),
                'pending_approvals':len(await w.store.list_pending()),'session_present':p.session.session_id in p.worker.sessions,
                'provider_calls':0,'login_calls':0,'native_mutations':0}
            tmp=output/'audit-next.json';tmp.write_text(json.dumps(value,indent=2));tmp.replace(output/'audit.json')
        await audit();(output/'dashboard-url.txt').write_text(w.origin+'/dashboard/')
        print('PREVIEW '+w.origin+'/dashboard/',flush=True)
        while not stop.is_set():
            control=output/'fixture-control.json'
            if control.exists():
                instruction=json.loads(control.read_text())
                assert set(instruction)=={'hide_session'} and type(instruction['hide_session']) is bool
                if instruction['hide_session']:p.worker.sessions.pop(p.session.session_id,None)
                else:p.worker.sessions[p.session.session_id]=p.session
            await audit()
            try:await asyncio.wait_for(stop.wait(),timeout=.1)
            except asyncio.TimeoutError:pass

if __name__=='__main__':
    output=Path(sys.argv[1]).resolve();output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='solvio-dashboard-portal-state-') as folder:
        with patch.dict(os.environ,{'SOLVIO_STATE_DIR':folder}):asyncio.run(main(output))
