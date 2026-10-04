"""Actual Router and Unix worker owner gates; no production or browser traffic."""
import asyncio
from pathlib import Path
import sys
from unittest.mock import AsyncMock
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_agent_action_portal import portal_fixture
from solvio.portal import service as PS, protocol as P
from solvio.capabilities.portal import SPECS
from solvio.capabilities.router import CapabilityRouter
from solvio.capabilities.invocation import CapabilityInvocationGate, voice_trust


def form_reader(p):
    calls = []
    async def inspect(method, params, **kwargs):
        require_equal(method, 'Runtime.evaluate')
        require_equal(params['expression'], PS._INSPECT_FORM.replace('%FORMSEL%',
            PS._js_string(p.binding.form_selector)).replace('%SUBMITSEL%', PS._js_string(p.binding.submit_selector)))
        calls.append(method)
        return {'result': {'value': {'found': True, 'origin': p.binding.login_origin,
            'url': p.binding.login_url, 'action': '/private-target', 'method': 'POST',
            'fields': ['private_customer_reference']}}}
    p.cdp.call = inspect
    return calls


async def t_every_owned_session_operation_rejects_missing_or_foreign_owner_before_cdp():
    for op in (P.NAVIGATE, P.PROBE, P.EXECUTE, P.CLOSE_SESSION):
        async with portal_fixture(owner='owner:alice') as p:
            touched = p.session.touched
            for owner in (None, '', 'owner:bob'):
                message = {'op': op, 'session_id': p.session.session_id}
                if owner is not None: message['owner_principal'] = owner
                response = await p.client.call(message)
                require_equal(response.get('reason'), 'unknown_session', str(response))
            require_equal(p.cdp.calls, [])
            require_equal(p.session.touched, touched)
            require(p.session.session_id in p.worker.sessions)
            require_equal(p.session.process.stop.await_count, 0)


async def t_true_router_foreign_login_cannot_read_private_manifest_or_request_approval():
    for principal, accepted in [('owner:bob', False), ('', False), ('owner:alice', True)]:
        async with portal_fixture(owner='owner:alice') as p:
            reads = form_reader(p)
            approver = type('Approver', (), {})()
            approver.request = AsyncMock(return_value='ap-synthetic-review')
            router = CapabilityRouter(mobile=approver, principal='')
            router.register(SPECS['portal_login'], p.portals.login, describe=p.portals.prepare_login)
            gate = CapabilityInvocationGate()
            gate.begin_turn(session_id='temporary-review', turn_id='owner-check', principal=principal,
                trust=voice_trust(True), user_text='Melde mich in meinem Portal an.')
            context = gate.context()
            result = await router.execute('portal_login', {'session': p.session.session_id},
                trust=context.trust, provenance={}, principal=principal)
            require_equal(result.outcome.value == 'approval_required', accepted, str(result))
            require_equal(len(reads), int(accepted))
            require_equal(approver.request.await_count, int(accepted))
            if not accepted: require_equal(p.portals._pending, {})


async def t_client_owner_probe_and_close_reach_the_same_native_session():
    async with portal_fixture(owner='owner:alice') as p:
        reads = form_reader(p)
        response = await p.client.probe(p.session.session_id, owner_principal='owner:alice')
        require(response['ok']); require_equal(len(reads), 1)
        require((await p.client.close_session(p.session.session_id, owner_principal='owner:alice'))['ok'])
        require_equal(p.session.process.stop.await_count, 1)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
