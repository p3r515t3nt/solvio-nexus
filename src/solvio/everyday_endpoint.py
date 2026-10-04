"""A small native setup form for existing background tasks, with real Face ID."""
import base64
import json
from types import SimpleNamespace
from aiohttp import web
from solvio import memory_mutation_endpoint as proof
from solvio import voice_session_proof as VSP
from solvio.capabilities.policy import OriginClass
from solvio.capabilities.contract import ArgumentSource
from solvio.contracts.trust import TrustContext, TrustLevel
from solvio.proactive.everyday import ACTIONS
from solvio.tools.agent_capability_tools import remember_pending_start

API='/v1/everyday'


def nonces(request):
    return request.app.setdefault('everyday_nonces',VSP.SessionNonces(ttl=60))


async def challenge(request):
    device=await proof._authed(request)
    if not device:return proof._err(401,'unauthorized')
    return web.json_response({'nonce':nonces(request).issue(device),
        'core_instance_id':request.app['control_plane'].core_instance_id})


async def create(request):
    device=await proof._authed(request)
    if not device:return proof._err(401,'unauthorized')
    try:
        body=await request.json();operation=body['capability'];args=body['arguments']
        if operation!='everyday_schedule' or not isinstance(args,dict) or set(args)!={'schedule_json'} or not isinstance(args['schedule_json'],str):raise ValueError()
        schedule=json.loads(args['schedule_json'])
        if (not isinstance(schedule,dict) or set(schedule)!={'titel','wann','aktion','argumente'}
            or schedule['aktion'] not in ACTIONS or any(not isinstance(schedule[k],str) or len(schedule[k])>120 for k in ('titel','wann'))):raise ValueError()
        nonce=body['nonce'];assertion=base64.b64decode(body['assertion_b64'],validate=True)
        if not isinstance(nonce,str):raise ValueError()
    except (KeyError,ValueError,TypeError):return proof._err(400,'invalid_schedule')
    cp=request.app['control_plane']
    if not nonces(request).consume(nonce,device) or not await proof.verify_mutation_proof(cp,device_id=device,
        core_instance_id=cp.core_instance_id,nonce=nonce,payload_digest=proof.payload_sha256(operation,args),assertion=assertion):
        return proof._err(401,'invalid_proof')
    if await proof._authed(request)!=device:return proof._err(401,'unauthorized')
    dispatcher=request.app['everyday_dispatcher'];ledger=getattr(getattr(dispatcher,'agent_runtime',None),'ledger',None)
    if ledger is None:return proof._err(503,'continuation_unavailable')
    dev=await cp.store.get_device(device)
    if not dev or not dev.get('principal'):return proof._err(401,'unauthorized')
    principal=dev['principal']
    result=await dispatcher.capabilities.execute('background_create',schedule,
        trust=TrustContext(TrustLevel.USER_DIRECT,user_authorized=True),
        provenance={k:ArgumentSource.TRUSTED_CONTEXT for k in schedule},
        principal=principal,origin=OriginClass.TRUSTED_INTERACTIVE_APP,commanded=True)
    remember_pending_start(ledger,capability='background_create',result=result,arguments=schedule,
        context=SimpleNamespace(principal=principal,origin=OriginClass.TRUSTED_INTERACTIVE_APP,commanded=True))
    request_id=(result.data or {}).get('request_id')
    if result.outcome.value=='approval_required':
        retained=ledger.get_pending_start(request_id)
        if not retained or retained['arguments']!=schedule or retained['principal']!=principal:
            await dispatcher.capabilities._abandon(request_id,'continuation_unavailable')
            return proof._err(503,'continuation_unavailable')
    return web.json_response({'ok':result.succeeded,'request_id':request_id,'human_message':result.human_message})


async def search_mail(request):
    device = await proof._authed(request)
    if not device: return proof._err(401, 'unauthorized')
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {'query'} or not isinstance(body['query'], str): raise ValueError()
        query = body['query'].strip()
        if not 2 <= len(query) <= 200: raise ValueError()
    except (ValueError, TypeError): return proof._err(400, 'invalid_query')
    if await proof._authed(request) != device: return proof._err(401, 'unauthorized')
    cp = request.app['control_plane']; dev = await cp.store.get_device(device)
    if not dev: return proof._err(401, 'unauthorized')
    dispatcher = request.app['everyday_dispatcher']
    result = await dispatcher.capabilities.execute('gmail_search',
        {'query': 'in:sent -in:drafts (' + query + ')', 'limit': 10},
        trust=TrustContext(TrustLevel.USER_DIRECT, user_authorized=True),
        provenance={'query': ArgumentSource.TRUSTED_CONTEXT, 'limit': ArgumentSource.TRUSTED_CONTEXT},
        principal=dev['principal'], origin=OriginClass.TRUSTED_INTERACTIVE_APP, commanded=True)
    if not result.succeeded: return proof._err(503, 'mail_search_unconfirmed')
    from solvio.capabilities.task_read import task_material
    rows = (result.data or {}).get('messages')
    if not isinstance(rows, list): return proof._err(503, 'mail_search_unconfirmed')
    messages = []
    for row in rows[:10]:
        if isinstance(row, dict) and row.get('sent') is True and row.get('draft') is False:
            messages.append({k: row.get(k, '') for k in ('id', 'thread_id', 'subject', 'date')})
    return web.json_response({'messages': task_material(messages)})


def attach(app,server):
    app['everyday_dispatcher']=server.dispatcher
    app.router.add_get(API+'/challenge',challenge)
    app.router.add_post(API,create)
    app.router.add_post(API+'/mail/search',search_mail)
