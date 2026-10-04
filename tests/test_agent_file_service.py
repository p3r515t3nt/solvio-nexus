"""Native file service, independent readback and the actual HTTPS file gateway.

Temporary stores and a locally authored synthetic implementation, published by
the existing Git/Autopilot seam. No provider, user document or external service.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','src'))
sys.path.insert(0,os.path.dirname(__file__))
from _guard import enforce_assertions,require,require_equal
enforce_assertions()
import test_agent_task_entry as E
import test_agent_file_task_entry as INPUT
import test_agent_extension_activation as A
from test_agent_file_tool_process import runtime
from solvio.agent_runtime import store as S, file_inputs as FI, file_results as FR
from solvio.agent_runtime import result_files as RF, table_report_contract as TC, requirements as RQ
from solvio.agent_runtime import steps
from solvio.agent_runtime.extension_activation import ExtensionActivation
from solvio.agent_runtime.task_start_service import TaskStepAuthority
from solvio.capabilities.file_adapter import TaskFileService, SPEC
from solvio.autopilot.store import AutopilotLedger
from solvio.autopilot.contract import Contract
from solvio.autopilot.publisher import CheckpointPublisher


def refused(call,reason=None):
    try:
        call()
    except (ValueError,OSError) as exc:
        if reason: require(reason in str(exc),str(exc))
    else:
        raise AssertionError('unbound file evidence was accepted')


@asynccontextmanager
async def world(*, requirement_entries=None, input_request=None, minimum_sources=0):
    from _table_report_adapter import SOURCE
    async with E.world() as w:
        accepted=await INPUT.accepted(w,input_request)
        w.run=w.ledger.get_run(accepted['run_id'])
        w.root=Path(S.state_dir()).resolve()
        w.development=AutopilotLedger(str(w.root/'development.db'))
        w.milestone='native-table-service'
        w.development.create_milestone(Contract(w.milestone,'1.0.0','Synthetic table report tool.',()))
        w.ledger.set_run_fields(w.run.run_id,development_ref=w.milestone)
        w.canonical,w.clone=w.root/'canonical',w.root/'clone'
        w.canonical.mkdir()
        A.git(w.canonical,'init','-q','-b','main')
        (w.canonical/'README.md').write_text('Temporary native table service fixture.\n')
        A.git(w.canonical,'add','.')
        A.git(w.canonical,'commit','-qm','seed')
        A.git(w.root,'clone','-q','--no-hardlinks',str(w.canonical),str(w.clone))
        w.publisher=CheckpointPublisher(str(w.canonical))
        w.activation=ExtensionActivation(w.ledger,development=w.development,
            publisher=w.publisher,file_runtime=runtime())
        w.ordinal=0
        try:
            candidate=await w.activation.prepare(w.run.run_id,**A.World.publish(w,SOURCE))
            activated=await w.activation.activate(w.run.run_id,candidate)
            require(activated.ok,activated.reason)
            w.service=TaskFileService(w.ledger,w.activation)
            w.router.register(SPEC,w.service)
            task=w.ledger.get_task(w.run.task_id)
            bound=RQ.validate({'auskunft':[{'id':'a1','text':'Die Tabellen auswerten.'}],
                'handlungen':requirement_entries if requirement_entries is not None else [{'id':'h1','text':'Excel, Diagramm und PDF bereitstellen.'}],
                'unklar':[],'belege':{'mindestens':minimum_sources}},objective=task.objective)
            require(w.ledger.bind_requirements(task.task_id,json.dumps(bound)))
            w.ledger.transition(w.run.run_id,S.PLANNING)
            w.ledger.transition(w.run.run_id,S.RUNNING)
            w.run=w.ledger.get_run(w.run.run_id)
            w.step=w.ledger.create_step(run_id=w.run.run_id,seq=1,kind='capability',capability=FI.CAPABILITY)
            w.ledger.update_step(w.step.step_id,state='running',started=True)
            w.arguments=FI.for_run(w.ledger,w.run.run_id).arguments
            grant=w.orch.task_authority.for_run(w.run.run_id)
            w.binding=TaskStepAuthority(grant.reference,w.run.task_id,w.run.run_id,w.step.step_id)
            w.planned=SimpleNamespace(capability=FI.CAPABILITY,arguments=w.arguments,requirement='h1')
            yield w
        finally:
            w.development.close()


async def execute(w):
    return await steps.execute_capability(w.router,name=FI.CAPABILITY,arguments=w.arguments,
        sources={key:'core' for key in w.arguments},run_id=w.run.run_id,task_id=w.run.task_id,
        when='native-table-fixture',task_step=w.binding)


def record(w,data):
    return FR.record_result(w.ledger,w.activation,w.run,w.step,w.planned,data)


async def t_native_service_cost_readback_public_files_and_historical_restore():
    async with world() as w:
        outcome=await execute(w)
        require_equal(outcome.state,'succeeded',str(outcome))
        require(type(outcome.data) is FR.VerifiedFileOutput)
        require_equal(RF.describe_files(w.ledger,w.run.run_id)[0],[],
            'service persisted before the original invocation settled')
        refused(lambda:record(w,dict(summary='plausible')),'file_result_binding_changed')
        changed=replace(outcome.data,resources_json=b'{}')
        refused(lambda:record(w,changed),'file_result_binding_changed')
        with w.ledger._open() as connection:
            cost=connection.execute("SELECT * FROM agent_provider_invocations WHERE phase='capability'").fetchone()
            require_equal(cost['state'],'finished')
            connection.execute("UPDATE agent_provider_invocations SET state='unknown' WHERE invocation_id=?",(cost['invocation_id'],))
        refused(lambda:record(w,outcome.data),'file_cost_receipt_unconfirmed')
        with w.ledger._open() as connection:
            connection.execute("UPDATE agent_provider_invocations SET state='finished' WHERE invocation_id=?",(cost['invocation_id'],))
        findings,refs=record(w,outcome.data)
        require(any(f.startswith('Core-Tabelleninhalt:') and '15' in f for f in findings))
        require(any(f.startswith('Core-Tabellenbeleg:') and 'eingebettete Diagramme' in f for f in findings))
        require_equal(w.ledger.get_step(w.step.step_id).state,'succeeded')
        require_equal(w.ledger.get_run(w.run.run_id).state,S.RUNNING,'files do not complete the objective')
        view=await (await w.client.get('/v1/agent/runs/'+w.run.run_id)).json()
        require_equal({f['name'] for f in view['dateien']},set(TC.OUTPUTS))
        for item in view['dateien']:
            response=await w.client.get(item['download_url'])
            require_equal(response.status,200)
            content=await response.read()
            require_equal(hashlib.sha256(content).hexdigest(),item['sha256'])
            if item['preview_url']:
                require_equal((await w.client.get(item['preview_url'])).status,200)
        private=next(a for a in w.ledger.artifacts_for_run(w.run.run_id) if a.kind=='file_work_receipt')
        require(private.artifact_id in refs)
        require_equal((await w.client.get('/v1/agent/runs/'+w.run.run_id+'/artifacts/'+private.artifact_id+'/download')).status,404)
        stranger=await w.new_client()
        require_equal((await stranger.get(view['dateien'][0]['download_url'])).status,401)
        await w.login(stranger,principal='foreign-owner')
        require_equal((await stranger.get(view['dateien'][0]['download_url'])).status,404)
        reopened=S.AgentRunLedger(w.ledger.path)
        w.orch.task_authority.revoke(w.binding.reference,'synthetic:task-finished')
        w.ledger.transition(w.run.run_id,S.FAILED,summary='Separate objective assessment is incomplete.')
        require_equal(list(dict.fromkeys(value for d in FR.completion_evidence(reopened,w.run.run_id)
            for value in (d.finding,d.evidence) if value)),findings)
        context=SimpleNamespace(findings=[],sources=[])
        FR.restore_context(reopened,w.run.run_id,context)
        FR.restore_context(reopened,w.run.run_id,context)
        require_equal(context.findings,findings)
        require_equal(context.sources,['Dateiquelle SHA-256 '+hashlib.sha256(INPUT.DATA).hexdigest()])
        output=next(a for a in reopened.artifacts_for_run(w.run.run_id) if a.kind=='result_file')
        Path(output.path).chmod(0o600)
        Path(output.path).write_bytes(b'changed result')
        Path(output.path).chmod(0o400)
        refused(lambda:FR.completion_evidence(reopened,w.run.run_id))
        require_equal((await w.client.get('/v1/agent/runs/'+w.run.run_id+'/artifacts/'+output.artifact_id+'/download')).status,404)
        require_equal(await w.store.list_pending(),[])
        require_equal(w.ledger.get_run(w.run.run_id).planner_calls,0)


async def t_owner_cancellation_after_native_checker_does_not_publish_or_claim_success():
    async with world() as w:
        native=TC.validate_output
        checked=0
        async def cancel_after_check(*args,**kwargs):
            nonlocal checked
            result=await native(*args,**kwargs)
            checked+=1
            response=await w.client.post('/v1/agent/runs/'+w.run.run_id+'/cancel',json={},headers=w.headers)
            require_equal(response.status,200)
            return result
        with patch.object(TC,'validate_output',cancel_after_check):
            outcome=await execute(w)
        require_equal(checked,1)
        require(outcome.state!='succeeded',str(outcome))
        require_equal(w.ledger.get_run(w.run.run_id).state,S.CANCELLED)
        require_equal(RF.describe_files(w.ledger,w.run.run_id)[0],[])
        require(not any(a.kind=='file_work_receipt' for a in w.ledger.artifacts_for_run(w.run.run_id)))
        require_equal(await w.store.list_pending(),[])


async def t_output_changed_during_publication_cannot_commit_succeeded_step():
    async with world() as w:
        outcome=await execute(w)
        require_equal(outcome.state,'succeeded',str(outcome))
        publish=RF.publish_file
        count=0
        def change_after_last(*args,**kwargs):
            nonlocal count
            result=publish(*args,**kwargs)
            count+=1
            if count==3:
                artifact=next(a for a in w.ledger.artifacts_for_run(w.run.run_id) if a.kind=='result_file')
                path=Path(artifact.path)
                path.chmod(0o600)
                path.write_bytes(b'changed during native publication')
                path.chmod(0o400)
            return result
        with patch.object(RF,'publish_file',change_after_last):
            refused(lambda:record(w,outcome.data))
        require_equal(count,3)
        require_equal(w.ledger.get_step(w.step.step_id).state,'running')
        require(not any(a.kind=='file_work_receipt' for a in w.ledger.artifacts_for_run(w.run.run_id)))
        require_equal(RF.describe_files(w.ledger,w.run.run_id)[0],[])


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
