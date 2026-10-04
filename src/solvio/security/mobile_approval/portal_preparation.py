"""Secret-free Portal preparation in the existing approval database.

A preparation is NOT an approval, device decision or executable claim. Stage
1/2 deliberately cannot enter execute_approved. The later, separately reviewed
TaskGrant-to-login adapter must verify this exact record before any claim.
"""
import hashlib
import json
import os
import time

SCHEMA = '''CREATE TABLE IF NOT EXISTS portal_task_preparations (
 connection_id TEXT PRIMARY KEY, reference TEXT NOT NULL UNIQUE,
 core_instance_id TEXT NOT NULL, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
 owner TEXT NOT NULL, manifest_json TEXT NOT NULL, manifest_digest TEXT NOT NULL,
 prepared_at REAL NOT NULL
);'''


async def record(control_plane, row):
    from solvio.security.mobile_approval.control import MobileApprovalControlPlane
    from solvio.security.mobile_approval.store import ApprovalControlStore
    if type(control_plane) is not MobileApprovalControlPlane or type(control_plane.store) is not ApprovalControlStore:
        raise ValueError('portal_original_journal_required')
    store = control_plane.store
    raw = row['manifest_json']
    payload = json.loads(raw)
    native = json.loads(row['native_json'])
    core_id = control_plane.core_instance_id
    if (row['phase'] != 'manifest_ready' or len(raw.encode()) > 16384
            or hashlib.sha256(raw.encode()).hexdigest() != row['manifest_digest']
            or native['journal'] != {'core_instance_id': core_id, 'path': os.path.realpath(store.path)}
            or any(payload[k] != row[k] for k in ('connection_id', 'task_id', 'run_id', 'owner', 'session_id', 'native_digest'))):
        raise ValueError('portal_preparation_binding_changed')
    reference = 'ppc-' + hashlib.sha256((core_id + ':' + row['connection_id']).encode()).hexdigest()[:32]
    values = (row['connection_id'], reference, core_id, row['task_id'], row['run_id'], row['owner'], raw, row['manifest_digest'])
    def write():
        if store._closed or store.read_only or store._conn is None:
            raise ValueError('portal_journal_unavailable')
        c = store._conn
        c.execute(SCHEMA)
        c.execute('BEGIN IMMEDIATE')
        try:
            previous = c.execute('SELECT * FROM portal_task_preparations WHERE connection_id=?', (row['connection_id'],)).fetchone()
            if previous is not None:
                keys = ('connection_id', 'reference', 'core_instance_id', 'task_id', 'run_id', 'owner', 'manifest_json', 'manifest_digest')
                if tuple(previous[k] for k in keys) != values:
                    raise ValueError('portal_preparation_already_bound')
            else:
                c.execute('INSERT INTO portal_task_preparations VALUES (?,?,?,?,?,?,?,?,?)', (*values, time.time()))
            c.execute('COMMIT')
        except BaseException:
            c.execute('ROLLBACK')
            raise
        return reference
    return await store._run(write)
