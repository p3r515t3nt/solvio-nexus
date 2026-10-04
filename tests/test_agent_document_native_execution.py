"""Public new-format continuation using the actual offline native converter.

Only the existing local subscription-CLI fixture supplies model replies.
HTTPS, admission, durable grants, development, Git publication, Core gates,
offline processes, restart, completion receipts and downloads are real.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_agent_document_execution as T
import test_document_native_formats as N
from solvio.agent_runtime import document_formats as F, document_contract as DC
from solvio.agent_runtime.document_results import read_result, completion_evidence


def configure(w, fmt):
    root=Path(T.S.state_dir()).resolve()
    cli=root/'document-cli'
    source=cli.read_text()
    require("'rtf_text_v1'" in source)
    # Adapt only this temporary, generated CLI's canned replies to the new
    # explicit contract; retain its existing physical claim/gate assertions.
    cli.write_text(source.replace("'rtf_text_v1'",repr(DC.profile(fmt).contract))
        .replace('Core offline RTF gate:',f'Core offline {fmt.upper()} gate:'))
    fixture=root/'fixture.json'
    fixture.write_text(json.dumps({**json.loads(fixture.read_text()),'adapter':N.adapter(fmt)}))
    body=dict(T.body(),document_request=N.wire(fmt,F.fixture(fmt,'Actual original input.')))
    return body


async def t_public_three_native_formats_resume_after_detach_and_download_same_result():
    for fmt in ('txt','docx','odt'):
        async with T.world() as w:
            body=configure(w,fmt)
            response=await w.start(body)
            require_equal(response.status,201,str(await response.text()))
            accepted=await response.json();run_id=accepted['run_id']
            grant=w.orch.task_authority.for_run(run_id)
            waiting=await T.advance(w.orch,run_id,until=lambda run:run.state==T.S.WAITING_CAPABILITY)
            require_equal(waiting.state,T.S.WAITING_CAPABILITY,waiting.result_summary)
            require_equal([call['phase'] for call in w.calls()],['plan'])
            require_equal(w.ledger.steps_for_run(run_id)[0].kind,'capability_need')
            require_equal((await w.client.post(T.B.SESSION_PATH+'/logout',json={},headers=w.headers)).status,200)
            fresh=T.fresh_runtime(w);w.orch=fresh
            finished=await T.advance(fresh,run_id)
            require_equal(finished.state,T.S.SUCCEEDED,finished.result_summary)
            require_equal(finished.task_id,accepted['task_id'])
            require_equal(fresh.task_authority.for_run(run_id),grant)
            require_equal(len(w.ledger.recent_runs()),1)
            require_equal([call['phase'] for call in w.calls()],
                ['plan','extension_build','extension_review','plan','assessment'])
            for call in w.calls():
                if call['phase'].startswith('extension_'):
                    require('Actual original input.' not in call['prompt'])
            results=[item for item in w.ledger.artifacts_for_run(run_id) if item.kind=='document_result']
            require_equal(len(results),1)
            output=results[0]
            require_equal(read_result(w.ledger,run_id,output.artifact_id).strip(),b'Actual original input.')
            proof=next(item for item in w.ledger.artifacts_for_run(run_id) if item.kind=='document_receipt')
            receipt=json.loads(Path(proof.path).read_text())
            require_equal(receipt['contract_digest'],DC.profile(fmt).contract_digest)
            require_equal(receipt['grant_reference'],grant.reference)
            require_equal(len(completion_evidence(w.ledger,run_id)),1)
            client=await w.new_client();await w.login(client)
            url=f'/v1/agent/runs/{run_id}/artifacts/{output.artifact_id}/download'
            downloaded=await client.get(url)
            require_equal(downloaded.status,200)
            require_equal(await downloaded.read(),Path(output.path).read_bytes())
            before=len(w.calls())
            w.orch=T.fresh_runtime(w);await w.orch.tick()
            require_equal((await client.get(url)).status,200)
            require_equal(len(w.calls()),before,'read/restart reran the conversion')
            # A historical download needs its signed source/result identity,
            # not a currently usable adapter environment or active authority.
            w.orch.task_authority.revoke(grant.reference,'fixture:completed-read-only')
            with patch('solvio.agent_runtime.extension_activation.environment_fingerprint',
                       side_effect=AssertionError('download must not run an activation probe')):
                require_equal((await client.get(url)).status,200)
                require_equal(read_result(w.ledger,run_id,output.artifact_id),Path(output.path).read_bytes())
            require_equal(len(w.calls()),before)
            require_equal(await w.store.list_pending(),[])


async def t_public_native_failed_gate_repairs_original_task_before_one_result():
    async with T.world(repair_first=True) as w:
        accepted=await (await w.start(configure(w,'odt'))).json()
        finished=await T.advance(w.orch,accepted['run_id'])
        require_equal(finished.state,T.S.SUCCEEDED,finished.result_summary)
        reviews=[call for call in w.calls() if call['phase']=='extension_review']
        require_equal([call['gate'][0] for call in reviews],[0,1])
        require_equal(reviews[0]['selected'],None)
        require_equal(len(w.ledger.recent_runs()),1)
        results=[a for a in w.ledger.artifacts_for_run(finished.run_id) if a.kind=='document_result']
        require_equal(len(results),1)
        require_equal(read_result(w.ledger,finished.run_id,results[0].artifact_id).strip(),b'Actual original input.')
        require_equal(await w.store.list_pending(),[])


class ProcessLoss(BaseException):
    pass


async def t_new_format_committed_receipt_survives_lost_checkpoint_without_second_converter():
    async with T.world() as w:
        accepted=await (await w.start(configure(w,'docx'))).json();run_id=accepted['run_id']
        original=w.ledger.update_step
        def crash(*args,**kwargs):
            result=original(*args,**kwargs)
            if kwargs.get('outcome_reason')=='document_text_verified':
                raise ProcessLoss()
            return result
        with patch.object(w.ledger,'update_step',crash):
            try:
                await T.advance(w.orch,run_id)
            except ProcessLoss:
                pass
            else:
                raise AssertionError('durable-result crash was not reached')
        before=len(w.calls());w.orch=T.fresh_runtime(w)
        await w.orch.reconcile()
        finished=await T.advance(w.orch,run_id)
        require_equal(finished.state,T.S.SUCCEEDED,finished.result_summary)
        require_equal([call['phase'] for call in w.calls()[before:]],['assessment'])
        steps=[step for step in w.ledger.steps_for_run(run_id) if step.capability==DC.CAPABILITY and step.kind=='capability']
        require_equal(len(steps),1);require_equal(steps[0].state,'succeeded')
        require_equal(len(completion_evidence(w.ledger,run_id)),1)


async def t_public_office_container_larger_than_old_rtf_limit_uses_bound_bytes():
    async with T.world() as w:
        body=configure(w,'docx')
        data=N.rewrite(F.fixture('docx','Actual original input.'), additions=[
            ('word/media/padding.bin',hashlib.shake_256(b'public-format-boundary').digest(120000))])
        require(len(data)>DC.MAX_BYTES)
        body['document_request']=N.wire('docx',data)
        response=await w.start(body)
        require_equal(response.status,201,str(await response.text()))
        accepted=await response.json();run_id=accepted['run_id']
        bound=DC.for_run(w.ledger,run_id)
        require_equal(bound.source_bytes,len(data))
        require_equal(DC.read_for_run(w.ledger,run_id),data)
        completed=await T.advance(w.orch,run_id)
        require_equal(completed.state,T.S.SUCCEEDED,completed.result_summary)
        require_equal(len(completion_evidence(w.ledger,run_id)),1)


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
