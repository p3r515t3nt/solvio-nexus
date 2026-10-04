"""Owner-approved account amendments to one proven-unstarted action.

The original task, grant, action payload and prior native receipts are immutable.
An additive chain records each explicit configured-account selection. It grants
no effect by itself: the original grant, native account check and action claim
remain required. Nothing here logs in or changes a service account.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
import re
import sqlite3
import time
import uuid

from solvio.agent_runtime import action_contract as AC, boundaries as B, store as S
from solvio.agent_runtime.task_authority import VerifiedTaskReceipt

_TABLE = 'agent_action_account_rebindings'
_FIELDS = ('reference', 'task_id', 'run_id', 'resource_id', 'contract_digest',
    'grant_reference', 'action_id', 'action_digest', 'original_account', 'previous_account',
    'selected_account', 'parent_reference', 'parent_digest', 'boundary_step_id',
    'boundary_receipt_digest', 'boundary_json', 'plan_revision', 'receipt_method',
    'receipt_reference', 'authorizer', 'created_at')
SCHEMA = '''CREATE TABLE IF NOT EXISTS agent_action_account_rebindings (
 reference TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
 resource_id TEXT NOT NULL, contract_digest TEXT NOT NULL, grant_reference TEXT NOT NULL,
 action_id TEXT NOT NULL, action_digest TEXT NOT NULL, original_account TEXT NOT NULL,
 previous_account TEXT NOT NULL, selected_account TEXT NOT NULL,
 parent_reference TEXT NOT NULL, parent_digest TEXT NOT NULL,
 boundary_step_id TEXT NOT NULL, boundary_receipt_digest TEXT NOT NULL,
 boundary_json TEXT NOT NULL, plan_revision INTEGER NOT NULL,
 receipt_method TEXT NOT NULL, receipt_reference TEXT NOT NULL, authorizer TEXT NOT NULL,
 created_at REAL NOT NULL, binding_digest TEXT NOT NULL,
 UNIQUE(task_id,action_id,parent_reference), UNIQUE(run_id,receipt_reference)
);'''
_DISPATCH_FIELDS = ('task_id', 'run_id', 'resource_id', 'contract_digest',
    'grant_reference', 'action_id', 'action_digest', 'step_id', 'dispatch_binding',
    'selected_account', 'account_rebind_reference', 'account_rebind_digest')
_DISPATCH_SCHEMA = '''CREATE TABLE IF NOT EXISTS agent_action_account_dispatches (
 step_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
 resource_id TEXT NOT NULL, contract_digest TEXT NOT NULL, grant_reference TEXT NOT NULL,
 action_id TEXT NOT NULL, action_digest TEXT NOT NULL, dispatch_binding TEXT NOT NULL,
 selected_account TEXT NOT NULL, account_rebind_reference TEXT NOT NULL,
 account_rebind_digest TEXT NOT NULL, binding_digest TEXT NOT NULL
)'''


@dataclass(frozen=True)
class AccountBinding:
    account: str
    reference: str = ''
    digest: str = ''


def initialize(ledger):
    with ledger._open() as connection:
        connection.executescript(SCHEMA)
        connection.execute(_DISPATCH_SCHEMA)


def _exists(connection):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)).fetchone() is not None


def _hash(value):
    return AC._hash('SOLVIO_ACTION_ACCOUNT_REBINDING_V1', value)


def _dispatch_selection(connection, bound, action, step):
    """Read the account captured by this exact semantic dispatch claim.

    Wall clocks may move backwards; the immutable step/dispatch binding orders
    authorization. A later owner selection cannot populate an older claim.
    """
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_action_account_dispatches'").fetchone() is None:
        return None
    row = connection.execute('SELECT * FROM agent_action_account_dispatches WHERE step_id=?',
                             (step['step_id'],)).fetchone()
    if row is None:
        return None
    body = {key: row[key] for key in _DISPATCH_FIELDS}
    grant = connection.execute('SELECT binding_digest FROM agent_task_grants WHERE reference=?',
                               (bound.grant_reference,)).fetchone()
    if grant is None:
        raise ValueError('action_account_dispatch_grant_missing')
    effect = AC.authority_digest('SOLVIO_TASK_EFFECT_V1', {
        'grant': bound.grant_reference, 'grant_binding': grant['binding_digest'],
        'capability': AC.CAPABILITY, 'version': AC.VERSION,
        'arguments': bound.action_arguments(action['action_id']),
        'task_id': bound.task_id, 'run_id': bound.run_id})
    dispatch = AC.authority_digest('SOLVIO_TASK_STEP_DISPATCH_V1', {
        'effect': effect, 'step_id': step['step_id'], 'seq': step['seq'], 'attempt': step['attempt']})
    if (any(row[key] != getattr(bound, key) for key in
            ('task_id', 'run_id', 'resource_id', 'contract_digest', 'grant_reference'))
            or row['action_id'] != action['action_id']
            or row['action_digest'] != AC._hash('SOLVIO_ACTION_V1', action)
            or step['run_id'] != bound.run_id or step['capability'] != AC.CAPABILITY
            or step['kind'] != 'capability'
            or step['dispatch_claimed_at'] is None
            or not row['dispatch_binding']
            or row['dispatch_binding'] != step['dispatch_binding_digest']
            or row['dispatch_binding'] != dispatch
            or row['binding_digest'] != AC._hash('SOLVIO_ACTION_ACCOUNT_DISPATCH_V1', body)):
        raise ValueError('action_account_dispatch_changed')
    return AccountBinding(row['selected_account'], row['account_rebind_reference'], row['account_rebind_digest'])


def bind_dispatch(connection, ledger, bound, action, step):
    """Capture current selection atomically with AC.claim_action, before effect.

    Called only for a fresh successful semantic claim, never a replay or a
    historical receipt. The existing task grant and dispatch are unchanged.
    """
    connection.execute(_DISPATCH_SCHEMA)
    if connection.execute('SELECT 1 FROM agent_action_account_dispatches WHERE step_id=?',
                          (step['step_id'],)).fetchone():
        raise ValueError('action_account_dispatch_already_bound')
    selected = _selection(_chain(connection, ledger, bound, action), action)
    body = {key: getattr(bound, key) for key in
            ('task_id', 'run_id', 'resource_id', 'contract_digest', 'grant_reference')}
    body.update(action_id=action['action_id'], action_digest=AC._hash('SOLVIO_ACTION_V1', action),
        step_id=step['step_id'], dispatch_binding=step['dispatch_binding_digest'],
        selected_account=selected.account, account_rebind_reference=selected.reference,
        account_rebind_digest=selected.digest)
    connection.execute('INSERT INTO agent_action_account_dispatches (' + ','.join(_DISPATCH_FIELDS)
        + ',binding_digest) VALUES (' + ','.join('?' for _ in range(len(_DISPATCH_FIELDS)+1)) + ')',
        (*[body[key] for key in _DISPATCH_FIELDS], AC._hash('SOLVIO_ACTION_ACCOUNT_DISPATCH_V1', body)))


def _chain(connection, ledger, bound, action):
    if not _exists(connection):
        return []
    rows = connection.execute('SELECT * FROM agent_action_account_rebindings WHERE task_id=? AND action_id=?',
                              (bound.task_id, action['action_id'])).fetchall()
    by_parent = {row['parent_reference']: row for row in rows}
    if len(by_parent) != len(rows):
        raise ValueError('action_account_rebinding_forked')
    grant = connection.execute('SELECT * FROM agent_task_grants WHERE reference=?', (bound.grant_reference,)).fetchone()
    if grant is None:
        raise ValueError('action_account_grant_missing')
    result, current = [], AccountBinding(action['account'])
    last_boundary, boundary_selection = '', current
    for _ in range(len(rows)):
        row = by_parent.get(current.reference)
        if row is None:
            raise ValueError('action_account_rebinding_chain_broken')
        body = {key: row[key] for key in _FIELDS}
        if (row['task_id'] != bound.task_id or row['run_id'] != bound.run_id
                or row['resource_id'] != bound.resource_id or row['contract_digest'] != bound.contract_digest
                or row['grant_reference'] != bound.grant_reference or row['action_id'] != action['action_id']
                or row['action_digest'] != AC._hash('SOLVIO_ACTION_V1', action)
                or row['original_account'] != action['account'] or row['previous_account'] != current.account
                or row['parent_digest'] != current.digest or row['binding_digest'] != _hash(body)
                or row['receipt_method'] != 'dashboard_session' or row['authorizer'] != grant['authorizer']
                or not re.fullmatch(re.escape(action['service']) + r'-[0-9a-f]{32}', row['selected_account'])):
            raise ValueError('action_account_rebinding_changed')
        boundary = B.UserBoundary.from_json(row['boundary_json'])
        if (boundary is None or boundary.kind != B.BROWSER_LOGIN or not boundary.repeat_step
                or boundary.step_id != row['boundary_step_id']):
            raise ValueError('action_account_rebinding_boundary_changed')
        if row['selected_account'] == current.account:
            raise ValueError('action_account_rebinding_without_change')
        proof = connection.execute('SELECT * FROM agent_action_claims WHERE run_id=? AND step_id=?',
            (bound.run_id, row['boundary_step_id'])).fetchone()
        if proof is None:
            proof = connection.execute('SELECT * FROM agent_action_attempt_receipts WHERE run_id=? AND step_id=?',
                (bound.run_id, row['boundary_step_id'])).fetchone()
        if proof is None or proof['status'] != 'not_dispatched' or proof['action_id'] != action['action_id']:
            raise ValueError('action_account_rebinding_nonstart_missing')
        # This receipt is necessarily noncompleted, so the native-account hook
        # returns without recursively resolving another amendment chain.
        native = AC._read_receipt(connection, ledger, proof)
        if (native.get('reason') != 'action_account_access_required'
                or native['receipt_digest'] != row['boundary_receipt_digest']):
            raise ValueError('action_account_rebinding_nonstart_changed')
        # Several owner choices may refine the same still-parked boundary.
        # A different boundary must come from a dispatch captured under the
        # previous choice, irrespective of clock corrections between them.
        if last_boundary != row['boundary_step_id']:
            last_boundary, boundary_selection = row['boundary_step_id'], current
        step = connection.execute('SELECT * FROM agent_steps WHERE step_id=?',
                                  (row['boundary_step_id'],)).fetchone()
        if step is None:
            raise ValueError('action_account_rebinding_nonstart_step_missing')
        dispatched = _dispatch_selection(connection, bound, action, step)
        if dispatched is None:
            # Legacy original-account nonstarts may be amended once. No
            # missing historical snapshot can assert an amended dispatch.
            dispatched = AccountBinding(action['account'])
        if dispatched != boundary_selection:
            raise ValueError('action_account_rebinding_nonstart_account_changed')
        current = AccountBinding(row['selected_account'], row['reference'], row['binding_digest'])
        result.append(dict(row))
    return result


def _selection(rows, action):
    return (AccountBinding(rows[-1]['selected_account'], rows[-1]['reference'], rows[-1]['binding_digest'])
            if rows else AccountBinding(action['account']))


def resolve_account(ledger, run_id, action_id, *, active=True):
    with ledger._open() as connection:
        connection.execute('BEGIN')
        row = connection.execute('SELECT * FROM agent_action_contracts WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise ValueError('action_contract_missing')
        bound = AC._with_grant(connection, ledger, AC._bound_row(row), active=active)
        action = next((a for a in bound.actions if a['action_id'] == action_id), None)
        if action is None:
            raise ValueError('action_not_bound')
        return _selection(_chain(connection, ledger, bound, action), action)


def _pending(connection, ledger, run_id):
    run = connection.execute('SELECT * FROM agent_runs WHERE run_id=?', (run_id,)).fetchone()
    task = (connection.execute('SELECT * FROM agent_tasks WHERE task_id=?', (run['task_id'],)).fetchone() if run else None)
    if run is None or task is None or task['scope'] != S.SCOPE_ACTION or run['state'] != S.WAITING_USER:
        raise ValueError('action_account_rebinding_requires_login_boundary')
    boundary = B.UserBoundary.from_json(run['boundary'])
    if boundary is None or boundary.kind != B.BROWSER_LOGIN or not boundary.repeat_step:
        raise ValueError('action_account_rebinding_requires_login_boundary')
    row = connection.execute('SELECT * FROM agent_action_contracts WHERE run_id=?', (run_id,)).fetchone()
    if row is None:
        raise ValueError('action_contract_missing')
    bound = AC._with_grant(connection, ledger, AC._bound_row(row), active=True)
    claim = connection.execute('SELECT * FROM agent_action_claims WHERE run_id=? AND step_id=?',
                               (run_id, boundary.step_id)).fetchone()
    action = next((a for a in bound.actions if claim is not None and a['action_id'] == claim['action_id']), None)
    step = connection.execute('SELECT * FROM agent_steps WHERE step_id=?', (boundary.step_id,)).fetchone()
    if (claim is None or claim['status'] != 'not_dispatched' or action is None
            or step is None or step['state'] != 'failed' or step['seq'] != boundary.seq
            or not AC.can_retry_step(connection, ledger, step, task_id=bound.task_id, run_id=run_id,
                capability=AC.CAPABILITY, arguments=bound.action_arguments(action['action_id']), version=AC.VERSION)):
        raise ValueError('action_account_rebinding_requires_proven_nonstart')
    native = AC._read_receipt(connection, ledger, claim)
    if native.get('reason') != 'action_account_access_required':
        raise ValueError('action_account_rebinding_requires_auth_nonstart')
    return run, bound, action, step, native


def pending_rebind(ledger, run_id):
    """Read-only current login boundary and optimistic account/receipt binding."""
    try:
        with ledger._open() as connection:
            connection.execute('BEGIN')
            run, bound, action, step, native = _pending(connection, ledger, run_id)
            current = _selection(_chain(connection, ledger, bound, action), action)
            return {'action_id': action['action_id'], 'service': action['service'],
                'target': action['target'], 'current_account': current.account,
                'receipt_digest': native['receipt_digest']}
    except (ValueError, KeyError, TypeError, sqlite3.Error):
        return None


def rebind_account(ledger, run_id, action_id, new_account, *, expected_account,
                   expected_receipt_digest, receipt):
    """Record a Dashboard-owner selection; leave the task parked for Resume.

    The endpoint constructs VerifiedTaskReceipt itself, obtains new_account from
    the configured native adapter, and supplies the exact optimistic bindings
    shown to the owner. Same receipt + same arguments replays its original
    amendment even after Resume; it never creates another effect or amendment.
    """
    if type(receipt) is not VerifiedTaskReceipt or receipt.method != 'dashboard_session':
        raise ValueError('dashboard_account_rebinding_receipt_required')
    initialize(ledger)
    with ledger._open() as connection:
        connection.execute('BEGIN IMMEDIATE')
        prior = connection.execute('SELECT * FROM agent_action_account_rebindings WHERE run_id=? AND receipt_reference=?',
                                   (run_id, receipt.reference)).fetchone()
        if prior is not None:
            if (prior['action_id'] != action_id or prior['selected_account'] != new_account
                    or prior['previous_account'] != expected_account
                    or prior['boundary_receipt_digest'] != expected_receipt_digest
                    or prior['receipt_method'] != receipt.method or prior['authorizer'] != receipt.authorizer):
                raise ValueError('action_account_rebinding_replay_changed')
            contract = connection.execute('SELECT * FROM agent_action_contracts WHERE run_id=?', (run_id,)).fetchone()
            if contract is None:
                raise ValueError('action_contract_missing')
            bound = AC._with_grant(connection, ledger, AC._bound_row(contract), active=False)
            action = next((a for a in bound.actions if a['action_id'] == action_id), None)
            if action is None or prior['reference'] not in {r['reference'] for r in _chain(connection, ledger, bound, action)}:
                raise ValueError('action_account_rebinding_replay_unbound')
            return AccountBinding(prior['selected_account'], prior['reference'], prior['binding_digest'])
        run, bound, action, step, native = _pending(connection, ledger, run_id)
        if action['action_id'] != action_id or type(new_account) is not str or not re.fullmatch(re.escape(action['service']) + r'-[0-9a-f]{32}', new_account):
            raise ValueError('action_account_choice_invalid')
        grant = connection.execute('SELECT * FROM agent_task_grants WHERE reference=?', (bound.grant_reference,)).fetchone()
        if grant['authorizer'] != receipt.authorizer:
            raise ValueError('action_account_owner_mismatch')
        current = _selection(_chain(connection, ledger, bound, action), action)
        if expected_account != current.account or expected_receipt_digest != native['receipt_digest']:
            raise ValueError('action_account_rebinding_view_changed')
        if current.account == new_account:
            raise ValueError('action_account_choice_unchanged')
        body = dict(reference='aar-' + uuid.uuid4().hex, task_id=bound.task_id, run_id=run_id,
            resource_id=bound.resource_id, contract_digest=bound.contract_digest,
            grant_reference=bound.grant_reference, action_id=action_id,
            action_digest=AC._hash('SOLVIO_ACTION_V1', action), original_account=action['account'],
            previous_account=current.account, selected_account=new_account,
            parent_reference=current.reference, parent_digest=current.digest,
            boundary_step_id=step['step_id'], boundary_receipt_digest=native['receipt_digest'],
            boundary_json=run['boundary'], plan_revision=run['plan_revision'],
            receipt_method=receipt.method, receipt_reference=receipt.reference,
            authorizer=receipt.authorizer, created_at=time.time())
        digest = _hash(body)
        connection.execute('INSERT INTO agent_action_account_rebindings (' + ','.join(_FIELDS) + ',binding_digest) VALUES ('
                           + ','.join('?' for _ in range(len(_FIELDS) + 1)) + ')',
                           tuple(body[k] for k in _FIELDS) + (digest,))
        return AccountBinding(new_account, body['reference'], digest)


def validate_native_receipt(connection, ledger, bound, action, receipt_body):
    """AC._read_receipt hook: confirm actual account against its historical amendment.

    Legacy unamended receipts remain readable. An amended execution must name
    its exact account and amendment, bound before that step's dispatch claim.
    Later owner choices neither rewrite nor invalidate this factual history.
    """
    if receipt_body.get('status') != 'completed':
        return
    step = connection.execute('SELECT * FROM agent_steps WHERE step_id=?', (receipt_body['step_id'],)).fetchone()
    if step is None or step['dispatch_claimed_at'] is None:
        raise ValueError('action_account_receipt_step_unbound')
    chain = _chain(connection, ledger, bound, action)
    selected = _dispatch_selection(connection, bound, action, step)
    if selected is None:
        selected = AccountBinding(action['account'])
    allowed = {AccountBinding(action['account']), *(AccountBinding(
        row['selected_account'], row['reference'], row['binding_digest']) for row in chain)}
    if selected not in allowed:
        raise ValueError('action_account_dispatch_selection_unbound')
    observed = receipt_body['native']['observed']
    fields = ('actual_account', 'account_rebind_reference', 'account_rebind_digest')
    if not any(k in observed for k in fields) and not selected.reference:
        return
    if any(observed.get(k) != expected for k, expected in zip(fields,
           (selected.account, selected.reference, selected.digest))):
        raise ValueError('action_actual_account_receipt_changed')
