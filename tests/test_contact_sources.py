"""Imported address books are searchable observations, never confirmed identities."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.communication.bindings import BindingStore
from solvio.communication.sources import ContactSources, contacts_payload, MAX_AGE
from solvio.capabilities.communication import CommunicationCapabilities, register
import test_memory_mutation_endpoint as mutation
from solvio import contacts_endpoint as endpoint
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from solvio.capabilities.router import CapabilityRouter

CONTACT = {'name': 'Alex Winter', 'emails': ['alex@example.test']}


def t_source_refresh_replaces_old_data_without_creating_or_changing_bindings():
    with tempfile.TemporaryDirectory() as d:
        store = BindingStore(os.path.join(d, 'contacts.sqlite3'))
        try:
            source = ContactSources(store, now=lambda: 100)
            before = store.confirm('alex', 'Alex Winter', [{'channel':'gmail','value':'bound@example.test'}], 'fixture')
            require_equal(source.replace('phone-a', json.dumps([CONTACT])), 1)
            hit = source.search('Alex Winter')[0]
            require_equal(hit['confirmed'], False)
            require_equal(hit['handles'][0]['value'], 'alex@example.test')
            require_equal(store.get('alex'), before)
            source.replace('phone-b', json.dumps([{'name':'Bo Sommer','emails':['bo@example.test']}]))
            source.replace('phone-a', '[]')
            require_equal(source.search('Alex Winter'), [])
            require_equal(len(source.search('Bo Sommer')), 1)
        finally: store._db.close()


def t_stale_and_future_imports_are_not_returned_as_current_contacts():
    with tempfile.TemporaryDirectory() as d:
        store = BindingStore(os.path.join(d, 'contacts.sqlite3'))
        try:
            source = ContactSources(store, now=lambda: 100)
            source.replace('phone', json.dumps([CONTACT]))
            source.now = lambda: 99
            require_equal(source.search('Alex Winter'), [])
            source.now = lambda: 101 + MAX_AGE
            require_equal(source.search('Alex Winter'), [])
        finally: store._db.close()


def t_similar_names_are_suggestions_but_addresses_are_never_repaired():
    with tempfile.TemporaryDirectory() as d:
        store = BindingStore(os.path.join(d, 'contacts.sqlite3'))
        try:
            source = ContactSources(store)
            source.replace('phone', json.dumps([CONTACT]))
            hit = source.search('Alex Wintar')[0]
            require_equal(hit['match'], 'similar')
            require_equal(hit['confirmed'], False)
            require_equal(hit['handles'][0]['value'], CONTACT['emails'][0])
        finally: store._db.close()


def t_malformed_or_hidden_addresses_are_rejected_before_storage():
    for contact in [dict(CONTACT, emails=['Alex <alex@example.test>']),
                    dict(CONTACT, emails=['one@example.test,two@example.test']),
                    dict(CONTACT, name='Alex\u202eWinter'), dict(CONTACT, notes='private'),
                    dict(CONTACT, emails=[]), dict(CONTACT, emails=['alex\u200b@example.test'])]:
        try: contacts_payload(json.dumps([contact]))
        except (ValueError, TypeError): pass
        else: raise AssertionError('invalid contact accepted')


async def _wire(access_log=None):
    cp = mutation._ControlPlane(mutation.H.fake_verifier())
    dev = mutation._enroll(cp, 'contact-device')
    temp = tempfile.TemporaryDirectory()
    contacts = CommunicationCapabilities(SimpleNamespace(), BindingStore(os.path.join(temp.name,'c.sqlite3')))
    router = CapabilityRouter()
    register(router, contacts)
    dispatcher = SimpleNamespace(capabilities=router, communication=contacts, agent_runtime=None)
    app = web.Application(); app['control_plane'] = cp
    endpoint.attach(app, SimpleNamespace(dispatcher=dispatcher))
    server = TestServer(app)
    await server.start_server(access_log=access_log)
    client = TestClient(server); await client.start_server()
    return SimpleNamespace(client=client, cp=cp, dev=dev, contacts=contacts, temp=temp)


async def _close(s):
    await s.client.close(); s.contacts.store._db.close(); s.temp.cleanup()


async def _signed(s, *, payload=None, signed_payload=None):
    payload = payload or {'contacts_json': json.dumps([CONTACT])}
    response = await s.client.get(endpoint.API+'/challenge', headers=s.dev.headers())
    challenge = await response.json()
    signature = mutation._assertion_b64(s.dev, core_id=challenge['core_instance_id'],
        nonce=challenge['nonce'], capability='contacts_import', arguments=signed_payload or payload)
    body = {'capability':'contacts_import','arguments':payload,
            'nonce':challenge['nonce'],'assertion_b64':signature}
    response = await s.client.post(endpoint.API, json=body, headers=s.dev.headers())
    return response, body


async def t_app_import_search_and_replay_use_the_real_router_and_proof():
    s = await _wire()
    try:
        response, body = await _signed(s)
        require_equal(response.status, 200)
        result = await response.json(); require(result['ok'], str(result))
        require_equal(result['imported'], 1)
        require_equal(s.contacts.store.get('Alex Winter'), None)
        found = await s.client.post(endpoint.API+'/search', json={'q':'Alex Winter'}, headers=s.dev.headers())
        require_equal((await found.json())['candidates'][0]['confirmed'], False)
        replay = await s.client.post(endpoint.API, json=body, headers=s.dev.headers())
        require_equal(replay.status, 401)
        denied = await s.client.post(endpoint.API+'/search', json={'q':'Alex Winter'})
        require_equal(denied.status, 401)
    finally: await _close(s)


async def t_changed_contact_payload_and_foreign_operations_cannot_pass():
    s = await _wire()
    try:
        response, body = await _signed(s, signed_payload={'contacts_json':'[]'})
        require_equal(response.status, 401)
        require_equal(s.contacts.sources.search('Alex Winter'), [])
        body['capability'] = 'gmail_send_draft'
        response = await s.client.post(endpoint.API, json=body, headers=s.dev.headers())
        require_equal(response.status, 400)
    finally: await _close(s)


async def t_contact_lookup_does_not_put_the_name_in_http_access_logs():
    import io, logging
    stream = io.StringIO()
    logger = logging.Logger('synthetic_contact_access', logging.INFO)
    logger.addHandler(logging.StreamHandler(stream))
    s = await _wire(access_log=logger)
    try:
        response = await s.client.post(endpoint.API+'/search', json={'q':'SyntheticContactCanary'}, headers=s.dev.headers())
        require_equal(response.status, 200)
        await response.json()
        await asyncio.sleep(0.02)
        captured = stream.getvalue()
        require('POST /v1/contacts/search ' in captured, 'access logger was not exercised')
        require('SyntheticContactCanary' not in captured, 'contact query leaked through HTTP access log')
    finally: await _close(s)


async def t_app_attest_contact_confirmation_alone_cannot_write_identity():
    s = await _wire()
    try:
        # The continuation exists, but no biometric approval channel does.
        s.client.server.app['contacts_dispatcher'].agent_runtime = SimpleNamespace(ledger=object())
        response = await s.client.get(endpoint.API+'/challenge', headers=s.dev.headers())
        ch = await response.json()
        args = {'alias':'alex', 'name':'Alex Winter', 'email':'alex@example.test'}
        signature = mutation._assertion_b64(s.dev, core_id=ch['core_instance_id'],
            nonce=ch['nonce'], capability='contacts_confirm', arguments=args)
        response = await s.client.post(endpoint.API, headers=s.dev.headers(), json={
            'capability':'contacts_confirm', 'arguments':args,
            'nonce':ch['nonce'], 'assertion_b64':signature})
        require_equal(response.status, 200)
        result = await response.json()
        require_equal(result['ok'], False)
        require_equal(result['reason'], 'no_approval_channel')
        require(s.contacts.store.get('alex') is None)
    finally: await _close(s)


async def t_contact_search_rechecks_device_after_receiving_the_private_body():
    from unittest.mock import AsyncMock, patch
    s = await _wire()
    try:
        s.contacts.sources.replace('phone', json.dumps([CONTACT]))
        with patch.object(endpoint.proof, '_authed', AsyncMock(side_effect=['device', None])):
            response = await s.client.post(endpoint.API+'/search', json={'q':'Alex Winter'})
        require_equal(response.status, 401)
        require('Alex Winter' not in await response.text())
        malformed = await s.client.post(endpoint.API+'/search', json={'q': []}, headers=s.dev.headers())
        require_equal(malformed.status, 400)
    finally: await _close(s)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
