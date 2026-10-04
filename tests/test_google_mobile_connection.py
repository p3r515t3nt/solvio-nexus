"""Mobile hand-off: synthetic account/code only, no Google or production access."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_google_reconnect import world, value, OLD
from test_google_mobile_binding import snapshot
from solvio.secret_vault import google_mobile as M, admin, policy as VP
from solvio.capabilities.secret_vault import SecretVaultCapabilities, register, SPECS
from solvio.capabilities import policy as AP
from solvio.capabilities.contract import ArgumentSource
from solvio.contracts.trust import TrustContext, TrustLevel

EMAIL = 'synthetic@example.invalid'
CODE = 'synthetic-one-use-code'
REFRESH = 'synthetic-mobile-refresh'
SECRET = 'synthetic-mobile-client'


def setup(w, exchange=None):
    folder = w.root / 'mobile'; folder.mkdir(mode=0o700)
    config = folder / 'web.json'
    config.write_text(json.dumps({'web': {'client_id': M.SERVER_CLIENT, 'client_secret': SECRET,
        'token_uri': M.TOKEN_URI, 'auth_uri': 'https://accounts.google.com/o/oauth2/auth'}}))
    config.chmod(0o600)
    calls = []
    async def synthetic(config, code, account_id, email):
        calls.append((code, account_id, email))
        return REFRESH
    mobile = M.GoogleMobileConnection(w.store, config_path=config, exchange=exchange or synthetic)
    args = dict(expected_binding=mobile.preview()['expected_binding'], account_id='synthetic-id',
                account_email=EMAIL, staging_id='synthetic-stage')
    return mobile, args, calls


async def t_complete_exchange_switches_both_values_only_once():
    with world() as w:
        mobile, args, calls = setup(w)
        out = await mobile.activate(args, CODE.encode())
        require(out['verbunden'])
        require_equal(value(w.store, M.CLIENT_REF), SECRET.encode())
        require_equal(value(w.store, M.REFRESH_REF), REFRESH.encode())
        require_equal(w.store.meta_get('google_oauth_client_id'), M.SERVER_CLIENT)
        try: await mobile.activate(args, CODE.encode())
        except M.GoogleConnectionError: pass
        else: raise AssertionError('authorization replay accepted')
        require_equal(len(calls), 1)
        require(all(s not in json.dumps(out) for s in [CODE, SECRET, REFRESH]))


async def t_failure_and_concurrent_disable_preserve_existing_pair():
    for mode in ('provider', 'disable', 'changed_config'):
        with world() as w:
            before = snapshot(w)
            async def exchange(*_):
                if mode == 'provider': raise M.GoogleConnectionError('google_exchange_unconfirmed')
                if mode == 'disable': admin.set_status(secret_ref=M.REFRESH_REF, status=VP.Status.DISABLED, store=w.store)
                else: mobile.config_path.write_text(mobile.config_path.read_text() + ' ')
                return REFRESH
            mobile, args, _ = setup(w, exchange)
            try: await mobile.activate(args, CODE.encode())
            except Exception: pass
            else: raise AssertionError('racing/incomplete login switched access')
            require_equal(value(w.store, M.CLIENT_REF), before[0][M.CLIENT_REF])
            require_equal(value(w.store, M.REFRESH_REF), OLD)
            require_equal(w.store.meta_get('google_oauth_client_id'), '')
            if mode != 'disable': require_equal(snapshot(w), before)
            else: require_equal(w.store.policy(M.REFRESH_REF).status, VP.Status.DISABLED)


async def t_explicit_reconnection_can_reactivate_but_not_expand_rights():
    with world() as w:
        admin.set_status(secret_ref=M.REFRESH_REF, status=VP.Status.REVOKED, store=w.store)
        before = {r: w.store.policy(r) for r in (M.CLIENT_REF, M.REFRESH_REF)}
        mobile, args, _ = setup(w)
        require(not mobile.preview()['currently_active'])
        await mobile.activate(args, CODE.encode())
        for ref, previous in before.items():
            now = w.store.policy(ref)
            require_equal(now.status, VP.Status.ACTIVE)
            require_equal(now.allowed_capabilities, previous.allowed_capabilities)
            require_equal(now.allowed_targets, previous.allowed_targets)
            require_equal(now.allowed_executors, previous.allowed_executors)
            require_equal(now.allow_background, previous.allow_background)
            require_equal(now.requires_user_presence, previous.requires_user_presence)


def t_insecure_setup_and_desktop_mismatch_do_not_open_credentials():
    from solvio.secret_vault import google_reconnect as G
    with world() as w:
        mobile, _, _ = setup(w)
        for mode in ('mode', 'symlink', 'wrong_client'):
            p=mobile.config_path; original=p.read_text()
            if mode == 'mode': p.chmod(0o644)
            if mode == 'symlink': p.rename(p.with_suffix('.saved')); p.symlink_to(p.with_suffix('.saved'))
            if mode == 'wrong_client': p.write_text(original.replace(M.SERVER_CLIENT, 'wrong'))
            try: mobile.preview()
            except M.GoogleConnectionError as exc: require_equal(str(exc), 'google_mobile_setup_unavailable')
            else: raise AssertionError('insecure config accepted')
            if mode == 'symlink': p.unlink(); p.with_suffix('.saved').rename(p)
            p.write_text(original);p.chmod(0o600)
        w.store.meta_set('google_oauth_client_id', M.SERVER_CLIENT)
        with patch.object(M.E, 'unseal', side_effect=AssertionError('must not open')):
            try: G.prepare_existing_desktop(confirmed_client_id='legacy.apps.googleusercontent.com',
                    client_id='legacy.apps.googleusercontent.com', store=w.store)
            except G.ReconnectError as exc: require_equal(str(exc), 'confirmed_desktop_client_mismatch')
            else: raise AssertionError('desktop mixed with mobile secret')


async def invoke(stack, args, origin=AP.OriginClass.TRUSTED_INTERACTIVE_APP):
    return await stack.router.execute('google_connect', args,
        trust=TrustContext(origin_trust=TrustLevel.USER_DIRECT, user_authorized=True),
        provenance={k: ArgumentSource.TRUSTED_CONTEXT for k in args},
        principal='synthetic-owner', origin=origin, commanded=True)


async def t_router_requires_face_id_denial_is_final_and_never_exchanges_early():
    from test_approval_policy_v2_router import _stack, S
    for decision in ('approve', 'deny'):
        with world() as w:
            mobile, args, calls = setup(w)
            caps = SecretVaultCapabilities(store=w.store, google_connection=mobile)
            sid=caps.staging.stage(CODE.encode(), device_id='synthetic-device', payload_sha256=hashlib.sha256(CODE.encode()).hexdigest())
            caps.device_for_staging[sid]='synthetic-device';args['staging_id']=sid
            stack=_stack();register(stack.router,caps)
            result=await invoke(stack,args)
            require_equal(result.outcome.value, 'approval_required')
            require_equal(calls, [])
            row=stack.requests[0];require_equal(row['mode'],'controlled-v1');require_equal(AP.base_class('google_connect', read_only=False), AP.ActionClass.VERY_CRITICAL)
            require(EMAIL in row['task'])
            require(all(s not in json.dumps(row) for s in [CODE, SECRET, REFRESH]))
            if decision=='approve': stack.cp.approve(row['approval_id'])
            else: row['state']=S.DENIED
            result=await invoke(stack,args)
            require_equal(result.succeeded, decision=='approve')
            require_equal(len(calls), 1 if decision=='approve' else 0)
            if decision=='deny':
                require_equal(result.reason,'denied')
                require_equal(len(stack.requests),1)


async def t_background_external_and_unknown_arguments_never_exchange():
    from test_approval_policy_v2_router import _stack
    with world() as w:
        mobile,args,calls=setup(w);caps=SecretVaultCapabilities(store=w.store,google_connection=mobile)
        stack=_stack();register(stack.router,caps)
        for origin in (AP.OriginClass.BACKGROUND_AUTOMATION, AP.OriginClass.EXTERNAL_UNTRUSTED):
            result=await invoke(stack,args,origin)
            require(not result.succeeded);require(result.outcome.value != 'approval_required')
        result=await invoke(stack,{**args,'code':CODE})
        require(not result.succeeded);require_equal(len(stack.requests),0);require_equal(calls,[])


async def t_endpoint_auth_attestation_and_staging_share_existing_path():
    from test_secret_vault_adversarial import _wire, _assertion, EP
    from test_approval_policy_v2_router import _stack
    wire=await _wire()
    try:
        with world() as w:
            mobile,args,calls=setup(w);caps=wire.caps
            caps.google_connection=mobile
            stack=_stack();register(stack.router,caps)
            wire.client.server.app['voice_core_server'].dispatcher.capabilities=stack.router
            r=await wire.client.get(EP.API+'/google');require_equal(r.status,401)
            r=await wire.client.get(EP.API+'/google',headers=wire.dev.headers())
            require_equal(r.status,200);require_equal(r.headers['Cache-Control'],'no-store')
            text = await r.text();require(all(s not in text for s in [CODE,SECRET,REFRESH]))
            args.pop('staging_id'); digest=hashlib.sha256(CODE.encode()).hexdigest()
            r=await wire.client.get(EP.API+'/mutation/challenge',headers=wire.dev.headers());challenge=await r.json()
            body=dict(capability='google_connect',arguments=args,nonce=challenge['nonce'],secret_sha256=digest,
                      secret_b64=base64.b64encode(CODE.encode()).decode(),assertion_b64='')
            r=await wire.client.post(EP.API+'/mutation',json=body,headers=wire.dev.headers());require_equal(r.status,401)
            r=await wire.client.get(EP.API+'/mutation/challenge',headers=wire.dev.headers());challenge=await r.json()
            body['nonce']=challenge['nonce'];body['assertion_b64']=_assertion(wire.dev,core_id=challenge['core_instance_id'],
                nonce=body['nonce'],capability='google_connect',arguments=args,secret_sha256=digest)
            r=await wire.client.post(EP.API+'/mutation',json=body,headers=wire.dev.headers())
            require_equal(r.status,200);result=await r.json();require_equal(result['outcome'],'approval_required')
            require(result['staging_id']);require_equal(calls,[])
            require(all(s not in json.dumps(result) for s in [CODE,SECRET,REFRESH]))
            r=await wire.client.post(EP.API+'/mutation',json=body,headers=wire.dev.headers());require_equal(r.status,401)
            from solvio.security.mobile_approval import store as AS
            stack.requests[0]['state']=AS.DENIED
            stage=result['staging_id'];body.pop('secret_b64');body['staging_id']=stage
            for counter,expected_status in ((2,200),(3,409)):
                r=await wire.client.get(EP.API+'/mutation/challenge',headers=wire.dev.headers());challenge=await r.json()
                body['nonce']=challenge['nonce'];body['assertion_b64']=_assertion(wire.dev,core_id=challenge['core_instance_id'],
                    nonce=body['nonce'],capability='google_connect',arguments=args,secret_sha256=digest,counter=counter)
                r=await wire.client.post(EP.API+'/mutation',json=body,headers=wire.dev.headers())
                require_equal(r.status,expected_status)
                if expected_status==200: require_equal((await r.json())['reason'],'denied')
            require_equal(len(stack.requests),1);require_equal(calls,[])
    finally: await wire.client.close()


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
