"""One exact Portal connection in the existing task ledger.

This first stage ends at a durable, NON-EXECUTABLE login preparation. It never
retrieves credential values, grants an approval, fills a form or clicks submit.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import stat
import time

from solvio.agent_runtime import action_contract as AC
from solvio.capabilities import task_action as TA
from solvio.portal.binding import BINDINGS, PortalBinding
from solvio.portal.client import PortalClient, _manifest_data
from solvio.portal.manifest import ActionManifest, FieldBinding, LOGIN
from solvio.portal.vault import PortalVault

SCHEMA = '''CREATE TABLE IF NOT EXISTS agent_portal_connections (
 connection_id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL UNIQUE,
 action_id TEXT NOT NULL, contract_digest TEXT NOT NULL, owner TEXT NOT NULL,
 native_json TEXT NOT NULL, native_digest TEXT NOT NULL, binding_digest TEXT NOT NULL,
 phase TEXT NOT NULL DEFAULT 'admitted', session_id TEXT NOT NULL DEFAULT '',
 manifest_json TEXT NOT NULL DEFAULT '', manifest_digest TEXT NOT NULL DEFAULT '',
 journal_ref TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL
);'''

_PORTAL = {name: getattr(TA.PortalCapabilities, name) for name in ('status', 'open', 'prepare_login', 'login', '_build')}
_CLIENT = {name: getattr(PortalClient, name) for name in
    ('call', 'ping', 'verify_build', 'open_session', 'open_connection', 'open_status', 'navigate', 'probe', 'execute', 'close_session')}
_VAULT = {name: getattr(PortalVault, name) for name in ('has_existing', '_ciphertext', 'ciphertext_identity')}


def _raw(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_raw(value).encode()).hexdigest()


def action_of(request):
    if request is None:
        return None
    request = AC.validate_request(request)
    return next((a for a in request.actions if (a['service'], a['operation']) == ('portal', 'connect')), None)


def native(service, portal_id):
    """Original configured adapter and encrypted-file metadata, never values."""
    if not TA._original(service, TA.TaskServiceAction, TA._TASK_ACTION_SERVICE_METHODS):
        raise ValueError('portal_native_service_required')
    from solvio.security.mobile_approval.control import MobileApprovalControlPlane
    from solvio.security.mobile_approval.store import ApprovalControlStore
    cp = getattr(service, 'control_plane', None)
    if (type(cp) is not MobileApprovalControlPlane or type(cp.store) is not ApprovalControlStore
            or cp.store.read_only or cp.store._closed or cp.store._conn is None):
        raise ValueError('portal_journal_required')
    portals = service.portals
    if (not TA._original(portals, TA.PortalCapabilities, _PORTAL)
            or not TA._original(portals.client, PortalClient, _CLIENT)
            or not TA._original(portals.vault, PortalVault, _VAULT)):
        raise ValueError('portal_native_client_required')
    binding = BINDINGS.get(portal_id)
    if type(binding) is not PortalBinding or binding.portal_id != portal_id:
        raise ValueError('portal_not_configured')
    client = portals.client
    s = os.stat(client.socket_path, follow_symlinks=False)
    if not os.path.isabs(client.socket_path) or not stat.S_ISSOCK(s.st_mode):
        raise ValueError('portal_native_socket_required')
    from solvio.portal.build import expected_build
    return {'portal_id': portal_id, 'binding': binding.as_data(),
        'socket': os.path.realpath(client.socket_path), 'socket_identity': [s.st_dev, s.st_ino, s.st_uid],
        'repo_root': os.path.realpath(client.repo_root), 'worker_build': expected_build(client.repo_root),
        'vault': portals.vault.ciphertext_identity(),
        'journal': {'core_instance_id': cp.core_instance_id, 'path': os.path.realpath(cp.store.path)},
        # This closed operation performs configured login only. No purchase,
        # generic form or provider dispatch is part of its cost contract.
        'operation': 'portal.connect', 'cost_contract': 'core:configured-portal-login-only:v1'}


def account_for(snapshot):
    return 'portal-connect-' + _digest(snapshot)[:32]


@dataclass(frozen=True)
class Admission:
    service: object
    owner: str
    action_json: str
    native_json: str

    def check(self, action, owner):
        if (owner != self.owner or _raw(action) != self.action_json
                or _raw(native(self.service, action['target']['portal_id'])) != self.native_json
                or action['account'] != account_for(json.loads(self.native_json))):
            raise ValueError('portal_admission_changed')


async def preflight(orch, request, principal, request_id):
    action = action_of(request)
    if action is None:
        return None
    owner = getattr(getattr(orch.router, '_mobile', None), 'owner_principal', '')
    if not owner or principal != owner:
        raise ValueError('portal_owner_required')
    with orch.ledger._open() as connection:
        # A replay only reads the existing task. TaskStartService independently
        # rejects changed bytes; native churn must not hide an accepted task.
        if connection.execute('SELECT 1 FROM agent_task_sources WHERE principal=? AND request_id=?',
                              (principal, request_id)).fetchone():
            return None
    service = getattr(orch, 'action_service', None)
    snapshot = native(service, action['target']['portal_id'])
    cp = getattr(service, 'control_plane', None)
    if cp is None or cp is not orch.control_plane or not cp.core_instance_id:
        raise ValueError('portal_journal_required')
    await service.portals.client.verify_build()
    admission = Admission(service, owner, _raw(action), _raw(snapshot))
    admission.check(action, principal)
    if not service.portals.vault.has_existing(BINDINGS[action['target']['portal_id']].credential_alias):
        raise ValueError('portal_credential_required')
    admission.check(action, principal)
    return admission


def record_admitted(connection, bound, owner, admission, now):
    action = action_of(AC.ActionRequest(bound._request_json))
    if action is None:
        return
    if type(admission) is not Admission:
        raise ValueError('portal_native_admission_required')
    admission.check(action, owner)
    snapshot = json.loads(admission.native_json)
    identity = 'pc-' + _digest({'task': bound.task_id, 'run': bound.run_id,
                              'action': action['action_id'], 'contract': bound.contract_digest})[:32]
    connection.execute('INSERT INTO agent_portal_connections '
        '(connection_id,task_id,run_id,action_id,contract_digest,owner,native_json,native_digest,binding_digest,updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)', (identity, bound.task_id, bound.run_id, action['action_id'],
        bound.contract_digest, owner, admission.native_json, _digest(snapshot), _digest(snapshot['binding']), now))


def read(ledger, run_id):
    with ledger._open() as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='agent_portal_connections'").fetchone():
            return None
        row = connection.execute('SELECT * FROM agent_portal_connections WHERE run_id=?', (run_id,)).fetchone()
    return dict(row) if row else None


def validate_row(connection, ledger, bound, receipt=None, capabilities=None):
    action = action_of(AC.ActionRequest(bound._request_json))
    if action is None:
        return
    row = connection.execute('SELECT * FROM agent_portal_connections WHERE run_id=?', (bound.run_id,)).fetchone()
    source = connection.execute('SELECT * FROM agent_task_sources WHERE run_id=?', (bound.run_id,)).fetchone()
    task = connection.execute('SELECT * FROM agent_tasks WHERE task_id=?', (bound.task_id,)).fetchone()
    if not row or not source or not task:
        raise ValueError('portal_connection_binding_missing')
    snapshot = json.loads(row['native_json'])
    expected = 'pc-' + _digest({'task': bound.task_id, 'run': bound.run_id,
                              'action': action['action_id'], 'contract': bound.contract_digest})[:32]
    if (row['connection_id'] != expected or row['task_id'] != bound.task_id
            or row['action_id'] != action['action_id'] or row['contract_digest'] != bound.contract_digest
            or row['owner'] != task['created_principal'] or source['authorizer'] != row['owner']
            or source['principal'] != row['owner'] or source['receipt_method'] not in ('app_session', 'dashboard_session')
            or row['native_digest'] != _digest(snapshot) or row['binding_digest'] != _digest(snapshot['binding'])
            or action['account'] != account_for(snapshot) or snapshot['portal_id'] != action['target']['portal_id']
            or (receipt is not None and (receipt.method, receipt.reference, receipt.authorizer) !=
                (source['receipt_method'], source['receipt_reference'], source['authorizer']))):
        raise ValueError('portal_connection_binding_changed')
    expected_caps = [{'name': bound.capability_grant.name, 'version': bound.capability_grant.version,
                      'constraints': bound.capability_grant.constraints}]
    if json.loads(source['capabilities']) != expected_caps or (capabilities is not None and capabilities != expected_caps):
        raise ValueError('portal_task_capabilities_changed')
    if row['phase'] in ('manifest_ready', 'prepared'):
        payload = json.loads(row['manifest_json'])
        manifest = payload.get('manifest', {})
        signature = manifest.get('page_signature')
        binding = PortalBinding.from_data(snapshot['binding'])
        if (type(signature) is not str or not re.fullmatch(r'[a-f0-9]{32}', signature)
                or _raw(payload) != row['manifest_json'] or _digest(payload) != row['manifest_digest']
                or payload != preparation_payload(row, binding, signature, bound.grant_reference)):
            raise ValueError('portal_preparation_binding_changed')


def preparation_payload(row, binding, signature, grant_reference):
    """Exact native fields, with an outer binding; old Manifest.digest is unchanged."""
    manifest = ActionManifest(portal_id=binding.portal_id, origin=binding.login_origin,
        page_url=binding.login_url, action_type=LOGIN, target=binding.form_selector or binding.submit_selector,
        method=binding.login_method, page_signature=signature, credential_alias=binding.credential_alias,
        principal=row['owner'], fields=(FieldBinding('Benutzer', binding.username_selector, alias=binding.credential_alias+'#user'),
                                      FieldBinding('Passwort', binding.password_selector, alias=binding.credential_alias)))
    return {'connection_id': row['connection_id'], 'task_id': row['task_id'], 'run_id': row['run_id'],
        'action_id': row['action_id'], 'contract_digest': row['contract_digest'], 'owner': row['owner'],
        'session_id': row['session_id'], 'native_digest': row['native_digest'],
        'grant_reference': grant_reference, 'manifest': _manifest_data(manifest)}


def checked(service, run_id):
    from solvio.agent_runtime.task_authority import TaskAuthority
    bound = AC.for_run(service.ledger, run_id)
    if bound is None:
        raise ValueError('portal_task_required')
    with service.ledger._open() as connection:
        validate_row(connection, service.ledger, bound)
    row = read(service.ledger, run_id)
    if row is None or not TaskAuthority(service.ledger).active(bound.grant_reference,
            task_id=bound.task_id, run_id=run_id).allowed:
        raise ValueError('portal_task_inactive')
    action = action_of(AC.ActionRequest(bound._request_json))
    if _raw(native(service, action['target']['portal_id'])) != row['native_json']:
        raise ValueError('portal_native_changed')
    return row


def view(ledger, run_id):
    row = read(ledger, run_id)
    if row is None:
        return None
    return {'id': row['connection_id'], 'portal_id': json.loads(row['native_json'])['portal_id'],
            'state': row['phase'], 'connected': False, 'login_wired': False}


def _phase(ledger, row, expected, phase, **fields):
    with ledger._open() as connection:
        connection.execute('BEGIN IMMEDIATE')
        values = dict(fields, phase=phase, updated_at=time.time())
        count = connection.execute('UPDATE agent_portal_connections SET '
            + ','.join(k + '=?' for k in values) + ' WHERE connection_id=? AND phase=?',
            (*values.values(), row['connection_id'], expected)).rowcount
        if count != 1:
            raise ValueError('portal_connection_claim_changed')


async def prepare(service, task_step):
    """After the genuine Router cost/step claim, open and bind; NEVER login."""
    row = checked(service, task_step.run_id)
    from solvio.secret_vault import context as SC
    from solvio.agent_runtime.steps import agent_principal
    with service.ledger._open() as c:
        claim = c.execute('SELECT * FROM agent_action_claims WHERE run_id=? AND action_id=?',
                          (row['run_id'], row['action_id'])).fetchone()
        step = c.execute('SELECT * FROM agent_steps WHERE step_id=?', (task_step.step_id,)).fetchone()
    bound = AC.for_run(service.ledger, row['run_id'])
    if (bound.grant_reference != task_step.reference or bound.task_id != task_step.task_id
            or SC.current().principal not in {row['owner'], agent_principal(row['run_id'])}
            or SC.current().capability != 'portal_login' or not claim or claim['status'] != 'claimed'
            or claim['step_id'] != task_step.step_id or not step or not step['dispatch_binding_digest']):
        raise ValueError('portal_router_claim_required')
    client = service.portals.client
    owner = row['owner']
    binding = BINDINGS[json.loads(row['native_json'])['portal_id']]
    if row['phase'] == 'admitted':
        await client.verify_build()
        checked(service, task_step.run_id)
        _phase(service.ledger, row, 'admitted', 'opening')
        checked(service, task_step.run_id)
        try:
            session_id = await client.open_connection(binding, owner_principal=owner,
                                                      connection_ref=row['connection_id'])
        except BaseException:
            # OPEN may already have happened. A later preparation can only READ
            # this exact worker reservation; it must not send another OPEN.
            raise
        checked(service, task_step.run_id)
        _phase(service.ledger, row, 'opening', 'opened', session_id=session_id)
    row = checked(service, task_step.run_id)
    if row['phase'] == 'opening':
        observed = await client.open_status(row['connection_id'], owner_principal=owner)
        checked(service, task_step.run_id)
        if (not observed.get('ok') or observed.get('state') != 'opened'
                or observed.get('binding_digest') != row['binding_digest']):
            raise ValueError('portal_open_outcome_unconfirmed')
        _phase(service.ledger, row, 'opening', 'opened', session_id=observed['session_id'])
    row = checked(service, task_step.run_id)
    if row['phase'] == 'opened':
        _phase(service.ledger, row, 'opened', 'navigating')
        response = await client.navigate(row['session_id'], binding.login_url, owner_principal=owner)
        checked(service, task_step.run_id)
        if not response.get('ok'):
            raise ValueError('portal_navigation_unconfirmed')
    row = checked(service, task_step.run_id)
    if row['phase'] == 'navigating':
        # Navigation is not repeated after a lost response. Probe only the
        # same owned session and require its actual configured login origin.
        probe = await client.probe(row['session_id'], owner_principal=owner)
        checked(service, task_step.run_id)
        native_binding = probe.get('session_binding', {})
        if (not probe.get('ok') or probe.get('origin') != binding.login_origin or probe.get('url') != binding.login_url
                or native_binding.get('session_id') != row['session_id']
                or native_binding.get('owner_principal') != owner
                or native_binding.get('binding_digest') != row['binding_digest']):
            raise ValueError('portal_manifest_binding_changed')
        payload = preparation_payload(row, binding, probe['page_signature'], task_step.reference)
        raw = _raw(payload)
        _phase(service.ledger, row, 'navigating', 'manifest_ready', manifest_json=raw,
               manifest_digest=_digest(payload))
    row = checked(service, task_step.run_id)
    if row['phase'] == 'manifest_ready':
        from solvio.security.mobile_approval.portal_preparation import record
        reference = await record(service.control_plane, row)
        checked(service, task_step.run_id)
        _phase(service.ledger, row, 'manifest_ready', 'prepared', journal_ref=reference)
    return checked(service, task_step.run_id)
