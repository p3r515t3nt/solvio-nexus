"""Explicit owner account selection after a proven native authentication nonstart.

Public HTTPS task entrance, real grants/runtime/native clients, temporary stores;
only Google REST transport and the model protocol are synthetic.
"""
from __future__ import annotations
import asyncio
from dataclasses import replace
import json
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_agent_action_execution import world, admit
from solvio.agent_runtime import action_account_rebinding as AB, action_contract as AC, store as S
from solvio.agent_runtime.task_authority import VerifiedTaskReceipt
from solvio.capabilities import task_action as TA
from solvio.secret_vault import admin, policy as VP


def rejected(call):
    try: call()
    except ValueError: return
    raise AssertionError('invalid account selection was accepted')


async def parked(w):
    run_id = await admit(w)
    w.native.calendar._access_token = ''
    w.native.calendar._expires_at = 0
    w.native.transport.auth_error = 'invalid_grant'
    for _ in range(3): await w.orch.tick()
    require_equal(w.ledger.get_run(run_id).state, S.WAITING_USER)
    require_equal(w.native.transport.mutations, [])
    return run_id


def rotate(w):
    caps = tuple(name for (service, _), name in TA._SECRET_CAPABILITIES.items() if service in {'calendar', 'gmail'})
    admin.add(secret_ref=w.native.calendar.REFRESH_TOKEN_REF, kind=VP.SecretKind.PASSWORD,
        plaintext=b'synthetic-new-refresh-credential', allowed_capabilities=caps,
        allowed_targets=('https://oauth2.googleapis.com',), allowed_executors=(VP.ExecutorId.HTTP,),
        allow_background=True, store=w.native.broker.store, replace=True)
    w.native.calendar._access_token = ''; w.native.calendar._expires_at = 0
    return TA.account_identity('calendar', w.native.calendar)


def receipt(w, run_id, reference='browser:account-choice-1'):
    grant = w.orch.task_authority.for_run(run_id)
    return VerifiedTaskReceipt('dashboard_session', reference, grant.authorizer)


def apply(w, run_id, account, *, reference='browser:account-choice-1', view=None, owner_receipt=None):
    view = view or AB.pending_rebind(w.ledger, run_id)
    require(view is not None)
    return AB.rebind_account(w.ledger, run_id, view['action_id'], account,
        expected_account=view['current_account'], expected_receipt_digest=view['receipt_digest'],
        receipt=owner_receipt or receipt(w, run_id, reference))


def canonical_before(ledger, run_id):
    with ledger._open() as connection:
        grant = tuple(connection.execute('SELECT * FROM agent_task_grants WHERE run_id=?', (run_id,)).fetchone())
        contract = tuple(connection.execute('SELECT * FROM agent_action_contracts WHERE run_id=?', (run_id,)).fetchone())
        native = tuple(connection.execute('SELECT receipt_json,receipt_digest FROM agent_action_claims WHERE run_id=?', (run_id,)).fetchone())
    return grant, contract, native


async def t_rotation_owner_amendment_same_task_native_write_and_historical_receipt():
    async with world() as w:
        run_id = await parked(w)
        view = AB.pending_rebind(w.ledger, run_id)
        before = canonical_before(w.ledger, run_id)
        new_account = rotate(w)
        require(new_account != view['current_account'])
        amendment = apply(w, run_id, new_account)
        require_equal(canonical_before(w.ledger, run_id), before)
        require_equal(w.ledger.get_run(run_id).state, S.WAITING_USER)
        require_equal(AB.resolve_account(w.ledger, run_id, 'a1'), amendment)
        w.native.transport.auth_error = ''
        require(await w.orch.resume(run_id))
        w.runtime(); await w.orch.reconcile()
        await w.finish(run_id)
        receipts = AC.read_receipts(w.ledger, run_id)
        require_equal([r['status'] for r in receipts], ['not_dispatched', 'completed'])
        last = receipts[-1]
        require_equal(last['account'], view['current_account'])
        require_equal(last['native']['observed']['actual_account'], new_account)
        require_equal(last['native']['observed']['account_rebind_reference'], amendment.reference)
        require_equal(last['native']['observed']['account_rebind_digest'], amendment.digest)
        require_equal(len(w.native.transport.mutations), 1)
        require_equal(len(w.ledger.recent_runs()), 1)


async def t_exact_owner_post_replays_after_resume_and_completion_without_new_amendment():
    async with world() as w:
        run_id = await parked(w); view = AB.pending_rebind(w.ledger, run_id)
        account = rotate(w); owner_receipt = receipt(w, run_id)
        amendment = apply(w, run_id, account, view=view, owner_receipt=owner_receipt)
        w.native.transport.auth_error = ''; require(await w.orch.resume(run_id))
        require_equal(apply(w, run_id, account, view=view, owner_receipt=owner_receipt), amendment)
        await w.finish(run_id)
        require_equal(apply(w, run_id, account, view=view, owner_receipt=owner_receipt), amendment)
        with w.ledger._open() as c:
            require_equal(c.execute('SELECT count(*) FROM agent_action_account_rebindings').fetchone()[0], 1)
        require_equal(len(w.native.transport.mutations), 1)
        rejected(lambda: apply(w, run_id, 'calendar-' + '1' * 32, view=view, owner_receipt=owner_receipt))
        rejected(lambda: apply(w, run_id, account, view=view, reference='browser:another-post'))


async def t_old_owner_view_cannot_overwrite_a_newer_account_choice():
    async with world() as w:
        run_id = await parked(w); stale = AB.pending_rebind(w.ledger, run_id)
        first = apply(w, run_id, rotate(w))
        second_account = rotate(w)
        rejected(lambda: apply(w, run_id, second_account, view=stale, reference='browser:stale-post'))
        fresh = AB.pending_rebind(w.ledger, run_id)
        require_equal(fresh['current_account'], first.account)
        second = apply(w, run_id, second_account, view=fresh, reference='browser:fresh-post')
        require_equal(AB.resolve_account(w.ledger, run_id, 'a1'), second)
        require_equal(w.native.transport.mutations, [])


async def t_app_start_receipt_or_another_owner_cannot_authorize_rebinding():
    async with world() as w:
        run_id = await parked(w); account = rotate(w)
        native = receipt(w, run_id)
        rejected(lambda: apply(w, run_id, account, owner_receipt=replace(native, method='app_session')))
        rejected(lambda: apply(w, run_id, account, owner_receipt=replace(native, authorizer='another-owner')))
        require_equal(AB.resolve_account(w.ledger, run_id, 'a1').reference, '')
        require_equal(w.native.transport.mutations, [])


async def t_expired_grant_is_not_renewed_by_account_selection():
    async with world() as w:
        run_id = await parked(w); view = AB.pending_rebind(w.ledger, run_id)
        account = rotate(w); owner_receipt = receipt(w, run_id)
        w.orch.task_authority.revoke(w.orch.task_authority.for_run(run_id).reference, 'owner:cancelled')
        require_equal(AB.pending_rebind(w.ledger, run_id), None)
        rejected(lambda: apply(w, run_id, account, view=view, owner_receipt=owner_receipt))
        require_equal(w.native.transport.mutations, [])


async def t_unknown_native_outcome_can_never_be_rebound_into_a_second_try():
    async with world() as w:
        run_id = await admit(w); w.native.transport.drop_after_write = True
        await w.finish(run_id, S.FAILED)
        require_equal(AB.pending_rebind(w.ledger, run_id), None)
        account = rotate(w)
        rejected(lambda: AB.rebind_account(w.ledger, run_id, 'a1', account,
            expected_account=w.body['action_request']['actions'][0]['account'],
            expected_receipt_digest=AC.read_receipts(w.ledger, run_id)[0]['receipt_digest'], receipt=receipt(w, run_id)))
        require_equal(len(w.native.transport.mutations), 1)


async def t_changed_actual_account_fails_native_receipt_validation_even_with_rehashed_body():
    async with world() as w:
        run_id = await parked(w); amendment = apply(w, run_id, rotate(w))
        w.native.transport.auth_error = ''; require(await w.orch.resume(run_id)); await w.finish(run_id)
        with w.ledger._open() as c:
            row = c.execute('SELECT * FROM agent_action_claims WHERE run_id=?', (run_id,)).fetchone()
            body = json.loads(row['receipt_json'])
            body['native']['observed']['actual_account'] = 'calendar-' + '0' * 32
            encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
            c.execute('UPDATE agent_action_claims SET receipt_json=?,receipt_digest=? WHERE run_id=?',
                (encoded, AC._hash('SOLVIO_ACTION_RECEIPT_V1', body), run_id))
        rejected(lambda: AC.read_receipts(w.ledger, run_id))
        require_equal(len(w.native.transport.mutations), 1)


async def t_another_rotation_after_owner_choice_stays_held_before_dispatch():
    async with world() as w:
        run_id = await parked(w); apply(w, run_id, rotate(w)); rotate(w)
        w.native.transport.auth_error = ''; require(await w.orch.resume(run_id))
        await w.orch.tick()
        require_equal(w.native.transport.mutations, [])
        require(w.ledger.get_run(run_id).state != S.SUCCEEDED)


async def t_second_auth_failure_and_rotation_preserve_each_historical_amendment():
    async with world() as w:
        run_id = await parked(w)
        first = apply(w, run_id, rotate(w))
        require(await w.orch.resume(run_id))
        await w.orch.tick()
        require_equal(w.ledger.get_run(run_id).state, S.WAITING_USER)
        require_equal([r['status'] for r in AC.read_receipts(w.ledger, run_id)], ['not_dispatched', 'not_dispatched'])
        second = apply(w, run_id, rotate(w), reference='browser:account-choice-2')
        require(first.reference != second.reference)
        w.native.transport.auth_error = ''; require(await w.orch.resume(run_id))
        await w.finish(run_id)
        observed = AC.read_receipts(w.ledger, run_id)[-1]['native']['observed']
        require_equal(observed['account_rebind_reference'], second.reference)
        require_equal(observed['actual_account'], second.account)
        require_equal(len(w.native.transport.mutations), 1)


async def t_account_label_is_display_metadata_and_does_not_replace_native_identity():
    async with world() as w:
        rows = w.orch.action_service.accounts()
        labels = {row['service']: row['label'] for row in rows}
        require_equal(labels, {'calendar': 'Google-Kalender', 'gmail': 'Gmail', 'ha': 'Home Assistant'})
        require(all(row['account'].startswith(row['service'] + '-') for row in rows))


async def t_clock_correction_after_owner_choice_preserves_native_success():
    async with world() as w:
        run_id = await parked(w)
        amendment = apply(w, run_id, rotate(w))
        w.native.transport.auth_error = ''
        original_clock = w.orch.task_authority.clock
        w.orch.task_authority.clock = lambda: original_clock() - 10
        require(await w.orch.resume(run_id))
        await w.finish(run_id)
        receipts = AC.read_receipts(w.ledger, run_id)
        observed = next(r for r in receipts if r['status'] == 'completed')['native']['observed']
        require_equal(observed['actual_account'], amendment.account)
        require_equal(observed['account_rebind_reference'], amendment.reference)
        with w.ledger._open() as c:
            step = c.execute('SELECT s.* FROM agent_steps s JOIN agent_action_claims a ON a.step_id=s.step_id WHERE a.run_id=?', (run_id,)).fetchone()
            changed_at = c.execute('SELECT created_at FROM agent_action_account_rebindings WHERE reference=?', (amendment.reference,)).fetchone()[0]
            require(step['dispatch_claimed_at'] < changed_at)
        require_equal(len(w.native.transport.mutations), 1)
        require_equal(len(AC.completion_evidence(w.ledger, run_id)), 1)


async def t_clock_correction_between_choices_uses_parent_and_dispatch_chain():
    async with world() as w:
        run_id = await parked(w)
        first = apply(w, run_id, rotate(w))
        real_clock = time.time
        with patch.object(AB, 'time', SimpleNamespace(time=lambda: real_clock() - 10)):
            second = apply(w, run_id, rotate(w), reference='browser:second-same-boundary')
        require_equal(AB.resolve_account(w.ledger, run_id, 'a1'), second)
        require(await w.orch.resume(run_id)); await w.orch.tick()
        require_equal(w.ledger.get_run(run_id).state, S.WAITING_USER)
        with patch.object(AB, 'time', SimpleNamespace(time=lambda: real_clock() - 20)):
            third = apply(w, run_id, rotate(w), reference='browser:third-new-boundary')
        require_equal(AB.resolve_account(w.ledger, run_id, 'a1'), third)
        w.native.transport.auth_error = ''; require(await w.orch.resume(run_id))
        await w.finish(run_id)
        observed = next(r for r in AC.read_receipts(w.ledger, run_id) if r['status'] == 'completed')['native']['observed']
        require_equal(observed['account_rebind_reference'], third.reference)
        require_equal(len(w.native.transport.mutations), 1)
        with w.ledger._open() as c:
            timestamps = {r['reference']:r['created_at'] for r in c.execute('SELECT reference,created_at FROM agent_action_account_rebindings')}
            require(timestamps[first.reference] > timestamps[second.reference] > timestamps[third.reference])


async def t_valid_prior_amendment_cannot_replace_the_executed_account_receipt():
    async with world() as w:
        run_id = await parked(w)
        first = apply(w, run_id, rotate(w))
        second = apply(w, run_id, rotate(w), reference='browser:second-choice')
        w.native.transport.auth_error = ''; require(await w.orch.resume(run_id)); await w.finish(run_id)
        require_equal(AC.read_receipts(w.ledger, run_id)[-1]['native']['observed']['account_rebind_reference'], second.reference)
        with w.ledger._open() as c:
            row = c.execute('SELECT * FROM agent_action_claims WHERE run_id=?', (run_id,)).fetchone()
            body = json.loads(row['receipt_json'])
            body['native']['observed'].update(actual_account=first.account,
                account_rebind_reference=first.reference, account_rebind_digest=first.digest)
            c.execute('UPDATE agent_action_claims SET receipt_json=?,receipt_digest=? WHERE run_id=?',
                (json.dumps(body,sort_keys=True,ensure_ascii=False,separators=(',',':')),
                 AC._hash('SOLVIO_ACTION_RECEIPT_V1',body),run_id))
        rejected(lambda: AC.read_receipts(w.ledger, run_id))
        require_equal(len(w.native.transport.mutations), 1)


async def t_missing_account_dispatch_cannot_retroactively_authorize_amended_receipt():
    async with world() as w:
        run_id = await parked(w)
        apply(w, run_id, rotate(w))
        w.native.transport.auth_error = ''; require(await w.orch.resume(run_id)); await w.finish(run_id)
        with w.ledger._open() as c:
            c.execute('DELETE FROM agent_action_account_dispatches WHERE step_id=(SELECT step_id FROM agent_action_claims WHERE run_id=?)', (run_id,))
        rejected(lambda: AC.read_receipts(w.ledger, run_id))
        require_equal(len(w.native.transport.mutations), 1)


async def t_dispatch_attempt_mutation_is_not_the_same_account_binding():
    async with world() as w:
        run_id = await parked(w)
        apply(w, run_id, rotate(w))
        w.native.transport.auth_error = ''; require(await w.orch.resume(run_id)); await w.finish(run_id)
        with w.ledger._open() as c:
            c.execute('UPDATE agent_steps SET attempt=attempt+1 WHERE step_id=(SELECT step_id FROM agent_action_claims WHERE run_id=?)', (run_id,))
        rejected(lambda: AC.read_receipts(w.ledger, run_id))
        require_equal(len(w.native.transport.mutations), 1)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
