"""Native enrolled-device HA discovery, public TLS and only temporary partners."""
from __future__ import annotations
import os
import sys
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','src'))
sys.path.insert(0,os.path.dirname(__file__))
from _guard import enforce_assertions,require,require_equal
enforce_assertions()
import mobile_attest_helper as H
from test_agent_action_resources import read_world,path
from solvio.capabilities.policy import OriginClass
from solvio.security.mobile_approval import browser_sessions as B


async def app(w, *, principal='local-owner'):
    device=await H.enroll_attested(w.cp,principal=principal,transport_cred='fixture-app-transport')
    client=await w.new_client()
    return client,device,{'X-Device-Id':device.device_id,'X-Transport-Cred':'fixture-app-transport'}


async def t_registered_app_reads_same_exposed_catalogue_without_a_task_or_effect():
    async with read_world() as w:
        client,device,headers=await app(w)
        services=await client.get('/v1/agent/action-services',headers=headers)
        require_equal(services.status,200)
        require(any(r['account']==w.account for r in (await services.json())['services']))
        response=await client.get(path(w),headers=headers)
        require_equal(response.status,200,await response.text())
        body=await response.json()
        require_equal(body['account'],w.account)
        require_equal([r['target'] for r in body['items']],[{'entity_id':'light.fixture'}])
        require_equal(body['items'][0]['operations'],['set_state','set_brightness'])
        require_equal(response.headers['Cache-Control'],'no-store')
        require(all(c==(OriginClass.TRUSTED_INTERACTIVE_APP,'ha_list_devices') for c in w.native.transport.contexts))
        require_equal(w.native.transport.mutations,[])
        require_equal(w.ledger.recent_runs(),[])
        require_equal(w.calls(),[])
        # Read credentials never become the fresh App Attest task proof.
        start=await client.post('/v1/agent/tasks',json={'task':w.body},headers=headers)
        require_equal(start.status,401)
        require_equal(w.ledger.recent_runs(),[])


async def t_missing_wrong_revoked_and_failing_browser_identity_do_not_fall_back():
    async with read_world() as w:
        client,device,headers=await app(w)
        for wire in ({},dict(headers,**{'X-Transport-Cred':'wrong'}),dict(headers,**{'X-Device-Id':'unknown'})):
            require_equal((await client.get(path(w),headers=wire)).status,401)
        require_equal((await client.get(path(w),headers=headers|{'Cookie':B.COOKIE_NAME+'=invalid'})).status,401)
        await w.cp.revoke_device(device.device_id)
        require_equal((await client.get(path(w),headers=headers)).status,401)
        require_equal(w.native.transport.calls,[])
        require_equal(w.native.transport.mutations,[])


async def t_other_registered_principal_is_not_the_configured_home_owner():
    async with read_world() as w:
        client,device,headers=await app(w,principal='another-owner')
        require_equal((await client.get(path(w),headers=headers)).status,403)
        require_equal(w.native.transport.calls,[])


async def t_native_identity_revoked_during_read_receives_no_catalogue():
    async with read_world() as w:
        client,device,headers=await app(w)
        revoked=False
        async def revoke(method,url):
            nonlocal revoked
            if not revoked:
                revoked=True
                await w.cp.revoke_device(device.device_id)
        w.native.transport.before_response=revoke
        response=await client.get(path(w),headers=headers)
        require_equal(response.status,401)
        require_equal(await response.json(),{'error':'unauthorized'})
        require_equal(w.native.transport.mutations,[])
        require_equal(w.ledger.recent_runs(),[])


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
