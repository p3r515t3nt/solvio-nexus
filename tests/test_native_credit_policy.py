"""Real isolated mobile signature/journal and native dispatch; no live accounts."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import native_credit_policy as K, native_costs as N, cost_dispatch as D, costs as C
from solvio.capabilities.approval_gateway import CapabilityApprovals
from solvio.security.mobile_approval import protocol as P
from solvio.specialists import subscription as U
from test_mobile_wiring import _wire
from test_native_subscription_costs import observation, native_contract, task_scope
from test_agent_cost_dispatch import fixture
import mobile_attest_helper as H

ACCOUNT = 'a' * 64

async def _decision(cp, device, key, *, decision=P.DECISION_APPROVE):
    challenge, reason = await cp.issue_challenge(approval_id=key, device_id=device.device_id)
    require_equal(reason, 'ok')
    row = await cp.store.get_device(device.device_id)
    return H.sign_decision(device, P.b64d(challenge['payload_b64']), decision=decision,
                           counter=row['app_attest_counter'] + 1)

@asynccontextmanager
async def consent():
    with tempfile.TemporaryDirectory() as directory:
        store, cp, coordinator, _ = await _wire(directory)
        device = await H.enroll_attested(cp)
        approvals = CapabilityApprovals(coordinator, owner_principal='local-owner')
        policy = K.NativeCreditPolicy(approvals)
        try:
            yield policy, store, cp, coordinator, device
        finally:
            await policy.close()
            await store.close()

async def decide(policy, cp, coordinator, device, enabled=True, *, denied=False, account_check=None):
    key = await policy.request(ACCOUNT, enabled, account_check=account_check or AsyncMock(return_value=True))
    wire = await _decision(cp, device, key, decision=P.DECISION_DENY if denied else P.DECISION_APPROVE)
    require_equal((await coordinator.apply_mobile_decision(**wire))[1], 'ok')
    await asyncio.gather(*policy.jobs)
    return key

def credit_observation(**changes):
    obs = observation('codex')
    values = obs.snapshots
    values['codex']['credits'] = dict(hasCredits=True, unlimited=False, balance='123')
    values['codex']['primary']['usedPercent'] = 100
    return replace(obs, snapshots_json=json.dumps(values), **changes)

async def t_face_id_consumed_exact_account_grants_and_restart_reads_same_journal():
    async with consent() as (policy, store, cp, co, dev):
        require_equal(policy.grant(ACCOUNT), '')
        key = await policy.request(ACCOUNT, True, account_check=AsyncMock(return_value=True))
        require_equal(policy.grant(ACCOUNT), '')
        require_equal((await co.apply_mobile_decision(**(await _decision(cp, dev, key))))[1], 'ok')
        require_equal(policy.grant(ACCOUNT), '', 'APPROVED without execution was accepted')
        await asyncio.gather(*policy.jobs)
        require_equal(policy.grant(ACCOUNT), 'approval:' + key)
        require_equal(policy.grant('b'*64), '')
        require_equal(K.NativeCreditPolicy(policy.approvals).grant(ACCOUNT), 'approval:' + key)
        require_equal((await store.get_request(key))['state'], 'CONSUMED')

async def t_denied_account_drift_disabled_and_revoked_never_grant():
    async with consent() as (policy, store, cp, co, dev):
        denied = await decide(policy, cp, co, dev, denied=True)
        require_equal((await store.get_request(denied))['state'], 'DENIED')
        require_equal(policy.grant(ACCOUNT), '')
        drift = await decide(policy, cp, co, dev, account_check=AsyncMock(return_value=False))
        require_equal((await store.get_request(drift))['state'], 'FAILED')
        require_equal(policy.grant(ACCOUNT), '')
        await decide(policy, cp, co, dev)
        require(bool(policy.grant(ACCOUNT)))
        await decide(policy, cp, co, dev, enabled=False)
        require_equal(policy.grant(ACCOUNT), '')
        await decide(policy, cp, co, dev)
        await cp.revoke_device(dev.device_id, reason='synthetic-owner-revocation')
        require_equal(policy.grant(ACCOUNT), '')

async def t_browser_session_cannot_substitute_for_biometrics():
    from solvio.security.mobile_approval.browser_sessions import BrowserSessionService
    async with consent() as (policy, store, cp, co, dev):
        sessions = BrowserSessionService(store, core_instance_id=cp.core_instance_id)
        enrollment = await sessions.issue_enrollment(principal='local-owner')
        session = await sessions.redeem(enrollment.token)
        actor = await sessions.authenticate(session.token, csrf_token=session.csrf_token)
        key = await policy.request(ACCOUNT, True, account_check=AsyncMock(return_value=True))
        row = await store.get_request(key)
        require_equal((await co.apply_dashboard_decision(actor=actor, approval_id=key,
            action_digest=row['action_digest'], decision='APPROVE'))[1], 'dashboard_tool_forbidden')
        require_equal(policy.grant(ACCOUNT), '')

async def t_approved_credit_dispatch_settles_unmeasured_not_zero_and_next_turn_can_work():
    async with consent() as (policy, store, cp, co, dev):
        await decide(policy, cp, co, dev)
        with fixture() as (ledger, costs, task, run, directory), native_contract(directory,
                obs=credit_observation()) as (adapter, reader, marker):
            adapter.credit_policy = policy
            for index in range(2):
                with task_scope(ledger, task, run, adapter, operation='credit:' + str(index)):
                    result = await U.SubscriptionTransport('codex', timeout=5)({'input': []})
                require_equal(result.get('reason', ''), '')
            require_equal(marker.read_text().count('turn'), 2)
            view = costs.view(task)
            require_equal(view['counts'], {'settled': 2})
            require_equal(view['ai_tool']['spent_cents'], None)
            require_equal(view['ai_tool']['credit_usage_unmeasured'], 2)
            with ledger._open() as db:
                rows = db.execute('SELECT actual_cents,upper_bound_cents,evidence FROM agent_cost_reservations').fetchall()
            require(all(r[0] is None and r[1] is None for r in rows))
            require(all(json.loads(r[2])['kind'] == 'owner_authorized_credits' for r in rows))
            require('owner_authorized_credits' not in C.ZERO_COST_EVIDENCE)

async def t_credit_quote_is_rechecked_before_dispatch_and_never_authorizes_other_accounts():
    async with consent() as (policy, store, cp, co, dev):
        await decide(policy, cp, co, dev)
        with fixture() as (ledger, costs, task, run, directory), native_contract(directory,
                obs=credit_observation()) as (adapter, reader, marker):
            adapter.credit_policy = policy
            invocation = U.text_invocation('codex', workdir=str(directory))
            quote = await adapter('codex', invocation)
            require(quote.validate_before_dispatch())
            await decide(policy, cp, co, dev, enabled=False)
            require(not quote.validate_before_dispatch())
            for index, obs in enumerate((credit_observation(account_digest='b'*64), credit_observation(auth_type='api_key'))):
                reader.read.return_value = obs
                with task_scope(ledger, task, run, adapter, operation='held:' + str(index)):
                    result = await U.SubscriptionTransport('codex', timeout=5)({'input': []})
                require_equal(result['reason'], 'cost_unbounded')
            require(not marker.exists())

def t_unlimited_negative_spend_control_missing_and_foreign_plan_remain_held():
    obs = credit_observation()
    require(N._personal_codex_credits(obs))
    for field, value in [('balance', '-1'), ('balance', 'NaN'), ('balance', None),
                         ('unlimited', True), ('hasCredits', False)]:
        snapshots = obs.snapshots
        snapshots['codex']['credits'][field] = value
        require(not N._personal_codex_credits(replace(obs, snapshots_json=json.dumps(snapshots))))
    snapshots = obs.snapshots
    snapshots['codex']['spendControlReached'] = True
    require(not N._personal_codex_credits(replace(obs, snapshots_json=json.dumps(snapshots))))
    require(not N._personal_codex_credits(replace(obs, plan_type='business')))

def t_credit_evidence_cannot_authorize_purchases_or_free_settlement():
    with fixture() as (ledger, costs, task, run, directory):
        proof = C.CostEvidence('owner_authorized_credits', 'native-credit:approval:synthetic')
        for category, route in [('purchase', 'codex'), ('ai_tool', 'arbitrary-api')]:
            decision = costs.reserve(task, 'blocked-' + category, None, category=category, route=route, evidence=proof)
            require_equal(decision.status, 'unbounded_cost')
        decision = costs.reserve(task, 'credit-ok', None, route='codex', evidence=proof)
        require(decision.allowed)
        require_equal(costs.settle(decision.reservation_id, 0,
            C.CostEvidence('included_no_extra_charge', 'false-zero')).status, 'refused')
        require_equal(costs.settle(decision.reservation_id, None, proof).status, 'settled')

async def t_https_settings_requires_owner_csrf_and_fresh_bound_account():
    from test_agent_task_entry import world
    async with world() as w:
        policy = K.NativeCreditPolicy(w.router._mobile)
        adapter = SimpleNamespace(credit_policy=policy, credit_account=AsyncMock(return_value=ACCOUNT))
        w.orch.cost_quote_adapter = adapter
        anonymous = await w.new_client()
        path = '/v1/agent/native-credits'
        require_equal((await anonymous.get(path)).status, 401)
        other = await w.new_client()
        await w.login(other, principal='different-owner')
        require_equal((await other.get(path)).status, 403)
        preview = await (await w.client.get(path)).json()
        require_equal(preview['enabled'], False)
        require_equal((await w.client.post(path, json={'account': ACCOUNT, 'enabled': True})).status, 401)
        require_equal((await w.client.post(path, json={'account': 'b'*64, 'enabled': True}, headers=w.headers)).status, 409)
        require_equal(await w.store.list_pending(), [])
        response = await w.client.post(path, json={'account': ACCOUNT, 'enabled': True}, headers=w.headers)
        require_equal(response.status, 200)
        payload = await response.json()
        require_equal(payload['state'], 'approval_required')
        require_equal(policy.grant(ACCOUNT), '')
        device = await H.enroll_attested(w.cp)
        require_equal((await w.co.apply_mobile_decision(**(await _decision(w.cp, device, payload['approval_id']))))[1], 'ok')
        await asyncio.gather(*policy.jobs)
        require(bool(policy.grant(ACCOUNT)))
        require_equal((await (await w.client.get(path)).json())['enabled'], True)

async def t_credit_timeout_preserves_unknown_and_blocks_automatic_retry():
    from solvio.specialists.launcher import Outcome
    from test_agent_cost_dispatch import scope, INVOCATION
    with fixture() as (ledger, costs, task, run, directory):
        quote = D.CostQuote(None, C.CostEvidence('owner_authorized_credits', 'native-credit:approval:synthetic'))
        runner = AsyncMock(return_value=Outcome(False, reason='timeout', process_started=True))
        with scope(ledger, task, run, quote=quote):
            first = await D.dispatch('codex', INVOCATION, 'synthetic', runner)
        require_equal(first.cost_status, 'unknown')
        with scope(ledger, task, run, quote=quote, operation_id='another-operation'):
            second = await D.dispatch('codex', INVOCATION, 'synthetic', runner)
        require_equal(second.outcome.reason, 'cost_recovery_required')
        require_equal(runner.await_count, 1)

if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
