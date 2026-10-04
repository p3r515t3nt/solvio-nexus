"""Synthetic APNs delivery; never sends a real notification or loads real keys."""
import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.proactive.store import ProactiveStore
from solvio.proactive.push import PushDelivery
from solvio.integrations.apple_push import ApplePush, provider_token, PAYLOAD
from solvio import push_endpoint as endpoint
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import test_memory_mutation_endpoint as mutation


async def t_delivery_is_generic_deduplicated_and_never_retries_unknown_outcome():
    with tempfile.TemporaryDirectory() as temp:
        store = ProactiveStore(os.path.join(temp, 'p.sqlite3'))
        sender = SimpleNamespace(configured=True, send=AsyncMock(return_value='unknown'))
        cp = SimpleNamespace(store=SimpleNamespace(get_device=AsyncMock(return_value={'principal':'owner'}),
             list_pending=AsyncMock(return_value=[])))
        delivery = PushDelivery(store, cp, None, sender=sender)
        delivery.eligible = AsyncMock(return_value=True)
        await delivery.register('phone', 'a'*64, 'development')
        await store.add_item({'notification_id':'n1','summary':'Private mail text', 'fingerprint':'f1'})
        await delivery.poll(); await delivery.poll()
        require_equal(sender.send.await_count, 1)
        require('Private mail text' not in json.dumps(PAYLOAD))
        await store.mark_read('n1'); await delivery.poll()
        require_equal(sender.send.await_count, 1)


async def t_revoked_device_is_removed_before_any_delivery():
    with tempfile.TemporaryDirectory() as temp:
        store = ProactiveStore(os.path.join(temp, 'p.sqlite3'))
        sender = SimpleNamespace(configured=True, send=AsyncMock())
        delivery = PushDelivery(store, None, None, sender=sender)
        delivery.eligible = AsyncMock(return_value=False)
        await delivery.register('phone', 'a'*64, 'development')
        await delivery.poll()
        require_equal(sender.send.await_count, 0)
        with store._open() as db: require_equal(db.execute('SELECT COUNT(*) FROM push_devices').fetchone()[0], 0)


async def t_unregistered_token_deleted_but_new_token_not_deleted_by_old_response():
    with tempfile.TemporaryDirectory() as temp:
        store = ProactiveStore(os.path.join(temp,'p.sqlite3'))
        cp = SimpleNamespace(store=SimpleNamespace(get_device=AsyncMock(return_value={'principal':'owner'}),
             list_pending=AsyncMock(return_value=[{'approval_id':'ap1'}])))
        sender = SimpleNamespace(configured=True)
        delivery = PushDelivery(store, cp, None, sender=sender)
        delivery.eligible = AsyncMock(return_value=True)
        started = asyncio.Event(); release = asyncio.Event()
        async def send(token, env):
            started.set(); await release.wait()
            return 'unregistered'
        sender.send=send
        await delivery.register('phone','a'*64,'development')
        polling = asyncio.create_task(delivery.poll()); await started.wait()
        rotating = asyncio.create_task(delivery.register('phone', 'b'*64, 'development'))
        await asyncio.sleep(0); require(not rotating.done())
        release.set(); await polling; await rotating
        with store._open() as db: require_equal(db.execute('SELECT token FROM push_devices').fetchone()[0], 'b'*64)


async def t_confirmed_disable_during_final_authority_check_prevents_send():
    with tempfile.TemporaryDirectory() as temp:
        store = ProactiveStore(os.path.join(temp, 'p.sqlite3'))
        cp = SimpleNamespace(store=SimpleNamespace(get_device=AsyncMock(return_value={'principal':'owner'}),
             list_pending=AsyncMock(return_value=[{'approval_id':'ap1'}])))
        sender = SimpleNamespace(configured=True, send=AsyncMock(return_value='accepted'))
        delivery = PushDelivery(store, cp, None, sender=sender)
        calls = 0
        async def eligible(device):
            nonlocal calls
            calls += 1
            if calls == 2:
                await delivery.register(device, '', 'development')
            return True
        delivery.eligible = eligible
        await delivery.register('phone','a'*64,'development')
        await delivery.poll()
        require_equal(sender.send.await_count, 0)


def t_jwt_uses_es256_and_only_expected_claims():
    from cryptography.hazmat.primitives.asymmetric import ec,utils
    from cryptography.hazmat.primitives import serialization,hashes
    key=ec.generate_private_key(ec.SECP256R1())
    pem=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
    jwt=provider_token(json.dumps({'team_id':'A'*10,'key_id':'B'*10,'private_key':pem}),12345)
    head,body,sig=jwt.split('.')
    def decode(s): return base64.urlsafe_b64decode(s+'='*((4-len(s)%4)%4))
    require_equal(json.loads(decode(body)),{'iss':'A'*10,'iat':12345})
    require_equal(json.loads(decode(head)),{'alg':'ES256','kid':'B'*10})
    signature=decode(sig); require_equal(len(signature),64)
    der=utils.encode_dss_signature(int.from_bytes(signature[:32],'big'),int.from_bytes(signature[32:],'big'))
    key.public_key().verify(der,(head+'.'+body).encode(),ec.ECDSA(hashes.SHA256()))


async def t_sender_rejects_injection_and_never_uses_an_unconfigured_broker():
    transport=AsyncMock()
    sender=ApplePush(None,transport=transport)
    require_equal(await sender.send('a'*64,'development'),'not_configured')
    require_equal(await sender.send('a'*64+'\nheader=bad','development'),'invalid_registration')
    require_equal(await sender.send('a'*64,'http://evil.test'),'invalid_registration')
    require_equal(transport.await_count,0)


async def t_registration_requires_proof_is_device_bound_and_cannot_replay():
    cp=mutation._ControlPlane(mutation.H.fake_verifier()); dev=mutation._enroll(cp,'push-phone')
    with tempfile.TemporaryDirectory() as temp:
        dispatcher=SimpleNamespace(proactive_store=ProactiveStore(os.path.join(temp,'p.sqlite3')),secret_broker=None)
        app=web.Application(); app['control_plane']=cp
        endpoint.attach(app,SimpleNamespace(dispatcher=dispatcher))
        client=TestClient(TestServer(app)); await client.start_server()
        try:
            response=await client.get(endpoint.API+'/challenge'); require_equal(response.status,401)
            response=await client.get(endpoint.API+'/challenge',headers=dev.headers()); ch=await response.json()
            args={'token':'a'*64,'environment':'development'}
            assertion=mutation._assertion_b64(dev,core_id=ch['core_instance_id'],nonce=ch['nonce'],capability='push_register',arguments=args)
            payload={'capability':'push_register','arguments':args,'nonce':ch['nonce'],'assertion_b64':assertion}
            response=await client.post(endpoint.API,json=payload,headers=dev.headers()); require_equal(response.status,200)
            require_equal((await response.json())['configured'],False)
            response=await client.post(endpoint.API,json=payload,headers=dev.headers()); require_equal(response.status,401)
            with dispatcher.proactive_store._open() as db:
                require_equal(db.execute('SELECT COUNT(*) FROM push_devices').fetchone()[0],1)
            payload['capability']='gmail_send_draft'
            response=await client.post(endpoint.API,json=payload,headers=dev.headers()); require_equal(response.status,400)
        finally: await client.close()


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
