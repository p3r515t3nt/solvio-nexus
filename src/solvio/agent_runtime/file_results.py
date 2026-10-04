"""Durable semantic file evidence through the existing result-file ledger.

Only the Core's typed, independently checked service result can be published.
Original input, selected implementation, settled service invocation, requirement
and each delivered byte hash remain joined by one immutable receipt. Historical
readback never requires a still-active execution grant or a current tool version.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import time

from solvio.agent_runtime import file_inputs as FI, result_files as RF, requirements as RQ, store as S

MAX_RECEIPT_BYTES = 128 * 1024


@dataclass(frozen=True)
class VerifiedOutputFile:
    name: str
    mime_type: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class VerifiedFileOutput:
    task_id: str
    run_id: str
    step_id: str
    grant_reference: str
    resources_json: bytes = field(repr=False)
    facts_json: bytes = field(repr=False)
    readback_json: bytes = field(repr=False)
    summary: str
    files: tuple[VerifiedOutputFile, ...] = field(repr=False)

    @classmethod
    def from_checked(cls, task_step, resources, checked):
        from solvio.agent_runtime import table_report_contract as TC
        if (type(checked) is not dict or set(checked) != {"summary","tables","files","readback"}
                or type(checked['files']) is not dict or set(checked['files']) != set(TC.OUTPUTS)):
            raise ValueError('file_checked_result_invalid')
        TC.validate_readback(checked['readback'], report_sha256=hashlib.sha256(checked['files']['Bericht.pdf']).hexdigest())
        return cls(task_step.task_id, task_step.run_id, task_step.step_id, task_step.reference,
            FI._json(resources), FI._json(checked['tables']), FI._json(checked['readback']), checked['summary'],
            tuple(VerifiedOutputFile(name, mime, checked['files'][name]) for name,mime in TC.OUTPUTS.items()))


def _cost_proof(connection, task_id, run_id, step_id, resources):
    from solvio.agent_runtime.cost_dispatch import ServiceInvocation
    call = ServiceInvocation.bind(capability=FI.CAPABILITY, version=FI.VERSION,
        service='local.file-work', operation='table_report', arguments=resources['input'], resources=resources)
    rows = connection.execute("SELECT i.reservation_id,i.invocation_id,i.request_digest,i.state,"
        "i.finished_at,r.state AS cost_state,r.actual_cents,r.route FROM agent_provider_invocations i "
        "JOIN agent_cost_reservations r ON r.reservation_id=i.reservation_id "
        "WHERE i.task_id=? AND i.run_id=? AND i.operation_id=? AND i.phase='capability' "
        "AND i.provider='local.file-work'", (task_id,run_id,step_id)).fetchall()
    if (len(rows) != 1 or rows[0]['request_digest'] != call.request_digest
            or rows[0]['state'] != 'finished' or rows[0]['finished_at'] is None
            or rows[0]['cost_state'] != 'settled' or rows[0]['actual_cents'] != 0
            or rows[0]['route'] != 'local.file-work'):
        raise ValueError('file_cost_receipt_unconfirmed')
    return {'reservation_id':rows[0]['reservation_id'],'invocation_id':rows[0]['invocation_id'],
            'request_digest':call.request_digest}


def _requirements(task, requirement):
    bound = RQ.load(task.requirements, objective=task.objective) if task else None
    if (bound is None or not isinstance(requirement,str) or not requirement or requirement not in {
            item['id'] for key in (RQ.ASK,RQ.ACTION,RQ.UNCLEAR) for item in bound[key]}):
        raise ValueError('file_requirement_unbound')
    return RQ.digest_of(bound)


def _assignment(planned):
    from solvio.agent_runtime import planner as PL
    ids = getattr(planned, 'requirements', ())
    if not ids:
        return ()
    if (planned.capability != FI.CAPABILITY or getattr(planned, 'kind', 'capability') != 'capability'
            or planned.requirement or type(ids) is not tuple):
        raise ValueError('file_requirement_assignment_invalid')
    return PL.file_requirement_ids(ids)


def _requirement_set(task, ids):
    from solvio.agent_runtime import planner as PL
    ids = PL.file_requirement_ids(ids)
    bound = RQ.load(task.requirements, objective=task.objective) if task else None
    known = {item['id'] for key in (RQ.ASK, RQ.ACTION, RQ.UNCLEAR) for item in bound[key]} if bound else set()
    if any(value not in known for value in ids):
        raise ValueError('file_requirement_unbound')
    return RQ.digest_of(bound)


def bind_requirements_for_step(ledger, run, step, planned):
    """Set once BEFORE the router claim; maps a plan, never claims fulfilment.

    The immutable checkpoint, selected IDs and current requirements are kept in
    the existing artifact ledger. No model/tool result or later replan can add
    an ID after physical execution. Legacy single-requirement steps keep v1.
    """
    ids = _assignment(planned)
    if not ids:
        return None
    from solvio.agent_runtime import checkpoint as CP
    from solvio.agent_runtime.task_revisions import task_view
    from solvio.agent_runtime.task_authority import TaskAuthority
    authority = TaskAuthority(ledger)
    grant = authority.for_run(run.run_id)
    if grant is None:
        raise ValueError('file_requirement_grant_missing')
    with ledger._open() as connection:
        connection.execute('BEGIN IMMEDIATE')
        current = connection.execute('SELECT * FROM agent_runs WHERE run_id=?', (run.run_id,)).fetchone()
        produced = connection.execute('SELECT * FROM agent_steps WHERE step_id=?', (step.step_id,)).fetchone()
        allowed = authority._verify(connection, grant.reference, FI.CAPABILITY, planned.arguments,
            FI.VERSION, task_id=run.task_id, run_id=run.run_id)
        if (not allowed.allowed or not current or current['task_id'] != run.task_id or current['state'] != S.RUNNING
                or not produced or produced['run_id'] != run.run_id or produced['kind'] != 'capability'
                or produced['capability'] != FI.CAPABILITY or produced['state'] != 'running'
                or produced['dispatch_claimed_at'] is not None or produced['dispatch_binding_digest']
                or produced['attempt'] != current['plan_revision'] + 1):
            raise ValueError('file_requirement_binding_too_late_or_changed')
        checkpoint = CP.decode(current['plan_checkpoint'])
        plan_step = CP._step_to_raw(planned)
        if (checkpoint is None or checkpoint['revision'] != current['plan_revision']
                or not 0 < produced['seq'] <= len(checkpoint['schritte'])
                or checkpoint['schritte'][produced['seq'] - 1] != plan_step):
            raise ValueError('file_requirement_plan_changed')
        requirement_digest = _requirement_set(task_view(ledger, run.run_id), ids)
        body = {'schema': 1, 'task_id': run.task_id, 'run_id': run.run_id, 'step_id': step.step_id,
            'seq': produced['seq'], 'attempt': produced['attempt'], 'grant_reference': grant.reference,
            'requirements': list(ids), 'requirements_digest': requirement_digest,
            'arguments': planned.arguments, 'plan_checkpoint': current['plan_checkpoint'],
            'checkpoint_sha256': hashlib.sha256(current['plan_checkpoint'].encode('utf-8')).hexdigest()}
        raw = FI._json(body)
        if len(raw) > MAX_RECEIPT_BYTES:
            raise ValueError('file_requirement_binding_limit')
        name = 'file-requirements-' + step.step_id + '.json'
        digest = hashlib.sha256(raw).hexdigest()
        aid = 'aa-' + hashlib.sha256((run.run_id + '\0' + name + '\0' + digest).encode()).hexdigest()[:16]
        old = connection.execute("SELECT * FROM agent_artifacts WHERE run_id=? AND kind='file_requirement_binding' AND path=?",
            (run.run_id, RF._path(run.run_id, name))).fetchall()
        if old and (len(old) != 1 or old[0]['artifact_id'] != aid or old[0]['sha256'] != digest):
            raise ValueError('file_requirement_binding_already_set')
        directory = FI.DC._directory(run.run_id)
        try:
            RF._write_once(directory, name, raw)
        finally:
            os.close(directory)
        connection.execute('INSERT OR IGNORE INTO agent_artifacts '
            '(artifact_id,run_id,kind,path,sha256,bytes,created_at) VALUES (?,?,?,?,?,?,?)',
            (aid, run.run_id, 'file_requirement_binding', RF._path(run.run_id, name), digest, len(raw), time.time()))
        refs = list(dict.fromkeys([*json.loads(produced['artifact_refs'] or '[]'), aid]))
        connection.execute('UPDATE agent_steps SET artifact_refs=? WHERE step_id=?', (json.dumps(refs), step.step_id))
    return {'artifact_id': aid, 'sha256': digest}


def _read_requirement_binding(ledger, run_id, step, *, ids, requirement_digest, grant_reference, arguments):
    """Historical proof uses its frozen plan, never the current later replan."""
    from solvio.agent_runtime import checkpoint as CP, planner as PL
    candidates = [a for a in ledger.artifacts_for_run(run_id) if a.kind == 'file_requirement_binding'
        and a.artifact_id in step.artifact_refs]
    if len(candidates) != 1:
        raise ValueError('file_requirement_binding_missing')
    artifact = candidates[0]
    name = 'file-requirements-' + step.step_id + '.json'
    if artifact.path != RF._path(run_id, name):
        raise ValueError('file_requirement_binding_path_changed')
    directory = FI.DC._directory(run_id)
    try:
        raw = RF._read_file(directory, name, artifact.bytes, artifact.sha256, limit=MAX_RECEIPT_BYTES)
    finally:
        os.close(directory)
    body = json.loads(raw)
    keys = {'schema','task_id','run_id','step_id','seq','attempt','grant_reference','requirements',
        'requirements_digest','arguments','plan_checkpoint','checkpoint_sha256'}
    run = ledger.get_run(run_id)
    if (type(body) is not dict or set(body) != keys or FI._json(body) != raw or body['schema'] != 1
            or body['task_id'] != run.task_id or body['run_id'] != run_id or body['step_id'] != step.step_id
            or body['seq'] != step.seq or body['attempt'] != step.attempt
            or body['grant_reference'] != grant_reference or body['arguments'] != arguments
            or body['requirements'] != list(ids) or body['requirements_digest'] != requirement_digest
            or hashlib.sha256(body['plan_checkpoint'].encode('utf-8')).hexdigest() != body['checkpoint_sha256']):
        raise ValueError('file_requirement_binding_changed')
    checkpoint = CP.decode(body['plan_checkpoint'])
    if (checkpoint is None or checkpoint['revision'] + 1 != step.attempt
            or not 0 < step.seq <= len(checkpoint['schritte'])):
        raise ValueError('file_requirement_plan_changed')
    selected = checkpoint['schritte'][step.seq - 1]
    plan = PL.validate({'schritte':[selected]}, scope='research', goal='', allowed_profiles=set(),
        known_capabilities={FI.CAPABILITY})
    proposed = plan.steps[0]
    if (proposed.capability != FI.CAPABILITY or proposed.arguments != arguments
            or _assignment(proposed) != tuple(ids)):
        raise ValueError('file_requirement_plan_changed')
    return {'artifact_id': artifact.artifact_id, 'sha256': artifact.sha256}


def record_result(ledger, activation, run, step, planned, data):
    """Publish after cost settlement; commit semantic receipt and succeeded step.

    Each RF publication remains hidden while the step is running. Final receipt
    and step completion share one transaction; cancellation or changed authority
    before that transaction leaves no confirmed result. Never starts a tool.
    """
    from solvio.capabilities.file_adapter import TaskFileService, _spec
    from solvio.agent_runtime import table_report_contract as TC
    from solvio.agent_runtime.task_start_service import TaskStepAuthority
    if (type(data) is not VerifiedFileOutput or data.task_id != run.task_id
            or data.run_id != run.run_id or data.step_id != step.step_id
            or planned.capability != FI.CAPABILITY):
        raise ValueError('file_result_binding_changed')
    token = TaskStepAuthority(data.grant_reference,run.task_id,run.run_id,step.step_id)
    service = TaskFileService(ledger,activation)
    resources = service.resources(_spec(),planned.arguments,token)
    if (FI._json(resources) != data.resources_json or type(data.files) is not tuple
            or len(data.files) != len(TC.OUTPUTS) or type(data.summary) is not str
            or not 1 <= len(data.summary) <= 4000 or '\x00' in data.summary
            or type(data.facts_json) is not bytes or len(data.facts_json) > 65536
            or type(data.readback_json) is not bytes or len(data.readback_json) > 65536):
        raise ValueError('file_result_binding_changed')
    facts = json.loads(data.facts_json)
    if type(facts) is not list or not 1 <= len(facts) <= 8 or FI._json(facts) != data.facts_json:
        raise ValueError('file_facts_binding_changed')
    size, names = 0, set()
    for item in data.files:
        if (type(item) is not VerifiedOutputFile or item.name not in TC.OUTPUTS or item.name in names
                or item.mime_type != TC.OUTPUTS[item.name] or type(item.content) is not bytes
                or not item.content):
            raise ValueError('file_output_binding_changed')
        size += len(item.content)
        names.add(item.name)
    if size > TC.MAX_RESULT_BYTES:
        raise ValueError('file_output_limit')
    readback = json.loads(data.readback_json)
    if FI._json(readback) != data.readback_json:
        raise ValueError('file_readback_binding_changed')
    TC.validate_readback(readback, report_sha256=hashlib.sha256(
        next(item.content for item in data.files if item.name == 'Bericht.pdf')).hexdigest())
    current_step = ledger.get_step(step.step_id)
    if (current_step is None or current_step.state != 'running'
            or current_step.dispatch_claimed_at is None or not current_step.dispatch_binding_digest):
        raise ValueError('file_step_not_claimed')
    from solvio.agent_runtime.task_revisions import task_view
    task = task_view(ledger,run.run_id)
    ids = _assignment(planned)
    requirement_digest = _requirement_set(task, ids) if ids else _requirements(task,planned.requirement)
    association = (_read_requirement_binding(ledger, run.run_id, current_step, ids=ids,
        requirement_digest=requirement_digest, grant_reference=data.grant_reference, arguments=planned.arguments)
        if ids else None)
    with ledger._open() as connection:
        cost = _cost_proof(connection,run.task_id,run.run_id,step.step_id,resources)
    implementation = next((a for a in ledger.artifacts_for_run(run.run_id)
        if a.artifact_id == resources['artifact'] and a.kind == 'extension_candidate'),None)
    if implementation is None:
        raise ValueError('file_implementation_missing')
    outputs = [RF.publish_file(ledger,run.run_id,step.step_id,item.content,item.name,item.mime_type,
        requirement=planned.requirement,provider='local.file-work',billing_mode='free_local') for item in data.files]
    proof = {'schema':3,'task_id':run.task_id,'run_id':run.run_id,'step_id':step.step_id,
        'grant_reference':data.grant_reference,'requirement':planned.requirement,
        'requirements_digest':requirement_digest,'resources':resources,
        'implementation_sha256':implementation.sha256,'cost':cost,
        'independent_check':'table_report_v1','facts':facts,'summary':data.summary,'readback':readback,
        'outputs':outputs,'execution_status':'terminal'}
    proof.update(requirements=list(ids), requirement_binding=association,
        dispatch_binding_digest=current_step.dispatch_binding_digest,
        dispatch_claimed_at=current_step.dispatch_claimed_at)
    raw = FI._json(proof)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError('file_receipt_limit')
    name = 'file-work-' + step.step_id + '.json'
    digest = hashlib.sha256(raw).hexdigest()
    aid = 'aa-' + hashlib.sha256((run.run_id+'\0'+name+'\0'+digest).encode()).hexdigest()[:16]
    directory = FI.DC._directory(run.run_id)
    try:
        RF._write_once(directory,name,raw)
    finally:
        os.close(directory)
    # Revalidate after publication helpers; a cancelled/revoked task must not
    # be reactivated by already generated bytes or a callback during writeback.
    if FI._json(service.resources(_spec(),planned.arguments,token)) != data.resources_json:
        raise ValueError('file_result_binding_changed')
    from solvio.agent_runtime.task_authority import TaskAuthority
    authority = TaskAuthority(ledger)
    with ledger._open() as connection:
        connection.execute('BEGIN IMMEDIATE')
        allowed = authority._verify(connection,data.grant_reference,FI.CAPABILITY,planned.arguments,
            FI.VERSION,task_id=run.task_id,run_id=run.run_id)
        active = connection.execute('SELECT state,task_id FROM agent_runs WHERE run_id=?',(run.run_id,)).fetchone()
        produced = connection.execute('SELECT * FROM agent_steps WHERE step_id=?',(step.step_id,)).fetchone()
        if (not allowed.allowed or not active or active['state'] != S.RUNNING
                or not produced or produced['run_id'] != run.run_id or produced['state'] != 'running'
                or produced['dispatch_binding_digest'] != current_step.dispatch_binding_digest
                or produced['dispatch_claimed_at'] != current_step.dispatch_claimed_at):
            raise ValueError('file_result_authority_ended')
        from solvio.agent_runtime.task_authority import _from_row
        grant_row = connection.execute('SELECT * FROM agent_task_grants WHERE run_id=?',(run.run_id,)).fetchone()
        bound = FI._from_entries(run.task_id,run.run_id,_from_row(grant_row).capabilities)
        FI._read_bound(connection,bound)
        # No writes precede this view. BEGIN IMMEDIATE prevents another writer
        # changing the original/revision requirements until this commit.
        current_task = task_view(ledger,run.run_id)
        if (_requirement_set(current_task, ids) if ids else _requirements(current_task,planned.requirement)) != requirement_digest:
            raise ValueError('file_requirement_changed')
        if ids and _read_requirement_binding(ledger, run.run_id, ledger.get_step(step.step_id), ids=ids,
                requirement_digest=requirement_digest, grant_reference=data.grant_reference,
                arguments=planned.arguments) != association:
            raise ValueError('file_requirement_binding_changed')
        if _cost_proof(connection,run.task_id,run.run_id,step.step_id,resources) != cost:
            raise ValueError('file_cost_receipt_changed')
        # RF intentionally hides a running producer. Read its immutable output
        # and receipt bytes directly before the same transaction confirms it.
        # A changed file during publication must not leave a succeeded step.
        directory = FI.DC._directory(run.run_id)
        try:
            RF._read_file(directory,name,len(raw),digest,limit=MAX_RECEIPT_BYTES,
                include_content=False)
            for output in outputs:
                for kind,suffix,limit in (('result_file','bin',RF.MAX_FILE_BYTES),
                        ('result_receipt','json',RF.MAX_RECEIPT_BYTES)):
                    filename = 'result-' + output['id'] + '.' + suffix
                    artifact = connection.execute('SELECT * FROM agent_artifacts '
                        'WHERE run_id=? AND kind=? AND path=?',
                        (run.run_id,kind,RF._path(run.run_id,filename))).fetchone()
                    if artifact is None or artifact['artifact_id'] not in json.loads(produced['artifact_refs']):
                        raise ValueError('file_output_receipt_changed')
                    if kind == 'result_file' and (artifact['sha256'] != output['sha256']
                            or artifact['bytes'] != output['size']):
                        raise ValueError('file_output_receipt_changed')
                    RF._read_file(directory,filename,artifact['bytes'],artifact['sha256'],
                        limit=limit,include_content=False)
        finally:
            os.close(directory)
        values = (run.run_id,'file_work_receipt',RF._path(run.run_id,name),digest,len(raw))
        prior = connection.execute('SELECT run_id,kind,path,sha256,bytes FROM agent_artifacts WHERE artifact_id=?',(aid,)).fetchone()
        if prior is not None and tuple(prior) != values:
            raise ValueError('file_receipt_changed')
        connection.execute('INSERT OR IGNORE INTO agent_artifacts '
            '(artifact_id,run_id,kind,path,sha256,bytes,created_at) VALUES (?,?,?,?,?,?,?)',(aid,*values,time.time()))
        refs = list(dict.fromkeys([*json.loads(produced['artifact_refs'] or '[]'),aid]))
        connection.execute("UPDATE agent_steps SET state='succeeded',finished_at=?,summary=?,"
            "outcome_reason='file_report_verified',artifact_refs=? WHERE step_id=?",
            (time.time(),'Tabellenauswertung und drei Ergebnisdateien unabhaengig geprueft.',json.dumps(refs),step.step_id))
    evidence = completion_evidence(ledger,run.run_id)
    return (list(dict.fromkeys(value for item in evidence
        for value in (item.finding,item.evidence) if value)),refs)


@dataclass(frozen=True)
class FileWorkDelivery:
    artifact_id: str
    evidence: str
    requirement: str
    finding: str = ''
    sources: tuple[str, ...] = ()


def completion_evidence(ledger,run_id):
    from solvio.agent_runtime.task_authority import _from_row,_run_fingerprint,VerifiedTaskReceipt
    artifacts = ledger.artifacts_for_run(run_id)
    if not any(a.kind in {'task_file_manifest','task_file_input','file_work_receipt'} for a in artifacts):
        return ()
    with ledger._open() as connection:
        grant_row = connection.execute('SELECT * FROM agent_task_grants WHERE run_id=?',(run_id,)).fetchone()
        task_row = connection.execute('SELECT * FROM agent_tasks WHERE task_id=(SELECT task_id FROM agent_runs WHERE run_id=?)',(run_id,)).fetchone()
        if not grant_row or not task_row:
            raise ValueError('file_historical_grant_missing')
        grant = _from_row(grant_row)
        if grant.task_fingerprint != _run_fingerprint(connection,task_row,run_id):
            raise ValueError('file_historical_task_changed')
        entries = json.loads(grant_row['capabilities'])
        FI.validate_issue(connection,grant.task_id,run_id,
            VerifiedTaskReceipt(grant.receipt_method,grant.receipt_reference,grant.authorizer),entries)
    bound = FI._from_entries(grant.task_id,run_id,grant.capabilities)
    if bound is None:
        raise ValueError('file_historical_grant_missing')
    # Sources are the immutable owner inputs, not the generated outputs.
    # Equal bytes under different names remain ONE source. Reading through
    # the existing binding verifies the manifest, ledger rows and every byte.
    with ledger._open() as connection:
        original = FI._read_bound(connection, bound)
    input_sources = tuple(dict.fromkeys('Dateiquelle SHA-256 '
        + hashlib.sha256(item.content).hexdigest() for item in original.files))
    from solvio.agent_runtime.task_revisions import task_view
    task = task_view(ledger,run_id)
    deliveries = []
    for artifact in artifacts:
        if artifact.kind != 'file_work_receipt':
            continue
        step = next((s for s in ledger.steps_for_run(run_id) if artifact.artifact_id in s.artifact_refs
            and s.kind == 'capability' and s.capability == FI.CAPABILITY and s.state == 'succeeded'
            and s.dispatch_claimed_at is not None and s.dispatch_binding_digest),None)
        if step is None:
            raise ValueError('file_result_step_unconfirmed')
        name = 'file-work-' + step.step_id + '.json'
        if artifact.path != RF._path(run_id,name):
            raise ValueError('file_receipt_path_changed')
        directory = FI.DC._directory(run_id)
        try:
            raw = RF._read_file(directory,name,artifact.bytes,artifact.sha256,limit=MAX_RECEIPT_BYTES)
        finally:
            os.close(directory)
        proof = json.loads(raw)
        if type(proof) is not dict or type(proof.get('schema')) is not int:
            raise ValueError('file_receipt_invalid')
        schema = proof.get('schema')
        ids = tuple(proof.get('requirements', ())) if schema in (2,3) else ()
        if schema == 2 or (schema == 3 and ids):
            requirement_digest = _requirement_set(task, ids)
            association = _read_requirement_binding(ledger, run_id, step, ids=ids,
                requirement_digest=requirement_digest, grant_reference=grant.reference, arguments=bound.arguments)
            if (proof.get('requirement') != '' or proof.get('requirement_binding') != association
                    or proof.get('dispatch_binding_digest') != step.dispatch_binding_digest
                    or proof.get('dispatch_claimed_at') != step.dispatch_claimed_at):
                raise ValueError('file_requirement_binding_changed')
        else:
            requirement_digest = _requirements(task, proof.get('requirement'))
        if schema == 3:
            if (type(proof.get('requirements')) is not list
                    or (not ids and proof.get('requirement_binding') is not None)
                    or proof.get('dispatch_binding_digest') != step.dispatch_binding_digest
                    or proof.get('dispatch_claimed_at') != step.dispatch_claimed_at):
                raise ValueError('file_requirement_binding_changed')
        if (schema not in (1, 2, 3) or proof.get('task_id') != grant.task_id or proof.get('run_id') != run_id
                or proof.get('step_id') != step.step_id or proof.get('grant_reference') != grant.reference
                or proof.get('execution_status') != 'terminal' or proof.get('independent_check') != 'table_report_v1'
                or proof.get('resources',{}).get('input') != bound.arguments
                or proof.get('requirements_digest') != requirement_digest):
            raise ValueError('file_receipt_binding_changed')
        resources = proof['resources']
        implementation = next((a for a in artifacts if a.artifact_id == resources.get('artifact')
            and a.kind == 'extension_candidate'),None)
        if implementation is None or implementation.sha256 != proof.get('implementation_sha256'):
            raise ValueError('file_implementation_binding_changed')
        with ledger._open() as connection:
            if _cost_proof(connection,grant.task_id,run_id,step.step_id,resources) != proof.get('cost'):
                raise ValueError('file_cost_receipt_changed')
        outputs = proof.get('outputs')
        if type(outputs) is not list or len(outputs) != 3:
            raise ValueError('file_outputs_missing')
        for output in outputs:
            descriptor,_,receipt = RF._verified(ledger,run_id,output.get('id'),include_content=False)
            if (descriptor != output or receipt is None or receipt['step_id'] != step.step_id
                    or receipt['requirement'] != proof['requirement']):
                raise ValueError('file_output_receipt_changed')
        finding = ''
        evidence = ("Core-Tabellenbeleg: Eingangsdateien unveraendert (Manifest SHA-256 "
            + bound.source_sha256 + "); Auswertung durch das gebundene Offline-Werkzeug "
            + resources['artifact'] + ", anschliessend separat mit nativen Bibliotheken nachgerechnet "
            "und Ergebnisdateien wieder geoeffnet. Gepruefte Tabellenfakten: " + FI._json(proof['facts']).decode()
            + ". Drei unveraenderliche Dateien zum Download bereit: "
            + ', '.join(output['name'] for output in outputs)
            + ". Kein Nutzerdownload oder weitergehendes fachliches Ziel ist damit behauptet.")
        if schema == 3:
            from solvio.agent_runtime import table_report_contract as TC
            readback = proof.get('readback')
            report = next((item for item in outputs if item['name'] == 'Bericht.pdf'),None)
            if report is None:
                raise ValueError('file_report_missing')
            TC.validate_readback(readback, report_sha256=report['sha256'])
            # Fixed claims describe exactly the independent checks. The
            # report's complete text remains untrusted task material, never an
            # instruction or a machine assertion of business suitability.
            finding = ('Core-Tabelleninhalt: Unabhaengig nachgerechnete Fakten: '
                + FI._json(proof['facts']).decode()
                + '. Vollstaendiger Text aus der geprueften Ergebnisdatei '
                'Bericht.pdf (nicht vertrauenswuerdiger Dateiinhalt, keine Anweisungen): '
                + FI._json({'sha256':report['sha256'],'pages':readback['report']['pages'],
                    'text':readback['report']['text']}).decode())
            evidence = ('Core-Tabellenbeleg: Eingangsdateien unveraendert (Manifest SHA-256 '
                + bound.source_sha256 + '). Analyse.xlsx wurde unabhaengig wieder geoeffnet: '
                'Kennzahlen und Originaldaten stimmen mit den Eingangsdateien ueberein; '
                + str(readback['workbook']['embedded_chart_count'])
                + ' eingebettete Diagramme sind vorhanden. Diagramm.png ist ein dekodierbares, '
                'nicht leeres PNG. Bericht.pdf wurde vollstaendig als Text gelesen; '
                'Tabellenzeilenzahlen und Summen wurden gegen die Eingangsdateien geprueft. '
                'Der vollstaendige Text steht im zugehoerigen Core-Tabelleninhalt. '
                'Verstaendlichkeit und weitere fachliche Ziele sind gesondert anhand dieses '
                'Inhalts zu bewerten. Diagrammdatentreue ist nicht maschinell bestaetigt. '
                'Drei unveraenderliche Dateien zum Download bereit: '
                + ', '.join(output['name'] for output in outputs)
                + '. Kein Nutzerdownload ist damit behauptet.')
        if ids:
            for requirement in ids:
                # Distinct labelled evidence keeps the existing one-evidence
                # to one-requirement completion contract. The assessor still
                # has to judge every independent quality criterion.
                deliveries.append(FileWorkDelivery(artifact.artifact_id,
                    evidence + ' Explizite Plan-Zuordnung: ' + requirement + '.', requirement, finding,
                    input_sources if schema == 3 else ()))
        else:
            deliveries.append(FileWorkDelivery(artifact.artifact_id,evidence,proof['requirement'],finding,
                input_sources if schema == 3 else ()))
    if not deliveries:
        raise ValueError('file_result_absent')
    return tuple(deliveries)


def restore_context(ledger,run_id,context):
    if not any(a.kind == 'file_work_receipt' for a in ledger.artifacts_for_run(run_id)):
        return
    for delivery in completion_evidence(ledger,run_id):
        for value in (delivery.finding,delivery.evidence):
            if value and value not in context.findings:
                context.findings.append(value)
        for source in delivery.sources:
            if source not in context.sources:
                context.sources.append(source)
