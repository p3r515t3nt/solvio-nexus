"""Actual authenticated HTTPS admission of immutable offline file tasks.

Native Core/Router/grants and temporary stores. Only App Attest signing is
synthetic; no provider or file-processing program runs. Admission is not a
claim of processed output or a completed user objective.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal, require_raises
enforce_assertions()
import test_agent_task_entry as E
from solvio.agent_runtime import file_inputs as F, document_contract as DC, store as S
from solvio.agent_runtime.task_start_service import AuthorizedTaskStart, TaskStartService
from solvio.agent_runtime.task_authority import TaskAuthority, CapabilityGrant
from solvio.agent_runtime.costs import CostLedger

DATA = b"month,amount\nJan,3\nFeb,5\nMar,7\n"


def body(content=DATA, request_id="file-request-001", name="amounts.csv"):
    return dict(E.BODY, objective="Werte die beigefuegte Tabelle aus und erstelle Diagramm und Bericht.",
        client_request_id=request_id, file_request={"operation":"process_files",
            "files":[{"name":name,"content_b64":base64.b64encode(content).decode()}]})


async def accepted(w, request=None):
    response = await w.start(request or body())
    data = await response.json()
    require_equal(response.status, 201, str(data))
    return data


def input_path(w, run_id):
    return Path(next(a.path for a in w.ledger.artifacts_for_run(run_id) if a.kind == "task_file_input"))


def read(ledger, run_id):
    return F.read_for_run(ledger, run_id).files[0].content


def fresh(w):
    ledger = S.AgentRunLedger(w.ledger.path)
    return TaskStartService(ledger, grants=TaskAuthority(ledger), costs=CostLedger(ledger), router=w.router)


async def t_parallel_https_accepts_exactly_one_immutable_file_grant_without_effects():
    async with E.world() as w:
        responses = await asyncio.gather(w.start(body()), w.start(body()))
        require_equal([r.status for r in responses], [201,201])
        results = [await r.json() for r in responses]
        require_equal(results[0]["run_id"], results[1]["run_id"])
        run_id = results[0]["run_id"]
        require_equal(len(w.ledger.recent_runs()), 1)
        bound = F.for_run(w.ledger, run_id)
        grant = w.orch.task_authority.for_run(run_id)
        require_equal([(c.name,c.version,c.constraints) for c in grant.capabilities],
            [(F.CAPABILITY,F.VERSION,bound.arguments)])
        require_equal(bound.grant_reference, grant.reference)
        require_equal(read(S.AgentRunLedger(w.ledger.path), run_id), DATA)
        require_equal(input_path(w, run_id).stat().st_mode & 0o777, 0o400)
        require_equal(await w.store.list_pending(), [])
        require_equal(w.ledger.get_run(run_id).planner_calls, 0)
        require_equal(w.ledger.get_run(run_id).state, S.CREATED)
        require(F.CAPABILITY not in w.router.names(), "admission installed an executor")
        with w.ledger._open() as connection:
            dump = "\n".join(connection.iterdump())
        require(DATA.decode() not in dump and body()["file_request"]["files"][0]["content_b64"] not in dump)


async def t_replay_cannot_change_remove_add_rename_or_reorder_bound_files():
    async with E.world() as w:
        original_body = body()
        original_body["file_request"]["files"].append({"name":"context.txt", "content_b64":"aGVsbG8="})
        original = await accepted(w, original_body)
        changed = json.loads(json.dumps(original_body))
        changed["file_request"]["files"].reverse()
        requests = [changed, body(DATA+b"Apr,9\n"), body(name="renamed.csv"),
            {k:v for k,v in body().items() if k != "file_request"}, body()]
        for request in requests:
            response = await w.start(request)
            require_equal(response.status, 409, str(await response.json()))
        require_equal(read(w.ledger, original["run_id"]), DATA)
        require_equal(len(F.read_for_run(w.ledger, original["run_id"]).files), 2)
        ordinary = dict(E.BODY, client_request_id="ordinary-file-001")
        require_equal((await w.start(ordinary)).status, 201)
        require_equal((await w.start(dict(ordinary, file_request=body()["file_request"]))).status, 409)


async def t_closed_wire_xor_scope_names_and_decoded_size_limits():
    async with E.world() as w:
        invalid = [dict(body(),scope="build"), dict(body(),file_request=None),
            dict(body(),document_request={"operation":"extract_text","format":"txt","content_b64":"aGk="})]
        for fields in ({"operation":"execute"}, {"path":"/tmp/input"}, {"sha256":"0"*64}):
            invalid.append(dict(body(),file_request=dict(body()["file_request"], **fields)))
        for fields in ({"path":"/tmp/source"}, {"content_b64":"%%%"}, {"name":"../amounts.csv"},
                       {"name":"/amounts.csv"}, {"content_b64":""}):
            invalid.append(dict(body(),file_request={"operation":"process_files",
                "files":[dict(body()["file_request"]["files"][0], **fields)]}))
        duplicate = dict(body(),file_request={"operation":"process_files", "files":[
            {"name":"amounts.csv","content_b64":"eA=="}, {"name":"AMOUNTS.csv","content_b64":"eA=="}]})
        invalid.append(duplicate)
        for request in invalid:
            response = await w.start(request)
            require_equal(response.status, 400, str(await response.json()))
        require_equal(w.ledger.recent_runs(), [])
        maximum = b"x"*F.MAX_TOTAL_BYTES
        result = await accepted(w, body(maximum))
        require_equal(len(read(w.ledger, result["run_id"])), F.MAX_TOTAL_BYTES)
        too_large = body(maximum,request_id="too-large-file-001")
        too_large["file_request"]["files"].append({"name":"extra.txt","content_b64":"eA=="})
        require_equal((await w.start(too_large)).status, 400)
        envelope = await w.client.post('/v1/agent/tasks', data=b' '* (12*1024*1024),
            headers=dict(w.headers, **{'Content-Type':'application/json'}))
        require_equal(envelope.status, 413)


async def t_csrf_and_transport_only_leave_no_task_or_input():
    async with E.world() as w:
        for headers in ({}, {"Origin":w.origin}, dict(w.headers, Origin="https://foreign.example")):
            response = await w.client.post('/v1/agent/tasks', json={"task":body()}, headers=headers)
            require_equal(response.status, 401)
        require_equal(w.ledger.recent_runs(), [])
        require(not Path(S.artifact_root()).exists())
        require_equal(await w.store.list_pending(), [])


async def t_app_attest_signs_file_name_and_actual_bytes_and_retry_needs_fresh_proof():
    async with E.world() as w:
        device = await E.H.enroll_attested(w.cp,transport_cred="synthetic-file-transport")
        client = await w.new_client()
        headers = {"X-Device-Id":device.device_id,"X-Transport-Cred":"synthetic-file-transport"}
        require_equal((await client.post('/v1/agent/tasks',json={"task":body()},headers=headers)).status,401)
        async def signed(request, counter):
            response = await client.post('/v1/agent/tasks/challenge',json={"task":request},headers=headers)
            require_equal(response.status,200)
            wire = await response.json()
            assertion = E.AA.fake_assertion(device.aakey,
                E.T.client_data_hash(base64.b64decode(wire['binding_b64'])),counter)
            return {'task':request,'proof':{'nonce':wire['nonce'],
                'assertion_b64':base64.b64encode(assertion).decode()}}
        for request in (body(DATA+b'Apr,1\n'),body(name='changed.csv')):
            payload = await signed(body(),1)
            payload['task']=request
            require_equal((await client.post('/v1/agent/tasks',json=payload,headers=headers)).status,401)
        require_equal(w.ledger.recent_runs(),[])
        payload = await signed(body(),1)
        response = await client.post('/v1/agent/tasks',json=payload,headers=headers)
        require_equal(response.status,201,str(await response.json()))
        original = await response.json()
        require_equal(read(w.ledger,original['run_id']),DATA)
        require_equal(w.orch.task_authority.for_run(original['run_id']).receipt_method,'app_session')
        require_equal((await client.post('/v1/agent/tasks',json=payload,headers=headers)).status,401)
        retry = await signed(body(),2)
        response = await client.post('/v1/agent/tasks',json=retry,headers=headers)
        require_equal(response.status,201,str(await response.json()))
        require_equal((await response.json())['run_id'],original['run_id'])
        require_equal(len(w.ledger.recent_runs()),1)


async def t_process_loss_after_acceptance_before_grant_resumes_same_files_and_task():
    async with E.world() as w:
        with patch.object(w.orch.task_authority,'issue',side_effect=RuntimeError('synthetic crash')):
            response = await w.start(body())
            require_equal(response.status,202)
            result = await response.json()
        run_id = result['run_id']
        require_equal(input_path(w,run_id).read_bytes(),DATA)
        require(w.orch.task_authority.for_run(run_id) is None)
        service = fresh(w)
        require(service.finish(run_id))
        reference = F.for_run(service.ledger,run_id).grant_reference
        retry = await accepted(w)
        require_equal(retry['run_id'],run_id)
        require_equal(F.for_run(w.ledger,run_id).grant_reference,reference)
        require_equal(read(w.ledger,run_id),DATA)


async def t_precommit_file_record_failure_rolls_back_all_accepted_state():
    async with E.world() as w:
        with patch.object(F,'record_prepared',side_effect=RuntimeError('synthetic precommit failure')):
            response = await w.start(body())
            require(response.status >= 400)
        require_equal(w.ledger.recent_runs(),[])
        with w.ledger._open() as connection:
            for table in ('agent_task_sources','agent_task_grants','agent_artifacts'):
                require_equal(connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        result = await accepted(w)
        require_equal(read(w.ledger,result['run_id']),DATA)


async def t_missing_input_before_or_during_grant_issue_never_grants_or_marks_ready():
    for moment in ('before_issue','after_issue'):
        async with E.world() as w:
            issue = w.orch.task_authority.issue
            def lose(task_id,run_id,**kwargs):
                if moment == 'before_issue': input_path(w,run_id).unlink()
                result = issue(task_id,run_id,**kwargs)
                if moment == 'after_issue': input_path(w,run_id).unlink()
                return result
            with patch.object(w.orch.task_authority,'issue',side_effect=lose):
                response = await w.start(body())
                require_equal(response.status,202)
                run_id = (await response.json())['run_id']
            require(not w.orch.task_starts.ready(run_id))
            if moment == 'before_issue': require(w.orch.task_authority.for_run(run_id) is None)
            service = fresh(w)
            require_raises((ValueError,OSError),service.finish,run_id)
            require(not service.ready(run_id))


async def t_actual_grant_transaction_rejects_changed_original_objective_and_removed_capability():
    for change in ('objective','capability','receipt'):
        async with E.world() as w:
            with patch.object(w.orch.task_authority,'issue',side_effect=RuntimeError('synthetic crash')):
                response=await w.start(body())
                require_equal(response.status,202)
                run_id=(await response.json())['run_id']
            service=fresh(w)
            with service.ledger._open() as connection:
                row=connection.execute('SELECT * FROM agent_task_sources WHERE run_id=?',(run_id,)).fetchone()
                if change == 'objective':
                    connection.execute('UPDATE agent_tasks SET objective=? WHERE task_id=?',
                        ('Ein anderer Dateiauftrag mit veraendertem Ziel.',row['task_id']))
            entries=tuple(CapabilityGrant(**c) for c in json.loads(row['capabilities']))
            receipt=E.VerifiedTaskReceipt(row['receipt_method'],row['receipt_reference'],row['authorizer'])
            if change == 'capability': entries=()
            if change == 'receipt': receipt=replace(receipt,reference='other:receipt')
            require_raises(ValueError,service.grants.issue,row['task_id'],run_id,receipt=receipt,capabilities=entries)
            require(service.grants.for_run(run_id) is None)


async def t_bound_reader_rejects_tampered_bytes_paths_permissions_and_links():
    for change in ('bytes','path','permissions','symlink','hardlink'):
        async with E.world() as w:
            run_id=(await accepted(w))['run_id']
            path=input_path(w,run_id)
            if change == 'bytes':
                path.chmod(0o600); path.write_bytes(DATA.replace(b'Jan',b'Feb')); path.chmod(0o400)
            elif change == 'path':
                with w.ledger._open() as connection:
                    connection.execute("UPDATE agent_artifacts SET path='/not/a/file' WHERE run_id=? AND kind='task_file_input'",(run_id,))
            elif change == 'permissions': path.chmod(0o644)
            elif change == 'symlink':
                original=path.with_name('redirect.bin');path.rename(original);path.symlink_to(original)
            else: os.link(path,path.with_name('copy.bin'))
            require_raises((ValueError,OSError),F.read_for_run,w.ledger,run_id)


async def t_other_run_arguments_and_revocation_do_not_authorize_file_read():
    async with E.world() as w:
        first=(await accepted(w))['run_id']
        second=(await accepted(w,body(request_id='file-request-002')))['run_id']
        bound=F.for_run(w.ledger,first)
        require_raises(ValueError,F.read_for_run,w.ledger,second,arguments=bound.arguments)
        require_raises(ValueError,F.read_for_run,w.ledger,first,arguments=dict(bound.arguments,path='/tmp/foreign'))
        w.orch.task_authority.revoke(bound.grant_reference,'test:owner-revocation')
        require_raises(ValueError,F.read_for_run,w.ledger,first)


async def t_core_receipt_binds_files_without_exposing_model_argument_or_document_xor_bypass():
    async with E.world() as w:
        result=await accepted(w)
        grant=w.orch.task_authority.for_run(result['run_id'])
        receipt=E.VerifiedTaskReceipt(grant.receipt_method,grant.receipt_reference,grant.authorizer)
        arguments={'objective':body()['objective']}
        bound=AuthorizedTaskStart.bind(receipt=receipt,request_id='binding-file-001',
            capability='agent_task_research',arguments=arguments,file_request=body()['file_request'])
        require(bound.matches('agent_task_research',arguments,'local-owner',E.OriginClass.TRUSTED_DASHBOARD))
        changed=replace(bound,file_request=F.FileTaskRequest((F.FileInput('changed.csv',DATA),)))
        require(not changed.matches('agent_task_research',arguments,'local-owner',E.OriginClass.TRUSTED_DASHBOARD))
        xor=replace(bound,document_request=DC.DocumentRequest(b'{\\rtf1\\ansi hi}'))
        require(not xor.matches('agent_task_research',arguments,'local-owner',E.OriginClass.TRUSTED_DASHBOARD))
        require(DATA.decode() not in repr(bound))
        require('file_request' not in E.SPECS['agent_task_research'].input_schema['properties'])
        require_equal((await w.router.execute('agent_task_research',dict(arguments,file_request=body()['file_request']),
            trust=E.OWNER,origin=E.OriginClass.TRUSTED_DASHBOARD,principal='local-owner')).outcome,E.OUT.INVALID_INPUT)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
