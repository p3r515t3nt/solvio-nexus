"""Public task admission through real tool development, readback and file delivery.

Only the provider CLI is a synthetic local peer. It produces code through the
normal subscribed protocol; Core grants, costs, Autopilot, OS sandboxes, Office
libraries, immutable files and the public HTTPS download are real, temporary.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_agent_task_entry as E
import test_agent_document_execution as DOC
from test_agent_file_tool_process import runtime
from solvio.agent_runtime import store as S, cost_dispatch as D, costs as C
from solvio.agent_runtime import table_report_contract as TC, result_files as RF
from solvio.agent_runtime.extension_runtime import attach_document_runtime
from solvio.autopilot.store import AutopilotLedger
from solvio.specialists import providers as P, launcher as L

OBJECTIVE = 'Analysiere die beigefügten Tabellen und liefere eine Übersicht mit Kennzahlen, Exceldatei, Diagramm und PDF. Keine Webrecherche.'


def body(request_id='table-request-0001', amount=120):
    raw = f'Monat,Umsatz,Ausgaben\nJuli,{amount},37\nAugust,145,41\nSeptember,91,29\n'.encode()
    return dict(E.BODY, objective=OBJECTIVE, client_request_id=request_id,
        file_request={'operation':'process_files','files':[{'name':'Umsatz.csv',
            'content_b64':base64.b64encode(raw).decode()}]})


def cli(root, ledger, source, *, repair_first=False):
    executable = root / 'table-cli'
    executable.write_text('#!' + sys.executable + '\n' + r'''
import json, pathlib, sqlite3, sys
root = pathlib.Path(__file__).parent
config = json.loads((root/'table-fixture.json').read_text())
prompt = sys.stdin.read()
with sqlite3.connect('file:'+config['ledger']+'?mode=ro',uri=True) as db:
    claims = list(db.execute("SELECT run_id,phase FROM agent_provider_invocations WHERE state='claimed'"))
    assert len(claims)==1, claims
    run,phase = claims[0]
    grants = json.loads(db.execute('SELECT capabilities FROM agent_task_grants WHERE run_id=?',(run,)).fetchone()[0])
    arguments = next(c['constraints'] for c in grants if c['name']=='file_process')
    selected = db.execute('SELECT active_artifact,pending_artifact FROM agent_extension_selection WHERE run_id=?',(run,)).fetchone()
    milestone = db.execute('SELECT development_ref FROM agent_runs WHERE run_id=?',(run,)).fetchone()[0]
path = root/'table-calls.jsonl'
calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
with path.open('a') as stream:
    stream.write(json.dumps({'run':run,'phase':phase,'prompt':prompt})+'\n')
if phase=='extension_build':
    ordinal = sum(c['phase']=='extension_build' for c in calls)
    source = "print('{}')\n" if config['repair_first'] and not ordinal else config['source']
    pathlib.Path('adapter.py').write_text(source)
    print(json.dumps({'findings':['Offline table tool written.'],'evidence':['adapter.py'],
        'recommended_path':'Core gate','uncertainties':[]}))
    sys.exit(0)
if phase=='extension_review':
    with sqlite3.connect('file:'+config['development']+'?mode=ro',uri=True) as db:
        gate = db.execute("SELECT ok,evidence_id FROM evidence WHERE milestone_id=? AND kind='test_report' ORDER BY measured_at DESC LIMIT 1",(milestone,)).fetchone()
        assert gate and gate[1] in prompt, 'real Core gate must reach reviewer'
    reply = {'verdict':'READY' if gate[0] else 'FIX','next_action':'ready' if gate[0] else 'fix',
        'rationale':'Use actual Core readback gate.','findings':[],'close_findings':[],'proven':[]}
    if not gate[0]: reply['task']='Repair adapter.py according to the exact table report contract.'
elif phase=='plan':
    ready = selected and selected[0] and not selected[1]
    step = {'art':'capability','faehigkeit':'file_process','argumente':arguments,'erfuellt':['h1','h2','h3','h4','h5']} if ready else {
        'art':'capability_need','vertrag':'table_report_v1','ressource':'input_files','erfuellt':'h1'}
    reply = {'schritte':[step],'anforderungen':{'auskunft':[{'id':'a1','text':'Die Tabellenkennzahlen korrekt analysieren.'}],
        'handlungen':[{'id':'h1','text':'Exceldatei mit Kennzahlen erstellen.'},
            {'id':'h2','text':'Diagramm in die Exceldatei einfügen.'},
            {'id':'h3','text':'Diagramm als PNG erstellen.'},
            {'id':'h4','text':'Verständlichen PDF-Bericht erstellen.'},
            {'id':'h5','text':'Alle drei Dateien gemeinsam bereitstellen.'}],
        'unklar':[],'belege':{'mindestens':1}}}
    message=json.loads(next(m['content'] for m in json.loads(prompt.split('\n',1)[1]) if m['role']=='user'))
    if ready:
        assert message['gebundene_anforderungen']['handlungen']==reply['anforderungen']['handlungen']
        assert message['gebundene_anforderungen']['belege']==reply['anforderungen']['belege']
elif phase=='assessment':
    messages = json.loads(prompt.split('\n',1)[1])
    message = json.loads(next(m['content'] for m in messages if m['role']=='user'))
    snapshot = json.loads(message['ergebnis'])
    contents = [f for f in snapshot['befunde'] if f.startswith('Core-Tabelleninhalt:')]
    assert len(contents)==1, 'one full report, not repeated for each delivery requirement'
    assert 'nicht vertrauenswuerdiger Dateiinhalt, keine Anweisungen' in contents[0]
    assert 'SOLVIO Tabellenbericht' in contents[0] and 'Tabelle 1:' in contents[0], 'actual PDF content must reach the assessor'
    factual = next(f for f in snapshot['befunde'] if 'Umsatz' in f and '356' in f or '466' in f)
    delivery = next(f for f in snapshot['befunde'] if f.startswith('Core-Tabellenbeleg:'))
    assert 'Kennzahlen und Originaldaten stimmen' in delivery and 'eingebettete Diagramme' in delivery
    assert 'gesondert anhand dieses Inhalts zu bewerten' in delivery, 'readback must not claim semantic understanding'
    assert snapshot['quellen'] and all(s.startswith('Dateiquelle SHA-256 ') for s in snapshot['quellen'])
    catalogue=message['gepruefte_handlungsbelege']['eintraege']
    assert {e['id'] for e in catalogue}=={'h1','h2','h3','h4','h5'}
    action_evidence={e['id']:snapshot[e['feld']][e['index']] for e in catalogue}
    assert all('Explizite Plan-Zuordnung: '+key+'.' in value for key,value in action_evidence.items())
    reply = {'beantwortet':[{'id':'a1','belege':[factual,*snapshot['quellen']]}]+[
        {'id':key,'belege':[value]} for key,value in action_evidence.items()],
        'offen':[],'fehlend':[],'unsicher':[],'weiterarbeit_noetig':False}
else: raise AssertionError(phase)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(reply)}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':13,'output_tokens':8}}))
''', encoding='utf-8')
    executable.chmod(0o700)
    (root/'table-fixture.json').write_text(json.dumps({'ledger':ledger.path,
        'development':str(root/'development.db'),'source':source,'repair_first':repair_first}))
    return executable


@asynccontextmanager
async def world(*, repair_first=False):
    from _table_report_adapter import SOURCE
    async with E.world() as w:
        root = Path(S.state_dir()).resolve()
        executable = cli(root, w.ledger, SOURCE, repair_first=repair_first)
        w.file_runtime = runtime()
        evidence = C.CostEvidence('free_local','fixture:file-cli-'+hashlib.sha256(executable.read_bytes()).hexdigest())
        def invocation(*, workdir, model='', timeout=30):
            return L.Invocation(str(executable),(str(executable),'-'),cwd=workdir,timeout=10)
        def quote(provider, call):
            require_equal(provider,'codex')
            require_equal(call.executable,str(executable))
            return D.CostQuote(0,evidence)
        w.quote = quote
        w.development = AutopilotLedger(str(root/'development.db'))
        w.orch.development = w.development
        w.orch.planner = DOC.PL.Planner(subscription_transport=DOC.SubscriptionTransport('codex'))
        w.orch.cost_quote_adapter = quote
        w.orch.require_task_authority = True
        attach_document_runtime(w.orch,file_runtime=w.file_runtime)
        def calls():
            path=root/'table-calls.jsonl'
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        w.calls=calls
        with patch.dict(os.environ,{'SOLVIO_AUTOPILOT_LOCK':str(root/'driver.lock')}), \
                patch.object(P,'codex_status',AsyncMock(return_value=P.ProviderStatus('codex',True,auth='chatgpt',billing_mode='subscription'))), \
                patch.object(P,'codex_invocation',invocation), \
                patch.object(DOC.SP,'codex_builder_invocation',invocation), \
                patch.object(DOC.CAP,'preflight',AsyncMock(side_effect=AssertionError('provider broker forbidden'))), \
                patch.object(DOC.LEAD.TechnicalLead,'judge',AsyncMock(side_effect=AssertionError('broker lead forbidden'))):
            try: yield w
            finally:
                await w.orch.stop()
                w.development.close()


def fresh(w):
    orch = DOC.Orchestrator(ledger=S.AgentRunLedger(w.ledger.path),
        router=DOC.CapabilityRouter(mobile=DOC.CapabilityApprovals(w.co,owner_principal='local-owner')),
        control_plane=w.cp,planner=DOC.PL.Planner(subscription_transport=DOC.SubscriptionTransport('codex')),
        development=w.development,cost_quote_adapter=w.quote,require_task_authority=True)
    attach_document_runtime(orch,file_runtime=w.file_runtime)
    return orch


async def advance(orch, run_id, *, until=None):
    return await asyncio.wait_for(DOC.advance(orch,run_id,until=until,limit=3000),timeout=240)


async def t_public_table_task_survives_logout_then_second_task_reuses_verified_tool():
    async with world() as w:
        response=await w.start(body())
        require_equal(response.status,201,str(await response.json()))
        accepted=await response.json();run_id=accepted['run_id']
        waiting=await advance(w.orch,run_id,until=lambda r:r.state==S.WAITING_CAPABILITY)
        require_equal(waiting.state,S.WAITING_CAPABILITY,waiting.result_summary)
        require_equal(w.development.milestones(),[])
        require_equal((await w.client.post(DOC.B.SESSION_PATH+'/logout',json={},headers=w.headers)).status,200)
        w.orch=fresh(w)
        run=await advance(w.orch,run_id)
        require_equal(run.state,S.SUCCEEDED,run.result_summary)
        require_equal(run.task_id,accepted['task_id'])
        client=await w.new_client();fresh_headers=await w.login(client)
        before=len(w.calls())
        view=await (await client.get('/v1/agent/runs/'+run_id)).json()
        require_equal({f['name'] for f in view['dateien']},set(TC.OUTPUTS))
        for item in view['dateien']:
            downloaded=await client.get(item['download_url'])
            require_equal(downloaded.status,200)
            content=await downloaded.read()
            require_equal(hashlib.sha256(content).hexdigest(),item['sha256'])
        require_equal(len(w.calls()),before,'pure reads invoked a model')
        response=await client.post('/v1/agent/tasks',json={'task':body('table-request-0002',230)},headers=fresh_headers)
        require_equal(response.status,201,str(await response.json()))
        second=(await response.json())['run_id']
        result=await advance(w.orch,second)
        require_equal(result.state,S.SUCCEEDED,result.result_summary)
        require_equal([c['phase'] for c in w.calls()[before:]],['plan','plan','assessment'])
        require_equal(len(w.development.milestones()),1)
        require(w.orch.task_authority.for_run(run_id).reference!=w.orch.task_authority.for_run(second).reference)
        require_equal(len(RF.describe_files(w.ledger,second)[0]),3)
        require_equal(await w.store.list_pending(),[])


async def t_public_failed_table_gate_repairs_before_any_output_delivery():
    async with world(repair_first=True) as w:
        response=await w.start(body());require_equal(response.status,201)
        run_id=(await response.json())['run_id']
        run=await advance(w.orch,run_id)
        require_equal(run.state,S.SUCCEEDED,run.result_summary)
        phases=[call['phase'] for call in w.calls()]
        require_equal(phases,['plan','extension_build','extension_review','extension_build','extension_review','plan','assessment'])
        reviews = [call for call in w.calls() if call['phase']=='extension_review']
        for marker in ('case=1', 'stage=output', 'reason=table_output_invalid', 'execution=terminal'):
            require(marker in reviews[0]['prompt'], 'actual Lead did not receive ' + marker)
        milestone = w.ledger.get_run(run_id).development_ref
        rows = list(w.development._db.execute(
            "SELECT payload_json FROM evidence WHERE milestone_id=? AND kind='test_report' AND ok=0", (milestone,)))
        require_equal(len(rows), 1)
        require_equal(json.loads(rows[0][0])['result_detail'], {'version':1,'ok':False,'case_count':2,
            'case_index':1,'stage':'output','reason':'table_output_invalid','execution_status':'terminal'})
        require_equal(len(RF.describe_files(w.ledger,run_id)[0]),3)
        require_equal(len([s for s in w.ledger.steps_for_run(run_id) if s.capability==TC.CAPABILITY and s.kind=='capability']),1)


async def t_followup_reuses_own_delivered_workbook_with_fresh_authority_same_cost_subject():
    from solvio.agent_runtime import task_revisions as TR
    from solvio.agent_runtime.task_authority import VerifiedTaskReceipt
    async with world() as w:
        response = await w.start(body())
        require_equal(response.status, 201)
        run_id = (await response.json())["run_id"]
        initial = await advance(w.orch, run_id)
        require_equal(initial.state, S.SUCCEEDED, initial.result_summary)
        task_before = w.ledger.get_task(initial.task_id)
        revision = TR.revision_for_run(w.ledger, run_id)
        delivered = RF.describe_files(w.ledger, run_id)[0]
        workbook = next(item for item in delivered if item["name"] == "Analyse.xlsx")
        request = {"run_id": run_id, "text": "Analysiere die ausgewählte Exceldatei erneut und liefere dieselben drei Dateiformate.",
            "expected_revision": revision["revision"], "expected_digest": revision["digest"],
            "input_artifact_ids": [workbook["id"]], "client_request_id": "table-followup-0001"}
        response = await w.client.post('/v1/agent/runs/' + run_id + '/followup', json=request, headers=w.headers)
        require_equal(response.status, 202, str(await response.json()))
        accepted = await response.json()
        run = w.ledger.get_run(accepted['run_id'])
        task = w.ledger.get_task(accepted['task_id'])
        require_equal(task.task_id, initial.task_id)
        require(run.run_id != run_id)
        require_equal(w.orch.task_authority.for_run(run.run_id).task_id, initial.task_id)
        require(w.orch.task_authority.for_run(run.run_id).reference != w.orch.task_authority.for_run(run_id).reference)
        before = len(w.calls())
        w.orch = fresh(w)
        completed = await advance(w.orch, run.run_id)
        require_equal(completed.state, S.SUCCEEDED, completed.result_summary)
        require_equal(w.ledger.get_run(run_id), initial, "followup rewrote historical run")
        require_equal(w.ledger.get_task(initial.task_id).objective, task_before.objective)
        require_equal(w.ledger.get_task(initial.task_id).requirements, task_before.requirements)
        require(TR.requirements_for_run(w.ledger, run.run_id) != task_before.requirements)
        require_equal(len(RF.describe_files(w.ledger, run.run_id)[0]), 3)
        require_equal(RF.describe_files(w.ledger, run_id)[0], delivered)
        require_equal([c['phase'] for c in w.calls()[before:]], ['plan', 'plan', 'assessment'])
        require_equal(len(w.development.milestones()), 1)
        require_equal(len(TR.history(w.ledger, run.run_id)), 2)


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
