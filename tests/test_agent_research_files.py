"""Public research → existing Builder → real Office files, under one task.

HTTPS, authentication, orchestration, Hermes transport, cost ledger, sandboxed
Office execution, readback, file delivery and restart are real. Only provider
protocols are local programs. They assert actual input and claim state instead
of returning unconditional success. No account, provider inference or web call.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from _artifact_creation_adapter import SOURCE
from test_agent_public_research import world as research_world, SOURCES
from test_agent_file_tool_process import runtime
from solvio.agent_runtime import planner as PL, specialists as SP, store as S
from solvio.agent_runtime import cost_dispatch as D, artifact_creation as AR

OBJECTIVE = ('Recherchiere drei Ausflugsziele und gib mir den Vergleich mit Quellen '
             'als Excel und PDF. Offene Eintrittspreise ausdrücklich offen lassen.')
OPEN = 'Eintrittspreis des dritten Ziels ist nicht bestaetigt.'
ANSWER = {'findings':['Drei Ziele: Park, Museum und Hafen.'], 'evidence':SOURCES,
          'recommended_path':'Park und Hafen passen zum Nachmittag.',
          'uncertainties':[OPEN], 'assumptions':[], 'rejected_alternatives':[],
          'risk_notes':[], 'confidence':'mittel'}
PLAN = {'schritte':[
    {'art':'specialist','profil':'researcher/hermes','auftrag':OBJECTIVE},
    {'art':'specialist','profil':SP.FILES_PROFILE,'auftrag':'Vergleich mit Quellen und offenen Preisen als Excel und PDF.',
     'erfuellt':['h1','h2']}],
    'anforderungen':{'auskunft':[{'id':'a1','text':'Drei Ziele mit belegten Quellen vergleichen.'}],
        'handlungen':[{'id':'h1','text':'Exceldatei mit Vergleich und Quellen bereitstellen.'},
                      {'id':'h2','text':'PDF mit Vergleich und Quellen bereitstellen.'}],
        'unklar':[],'belege':{'mindestens':2}}}

PEER = r'''
import json, os, pathlib, sqlite3, sys
root = pathlib.Path(__file__).parent
if sys.argv[1:3] == ['login','status']:
    print('Logged in using ChatGPT', file=sys.stderr)
    raise SystemExit(0)
fixture=json.loads((root/'files-fixture.json').read_text())
prompt=sys.stdin.read()
request=None
if '--json' in sys.argv:
    for message in json.loads(prompt.split('\n',1)[1]):
        try: candidate=json.loads(message['content'])
        except ValueError: continue
        if isinstance(candidate,dict) and ('ziel' in candidate or 'ergebnis' in candidate): request=candidate
    assert request is not None
    kind='assessment' if 'ergebnis' in request else 'plan'
else: kind='specialist'
with sqlite3.connect('file:'+fixture['ledger']+'?mode=ro',uri=True) as db:
    claims=db.execute("SELECT task_id,run_id,phase,operation_id FROM agent_provider_invocations WHERE state='claimed'").fetchall()
    assert len(claims)==1, claims
    task,run,phase,operation=claims[0]
with (root/'calls.jsonl').open('a') as log:
    log.write(json.dumps({'kind':kind,'claimed':1,'task':task,'run':run,'phase':phase,'operation':operation,'request':request})+'\n')
if kind=='specialist':
    data=json.loads(pathlib.Path('input.json').read_text())
    assert (data['task_id'],data['run_id'],data['step_id'])==(task,run,operation)
    assert data['derived_source']['owner_upload'] is False
    assert fixture['open'] in json.dumps(data)
    assert data['owner_objective']==fixture['objective']
    pathlib.Path('adapter.py').write_text(fixture['source'])
    print(json.dumps({'findings':['adapter.py erstellt.'],'evidence':['adapter.py'],
                     'recommended_path':'Core muss Code ausführen und Dateien prüfen.','uncertainties':[]}))
    raise SystemExit(0)
if kind=='plan':
    assert 'files/codex' in json.dumps(request)
    reply=fixture['plan']
else:
    snapshot=json.loads(request['ergebnis'])
    content=[line for line in snapshot['befunde'] if line.startswith('Vollständiger unabhängig gelesener Dateiinhalt')]
    assert len(content)==1, 'actual complete readback once, not builder prose'
    assert fixture['open'] in content[0], 'uncertainty must survive in actual files'
    assert all(source in content[0] for source in fixture['sources']), 'sources absent in files'
    complete='Comparison.xlsx' in content[0] and 'Comparison.pdf' in content[0]
    catalogue=request['gepruefte_handlungsbelege']['eintraege']
    assert {row['id'] for row in catalogue}=={'h1','h2'}
    covered=[{'id':'a1','belege':snapshot['quellen']}]
    if complete:
        covered += [{'id':row['id'],'belege':[snapshot[row['feld']][row['index']]]} for row in catalogue]
    reply={'beantwortet':covered,'offen':[] if complete else ['h1','h2'],
           'fehlend':[] if complete else ['Die verlangte PDF-Datei fehlt.'],
           'unsicher':[],'weiterarbeit_noetig':not complete}
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(reply)}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':11,'output_tokens':7}}))
'''


@asynccontextmanager
async def world(*, missing_pdf=False):
    async with research_world(objective=OBJECTIVE, plan=PLAN, answer=ANSWER) as w:
        source = SOURCE.replace("'files':files", "'files':files[:1]") if missing_pdf else SOURCE
        (w.folder/'files-fixture.json').write_text(json.dumps({'ledger':w.ledger.path,
            'plan':PLAN,'source':source,'objective':OBJECTIVE,'open':OPEN,'sources':SOURCES}))
        w.executable.write_text('#!'+str(Path(sys.executable).resolve())+'\n'+PEER)
        office = runtime()
        original = w.runtime
        def configured():
            orch = original()
            orch.extension_activation = SimpleNamespace(file_runtime=office, selected=lambda run:None)
            return orch
        w.runtime = configured
        w.runtime()
        with patch.object(SP, 'resolve', return_value=str(w.executable)):
            yield w


async def t_public_research_files_finish_after_logout_and_fresh_reader_downloads_both():
    async with world() as w:
        accepted, grant = await w.admit()
        await w.detach()
        final = await w.tick_until(accepted['run_id'], S.SUCCEEDED)
        require_equal(final.assessment_calls, 1, 'Do not spend an assessment before planned files exist')
        require_equal([c['kind'] for c in w.calls()], ['plan','specialist','assessment'])
        require_equal(final.specialist_count, 2)
        require_equal(len(w.ledger.runs_for_task(final.task_id)), 1)
        claims=D.invocations(w.ledger, final.task_id)
        require_equal(len(claims), 4)
        require(all(c['task_id']==final.task_id and c['run_id']==final.run_id and c['state']=='finished' for c in claims))
        require_equal(w.orch.costs.view(final.task_id)['counts'], {'settled':4})
        retained=w.calls()
        w.runtime()
        await w.orch.reconcile()
        await w.orch.tick()
        reader,_=await w.fresh_reader()
        view=await (await reader.get('/v1/agent/runs/'+final.run_id)).json()
        require_equal(view['zustand_code'],S.SUCCEEDED)
        require_equal({f['name'] for f in view['dateien']},{'Comparison.xlsx','Comparison.pdf'})
        for file in view['dateien']:
            response=await reader.get(file['download_url'])
            require_equal(response.status,200)
            content=await response.read()
            require_equal(hashlib.sha256(content).hexdigest(),file['sha256'])
        deliveries=AR.completion_evidence(w.ledger,final.run_id)
        require(all(OPEN in d.finding for d in deliveries))
        require_equal(w.calls(),retained,'reading/restart created additional files or model calls')
        require_equal(w.orch.task_authority.for_run(final.run_id).reference,grant.reference)
        require_equal(await w.store.list_pending(),[])


async def t_public_missing_requested_pdf_keeps_partial_file_without_claiming_success():
    async with world(missing_pdf=True) as w:
        accepted,_=await w.admit()
        run=await w.tick_until(accepted['run_id'],S.FAILED)
        require_equal(run.failure_category,'goal_unverified')
        reader,_=await w.fresh_reader()
        view=await (await reader.get('/v1/agent/runs/'+run.run_id)).json()
        require_equal([f['name'] for f in view['dateien']],['Comparison.xlsx'])
        require_equal([c['kind'] for c in w.calls()],['plan','specialist','assessment'])
        require_equal(len(w.ledger.runs_for_task(run.task_id)),1)


async def t_public_cancel_after_research_prevents_builder_and_publication():
    async with world() as w:
        accepted,_=await w.admit();run_id=accepted['run_id']
        for _ in range(8):
            await w.orch.tick()
            if any(s.specialist_profile=='researcher/hermes' and s.state=='succeeded' for s in w.ledger.steps_for_run(run_id)):
                break
        else: raise AssertionError('research not reached')
        require_equal(w.ledger.get_run(run_id).state,S.RUNNING)
        response=await w.client.post('/v1/agent/runs/'+run_id+'/cancel',json={},headers=w.headers)
        require_equal(response.status,200)
        await w.orch.tick()
        require_equal(w.ledger.get_run(run_id).state,S.CANCELLED)
        require_equal([c['kind'] for c in w.calls()],['plan'])
        require_equal(AR.completion_evidence(w.ledger,run_id),())


def t_plan_file_bundle_requires_research_explicit_assignment_and_one_final_producer():
    kwargs=dict(scope='research',allowed_profiles={'researcher/hermes',SP.FILES_PROFILE},
                known_capabilities=set(),goal=OBJECTIVE)
    plan=PL.validate(PLAN,**kwargs)
    require_equal(plan.steps[1].requirements,('h1','h2'))
    mutations=[]
    for key,value in [('verzichtbar',True),('erfuellt',[]),('argumente',{'path':'/tmp/foreign'})]:
        raw=copy.deepcopy(PLAN);raw['schritte'][1][key]=value;mutations.append(raw)
    raw=copy.deepcopy(PLAN);raw['schritte'].reverse();mutations.append(raw)
    raw=copy.deepcopy(PLAN);raw['schritte'].append(raw['schritte'][1]);mutations.append(raw)
    for raw in mutations:
        try: PL.validate(raw,**kwargs)
        except PL.PlanInvalid: pass
        else: raise AssertionError('invalid file plan accepted')


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
