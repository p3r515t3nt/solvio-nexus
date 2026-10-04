"""Internal verified interaction admission in the existing agent/cost database.

No HTTP/model factory is exposed. The server calls _verified_source only after
checking its existing session/attestation/task receipt. Identifiers and digests
are retained here, never conversation text. Authentication revocation must call
revoke_source; expiry and cancellation are checked again at physical claim time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import secrets
import time

from solvio.agent_runtime import store as S

_SEAL = object()
MAX_ACTIVITY_SECONDS = 3600
SOURCE_KINDS = frozenset({"app", "dashboard", "task", "voice_room"})
ACTIVITY_PURPOSES = frozenset({"adaptive_extract", "voice_delegate"})

SUBJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_cost_schema (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS agent_cost_subjects (
 subject_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('task','interaction')),
 task_id TEXT UNIQUE REFERENCES agent_tasks(task_id), principal TEXT NOT NULL,
 source_kind TEXT NOT NULL, conversation_id TEXT NOT NULL, created_at REAL NOT NULL,
 revoked_at REAL, CHECK((kind='task' AND task_id=subject_id) OR (kind='interaction' AND task_id IS NULL)),
 UNIQUE(kind,principal,source_kind,conversation_id)
);
CREATE TABLE IF NOT EXISTS agent_cost_source_revocations (
 source_kind TEXT NOT NULL, source_ref TEXT NOT NULL, revoked_at REAL NOT NULL,
 PRIMARY KEY(source_kind,source_ref)
);
CREATE TABLE IF NOT EXISTS agent_cost_activities (
 activity_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES agent_cost_subjects(subject_id),
 purpose TEXT NOT NULL CHECK(purpose IN ('adaptive_extract','voice_delegate')),
 operation_key TEXT NOT NULL DEFAULT '', principal TEXT NOT NULL,
 source_kind TEXT NOT NULL, source_ref TEXT NOT NULL, conversation_id TEXT NOT NULL,
 message_id TEXT NOT NULL, content_digest TEXT NOT NULL,
 task_id TEXT REFERENCES agent_tasks(task_id), run_id TEXT REFERENCES agent_runs(run_id),
 accepted_at REAL NOT NULL, expires_at REAL NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','completed','cancelled')),
 held_reason TEXT NOT NULL DEFAULT '', held_at REAL, resume_generation INTEGER NOT NULL DEFAULT 0 CHECK(resume_generation>=0),
 revoked_at REAL,
 UNIQUE(principal,source_kind,conversation_id,message_id,purpose,operation_key),
 CHECK(expires_at>accepted_at), CHECK((task_id IS NULL)=(run_id IS NULL))
);
CREATE TRIGGER IF NOT EXISTS agent_cost_activity_immutable BEFORE UPDATE OF
 activity_id,subject_id,purpose,operation_key,principal,source_kind,source_ref,conversation_id,message_id,
 content_digest,task_id,run_id,accepted_at,expires_at ON agent_cost_activities
 BEGIN SELECT RAISE(ABORT,'immutable_cost_activity'); END;
"""
INVOCATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_provider_invocations (
 reservation_id TEXT PRIMARY KEY REFERENCES agent_cost_reservations(reservation_id),
 invocation_id TEXT NOT NULL, subject_id TEXT NOT NULL REFERENCES agent_cost_subjects(subject_id),
 task_id TEXT REFERENCES agent_tasks(task_id), run_id TEXT REFERENCES agent_runs(run_id),
 activity_id TEXT REFERENCES agent_cost_activities(activity_id), phase TEXT NOT NULL,
 operation_id TEXT NOT NULL, ordinal INTEGER NOT NULL, provider TEXT NOT NULL,
 request_digest TEXT NOT NULL, process_owner TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('claimed','finished','unknown','not_dispatched')),
 claimed_at REAL NOT NULL, finished_at REAL,
 UNIQUE(subject_id,invocation_id), CHECK((task_id IS NULL)=(run_id IS NULL))
);
"""


def _statements(connection, script):
    # This helper is only for simple CREATE statements (the trigger is separate).
    for statement in script.split(';'):
        if statement.strip():
            connection.execute(statement)


def ensure_schema(ledger, legacy_schema):
    """Atomic N2 -> typed-subject migration, with FK enforcement left enabled.

    Rebuild is necessary: N2's mandatory task/run FKs cannot describe an actual
    standalone interaction. No reservation or physical claim is renumbered.
    """
    with ledger._open() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS agent_cost_schema (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)")
        version = db.execute("SELECT version FROM agent_cost_schema WHERE singleton=1").fetchone()
        if version:
            if version[0] == 2:
                _upgrade_activities_v3(db)
            elif version[0] != 3:
                raise ValueError("unsupported_cost_schema")
            return
        trigger_at = SUBJECT_SCHEMA.index('CREATE TRIGGER')
        _statements(db, SUBJECT_SCHEMA[:trigger_at])
        db.execute(SUBJECT_SCHEMA[trigger_at:].strip().rstrip(';'))
        columns = {r['name'] for r in db.execute('PRAGMA table_info(agent_cost_policies)')}
        legacy = 'task_id' in columns
        base = legacy_schema.replace('task_id', 'subject_id').replace(
            'REFERENCES agent_tasks(subject_id)', 'REFERENCES agent_cost_subjects(subject_id)')
        # Settings are unchanged; table indexes are recreated after the swap.
        base = re.sub(r'CREATE INDEX[^;]+;', '', base)
        tables = ('agent_cost_policies', 'agent_cost_reservations', 'agent_cost_authorizations',
                  'agent_provider_invocations')
        if legacy:
            db.execute("INSERT INTO agent_cost_subjects "
                "(subject_id,kind,task_id,principal,source_kind,conversation_id,created_at) "
                "SELECT p.task_id,'task',p.task_id,t.created_principal,'task',p.task_id,p.created_at "
                "FROM agent_cost_policies p JOIN agent_tasks t ON t.task_id=p.task_id")
            script = base + INVOCATION_SCHEMA
            for table in tables:
                script = script.replace(table, table + '_v2')
            _statements(db, script)
            existed = []
            for table in tables:
                old_columns = [r['name'] for r in db.execute(f'PRAGMA table_info({table})')]
                if not old_columns:
                    continue
                existed.append(table)
                new_columns = list(old_columns)
                expressions = list(old_columns)
                if table == 'agent_provider_invocations':
                    new_columns += ['subject_id', 'activity_id']
                    expressions += ['task_id', 'NULL']
                else:
                    new_columns[new_columns.index('task_id')] = 'subject_id'
                db.execute(f"INSERT INTO {table}_v2 ({','.join(new_columns)}) "
                           f"SELECT {','.join(expressions)} FROM {table}")
                if db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] != db.execute(
                        f'SELECT COUNT(*) FROM {table}_v2').fetchone()[0]:
                    raise ValueError('cost_migration_count_mismatch')
            if db.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('cost_migration_foreign_key_failure')
            for table in reversed(tables):
                if table in existed:
                    db.execute(f'DROP TABLE {table}')
            for table in tables:
                db.execute(f'ALTER TABLE {table}_v2 RENAME TO {table}')
        else:
            if columns:
                raise ValueError('unversioned_cost_schema')
            _statements(db, base + INVOCATION_SCHEMA)
        db.execute('CREATE INDEX IF NOT EXISTS agent_cost_task ON agent_cost_reservations(subject_id)')
        db.execute('CREATE INDEX IF NOT EXISTS agent_provider_invocations_task ON agent_provider_invocations(task_id)')
        db.execute('CREATE INDEX IF NOT EXISTS agent_provider_invocations_subject ON agent_provider_invocations(subject_id)')
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('cost_migration_foreign_key_failure')
        db.execute('INSERT INTO agent_cost_schema VALUES(1,3)')



def _upgrade_activities_v3(db):
    """Keep every claim/receipt while adding an explicit voice activity purpose.

    Rebuild the referencing invocation table too, with foreign keys enabled
    throughout. The caller's IMMEDIATE transaction covers the entire swap.
    """
    start = SUBJECT_SCHEMA.index('CREATE TABLE IF NOT EXISTS agent_cost_activities')
    end = SUBJECT_SCHEMA.index('CREATE TRIGGER')
    activity = SUBJECT_SCHEMA[start:end].replace('agent_cost_activities', 'agent_cost_activities_v3')
    invocation = INVOCATION_SCHEMA.replace('agent_provider_invocations', 'agent_provider_invocations_v3').replace(
        'REFERENCES agent_cost_activities(', 'REFERENCES agent_cost_activities_v3(')
    _statements(db, activity + invocation)
    for table in ('agent_cost_activities', 'agent_provider_invocations'):
        columns = ','.join(r['name'] for r in db.execute('PRAGMA table_info(' + table + ')'))
        db.execute('INSERT INTO ' + table + '_v3 (' + columns + ') SELECT ' + columns + ' FROM ' + table)
        if db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] != db.execute(
                'SELECT COUNT(*) FROM ' + table + '_v3').fetchone()[0]:
            raise ValueError('cost_migration_count_mismatch')
    if db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('cost_migration_foreign_key_failure')
    db.execute('DROP TABLE agent_provider_invocations')
    db.execute('DROP TABLE agent_cost_activities')
    db.execute('ALTER TABLE agent_cost_activities_v3 RENAME TO agent_cost_activities')
    db.execute('ALTER TABLE agent_provider_invocations_v3 RENAME TO agent_provider_invocations')
    db.execute(SUBJECT_SCHEMA[end:].strip().rstrip(';'))
    db.execute('CREATE INDEX agent_provider_invocations_task ON agent_provider_invocations(task_id)')
    db.execute('CREATE INDEX agent_provider_invocations_subject ON agent_provider_invocations(subject_id)')
    if db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('cost_migration_foreign_key_failure')
    db.execute('UPDATE agent_cost_schema SET version=3 WHERE singleton=1')


def _identifier(value, name, *, empty=False):
    from solvio.agent_runtime.costs import _text
    return _text(value, name, 512, empty=empty)


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('invalid_content_digest')
    return value


@dataclass(frozen=True)
class VerifiedInteractionSource:
    principal: str
    source_kind: str
    source_ref: str
    conversation_id: str
    message_id: str
    _seal: object = field(repr=False, compare=False, default=None)

    def __post_init__(self):
        if self._seal is not _SEAL:
            raise ValueError('verified_interaction_source_required')


def _verified_source(*, principal, source_kind, source_ref, conversation_id, message_id):
    """Core-only: arguments come from verified server objects, never request claims."""
    if source_kind not in SOURCE_KINDS:
        raise ValueError('unsupported_interaction_source')
    for name, value in locals().copy().items():
        _identifier(value, name)
    return VerifiedInteractionSource(principal, source_kind, source_ref, conversation_id, message_id, _SEAL)


@dataclass(frozen=True)
class CostSubject:
    subject_id: str
    kind: str
    task_id: str | None
    principal: str


@dataclass(frozen=True)
class ActivityBinding:
    activity_id: str
    subject_id: str
    principal: str
    source_kind: str
    source_ref: str
    conversation_id: str
    message_id: str
    content_digest: str
    task_id: str | None
    run_id: str | None
    expires_at: float
    purpose: str = 'adaptive_extract'
    operation_key: str = ''


def _task_authority_reason(db, *, source_kind, source_ref, task_id, run_id, now):
    """Read the existing grant; permit no broader authority after task success."""
    from solvio.agent_runtime.task_authority import _run_fingerprint, _grant_binding
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_task_grants'").fetchone():
        return 'activity_grant_missing'
    grant = db.execute('SELECT * FROM agent_task_grants WHERE run_id=? AND task_id=?', (run_id,task_id)).fetchone()
    task = db.execute('SELECT * FROM agent_tasks WHERE task_id=?', (task_id,)).fetchone()
    if not grant or not task or (source_kind == 'task' and grant['reference'] != source_ref):
        return 'activity_grant_mismatch'
    if grant['revoked_at'] is not None:
        return 'activity_grant_revoked'
    if grant['expires_at'] is not None and grant['expires_at'] <= now:
        return 'activity_grant_expired'
    if (task['created_origin'] not in ('trusted_interactive_app','trusted_dashboard') or
        grant['receipt_method'] not in ('app_session','dashboard_session') or
        (grant['receipt_method'] == 'app_session' and not grant['receipt_reference'].startswith('app:'))):
        return 'activity_personal_source_unproven'
    try:
        fingerprint = _run_fingerprint(db, task, run_id)
        binding = _grant_binding(task_id=task_id, run_id=run_id, fingerprint=fingerprint,
            method=grant['receipt_method'], receipt=grant['receipt_reference'], authorizer=grant['authorizer'],
            capabilities=json.loads(grant['capabilities']), expires_at=grant['expires_at'])
        if fingerprint != grant['task_fingerprint'] or binding != grant['binding_digest']:
            return 'activity_grant_binding_changed'
    except (ValueError,TypeError,KeyError):
        return 'activity_grant_binding_invalid'
    return ''


def activity_reason(db, activity_id, content_digest, *, now=None, generation=None):
    """Recheck under the same transaction that claims physical execution."""
    row = db.execute('SELECT a.*,s.revoked_at AS subject_revoked FROM agent_cost_activities a '
        'JOIN agent_cost_subjects s ON s.subject_id=a.subject_id WHERE activity_id=?', (activity_id,)).fetchone()
    if not row or row['content_digest'] != content_digest:
        return 'activity_binding_mismatch'
    if db.execute('SELECT 1 FROM agent_cost_source_revocations WHERE source_kind=? AND source_ref=?',
                  (row['source_kind'],row['source_ref'])).fetchone():
        return 'activity_source_revoked'
    if generation is not None and row['resume_generation'] != generation:
        return 'activity_generation_changed'
    if row['state'] != 'pending' or row['revoked_at'] is not None or row['subject_revoked'] is not None:
        return 'activity_inactive'
    if row['held_reason']:
        return 'activity_held:' + row['held_reason']
    if row['expires_at'] <= (time.time() if now is None else now):
        return 'activity_expired'
    if row['task_id']:
        reason = _task_authority_reason(db, source_kind=row['source_kind'], source_ref=row['source_ref'],
            task_id=row['task_id'], run_id=row['run_id'], now=time.time() if now is None else now)
        if reason:
            return reason
        parent = db.execute('SELECT t.state AS task_state,r.state,r.finished_at FROM agent_tasks t '
            'JOIN agent_runs r ON r.task_id=t.task_id WHERE t.task_id=? AND r.run_id=?',
            (row['task_id'], row['run_id'])).fetchone()
        if not parent or parent['task_state'] in (S.TASK_CANCELLED, S.TASK_FAILED):
            return 'activity_parent_inactive'
        # Only success is eligible for bounded post-run learning. Never reopen it.
        if parent['state'] in S.TERMINAL_STATES and parent['state'] != S.SUCCEEDED:
            return 'activity_parent_inactive'
        if parent['state'] == S.WAITING_USER:
            return 'activity_parent_waiting'
    return ''


class ActivityLedger:
    def __init__(self, ledger):
        from solvio.agent_runtime.costs import CostLedger
        self.ledger = ledger
        self.costs = CostLedger(ledger)

    def learning_view(self, principal, *, limit=25):
        """Owner-filtered durable observation state; no admission, retry or probe."""
        now = time.time()
        with self.ledger._open() as db:
            projection = """SELECT a.activity_id,a.run_id,a.source_kind,a.accepted_at,a.expires_at,
                a.held_reason,CASE WHEN a.state<>'pending' THEN a.state
                WHEN a.revoked_at IS NOT NULL OR s.revoked_at IS NOT NULL OR EXISTS(
                    SELECT 1 FROM agent_cost_source_revocations v WHERE v.source_kind=a.source_kind
                    AND v.source_ref=a.source_ref) THEN 'revoked'
                WHEN a.expires_at<=? THEN 'expired'
                WHEN a.held_reason<>'' THEN 'held' ELSE 'pending' END AS status
                FROM agent_cost_activities a JOIN agent_cost_subjects s ON s.subject_id=a.subject_id
                WHERE a.principal=? AND a.purpose='adaptive_extract'"""
            db.execute('BEGIN')
            counts = {r['status']: r['n'] for r in db.execute(
                'SELECT status,COUNT(*) AS n FROM ('+projection+') GROUP BY status', (now, principal))}
            rows = [dict(r) for r in db.execute('SELECT * FROM ('+projection+
                ") WHERE status IN ('held','pending','expired','revoked') ORDER BY accepted_at DESC LIMIT ?",
                (now, principal, min(50, max(1, int(limit)))))]
            return {'observed_at': now, 'counts': counts, 'activities': rows}

    def held_task(self, activity_id, principal):
        with self.ledger._open() as db:
            row = db.execute("SELECT task_id,run_id FROM agent_cost_activities WHERE activity_id=? "
                "AND principal=? AND source_kind='task' AND purpose='adaptive_extract' AND state='pending' AND held_reason<>''",
                (activity_id, principal)).fetchone()
            return dict(row) if row else None

    def hold_interrupted_observations(self):
        """Observation worker startup: queue ownership did not survive restart."""
        with self.ledger._open() as db:
            return db.execute("UPDATE agent_cost_activities SET held_reason='observation_interrupted',held_at=? "
                "WHERE state='pending' AND purpose='adaptive_extract' AND held_reason=''", (time.time(),)).rowcount

    def resume_bound_task(self, activity_id, principal, source, content_digest):
        """Explicit owner resume of the SAME reconstructable task observation."""
        if (type(source) is not VerifiedInteractionSource or source._seal is not _SEAL
                or source.source_kind != 'task' or source.principal != principal):
            raise ValueError('observation_resume_unavailable')
        with self.ledger._open() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM agent_cost_activities WHERE activity_id=? AND principal=?',
                             (activity_id, principal)).fetchone()
            if (not row or row['purpose'] != 'adaptive_extract' or row['state'] != 'pending' or not row['held_reason']
                    or row['content_digest'] != content_digest
                    or any(row[k] != getattr(source, k) for k in
                           ('source_kind','source_ref','conversation_id','message_id'))):
                raise ValueError('observation_resume_unavailable')
            db.execute("UPDATE agent_cost_activities SET held_reason='',held_at=NULL,resume_generation=resume_generation+1 WHERE activity_id=?",
                       (activity_id,))
            reason = activity_reason(db, activity_id, content_digest)
            if reason:
                raise ValueError(reason)  # Context rolls back the release too.
            return self._binding(row)

    def admit(self, source: VerifiedInteractionSource, *, content_digest: str,
              task_id=None, run_id=None, lifetime_seconds=MAX_ACTIVITY_SECONDS,
              purpose="adaptive_extract", operation_key="") -> ActivityBinding:
        if type(source) is not VerifiedInteractionSource or source._seal is not _SEAL:
            raise ValueError('verified_interaction_source_required')
        _digest(content_digest)
        if purpose not in ACTIVITY_PURPOSES:
            raise ValueError('unsupported_activity_purpose')
        _identifier(operation_key, 'activity_operation_key', empty=purpose == 'adaptive_extract')
        if purpose == 'adaptive_extract' and (operation_key or source.source_kind == 'voice_room'):
            raise ValueError('personal_observation_source_required')
        if purpose == 'voice_delegate' and (task_id is not None or source.source_kind == 'task'):
            raise ValueError('voice_interaction_source_required')
        if type(lifetime_seconds) not in (int, float) or not 0 < lifetime_seconds <= MAX_ACTIVITY_SECONDS:
            raise ValueError('invalid_activity_lifetime')
        if (task_id is None) != (run_id is None):
            raise ValueError('activity_task_run_required')
        if source.source_kind == 'task' and task_id is None:
            raise ValueError('task_source_requires_task')
        with self.ledger._open() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM agent_cost_activities WHERE principal=? AND source_kind=? '
                'AND conversation_id=? AND message_id=? AND purpose=? AND operation_key=?',
                (source.principal, source.source_kind, source.conversation_id, source.message_id, purpose, operation_key)).fetchone()
            if prior:
                if (prior['source_ref'], prior['content_digest'], prior['task_id'], prior['run_id']) != (
                        source.source_ref, content_digest, task_id, run_id):
                    raise ValueError('activity_replay_binding_mismatch')
                return self._binding(prior)
            now = time.time()
            if db.execute('SELECT 1 FROM agent_cost_source_revocations WHERE source_kind=? AND source_ref=?',
                          (source.source_kind,source.source_ref)).fetchone():
                raise ValueError('activity_source_revoked')
            if task_id:
                _identifier(task_id, 'task_id'); _identifier(run_id, 'run_id')
                parent = db.execute('SELECT t.created_principal,t.state AS task_state,r.state,r.finished_at '
                    'FROM agent_tasks t JOIN agent_runs r ON r.task_id=t.task_id '
                    'WHERE t.task_id=? AND r.run_id=?', (task_id, run_id)).fetchone()
                if (not parent or parent['created_principal'] != source.principal or
                    parent['task_state'] != S.TASK_ACTIVE or parent['state'] in S.TERMINAL_STATES or
                    parent['finished_at'] is not None):
                    raise ValueError('activity_task_not_active_or_owned')
                reason = _task_authority_reason(db, source_kind=source.source_kind, source_ref=source.source_ref,
                    task_id=task_id, run_id=run_id, now=now)
                if reason:
                    raise ValueError(reason)
                subject_id = task_id
                if not db.execute('SELECT 1 FROM agent_cost_policies WHERE subject_id=?', (subject_id,)).fetchone():
                    raise ValueError('cost_policy_missing')
            else:
                bound = ['interaction-cost-subject-v1', source.principal, source.source_kind, source.conversation_id]
                subject_id = 'ci-' + hashlib.sha256(json.dumps(bound, separators=(',', ':')).encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO agent_cost_subjects '
                    '(subject_id,kind,principal,source_kind,conversation_id,created_at) VALUES (?,\'interaction\',?,?,?,?)',
                    (subject_id, source.principal, source.source_kind, source.conversation_id, now))
                db.execute('INSERT OR IGNORE INTO agent_cost_policies (subject_id,ask_threshold_cents,created_at) '
                    'SELECT ?,ask_threshold_cents,? FROM agent_cost_settings WHERE singleton=1', (subject_id, now))
            subject = db.execute('SELECT revoked_at FROM agent_cost_subjects WHERE subject_id=?', (subject_id,)).fetchone()
            if not subject or subject['revoked_at'] is not None:
                raise ValueError('cost_subject_revoked')
            activity_id = 'ca-' + secrets.token_hex(16)
            db.execute('INSERT INTO agent_cost_activities '
                '(activity_id,subject_id,purpose,operation_key,principal,source_kind,source_ref,conversation_id,message_id,'
                'content_digest,task_id,run_id,accepted_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (activity_id, subject_id, purpose, operation_key, source.principal, source.source_kind, source.source_ref,
                 source.conversation_id, source.message_id, content_digest, task_id, run_id, now, now+lifetime_seconds))
            return self._binding(db.execute('SELECT * FROM agent_cost_activities WHERE activity_id=?', (activity_id,)).fetchone())

    @staticmethod
    def _binding(row):
        return ActivityBinding(**{name: row[name] for name in ActivityBinding.__dataclass_fields__})

    def binding(self, activity_id, *, content_digest, source=None) -> ActivityBinding:
        _identifier(activity_id, 'activity_id'); _digest(content_digest)
        with self.ledger._open() as db:
            reason = activity_reason(db, activity_id, content_digest)
            if reason:
                raise ValueError(reason)
            row = db.execute('SELECT * FROM agent_cost_activities WHERE activity_id=?', (activity_id,)).fetchone()
            if source is not None:
                if type(source) is not VerifiedInteractionSource or source._seal is not _SEAL:
                    raise ValueError('verified_interaction_source_required')
                if any(row[name] != getattr(source, name) for name in
                       ('principal','source_kind','source_ref','conversation_id','message_id')):
                    raise ValueError('activity_source_mismatch')
            return self._binding(row)

    def generation(self, activity_id):
        _identifier(activity_id, 'activity_id')
        with self.ledger._open() as db:
            row = db.execute('SELECT resume_generation FROM agent_cost_activities WHERE activity_id=?', (activity_id,)).fetchone()
            if not row:
                raise ValueError('activity_binding_mismatch')
            return row[0]

    def hold(self, activity_id, reason):
        """Persist a provider/cost hold; repeated ingress never authorizes a retry."""
        _identifier(activity_id, 'activity_id'); _identifier(reason, 'hold_reason')
        with self.ledger._open() as db:
            return bool(db.execute("UPDATE agent_cost_activities SET held_reason=?,held_at=? "
                "WHERE activity_id=? AND state='pending' AND held_reason=''",
                (reason,time.time(),activity_id)).rowcount)

    def release_hold(self, activity_id):
        """Core-only explicit owner resume. Neither expiry nor cost UNKNOWN is reset."""
        _identifier(activity_id, 'activity_id')
        with self.ledger._open() as db:
            return bool(db.execute("UPDATE agent_cost_activities SET held_reason='',held_at=NULL,resume_generation=resume_generation+1 "
                "WHERE activity_id=? AND state='pending' AND held_reason<>'' AND revoked_at IS NULL AND expires_at>?",
                (activity_id,time.time())).rowcount)

    def finish(self, activity_id, *, cancelled=False):
        _identifier(activity_id, 'activity_id')
        with self.ledger._open() as db:
            return bool(db.execute("UPDATE agent_cost_activities SET state=? WHERE activity_id=? AND state='pending'",
                ('cancelled' if cancelled else 'completed', activity_id)).rowcount)

    def revoke_source(self, *, source_kind, source_ref):
        _identifier(source_kind, 'source_kind'); _identifier(source_ref, 'source_ref')
        with self.ledger._open() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            db.execute('INSERT OR IGNORE INTO agent_cost_source_revocations VALUES (?,?,?)',
                       (source_kind,source_ref,now))
            return db.execute('UPDATE agent_cost_activities SET revoked_at=? '
                'WHERE source_kind=? AND source_ref=? AND revoked_at IS NULL',
                (now, source_kind, source_ref)).rowcount

    def revoke_subject(self, subject_id):
        _identifier(subject_id, 'subject_id')
        with self.ledger._open() as db:
            return bool(db.execute('UPDATE agent_cost_subjects SET revoked_at=? '
                'WHERE subject_id=? AND revoked_at IS NULL', (time.time(), subject_id)).rowcount)
