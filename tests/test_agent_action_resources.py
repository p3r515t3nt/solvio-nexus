"""Owner HTTPS device discovery through native HA/Vault ports; transport only fake."""
from __future__ import annotations
from contextlib import asynccontextmanager
import os
import sys
from urllib.parse import urlencode

sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','src'))
sys.path.insert(0,os.path.dirname(__file__))
from _guard import enforce_assertions,require,require_equal
enforce_assertions()
from test_agent_action_execution import world
from solvio.capabilities import task_action as TA
from solvio.capabilities.policy import OriginClass
from solvio.secret_vault import admin


def change_credential(w,*,read=True):
    store=w.native.broker.store
    ref=w.native.ha._credential_ref
    policy=store.policy(ref)
    capabilities=tuple(c for c in policy.allowed_capabilities if c!='ha_list_devices')
    if read: capabilities+=('ha_list_devices',)
    admin.add(secret_ref=ref,kind=policy.kind,plaintext=b'synthetic-new-ha-read-fixture',
        allowed_capabilities=capabilities,allowed_targets=policy.allowed_targets,
        allowed_executors=policy.allowed_executors,allow_background=policy.allow_background,
        requires_user_presence=policy.requires_user_presence,replace=True,store=store)
    return TA.account_identity('ha',w.native.ha)


@asynccontextmanager
async def read_world():
    async with world('ha') as w:
        w.account=change_credential(w)
        yield w


def path(w,**changes):
    return '/v1/agent/action-resources?'+urlencode(dict(service='ha',account=w.account,**changes))


def add(w,eid,*,exposed=True,device_class='',name='',state='off'):
    w.native.transport.states[eid]={'entity_id':eid,'state':state,'attributes':{
        'friendly_name':name or eid,'device_class':device_class,'private_attribute':'never-return-me'}}
    w.native.transport.exposed[eid]={'conversation':exposed}


async def t_owner_get_lists_only_exact_safe_exposed_targets_without_a_task_or_write():
    async with read_world() as w:
        add(w,'switch.safe',name='<script>external name</script>',state='on')
        add(w,'switch.garage',device_class='garage')
        add(w,'light.owner_protected')
        add(w,'light.hidden',exposed=False)
        add(w,'lock.door'); add(w,'sensor.temperature')
        w.orch.action_service.security_entities=frozenset({'light.owner_protected'})
        response=await w.client.get(path(w))
        require_equal(response.status,200,str(await response.json()))
        body=await response.json()
        require_equal(body['service'],'ha'); require_equal(body['account'],w.account)
        require_equal(body['truncated'],False)
        items={r['target']['entity_id']:r for r in body['items']}
        require_equal(set(items),{'light.fixture','switch.safe'})
        require_equal(items['light.fixture']['operations'],['set_state','set_brightness'])
        require_equal(items['switch.safe']['operations'],['set_state'])
        require_equal(items['switch.safe']['state'],'on')
        require_equal(items['switch.safe']['name'],'<script>external name</script>')
        require(all(set(r)=={'target','name','area','domain','state','operations'} for r in items.values()))
        require_equal(response.headers['Cache-Control'],'no-store')
        require_equal(len(w.native.transport.calls),2)
        require(all(method=='GET' and url.endswith('/api/states') for method,url in w.native.transport.calls))
        require(all(c==(OriginClass.TRUSTED_DASHBOARD,'ha_list_devices') for c in w.native.transport.contexts))
        require_equal(w.native.transport.mutations,[])
        require_equal(w.ledger.recent_runs(),[])
        require_equal(w.calls(),[])


async def t_unauthenticated_and_other_owner_never_reach_native_discovery():
    async with read_world() as w:
        stranger=await w.new_client()
        require_equal((await stranger.get(path(w))).status,401)
        await w.login(stranger,'another-owner')
        require_equal((await stranger.get(path(w))).status,403)
        require_equal(w.native.transport.calls,[])


async def t_query_is_closed_and_bounded_before_native_access():
    async with read_world() as w:
        for suffix in ('&authority=owner','&service=ha','&account='+w.account,'&limit=0','&limit=101','&limit=-1','&limit=true','&limit=1&limit=2'):
            response=await w.client.get(path(w)+suffix)
            require_equal(response.status,400)
            require_equal((await response.json())['error'],'invalid_action_resources')
        require_equal(w.native.transport.calls,[])
        response=await w.client.get('/v1/agent/action-resources?'+urlencode({'service':'ha','account':'ha-'+'0'*32}))
        require_equal(response.status,409)
        require_equal(w.native.transport.calls,[])


async def t_result_limit_is_explicit_without_truncating_individual_targets():
    async with read_world() as w:
        for n in range(102): add(w,f'light.test_{n:03d}',name=f'Lamp {n:03d}')
        response=await w.client.get(path(w))
        require_equal(response.status,200)
        body=await response.json()
        require_equal(len(body['items']),100); require_equal(body['truncated'],True)
        require(all(r['target']['entity_id'].startswith('light.') for r in body['items']))
        response=await w.client.get(path(w,limit=2))
        body=await response.json()
        require_equal(len(body['items']),2); require_equal(body['truncated'],True)
        require_equal(w.native.transport.mutations,[])


async def t_account_rotation_during_read_discards_the_old_result():
    async with read_world() as w:
        async def rotate(method,url):
            change_credential(w)
        w.native.transport.before_response=rotate
        response=await w.client.get(path(w))
        require_equal(response.status,409)
        require_equal(await response.json(),{'error':'action_account_binding_changed'})
        require_equal(w.native.transport.mutations,[])


async def t_failed_refresh_returns_no_stale_list_or_native_error_detail():
    async with read_world() as w:
        require_equal((await w.client.get(path(w))).status,200)
        async def fail(method,url):
            raise RuntimeError('native-private-information')
        w.native.transport.before_response=fail
        response=await w.client.get(path(w))
        require_equal(response.status,503)
        require_equal(await response.json(),{'error':'action_resources_unavailable'})
        require_equal(w.native.exposure._entities,{})
        require_equal(w.native.transport.mutations,[])


async def t_native_vault_denial_is_not_bypassed_for_device_discovery():
    async with read_world() as w:
        w.account=change_credential(w,read=False)
        response=await w.client.get(path(w))
        require_equal(response.status,503)
        require_equal(await response.json(),{'error':'action_resources_unavailable'})
        require_equal(w.native.transport.calls,[])


async def t_swapped_native_method_cannot_run_through_the_read_endpoint():
    async with read_world() as w:
        invoked=[]
        async def substituted():
            invoked.append('arbitrary work')
            return []
        w.native.ha.states=substituted
        response=await w.client.get(path(w))
        require_equal(response.status,503)
        require_equal(invoked,[])
        require_equal(w.native.transport.calls,[])


async def t_new_security_class_in_fresh_state_closes_device_selection():
    async with read_world() as w:
        add(w,'switch.changed')
        async def changed(method,url):
            w.native.transport.states['switch.changed']['attributes']['device_class']='garage'
        w.native.transport.before_response=changed
        response=await w.client.get(path(w))
        require_equal(response.status,200)
        items=(await response.json())['items']
        require_equal([r['target']['entity_id'] for r in items],['light.fixture'])
        require_equal(w.native.transport.mutations,[])


async def t_owner_session_revoked_during_read_receives_no_device_list():
    async with read_world() as w:
        revoked=False
        async def revoke(method,url):
            nonlocal revoked
            if not revoked:
                revoked=True
                response=await w.client.post('/v1/browser/session/logout',json={},headers=w.headers)
                require_equal(response.status,200)
        w.native.transport.before_response=revoke
        response=await w.client.get(path(w))
        require_equal(response.status,401)
        require_equal(await response.json(),{'error':'unauthorized'})
        require_equal(w.native.transport.mutations,[])


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
