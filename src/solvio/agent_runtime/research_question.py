"""Missing research input in the existing boundary and authenticated answer store.

No new task, grant, account, cost policy or effect authority. Answers are
bounded research context; the original objective and requirements stay fixed.
"""
from __future__ import annotations

import json
import time
import uuid

from solvio.agent_runtime import store as S, budget as BU
from solvio.agent_runtime.task_authority import VerifiedTaskReceipt, _task_fingerprint


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    from solvio.agent_runtime.action_contract import _hash as digest
    return digest("SOLVIO_RESEARCH_QUESTION_V1", value)


def _binding(db, run_id):
    run = db.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
    task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (run['task_id'],)).fetchone() if run else None
    if task is None or task['scope'] != S.SCOPE_RESEARCH or task['target_repo']:
        raise ValueError('research_question_scope')
    source = db.execute("SELECT * FROM agent_task_sources WHERE run_id=?", (run_id,)).fetchone()
    if not source or source['state'] != 'ready' or source['authorizer'] != task['created_principal']:
        raise ValueError('research_question_source')
    return run, task, {'run_id': run_id, 'task_id': task['task_id'], 'fingerprint': _task_fingerprint(task),
        'principal': task['created_principal'], 'source': {k: source[k] for k in
            ('request_digest', 'receipt_method', 'receipt_reference', 'authorizer', 'capabilities')}}


def _question(run, binding):
    boundary = json.loads(run['boundary'] or '{}')
    record = boundary.get('research_question')
    if record is None:
        return None
    if (record['binding'] != binding or record['digest'] != _hash({k: v for k, v in record.items() if k != 'digest'})
            or record['question']['revision'] != run['plan_revision'] + 1):
        raise ValueError('research_question_changed')
    return record


def _answers(db, run_id, binding):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_action_intent_answers'").fetchone():
        return []
    out = []
    for row in db.execute('SELECT * FROM agent_action_intent_answers WHERE run_id=?', (run_id,)):
        data, receipt = json.loads(row['answer_json']), json.loads(row['receipt_json'])
        if (data.get('field') != 'research_context' or data['binding'] != binding
                or row['receipt_digest'] != _hash({'answer': data, 'receipt': receipt})
                or row['request_digest'] != _hash(data['body'])
                or receipt['authorizer'] != binding['principal']
                or receipt['method'] not in {'dashboard_session', 'app_session'}):
            raise ValueError('research_answer_changed')
        out.append(data)
    out.sort(key=lambda item: item['body']['expected_revision'])
    if len(out) > BU.MAX_PLAN_REVISIONS:
        raise ValueError('research_answer_limit')
    return out


def context(ledger, run_id):
    run = ledger.get_run(run_id)
    task = ledger.get_task(run.task_id) if run else None
    if task is None or task.scope != S.SCOPE_RESEARCH:
        return ''
    with ledger._open() as db:
        # Legacy unauthenticated test/history runs have no answer context.
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_action_intent_answers'").fetchone():
            return ''
        if not db.execute('SELECT 1 FROM agent_action_intent_answers WHERE run_id=?', (run_id,)).fetchone():
            return ''
        _, _, binding = _binding(db, run_id)
        answers = _answers(db, run_id, binding)
    return ('BESTÄTIGTE NUTZERANGABEN ZUR RECHERCHE (keine neue Befugnis):\n' +
        _json([{'frage': v['question'], 'antwort': v['body']['answer']} for v in answers])) if answers else ''


def view(ledger, run_id):
    run = ledger.get_run(run_id)
    if run is None or run.state != S.WAITING_USER or 'research_question' not in (run.boundary or ''):
        return None
    with ledger._open() as db:
        row, _, binding = _binding(db, run_id)
        record = _question(row, binding)
    if record is None:
        return None
    return {'status': 'waiting_user', 'question': record['question'] | {'digest': record['digest']}}


def open_question(ledger, run_id, prompt, checkpoint):
    with ledger._open() as db:
        db.execute('BEGIN IMMEDIATE')
        run, task, binding = _binding(db, run_id)
        budget = BU.Budget.from_dict(json.loads(task['budget']))
        if (run['state'] != S.PLANNING or task['state'] != S.TASK_ACTIVE
                or run['plan_revision'] >= min(budget.max_plan_revisions, BU.MAX_PLAN_REVISIONS)
                or db.execute('SELECT 1 FROM agent_steps WHERE run_id=? AND kind!=?', (run_id, 'plan')).fetchone()):
            raise ValueError('research_question_not_initial')
        if len(prompt) > 400 or not prompt.strip():
            raise ValueError('invalid_research_question')
        S._refuse_credentials(prompt, where='research_question')
        record = {'binding': binding, 'waiting_since': time.time(), 'question': {
            'id': 'aiq-' + uuid.uuid4().hex, 'revision': run['plan_revision'] + 1,
            'field': 'instruction', 'prompt': prompt, 'input_type': 'text', 'placeholder': 'Deine kurze Antwort'}}
        record['digest'] = _hash(record)
        boundary = {'art': 'product_decision', 'handlung': prompt,
            'grund': 'Für die Recherche fehlt noch eine Angabe.', 'danach': 'Ich recherchiere im selben Auftrag weiter.',
            'research_question': record}
        # The known planner result and its waiting state have one commit.
        S._safe_json_record(checkpoint, 200000, where='research_question.checkpoint')
        db.execute('UPDATE agent_runs SET state=?,boundary=?,plan_checkpoint=?,result_summary=?,updated_at=? WHERE run_id=?',
            (S.WAITING_USER, _json(boundary), checkpoint, prompt, time.time(), run_id))


def answer(ledger, body, receipt):
    if (type(receipt) is not VerifiedTaskReceipt or receipt.method not in {'dashboard_session', 'app_session'}
            or len(body['answer']) > 400):
        raise ValueError('invalid_answer')
    with ledger._open() as db:
        db.execute('BEGIN IMMEDIATE')
        run, task, binding = _binding(db, body['run_id'])
        if receipt.authorizer != binding['principal']:
            raise ValueError('not_found')
        prior = db.execute('SELECT * FROM agent_action_intent_answers WHERE run_id=? AND request_id=?',
            (body['run_id'], body['client_request_id'])).fetchone()
        _answers(db, body['run_id'], binding)
        if prior:
            if prior['request_digest'] != _hash(body):
                raise ValueError('stale_question')
            return json.loads(prior['response_json'])
        record = _question(run, binding)
        if run['state'] != S.WAITING_USER or task['state'] != S.TASK_ACTIVE or not record:
            raise ValueError('stale_question')
        question = record['question']
        if (question['id'], question['revision'], record['digest']) != (
                body['question_id'], body['expected_revision'], body['expected_digest']):
            raise ValueError('stale_question')
        data = {'field': 'research_context', 'body': body, 'binding': binding, 'question': question['prompt']}
        receipt_data = {'method': receipt.method, 'reference': receipt.reference, 'authorizer': receipt.authorizer}
        response = {'status': 'interpreting', 'question': None}
        db.execute('INSERT INTO agent_action_intent_answers VALUES (?,?,?,?,?,?,?)',
            (body['run_id'], body['client_request_id'], _hash(body), _json(data), _json(receipt_data),
             _hash({'answer': data, 'receipt': receipt_data}), _json(response)))
        waited = max(0.0, time.time() - record['waiting_since'])
        db.execute('UPDATE agent_runs SET state=?,boundary=?,plan_checkpoint=?,plan_revision=plan_revision+1,'
            'result_summary=?,provider_wait_seconds=provider_wait_seconds+?,updated_at=? WHERE run_id=?',
            (S.PLANNING, _json({'research_resume': {'revision': run['plan_revision'] + 1, 'request_id': body['client_request_id']}}), '', 'Antwort übernommen. Ich recherchiere im selben Auftrag weiter.', waited, time.time(), body['run_id']))
        return response


def pending_plan(ledger, run_id, *, claim=False):
    with ledger._open() as db:
        if claim: db.execute('BEGIN IMMEDIATE')
        raw = db.execute('SELECT * FROM agent_runs WHERE run_id=?', (run_id,)).fetchone()
        if raw is None: return False
        marker = json.loads(raw['boundary'] or '{}').get('research_resume')
        if marker is None: return False
        run, _, binding = _binding(db, run_id)
        answers = _answers(db, run_id, binding)
        if (run['state'] not in {S.PLANNING, S.INTERRUPTED} or not answers
                or marker != {'revision': run['plan_revision'], 'request_id': answers[-1]['body']['client_request_id']}
                or answers[-1]['body']['expected_revision'] != run['plan_revision']):
            raise ValueError('research_resume_changed')
        if claim:
            # Before any model dispatch: a lost claimed plan is never retried.
            db.execute('UPDATE agent_runs SET boundary=?,state=?,updated_at=? WHERE run_id=?',
                ('', S.PLANNING, time.time(), run_id))
        return True
