"""Temporary HTTPS dashboard with one genuinely parked native account action.

Only the native service's HTTP transport is synthetic. Exactly three Core
ticks prepare the login boundary, then the fixture only reads state. Browser
account amendments must never execute a calendar write or a model call.
"""
import asyncio
from contextlib import AsyncExitStack
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from unittest.mock import AsyncMock, patch

root=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(root/'src'),str(root/'tests')]
from _guard import require,require_equal
from test_nexus_dashboard import world
from test_agent_action_services import native_fixture,calendar_action
from test_agent_action_account_public import rotate_fixture_access,immutable_rows
from test_document_native_formats import native_input
from solvio.agent_runtime import store as S,action_contract as AC
from solvio.capabilities.agent import AgentCapabilities,SPECS
from solvio.capabilities.task_action import TaskServiceAction,SPEC

output=Path(sys.argv[1]).resolve();output.mkdir(parents=True,exist_ok=True)


def save(name,value):
    staging=output/(name+'.part')
    staging.write_text(json.dumps(value,sort_keys=True,indent=2)+'\n')
    staging.replace(output/name)


async def main():
    with tempfile.TemporaryDirectory(prefix='solvio-dashboard-account-') as directory:
        async with AsyncExitStack() as stack:
            native=stack.enter_context(native_fixture(Path(directory)))
            Path(S.state_dir()).mkdir(parents=True,exist_ok=True)
            w=await stack.enter_async_context(world());w.native=native
            w.orch.require_task_authority=True
            w.router.register(SPECS['agent_task_action'],AgentCapabilities(w.orch).action)
            service=TaskServiceAction(w.ledger,calendar=native.calendar,gmail=native.gmail,
                ha=native.ha,exposure=native.exposure)
            w.router.register(SPEC,service);w.orch.action_service=service
            spies=[]
            for name in ('plan','assess_async','assess'):
                if callable(getattr(w.orch.planner,name,None)):
                    spy=AsyncMock(side_effect=AssertionError('browser fixture must not call a model'))
                    stack.enter_context(patch.object(w.orch.planner,name,spy));spies.append(spy)
            body={'scope':'action','objective':'Browserprobe: Diesen synthetischen Kalendertermin fortsetzen.',
                'target_repo':'','client_request_id':'browser-account-prepare',
                'action_request':{'actions':[calendar_action(native.calendar)]}}
            response=await w.start(body);require_equal(response.status,201,str(await response.text()))
            accepted=await response.json();run_id=accepted['run_id']
            native.calendar._access_token='';native.calendar._expires_at=0;native.transport.auth_error='invalid_grant'
            for _ in range(3):
                await w.orch.tick()
            require_equal(w.ledger.get_run(run_id).state,S.WAITING_USER)
            require_equal(native.transport.mutations,[])
            require_equal(native.transport.calls,[('POST','https://oauth2.googleapis.com/token')])
            require_equal(sum(spy.call_count for spy in spies),0)
            original=immutable_rows(w,run_id)
            old_receipt=AC.read_receipts(w.ledger,run_id)[0]
            rotate_fixture_access(w)
            view=await (await w.client.get('/v1/agent/runs/'+run_id)).json()
            binding=view['kontobindung'];require(binding['selection_required'])
            require_equal(len(binding['choices']),1)
            require(binding['current_account']!=binding['choices'][0]['account'])
            baseline={'run_id':run_id,'task_id':accepted['task_id'],'objective':body['objective'],
                'binding':binding,'state':S.WAITING_USER,'native_auth_transport_calls':1,
                'native_writes':0,'model_calls':0,'ticks':3}
            save('account-fixture.json',baseline)
            (output/'fixture-state-root.txt').write_text(S.state_dir())
            (output/'fixture-pid.txt').write_text(str(os.getpid()))
            (output/'dashboard-url.txt').write_text(w.origin+'/dashboard/')
            documents=[]
            for fmt in ('txt','docx','odt'):
                data=native_input(fmt);name='synthetic-document.'+fmt
                (output/name).write_bytes(data)
                documents.append({'format':fmt,'name':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
            save('native-documents.json',documents)
            with patch('solvio.security.mobile_approval.browser_sessions.secrets.token_urlsafe',
                       return_value='n5-test-only-'.ljust(43,'0')):
                await w.sessions.issue_enrollment(principal='local-owner')
            stop=asyncio.Event()
            for sig in (signal.SIGINT,signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(sig,stop.set)
            def observed():
                with w.ledger._open() as connection:
                    changes=connection.execute('SELECT count(*) FROM agent_action_account_rebindings').fetchone()[0]
                result={'run_id':run_id,'state':w.ledger.get_run(run_id).state,
                    'account_amendments':changes,'original_rows_unchanged':immutable_rows(w,run_id)==original,
                    'original_receipt_unchanged':AC.read_receipts(w.ledger,run_id)[0]==old_receipt,
                    'native_transport_calls':len(native.transport.calls),'native_writes':len(native.transport.mutations),
                    'model_calls':sum(spy.call_count for spy in spies),'ticks':3,'run_count':len(w.ledger.recent_runs())}
                save('account-observed.json',result)
                require_equal(result['native_transport_calls'],1)
                require_equal(result['native_writes'],0);require_equal(result['model_calls'],0)
            observed();print('PREVIEW '+w.origin+'/dashboard/',flush=True)
            try:
                while not stop.is_set() and not (output/'stop.fixture').exists():
                    observed()
                    try:
                        await asyncio.wait_for(stop.wait(),.1)
                    except asyncio.TimeoutError:
                        pass
            finally:
                observed();await w.orch.stop()


asyncio.run(main())
