"""Original grant/cost ledger -> existing Autopilot -> published offline adapter.

Only the provider executable/auth-status is local fixture code. The native
textutil sandbox, Git publisher, lead validator, Driver and stores are real.
"""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from solvio.agent_runtime import extension_development as E, document_contract as DC
from solvio.agent_runtime import store as S, cost_dispatch as D, costs as C, specialists as SP
from solvio.agent_runtime import extension_activation as EA, workspace as W
from solvio.agent_runtime.task_start_service import TaskStartService
from solvio.agent_runtime.task_authority import TaskAuthority, VerifiedTaskReceipt
from solvio.autopilot import store as A, capacity as CAP, evidence as EV, lead as LEAD, machine as M
from solvio.autopilot.publisher import CheckpointPublisher
from solvio.specialists import providers as P, launcher as L

ADAPTER = "import os\nos.execv('/usr/bin/textutil', ['/usr/bin/textutil','-format','rtf','-convert','txt','-stdin','-stdout','-encoding','UTF-8'])\n"


class World:
    def __init__(self, root, *, mode='ok', quote=True):
        self.root, self.mode = root, mode
        self.ledger = S.AgentRunLedger(str(root / 'runs.db'))
        self.grants = TaskAuthority(self.ledger)
        self.costs = C.CostLedger(self.ledger)
        starts = TaskStartService(self.ledger, grants=self.grants, costs=self.costs)
        self.task, self.run = starts.create(objective='Lies mein privates Geheimdokument.',
            scope='research', origin='trusted_dashboard', principal='local-owner',
            receipt=VerifiedTaskReceipt('dashboard_session', 'fixture:session', 'local-owner'),
            request_id='extension-development-1',
            document_request=DC.DocumentRequest(b'{\\rtf1\\ansi Secret owner material.}'))
        require(starts.ready(self.run.run_id))
        self.milestone = 'document-development-test'
        self.ledger.set_run_fields(self.run.run_id, development_ref=self.milestone)
        self.ledger.transition(self.run.run_id, S.PLANNING)
        self.ledger.transition(self.run.run_id, S.RUNNING)
        self.ledger.transition(self.run.run_id, S.WAITING_CAPABILITY)
        self.development = A.AutopilotLedger(str(root / 'development.db'))
        self.seed = E.seed_repository(str(root / 'seed'))
        self.workspaces = W.WorkspaceManager(allowed=(self.seed,))
        self.publisher = CheckpointPublisher(self.seed)
        self.executable = self.cli()
        self.evidence = C.CostEvidence('free_local', 'fixture:' + hashlib.sha256(self.executable.read_bytes()).hexdigest())
        self.quote = (lambda *args: D.CostQuote(0, self.evidence)) if quote else None
        self.settlement = lambda *args: D.CostSettlement(0, self.evidence)
        self.service = self.fresh()
        self.development.create_milestone(self.service.contract_for(self.run.run_id, self.seed), repository=self.seed)

    def fresh(self):
        return E.ExtensionDevelopment(self.ledger, self.development, self.workspaces,
            self.publisher, quote_adapter=self.quote, settlement_adapter=self.settlement)

    def cli(self):
        executable = self.root / 'codex-fixture'
        executable.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, sqlite3, sys, time
folder = pathlib.Path(__file__).parent
config = json.loads((folder/'fixture.json').read_text())
prompt = sys.stdin.read()
kind = 'review' if '--json' in sys.argv else 'build'
with sqlite3.connect('file:'+config['ledger']+'?mode=ro', uri=True) as db:
    claims = [list(row) for row in db.execute("SELECT run_id,phase,state FROM agent_provider_invocations WHERE state='claimed'")]
with (folder/'calls.jsonl').open('a') as log:
    log.write(json.dumps({'kind':kind, 'prompt':prompt, 'claims':claims, 'cwd':os.getcwd(), 'pid':os.getpid()})+'\\n')
if config['mode'] == 'timeout' or (config['mode'] == 'review_timeout' and kind == 'review'):
    time.sleep(5)
if config['mode'] == 'quota' or (config['mode']=='review_quota' and kind=='review'):
    print('Rate limit reached; usage limit exceeded', file=sys.stderr)
    sys.exit(1)
if kind == 'build':
    pathlib.Path('adapter.py').write_text(config['adapter'])
    pathlib.Path('scripts').mkdir(exist_ok=True)
    pathlib.Path('scripts/run_tests.py').write_text("from pathlib import Path; Path("+repr(str(folder/'foreign-test-ran'))+").write_text('BAD')")
    print(json.dumps({'findings':['The native converter adapter is implemented.'], 'evidence':['adapter.py:2'], 'recommended_path':'Core fixture gate', 'uncertainties':[]}))
else:
    reply = {'verdict':'READY','next_action':'ready','rationale':'The Core gate passed.','findings':[], 'close_findings':[], 'proven':[]}
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(reply)}}))
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':11,'output_tokens':7}}))
''', encoding='utf-8')
        executable.chmod(0o700)
        (self.root / 'fixture.json').write_text(json.dumps({'mode': self.mode,
            'ledger': self.ledger.path, 'adapter': ADAPTER if self.mode != 'wrong_adapter' else "print('wrong')\n"}))
        return executable

    def invocation(self, *, workdir, model='', timeout=10):
        require_equal(model, '', 'No automatic tier/model override')
        return L.Invocation(str(self.executable), (str(self.executable), '-'),
            cwd=workdir, timeout=1.5 if self.mode in {'timeout', 'review_timeout'} else 5)

    def calls(self):
        path = self.root / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@contextmanager
def world(**kwargs):
    with tempfile.TemporaryDirectory(prefix='extension-development-') as folder:
        root = Path(folder).resolve()
        with patch.dict(os.environ, {'SOLVIO_STATE_DIR': str(root),
                                    'SOLVIO_AUTOPILOT_LOCK': str(root / 'driver.lock')}):
            w = World(root, **kwargs)
            status = AsyncMock(return_value=P.ProviderStatus('codex', True, auth='chatgpt', billing_mode='subscription'))
            with patch.object(P, 'codex_status', status), \
                    patch.object(SP, 'codex_builder_invocation', w.invocation), \
                    patch.object(P, 'codex_invocation', w.invocation), \
                    patch.object(CAP, 'preflight', AsyncMock(side_effect=AssertionError('legacy broker capacity'))), \
                    patch.object(EV, 'record_gate', side_effect=AssertionError('untrusted workspace test')), \
                    patch.object(LEAD.TechnicalLead, 'judge', AsyncMock(side_effect=AssertionError('broker lead'))):
                try:
                    yield w
                finally:
                    w.development.close()


async def t_real_autopilot_build_and_lead_are_bound_paid_and_published_without_activation():
    with world() as w:
        initial = W._git('rev-parse', 'HEAD', cwd=w.seed).stdout
        result = await w.service.drive(w.run.run_id)
        require_equal(result.state, 'ready', result.reason)
        require_equal(len(w.calls()), 2)
        require_equal([c['kind'] for c in w.calls()], ['build', 'review'])
        for call in w.calls():
            require_equal(len(call['claims']), 1)
            require_equal(call['claims'][0], [w.run.run_id, 'extension_' + call['kind'], 'claimed'])
            require('Secret owner material' not in call['prompt'])
            require('Geheimdokument' not in call['prompt'])
        require(not (w.root / 'foreign-test-ran').exists())
        require_equal(W._git('rev-parse', 'HEAD', cwd=w.seed).stdout, initial)
        require_equal(w.development.acceptance_counts(w.milestone), (1, 1))
        require_equal({u['provider'] for u in w.development.usage_rows(w.milestone)}, {'codex'})
        with w.ledger._open() as db:
            claims = db.execute('SELECT phase,state FROM agent_provider_invocations').fetchall()
        require_equal([(r['phase'], r['state']) for r in claims], [('extension_build', 'finished'), ('extension_review', 'finished')])
        activation = EA.ExtensionActivation(w.ledger, development=w.development, publisher=w.publisher)
        require(activation.selected(w.run.run_id) is None)
        artifact = await activation.prepare(w.run.run_id, milestone_id=w.milestone,
            commit=result.commit, checkpoint_id=result.checkpoint_id)
        require(bool(artifact))
        require_equal((await w.fresh().drive(w.run.run_id)).state, 'ready')
        require_equal(len(w.calls()), 2, 'No provider replay after reconstruction')


async def t_unknown_cost_blocks_before_first_physical_call_and_remains_held():
    with world(quote=False) as w:
        result = await w.service.drive(w.run.run_id)
        require_equal(result.reason, 'cost_unbounded')
        require(result.resume_allowed)
        require_equal(w.calls(), [])
        require_equal((await w.fresh().drive(w.run.run_id)).reason, 'cost_unbounded')
        require_equal(w.calls(), [])


async def t_quota_never_switches_provider_or_repeats_on_fresh_drive():
    with world(mode='quota') as w:
        result = await w.service.drive(w.run.run_id)
        require_equal(result.reason, 'quota')
        require(not result.resume_allowed, 'A builder may have partially changed its workspace')
        require_equal(len(w.calls()), 1)
        require_equal((await w.fresh().drive(w.run.run_id)).reason, 'quota')
        require_equal(len(w.calls()), 1)
        require(not any(e['kind'] in {'quota_failover', 'builder_switched'} for e in w.development.events(w.milestone)))


async def t_lead_quota_uses_original_cost_scope_and_holds_published_build():
    with world(mode='review_quota') as w:
        result = await w.service.drive(w.run.run_id)
        require_equal(result.reason, 'quota')
        require_equal(len(w.calls()), 2)
        require_equal(w.development.milestone(w.milestone).state, A.HUMAN_REQUIRED)
        require(result.resume_allowed)
        require(result.dispatch_started, 'Do not disguise a completed physical review as not dispatched')
        commit = w.development.milestone(w.milestone).last_commit
        publications = w.publisher.published(w.milestone)
        require_equal((await w.fresh().drive(w.run.run_id)).reason, 'quota')
        require_equal(len(w.calls()), 2)
        fixture = json.loads((w.root / 'fixture.json').read_text())
        fixture['mode'] = 'ok'
        (w.root / 'fixture.json').write_text(json.dumps(fixture))
        wait = {'phase': 'development', 'provider': 'codex', 'billing_mode': 'subscription',
                'requested_billing_mode': 'subscription', 'reason': 'quota',
                'resume_allowed': True, 'resume_state': S.WAITING_CAPABILITY}
        require(w.ledger.park_provider_boundary(w.run.run_id, {'provider_wait': wait}, 'Owner quota decision'))
        require(w.ledger.resume_provider_boundary(w.run.run_id, provider='codex', billing_mode='subscription'))
        result = await w.fresh().drive(w.run.run_id)
        require_equal(result.state, 'ready', result.reason)
        require_equal(result.commit, commit)
        require_equal(w.publisher.published(w.milestone), publications)
        require_equal([c['kind'] for c in w.calls()], ['build', 'review', 'review'])
        require_equal((await w.fresh().drive(w.run.run_id)).state, 'ready')
        require_equal(len(w.calls()), 3)


async def t_unknown_review_outcome_remains_nonresumable_after_owner_limit_decision():
    with world(mode='review_timeout') as w:
        result = await w.service.drive(w.run.run_id)
        require_equal(result.reason, 'cost_recovery_required')
        require(not result.resume_allowed)
        require_equal([c['kind'] for c in w.calls()], ['build', 'review'])
        with w.ledger._open() as connection:
            claim = connection.execute("SELECT state FROM agent_provider_invocations WHERE phase='extension_review'").fetchone()
        require_equal(claim['state'], 'unknown')
        require_equal((await w.fresh().drive(w.run.run_id)).reason, 'cost_recovery_required')
        require_equal(len(w.calls()), 2)


async def t_terminal_lead_quota_without_actual_cost_settlement_stays_held():
    with world(mode='review_quota') as w:
        # A measured cap is not the final bill. The review is known terminal,
        # but its remaining reservation may not be treated as settled zero.
        def quote(provider, invocation):
            if '--json' in invocation.argv:
                return D.CostQuote(1, C.CostEvidence('enforceable_upper_bound', 'fixture:review-cap'))
            return D.CostQuote(0, w.evidence)
        w.service.quote_adapter = quote
        w.service.settlement_adapter = lambda *args: None
        result = await w.service.drive(w.run.run_id)
        require_equal(result.reason, 'quota')
        require_equal(result.cost_status, 'reserved')
        require(not result.resume_allowed)
        require_equal((await w.fresh().drive(w.run.run_id)).reason, 'quota')
        require_equal(len(w.calls()), 2)


async def t_timeout_has_durable_unknown_and_no_replay():
    with world(mode='timeout') as w:
        result = await w.service.drive(w.run.run_id)
        require_equal(result.reason, 'cost_recovery_required')
        require(not result.resume_allowed)
        with w.ledger._open() as db:
            states = [r['state'] for r in db.execute('SELECT state FROM agent_provider_invocations')]
        require_equal(states, ['unknown'])
        require_equal((await w.fresh().drive(w.run.run_id)).reason, 'cost_recovery_required')
        require_equal(len(w.calls()), 1)


async def t_original_grant_revocation_before_build_stops_every_provider():
    with world() as w:
        bound = DC.for_run(w.ledger, w.run.run_id)
        require(w.grants.revoke(bound.grant_reference, 'fixture:revoke'))
        result = await w.service.drive(w.run.run_id)
        require_equal(result.reason, 'document_contract_not_authorized')
        require_equal(w.calls(), [])


async def t_auth_api_mode_cannot_start_builder_or_lead():
    with world() as w:
        with patch.object(P, 'codex_status', AsyncMock(return_value=P.ProviderStatus(
                'codex', False, 'subscription_required', auth='api_key', billing_mode='metered_api'))):
            result = await w.service.drive(w.run.run_id)
        require_equal(result.reason, 'subscription_required')
        require_equal(w.calls(), [])


async def t_incorrect_native_adapter_cannot_satisfy_ready_even_if_lead_says_ready():
    with world(mode='wrong_adapter') as w:
        result = await w.service.drive(w.run.run_id, max_rounds=1)
        require(result.state != 'ready')
        require_equal(w.development.acceptance_counts(w.milestone), (0, 1))
        require(not (w.root / 'foreign-test-ran').exists())


async def t_seed_is_idempotent_and_refuses_existing_unrelated_code():
    with world() as w:
        require_equal(E.seed_repository(w.seed), w.seed)
        Path(w.seed, 'unexpected.py').write_text("print('foreign')\n")
        try:
            E.seed_repository(w.seed)
        except E.DevelopmentPaused as exc:
            require_equal(exc.result.reason, 'seed_not_empty_contract')
        else:
            raise AssertionError('foreign seed accepted')


async def t_revocation_during_builder_prevents_publication_and_lead():
    with world() as w:
        original = SP.run_specialist
        bound = DC.for_run(w.ledger, w.run.run_id)
        async def revoke_after_real_call(*args, **kwargs):
            result = await original(*args, **kwargs)
            w.grants.revoke(bound.grant_reference, 'fixture:revoke-after-builder')
            return result
        with patch.object(SP, 'run_specialist', revoke_after_real_call):
            result = await w.service.drive(w.run.run_id)
        require_equal(result.reason, 'document_contract_not_authorized')
        require_equal(len(w.calls()), 1)
        require_equal(w.publisher.published(w.milestone), [])


async def t_open_phase_after_restart_is_held_without_replaying_even_if_no_claim_is_visible():
    with world() as w:
        w.service.factory(w.run.run_id)
        phase = w.development.start_phase(w.milestone, kind='build', builder='codex')
        result = await w.fresh().drive(w.run.run_id)
        require_equal(result.reason, 'cost_recovery_required')
        require(not result.resume_allowed)
        require_equal(w.calls(), [])
        require_equal(w.development.open_phases(w.milestone), [])
        require_equal((await w.fresh().drive(w.run.run_id)).reason, 'cost_recovery_required')


async def t_cancel_after_dispatch_reaps_process_and_holds_unknown():
    with world(mode='timeout') as w:
        operation = asyncio.create_task(w.service.drive(w.run.run_id))
        for _ in range(100):
            if w.calls():
                break
            await asyncio.sleep(0.03)
        require_equal(len(w.calls()), 1)
        operation.cancel()
        try:
            await operation
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('Cancellation not propagated')
        try:
            os.kill(w.calls()[0]['pid'], 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError('The owned provider process survived cancellation')
        result = await w.fresh().drive(w.run.run_id)
        require_equal(result.reason, 'cost_recovery_required')
        require(not result.resume_allowed)
        require_equal(len(w.calls()), 1)


async def t_only_canonical_original_task_owner_resume_unparks_development_once():
    with world(quote=False) as w:
        blocked = await w.service.drive(w.run.run_id)
        require_equal(blocked.reason, 'cost_unbounded')
        wait = {'phase': 'development', 'provider': 'codex', 'billing_mode': 'subscription',
                'requested_billing_mode': 'subscription', 'reason': 'cost_unbounded',
                'resume_allowed': True, 'resume_state': S.WAITING_CAPABILITY}
        require(w.ledger.park_provider_boundary(w.run.run_id, {'provider_wait': wait}, 'Owner cost decision'))
        require(w.ledger.resume_provider_boundary(w.run.run_id, provider='codex', billing_mode='subscription'))
        # Still unknown: the newly authorized attempt holds again. The stale
        # resuming marker must not unlock that second hold on the next tick.
        result = await w.fresh().drive(w.run.run_id)
        require_equal(result.reason, 'cost_unbounded')
        count = len(w.development.phases(w.milestone))
        require_equal(count, 2)
        require_equal((await w.fresh().drive(w.run.run_id)).reason, 'cost_unbounded')
        require_equal(len(w.development.phases(w.milestone)), count)
        # A second real Owner decision after installing a valid local quote
        # resumes the same task/grant/contract and can finish normally.
        require(w.ledger.park_provider_boundary(w.run.run_id, {'provider_wait': wait}, 'New Owner cost decision'))
        require(w.ledger.resume_provider_boundary(w.run.run_id, provider='codex', billing_mode='subscription'))
        w.quote = lambda *args: D.CostQuote(0, w.evidence)
        result = await w.fresh().drive(w.run.run_id)
        require_equal(result.state, 'ready', result.reason)
        require_equal(len(w.calls()), 2)


async def t_build_cost_and_review_reservation_share_original_ten_euro_limit():
    with world() as w:
        # Synthetic enforceable accounting fixtures, not a claim that the local
        # test executable or subscription login actually costs six euros.
        quote = C.CostEvidence('enforceable_upper_bound', 'fixture:600-cent-cap')
        actual = C.CostEvidence('actual_charge', 'fixture:600-cent-charge')
        w.service.quote_adapter = lambda *args: D.CostQuote(600, quote)
        w.service.settlement_adapter = lambda *args: D.CostSettlement(600, actual)
        result = await w.service.drive(w.run.run_id)
        require_equal(result.reason, 'cost_approval_required')
        require(result.resume_allowed)
        require_equal([c['kind'] for c in w.calls()], ['build'])
        require_equal(w.development.milestone(w.milestone).state, A.HUMAN_REQUIRED)
        with w.ledger._open() as db:
            rows = db.execute('SELECT phase,state FROM agent_provider_invocations').fetchall()
        require_equal([(r['phase'], r['state']) for r in rows], [('extension_build', 'finished')])


async def t_hold_failure_rolls_back_phase_closure_and_cannot_retry_quota():
    with world() as w:
        driver = w.service.factory(w.run.run_id)
        M.transition(w.development, w.milestone, A.BUILDING)
        phase = w.development.start_phase(w.milestone, kind='build', builder='codex')
        with patch.object(M, 'park', side_effect=RuntimeError('local crash failpoint')):
            try:
                driver.hold(E.DevelopmentPaused('quota', dispatch_started=True, provider='codex'))
            except RuntimeError:
                pass
            else:
                raise AssertionError('Failpoint did not interrupt hold')
        require_equal([p['phase_id'] for p in w.development.open_phases(w.milestone)], [phase])
        result = await w.fresh().drive(w.run.run_id)
        require_equal(result.reason, 'cost_recovery_required')
        require_equal(w.calls(), [])


async def t_finished_model_phase_without_following_state_is_not_replayed():
    with world() as w:
        w.service.factory(w.run.run_id)
        M.transition(w.development, w.milestone, A.BUILDING)
        phase = w.development.start_phase(w.milestone, kind='build', builder='codex')
        w.development.finish_phase(phase, state='succeeded', summary='Provider finished before process loss')
        result = await w.fresh().drive(w.run.run_id)
        require_equal(result.reason, 'cost_recovery_required')
        require(not result.resume_allowed)
        require_equal(w.calls(), [])


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
