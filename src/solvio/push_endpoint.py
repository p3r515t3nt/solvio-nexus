"""Native device opt-in for generic APNs hints; no model-callable sender."""
from __future__ import annotations
import asyncio
import base64
import re
from aiohttp import web
from solvio import memory_mutation_endpoint as proof
from solvio import voice_session_proof as VSP
from solvio.proactive.push import PushDelivery

API = '/v1/push'


def nonces(request):
    return request.app.setdefault('push_nonces', VSP.SessionNonces(ttl=60))


async def challenge(request):
    device = await proof._authed(request)
    if not device:
        return proof._err(401, 'unauthorized')
    return web.json_response({'nonce': nonces(request).issue(device),
        'core_instance_id': request.app['control_plane'].core_instance_id,
        'configured': request.app['push_delivery'].sender.configured})


async def register(request):
    device = await proof._authed(request)
    if not device:
        return proof._err(401, 'unauthorized')
    try:
        body = await request.json()
        operation, args = body['capability'], body['arguments']
        if operation != 'push_register' or not isinstance(args, dict) or set(args) != {'token', 'environment'}:
            raise ValueError()
        if (not isinstance(args['token'], str) or not isinstance(args['environment'], str)
                or args['environment'] not in ('development', 'production')
                or (args['token'] and not re.fullmatch('[0-9a-f]{64,200}', args['token']))):
            raise ValueError()
        assertion = base64.b64decode(body['assertion_b64'], validate=True)
        nonce = body['nonce']
        if not isinstance(nonce, str):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        return proof._err(400, 'invalid_registration')
    cp = request.app['control_plane']
    if (not nonces(request).consume(nonce, device)
            or not await proof.verify_mutation_proof(cp, device_id=device,
                core_instance_id=cp.core_instance_id, nonce=nonce,
                payload_digest=proof.payload_sha256(operation, args), assertion=assertion)):
        return proof._err(401, 'invalid_proof')
    if await proof._authed(request) != device:
        return proof._err(401, 'unauthorized')
    service = request.app['push_delivery']
    await service.register(device, args['token'], args['environment'])
    return web.json_response({'ok': True, 'configured': service.sender.configured})


def attach(app, server):
    dispatcher = server.dispatcher
    store = getattr(dispatcher, 'proactive_store', None)
    if store is None:
        return
    app['push_delivery'] = PushDelivery(store, app['control_plane'], getattr(dispatcher, 'secret_broker', None))
    app.router.add_get(API + '/challenge', challenge)
    app.router.add_post(API, register)
    async def lifecycle(app):
        task = asyncio.create_task(app['push_delivery'].run())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    app.cleanup_ctx.append(lifecycle)
