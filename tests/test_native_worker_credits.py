"""Bound existing Face-ID credit authority through a real local native worker.

The provider is a synthetic JSON-RPC peer; no live account or model request.
"""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import native_costs as NC, cost_dispatch as D, costs as C
from solvio.specialists import openai_usage as O, hermes_native as N, launcher as L
import test_hermes_native as H
import test_native_credit_policy as K
from test_native_subscription_costs import observation

ACCOUNT_BODY = {'requiresOpenaiAuth': True, 'account': {
    'type': 'chatgpt', 'planType': 'pro', 'email': 'worker-credit@example.invalid'}}
ACCOUNT = O._account(ACCOUNT_BODY)[1]


def included_limit_snapshot():
    # Synthetic form of the observed personal-account condition. No live
    # identity, balance or reset timestamp is copied into this fixture.
    bucket = {'limitId': 'codex', 'planType': 'pro',
        'primary': {'usedPercent': 100, 'windowDurationMins': 10080},
        'secondary': None, 'credits': {'hasCredits': True, 'unlimited': False, 'balance': '5'},
        'spendControlReached': False, 'individualLimit': None,
        'rateLimitReachedType': 'rate_limit_reached'}
    return {'rateLimits': bucket, 'rateLimitsByLimitId': {'codex': bucket}}


def credit_peer(root, config):
    path = Path(config.codex_bin)
    source = path.read_text().replace("'type':'chatgpt','planType':'pro'",
        "'type':'chatgpt','planType':'pro','email':'worker-credit@example.invalid'")
    source = source.replace("    send({'id':rid,'result':response})", """    if method=='account/read' and (root/'account.json').exists():
        response=json.loads((root/'account.json').read_text())
    if method=='account/rateLimits/read' and (root/'limits.json').exists():
        response=json.loads((root/'limits.json').read_text())
    send({'id':rid,'result':response})""")
    path.write_text(source)
    (root/'limits.json').write_text(json.dumps(included_limit_snapshot()))


@contextmanager
def credit_adapter(policy):
    obs = observation('codex', account_digest=ACCOUNT)
    snapshots = O._limits(included_limit_snapshot(), 'pro')
    obs = replace(obs, snapshots_json=json.dumps(snapshots))
    reader = SimpleNamespace(read=AsyncMock(return_value=obs))
    adapter = NC.NativeSubscriptionCosts(codex_reader=reader, credit_policy=policy)
    with patch.object(O.UsageObservation, 'applies_to', return_value=True):
        yield adapter


async def t_face_id_credit_grant_reaches_the_same_claimed_research_worker_at_full_plan():
    async with K.consent() as (policy, store, cp, coordinator, device):
        with patch.object(K, 'ACCOUNT', ACCOUNT):
            await K.decide(policy, cp, coordinator, device)
        with H.native_fixture('quota_before', timeout=5.0) as (root, config, ledger, task, run, request), credit_adapter(policy) as adapter:
            credit_peer(root, config)
            actual_runner = N._run_worker
            async def bound_runner(invocation, prompt, **kwargs):
                require_equal(invocation.argv[-2:], ('--authorized-credit-account', ACCOUNT))
                rows = D.invocations(ledger, task)
                require_equal(len(rows), 1)
                require_equal(rows[0]['state'], 'claimed')
                require_equal(rows[0]['request_digest'], D._request_digest('codex', invocation, prompt))
                return await actual_runner(invocation, prompt, **kwargs)
            with D.task_cost_scope(ledger, task_id=task, run_id=run, phase='specialist',
                    operation_id='credit-research', quote_adapter=adapter), patch.object(N, '_run_worker', bound_runner):
                result = await N.run_research(request, config=config)
            require(result.result.ok, result.result.reason)
            require_equal(len(H.method_rows(root, 'turn/start')), 1)
            require_equal(len(D.invocations(ledger, task)), 1)
            view = C.CostLedger(ledger).view(task)
            require_equal(view['counts'], {'settled': 1})
            require_equal(view['ai_tool']['credit_usage_unmeasured'], 1)
            require_equal(view['ai_tool']['spent_cents'], None)


async def t_large_research_envelope_preserves_exact_credit_account_and_claim_binding():
    with H.native_fixture('quota_before', timeout=5.0) as (root, config, ledger, task, run, request), credit_adapter(allowed_policy()) as adapter:
        credit_peer(root, config)
        request.research_briefing, qualification = H.large_research_briefing()
        request.research_strategy = 'alternative_sources'
        actual_runner = N._run_worker
        async def bound_runner(invocation, prompt, **kwargs):
            require_equal(Path(invocation.argv[2]).name, 'hermes_research_worker.py')
            require_equal(invocation.argv[-2:], ('--authorized-credit-account', ACCOUNT))
            claims = D.invocations(ledger, task)
            require_equal(len(claims), 1)
            require_equal(claims[0]['state'], 'claimed')
            require_equal(claims[0]['request_digest'], D._request_digest('codex', invocation, prompt))
            return await actual_runner(invocation, prompt, **kwargs)
        with D.task_cost_scope(ledger, task_id=task, run_id=run, phase='specialist',
                operation_id='large-credit-research', quote_adapter=adapter), patch.object(N, '_run_worker', bound_runner):
            result = await N.run_research(request, config=config)
        require(result.result.ok, result.result.reason)
        require_equal(len(H.method_rows(root, 'turn/start')), 1)
        delivered = H.method_rows(root, 'turn/start')[0]['params']['input'][0]['text']
        require(request.research_briefing in delivered and qualification in delivered)
        require_equal(C.CostLedger(ledger).view(task)['ai_tool']['credit_usage_unmeasured'], 1)
        require_equal(C.CostLedger(ledger).view(task)['ai_tool']['spent_cents'], None)


def allowed_policy():
    return SimpleNamespace(grant=lambda account: 'approval:synthetic-credit' if account == ACCOUNT else '')


async def t_missing_and_revoked_face_id_credit_grants_never_claim_or_start_research():
    async with K.consent() as (policy, store, cp, coordinator, device):
        for revoke in (False, True):
            if revoke:
                with patch.object(K, 'ACCOUNT', ACCOUNT):
                    await K.decide(policy, cp, coordinator, device)
            with H.native_fixture('quota_before', timeout=5.0) as (root, config, ledger, task, run, request), credit_adapter(policy) as adapter:
                credit_peer(root, config)
                with D.task_cost_scope(ledger, task_id=task, run_id=run, phase='specialist',
                        operation_id='held-research', quote_adapter=adapter) as scope:
                    if revoke:
                        async def revoke_before_claim():
                            await cp.revoke_device(device.device_id, reason='synthetic-credit-revocation')
                        scope.source_check = revoke_before_claim
                    result = await N.run_research(request, config=config)
                require_equal(result.result.reason, 'cost_unbounded')
                require_equal(D.invocations(ledger, task), [])
                require_equal(H.method_rows(root, 'initialize'), [])


async def t_credit_worker_keeps_account_api_hardlimit_extra_bucket_and_unknown_guards():
    base = included_limit_snapshot()
    cases = []
    for field, value in [('balance', '0'), ('balance', None), ('balance', '-1'),
                         ('balance', 'NaN'), ('unlimited', True), ('hasCredits', False)]:
        data = included_limit_snapshot(); data['rateLimits']['credits'][field] = value
        cases.append((field + str(value), data, None))
    for field, value in [('spendControlReached', True),
                         ('rateLimitReachedType', 'workspace_owner_credits_depleted'),
                         ('rateLimitReachedType', 'workspace_member_credits_depleted'),
                         ('rateLimitReachedType', 'workspace_owner_usage_limit_reached'),
                         ('rateLimitReachedType', 'workspace_member_usage_limit_reached'),
                         ('individualLimit', {'limit': '5', 'used': '5', 'remainingPercent': 0, 'resetsAt': 1}),
                         ('rateLimitReachedType', 'unexpected-provider-limit')]:
        data = included_limit_snapshot(); data['rateLimits'][field] = value
        cases.append((field + str(value), data, None))
    for extra in ({'primary': {'usedPercent': 100}}, {'primary': None},
                  {'primary': {'usedPercent': 1}, 'spendControlReached': True},
                  *({'primary': {'usedPercent': 1}, 'rateLimitReachedType': state}
                    for state in sorted(O.REACHED | {'unexpected-provider-limit'})),
                  {'primary': {'usedPercent': 1}, 'credits': {'hasCredits': True, 'unlimited': True, 'balance': '5'}}):
        data = included_limit_snapshot()
        data['rateLimitsByLimitId'] = {'codex': data['rateLimits'], 'separate-model': extra}
        cases.append(('separate-bucket', data, None))
    foreign = json.loads(json.dumps(ACCOUNT_BODY)); foreign['account']['email'] = 'foreign@example.invalid'
    cases += [('foreign-account', base, foreign), ('api-account', base,
              {'requiresOpenaiAuth': True, 'account': {'type': 'apiKey'}})]
    for name, limits, account in cases:
        with H.native_fixture('quota_before', timeout=5.0) as (root, config, ledger, task, run, request), credit_adapter(allowed_policy()) as adapter:
            credit_peer(root, config)
            (root/'limits.json').write_text(json.dumps(limits))
            if account is not None: (root/'account.json').write_text(json.dumps(account))
            with D.task_cost_scope(ledger, task_id=task, run_id=run, phase='specialist',
                    operation_id='credit-held', quote_adapter=adapter):
                result = await N.run_research(request, config=config)
            require(not result.result.ok, name)
            require_equal(H.method_rows(root, 'thread/start'), [], name)
            require_equal(H.method_rows(root, 'turn/start'), [], name)


async def t_native_provider_quota_still_interrupts_once_with_credit_delegation():
    with H.native_fixture('quota_turn', timeout=5.0) as (root, config, ledger, task, run, request), credit_adapter(allowed_policy()) as adapter:
        credit_peer(root, config)
        with D.task_cost_scope(ledger, task_id=task, run_id=run, phase='specialist',
                operation_id='credit-provider-refusal', quote_adapter=adapter):
            result = await N.run_research(request, config=config)
        require_equal(result.result.reason, 'quota')
        require_equal(len(H.method_rows(root, 'turn/start')), 1)
        require(H.method_rows(root, 'turn/interrupt'))
        require_equal(len(D.invocations(ledger, task)), 1)


async def t_manually_added_credit_flags_cannot_borrow_scoped_or_unscoped_authority():
    with H.native_fixture('quota_before', timeout=5.0) as (root, config, ledger, task, run, request), credit_adapter(allowed_policy()) as adapter:
        base = N.worker_invocation(config, str(root))
        for arguments in (('--authorized-credit-account', ACCOUNT), ('--authorized-credit-account=' + ACCOUNT,)):
            invocation = replace(base, argv=base.argv + arguments)
            runner = AsyncMock(return_value=L.Outcome(True, process_started=True, exit_code=0))
            result = await D.dispatch('codex', invocation, 'synthetic', runner)
            require_equal(result.outcome.reason, 'cost_unbounded')
            with D.task_cost_scope(ledger, task_id=task, run_id=run, phase='specialist',
                    operation_id='forged', quote_adapter=adapter):
                result = await D.dispatch('codex', invocation, 'synthetic', runner)
            require_equal(result.outcome.reason, 'cost_unbounded')
            require_equal(runner.await_count, 0)
        require_equal(D.invocations(ledger, task), [])


async def t_credit_delegation_preserves_image_bytes_policy_and_claim():
    import test_native_image_generation as I
    from solvio.agent_runtime import image_generation as G
    with I.fixture('quota_before', timeout=5.0) as (root, config, ledger, task, run, step, planned), credit_adapter(allowed_policy()) as adapter:
        credit_peer(root, config)
        with D.task_cost_scope(ledger, task_id=task, run_id=run.run_id, phase='specialist',
                operation_id=step.step_id, quote_adapter=adapter):
            result = await G.NativeImageGenerator(config).generate(run, step, planned)
        require(result.ok, result.reason)
        require_equal(result.content, I.PNG)
        require_equal(len(H.method_rows(root, 'turn/start')), 1)
        require_equal(C.CostLedger(ledger).view(task)['ai_tool']['credit_usage_unmeasured'], 1)


async def t_credit_delegation_keeps_native_task_session_tools_and_terminal_binding():
    import test_native_task_transport as T
    from solvio.agent_runtime.native_tool_bridge import NativeToolBridge
    from solvio.specialists import native_task as NT
    with T.world() as w, credit_adapter(allowed_policy()) as adapter:
        # Same real worker/bridge/ledger, only the local provider account/limits
        # fixture changes from available-plan to authorized-credit overflow.
        peer = Path(w.config.codex_bin)
        peer.write_text(peer.read_text().replace("'type':'chatgpt','planType':'pro'",
            "'type':'chatgpt','planType':'pro','email':'worker-credit@example.invalid'").replace(
            "{'rateLimits':{'primary':{'usedPercent':1}}}",
            repr(included_limit_snapshot())))
        staged = w.root/'worker-source/src/solvio/specialists'
        (staged/'openai_usage.py').write_bytes(Path(O.__file__).read_bytes())
        tools = T.T.NativeCoreTools(w.ledger, w.sessions, w.session.session_id, w.run, w.router)
        bridge = NativeToolBridge(tools, socket_root=w.socket_root)
        with D.task_cost_scope(w.ledger, task_id=w.task, run_id=w.run, phase='specialist',
                operation_id='credit-native-task', quote_adapter=adapter):
            result = await NT.run_task(w.request, config=w.config, continuation=w.continuation, bridge=bridge)
        require(result.result.ok, result.result.reason)
        require_equal(len(T.methods(w, 'turn/start')), 1)
        turn = w.sessions.latest_turn(w.session.session_id)
        require_equal((turn.state, turn.terminal_status), ('terminal', 'completed'))
        require_equal(turn.native_turn_id, result.native_turn_id)
        require_equal(C.CostLedger(w.ledger).view(w.task)['ai_tool']['credit_usage_unmeasured'], 1)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
