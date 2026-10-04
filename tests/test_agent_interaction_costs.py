"""Real temporary SQLite admission, migration and physical-dispatch boundaries."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import costs as C, cost_dispatch as D, cost_subjects as A, store as S, task_authority as T
from solvio.specialists.launcher import Invocation, Outcome

DIGEST = hashlib.sha256(b'synthetic preference').hexdigest()
OTHER_DIGEST = hashlib.sha256(b'different synthetic preference').hexdigest()
FREE = D.CostQuote(0, C.CostEvidence('free_local', 'test:local-only'))
BOUND = D.CostQuote(600, C.CostEvidence('enforceable_upper_bound', 'test:hard-cap'))
INVOCATION = Invocation('/synthetic/local', (), cwd='/', timeout=1)


@contextmanager
def fixture(*, task=False, initialize=True):
    with tempfile.TemporaryDirectory(prefix='solvio-interaction-costs-') as path:
        ledger = S.AgentRunLedger(os.path.join(path, 'agent.sqlite3'))
        task_id = run_id = None
        if task:
            record = ledger.create_task(objective='Pruefe die belegbare Auftragsauskunft',
                scope='research', created_origin='trusted_interactive_app', created_principal='owner:device')
            task_id = record.task_id
            run_id = ledger.create_run(task_id=task_id).run_id
            ledger.transition(run_id, S.PLANNING)
            ledger.transition(run_id, S.RUNNING)
            T.TaskAuthority(ledger).issue(task_id,run_id,
                receipt=T.VerifiedTaskReceipt('app_session','app:test-start','owner:device'),capabilities=())
        if initialize:
            costs = C.CostLedger(ledger)
            if task:
                costs.configure(task_id)
        yield ledger, task_id, run_id


def source(*, kind='app', conversation='conversation:one', message='message:one', ref='session:one', principal='owner:device'):
    return A._verified_source(principal=principal, source_kind=kind, source_ref=ref,
        conversation_id=conversation, message_id=message)


def task_source(ledger, run, **kwargs):
    grant = T.TaskAuthority(ledger).for_run(run)
    return source(kind='task', ref=grant.reference, **kwargs)


def admit(ledger, **kwargs):
    return A.ActivityLedger(ledger).admit(source(**kwargs), content_digest=DIGEST)


def scoped(ledger, binding, quote=FREE):
    return D.interaction_cost_scope(ledger, activity_id=binding.activity_id,
        content_digest=binding.content_digest, quote_adapter=lambda *_: quote)


async def completed(*_):
    return Outcome(True, text='synthetic result', exit_code=0, process_started=True)


def raises(fn, error=ValueError):
    try:
        fn()
    except error:
        return
    raise AssertionError('expected exception')


def t_standalone_subject_is_not_a_fake_task_and_messages_share_one_budget():
    async def go():
        with fixture() as (ledger, _, _):
            one, two = admit(ledger), admit(ledger, message='message:two')
            require_equal(one.subject_id, two.subject_id)
            with scoped(ledger, one, BOUND):
                require((await D.dispatch('codex', INVOCATION, 'one', completed)).outcome.ok)
            runner = AsyncMock(side_effect=AssertionError('over budget dispatched'))
            with scoped(ledger, two, BOUND):
                require_equal((await D.dispatch('codex', INVOCATION, 'two', runner)).outcome.reason, 'cost_approval_required')
            view = C.CostLedger(ledger).view_subject(one.subject_id)
            require_equal((view['subject_kind'], view['task_id'], view['ai_tool']['total_cents']), ('interaction', None, 600))
            with ledger._open() as db:
                require_equal(db.execute('SELECT COUNT(*) FROM agent_tasks').fetchone()[0], 0)
                require_equal(db.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0], 0)
                require_equal(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            row = D.subject_invocations(ledger, one.subject_id)[0]
            require_equal((row['task_id'], row['run_id'], row['activity_id']), (None, None, one.activity_id))
            require('synthetic preference' not in json.dumps(row))
    asyncio.run(go())


def t_admission_replay_survives_restart_and_refuses_source_digest_or_task_rebinding():
    with fixture(task=True) as (ledger, task, run):
        service = A.ActivityLedger(ledger)
        first = service.admit(source(), content_digest=DIGEST)
        reopened = A.ActivityLedger(S.AgentRunLedger(ledger.path))
        require_equal(reopened.admit(source(), content_digest=DIGEST), first)
        for changed, digest in ((source(ref='session:other'), DIGEST), (source(), OTHER_DIGEST)):
            raises(lambda: reopened.admit(changed, content_digest=digest))
        raises(lambda: reopened.admit(source(), content_digest=DIGEST, task_id=task, run_id=run))
        raises(lambda: reopened.binding(first.activity_id, content_digest=DIGEST, source=source(principal='owner:other')))
        raises(lambda: D.InteractionCostScope(ledger, activity_id=first.activity_id, content_digest=OTHER_DIGEST))


def t_admission_needs_core_verified_source_and_immutable_activity():
    with fixture() as (ledger, _, _):
        service = A.ActivityLedger(ledger)
        raises(lambda: A.VerifiedInteractionSource('p', 'app', 'r', 'c', 'm'))
        raises(lambda: service.admit({'principal':'owner:device'}, content_digest=DIGEST))
        raises(lambda: source(kind='pi'))
        raises(lambda: service.admit(source(), content_digest='not-a-digest'))
        binding = admit(ledger)
        import sqlite3
        with ledger._open() as db:
            raises(lambda: db.execute('UPDATE agent_cost_activities SET content_digest=? WHERE activity_id=?',
                (OTHER_DIGEST, binding.activity_id)), sqlite3.IntegrityError)


def t_parallel_same_activity_admission_and_physical_claim_are_exactly_once():
    async def go():
        with fixture() as (ledger, _, _):
            with ThreadPoolExecutor(max_workers=2) as pool:
                admitted = list(pool.map(lambda _: admit(S.AgentRunLedger(ledger.path)), range(2)))
            require_equal(admitted[0], admitted[1])
            started, finish = asyncio.Event(), asyncio.Event()
            calls = []
            async def waiting(*_):
                calls.append(1); started.set(); await finish.wait()
                return await completed()
            with scoped(ledger, admitted[0]):
                first = asyncio.create_task(D.dispatch('codex', INVOCATION, 'same', waiting))
            await asyncio.wait_for(started.wait(), 2)
            with scoped(ledger, admitted[1]):
                second = await D.dispatch('codex', INVOCATION, 'same', completed)
            require_equal(second.outcome.reason, 'cost_recovery_required')
            finish.set(); require((await first).outcome.ok)
            require_equal(calls, [1])
            require_equal(len(D.subject_invocations(ledger, admitted[0].subject_id)), 1)
    asyncio.run(go())


def t_two_parallel_messages_cannot_each_spend_same_remaining_budget():
    async def go():
        with fixture() as (ledger, _, _):
            one, two = admit(ledger), admit(ledger, message='message:two')
            async def perform(binding):
                with scoped(ledger, binding, BOUND):
                    return await D.dispatch('codex', INVOCATION, 'synthetic', completed)
            results = await asyncio.gather(perform(one), perform(two))
            require_equal(sum(result.outcome.ok for result in results), 1)
            require_equal(C.CostLedger(ledger).view_subject(one.subject_id)['ai_tool']['total_cents'], 600)
    asyncio.run(go())


def t_default_and_subscription_auth_are_still_unknown_for_interactions():
    async def go():
        with fixture() as (ledger, _, _):
            for index, quote in enumerate((D.CostQuote(), D.CostQuote(0, C.CostEvidence('subscription_auth', 'auth:chatgpt')))):
                binding = admit(ledger, message='message:'+str(index))
                with scoped(ledger, binding, quote):
                    result = await D.dispatch('codex', INVOCATION, 'synthetic', AsyncMock(side_effect=AssertionError('unproven zero')))
                require_equal(result.outcome.reason, 'cost_unbounded')
                require(not result.dispatch_started)
    asyncio.run(go())


def t_task_learning_uses_same_budget_and_preserves_existing_invocation_id():
    async def go():
        with fixture(task=True) as (ledger, task, run):
            binding = A.ActivityLedger(ledger).admit(task_source(ledger, run), content_digest=DIGEST, task_id=task, run_id=run)
            require_equal(binding.subject_id, task)
            with D.task_cost_scope(ledger, task_id=task, run_id=run, phase='plan', operation_id='plan:one', quote_adapter=lambda *_: BOUND) as scope:
                expected = 'pc-'+hashlib.sha256(json.dumps([task,run,'plan','plan:one',1], separators=(',', ':')).encode()).hexdigest()
                result = await D.dispatch('codex', INVOCATION, 'plan', completed)
                require_equal(result.invocation_id, expected)
            with scoped(ledger, binding, BOUND):
                require_equal((await D.dispatch('codex', INVOCATION, 'extract', completed)).outcome.reason, 'cost_approval_required')
            require_equal(C.CostLedger(ledger).view(task)['ai_tool']['total_cents'], 600)
    asyncio.run(go())


def t_successful_task_can_finish_admitted_learning_without_reactivating_run():
    async def go():
        with fixture(task=True) as (ledger, task, run):
            binding = A.ActivityLedger(ledger).admit(task_source(ledger, run), content_digest=DIGEST, task_id=task, run_id=run)
            ledger.transition(run, S.VERIFYING)
            ledger.transition(run, S.SUCCEEDED)
            with ledger._open() as db:
                db.execute('UPDATE agent_tasks SET state=? WHERE task_id=?', (S.TASK_COMPLETED, task))
                before = tuple(db.execute('SELECT state,started_at,finished_at FROM agent_runs WHERE run_id=?', (run,)).fetchone())
            ledger = S.AgentRunLedger(ledger.path)
            with scoped(ledger, binding):
                require((await D.dispatch('codex', INVOCATION, 'extract', completed)).outcome.ok)
            with D.task_cost_scope(ledger, task_id=task, run_id=run, phase='plan', operation_id='plan:late', quote_adapter=lambda *_: FREE):
                require_equal((await D.dispatch('codex', INVOCATION, 'plan', completed)).outcome.reason, 'cost_recovery_required')
            raises(lambda: A.ActivityLedger(ledger).admit(task_source(ledger, run, message='message:late'), content_digest=DIGEST, task_id=task, run_id=run))
            with ledger._open() as db:
                require_equal(tuple(db.execute('SELECT state,started_at,finished_at FROM agent_runs WHERE run_id=?', (run,)).fetchone()), before)
    asyncio.run(go())


def t_cancelled_failed_parent_or_other_principal_never_gets_learning_tail():
    for end in (S.CANCELLED, S.FAILED):
        with fixture(task=True) as (ledger, task, run):
            service = A.ActivityLedger(ledger)
            raises(lambda: service.admit(task_source(ledger, run, principal='owner:other'), content_digest=DIGEST, task_id=task, run_id=run))
            binding = service.admit(task_source(ledger, run), content_digest=DIGEST, task_id=task, run_id=run)
            ledger.transition(run, end)
            raises(lambda: service.binding(binding.activity_id, content_digest=DIGEST))


def t_expiry_cancellation_and_revocation_are_rechecked_after_scope_before_claim():
    async def go():
        for mode in ('expiry','cancel','source','subject'):
            with fixture() as (ledger, _, _):
                binding = admit(ledger)
                service = A.ActivityLedger(ledger)
                async def quote(*_):
                    if mode == 'cancel': service.finish(binding.activity_id, cancelled=True)
                    if mode == 'source': service.revoke_source(source_kind='app', source_ref='session:one')
                    if mode == 'subject': service.revoke_subject(binding.subject_id)
                    return FREE
                with D.interaction_cost_scope(ledger, activity_id=binding.activity_id, content_digest=DIGEST, quote_adapter=quote):
                    with patch.object(A.time, 'time', return_value=binding.expires_at if mode == 'expiry' else binding.expires_at-1):
                        result = await D.dispatch('codex', INVOCATION, 'extract', AsyncMock(side_effect=AssertionError('inactive dispatched')))
                require(not result.dispatch_started)
                require_equal(D.subject_invocations(ledger, binding.subject_id), [])
    asyncio.run(go())


def t_unknown_or_orphan_blocks_entire_subject_across_messages_restart():
    async def go():
        with fixture() as (ledger, _, _):
            one, two = admit(ledger), admit(ledger, message='message:two')
            async def uncertain(*_): return Outcome(False, reason='lost', process_started=True)
            with scoped(ledger, one, BOUND):
                require_equal((await D.dispatch('codex', INVOCATION, 'one', uncertain)).cost_status, 'unknown')
            ledger = S.AgentRunLedger(ledger.path)
            with scoped(ledger, two):
                result = await D.dispatch('codex', INVOCATION, 'two', AsyncMock(side_effect=AssertionError('unknown bypassed')))
            require_equal(result.outcome.reason, 'cost_recovery_required')
            require_equal(C.CostLedger(ledger).view_subject(one.subject_id)['ai_tool']['reserved_cents'], 600)
    asyncio.run(go())


def t_each_explicit_queue_job_has_own_scope_and_resets_previous_context():
    async def go():
        with fixture(task=True) as (ledger, task, run):
            one, two = admit(ledger), admit(ledger, conversation='conversation:two')
            with D.task_cost_scope(ledger, task_id=task, run_id=run, phase='plan', operation_id='plan:outer') as outer:
                for binding in (one, two):
                    with scoped(ledger, binding):
                        require_equal(D.current_scope().phase, 'adaptive_extract')
                        require((await D.dispatch('codex', INVOCATION, 'extract', completed)).outcome.ok)
                    require(D.current_scope() is outer)
            require_equal(D.current_scope(), None)
            require_equal(D.invocations(ledger, task), [])
            for binding in (one,two):
                require_equal(len(D.subject_invocations(ledger, binding.subject_id)), 1)
    asyncio.run(go())


def t_existing_task_grant_revocation_expiry_and_fingerprint_gate_learning_tail():
    async def go():
        for mode in ('revoke','expire','fingerprint','grant_binding'):
            with fixture(task=True) as (ledger,task,run):
                binding = A.ActivityLedger(ledger).admit(task_source(ledger,run), content_digest=DIGEST, task_id=task,run_id=run)
                with scoped(ledger,binding):
                    with ledger._open() as db:
                        if mode=='revoke': db.execute('UPDATE agent_task_grants SET revoked_at=1 WHERE run_id=?',(run,))
                        if mode=='expire': db.execute('UPDATE agent_task_grants SET expires_at=1 WHERE run_id=?',(run,))
                        if mode=='fingerprint': db.execute("UPDATE agent_tasks SET objective='A different objective altogether' WHERE task_id=?",(task,))
                        if mode=='grant_binding': db.execute("UPDATE agent_task_grants SET capabilities='[]',binding_digest='changed' WHERE run_id=?",(run,))
                    result=await D.dispatch('codex',INVOCATION,'extract',AsyncMock(side_effect=AssertionError('invalid authority')))
                require(not result.dispatch_started)
                require_equal(D.invocations(ledger,task),[])
    asyncio.run(go())


def t_task_source_cannot_use_other_grant_and_revoked_session_cannot_admit_new_message():
    with fixture(task=True) as (ledger,task,run):
        service=A.ActivityLedger(ledger)
        raises(lambda:service.admit(source(kind='task',ref='ag-unknown'),content_digest=DIGEST,task_id=task,run_id=run))
        admit(ledger)
        service.revoke_source(source_kind='app',source_ref='session:one')
        raises(lambda:admit(ledger,message='message:after-revocation'))


def t_provider_hold_survives_ingress_replay_and_restart_until_explicit_resume():
    with fixture() as (ledger,_,_):
        binding=admit(ledger)
        service=A.ActivityLedger(ledger)
        require(service.hold(binding.activity_id,'quota'))
        require(not service.hold(binding.activity_id,'logged_out'))
        require_equal(A.ActivityLedger(S.AgentRunLedger(ledger.path)).admit(source(),content_digest=DIGEST),binding)
        raises(lambda:service.binding(binding.activity_id,content_digest=DIGEST))
        require(service.release_hold(binding.activity_id))
        require_equal(service.binding(binding.activity_id,content_digest=DIGEST),binding)
        service.finish(binding.activity_id,cancelled=True)
        require(not service.release_hold(binding.activity_id))


def t_releasing_hold_does_not_grant_a_retry_for_unknown_physical_outcome():
    async def go():
        with fixture() as (ledger,_,_):
            binding=admit(ledger)
            service=A.ActivityLedger(ledger)
            async def unknown(*_): return Outcome(False,reason='lost',process_started=True)
            with scoped(ledger,binding,BOUND):
                require_equal((await D.dispatch('codex',INVOCATION,'extract',unknown)).cost_status,'unknown')
            service.hold(binding.activity_id,'provider_unavailable')
            service.release_hold(binding.activity_id)
            with scoped(ledger,binding,BOUND):
                result=await D.dispatch('codex',INVOCATION,'extract',AsyncMock(side_effect=AssertionError('unknown retry')))
            require(not result.dispatch_started)
            require_equal(C.CostLedger(ledger).view_subject(binding.subject_id)['ai_tool']['reserved_cents'],600)
    asyncio.run(go())


def t_explicit_resume_gets_new_physical_attempt_without_rebinding_old_unknown_quote():
    async def go():
        with fixture() as (ledger,_,_):
            binding=admit(ledger)
            service=A.ActivityLedger(ledger)
            with scoped(ledger,binding,D.CostQuote()):
                held=await D.dispatch('codex',INVOCATION,'extract',AsyncMock(side_effect=AssertionError('unbounded')))
                require_equal(held.outcome.reason,'cost_unbounded')
                service.hold(binding.activity_id,'cost_unbounded')
                require(service.release_hold(binding.activity_id))
                require(not service.release_hold(binding.activity_id),'repeated release created another attempt')
                # The old context may still exist; it cannot adopt the new resume.
                stale=await D.dispatch('codex',INVOCATION,'extract',AsyncMock(side_effect=AssertionError('stale scope')))
                require(not stale.dispatch_started)
            with scoped(ledger,binding,FREE):
                accepted=await D.dispatch('codex',INVOCATION,'extract',completed)
            require(accepted.outcome.ok)
            require(held.invocation_id!=accepted.invocation_id)
            require_equal(C.CostLedger(ledger).view_subject(binding.subject_id)['counts'],{'unbounded_cost':1,'settled':1})
            require_equal(service.generation(binding.activity_id),1)
    asyncio.run(go())


def t_revocation_before_first_admission_is_durable_and_blocks_frozen_source():
    with fixture() as (ledger,_,_):
        verified=source()
        service=A.ActivityLedger(ledger)
        require_equal(service.revoke_source(source_kind='app',source_ref='session:one'),0)
        service=A.ActivityLedger(S.AgentRunLedger(ledger.path))
        raises(lambda:service.admit(verified,content_digest=DIGEST))
        with ledger._open() as db:
            require_equal(db.execute('SELECT COUNT(*) FROM agent_cost_activities').fetchone()[0],0)
            require_equal(db.execute('SELECT COUNT(*) FROM agent_cost_source_revocations').fetchone()[0],1)


def t_cancellation_during_quote_releases_only_its_unclaimed_reservation():
    async def go():
        with fixture() as (ledger,_,_):
            first,second=admit(ledger),admit(ledger,message='message:two')
            async def cancel_during_quote(*_):
                A.ActivityLedger(ledger).finish(first.activity_id,cancelled=True)
                return BOUND
            with D.interaction_cost_scope(ledger,activity_id=first.activity_id,content_digest=DIGEST,quote_adapter=cancel_during_quote):
                result=await D.dispatch('codex',INVOCATION,'one',AsyncMock(side_effect=AssertionError('cancelled')))
            require_equal(result.cost_status,'released')
            require_equal(C.CostLedger(ledger).view_subject(first.subject_id)['ai_tool']['total_cents'],0)
            with scoped(ledger,second,BOUND):
                require((await D.dispatch('codex',INVOCATION,'two',completed)).outcome.ok)
            require_equal(C.CostLedger(ledger).view_subject(first.subject_id)['ai_tool']['total_cents'],600)
            require_equal(len(D.subject_invocations(ledger,first.subject_id)),1)
    asyncio.run(go())


def t_task_scope_rechecks_existing_grant_after_quote_and_releases_unclaimed_money():
    async def go():
        with fixture(task=True) as (ledger,task,run):
            authority=T.TaskAuthority(ledger)
            grant=authority.for_run(run)
            async def revoke_during_quote(*_):
                authority.revoke(grant.reference,'owner:test-revocation')
                return BOUND
            with D.task_cost_scope(ledger,task_id=task,run_id=run,phase='plan',operation_id='plan:one',quote_adapter=revoke_during_quote):
                result=await D.dispatch('codex',INVOCATION,'plan',AsyncMock(side_effect=AssertionError('revoked grant')))
            require(not result.dispatch_started)
            require_equal(result.cost_status,'released')
            require_equal(C.CostLedger(ledger).view(task)['ai_tool']['total_cents'],0)
            require_equal(D.invocations(ledger,task),[])
    asyncio.run(go())


def t_unconfigured_existing_task_keeps_original_n2_decision_identity():
    with fixture(task=True,initialize=False) as (ledger,task,_):
        result=C.CostLedger(ledger).reserve(task,'invocation:before-policy',0,route='codex',evidence=FREE.evidence)
        require_equal((result.reason,result.task_id,result.subject_id),('cost_policy_missing',task,task))


LEGACY_INVOCATIONS = """CREATE TABLE agent_provider_invocations (
reservation_id TEXT PRIMARY KEY REFERENCES agent_cost_reservations(reservation_id), invocation_id TEXT NOT NULL,
task_id TEXT NOT NULL REFERENCES agent_tasks(task_id),run_id TEXT NOT NULL REFERENCES agent_runs(run_id),
phase TEXT NOT NULL,operation_id TEXT NOT NULL,ordinal INTEGER NOT NULL,provider TEXT NOT NULL,
request_digest TEXT NOT NULL,process_owner TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('claimed','finished','unknown','not_dispatched')),
claimed_at REAL NOT NULL,finished_at REAL,UNIQUE(task_id,invocation_id));"""


def legacy(ledger, task, run, *, claims=True):
    with ledger._open() as db:
        db.executescript(C.SCHEMA)
        db.execute('INSERT INTO agent_cost_settings VALUES (1,1000,\'owner:old-default\',100)')
        db.execute('INSERT INTO agent_cost_policies VALUES (?,1000,2000,20000,\'owner:old-purchase\',101)', (task,))
        db.execute('INSERT INTO agent_cost_authorizations VALUES (?,\'owner:old-ai-cap\',\'ai_tool\',2000,102)', (task,))
        for index,state in enumerate(('reserved','approval_required','unbounded_cost','unknown','settled','released')):
            upper = None if state == 'unbounded_cost' else 100
            db.execute('INSERT INTO agent_cost_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                ('ac-legacy-'+str(index), task, 'pc-legacy-'+str(index), 'ai_tool', 'codex', upper,
                 C.CostEvidence('enforceable_upper_bound','old:bound').json(),state,75 if state=='settled' else None,
                 'old-settlement' if state in ('settled','released') else '',103,104))
        if claims:
            db.executescript(LEGACY_INVOCATIONS)
            for index,state in ((0,'claimed'),(3,'unknown'),(4,'finished'),(5,'not_dispatched')):
                db.execute('INSERT INTO agent_provider_invocations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('ac-legacy-'+str(index),'pc-legacy-'+str(index),task,run,'plan','plan:old',index,
                     'codex','digest:old','process:old',state,105,None if state=='claimed' else 106))


def snapshots(ledger):
    with ledger._open() as db:
        result = {}
        for table in ('agent_cost_policies','agent_cost_authorizations','agent_cost_reservations','agent_provider_invocations'):
            if db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?',(table,)).fetchone():
                result[table] = [dict(row) for row in db.execute('SELECT * FROM '+table)]
        return result


def t_atomic_legacy_migration_preserves_every_money_state_claim_id_and_fk():
    with fixture(task=True, initialize=False) as (ledger,task,run):
        legacy(ledger,task,run)
        before = snapshots(ledger)
        costs = C.CostLedger(ledger)
        after = snapshots(ledger)
        for table, rows in before.items():
            expected = []
            for row in rows:
                row = dict(row)
                if table=='agent_provider_invocations': row.update(subject_id=row['task_id'],activity_id=None)
                else: row['subject_id']=row.pop('task_id')
                expected.append(row)
            require_equal(after[table], expected)
        require_equal(costs.view(task)['ai_tool']['total_cents'], 275)
        require_equal(costs.settings()['authority_ref'], 'owner:old-default')
        with ledger._open() as db:
            require_equal(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            # N8/C3: die Erstmigration endet beim Endstand 4 (`text_chat`).
            require_equal(db.execute('SELECT version FROM agent_cost_schema').fetchone()[0],4)
        with D.task_cost_scope(ledger,task_id=task,run_id=run,phase='plan',operation_id='plan:new'):
            require(D.recovery_pending(), 'old unknown/orphan claim lost')
        require_equal(snapshots(S.AgentRunLedger(ledger.path)), after)


def t_parallel_initializers_migrate_once_including_costs_without_dispatch_table():
    with fixture(task=True, initialize=False) as (ledger,task,run):
        legacy(ledger,task,run,claims=False)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _: C.CostLedger(S.AgentRunLedger(ledger.path)).view(task),range(2)))
        require_equal(results[0],results[1])
        with ledger._open() as db:
            require_equal(db.execute('SELECT COUNT(*) FROM agent_cost_reservations').fetchone()[0],6)
            require_equal(db.execute('SELECT COUNT(*) FROM agent_provider_invocations').fetchone()[0],0)
            require_equal(db.execute('PRAGMA foreign_key_check').fetchall(),[])


def t_migration_crash_after_dropping_old_tables_rolls_back_schema_and_all_values():
    with fixture(task=True, initialize=False) as (ledger,task,run):
        legacy(ledger,task,run)
        before=snapshots(ledger)
        original=ledger._open
        class Fail:
            def __init__(self, db): self.db=db
            def __getattr__(self,name): return getattr(self.db,name)
            def execute(self, sql, *args):
                if sql.startswith('ALTER TABLE agent_cost_policies_v2'):
                    raise RuntimeError('synthetic migration crash')
                return self.db.execute(sql,*args)
        @contextmanager
        def broken():
            with original() as db: yield Fail(db)
        with patch.object(ledger,'_open',broken):
            raises(lambda:C.CostLedger(ledger),RuntimeError)
        require_equal(snapshots(ledger),before)
        with ledger._open() as db:
            require_equal(db.execute('SELECT name FROM sqlite_master WHERE name LIKE \'%_v2\'').fetchall(),[])
            require_equal(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        require_equal(C.CostLedger(ledger).view(task)['ai_tool']['total_cents'],275)


# ---------------------------------------------------------------- N8/C3: text_chat, v4, Downgrade

import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

DELIVERY_ONE, DELIVERY_TWO = 'cd-0123456789abcdef', 'cd-0123456789abcdee'
CHAT = 'c-0123456789abcdef'


def chat_source(*, kind='dashboard', message='m-0123456789abcdef', principal='owner:device'):
    return source(kind=kind, conversation=CHAT, message=message, ref='browser:session-one', principal=principal)


def admit_chat(ledger, delivery=DELIVERY_ONE, *, digest=DIGEST, **kwargs):
    return A.ActivityLedger(ledger).admit(chat_source(**kwargs), content_digest=digest,
                                          purpose='text_chat', operation_key=delivery, lifetime_seconds=900)


def seed_v3(ledger):
    """The exact v3 activity schema: two purposes only, version 3. No production DB."""
    v3 = A.SUBJECT_SCHEMA.replace(A._purpose_check(A._PURPOSES_V4), A._purpose_check(A._PURPOSES_V3))
    require('text_chat' not in v3, 'the v3 seed no longer matches the schema text')
    with patch.object(A, 'SUBJECT_SCHEMA', v3), patch.object(A, 'COST_SCHEMA_VERSION', 3):
        C.CostLedger(ledger)
    with ledger._open() as db:
        require_equal(db.execute('SELECT version FROM agent_cost_schema').fetchone()[0], 3)
        db.execute("INSERT INTO agent_cost_subjects VALUES ('ci-v3','interaction',NULL,'owner:fixture','dashboard','conversation:v3',1,NULL)")
        db.execute("INSERT INTO agent_cost_policies(subject_id,ask_threshold_cents,created_at) VALUES ('ci-v3',1000,1)")
        for index, (purpose, state, held) in enumerate((('adaptive_extract', 'pending', ''), ('voice_delegate', 'completed', ''),
                                                       ('voice_delegate', 'pending', 'provider_hold'))):
            db.execute("INSERT INTO agent_cost_activities(activity_id,subject_id,purpose,operation_key,principal,source_kind,"
                       "source_ref,conversation_id,message_id,content_digest,accepted_at,expires_at,state,held_reason) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (f'ca-v3-{index}', 'ci-v3', purpose, '' if purpose == 'adaptive_extract' else f'op-{index}',
                        'owner:fixture', 'dashboard', 'old-session', 'conversation:v3', f'old-message-{index}',
                        'c' * 64, 1, 9999999999, state, held))
        for index, (rstate, istate) in enumerate((('unknown', 'unknown'), ('reserved', 'claimed'), ('settled', 'finished'),
                                                  ('released', 'not_dispatched'))):
            db.execute("INSERT INTO agent_cost_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (f'ac-v3-{index}', 'ci-v3', f'pc-v3-{index}', 'ai_tool', 'codex', 600,
                        C.CostEvidence('enforceable_upper_bound', 'old-bound').json(), rstate,
                        75 if rstate == 'settled' else None, '', 2, 3))
            db.execute("INSERT INTO agent_provider_invocations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (f'ac-v3-{index}', f'pc-v3-{index}', 'ci-v3', None, None, f'ca-v3-{index % 3}',
                        'voice_delegate', f'ca-v3-{index % 3}', index + 1, 'codex', 'old-digest', 'old-process',
                        istate, 2, None if istate == 'claimed' else 3))
        require_equal(db.execute('PRAGMA foreign_key_check').fetchall(), [])


def rows(ledger):
    with ledger._open() as db:
        return {table: sorted(tuple(r) for r in db.execute('SELECT * FROM ' + table))
                for table in ('agent_cost_activities', 'agent_provider_invocations', 'agent_cost_reservations')}


def t_v3_migration_to_v4_keeps_every_money_state_and_admits_text_chat():
    with fixture(initialize=False) as (ledger, _, _):
        seed_v3(ledger)
        before = rows(ledger)
        C.CostLedger(ledger)
        require_equal(rows(ledger), before, 'a v3 row changed during the v4 rebuild')
        with ledger._open() as db:
            require_equal(db.execute('SELECT version FROM agent_cost_schema').fetchone()[0], 4)
            require_equal(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='agent_cost_activities'").fetchone()[0]
            require("'text_chat'" in sql, sql)
            require(db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='agent_cost_activity_immutable'").fetchone(),
                    'the immutability trigger was not recreated')
            raises(lambda: db.execute("UPDATE agent_cost_activities SET content_digest=? WHERE activity_id='ca-v3-0'", (OTHER_DIGEST,)),
                   sqlite3.IntegrityError)
        binding = admit_chat(ledger)
        require_equal(binding.purpose, 'text_chat')
        require_equal(rows(S.AgentRunLedger(ledger.path))['agent_provider_invocations'], before['agent_provider_invocations'])
        # Ein zweites Oeffnen bleibt bei 4 und baut nichts um.
        C.CostLedger(S.AgentRunLedger(ledger.path))
        with ledger._open() as db:
            require_equal(db.execute('SELECT version FROM agent_cost_schema').fetchone()[0], 4)
            require_equal(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%_v4' OR name LIKE '%_v3'").fetchone()[0], 0)


def t_unsupported_versions_are_refused_before_any_rebuild():
    with fixture() as (ledger, _, _):
        before = rows(ledger)
        with ledger._open() as db:
            db.execute('UPDATE agent_cost_schema SET version=5')
        raises(lambda: C.CostLedger(S.AgentRunLedger(ledger.path)))
        require_equal(rows(ledger), before)


def t_text_chat_admission_requires_delivery_form_app_or_dashboard_and_no_task():
    with fixture(task=True) as (ledger, task, run):
        service = A.ActivityLedger(ledger)
        good = admit_chat(ledger)
        require_equal((good.purpose, good.operation_key), ('text_chat', DELIVERY_ONE))
        for bad in (dict(delivery='not-a-delivery'), dict(delivery='cd-0123'), dict(delivery=''),
                    dict(kind='voice_room'), dict(kind='task')):
            raises(lambda: admit_chat(ledger, **bad) if 'kind' not in bad else
                   service.admit(source(kind=bad['kind'], conversation=CHAT, ref='r'), content_digest=DIGEST,
                                 purpose='text_chat', operation_key=DELIVERY_TWO))
        raises(lambda: service.admit(chat_source(), content_digest=DIGEST, purpose='text_chat',
                                     operation_key=DELIVERY_TWO, task_id=task, run_id=run))
        raises(lambda: service.admit(chat_source(), content_digest=DIGEST, purpose='sonstiges', operation_key=DELIVERY_TWO))
        with ledger._open() as db:
            require_equal(db.execute("SELECT COUNT(*) FROM agent_cost_activities WHERE purpose='text_chat'").fetchone()[0], 1)


def t_text_chat_subject_is_per_delivery_and_chat_costs_aggregate_over_activities():
    async def go():
        with fixture() as (ledger, _, _):
            one = admit_chat(ledger, DELIVERY_ONE)
            two = admit_chat(ledger, DELIVERY_TWO, message='m-0123456789abcdee')
            require(one.subject_id != two.subject_id, 'two deliveries of one chat share a cost subject')
            require_equal((one.conversation_id, two.conversation_id, one.source_kind), (CHAT, CHAT, 'dashboard'))
            with ledger._open() as db:
                subjects = [dict(r) for r in db.execute('SELECT kind,principal,source_kind,conversation_id FROM agent_cost_subjects ORDER BY conversation_id')]
                require_equal(subjects, [
                    {'kind': 'interaction', 'principal': 'owner:device', 'source_kind': 'conversation_message', 'conversation_id': DELIVERY_TWO},
                    {'kind': 'interaction', 'principal': 'owner:device', 'source_kind': 'conversation_message', 'conversation_id': DELIVERY_ONE}])
            with scoped(ledger, one, BOUND):
                require((await D.dispatch('codex', INVOCATION, 'one', completed)).outcome.ok)
            with scoped(ledger, two, BOUND):
                require((await D.dispatch('codex', INVOCATION, 'two', completed)).outcome.ok)
            for binding in (one, two):
                require_equal(C.CostLedger(ledger).view_subject(binding.subject_id)['ai_tool']['total_cents'], 600)
            with ledger._open() as db:
                total = db.execute("SELECT SUM(r.upper_bound_cents) FROM agent_cost_activities a "
                    "JOIN agent_provider_invocations i ON i.activity_id=a.activity_id "
                    "JOIN agent_cost_reservations r ON r.reservation_id=i.reservation_id "
                    "WHERE a.conversation_id=? AND a.purpose='text_chat'", (CHAT,)).fetchone()[0]
                require_equal(total, 1200, 'the per-chat aggregation over activities is wrong')
            # Replay bucht nichts neu: dieselbe Zustellung, dieselbe Aktivitaet.
            require_equal(admit_chat(S.AgentRunLedger(ledger.path), DELIVERY_ONE), one)
            raises(lambda: admit_chat(ledger, DELIVERY_ONE, digest=OTHER_DIGEST))
            with ledger._open() as db:
                require_equal(db.execute("SELECT COUNT(*) FROM agent_cost_activities WHERE purpose='text_chat'").fetchone()[0], 2)
    asyncio.run(go())


def t_an_uncertain_text_chat_claim_blocks_only_its_own_delivery():
    """§9.1 Fall 4 auf Kostenebene: `unknown` sperrt das Subjekt — und das ist genau EINE Zustellung."""
    async def go():
        with fixture() as (ledger, _, _):
            one = admit_chat(ledger, DELIVERY_ONE)
            async def uncertain(*_): return Outcome(False, reason='lost', process_started=True)
            with scoped(ledger, one, BOUND):
                require_equal((await D.dispatch('codex', INVOCATION, 'one', uncertain)).cost_status, 'unknown')
            ledger = S.AgentRunLedger(ledger.path)
            with scoped(ledger, one):
                require_equal((await D.dispatch('codex', INVOCATION, 'again', AsyncMock(side_effect=AssertionError('unknown bypassed')))).outcome.reason,
                              'cost_recovery_required')
            two = admit_chat(ledger, DELIVERY_TWO, message='m-0123456789abcdee')
            with scoped(ledger, two):
                require(not D.recovery_pending(), 'the next delivery of the same chat inherited the lock')
                require((await D.dispatch('codex', INVOCATION, 'two', completed)).outcome.ok,
                        'the next message of the same chat was not answered')
    asyncio.run(go())


def t_the_invocation_domain_table_names_text_chat_and_rejects_an_unknown_purpose():
    with fixture() as (ledger, _, _):
        require_equal(D.INVOCATION_DOMAINS, {'adaptive_extract': 'adaptive-extract-cost-invocation-v1',
                                             'voice_delegate': 'voice-delegate-cost-invocation-v1',
                                             'text_chat': 'text-chat-cost-invocation-v1'})
        binding = admit_chat(ledger)
        scope = D.InteractionCostScope(ledger, activity_id=binding.activity_id, content_digest=DIGEST)
        ordinal, invocation = scope.next_invocation()
        expected = 'pc-' + hashlib.sha256(json.dumps(['text-chat-cost-invocation-v1', binding.subject_id,
            binding.activity_id, 0, 1], separators=(',', ':')).encode()).hexdigest()
        require_equal((ordinal, invocation), (1, expected))
        require_equal(scope.next_invocation()[0], 2, 'ordinal 2 is the answer call')
        scope.phase = 'sonstiges'
        raises(scope.next_invocation)


def t_downgrade_script_parks_text_chat_rows_and_refuses_only_live_claims():
    import cost_schema_downgrade_v4_to_v3 as DG
    async def go():
        with fixture() as (ledger, _, _):
            service = A.ActivityLedger(ledger)
            one = admit_chat(ledger, DELIVERY_ONE)
            two = admit_chat(ledger, DELIVERY_TWO, message='m-0123456789abcdee')
            voice = admit(ledger)                      # bleibt in jedem Fall stehen
            with scoped(ledger, one, BOUND):
                require((await D.dispatch('codex', INVOCATION, 'one', completed)).outcome.ok)
            async def uncertain(*_): return Outcome(False, reason='lost', process_started=True)
            with scoped(ledger, two, BOUND):
                require_equal((await D.dispatch('codex', INVOCATION, 'two', uncertain)).cost_status, 'unknown')
            # Zwei offene (pending, ungehaltene, nicht abgelaufene) Aktivitaeten → verweigert.
            raises(lambda: DG.downgrade(ledger.path), DG.DowngradeRefused)
            with ledger._open() as db:
                require_equal(db.execute('SELECT version FROM agent_cost_schema').fetchone()[0], 4, 'a refusal changed the schema')
            # Der Prozessor schliesst jede Aktivitaet einer terminalen Zustellung:
            # `completed` → finish(); `blocked` → finish(cancelled=True). `unknown` blockiert nie.
            service.finish(one.activity_id)
            service.finish(two.activity_id, cancelled=True)
            report = DG.downgrade(ledger.path)
            require_equal((report['version_after'], report['parked_activities'], report['parked_invocations'],
                           report['unknown_invocations']), (3, 2, 2, 1), report)
            with ledger._open() as db:
                require_equal(db.execute('SELECT version FROM agent_cost_schema').fetchone()[0], 3)
                require_equal(db.execute("SELECT COUNT(*) FROM agent_cost_activities").fetchone()[0], 1)
                require_equal(db.execute("SELECT COUNT(*) FROM agent_cost_activities_textchat_park").fetchone()[0], 2)
                require_equal(db.execute("SELECT COUNT(*) FROM agent_provider_invocations_textchat_park").fetchone()[0], 2)
                require_equal(db.execute("SELECT COUNT(*) FROM agent_cost_reservations").fetchone()[0], 2, 'reservations must stay')
                require_equal(db.execute("SELECT state FROM agent_cost_reservations WHERE subject_id=?", (two.subject_id,)).fetchone()[0], 'unknown')
                require_equal(db.execute('PRAGMA foreign_key_check').fetchall(), [])
                sql = db.execute("SELECT sql FROM sqlite_master WHERE name='agent_cost_activities'").fetchone()[0]
                require("'text_chat'" not in sql, sql)
            # Rueckweg: v3 → v4 importiert die geparkten Zeilen mit ihrem Zustand zurueck.
            C.CostLedger(ledger)
            with ledger._open() as db:
                require_equal(db.execute('SELECT version FROM agent_cost_schema').fetchone()[0], 4)
                states = dict(db.execute("SELECT activity_id,state FROM agent_cost_activities WHERE purpose='text_chat'").fetchall())
                require_equal(states, {one.activity_id: 'completed', two.activity_id: 'cancelled'})
                claims = dict(db.execute("SELECT activity_id,state FROM agent_provider_invocations WHERE activity_id IN (?,?)",
                                         (one.activity_id, two.activity_id)).fetchall())
                require_equal(claims, {one.activity_id: 'finished', two.activity_id: 'unknown'})
                require_equal(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%textchat_park'").fetchone()[0], 0)
                require_equal(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            from types import SimpleNamespace
            with ledger._open() as db:
                require(D._recovery_pending(db, SimpleNamespace(subject_id=two.subject_id, ledger=ledger)),
                        'the parked unknown claim lost its lock after the round trip')
            require_equal(A.ActivityLedger(ledger).binding(voice.activity_id, content_digest=DIGEST), voice)
            # Ein lebender Claim verweigert ohne Flag; `--core-stopped` ist die Operatorbestaetigung.
            with ledger._open() as db:
                db.execute("UPDATE agent_provider_invocations SET state='claimed',finished_at=NULL WHERE activity_id=?", (one.activity_id,))
            raises(lambda: DG.downgrade(ledger.path), DG.DowngradeRefused)
            require_equal(DG.downgrade(ledger.path, core_stopped=True)['version_after'], 3)
            # Gehaltene oder abgelaufene pending-Aktivitaeten verweigern nie.
            C.CostLedger(ledger)
            three = admit_chat(ledger, 'cd-0123456789abcded', message='m-0123456789abcded')
            service.hold(three.activity_id, 'quota')
            four = admit_chat(ledger, 'cd-0123456789abcdec', message='m-0123456789abcdec')
            with ledger._open() as db:
                db.execute("UPDATE agent_provider_invocations SET state='finished',finished_at=3 WHERE activity_id=?", (one.activity_id,))
            import time as _time
            raises(lambda: DG.downgrade(ledger.path), DG.DowngradeRefused)          # `four` ist offen
            expired = _time.time() + 2 * A.MAX_ACTIVITY_SECONDS                       # ... bis die Frist um ist
            require_equal(DG.downgrade(ledger.path, now=expired)['parked_activities'], 4)
    asyncio.run(go())


def t_downgrade_dry_run_and_main_change_nothing_and_report_counts_only():
    import cost_schema_downgrade_v4_to_v3 as DG
    with fixture() as (ledger, _, _):
        one = admit_chat(ledger)
        A.ActivityLedger(ledger).finish(one.activity_id)
        before = rows(ledger)
        report = DG.downgrade(ledger.path, dry_run=True)
        require_equal((report['dry_run'], report['text_chat_activities']), (True, 1))
        require_equal(rows(ledger), before)
        with ledger._open() as db:
            require_equal(db.execute('SELECT version FROM agent_cost_schema').fetchone()[0], 4)
        require_equal(DG.main(['--db', ledger.path, '--dry-run']), 0)
        require_equal(DG.main(['--db', ledger.path]), 0)
        with ledger._open() as db:
            require_equal(db.execute('SELECT version FROM agent_cost_schema').fetchone()[0], 3)
        require_equal(DG.main(['--db', ledger.path]), 3, 'a v3 book is not a v4 book')
        require_equal(DG.main(['--db', os.path.join(os.path.dirname(ledger.path), 'missing.sqlite3')]), 2)
        blob = json.dumps(report)
        require('synthetic' not in blob and DIGEST not in blob, blob)


def t_downgrade_without_text_chat_rows_leaves_no_park_tables_so_a_second_takeover_sees_no_loss():
    """Review Runde 8, C8-1: der Rueckbau legte die beiden Park-Tabellen auch bei
    0 text_chat-Zeilen an; der naechste Start des neuen Codes raeumt jede
    Park-Tabelle ab, und der Uebernahmeweg liest ein verschwundenes Vorher-Objekt
    als `agent_table_lost` — ein Rollback OHNE Chat blockierte den zweiten
    Anlauf. Jetzt entsteht ohne Zeilen keine Park-Tabelle: die Tabellenmenge vor
    dem Rueckbau ist nach Rueckbau + Wiederaufstieg dieselbe."""
    import cost_schema_downgrade_v4_to_v3 as DG
    def tables(ledger):
        with ledger._open() as db:
            return sorted(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    with fixture() as (ledger, _, _):
        admit(ledger)                                   # nur eine Sprachaktivitaet, kein text_chat
        before = tables(ledger)
        require(not any(name.endswith('textchat_park') for name in before))
        report = DG.downgrade(ledger.path)
        require_equal((report['version_after'], report['parked_activities'], report['parked_invocations']), (3, 0, 0), report)
        after_down = tables(ledger)
        require(not any(name.endswith('textchat_park') for name in after_down), after_down)
        C.CostLedger(ledger)                            # Wiederaufstieg v3 → v4 (zweite Uebernahme)
        with ledger._open() as db:
            require_equal(db.execute('SELECT version FROM agent_cost_schema').fetchone()[0], 4)
        require_equal(tables(ledger), before, 'a table vanished across downgrade + re-migration')
        # Mit Zeilen entsteht die Park-Tabelle weiterhin (der dokumentierte Fall bleibt).
        one = admit_chat(ledger)
        A.ActivityLedger(ledger).finish(one.activity_id)
        require_equal(DG.downgrade(ledger.path)['parked_activities'], 1)
        require('agent_cost_activities_textchat_park' in tables(ledger))


def t_downgrade_refuses_a_foreign_or_pre_v4_source_tree_and_a_book_without_cost_tables():
    """Review Runde 7 C7-H1/C7-H3 und Runde 8 R8-F-3/C8-2: die drei Verweigerungen
    vor jedem Schreibzugriff — Modul aus einem fremden Baum, eigener Baum ohne
    Schema 4 (der alte Code), Buch ohne Kostentabellen — als Verhalten, nicht
    als Sonde: das Skript laeuft als Kopie in einem Wegwerfbaum unter dem
    Testinterpreter."""
    import shutil, subprocess
    import cost_schema_downgrade_v4_to_v3 as DG
    script = Path(DG.__file__).resolve()
    candidate_src = script.parents[1] / 'src'
    with tempfile.TemporaryDirectory(prefix='solvio-downgrade-origin-') as folder:
        root = Path(folder)
        db_path = root / 'agent_runs.sqlite3'
        with sqlite3.connect(db_path) as db:
            db.execute('CREATE TABLE agent_cost_schema(singleton INTEGER PRIMARY KEY, version INTEGER)')
            db.execute('INSERT INTO agent_cost_schema VALUES (1, 4)')
        # (a) Skript allein, Modul aus dem Kandidatenbaum → wrong_source_tree.
        alone = root / 'alone'; alone.mkdir()
        shutil.copy(script, alone / script.name)
        env = dict(os.environ, PYTHONPATH=str(candidate_src), PYTHONDONTWRITEBYTECODE='1')
        run = subprocess.run([sys.executable, str(alone / script.name), '--db', str(db_path), '--core-stopped'],
                             capture_output=True, text=True, env=env, timeout=60)
        require_equal(run.returncode, 3, run.stderr)
        require(run.stderr.startswith('REFUSED: wrong_source_tree'), run.stderr)
        # (b) Eigener Baum ohne die v4-Konstante (der alte Code) → source_tree_without_v4.
        old = root / 'old'; (old / 'scripts').mkdir(parents=True)
        pkg = old / 'src' / 'solvio' / 'agent_runtime'; pkg.mkdir(parents=True)
        (old / 'src' / 'solvio' / '__init__.py').write_text('')
        (pkg / '__init__.py').write_text('')
        (pkg / 'store.py').write_text('def resolve_path(value):\n    return value\n')
        (pkg / 'cost_subjects.py').write_text('# pre-v4 module without TEXT_CHAT_PARK_ACTIVITIES\n')
        shutil.copy(script, old / 'scripts' / script.name)
        run = subprocess.run([sys.executable, str(old / 'scripts' / script.name), '--db', str(db_path), '--core-stopped'],
                             capture_output=True, text=True, env=dict(env, PYTHONPATH=''), timeout=60)
        require_equal(run.returncode, 3, run.stderr)
        require(run.stderr.startswith('REFUSED: source_tree_without_v4'), run.stderr)
        # (c) Richtiger Baum, Buch mit Schema-Tabelle aber ohne Kostentabellen → REFUSED, kein Traceback.
        try:
            DG.downgrade(str(db_path), core_stopped=True)
        except DG.DowngradeRefused as exc:
            require(str(exc).startswith('cost_tables_missing:agent_cost_activities'), str(exc))
        else:
            raise AssertionError('a book without cost tables was downgraded')
        with sqlite3.connect(db_path) as db:
            require_equal(db.execute('SELECT version FROM agent_cost_schema').fetchone()[0], 4, 'a refusal changed the schema')


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
