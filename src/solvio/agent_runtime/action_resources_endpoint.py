"""Owner-only discovery of exact, currently exposed native action targets.

This route reads the configured Home Assistant instance. It never creates a
run, submits a service call, logs in, or treats a device name as authority.
"""
from __future__ import annotations

import re
from dataclasses import replace
from aiohttp import web

from solvio.capabilities import task_action as TA
from solvio.capabilities.home_assistant import HAExposure
from solvio.capabilities.policy import ActionClass
from solvio.secret_vault import context as SC
from solvio.security.mobile_approval import browser_sessions as B


def _response(data, status=200):
    return web.json_response(data, status=status, headers={'Cache-Control':'no-store'})


def _native(orch, account):
    service = getattr(orch, 'action_service', None)
    if (not TA._original(service, TA.TaskServiceAction, TA._TASK_ACTION_SERVICE_METHODS)
            or not TA._original(service.exposure, HAExposure, TA._EXPOSURE_METHODS)
            or service.exposure.ha is not service.ha):
        raise TA.ExecutorUnavailable('action_resources_unavailable')
    identity = TA._identity('ha', service.ha)
    if TA.account_identity('ha', service.ha) != account:
        raise TA.CapabilityRefused('action_account_binding_changed')
    return service, identity, frozenset(service.security_entities)


def _label(value, maximum=200):
    # HA labels are external display data. Return no arbitrary attributes or
    # controls; browser rendering must still use textContent, never HTML.
    return ''.join(c for c in value if ord(c) >= 32)[:maximum] if type(value) is str else ''


def attach(app, orchestrator):
    async def authenticated(request):
        from solvio.agent_runtime.task_endpoint import dashboard_ha_catalog_context, native_ha_catalog_context
        browser = await B.actor(request)
        if browser is not None:
            return (browser.principal, ('browser', browser), dashboard_ha_catalog_context(browser))
        native = await native_ha_catalog_context(request)
        if native is None:
            return None
        device_id, principal, context = native
        return (principal, ('native', device_id, principal), context)

    async def resources(request):
        actor = await authenticated(request)
        if actor is None:
            return _response({'error':'unauthorized'},401)
        orch = orchestrator(request)
        if orch is None:
            return _response({'error':'agent_runtime_disabled'},503)
        configured_owner = getattr(getattr(orch.router,'_mobile',None),'owner_principal','')
        if not configured_owner or actor[0] != configured_owner:
            return _response({'error':'owner_configuration_required'},403)
        query = request.query
        if (set(query) - {'service','account','limit'} or not {'service','account'} <= set(query)
                or any(len(query.getall(key)) != 1 for key in query)
                or query['service'] != 'ha'
                or not re.fullmatch(r'ha-[0-9a-f]{32}',query['account'])
                or not re.fullmatch(r'[1-9][0-9]{0,2}',query.get('limit','100'))):
            return _response({'error':'invalid_action_resources'},400)
        limit = int(query.get('limit','100'))
        if limit > 100:
            return _response({'error':'invalid_action_resources'},400)
        account = query['account']
        try:
            service, identity, security = _native(orch,account)
            with SC.bound(actor[2]):
                # Force discovery once. snapshot() would call boundary again;
                # a second direct state read supplies fresh states without a
                # duplicate registry fetch (including an exposure with ttl=0).
                border = await service.exposure.boundary(force=True)
                if _native(orch,account) != (service,identity,security):
                    raise TA.CapabilityRefused('action_account_binding_changed')
                live = await service.ha.states()
            if _native(orch,account) != (service,identity,security):
                raise TA.CapabilityRefused('action_account_binding_changed')
            # An account/session revoked while the read was in flight does not
            # receive a stale authenticated list after its request resumes.
            current = await authenticated(request)
            if current is None or current != actor:
                return _response({'error':'unauthorized'},401)
            if _native(orch,account) != (service,identity,security):
                raise TA.CapabilityRefused('action_account_binding_changed')
            if type(live) is not list or any(type(s) is not dict for s in live):
                raise TA.ExecutorUnavailable('action_resources_unavailable')
            states = {s['entity_id']:s for s in live if type(s) is dict and type(s.get('entity_id')) is str}
            items = []
            for entity_id, entity in border.items():
                state = states.get(entity_id) or {}
                attributes = state.get('attributes') or {}
                if type(attributes) is not dict:
                    raise TA.ExecutorUnavailable('action_resources_unavailable')
                # A freshly reported security class also closes discovery.
                # The original border's class is still checked below, so an
                # absent new attribute cannot downgrade its earlier class.
                fresh = replace(entity, device_class=str(attributes.get('device_class') or ''))
                if (not re.fullmatch(r'[a-z][a-z0-9_]*\.[a-z0-9_]+',entity_id)
                        or entity.entity_id != entity_id or not entity.executable
                        or entity.action_class(security) is not ActionClass.HA_NORMAL
                        or not fresh.executable or fresh.action_class(security) is not ActionClass.HA_NORMAL):
                    continue
                items.append({'target':{'entity_id':entity_id}, 'name':_label(entity.name) or entity_id,
                    'area':_label(entity.area), 'domain':entity.domain,
                    'state':_label(state.get('state'),64) or 'unknown',
                    'operations':['set_state','set_brightness'] if entity.domain=='light' else ['set_state']})
            items.sort(key=lambda row:(row['area'],row['name'],row['target']['entity_id']))
            return _response({'service':'ha','account':account,'items':items[:limit],
                              'truncated':len(items)>limit})
        except TA.CapabilityRefused:
            return _response({'error':'action_account_binding_changed'},409)
        except Exception:
            # Native errors may contain service content or credential context.
            # Never substitute the previously visible list for a failed read.
            return _response({'error':'action_resources_unavailable'},503)

    app.add_routes([web.get('/v1/agent/action-resources',resources)])
