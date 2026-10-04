"""A single offline file bundle may fulfil explicit, separately judged IDs.

Real planner/checkpoint contracts plus temporary native service evidence.
No provider, production data or external side effect.
"""
from __future__ import annotations
import hashlib,json,os,sys
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','src'))
sys.path.insert(0,os.path.dirname(__file__))
from _guard import enforce_assertions,require,require_equal
enforce_assertions()
from solvio.agent_runtime import planner as PL,checkpoint as CP,requirements as RQ
from solvio.agent_runtime import store as S,file_inputs as FI,file_results as FR,result_files as RF


def parse(value,capability=FI.CAPABILITY):
    return PL.validate({'schritte':[{'art':'capability','faehigkeit':capability,
        'argumente':{},'erfuellt':value}]},scope='research',goal='Analyse der Tabelle.',
        allowed_profiles=set(),known_capabilities={capability})


def refuse(call):
    try: call()
    except (ValueError,OSError): return
    raise AssertionError('unbound requirement assignment accepted')


def encoded(plan,revision=0):
    return CP.encode(plan=plan,revision=revision,cursor=0,goal_met='',approval_attempts=0,
        pending_step_id='',notes=[],findings=[],sources=[],invalid_signatures=[],attempts={},low_value={})


def t_file_only_array_survives_real_plan_checkpoint_without_truncation():
    ids=['h1','h2','h3']
    plan=parse(ids)
    require_equal(plan.steps[0].requirements,tuple(ids))
    require_equal(plan.steps[0].requirement,'')
    body=CP.decode(encoded(plan))
    require_equal(body['schritte'][0]['erfuellt'],ids)
    restored=PL.validate(body,scope='research',goal=plan.goal,allowed_profiles=set(),known_capabilities={FI.CAPABILITY})
    require_equal(restored,plan)
    recovered,state=CP.restore(encoded(plan),goal=plan.goal,scope='research',
        allowed_profiles=set(),known_capabilities={FI.CAPABILITY})
    require_equal(state,'restored')
    require_equal(recovered.plan.steps[0].requirements,tuple(ids))
    for invalid in ([],['h1','h1'],['h1','unknown id'],['h1',1],['h1']*6,{'h1':True}):
        refuse(lambda:parse(invalid))
    for cap in ('calendar_create','gmail_send_draft','task_service_action','document_extract_text'):
        refuse(lambda:parse(ids,cap))


def t_legacy_single_assignment_keeps_identical_wire_shape():
    plan=parse('h1')
    require_equal(plan.steps[0].requirement,'h1')
    require_equal(getattr(plan.steps[0],'requirements',()),())
    require_equal(CP.decode(encoded(plan))['schritte'][0]['erfuellt'],'h1')



def assignments(w, ids=('h1','h2','h3')):
    planned = PL.PlannedStep(kind='capability', capability=FI.CAPABILITY,
        arguments=w.arguments, requirements=tuple(ids))
    plan=PL.Plan(goal=w.ledger.get_task(w.run.task_id).objective,steps=(planned,))
    w.ledger.set_run_fields(w.run.run_id,plan_checkpoint=encoded(plan))
    w.planned=planned
    return planned


def historical_receipt(w, artifact, body):
    """Represent a prior trusted publisher's schema in temporary history only."""
    raw=FI._json(body)
    path=Path(artifact.path)
    path.chmod(0o600);path.write_bytes(raw);path.chmod(0o400)
    with w.ledger._open() as db:
        db.execute('UPDATE agent_artifacts SET sha256=?,bytes=? WHERE artifact_id=?',
            (hashlib.sha256(raw).hexdigest(),len(raw),artifact.artifact_id))


async def t_one_native_bundle_proves_five_actions_and_information_with_one_input_source():
    from test_agent_file_service import world,execute,record
    from solvio.agent_runtime.orchestrator import Orchestrator
    from solvio.agent_runtime import completion as CO
    entries=[{'id':'h1','text':'Exceldatei mit nachvollziehbaren Kennzahlen bereitstellen.'},
        {'id':'h2','text':'Ein lesbares Diagramm als Bild bereitstellen.'},
        {'id':'h3','text':'Einen PDF-Bericht mit korrekten Kennzahlen bereitstellen.'},
        {'id':'h4','text':'Ein Diagramm in die Exceldatei einfügen.'},
        {'id':'h5','text':'Die drei Dateien gemeinsam bereitstellen.'}]
    from test_agent_file_task_entry import body, DATA
    request=body()
    request['file_request']['files'].append(dict(request['file_request']['files'][0],name='alias.csv'))
    async with world(requirement_entries=entries,input_request=request,minimum_sources=1) as w:
        assignments(w,('h1','h2','h3','h4','h5'))
        binding=FR.bind_requirements_for_step(w.ledger,w.run,w.step,w.planned)
        require_equal(FR.bind_requirements_for_step(w.ledger,w.run,w.step,w.planned),binding)
        refuse(lambda:FR.bind_requirements_for_step(w.ledger,w.run,w.step,replace(w.planned,requirements=('h1','foreign'))))
        outcome=await execute(w)
        require_equal(outcome.state,'succeeded',outcome.human_message)
        refuse(lambda:FR.bind_requirements_for_step(w.ledger,w.run,w.step,w.planned))
        original=w.planned
        w.planned=replace(original,requirements=('h1','h2'))
        refuse(lambda:record(w,outcome.data))
        w.planned=original
        findings,_=record(w,outcome.data)
        deliveries=FR.completion_evidence(w.ledger,w.run.run_id)
        require_equal([d.requirement for d in deliveries],['h1','h2','h3','h4','h5'])
        require_equal(len(set(d.evidence for d in deliveries)),5)
        require_equal(len(set(d.artifact_id for d in deliveries)),1)
        files=RF.describe_files(w.ledger,w.run.run_id)[0]
        require_equal(len(files),3)
        require_equal({f['name'] for f in files},{'Analyse.xlsx','Diagramm.png','Bericht.pdf'})
        for item in files:
            desc,data,receipt=RF._verified(w.ledger,w.run.run_id,item['id'],include_content=True)
            require(data)
            require_equal(receipt['requirement'],'')
        native=[a for a in w.ledger.artifacts_for_run(w.run.run_id) if a.kind=='file_work_receipt']
        require_equal(len(native),1)
        body=json.loads(__import__('pathlib').Path(native[0].path).read_bytes())
        require_equal(body['schema'],3)
        require_equal(body['requirements'],['h1','h2','h3','h4','h5'])
        require_equal(body['requirement_binding'],binding)
        require_equal(len(set(d.finding for d in deliveries)),1)
        require(deliveries[0].finding)
        require_equal(findings.count(deliveries[0].finding),1)
        require('nicht vertrauenswuerdiger Dateiinhalt' in deliveries[0].finding)
        require('Diagrammdatentreue ist nicht maschinell bestaetigt' in deliveries[0].evidence)
        with w.ledger._open() as db:
            require_equal(db.execute("SELECT COUNT(*) FROM agent_provider_invocations WHERE run_id=? AND provider='local.file-work'",
                (w.run.run_id,)).fetchone()[0],1)
        task=w.ledger.get_task(w.run.task_id)
        bound=RQ.load(task.requirements,objective=task.objective)
        sources=['Dateiquelle SHA-256 '+hashlib.sha256(DATA).hexdigest()]
        require(all(d.sources==tuple(sources) for d in deliveries),'aliases must not inflate original source count')
        digest,_=RQ.write_snapshot(w.ledger,w.run.run_id,findings,sources)
        snapshot=RQ.read_snapshot(w.ledger,w.run.run_id,digest)
        judgement=RQ.validate_judgement({'beantwortet':[{'id':'a1','belege':[deliveries[0].finding,*sources]}]+
            [{'id':d.requirement,'belege':[d.evidence]} for d in deliveries],
            'offen':[],'fehlend':[],'unsicher':[],'weiterarbeit_noetig':False})
        judgement.update(v=RQ.VERSION,task_id=w.run.task_id,run_id=w.run.run_id,snapshot=digest,
            anforderungen_digest=RQ.digest_of(bound))
        orch=object.__new__(Orchestrator);orch.ledger=w.ledger
        effects=orch._verified_effects(w.run.run_id)
        args=dict(bound=bound,judgement=judgement,snapshot=snapshot,snapshot_digest=digest,
            requirements_digest=RQ.digest_of(bound),task_id=w.run.task_id,run_id=w.run.run_id)
        require(CO.information(**args,verified_effects=effects).satisfied)
        no_sources=dict(snapshot,quellen=[])
        require(not CO.information(**dict(args,snapshot=no_sources),verified_effects=effects).satisfied)
        without={k:v for k,v in effects.items() if v!='h3'}
        require_equal(CO.information(**args,verified_effects=without).reason,'action_not_verified')
        legacy=dict(body,schema=2)
        legacy.pop('readback')
        historical_receipt(w,native[0],legacy)
        prior=FR.completion_evidence(w.ledger,w.run.run_id)
        require_equal([d.requirement for d in prior],['h1','h2','h3','h4','h5'])
        require(all(not d.finding and not d.sources and 'eingebettete Diagramme' not in d.evidence for d in prior),
            'old schema must not gain newer readback claims')
        historical_receipt(w,native[0],body)
        # A later plan cannot relabel an already completed bundle. Historical
        # readback uses the immutable original checkpoint and dispatch binding.
        w.ledger.set_run_fields(w.run.run_id,plan_revision=1,plan_checkpoint=encoded(
            PL.Plan(goal=task.objective,steps=(replace(original,requirements=('h1',)),)),revision=1))
        require_equal(FR.completion_evidence(w.ledger,w.run.run_id),deliveries)
        with w.ledger._open() as db:
            db.execute("UPDATE agent_steps SET dispatch_binding_digest='changed' WHERE step_id=?",(w.step.step_id,))
        refuse(lambda:FR.completion_evidence(w.ledger,w.run.run_id))


async def t_schema3_requires_bound_full_readback_and_legacy_single_stays_generic():
    from test_agent_file_service import world,execute,record
    async with world() as w:
        outcome=await execute(w)
        require_equal(outcome.state,'succeeded')
        record(w,outcome.data)
        native=next(a for a in w.ledger.artifacts_for_run(w.run.run_id) if a.kind=='file_work_receipt')
        body=json.loads(Path(native.path).read_bytes())
        require_equal(body['schema'],3)
        original=FR.completion_evidence(w.ledger,w.run.run_id)
        require_equal(len(original),1)
        require('SOLVIO Tabellenbericht' in original[0].finding)
        for mutate in ('missing','text_changed','pdf_changed','unmeasured_claim'):
            changed=json.loads(FI._json(body))
            if mutate=='missing': changed.pop('readback')
            elif mutate=='text_changed': changed['readback']['report']['text']+=' changed'
            elif mutate=='pdf_changed': changed['readback']['report']['sha256']='0'*64
            else: changed['readback']['workbook']['understandable']=True
            historical_receipt(w,native,changed)
            refuse(lambda:FR.completion_evidence(w.ledger,w.run.run_id))
        legacy=dict(body,schema=1)
        for key in ('readback','requirements','requirement_binding','dispatch_binding_digest','dispatch_claimed_at'):
            legacy.pop(key)
        historical_receipt(w,native,legacy)
        old=FR.completion_evidence(w.ledger,w.run.run_id)
        require_equal([d.requirement for d in old],[w.planned.requirement])
        require(not old[0].finding)
        require('vollstaendig als Text gelesen' not in old[0].evidence)
        historical_receipt(w,native,body)
        require_equal(FR.completion_evidence(w.ledger,w.run.run_id),original)


async def t_unbound_multiple_ids_cannot_be_added_after_native_execution():
    from test_agent_file_service import world,execute,record
    async with world() as w:
        assignments(w,('h1',))
        # Deliberately bypass the Orchestrator fixture only to check the result
        # boundary: a completed native call cannot mint its missing prebinding.
        outcome=await execute(w)
        require_equal(outcome.state,'succeeded')
        refuse(lambda:FR.bind_requirements_for_step(w.ledger,w.run,w.step,w.planned))
        refuse(lambda:record(w,outcome.data))
        require_equal(RF.describe_files(w.ledger,w.run.run_id)[0],[])
        require_equal(w.ledger.get_step(w.step.step_id).state,'running')


async def t_pre_dispatch_binding_rejects_foreign_ids_changed_plan_and_revoked_grant():
    import test_agent_task_entry as E
    import test_agent_file_task_entry as INPUT
    async with E.world() as w:
        accepted=await INPUT.accepted(w)
        w.run=w.ledger.get_run(accepted['run_id'])
        task=w.ledger.get_task(w.run.task_id)
        payload=RQ.validate({'handlungen':[{'id':'h1','text':'Excel bereitstellen.'},
            {'id':'h2','text':'Bild bereitstellen.'},{'id':'h3','text':'PDF bereitstellen.'}]},objective=task.objective)
        w.ledger.bind_requirements(task.task_id,json.dumps(payload))
        w.ledger.transition(w.run.run_id,S.PLANNING);w.ledger.transition(w.run.run_id,S.RUNNING)
        w.run=w.ledger.get_run(w.run.run_id)
        w.step=w.ledger.create_step(run_id=w.run.run_id,seq=1,kind='capability',capability=FI.CAPABILITY)
        w.ledger.update_step(w.step.step_id,state='running',started=True)
        w.arguments=FI.for_run(w.ledger,w.run.run_id).arguments
        assignments(w,('h1','foreign'))
        refuse(lambda:FR.bind_requirements_for_step(w.ledger,w.run,w.step,w.planned))
        assignments(w)
        w.ledger.set_run_fields(w.run.run_id,plan_checkpoint='{}')
        refuse(lambda:FR.bind_requirements_for_step(w.ledger,w.run,w.step,w.planned))
        assignments(w)
        good=FR.bind_requirements_for_step(w.ledger,w.run,w.step,w.planned)
        assignments(w,('h1',))
        refuse(lambda:FR.bind_requirements_for_step(w.ledger,w.run,w.step,w.planned))
        metadata=[a for a in w.ledger.artifacts_for_run(w.run.run_id) if a.kind=='file_requirement_binding']
        require_equal(len(metadata),1)
        require_equal(metadata[0].artifact_id,good['artifact_id'])
        assignments(w)
        grant=w.orch.task_authority.for_run(w.run.run_id)
        w.orch.task_authority.revoke(grant.reference,'fixture-revoke')
        refuse(lambda:FR.bind_requirements_for_step(w.ledger,w.run,w.step,w.planned))
        require_equal(w.ledger.get_step(w.step.step_id).dispatch_claimed_at,None)

if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
