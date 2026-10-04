"""Owner catalogue over actual HTTPS and Unix worker; synthetic browser only."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock,patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions,require,require_equal
enforce_assertions()
from test_agent_action_portal import portal_fixture,portal_action
from test_agent_action_execution import world
from test_agent_action_services import native_fixture,action_fixture
from solvio.portal import service as PS,protocol as P
from solvio.portal.binding import BINDINGS
from solvio.capabilities import task_action as TA
from solvio.capabilities.portal import SPECS
from solvio.capabilities.invocation import CapabilityInvocationGate,voice_trust
import mobile_attest_helper as H
PATH='/v1/agent/action-portal-sessions'

@asynccontextmanager
async def catalog_world(owner='local-owner'):
 async with portal_fixture(owner=owner) as p,world() as w:
  w.orch.action_service.portals=p.portals
  yield p,w
  require_equal(w.native.transport.calls,[])
  require_equal(w.calls(),[])
  require_equal(w.ledger.recent_runs(),[])

async def t_https_catalog_exact_native_account_no_page_content_or_idle_extension():
 async with catalog_world() as (p,w):
  touched=p.session.touched
  response=await w.client.get(PATH);body=await response.json()
  require_equal(response.status,200,str(body));require_equal(response.headers['Cache-Control'],'no-store')
  item=body['items'][0]
  require_equal(item['account'],portal_action(p)['account'])
  require_equal(item['target'],portal_action(p)['target'])
  require(item['authenticated']);require_equal(p.session.touched,touched)
  require_equal(p.operations,['ping','list_sessions','ping','ping'])
  require_equal(len(p.cdp.calls),1)
  require_equal(set(item),{'account','target','label','origin','authenticated','expires_in_s'})
  require('42.00' not in str(body) and 'secret' not in str(body) and 'Synthetic account' not in str(body))

async def t_foreign_and_legacy_ownerless_sessions_are_never_adopted():
 for owner in ('foreign-owner',''):
  async with catalog_world(owner) as (p,w):
   response=await w.client.get(PATH)
   require_equal(response.status,200);require_equal((await response.json())['items'],[])
   require_equal(p.cdp.calls,[]);require_equal(p.session.owner_principal,owner)

async def t_private_owner_guard_denies_anonymous_and_other_owner_before_socket():
 async with catalog_world() as (p,w):
  other=await w.new_client()
  require_equal((await other.get(PATH)).status,401)
  await w.login(other,'other-owner')
  require_equal((await other.get(PATH)).status,403)
  require_equal(p.operations,[])

async def t_native_app_reads_with_existing_device_authority():
 async with catalog_world() as (p,w):
  device=await H.enroll_attested(w.cp,transport_cred='synthetic-portal-transport')
  app=await w.new_client();headers={'X-Device-Id':device.device_id,'X-Transport-Cred':'synthetic-portal-transport'}
  response=await app.get(PATH,headers=headers)
  require_equal(response.status,200,await response.text())
  require_equal((await response.json())['items'][0]['target'],portal_action(p)['target'])

async def t_browser_revoked_during_cdp_read_receives_no_catalogue():
 async with catalog_world() as (p,w):
  async def revoke():
   response=await w.client.post('/v1/browser/session/logout',json={},headers=w.headers)
   require_equal(response.status,200)
  p.cdp.on_read=revoke
  response=await w.client.get(PATH)
  require_equal(response.status,401);require_equal(await response.json(),{'error':'unauthorized'})

async def t_native_app_revoked_during_read_receives_no_catalogue():
 async with catalog_world() as (p,w):
  device=await H.enroll_attested(w.cp,transport_cred='synthetic-portal-transport')
  app=await w.new_client();headers={'X-Device-Id':device.device_id,'X-Transport-Cred':'synthetic-portal-transport'}
  async def revoke():
   await w.cp.revoke_device(device.device_id)
  p.cdp.on_read=revoke
  response=await app.get(PATH,headers=headers)
  require_equal(response.status,401,await response.text())

async def t_stale_page_origin_path_or_marker_never_appears_authenticated():
 for url,text in [('https://evil.example/account','SIGNED IN'),
   ('https://portal.example.invalid/login?redirect=/account','SIGNED IN'),
   ('https://portal.example.invalid/login#/account','SIGNED IN'),
   ('https://portal.example.invalid/account-other','SIGNED IN'),
   ('https://portal.example.invalid/account','Logged out')]:
  async with catalog_world() as (p,w):
   p.cdp.page.update(url=url,text=text)
   response=await w.client.get(PATH);require_equal(response.status,200)
   require_equal((await response.json())['items'][0]['authenticated'],False)
   require_equal(p.session.authenticated,True,'read cannot mint or clear native login state')

async def t_expired_or_closed_during_read_is_absent_and_not_refreshed():
 for when in ('before','during'):
  async with catalog_world() as (p,w):
   if when=='before':p.session.touched-=PS.SESSION_IDLE+1
   else:
    async def close():p.worker.sessions.pop(p.session.session_id)
    p.cdp.on_read=close
   response=await w.client.get(PATH);require_equal(response.status,200)
   require_equal((await response.json())['items'],[])

async def t_changed_build_or_socket_never_returns_old_catalogue():
 for when in ('before','during','socket'):
  async with catalog_world() as (p,w):
   original=p.worker._build
   if when=='before':p.worker._build=lambda:'changed'
   else:
    async def change():
     if when=='socket':p.client.socket_path+='.missing'
     else:p.worker._build=lambda:'changed'
    p.cdp.on_read=change
   response=await w.client.get(PATH);require_equal(response.status,503,await response.text())
   if when=='before':require_equal(p.cdp.calls,[])
   p.worker._build=original

async def t_limits_extra_fields_and_worker_unknown_operation_are_closed():
 async with catalog_world() as (p,w):
  for query in ('?limit=0','?limit=51','?limit=1&limit=2','?owner_principal=foreign'):
   require_equal((await w.client.get(PATH+query)).status,400)
  require_equal(p.operations,[])
  require_equal((await p.client.call({'op':'not_registered'}))['reason'],'unknown_operation')
  require_equal((await p.client.call({'op':P.LIST_SESSIONS,'owner_principal':'local-owner','limit':True}))['ok'],False)

async def t_native_limit_is_bounded_and_explicitly_truncated():
 async with catalog_world() as (p,w):
  from copy import copy
  second=copy(p.session);second.session_id='ps-4242-2';p.worker.sessions[second.session_id]=second
  response=await w.client.get(PATH+'?limit=1');body=await response.json()
  require_equal(response.status,200);require_equal(len(body['items']),1);require(body['truncated'])
  require_equal(len(p.cdp.calls),1)

async def t_router_open_uses_verified_principal_not_argument_or_dom():
 async with portal_fixture() as p:
  from solvio.capabilities.router import CapabilityRouter
  p.portals.vault=type('Vault',(),{'has':lambda *_:True})()
  router=CapabilityRouter();router.register(SPECS['portal_open'],p.portals.open)
  gate=CapabilityInvocationGate();gate.begin_turn(session_id='fixture',turn_id='owner-open',
   principal='actual-owner',trust=voice_trust(True),user_text='Öffne mein Portal.')
  process=p.session.process;process.start=AsyncMock();process.new_page_socket=AsyncMock(return_value=p.cdp)
  page=p.session.page;page.prepare=AsyncMock();page.navigate=AsyncMock()
  with patch.object(PS,'BrowserProcess',return_value=process),patch.object(PS,'BrowserPage',return_value=page),patch.object(p.worker,'_policy',return_value=True):
   args={'portal':p.binding.portal_id,'owner_principal':'forged-owner'}
   ctx=gate.context()
   result=await router.execute('portal_open',args,trust=ctx.trust,provenance=gate.provenance_for(args),principal=ctx.principal)
   require_equal(result.outcome.value,'invalid_input')
   require_equal(p.operations,[])
   args={'portal':p.binding.portal_id}
   result=await router.execute('portal_open',args,trust=ctx.trust,provenance=gate.provenance_for(args),principal=ctx.principal)
   require_equal(result.outcome.value,'success',str(result))
  sessions=[s for s in p.worker.sessions.values() if s is not p.session]
  require_equal(len(sessions),1);require_equal(sessions[0].owner_principal,'actual-owner')

async def t_failed_ping_cannot_reuse_previous_successful_build():
 async with catalog_world() as (p,w):
  await p.client.verify_build()
  with patch.object(p.worker,'_op_ping',AsyncMock(return_value={'ok':False})):
   response=await w.client.get(PATH)
  require_equal(response.status,503);require_equal(p.cdp.calls,[])

async def t_owner_bound_worker_read_requires_owner_even_when_key_omitted():
 async with portal_fixture(owner='actual-owner') as p:
  for extra in ({},{'owner_principal':''},{'owner_principal':'foreign-owner'}):
   response=await p.client.call({'op':P.READ,'session_id':p.session.session_id,**extra})
   require_equal(response.get('ok'),False)
  require_equal(p.cdp.calls,[])
  require((await p.client.read(p.session.session_id,owner_principal='actual-owner'))['ok'])
 async with portal_fixture(owner='') as legacy:
  require((await legacy.client.read(legacy.session.session_id))['ok'])
  require_equal((await legacy.client.list_sessions('actual-owner'))['items'],[])

async def t_router_plain_read_transports_owner_and_cannot_bypass_owned_session():
 from solvio.capabilities.router import CapabilityRouter
 from solvio.contracts.trust import TrustContext,TrustLevel
 from solvio.capabilities.contract import ArgumentSource
 for principal in ('','foreign-owner','actual-owner'):
  async with portal_fixture(owner='actual-owner') as p:
   router=CapabilityRouter(principal='')
   router.register(SPECS['portal_read'],p.portals.read)
   result=await router.execute('portal_read',{'session':p.session.session_id},
    trust=TrustContext(TrustLevel.USER_DIRECT,user_authorized=True),
    provenance={'session':ArgumentSource.USER_DIRECT},principal=principal)
   require_equal(result.outcome.value=='success',principal=='actual-owner')
   if principal!='actual-owner':require_equal(p.cdp.calls,[])

async def t_task_status_foreign_or_ownerless_session_fails_before_page_read():
 for owner in ('different-owner',''):
  async with portal_fixture(owner=owner) as p:
   with native_fixture() as n,action_fixture(n,portal_action(p)) as w:
    w.adapter.portals=p.portals
    outcome=await w.adapter.execute(w.arguments,w.binding)
    require_equal(outcome.state,'not_dispatched');require_equal(p.cdp.calls,[])
    require_equal(p.operations,['ping','read'])

async def t_public_task_revocation_before_and_during_read_or_changed_authorizer_has_no_result():
 from test_agent_action_execution import admit
 for mode in ('before','handshake','during','authorizer'):
  async with portal_fixture(owner='local-owner') as p,world(expected_native='Synthetic account') as w:
   w.portals=p.portals;w.runtime()
   w.body['action_request']['actions']=[portal_action(p)]
   w.body['objective']='Lies den Status dieser bestehenden synthetischen Portalsitzung.'
   run=await admit(w);await w.detach();await w.orch.tick();await w.orch.tick()
   grant=w.orch.task_authority.for_run(run)
   require_equal(grant.authorizer,w.ledger.get_task(w.ledger.get_run(run).task_id).created_principal)
   changed=False
   async def invalidate():
    nonlocal changed
    if changed:return
    changed=True
    if mode=='authorizer':
     with w.ledger._open() as c:c.execute('UPDATE agent_task_grants SET authorizer=? WHERE reference=?',('foreign-owner',grant.reference))
    else:w.orch.task_authority.revoke(grant.reference,'fixture:revoke')
   if mode=='before':await invalidate()
   elif mode=='handshake':
    ping=p.worker._op_ping
    async def revoke_after_ping(message):
     response=await ping(message);await invalidate();return response
    p.worker._op_ping=revoke_after_ping
   else:p.cdp.on_read=invalidate
   await w.orch.tick()
   require(changed);require(w.ledger.get_run(run).state!='SUCCEEDED')
   with w.ledger._open() as c:
    require_equal(c.execute("SELECT count(*) FROM agent_action_claims WHERE status='completed'").fetchone()[0],0)
   if mode in ('before','handshake'):require_equal(p.cdp.calls,[])
   else:require(p.cdp.calls)
   require_equal(w.calls(),[]);require_equal(w.native.transport.calls,[])

if __name__=='__main__':
 from _harness import run_module
 raise SystemExit(run_module(globals(),__name__))
