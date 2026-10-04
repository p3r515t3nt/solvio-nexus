"""Two deliberate contact actions, reusing the app's existing mutation proof.

Separate nonce pool and closed operation names prevent cross-endpoint replay.
Imports remain unconfirmed data; identity changes go through the unchanged
communication_confirm_binding capability, Face ID and the existing pending ledger.
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from aiohttp import web

from solvio import memory_mutation_endpoint as proof
from solvio import voice_session_proof as VSP
from solvio.capabilities.contract import ArgumentSource
from solvio.capabilities.policy import OriginClass
from solvio.contracts.trust import TrustContext, TrustLevel
from solvio.communication.sources import contacts_payload

API = '/v1/contacts'
OPERATIONS = {'contacts_import': {'contacts_json'},
              'contacts_confirm': {'alias', 'name', 'email'}}


def nonces(request):
    return request.app.setdefault('contacts_nonces', VSP.SessionNonces(ttl=60))


async def challenge(request):
    device = await proof._authed(request)
    if not device:
        return proof._err(401, 'unauthorized')
    cp = request.app.get('control_plane')
    if not cp or not cp.core_instance_id:
        return proof._err(503, 'unavailable')
    return web.json_response({'nonce': nonces(request).issue(device),
                              'core_instance_id': cp.core_instance_id})


async def mutate(request):
    device = await proof._authed(request)
    if not device:
        return proof._err(401, 'unauthorized')
    try:
        body = await request.json()
        operation, arguments = body['capability'], body['arguments']
        if (operation not in OPERATIONS or not isinstance(arguments, dict)
                or set(arguments) != OPERATIONS[operation]
                or any(not isinstance(v, str) for v in arguments.values())):
            return proof._err(400, 'invalid_contact_operation')
        if operation == 'contacts_import':
            contacts_payload(arguments['contacts_json'])
        else:
            contacts_payload(json.dumps([
                {'name': arguments['name'], 'emails': [arguments['email']]}]))
            if not 1 <= len(arguments['alias'].strip()) <= 200:
                return proof._err(400, 'invalid_contact_alias')
        assertion = base64.b64decode(body['assertion_b64'], validate=True)
        nonce = body['nonce']
        if not isinstance(nonce, str):
            return proof._err(401, 'invalid_proof')
    except (KeyError, ValueError, TypeError):
        return proof._err(400, 'invalid_contact_operation')
    cp = request.app.get('control_plane')
    if cp is None or not nonces(request).consume(nonce, device):
        return proof._err(401, 'invalid_proof')
    if not await proof.verify_mutation_proof(cp, device_id=device,
            core_instance_id=cp.core_instance_id, nonce=nonce,
            payload_digest=proof.payload_sha256(operation, arguments), assertion=assertion):
        return proof._err(401, 'invalid_proof')
    if await proof._authed(request) != device:
        return proof._err(401, 'unauthorized')
    dispatcher = request.app['contacts_dispatcher']
    runtime = getattr(dispatcher, 'agent_runtime', None)
    ledger = getattr(runtime, 'ledger', None)
    if operation == 'contacts_confirm' and ledger is None:
        return proof._err(503, 'contact_continuation_unavailable')
    capability = ('communication_import_contacts' if operation == 'contacts_import'
                  else 'communication_confirm_binding')
    args = ({'source_device': device, 'contacts_json': arguments['contacts_json']}
            if operation == 'contacts_import' else {
                'alias': arguments['alias'], 'display_name': arguments['name'],
                'handles': [{'channel': 'gmail', 'value': arguments['email']}],
                'source': 'owner_contact_selection'})
    principal = 'iphone-' + device[:12] + '-contacts'
    result = await dispatcher.capabilities.execute(capability, args,
        trust=TrustContext(TrustLevel.USER_DIRECT, user_authorized=True),
        provenance={k: ArgumentSource.TRUSTED_CONTEXT for k in args},
        principal=principal, origin=OriginClass.TRUSTED_INTERACTIVE_APP, commanded=True)
    if operation == 'contacts_confirm':
        from solvio.tools.agent_capability_tools import remember_pending_start
        remember_pending_start(ledger, capability=capability, result=result, arguments=args,
            context=SimpleNamespace(principal=principal,
                origin=OriginClass.TRUSTED_INTERACTIVE_APP, commanded=True))
        if result.outcome.value == 'approval_required':
            request_id = str((result.data or {}).get('request_id') or '')
            try:
                pending = ledger.get_pending_start(request_id)
                retained = (pending and pending['capability'] == capability
                            and pending['arguments'] == args and pending['principal'] == principal)
            except Exception:
                retained = False
            if not retained:
                await dispatcher.capabilities._abandon(request_id, 'contact_continuation_unavailable')
                return proof._err(503, 'contact_continuation_unavailable')
    return web.json_response({'ok': result.succeeded, 'outcome': result.outcome.value,
        'reason': result.reason, 'human_message': result.human_message,
        'imported': (result.data or {}).get('imported'),
        'request_id': (result.data or {}).get('request_id')})


async def lookup(request):
    device = await proof._authed(request)
    if not device:
        return proof._err(401, 'unauthorized')
    # Names belong in the body, never in HTTP access-log URLs.
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {'q'} or not isinstance(body['q'], str):
            return proof._err(400, 'invalid_query')
        query = body['q'].strip()
    except (ValueError, TypeError):
        return proof._err(400, 'invalid_query')
    if not 2 <= len(query) <= 200:
        return proof._err(400, 'invalid_query')
    # Reading a request body may await the network; a revoked device stays revoked.
    if await proof._authed(request) != device:
        return proof._err(401, 'unauthorized')
    contacts = request.app['contacts_dispatcher'].communication
    # This view searches only local contacts, never mailbox messages.
    binding = contacts.store.get(query)
    matches = [binding] if binding else contacts.store.matching_name(query)
    return web.json_response({'bindings': matches, 'candidates': contacts.sources.search(query)})


def attach(app, server):
    dispatcher = server.dispatcher
    if getattr(dispatcher, 'communication', None) is None:
        return
    app['contacts_dispatcher'] = dispatcher
    app.router.add_get(API + '/challenge', challenge)
    app.router.add_post(API, mutate)
    app.router.add_post(API + '/search', lookup)
