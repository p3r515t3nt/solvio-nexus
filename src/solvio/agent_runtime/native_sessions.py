"""Native execution references in the existing task ledger, not another agent.

The transport calls request_turn INSIDE the existing cost-dispatch runner,
before sending any native turn request. Only a fresh admission permits that
one send. A repeated admission is a readback, never permission to replay.
Native terminal observations do not settle costs or prove goal completion.
Admitted providers are the two native task workers (Codex, and Claude Code as
the brokered `worker/claude` of N8/C4); this never unlocks the blocked Claude
builder profile with a subscription session inside the jail.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import time

from solvio.agent_runtime import cost_dispatch as D, store as S, task_revisions as TR
from solvio.agent_runtime.task_authority import TaskAuthority

#: The closed provider set of native task sessions. One task may hold one
#: session per provider on the SAME workspace (a provider switch keeps the
#: workspace); `request_turn` still refuses any new session while another turn
#: of the task is not terminal/settled.
PROVIDERS = frozenset({'codex', 'claude-code'})

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_native_sessions (
 session_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
 provider TEXT NOT NULL, profile TEXT NOT NULL, policy_digest TEXT NOT NULL,
 workspace TEXT NOT NULL, native_thread_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
 UNIQUE(task_id,provider,profile)
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_native_thread_identity ON
 agent_native_sessions(provider,native_thread_id) WHERE native_thread_id<>'';
CREATE TABLE IF NOT EXISTS agent_native_turns (
 invocation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES agent_native_sessions(session_id) ON DELETE CASCADE,
 run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE, revision INTEGER NOT NULL,
 revision_digest TEXT NOT NULL, reservation_id TEXT NOT NULL UNIQUE,
 request_digest TEXT NOT NULL, native_turn_id TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL CHECK(state IN ('requested','started','terminal','unknown')),
 terminal_status TEXT NOT NULL DEFAULT '', requested_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_native_turn_identity ON
 agent_native_turns(session_id,native_turn_id) WHERE native_turn_id<>'';
"""


@dataclass(frozen=True)
class Session:
    session_id: str
    task_id: str
    provider: str
    profile: str
    policy_digest: str
    workspace: str
    native_thread_id: str


@dataclass(frozen=True)
class Turn:
    invocation_id: str
    session_id: str
    run_id: str
    revision: int
    revision_digest: str
    reservation_id: str
    request_digest: str
    native_turn_id: str
    state: str
    terminal_status: str


def _record(kind, row):
    return kind(**{key: row[key] for key in kind.__dataclass_fields__}) if row else None


def _text(value, field, *, digest=False):
    pattern = r'[a-f0-9]{64}' if digest else r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}'
    if (type(value) is not str or not re.fullmatch(pattern, value)
            or S._safe_text(value, 128, where='native_session.' + field) != value):
        raise ValueError('native_' + field + '_invalid')
    return value


def _workspace(value):
    if type(value) is not str or not value or len(value) > 2000:
        raise ValueError('native_workspace_invalid')
    path = Path(value)
    if (not path.is_absolute() or path.is_symlink() or not path.is_dir()
            or str(path.resolve()) != value or path == Path('/')
            or S._safe_text(value, 2000, where='native_session.workspace') != value):
        raise ValueError('native_workspace_invalid')
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('native_workspace_not_private')
    return value


class NativeSessions:
    def __init__(self, ledger, *, authority=None):
        self.ledger = ledger
        self.authority = authority or TaskAuthority(ledger)
        if self.authority.ledger is not ledger:
            raise ValueError('native_authority_ledger_mismatch')
        with ledger._open() as db:
            db.execute('BEGIN IMMEDIATE')
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    db.execute(statement)

    def session(self, session_id):
        with self.ledger._open() as db:
            return _record(Session, db.execute('SELECT * FROM agent_native_sessions WHERE session_id=?',
                                               (session_id,)).fetchone())

    def turn(self, invocation_id):
        with self.ledger._open() as db:
            return _record(Turn, db.execute('SELECT * FROM agent_native_turns WHERE invocation_id=?',
                                           (invocation_id,)).fetchone())

    def latest_turn(self, session_id):
        with self.ledger._open() as db:
            return _record(Turn, db.execute('SELECT * FROM agent_native_turns WHERE session_id=? '
                'ORDER BY rowid DESC LIMIT 1', (session_id,)).fetchone())

    def latest_native_turn(self, session_id):
        """Last observed terminal native turn; no-start attempts do not replace it."""
        with self.ledger._open() as db:
            return _record(Turn, db.execute('SELECT * FROM agent_native_turns WHERE session_id=? '
                "AND state='terminal' AND native_turn_id<>'' "
                'ORDER BY rowid DESC LIMIT 1', (session_id,)).fetchone())

    def active_claim(self, run_id):
        """Current dispatch ownership; outside its runner this returns None."""
        with self.ledger._open() as db:
            return D.active_task_invocation(db, self.ledger, run_id)

    def _active(self, db, task_id, run_id):
        grant = db.execute('SELECT reference FROM agent_task_grants WHERE task_id=? AND run_id=?',
                           (task_id, run_id)).fetchone()
        if not grant or not self.authority._verify(db, grant['reference'], None, {}, None,
                task_id=task_id, run_id=run_id, task_only=True).allowed:
            raise ValueError('native_task_authority_required')

    def bind(self, *, task_id, run_id, provider, profile, policy_digest, workspace):
        """Bind one private workspace/policy per task and native execution profile.

        The caller provisions an isolated directory; this metadata check does
        not grant filesystem access or replace the provider sandbox.
        """
        for key, value in (('task_id', task_id), ('run_id', run_id), ('profile', profile)):
            _text(value, key)
        if provider not in PROVIDERS:
            raise ValueError('native_provider_unsupported')
        _text(policy_digest, 'policy_digest', digest=True)
        workspace = _workspace(workspace)
        session_id = 'ns-' + hashlib.sha256(json.dumps([task_id, provider, profile],
                                                       separators=(',', ':')).encode()).hexdigest()
        with self.ledger._open() as db:
            db.execute('BEGIN IMMEDIATE')
            self._active(db, task_id, run_id)
            prior = db.execute('SELECT * FROM agent_native_sessions WHERE session_id=?', (session_id,)).fetchone()
            if prior:
                if (prior['policy_digest'], prior['workspace']) != (policy_digest, workspace):
                    raise ValueError('native_session_binding_changed')
                return _record(Session, prior)
            for row in db.execute('SELECT task_id,workspace FROM agent_native_sessions'):
                other = Path(row['workspace'])
                if row['task_id'] != task_id and (Path(workspace).is_relative_to(other)
                        or other.is_relative_to(Path(workspace))):
                    raise ValueError('native_workspace_other_task')
            db.execute('INSERT INTO agent_native_sessions '
                '(session_id,task_id,provider,profile,policy_digest,workspace,created_at) VALUES (?,?,?,?,?,?,?)',
                (session_id, task_id, provider, profile, policy_digest, workspace, time.time()))
            return _record(Session, db.execute('SELECT * FROM agent_native_sessions WHERE session_id=?',
                                               (session_id,)).fetchone())

    def request_turn(self, *, session_id, run_id, revision, invocation_id, request_digest='',
                     expected_native_thread_id=None, expected_previous_turn_id=None):
        """Return (record, fresh); only fresh=True authorizes one native send.

        The physical claim must already belong to the current dispatch runner.
        Existing requested/started/unknown records cannot be replayed, including
        after a restart. A new invocation also cannot pass an unfinished turn.
        """
        _text(invocation_id, 'invocation_id')
        if type(revision) is not int or revision < 1:
            raise ValueError('native_revision_invalid')
        if request_digest:
            _text(request_digest, 'request_digest', digest=True)
        if (expected_native_thread_id is None) != (expected_previous_turn_id is None):
            raise ValueError('native_prepared_binding_invalid')
        if expected_native_thread_id is not None:
            for key, value in (('thread_id', expected_native_thread_id), ('turn_id', expected_previous_turn_id)):
                if value != '':
                    _text(value, key)
        with self.ledger._open() as db:
            db.execute('BEGIN IMMEDIATE')
            session = db.execute('SELECT * FROM agent_native_sessions WHERE session_id=?', (session_id,)).fetchone()
            if not session:
                raise ValueError('native_session_missing')
            self._active(db, session['task_id'], run_id)
            _workspace(session['workspace'])
            task, run = TR._task_run(db, run_id)
            current = TR._current(db, task, run, TR._chain(db, task))
            if current['revision'] != revision:
                raise ValueError('native_revision_changed')
            prior = db.execute('SELECT * FROM agent_native_turns WHERE invocation_id=?', (invocation_id,)).fetchone()
            if prior:
                if ((prior['session_id'], prior['run_id'], prior['revision'], prior['revision_digest']) !=
                        (session_id, run_id, revision, current['digest']) or
                        request_digest and prior['request_digest'] != request_digest):
                    raise ValueError('native_turn_binding_changed')
                return _record(Turn, prior), False
            claim = D.active_task_invocation(db, self.ledger, run_id)
            if not claim or claim['invocation_id'] != invocation_id:
                raise ValueError('native_active_cost_claim_required')
            cost = db.execute('SELECT * FROM agent_provider_invocations WHERE reservation_id=?',
                              (claim['reservation_id'],)).fetchone()
            if (cost['task_id'] != session['task_id'] or cost['provider'] != session['provider']
                    or cost['subject_id'] != session['task_id'] or
                    request_digest and cost['request_digest'] != request_digest):
                raise ValueError('native_cost_binding_changed')
            # One task can switch native profile/provider, but never use a new
            # session to bypass an unfinished execution or unsettled old cost.
            blocked = db.execute('SELECT 1 FROM agent_native_turns t JOIN agent_native_sessions s '
                'ON s.session_id=t.session_id LEFT JOIN agent_provider_invocations i '
                'ON i.reservation_id=t.reservation_id LEFT JOIN agent_cost_reservations c '
                'ON c.reservation_id=t.reservation_id WHERE s.task_id=? '
                "AND (t.state<>'terminal' OR i.state IS NULL OR c.state IS NULL OR NOT "
                "((i.state='finished' AND c.state='settled') OR "
                "(t.terminal_status='not_started' AND i.state='not_dispatched' AND c.state='released'))) LIMIT 1",
                (session['task_id'],)).fetchone()
            if blocked:
                raise ValueError('native_turn_recovery_required')
            if expected_native_thread_id is not None:
                previous = db.execute('SELECT native_turn_id FROM agent_native_turns WHERE session_id=? '
                    "AND state='terminal' AND native_turn_id<>'' ORDER BY rowid DESC LIMIT 1",
                    (session_id,)).fetchone()
                if (session['native_thread_id'], previous['native_turn_id'] if previous else '') != (
                        expected_native_thread_id, expected_previous_turn_id):
                    raise ValueError('native_prepared_binding_changed')
            now = time.time()
            db.execute('INSERT INTO agent_native_turns '
                '(invocation_id,session_id,run_id,revision,revision_digest,reservation_id,request_digest,'
                "state,requested_at,updated_at) VALUES (?,?,?,?,?,?,?,'requested',?,?)",
                (invocation_id, session_id, run_id, revision, current['digest'], claim['reservation_id'],
                 cost['request_digest'], now, now))
            return _record(Turn, db.execute('SELECT * FROM agent_native_turns WHERE invocation_id=?',
                                           (invocation_id,)).fetchone()), True

    def _bound(self, db, invocation_id):
        turn = db.execute('SELECT * FROM agent_native_turns WHERE invocation_id=?', (invocation_id,)).fetchone()
        if not turn:
            raise ValueError('native_turn_missing')
        session = db.execute('SELECT * FROM agent_native_sessions WHERE session_id=?',
                             (turn['session_id'],)).fetchone()
        return turn, session

    def bind_thread(self, invocation_id, native_thread_id):
        """Bind observed thread identity under the live claim.

        A transport may first receive this in its started event. Only the
        requested intent is guaranteed durable before the actual native RPC.
        """
        _text(native_thread_id, 'thread_id')
        with self.ledger._open() as db:
            db.execute('BEGIN IMMEDIATE')
            turn, session = self._bound(db, invocation_id)
            claim = D.active_task_invocation(db, self.ledger, turn['run_id'])
            if not claim or claim['invocation_id'] != invocation_id:
                raise ValueError('native_active_cost_claim_required')
            self._active(db, session['task_id'], turn['run_id'])
            if turn['state'] != 'requested':
                raise ValueError('native_turn_not_requested')
            if session['native_thread_id'] and session['native_thread_id'] != native_thread_id:
                raise ValueError('native_thread_binding_changed')
            db.execute('UPDATE agent_native_sessions SET native_thread_id=? WHERE session_id=?',
                       (native_thread_id, session['session_id']))
        return self.session(session['session_id'])

    def started(self, invocation_id, *, native_thread_id, native_turn_id):
        _text(native_thread_id, 'thread_id')
        _text(native_turn_id, 'turn_id')
        with self.ledger._open() as db:
            db.execute('BEGIN IMMEDIATE')
            turn, session = self._bound(db, invocation_id)
            if session['native_thread_id'] != native_thread_id:
                raise ValueError('native_thread_binding_changed')
            if turn['state'] == 'started' and turn['native_turn_id'] == native_turn_id:
                return _record(Turn, turn)
            claim = D.active_task_invocation(db, self.ledger, turn['run_id'])
            if not claim or claim['invocation_id'] != invocation_id:
                raise ValueError('native_active_cost_claim_required')
            if turn['state'] != 'requested':
                raise ValueError('native_turn_not_requested')
            # Record an actual start even if cancellation raced the RPC. This
            # is evidence, not a fresh authorization after cancellation.
            db.execute("UPDATE agent_native_turns SET state='started',native_turn_id=?,updated_at=? "
                       'WHERE invocation_id=?', (native_turn_id, time.time(), invocation_id))
        return self.turn(invocation_id)

    def terminal(self, invocation_id, *, native_thread_id, native_turn_id, status):
        if status not in {'completed', 'failed', 'interrupted'}:
            raise ValueError('native_terminal_invalid')
        with self.ledger._open() as db:
            db.execute('BEGIN IMMEDIATE')
            turn, session = self._bound(db, invocation_id)
            if (not native_turn_id or session['native_thread_id'] != native_thread_id
                    or turn['native_turn_id'] != native_turn_id):
                raise ValueError('native_turn_binding_changed')
            if turn['state'] == 'terminal' and turn['terminal_status'] == status:
                return _record(Turn, turn)
            if turn['state'] != 'started':
                raise ValueError('native_terminal_recovery_required')
            db.execute("UPDATE agent_native_turns SET state='terminal',terminal_status=?,updated_at=? "
                       'WHERE invocation_id=?', (status, time.time(), invocation_id))
        return self.turn(invocation_id)

    def unknown(self, invocation_id):
        """Retain uncertainty. This does not release or settle a cost claim."""
        with self.ledger._open() as db:
            db.execute('BEGIN IMMEDIATE')
            turn, _ = self._bound(db, invocation_id)
            if turn['state'] not in {'terminal', 'unknown'}:
                db.execute("UPDATE agent_native_turns SET state='unknown',updated_at=? WHERE invocation_id=?",
                           (time.time(), invocation_id))
        return self.turn(invocation_id)

    def not_started(self, invocation_id):
        """Record the validated worker's explicit no-turn-start terminal proof.

        The caller must have accepted the complete worker protocol/result and
        its execution_status=not_started. A timeout, missing response, exception
        or mere quota wording is not this evidence. Costs settle independently.
        """
        with self.ledger._open() as db:
            db.execute('BEGIN IMMEDIATE')
            turn, _ = self._bound(db, invocation_id)
            claim = D.active_task_invocation(db, self.ledger, turn['run_id'])
            if not claim or claim['invocation_id'] != invocation_id:
                raise ValueError('native_active_cost_claim_required')
            if turn['state'] == 'terminal' and turn['terminal_status'] == 'not_started':
                return _record(Turn, turn)
            if turn['state'] != 'requested' or turn['native_turn_id']:
                raise ValueError('native_no_start_unproven')
            db.execute("UPDATE agent_native_turns SET state='terminal',terminal_status='not_started',updated_at=? "
                       'WHERE invocation_id=?', (time.time(), invocation_id))
        return self.turn(invocation_id)
