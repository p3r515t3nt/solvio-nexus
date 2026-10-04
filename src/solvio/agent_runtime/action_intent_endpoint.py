"""Owner answers to one current task question; no second start or loose resume."""
from __future__ import annotations
import base64
import hashlib
from aiohttp import web
from solvio.agent_runtime import action_intent as AI
from solvio.agent_runtime.task_start_proof import TaskStartProofService
from solvio.agent_runtime.task_authority import VerifiedTaskReceipt
from solvio.agent_runtime.task_endpoint import body, response, error
from solvio.security.mobile_approval import browser_sessions as B, protocol as P

DOMAIN_ACTION_ANSWER=b'SOLVIO_APP_ACTION_ANSWER_V1'
TYPE_ACTION_ANSWER='app_action_answer_binding'


def answer_digest(value):
    return hashlib.sha256(P.canonical_bytes(AI.canonical_answer(value))).hexdigest()


def client_data_hash(raw):
    return hashlib.sha256(DOMAIN_ACTION_ANSWER+b'\0'+raw).digest()


class TaskAnswerProofService(TaskStartProofService):
    """Same registered identity/CAS machinery, distinct exact-purpose domain."""
    body_digest=staticmethod(answer_digest)
    assertion_hash=staticmethod(client_data_hash)
    challenge_prefix='app-action-answer:'
    binding_type=TYPE_ACTION_ANSWER
    audit_prefix='app_action_answer'


def _owned(orch,run_id,principal):
    run=orch.ledger.get_run(run_id)
    task=orch.ledger.get_task(run.task_id) if run else None
    return task is not None and task.created_principal==principal


def attach(app,orchestrator):
    async def challenge(request):
        cp=request.app.get('control_plane')
        if cp is None or not request.secure: return error(401,'unauthorized')
        from solvio.security.mobile_approval.gateway import _authed_device
        device_id=await _authed_device(request)
        device=await cp.store.get_device(device_id) if device_id else None
        if device is None: return error(401,'unauthorized')
        orch=orchestrator(request)
        if orch is None: return error(503,'agent_runtime_disabled')
        run_id=request.match_info['run_id']
        if not _owned(orch,run_id,device['principal']): return error(404,'unknown_run')
        try:
            data=await body(request,{'answer'})
            value=AI.canonical_answer(data['answer'])
            if value['run_id']!=run_id: raise ValueError('invalid_answer')
        except (ValueError,TypeError): return error(400,'invalid_answer')
        result=await TaskAnswerProofService(cp).issue(device_id=device_id,
            transport_cred=request.headers.get('X-Transport-Cred',''),task_body=value)
        return response(result.as_dict()) if result else error(401,'unauthorized')

    async def answer(request):
        if not request.secure: return error(401,'unauthorized')
        browser=await B.actor(request,mutating=True)
        if browser is None and B.has_credentials(request): return error(401,'unauthorized')
        run_id=request.match_info['run_id']
        try:
            if browser:
                data=await body(request,{'question_id','expected_revision','expected_digest','answer','client_request_id'})
                value=AI.canonical_answer({'run_id':run_id,**data})
                principal=browser.principal
                receipt=VerifiedTaskReceipt('dashboard_session','browser:'+browser.session_id+':'+value['client_request_id'],principal)
            else:
                cp=request.app.get('control_plane')
                if cp is None: return error(401,'unauthorized')
                data=await body(request,{'answer','proof'})
                value=AI.canonical_answer(data['answer'])
                if value['run_id']!=run_id: raise ValueError('invalid_answer')
                proof=data['proof']
                if type(proof) is not dict or set(proof)!={'nonce','assertion_b64'}: raise ValueError('invalid_proof')
                actor=await TaskAnswerProofService(cp).verify(device_id=request.headers.get('X-Device-Id',''),
                    nonce=proof['nonce'],task_body=value,assertion=base64.b64decode(proof['assertion_b64'],validate=True))
                if actor is None: return error(401,'unauthorized')
                principal=actor.principal
                receipt=VerifiedTaskReceipt('app_session','app-answer:'+actor.nonce,principal)
        except (ValueError,TypeError,KeyError): return error(400 if browser else 401,'invalid_answer' if browser else 'unauthorized')
        orch=orchestrator(request)
        if orch is None: return error(503,'agent_runtime_disabled')
        if not _owned(orch,run_id,principal): return error(404,'unknown_run')
        try:
            result=AI.answer(orch.ledger,value,receipt)
        except (ValueError,TypeError,KeyError) as exc:
            reason=str(exc) if str(exc) in {'stale_question','invalid_answer'} else 'stale_question'
            return response({'error':reason,'reason':('Diese Frage ist inzwischen beantwortet oder geändert. Bitte lade den Auftrag erneut.' if reason=='stale_question' else 'Diese Angabe passt noch nicht zur Frage. Bitte prüfe sie.')},409)
        return response({'action_intent':result})

    app.add_routes([web.post('/v1/agent/runs/{run_id}/action-answer/challenge',challenge),
                    web.post('/v1/agent/runs/{run_id}/action-answer',answer)])
