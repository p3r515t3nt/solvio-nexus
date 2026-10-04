"""Durable approval control-plane store (STEP S2A). MAC ONLY.

`approval_control.sqlite3` — separate from memory.sqlite3 / research_runs.sqlite3 /
background_jobs.sqlite3. File 0600, dir 0700, WAL, busy_timeout, integrity_check;
fails closed on corruption. Holds device registrations, approval requests (with the
EXACT stored action), the request state machine, single-use challenge nonces, one-time
enrollment tokens, and an audit log. Pending mobile approvals are NOT bound to a voice
session — they live here across restarts (TTL + explicit state machine).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
import time
from concurrent.futures import ThreadPoolExecutor

# P1A.8/C1: the store derives the security identities it checks. Neither module imports
# this one, so there is no cycle.
from . import app_attest as _AA
from . import crypto as _crypto
from . import execution as _X
from solvio.security.approval import action_digest as _action_digest

DASHBOARD_TASK_TOOLS = frozenset({"agent_task_research", "agent_task_build"})
DASHBOARD_METHOD = "dashboard_session"
MEMORY_COMMAND_METHOD = "browser_memory_command"
MEMORY_COMMAND_TOOLS = frozenset({"memory_confirm_candidate", "memory_decline_candidate",
                                  "memory_correct", "memory_forget", "memory_purge"})


@dataclass(frozen=True)
class DashboardAuthorization:
    """Server-read task authority, never a device or biometric assertion.

    Every use re-derives this value from the request and actual browser session.
    Constructing this type alone grants no authority.
    """
    approval_id: str
    session_id: str
    session_binding: str
    principal: str
    core_instance_id: str
    action_digest: str
    tool: str


@dataclass(frozen=True)
class BrowserCommandAuthorization:
    """N5: one explicit memory command, never task or device authority."""
    approval_id: str
    session_id: str
    session_binding: str
    principal: str
    core_instance_id: str
    action_digest: str
    tool: str

# ---- request state machine ------------------------------------------------
PENDING = "PENDING"
APPROVED = "APPROVED"
DENIED = "DENIED"
EXECUTING = "EXECUTING"
CONSUMED = "CONSUMED"
EXPIRED = "EXPIRED"
FAILED = "FAILED"

TERMINAL = {DENIED, CONSUMED, EXPIRED, FAILED}
_ALLOWED = {
    PENDING: {APPROVED, DENIED, EXPIRED},
    APPROVED: {EXECUTING, EXPIRED},
    EXECUTING: {CONSUMED, FAILED},
}

DEVICE_ACTIVE = "ACTIVE"
DEVICE_REVOKED = "REVOKED"

# device attestation lifecycle (STEP S2A.1)
ATT_PENDING = "PENDING_ATTESTATION"
ATT_ATTESTED = "ATTESTED"
ATT_FAILED = "ATTESTATION_FAILED"
ATT_UNATTESTED = "UNATTESTED"
ATT_REVOKED = "REVOKED"

# ---- P1A durable revocation identities -----------------------------------
# A device row can be replaced or re-created under a NEW device_id, so device-level
# status alone cannot express "this key must never approve again". Revocations are kept
# in their own durable table, keyed by identity KIND, and are terminal: there is
# deliberately no un-revoke path (an explicit owner-recovery policy is future work).
REVOKE_DEVICE = "device"                  # a concrete device record (device_id)
REVOKE_APPROVAL_KEY = "approval_key"      # sha256 of the approval public key (x963)
REVOKE_APP_ATTEST_KEY = "app_attest_key"  # Apple App Attest key id
REVOKE_KINDS = (REVOKE_DEVICE, REVOKE_APPROVAL_KEY, REVOKE_APP_ATTEST_KEY)
_REVOKE_EVENT = {REVOKE_DEVICE: "device_revoked",
                 REVOKE_APPROVAL_KEY: "approval_key_revoked",
                 REVOKE_APP_ATTEST_KEY: "app_attest_key_revoked"}


@dataclass(frozen=True)
class RevocationResult:
    """P1A.5/H1: what a revocation transaction ACTUALLY committed.

    Everything here comes out of the transaction that committed. Nothing is derived from a
    read taken beforehand — that mismatch is what let the admin CLI report identities as
    revoked while the transaction had revoked different ones. Contains only public
    identifiers; never secrets.
    """
    operation: str
    committed: bool
    newly_revoked: tuple           # ((kind, value), ...)
    already_revoked: tuple         # ((kind, value), ...)
    affected_devices: tuple        # (device_id, ...)

    @property
    def identities(self) -> tuple:
        return self.newly_revoked + self.already_revoked


class StateMigrationRequired(Exception):
    """P1A.7/§17: the store needs a migration that a read-only caller must never apply."""


class ApprovalStoreError(Exception):
    ...


class ApprovalStoreCorrupt(ApprovalStoreError):
    ...


class ConcurrentTransition(ApprovalStoreError):
    """P1B/F9: the row moved between the read and the compare-and-swap. Nobody may treat
    this as a success — the caller lost a single-use race."""


class ExecutionBypassError(ApprovalStoreError):
    """FREEZE/F3: something tried to enter EXECUTING outside the journalled claim."""


class IllegalTransition(ApprovalStoreError):
    ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY, key_id TEXT NOT NULL, public_key_x963 TEXT NOT NULL,
    principal TEXT NOT NULL, transport_cred_hash TEXT, attested INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ACTIVE', enrolled_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id TEXT PRIMARY KEY, principal TEXT NOT NULL, tool TEXT NOT NULL,
    mode TEXT NOT NULL, task TEXT NOT NULL, workspace TEXT NOT NULL,
    action_digest TEXT NOT NULL, human_summary TEXT NOT NULL, state TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL, decided_at REAL,
    decided_device TEXT, error TEXT,
    request_core_instance_id TEXT NOT NULL DEFAULT '',
    decision_method TEXT NOT NULL DEFAULT '', decided_session TEXT);
CREATE INDEX IF NOT EXISTS idx_req_state ON approval_requests(state);
CREATE TABLE IF NOT EXISTS challenges (
    challenge_nonce TEXT PRIMARY KEY, approval_id TEXT NOT NULL, device_id TEXT NOT NULL,
    principal TEXT NOT NULL, action_digest TEXT NOT NULL, issued_at REAL NOT NULL,
    expires_at REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
    payload_sha256 TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_ch_appr ON challenges(approval_id);
CREATE TABLE IF NOT EXISTS enrollment_tokens (
    token_hash TEXT PRIMARY KEY, principal TEXT NOT NULL, created_at REAL NOT NULL,
    expires_at REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
-- Browser credentials belong to this control plane, but are never device credentials
-- or Face-ID proofs. Separate hash domains and tables prevent credential substitution.
CREATE TABLE IF NOT EXISTS browser_enrollment_tokens (
    token_hash TEXT PRIMARY KEY, principal TEXT NOT NULL, core_instance_id TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL, consumed_at REAL,
    CHECK (expires_at > created_at));
CREATE TABLE IF NOT EXISTS browser_sessions (
    session_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, csrf_hash TEXT NOT NULL,
    principal TEXT NOT NULL, core_instance_id TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL,
    CHECK (expires_at > created_at));
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, approval_id TEXT,
    device_id TEXT, event TEXT NOT NULL, reason TEXT, identity TEXT);
CREATE TABLE IF NOT EXISTS attestation_challenges (
    enrollment_id TEXT PRIMARY KEY, nonce TEXT NOT NULL, device_id TEXT NOT NULL,
    principal TEXT NOT NULL, approval_key_id TEXT NOT NULL,
    approval_pubkey_sha256 TEXT NOT NULL, binding_raw BLOB NOT NULL,
    issued_at REAL NOT NULL, expires_at REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
    -- P1A.7/H1: the challenge carries the EXACT identity it was issued for. Attestation
    -- authority must never be reconstructed from the mutable devices row: a second
    -- begin_enrollment used to overwrite it, and an older challenge then attested a binding
    -- its attestation had never proved.
    app_attest_key_id TEXT, superseded INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- P1A.6/§10: append-only record of every binding that was ever TRUSTED for a device_id.
-- `devices` holds only the CURRENT binding, so a legitimate key rotation used to orphan the
-- previous approval key: revoke-device could no longer see it, and it stayed enrollable.
-- Rows are written when an attestation SUCCEEDS (the moment a binding becomes trusted) and
-- are never updated or deleted.
CREATE TABLE IF NOT EXISTS device_identity_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
    approval_key_sha256 TEXT, app_attest_key_id TEXT,
    bound_at REAL NOT NULL, provenance TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_hist_dev ON device_identity_history(device_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hist_binding ON device_identity_history(
    device_id, approval_key_sha256, app_attest_key_id);
-- P1C/F4: the durable execution journal. Before it, a crash after the claim and a crash
-- after the external side effect left an IDENTICAL row (state=EXECUTING) — SOLVIO could not
-- tell "definitely did not happen" from "may have happened", so it could only wedge.
CREATE TABLE IF NOT EXISTS execution_attempts (
    attempt_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,          -- stable identity of the approved logical action
    approval_id TEXT NOT NULL,
    device_id TEXT,
    capability TEXT NOT NULL,
    semantics TEXT NOT NULL,             -- declared server-side, recorded at claim time
    idempotency_key TEXT NOT NULL,       -- derived, never caller-supplied
    status TEXT NOT NULL,
    lease_owner TEXT, lease_expires_at REAL,
    claimed_at REAL NOT NULL, boundary_at REAL, finished_at REAL,
    detail TEXT,
    -- FREEZE/F2: the authority this execution was CLAIMED under, captured server-side
    -- inside the claim transaction. A recovery retry is a NEW external write, so it must
    -- match the authority the human's approval was actually exercised under -- not merely
    -- "some device that is healthy today". A completed re-enrolment or key rotation
    -- replaces this material while keeping the same device_id, and without a snapshot the
    -- retry would silently run under the new identity.
    claim_identity_bound INTEGER NOT NULL DEFAULT 0,   -- 0 = legacy row, never auto-retried
    claim_core_instance_id TEXT,
    claim_principal TEXT,
    claim_environment TEXT,
    claim_enrollment_id TEXT,
    claim_approval_key_sha256 TEXT,
    claim_app_attest_key_id TEXT,
    claim_authorization_method TEXT NOT NULL DEFAULT 'face_id',
    claim_browser_session_id TEXT, claim_browser_session_binding TEXT,
    claim_action_digest TEXT);
CREATE INDEX IF NOT EXISTS idx_attempt_exec ON execution_attempts(execution_id);
CREATE INDEX IF NOT EXISTS idx_attempt_appr ON execution_attempts(approval_id);
-- At most ONE success per logical action, enforced by the database rather than by a code
-- path someone could forget to call.
CREATE UNIQUE INDEX IF NOT EXISTS idx_attempt_one_success
    ON execution_attempts(execution_id) WHERE status='SUCCEEDED';
-- At most ONE open attempt per logical action: no two owners in flight.
CREATE UNIQUE INDEX IF NOT EXISTS idx_attempt_one_open
    ON execution_attempts(execution_id)
    WHERE status IN ('CLAIMED','EXTERNAL_PENDING','UNKNOWN');
CREATE TABLE IF NOT EXISTS revocations (
    kind TEXT NOT NULL, value TEXT NOT NULL, reason TEXT, revoked_at REAL NOT NULL,
    PRIMARY KEY (kind, value));
"""


class ApprovalControlStore:
    def __init__(self, path: str, *, read_only: bool = False) -> None:
        self.path = path
        # P1A.7/§16: a read-only store never migrates and never writes.
        self.read_only = read_only
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="solvio-approval")
        self._conn: sqlite3.Connection | None = None
        self._closed = False
        self.read_only = read_only

    async def _run(self, fn, *a):
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *a)

    async def open(self) -> None:
        await self._run(self._open)

    def _open(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        first = not os.path.exists(self.path)
        if self.read_only:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            self._conn = conn
            if self._pending_migrations(conn) or self._schema_incomplete(conn):
                conn.close()
                self._conn = None
                raise StateMigrationRequired(
                    "state migration required — run the writable startup path once. "
                    "Read-only admin never migrates.")
            return
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        # P1A.4/§12: this database decides who may approve privileged actions. A revoke that
        # printed "terminal" must not evaporate on power loss, so commits are fsynced.
        # FULL protects the commit point; it does not and cannot promise more than the
        # filesystem and hardware honour.
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            conn.close()
            raise ApprovalStoreCorrupt("integrity_check failed")
        conn.executescript(_SCHEMA)
        self._migrate(conn)
        self._canonicalise_app_attest_ids(conn)
        self._backfill_legacy_revocations(conn)
        self._mark_claim_snapshot_migration(conn)
        if first:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self._conn = conn

    _REQUIRED_TABLES = ("devices", "revocations", "approval_requests", "audit",
                       "attestation_challenges", "device_identity_history", "schema_meta",
                       "execution_attempts", "browser_enrollment_tokens", "browser_sessions")
    _REQUIRED_COLUMNS = {"audit": ("identity",),
                         "approval_requests": ("request_core_instance_id", "decision_method", "decided_session"),
                         "attestation_challenges": ("app_attest_key_id", "superseded"),
                         "execution_attempts": ("claim_identity_bound",
                                                "claim_enrollment_id",
                                                "claim_approval_key_sha256", "claim_authorization_method",
                                                "claim_browser_session_id", "claim_browser_session_binding",
                                                "claim_action_digest"),
                         "browser_enrollment_tokens": ("token_hash", "principal",
                             "core_instance_id", "created_at", "expires_at", "consumed_at"),
                         "browser_sessions": ("session_id", "token_hash", "csrf_hash",
                             "principal", "core_instance_id", "created_at", "expires_at",
                             "revoked_at")}

    @classmethod
    def _schema_incomplete(cls, conn) -> bool:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if set(cls._REQUIRED_TABLES) - names:
            return True
        for table, cols in cls._REQUIRED_COLUMNS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if set(cols) - have:
                return True
        return False

    SCHEMA_VERSION = 3          # FREEZE/F2: claim-time authority snapshot on attempts
    #                             2 = P1A.7 legacy REVOKED rows backfilled into `revocations`

    @classmethod
    def _mark_claim_snapshot_migration(cls, conn) -> None:
        """FREEZE/F2: stamp schema v3 once the claim-time snapshot columns exist.

        There is deliberately NO backfill. An attempt claimed before this migration has no
        trustworthy record of the authority it ran under; writing a plausible-looking
        snapshot from today's device row would manufacture exactly the false history the
        snapshot exists to prevent. Those rows keep `claim_identity_bound = 0` and are
        refused a new external write — see `_recheck_execution_authority`.

        Idempotent and atomic: a second process that already won the race just commits.
        """
        if cls._schema_version(conn) >= 3:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            if cls._schema_version(conn) >= 3:      # another process won the race
                conn.execute("COMMIT")
                return
            legacy = conn.execute(
                "SELECT COUNT(*) FROM execution_attempts WHERE claim_identity_bound=0"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(3),))
            conn.execute(
                "INSERT INTO audit (ts, approval_id, device_id, event, reason, identity) "
                "VALUES (?,?,?,?,?,?)",
                (time.time(), None, None, "schema_migrated",
                 f"v3: claim-time authority snapshot; {legacy} legacy attempt(s) left "
                 f"unbound and therefore never auto-retried", None))
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    @staticmethod
    def _schema_version(conn) -> int:
        row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _pending_migrations(cls, conn) -> bool:
        return cls._schema_version(conn) < cls.SCHEMA_VERSION

    @classmethod
    def _backfill_legacy_revocations(cls, conn) -> None:
        """P1A.7/H2: identity-level revocations for devices revoked BEFORE this table existed.

        On core main (3233e99) there was no `revocations` table at all — `revoke_device` was
        `set_device_status(device_id, 'REVOKED')` and nothing else. After the upgrade those
        rows still blocked their own device_id (the enrollment upsert refuses a REVOKED row),
        but the KEY MATERIAL was free again: the same approval key and app-attest key
        re-enrolled under a fresh device_id and regained full authority, with no signal to
        the operator. Reproduced before this fix.

        Only what the row still reliably carries is used. Nothing historical is invented: a
        legacy row that lost its key material contributes just its device identity, and that
        limitation is documented rather than papered over.
        """
        if cls._schema_version(conn) >= 2:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            if cls._schema_version(conn) >= 2:      # another process won the race
                conn.execute("COMMIT")
                return
            from solvio.security.mobile_approval.app_attest import (
                AppAttestIdentityError, canonical_app_attest_key_id)
            from solvio.security.mobile_approval.crypto import fingerprint
            added = 0
            rows = conn.execute("SELECT * FROM devices WHERE status=?",
                                (DEVICE_REVOKED,)).fetchall()
            for row in rows:
                entries = [(REVOKE_DEVICE, row["device_id"])]
                pk = row["public_key_x963"]
                if pk:
                    try:
                        entries.append((REVOKE_APPROVAL_KEY, fingerprint(bytes.fromhex(pk))))
                    except ValueError:
                        pass
                aak = row["app_attest_key_id"] if "app_attest_key_id" in row.keys() else None
                if aak:
                    try:
                        entries.append((REVOKE_APP_ATTEST_KEY,
                                        canonical_app_attest_key_id(aak)))
                    except (AppAttestIdentityError, ValueError):
                        pass
                for kind, value in entries:
                    cur = conn.execute(
                        "INSERT INTO revocations (kind, value, reason, revoked_at) "
                        "VALUES (?,?,?,?) ON CONFLICT(kind, value) DO NOTHING",
                        (kind, value, "legacy pre-P1A revoked device", time.time()))
                    if cur.rowcount == 1:
                        added += 1
                        conn.execute(
                            "INSERT INTO audit (ts, approval_id, device_id, event, reason, "
                            "identity) VALUES (?,?,?,?,?,?)",
                            (time.time(), None, row["device_id"], "legacy_revocation_backfilled",
                             "pre-P1A devices.status=REVOKED", f"{kind}:{value}"))
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(2),))
            conn.execute(
                "INSERT INTO audit (ts, approval_id, device_id, event, reason, identity) "
                "VALUES (?,?,?,?,?,?)",
                (time.time(), None, None, "schema_migrated",
                 f"v2: {added} legacy identities backfilled", None))
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    @staticmethod
    def _canonicalise_app_attest_ids(conn) -> None:
        """P1A.4/C1 migration: bring any stored non-canonical App Attest key id onto the
        canonical form, idempotently and in one transaction.

        Without this, a revocation recorded before the fix would simply stop matching after
        it — the lookup would silently miss and the key would count as un-revoked. Rows that
        cannot be decoded are LEFT ALONE: they never matched a real Apple key anyway, and
        rewriting an unparseable identity would invent one.
        """
        from solvio.security.mobile_approval.app_attest import (
            AppAttestIdentityError, canonical_app_attest_key_id)

        def canon(v):
            try:
                return canonical_app_attest_key_id(v)
            except (AppAttestIdentityError, ValueError):
                return None

        conn.execute("BEGIN IMMEDIATE")
        try:
            for row in conn.execute(
                    "SELECT device_id, app_attest_key_id FROM devices "
                    "WHERE app_attest_key_id IS NOT NULL").fetchall():
                c = canon(row["app_attest_key_id"])
                if c and c != row["app_attest_key_id"]:
                    conn.execute("UPDATE devices SET app_attest_key_id=? WHERE device_id=?",
                                 (c, row["device_id"]))
            for row in conn.execute("SELECT value, reason, revoked_at FROM revocations "
                                    "WHERE kind=?", (REVOKE_APP_ATTEST_KEY,)).fetchall():
                c = canon(row["value"])
                if not c or c == row["value"]:
                    continue
                # Two spellings of ONE key collapse to one identity. Keep the revocation:
                # insert the canonical row (ignore an existing one) and drop the stale text.
                conn.execute(
                    "INSERT INTO revocations (kind, value, reason, revoked_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(kind, value) DO NOTHING",
                    (REVOKE_APP_ATTEST_KEY, c, row["reason"], row["revoked_at"]))
                conn.execute("DELETE FROM revocations WHERE kind=? AND value=?",
                             (REVOKE_APP_ATTEST_KEY, row["value"]))
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._run(self._close)
        self._pool.shutdown(wait=True)

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- audit ----
    def _audit(self, event, approval_id=None, device_id=None, reason=None, identity=None):
        self._conn.execute(
            "INSERT INTO audit (ts, approval_id, device_id, event, reason, identity) "
            "VALUES (?,?,?,?,?,?)",
            (time.time(), approval_id, device_id, event, reason, identity))

    async def audit(self, event, *, approval_id=None, device_id=None, reason=None,
                    identity=None):
        """Public audit hook for the control plane. Never pass secrets as `reason`."""
        await self._run(self._audit, event, approval_id, device_id, reason, identity)

    async def audit_events(self):
        return await self._run(lambda: [dict(r) for r in self._conn.execute(
            "SELECT ts, approval_id, device_id, event, reason FROM audit ORDER BY id").fetchall()])

    # ---- enrollment tokens (one-time) ----
    async def add_enrollment_token(self, token_hash, principal, expires_at):
        await self._run(self._add_enroll, token_hash, principal, expires_at)

    def _add_enroll(self, token_hash, principal, expires_at):
        self._conn.execute(
            "INSERT INTO enrollment_tokens (token_hash, principal, created_at, expires_at) "
            "VALUES (?,?,?,?)", (token_hash, principal, time.time(), expires_at))
        self._audit("enroll_token_created", reason=principal)

    async def consume_enrollment_token(self, token_hash):
        return await self._run(self._consume_enroll, token_hash)

    def _consume_enroll(self, token_hash):
        """P1B/§4: exactly one redemption, decided by the DATABASE.

        Reproduced before this: two connections redeeming the same token concurrently both
        returned ("local-owner", "ok"), so one pairing token could bind two devices. The
        `consumed=0` predicate is now part of the UPDATE and rowcount picks the winner; the
        SELECT that follows exists only to explain a loss, never to authorise a win.
        """
        now = time.time()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            upd = self._conn.execute(
                "UPDATE enrollment_tokens SET consumed=1 "
                "WHERE token_hash=? AND consumed=0 AND expires_at >= ?", (token_hash, now))
            if upd.rowcount == 1:
                principal = self._conn.execute(
                    "SELECT principal FROM enrollment_tokens WHERE token_hash=?",
                    (token_hash,)).fetchone()["principal"]
                self._conn.execute("COMMIT")
                return principal, "ok"
            r = self._conn.execute(
                "SELECT * FROM enrollment_tokens WHERE token_hash=?", (token_hash,)).fetchone()
            self._conn.execute("COMMIT")
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        if r is None:
            return None, "unknown"
        if r["consumed"]:
            return None, "already_consumed"
        return None, "expired"

    # ---- devices ----
    # P1A: legacy `add_device` was REMOVED. It was dead code (zero callers) carrying an
    # unguarded `INSERT OR REPLACE ... status='ACTIVE'` that would have silently
    # resurrected a REVOKED device — exactly the F5 class of bug, waiting to be wired up.
    # Enrollment goes through begin_device_enrollment(), which is guarded in SQL.

    async def get_device(self, device_id):
        return await self._run(lambda: (lambda r: dict(r) if r else None)(
            self._conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()))

    # P1A.4/§11: `set_device_status` and `add_revocation` were removed; both were footguns —
    # an unguarded public un-revoke of a terminal lifecycle column, and a non-transactional
    # revocation writer. Same reason `add_device` was deleted. `revoke_identities*` is now
    # the only writer.
    # P1A.6/§15 correction: an earlier version of this comment claimed both "had zero
    # callers". That was wrong about the history — on `main` (3233e99) `set_device_status`
    # WAS the production revoke writer (control.py:159); it had lost its callers only over
    # the course of this branch. `add_revocation` never existed on main at all.

    # ---- P1A: durable revocation identities ------------------------------
    async def revoke_identities_planned(self, *, plan, reason: str | None = None,
                                        device_id: str | None = None,
                                        operation: str = "revoke") -> "RevocationResult":
        """P1A.4/§3: like `revoke_identities`, but the SET OF IDENTITIES is decided INSIDE
        the transaction.

        `plan(all_device_rows) -> (entries, doomed_device_ids)`. A device revoke has to read
        the record to learn which key material it carries; doing that read before the write
        lock would reintroduce exactly the TOCTOU this release is closing — a re-enrollment
        landing in between would leave the new key material unrevoked.
        """
        return await self._run(self._revoke_planned, plan, reason, device_id, operation)

    def _revoke_planned(self, plan, reason, device_id, operation="revoke"):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute("SELECT * FROM devices").fetchall()
            # P1A.6/§11: scope discovery — including HISTORICAL bindings — happens entirely
            # after BEGIN IMMEDIATE, so a rotation racing in cannot narrow it.
            history = self._conn.execute(
                "SELECT * FROM device_identity_history").fetchall()
            entries, doomed = plan(rows, history)
            for kind, value in entries:
                if kind not in REVOKE_KINDS:
                    raise ValueError(f"unknown revocation kind: {kind!r}")
                if not value:
                    raise ValueError("revocation value must not be empty")
            newly, already = self._write_revocations(entries, reason, device_id, doomed)
            self._conn.execute("COMMIT")
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        # P1A.5/H1: the ONLY authoritative description of what happened. It is built from
        # the rows this committed transaction wrote — never from a read taken before the
        # lock, which is what let the CLI announce identities it had not revoked.
        return RevocationResult(operation=operation, committed=True,
                                newly_revoked=tuple(newly), already_revoked=tuple(already),
                                affected_devices=tuple(doomed))

    def _write_revocations(self, entries, reason, device_id, doomed):
        """Returns (newly_revoked, already_revoked) as lists of (kind, value)."""
        newly, already = [], []
        for kind, value in entries:
            cur = self._conn.execute(
                "INSERT INTO revocations (kind, value, reason, revoked_at) "
                "VALUES (?,?,?,?) ON CONFLICT(kind, value) DO NOTHING",
                (kind, value, reason, time.time()))
            if cur.rowcount == 1:
                newly.append((kind, value))
                # P1A.5/M1: identity and operator reason are SEPARATE. The reason no longer
                # displaces what was revoked.
                self._audit(_REVOKE_EVENT[kind], device_id=device_id, reason=reason,
                            identity=f"{kind}:{value}")
            else:
                already.append((kind, value))
                # honest event name — this is NOT a first revocation
                self._audit("revocation_reaffirmed", device_id=device_id, reason=reason,
                            identity=f"{kind}:{value}")
        for did in doomed:
            self._conn.execute("UPDATE devices SET status=? WHERE device_id=?",
                               (DEVICE_REVOKED, did))
            self._audit("device_status", device_id=did, reason=DEVICE_REVOKED,
                        identity=f"{REVOKE_DEVICE}:{did}")
        return newly, already

    async def revoke_identities(self, *, entries, match, reason: str | None = None,
                                device_id: str | None = None,
                                operation: str = "revoke") -> "RevocationResult":
        """P1A.4/§3: revoke SEVERAL identities as one logical security transition.

        `entries` is a list of (kind, value). A device revoke now carries all three bound
        identities, so cutting off a stolen approver also cuts off its key material —
        otherwise the same Secure Enclave key re-pairs under a fresh device_id and regains
        full authority, which is exactly what the CLI used to call "terminal".

        Returns (identities_newly_recorded, devices_moved_to_revoked).
        """
        return await self._run(self._revoke_identities, list(entries), reason, match,
                               device_id, operation)

    def _revoke_identities(self, entries, reason, match, device_id, operation="revoke"):
        for kind, value in entries:
            if kind not in REVOKE_KINDS:
                raise ValueError(f"unknown revocation kind: {kind!r}")
            if not value:
                raise ValueError("revocation value must not be empty")
        def plan(rows, _history):
            return entries, [r["device_id"] for r in rows
                             if r["status"] != DEVICE_REVOKED and match(r)]
        return self._revoke_planned(plan, reason, device_id, operation)

    # P1A.7/§18: `revoke_identity` (single-kind) was removed — 0 callers in src, tests and
    # scripts, and it mislabelled the result's `operation` with the revocation KIND instead
    # of the operation name. `revoke_identities*` is the only revocation writer.

    async def is_revoked(self, kind, value) -> bool:
        return await self._run(self._is_revoked, kind, value)

    def _is_revoked(self, kind, value):
        if not value:
            return False
        return self._conn.execute(
            "SELECT 1 FROM revocations WHERE kind=? AND value=?", (kind, value)
        ).fetchone() is not None

    async def revoked_identities(self, identities) -> str | None:
        """`identities` is an iterable of (kind, value). Returns the first revoked kind,
        or None. One round-trip so every authority path can check all identities cheaply."""
        return await self._run(self._revoked_identities, list(identities))

    def _revoked_identities(self, identities):
        for kind, value in identities:
            if value and self._is_revoked(kind, value):
                return kind
        return None

    async def list_revocations(self):
        return await self._run(lambda: [dict(r) for r in self._conn.execute(
            "SELECT kind, value, reason, revoked_at FROM revocations "
            "ORDER BY revoked_at DESC")])

    async def list_devices(self):
        return await self._run(lambda: [dict(r) for r in self._conn.execute(
            "SELECT device_id, key_id, principal, status, attested, enrolled_at FROM devices").fetchall()])

    async def list_devices_full(self):
        """All columns — needed to derive approval-key identities for key revocation.
        Contains public keys and hashes only; never private material."""
        return await self._run(lambda: [dict(r) for r in self._conn.execute(
            "SELECT * FROM devices").fetchall()])

    # ---- approval requests ----
    async def create_request(self, *, approval_id, principal, tool, mode, task, workspace,
                             action_digest, human_summary, expires_at, core_instance_id=""):
        await self._run(self._create_request, approval_id, principal, tool, mode, task,
                        workspace, action_digest, human_summary, expires_at, core_instance_id)

    def _create_request(self, approval_id, principal, tool, mode, task, workspace,
                        action_digest, human_summary, expires_at, core_instance_id=""):
        self._conn.execute(
            "INSERT INTO approval_requests (approval_id, principal, tool, mode, task, "
            "workspace, action_digest, human_summary, state, created_at, expires_at, request_core_instance_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (approval_id, principal, tool, mode, task, workspace, action_digest,
             human_summary, PENDING, time.time(), expires_at, core_instance_id))
        self._audit("request_created", approval_id=approval_id, reason=principal)

    async def get_request(self, approval_id):
        return await self._run(lambda: (lambda r: dict(r) if r else None)(
            self._conn.execute("SELECT * FROM approval_requests WHERE approval_id=?",
                               (approval_id,)).fetchone()))

    async def list_pending(self, principal=None):
        return await self._run(self._list_pending, principal)

    def _list_pending(self, principal):
        self._expire_due()
        if principal is None:
            rows = self._conn.execute(
                "SELECT * FROM approval_requests WHERE state=?", (PENDING,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM approval_requests WHERE state=? AND principal=?",
                (PENDING, principal)).fetchall()
        return [dict(r) for r in rows]

    def _expire_due(self):
        """P1B/§10: the bulk TTL sweeper — guarded, transactional and audited.

        It is a lifecycle writer outside `_cas_transition`, and it stays one: it expires many
        rows at once and a per-row CAS would be the wrong shape. It is safe to keep because
        it can only move rows in the fail-closed direction (PENDING/APPROVED -> EXPIRED,
        both legal edges) and it carries a state predicate, so it can never resurrect or
        create authority. What it lacked was a transaction boundary and any record at all:
        a request could vanish from the operator's view with nothing in the audit.

        Callers must NOT already hold a transaction.
        """
        now = time.time()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            due = [r["approval_id"] for r in self._conn.execute(
                "SELECT approval_id FROM approval_requests "
                "WHERE state IN (?,?) AND expires_at < ?", (PENDING, APPROVED, now)).fetchall()]
            if due:
                self._conn.execute(
                    "UPDATE approval_requests SET state=? WHERE state IN (?,?) AND expires_at < ?",
                    (EXPIRED, PENDING, APPROVED, now))
                for approval_id in due:
                    self._audit("state_" + EXPIRED, approval_id=approval_id, reason="ttl")
            self._conn.execute("COMMIT")
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    async def transition(self, approval_id, new_state, *, device_id=None, error=None):
        return await self._run(self._transition, approval_id, new_state, device_id, error)

    def _transition(self, approval_id, new_state, device_id, error):
        """P1B/F9: the transition is a compare-and-swap inside ONE write transaction.

        Before this it was SELECT state -> Python check -> unconditional UPDATE, in
        autocommit. Reproduced with two connections released by a barrier: a concurrent
        APPROVE and DENY both read PENDING, both passed the check, and both wrote — final
        state was whichever committed last, BOTH callers were told they had succeeded, and
        the audit carried `state_APPROVED` AND `state_DENIED` for the same request. Two
        successes for an operation that may succeed once.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._cas_transition(approval_id, new_state, device_id, error)
            self._conn.execute("COMMIT")
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return cur

    def _cas_transition(self, approval_id, new_state, device_id, error,
                        *, stamp_decision: bool = False):
        """The state change itself. MUST be called inside an open write transaction.

        FREEZE/F3: this primitive may NOT produce EXECUTING. Entering execution is not a
        state change, it is a claim: it must publish an execution attempt in the same
        transaction, or the row says "executing" while nothing records what was about to
        run — the state P1C exists to abolish. The only writer that may set EXECUTING is
        `_claim_execution`, the single statement inside `_claim_execution_attempt`.
        A caller that reaches here with EXECUTING is a bug, so it raises rather than
        silently doing something reasonable-looking.

        Decision owners are `_transition`, `_commit_decision` and N2's explicitly
        task-limited `_commit_dashboard_decision`. All use this same state CAS.

        Raises IllegalTransition when the graph forbids it; ConcurrentTransition when the row
        moved underneath us. The `AND state=?` predicate is load-bearing, not decoration: it
        is what makes the swap atomic even if the surrounding transaction is ever weakened.
        """
        if new_state == EXECUTING:
            raise ExecutionBypassError(
                "EXECUTING must be claimed through claim_execution_attempt, which journals "
                "the attempt in the same transaction; the generic transition cannot do that")
        r = self._conn.execute("SELECT state FROM approval_requests WHERE approval_id=?",
                               (approval_id,)).fetchone()
        if r is None:
            raise ApprovalStoreError("unknown approval_id")
        cur = r["state"]
        if new_state not in _ALLOWED.get(cur, set()):
            raise IllegalTransition(f"{cur} -> {new_state}")
        # `decided_at` ist der Zeitpunkt der MENSCHLICHEN Entscheidung — und nur der.
        #
        # Gefunden in der Live-Abnahme von DEBT-0126 (2026-08-31): die Spalte wurde
        # bei JEDEM Zustandswechsel neu gestempelt, also auch beim Anspruch und beim
        # Verbrauch. Bei einer verbrauchten Zahlungsfreigabe stand danach der
        # Verbrauchszeitpunkt drin, und wann der Mensch per Face ID zugestimmt hatte,
        # war aus dem Buch nicht mehr lesbar. Gemessen an `ap-8aa1709c…`: Anspruch
        # 110 ms VOR der eigenen „Entscheidung" — eine Reihenfolge, die es nie gab.
        #
        # Bei einer Geldbewegung ist das die eine Angabe, auf die es ankommt. Ein
        # Buch, das den Zeitpunkt der Zustimmung ueberschreibt, kann eine Zahlung
        # nicht mehr belegen.
        #
        # Nur die beiden geprueften Entscheidungswege stempeln (`stamp_decision=True`); jeder spaetere
        # Uebergang laesst die Spalte stehen. Die spaeteren Zeitpunkte gehen dabei
        # NICHT verloren — sie stehen laengst woanders und genauer: der Anspruch in
        # `execution_attempts.claimed_at`, die durable Grenze in `boundary_at`, der
        # Ausgang in `finished_at`, und jeder Uebergang mit eigenem `ts` im `audit`.
        # Es entsteht also keine neue Spalte fuer etwas, das schon aufgeschrieben ist.
        upd = self._conn.execute(
            "UPDATE approval_requests SET state=?, "
            "decided_at=CASE WHEN ? THEN ? ELSE decided_at END, "
            "decided_device=COALESCE(?, decided_device), error=COALESCE(?, error) "
            "WHERE approval_id=? AND state=?",
            (new_state, 1 if stamp_decision else 0, time.time(),
             device_id, error, approval_id, cur))
        if upd.rowcount != 1:
            raise ConcurrentTransition(f"{approval_id}: {cur} -> {new_state} lost the race")
        self._audit("state_" + new_state, approval_id=approval_id, device_id=device_id,
                    reason=error)
        return cur

    async def commit_decision(self, *, approval_id, device_id, new_state, challenge_nonce,
                              challenge_payload_sha256, verified_public_key_x963,
                              verified_app_attest_public_key=None, app_attest_counter=None,
                              allowed_environments=None) -> str:
        """P1B/F9: the whole human decision as ONE atomic authority operation.

        Before this, `submit_decision` did its authority checks as separate autocommit reads
        and then wrote in three more: consume the nonce, bump the App Attest counter,
        transition the request. Reproduced: a `revoke-device` committed from a SECOND
        connection after the revocation check and before the transition still produced an
        APPROVED request bound to a REVOKED device, with `state_APPROVED` in the audit. The
        S1 execution claim still refused to run it (P1A.1/F3), so this was not an execution
        bypass — but a trusted decision was recorded for authority that no longer existed.

        Everything authority-bearing is re-read HERE, under the write lock, from the request
        row and the device row. The caller passes only what it actually VERIFIED — the public
        key the signature checked out against, the App Attest key the assertion checked out
        against, the counter the assertion proved — plus this core's own environment policy.
        It passes no expected identity, no principal and no action digest: those come from the
        stored request, so a caller cannot nominate what it will be compared to.
        """
        return await self._run(
            self._commit_decision, approval_id, device_id, new_state, challenge_nonce,
            challenge_payload_sha256, bytes(verified_public_key_x963),
            None if verified_app_attest_public_key is None
            else bytes(verified_app_attest_public_key),
            app_attest_counter,
            frozenset(allowed_environments) if allowed_environments else frozenset())

    def _browser_session(self, session_id, principal, core_instance_id):
        row = self._conn.execute("SELECT * FROM browser_sessions WHERE session_id=?",
                                 (session_id,)).fetchone()
        if row is None:
            return None, "unknown_browser_session"
        if row["principal"] != principal:
            return None, "principal_mismatch"
        if not core_instance_id or row["core_instance_id"] != core_instance_id:
            return None, "wrong_core_instance"
        if row["revoked_at"] is not None:
            return None, "browser_session_revoked"
        if row["expires_at"] <= time.time():
            return None, "browser_session_expired"
        return row, None

    @staticmethod
    def _browser_binding(session):
        # Public identity/generation only. No cookie, CSRF value or credential hash.
        body = [session[k] for k in ("session_id", "principal", "core_instance_id",
                                    "created_at", "expires_at")]
        return hashlib.sha256(json.dumps(body, separators=(",", ":")).encode()).hexdigest()

    def _dashboard_request_reason(self, req, core_instance_id):
        if req is None:
            return "unknown_request"
        if req["tool"] not in DASHBOARD_TASK_TOOLS:
            return "dashboard_tool_forbidden"
        if not core_instance_id or req["request_core_instance_id"] != core_instance_id:
            return "wrong_core_instance"
        if req["expires_at"] <= time.time():
            return "request_expired"
        actual = _action_digest(tool_id=req["tool"], mode=req["mode"],
                                task=req["task"], workspace=req["workspace"])
        if actual != req["action_digest"]:
            return "action_digest_mismatch"
        return None

    async def commit_dashboard_decision(self, *, approval_id, session_id, principal,
                                         core_instance_id, action_digest, decision):
        """Authenticated dashboard choice competes with Face ID on the SAME row.

        Caller principal/session came from BrowserSessionService, never the model.
        Actual session, request, digest and TTL are rechecked under the write lock.
        This deliberately cannot authorize any capability outside the two task starts.
        """
        return await self._run(self._commit_dashboard_decision, approval_id, session_id,
                               principal, core_instance_id, action_digest, decision)

    def _commit_dashboard_decision(self, approval_id, session_id, principal,
                                    core_instance_id, action_digest, decision):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            req = self._conn.execute("SELECT * FROM approval_requests WHERE approval_id=?",
                                     (approval_id,)).fetchone()
            reason = self._dashboard_request_reason(req, core_instance_id)
            if reason:
                return self._decision_deny(approval_id, None, reason)
            if req["state"] != PENDING:
                return self._decision_deny(approval_id, None, "not_pending")
            if decision not in ("APPROVE", "DENY"):
                return self._decision_deny(approval_id, None, "invalid_decision")
            if req["principal"] != principal:
                return self._decision_deny(approval_id, None, "principal_mismatch")
            if action_digest != req["action_digest"]:
                return self._decision_deny(approval_id, None, "action_digest_mismatch")
            session, reason = self._browser_session(session_id, principal, core_instance_id)
            if reason:
                return self._decision_deny(approval_id, None, reason)
            self._cas_transition(approval_id, APPROVED if decision == "APPROVE" else DENIED,
                                 None, None, stamp_decision=True)
            self._conn.execute("UPDATE approval_requests SET decision_method=?, "
                "decided_session=?, decided_device=NULL WHERE approval_id=?",
                (DASHBOARD_METHOD, session["session_id"], approval_id))
            self._audit("dashboard_decision", approval_id=approval_id,
                        reason=decision, identity=session["session_id"])
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return "ok"

    async def dashboard_execution_authorization(self, approval_id, core_instance_id):
        return await self._run(self._dashboard_authorization, approval_id, core_instance_id)

    def _dashboard_authorization(self, approval_id, core_instance_id):
        req = self._conn.execute("SELECT * FROM approval_requests WHERE approval_id=?",
                                 (approval_id,)).fetchone()
        reason = self._dashboard_request_reason(req, core_instance_id)
        if reason:
            return None, reason
        if req["state"] not in (APPROVED, EXECUTING):
            return None, "request_not_executable"
        if req["decision_method"] != DASHBOARD_METHOD or req["decided_device"]:
            return None, "dashboard_decision_required"
        session, reason = self._browser_session(req["decided_session"], req["principal"], core_instance_id)
        if reason:
            return None, reason
        return DashboardAuthorization(approval_id, session["session_id"], self._browser_binding(session),
                                      req["principal"], core_instance_id,
                                      req["action_digest"], req["tool"]), None

    def _dashboard_attempt_reason(self, att, core_instance_id, expected=None):
        auth, reason = self._dashboard_authorization(att["approval_id"], core_instance_id)
        if reason:
            return reason
        if not att["claim_identity_bound"] or att["claim_authorization_method"] != DASHBOARD_METHOD:
            return "claim_identity_unbound"
        if (att["device_id"] or att["claim_browser_session_id"] != auth.session_id
                or att["claim_browser_session_binding"] != auth.session_binding
                or att["claim_core_instance_id"] != auth.core_instance_id
                or att["claim_principal"] != auth.principal
                or att["claim_action_digest"] != auth.action_digest
                or att["capability"] != auth.tool
                or att["execution_id"] != _X.execution_id_for(core_instance_id, auth.approval_id)
                or (expected is not None and auth != expected)):
            return "claim_authority_changed:dashboard"
        return None

    async def prepare_browser_memory_command(self, *, approval_id, session_id, principal,
            core_instance_id, tool, mode, task, action_digest, expires_at):
        return await self._run(self._prepare_browser_memory_command, approval_id, session_id,
            principal, core_instance_id, tool, mode, task, action_digest, expires_at)

    def _prepare_browser_memory_command(self, approval_id, session_id, principal,
            core_instance_id, tool, mode, task, action_digest, expires_at):
        """One transaction, no intermediate pending request and no renewal on replay."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            session, reason = self._browser_session(session_id, principal, core_instance_id)
            if reason:
                return self._decision_deny(approval_id, None, reason)
            if (tool not in MEMORY_COMMAND_TOOLS or mode != "controlled-v1"
                    or expires_at <= time.time()
                    or action_digest != _action_digest(tool_id=tool, mode=mode, task=task, workspace="")):
                return self._decision_deny(approval_id, None, "memory_command_binding_invalid")
            old = self._conn.execute("SELECT * FROM approval_requests WHERE approval_id=?",
                                     (approval_id,)).fetchone()
            if old is not None:
                expected = {"principal": principal, "request_core_instance_id": core_instance_id,
                    "tool": tool, "mode": mode, "task": task, "workspace": "",
                    "action_digest": action_digest, "decision_method": MEMORY_COMMAND_METHOD}
                if any(old[key] != value for key, value in expected.items()):
                    return self._decision_deny(approval_id, None, "memory_command_conflict")
                self._conn.execute("COMMIT")
                return "existing"
            self._create_request(approval_id, principal, tool, mode, task, "", action_digest,
                                 "Bewusster Gedächtnisbefehl im Dashboard", expires_at, core_instance_id)
            self._cas_transition(approval_id, APPROVED, None, None, stamp_decision=True)
            self._conn.execute("UPDATE approval_requests SET decision_method=?, "
                "decided_session=?, decided_device=NULL WHERE approval_id=?",
                (MEMORY_COMMAND_METHOD, session["session_id"], approval_id))
            self._audit("browser_memory_command", approval_id=approval_id,
                        identity=session["session_id"])
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return "ok"

    async def browser_command_authorization(self, approval_id, core_instance_id):
        return await self._run(self._browser_command_authorization, approval_id, core_instance_id)

    async def recent_browser_memory_commands(self, principal, core_instance_id, limit=25):
        return await self._run(lambda: [r[0] for r in self._conn.execute(
            "SELECT approval_id FROM approval_requests WHERE principal=? AND request_core_instance_id=? "
            "AND decision_method=? ORDER BY created_at DESC, approval_id LIMIT ?",
            (principal, core_instance_id, MEMORY_COMMAND_METHOD, min(100, max(1, int(limit))))).fetchall()])

    async def browser_memory_status_snapshot(self, approval_id, principal, core_instance_id):
        """One read transaction, including across independent WAL connections."""
        return await self._run(self._browser_memory_status_snapshot, approval_id, principal, core_instance_id)

    def _browser_memory_status_snapshot(self, approval_id, principal, core_instance_id):
        self._conn.execute("BEGIN")
        try:
            req = self._conn.execute("SELECT * FROM approval_requests WHERE approval_id=?",
                                     (approval_id,)).fetchone()
            result = None
            if (req is not None and req["principal"] == principal
                    and req["request_core_instance_id"] == core_instance_id
                    and req["decision_method"] == MEMORY_COMMAND_METHOD
                    and req["tool"] in MEMORY_COMMAND_TOOLS):
                execution_id = _X.execution_id_for(core_instance_id, approval_id)
                attempts = [dict(r) for r in self._conn.execute(
                    "SELECT * FROM execution_attempts WHERE approval_id=? AND execution_id=? ORDER BY claimed_at",
                    (approval_id, execution_id)).fetchall()]
                auth, reason = (None, "")
                if req["state"] == APPROVED and not attempts:
                    auth, reason = self._browser_command_authorization(approval_id, core_instance_id)
                result = {"request": dict(req), "attempts": attempts,
                          "authorized": auth is not None, "reason": reason or "",
                          "observed_at": time.time(),
                          "succeeded": any(self._memory_command_receipt_matches(req, a, core_instance_id)
                                           for a in attempts)}
            self._conn.execute("COMMIT")
            return result
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _memory_command_receipt_matches(req, att, core_instance_id):
        # Historical execution proof: today's logout/expiry cannot undo it.
        execution_id = _X.execution_id_for(core_instance_id, req["approval_id"])
        digest = _action_digest(tool_id=req["tool"], mode=req["mode"],
                                task=req["task"], workspace=req["workspace"])
        return (req["state"] == CONSUMED and req["tool"] in MEMORY_COMMAND_TOOLS
            and req["mode"] == "controlled-v1" and req["workspace"] == ""
            and req["decision_method"] == MEMORY_COMMAND_METHOD and req["decided_device"] is None
            and att["status"] == _X.SUCCEEDED and bool(att["claim_identity_bound"])
            and bool(att["boundary_at"]) and att["approval_id"] == req["approval_id"]
            and att["execution_id"] == execution_id and att["device_id"] is None
            and att["claim_authorization_method"] == MEMORY_COMMAND_METHOD
            and bool(att["claim_browser_session_binding"]) and bool(req["decided_session"])
            and att["claim_browser_session_id"] == req["decided_session"]
            and att["claim_principal"] == req["principal"]
            and att["claim_core_instance_id"] == req["request_core_instance_id"] == core_instance_id
            and att["capability"] == req["tool"] and att["semantics"] == _X.NON_IDEMPOTENT_WRITE
            and att["idempotency_key"] == _X.idempotency_key_for(execution_id, req["tool"])
            and att["claim_action_digest"] == req["action_digest"] == digest)

    def _browser_command_authorization(self, approval_id, core_instance_id):
        req = self._conn.execute("SELECT * FROM approval_requests WHERE approval_id=?",
                                 (approval_id,)).fetchone()
        if req is None:
            return None, "unknown_request"
        if (req["tool"] not in MEMORY_COMMAND_TOOLS or req["mode"] != "controlled-v1"
                or req["workspace"] != "" or req["decision_method"] != MEMORY_COMMAND_METHOD
                or req["decided_device"] is not None):
            return None, "memory_command_binding_invalid"
        if not core_instance_id or req["request_core_instance_id"] != core_instance_id:
            return None, "wrong_core_instance"
        if req["expires_at"] <= time.time():
            return None, "request_expired"
        if req["state"] not in (APPROVED, EXECUTING):
            return None, "request_not_executable"
        if req["action_digest"] != _action_digest(tool_id=req["tool"], mode=req["mode"],
                                                  task=req["task"], workspace=req["workspace"]):
            return None, "action_digest_mismatch"
        session, reason = self._browser_session(req["decided_session"], req["principal"], core_instance_id)
        if reason:
            return None, reason
        return BrowserCommandAuthorization(approval_id, session["session_id"], self._browser_binding(session),
                                            req["principal"], core_instance_id,
                                            req["action_digest"], req["tool"]), None

    def _browser_command_attempt_reason(self, att, core_instance_id, expected):
        if type(expected) is not BrowserCommandAuthorization:
            return "memory_command_authorization_required"
        auth, reason = self._browser_command_authorization(att["approval_id"], core_instance_id)
        if reason:
            return reason
        if (auth != expected or not att["claim_identity_bound"]
                or att["claim_authorization_method"] != MEMORY_COMMAND_METHOD
                or att["device_id"] is not None or att["claim_browser_session_id"] != auth.session_id
                or att["claim_browser_session_binding"] != auth.session_binding
                or att["claim_core_instance_id"] != auth.core_instance_id
                or att["claim_principal"] != auth.principal or att["claim_action_digest"] != auth.action_digest
                or att["capability"] != auth.tool or att["semantics"] != _X.NON_IDEMPOTENT_WRITE
                or att["execution_id"] != _X.execution_id_for(core_instance_id, auth.approval_id)
                or att["idempotency_key"] != _X.idempotency_key_for(att["execution_id"], auth.tool)):
            return "claim_authority_changed:memory_command"
        return None

    def _execution_authority(self, req, dev, allowed_envs):
        """HYGIENE/H1: the ONE authority predicate, shared by the decision commit and by a
        recovery retry. MUST be called inside an open write transaction.

        Returns `(reason, revoked_kind, dev_fp, aakid)`; `reason is None` means the authority
        holds. It exists because the recovery retry used to perform a NEW external write with
        no re-check at all — the checks had happened before the crash, possibly hours earlier.
        Duplicating the predicate would have let the two copies drift, which is the failure
        mode a reviewer should assume; there is one copy.

        Order matters and is preserved from P1B: revocation is evaluated BEFORE the generic
        lifecycle state, because every revoke kind also flips `devices.status` and folding
        them together would report a revoked device as merely inactive.
        """
        try:
            dev_fp = _crypto.fingerprint(bytes.fromhex(dev["public_key_x963"] or ""))
        except ValueError:
            return "device_key_unreadable", None, None, None
        aakid = dev["app_attest_key_id"] or ""
        pred = "(kind=? AND value=?) OR (kind=? AND value=?) OR (kind=? AND value=?)"
        revoked = self._conn.execute(
            f"SELECT kind FROM revocations WHERE {pred} LIMIT 1",
            (REVOKE_DEVICE, dev["device_id"], REVOKE_APPROVAL_KEY, dev_fp,
             REVOKE_APP_ATTEST_KEY, aakid)).fetchone()
        if revoked or dev["status"] == DEVICE_REVOKED:
            return ("device_revoked",
                    revoked["kind"] if revoked else dev["status"], dev_fp, aakid)
        if dev["status"] != DEVICE_ACTIVE:
            return "device_not_active", None, dev_fp, aakid
        if dev["attestation_status"] != ATT_ATTESTED:
            return "device_not_attested", None, dev_fp, aakid
        if not dev["current_enrollment_id"]:
            # P1A.8 generation: a device mid-(re-)enrolment owns no execution authority.
            return "no_current_enrollment", None, dev_fp, aakid
        if dev["principal"] != req["principal"]:
            return "principal_mismatch", None, dev_fp, aakid
        # Fail closed both ways: no policy at all means deny-all, exactly as at decision time.
        if not allowed_envs or (dev["environment"] or "") not in allowed_envs:
            return "device_environment_not_allowed", None, dev_fp, aakid
        return None, None, dev_fp, aakid

    def _claim_authority_snapshot(self, approval_id, device_id, core_instance_id):
        """FREEZE/F2: the identity material this claim is bound to. Write-transaction only.

        Returns None when the row cannot produce a complete, meaningful binding; the caller
        then stores `claim_identity_bound = 0` and the attempt can never be auto-retried.
        Half a snapshot is worse than none: it would look bound while silently omitting the
        field that actually changed.
        """
        req = self._conn.execute(
            "SELECT principal FROM approval_requests WHERE approval_id=?",
            (approval_id,)).fetchone()
        dev = self._conn.execute("SELECT * FROM devices WHERE device_id=?",
                                 (device_id,)).fetchone()
        if req is None or dev is None:
            return None
        try:
            appr_fp = _crypto.fingerprint(bytes.fromhex(dev["public_key_x963"] or ""))
        except ValueError:
            return None
        aakid = dev["app_attest_key_id"] or ""
        enrollment = dev["current_enrollment_id"] or ""
        environment = dev["environment"] or ""
        # Every field must be present. A device mid-enrolment or without attested material
        # has nothing stable to bind to, so it is left unbound rather than half-bound.
        if not (appr_fp and aakid and enrollment and environment and req["principal"]
                and core_instance_id):
            return None
        return {"core_instance_id": core_instance_id,
                "principal": req["principal"], "environment": environment,
                "enrollment_id": enrollment, "approval_key_sha256": appr_fp,
                "app_attest_key_id": aakid}

    async def recheck_execution_authority(self, *, attempt_id, allowed_environments,
                                          core_instance_id) -> str:
        """HYGIENE/H1: re-verify authority IMMEDIATELY before a recovery retry writes again.

        A retry is a NEW external write. The fact that the authority held before the crash
        says nothing about now — the device may have been revoked, re-enrolled or expired in
        between. Everything is re-read here, under the write lock, from the stored rows.

        This is NOT called before a RECONCILE: observing whether an effect already exists is
        not a write and must stay possible even for revoked authority, because the operator
        needs to know what happened.

        Nor is there a retry after an unsuccessful reconcile: `recover_execution` closes that
        attempt instead of quietly repeating it, so no such call site exists. The one caller
        is the RETRY_SAME_KEY path — do not add another without keeping this gate in front.
        """
        return await self._run(
            self._recheck_execution_authority, attempt_id,
            frozenset(allowed_environments) if allowed_environments else frozenset(),
            core_instance_id)

    def _recheck_execution_authority(self, attempt_id, allowed_envs, core_instance_id):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            att = self._conn.execute("SELECT * FROM execution_attempts WHERE attempt_id=?",
                                     (attempt_id,)).fetchone()
            if att is None:
                return self._exec_deny(None, None, "unknown_attempt")
            if att["status"] not in _X.OPEN_STATUSES:
                return self._exec_deny(att["approval_id"], att["device_id"],
                                       "attempt_not_open:" + att["status"])
            # The attempt must belong to THIS core: the execution identity is a function of
            # the core instance, so recomputing it is the check.
            if att["execution_id"] != _X.execution_id_for(core_instance_id or "",
                                                          att["approval_id"]):
                return self._exec_deny(att["approval_id"], att["device_id"],
                                       "wrong_core_instance")
            req = self._conn.execute("SELECT * FROM approval_requests WHERE approval_id=?",
                                     (att["approval_id"],)).fetchone()
            if req is None:
                return self._exec_deny(att["approval_id"], att["device_id"],
                                       "unknown_request")
            if req["state"] not in (EXECUTING, APPROVED):
                return self._exec_deny(att["approval_id"], att["device_id"],
                                       "request_not_executable:" + req["state"])
            if time.time() > req["expires_at"]:
                return self._exec_deny(att["approval_id"], att["device_id"],
                                       "request_expired")
            if att["claim_authorization_method"] == MEMORY_COMMAND_METHOD:
                return self._exec_deny(att["approval_id"], None, "memory_command_not_retryable")
            if att["claim_authorization_method"] == DASHBOARD_METHOD:
                reason = self._dashboard_attempt_reason(att, core_instance_id)
                if reason:
                    return self._exec_deny(att["approval_id"], None, reason)
                self._conn.execute("COMMIT")
                return "ok"
            if req["decided_device"] != att["device_id"]:
                return self._exec_deny(att["approval_id"], att["device_id"],
                                       "device_binding_mismatch")
            dev = self._conn.execute("SELECT * FROM devices WHERE device_id=?",
                                     (att["device_id"],)).fetchone()
            if dev is None:
                return self._exec_deny(att["approval_id"], att["device_id"], "unknown_device")
            reason, revoked_kind, dev_fp, aakid = self._execution_authority(
                req, dev, allowed_envs)
            if reason is not None:
                if reason == "device_revoked":
                    self._audit("execution_rejected_revoked_key",
                                approval_id=att["approval_id"], device_id=att["device_id"],
                                reason=revoked_kind or dev["status"])
                return self._exec_deny(att["approval_id"], att["device_id"], reason)
            # FREEZE/F2A: healthy TODAY is not the question. The question is whether today's
            # authority is still the SAME authority this execution was claimed under. A
            # completed re-enrolment or approval-key rotation keeps the device_id, passes
            # every check above, and would otherwise let a months-old approval be retried
            # under identity material the human never approved anything with.
            bound = self._claim_binding_mismatch(att, req, dev, dev_fp, aakid,
                                                 core_instance_id)
            if bound is not None:
                return self._exec_deny(att["approval_id"], att["device_id"], bound)
            self._conn.execute("COMMIT")
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return "ok"

    def _claim_binding_mismatch(self, att, req, dev, dev_fp, aakid, core_instance_id):
        """FREEZE/F2A: does today's authority still equal the CLAIM-time authority?

        Returns None when it does, otherwise the specific reason. Write-transaction only.

        An attempt with no snapshot (`claim_identity_bound = 0`) is refused outright: it was
        claimed before the snapshot existed, so there is nothing to compare against and
        "probably fine" is not a security answer. Such an attempt stays recoverable by
        OBSERVATION — reconcile never calls this — it just may not drive a new write.
        """
        keys = att.keys()
        if "claim_identity_bound" not in keys or not att["claim_identity_bound"]:
            return "claim_identity_unbound"
        for label, claimed, current in (
                ("core_instance", att["claim_core_instance_id"], core_instance_id or ""),
                ("principal", att["claim_principal"], req["principal"]),
                ("environment", att["claim_environment"], dev["environment"] or ""),
                ("enrollment", att["claim_enrollment_id"], dev["current_enrollment_id"] or ""),
                ("approval_key", att["claim_approval_key_sha256"], dev_fp or ""),
                ("app_attest_key", att["claim_app_attest_key_id"], aakid or "")):
            if (claimed or "") != (current or ""):
                self._audit("execution_rejected_claim_rebound",
                            approval_id=att["approval_id"], device_id=att["device_id"],
                            reason=label)
                return "claim_authority_changed:" + label
        return None

    def _commit_decision(self, approval_id, device_id, new_state, nonce, payload_sha256,
                         verified_pk, verified_aapk, counter, allowed_envs):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            req = self._conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id=?", (approval_id,)).fetchone()
            if req is None:
                return self._decision_deny(approval_id, device_id, "unknown_request")
            if req["state"] != PENDING:
                return self._decision_deny(approval_id, device_id, "not_pending")
            if time.time() > req["expires_at"]:
                # The request outlived its TTL while the decision was in flight.
                return self._decision_deny(approval_id, device_id, "request_expired")

            dev = self._conn.execute("SELECT * FROM devices WHERE device_id=?",
                                     (device_id,)).fetchone()
            if dev is None:
                return self._decision_deny(approval_id, device_id, "unknown_device")

            # HYGIENE/H1: the SAME predicate a recovery retry uses. Identities are computed
            # from the row inside it, never accepted as strings.
            reason, revoked_kind, dev_fp, _aa = self._execution_authority(req, dev,
                                                                         allowed_envs)
            if reason == "device_revoked":
                self._audit("decision_rejected_revoked_key", approval_id=approval_id,
                            device_id=device_id, reason=revoked_kind or dev["status"])
                return self._decision_deny(approval_id, device_id, "device_revoked")
            if reason == "device_environment_not_allowed":
                self._audit("decision_rejected_environment", approval_id=approval_id,
                            device_id=device_id, reason=dev["environment"])
                return self._decision_deny(approval_id, device_id, reason)
            if reason is not None:
                return self._decision_deny(approval_id, device_id, reason)

            # Decision-specific and deliberately NOT in the shared predicate: the signature
            # was checked against a key read BEFORE this transaction. If the device was
            # re-enrolled in between, that key is no longer the device's.
            if _crypto.fingerprint(verified_pk) != dev_fp:
                return self._decision_deny(approval_id, device_id, "device_rebound")
            if new_state == APPROVED:
                if verified_aapk is None:
                    return self._decision_deny(approval_id, device_id, "assertion_required")
                if verified_aapk.hex() != (dev["app_attest_public_key"] or ""):
                    return self._decision_deny(approval_id, device_id, "device_rebound")

            # The nonce, burned with the bindings taken from the STORED request.
            nst = self._cas_consume_challenge(nonce, approval_id, device_id, req["principal"],
                                              req["action_digest"], payload_sha256)
            if nst != "ok":
                return self._decision_deny(approval_id, device_id, "nonce_" + nst)

            if new_state == APPROVED and counter is not None:
                # Monotonic: an assertion counter never moves backwards.
                self._conn.execute(
                    "UPDATE devices SET app_attest_counter=? "
                    "WHERE device_id=? AND app_attest_counter < ?",
                    (counter, device_id, counter))

            # Der mobile Entscheidungsweg stempelt erst nach beiden Beweisen.
            # N2 hat daneben einen ausdruecklichen, separat geprueften Browserweg.
            self._cas_transition(approval_id, new_state, device_id, None,
                                 stamp_decision=True)
            self._conn.execute("UPDATE approval_requests SET decision_method=? WHERE approval_id=?",
                               ("face_id" if new_state == APPROVED else "mobile_signature", approval_id))
            self._conn.execute("COMMIT")
        except (IllegalTransition, ConcurrentTransition) as exc:
            # A lost single-use race is a CONFLICT, never an exception escaping to the caller
            # and never a second success. Nothing of this attempt is committed.
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            self._audit("decision_conflict", approval_id=approval_id, device_id=device_id,
                        reason=str(exc)[:120])
            return "already_decided"
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return "ok"

    def _decision_deny(self, approval_id, device_id, reason: str) -> str:
        """Refusal path: the audit row is the only write, and it is never a success event."""
        self._audit("decision_rejected", approval_id=approval_id, device_id=device_id,
                    reason=reason)
        self._conn.execute("COMMIT")
        return reason

    # HYGIENE/H3: the PUBLIC `claim_execution` is GONE. It moved APPROVED -> EXECUTING
    # without writing an execution attempt, which is precisely the state P1C exists to
    # abolish: a row that says "executing" while nothing records what was about to run or
    # whether it started. It had zero callers. `_claim_execution` survives as the private
    # statement INSIDE `_claim_execution_attempt`, which is the only journalled entry point.
    def _claim_execution(self, approval_id, device_id, identities) -> bool:
        if not identities:
            # P1A.4/H3: an empty identity list would compile to `NOT EXISTS (... WHERE 0)`,
            # i.e. no revocation check at all. Refuse rather than silently pass.
            raise ValueError("claim_execution requires at least one identity")
        now = time.time()
        params = [EXECUTING, approval_id, APPROVED, now]
        if type(identities) in (DashboardAuthorization, BrowserCommandAuthorization):
            command = type(identities) is BrowserCommandAuthorization
            verify = self._browser_command_authorization if command else self._dashboard_authorization
            auth, reason = verify(approval_id, identities.core_instance_id)
            if reason or auth != identities or device_id:
                return False
            predicate = ("decided_device IS NULL AND decision_method=? AND decided_session=? "
                         "AND request_core_instance_id=?")
            params += [MEMORY_COMMAND_METHOD if command else DASHBOARD_METHOD,
                       auth.session_id, auth.core_instance_id]
        else:
            # P1A.5/M4: the mobile branch retains every original device predicate.
            pred = " OR ".join(["(r.kind=? AND r.value=?)"] * len(identities))
            predicate = ("decided_device=? AND EXISTS (SELECT 1 FROM devices d "
                         "WHERE d.device_id=approval_requests.decided_device "
                         "AND d.status=? AND d.attestation_status=?) "
                         f"AND NOT EXISTS (SELECT 1 FROM revocations r WHERE {pred})")
            params += [device_id, DEVICE_ACTIVE, ATT_ATTESTED]
            for kind, value in identities:
                params += [kind, value]
        # `decided_at` bleibt hier UNBERUEHRT. Der Anspruch ist keine Entscheidung;
        # sein Zeitpunkt steht in `execution_attempts.claimed_at`. Siehe die
        # Begruendung an `_cas_transition`.
        cur = self._conn.execute(
            "UPDATE approval_requests SET state=? "
            "WHERE approval_id=? AND state=? "
            # P1A.4/H1: the human's authorisation has a lifetime. It used to be enforced
            # only if the phone happened to poll `GET /approvals`; an APPROVED row past its
            # expiry executed indefinitely. The TTL is now part of the claim itself.
            "  AND expires_at > ? AND " + predicate,
            params)
        won = cur.rowcount == 1
        self._audit("state_" + EXECUTING if won else "execution_claim_rejected",
                    approval_id=approval_id, device_id=device_id,
                    reason=None if won else "expired_revoked_or_inactive")
        return won

    # ---- P1C/F4: the durable execution journal --------------------------------------
    async def claim_execution_attempt(self, *, approval_id, device_id, identities,
                                      execution_id, capability, semantics, idempotency_key,
                                      owner, lease_seconds=300.0, core_instance_id=None):
        """P1C/F4: the P1A claim AND the journal entry, in ONE transaction.

        The P1A/F3 claim was already a single atomic statement and keeps every predicate it
        had — TTL, decided_device, device ACTIVE + ATTESTED, no revoked identity. What it did
        not do was leave any trace of WHAT was about to be executed, so a crash a millisecond
        later was indistinguishable from a crash after the side effect. The attempt row is
        written in the same transaction as the state change, so the two can never disagree.

        Returns (attempt_id, "ok") or (None, reason).
        """
        return await self._run(self._claim_execution_attempt, approval_id, device_id,
                               identities if type(identities) in (DashboardAuthorization, BrowserCommandAuthorization) else list(identities),
                               execution_id, capability, semantics,
                               idempotency_key, owner, lease_seconds, core_instance_id)

    def _claim_execution_attempt(self, approval_id, device_id, identities, execution_id,
                                 capability, semantics, idempotency_key, owner, lease_seconds,
                                 core_instance_id=None):
        if not identities:
            raise ValueError("claim_execution requires at least one identity")
        if semantics not in _X.ALL_SEMANTICS:
            raise ValueError(f"unknown execution semantics: {semantics!r}")
        now = time.time()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # A logical action that already succeeded is terminal, full stop. This is checked
            # before anything else so a replayed execute can never re-enter the pipeline.
            done = self._conn.execute(
                "SELECT attempt_id FROM execution_attempts "
                "WHERE execution_id=? AND status=?", (execution_id, _X.SUCCEEDED)).fetchone()
            if done:
                self._audit("execution_already_succeeded", approval_id=approval_id,
                            device_id=device_id, reason=execution_id)
                self._conn.execute("COMMIT")
                return None, "already_succeeded"
            open_row = self._conn.execute(
                "SELECT attempt_id, status FROM execution_attempts "
                "WHERE execution_id=? AND status IN (?,?,?)",
                (execution_id, _X.CLAIMED, _X.EXTERNAL_PENDING, _X.UNKNOWN)).fetchone()
            if open_row:
                self._audit("execution_attempt_in_flight", approval_id=approval_id,
                            device_id=device_id, reason=open_row["status"])
                self._conn.execute("COMMIT")
                return None, "attempt_in_flight:" + open_row["status"]

            command = type(identities) is BrowserCommandAuthorization
            dashboard = type(identities) in (DashboardAuthorization, BrowserCommandAuthorization)
            if dashboard:
                verify = self._browser_command_authorization if command else self._dashboard_authorization
                auth, reason = verify(approval_id, core_instance_id)
                if (reason or auth != identities or device_id
                        or execution_id != _X.execution_id_for(core_instance_id, approval_id)
                        or capability != auth.tool or semantics != _X.semantics_for(capability)
                        or (command and semantics != _X.NON_IDEMPOTENT_WRITE)
                        or idempotency_key != _X.idempotency_key_for(execution_id, capability)):
                    self._audit("execution_rejected", approval_id=approval_id,
                                reason=reason or "dashboard_authorization_mismatch")
                    self._conn.execute("COMMIT")
                    return None, reason or "dashboard_authorization_mismatch"
            won = self._claim_execution(approval_id, device_id, identities)
            if not won:
                self._conn.execute("COMMIT")
                return None, "claim_rejected"

            attempt_id = "att-" + hashlib.sha256(
                f"{execution_id}|{now!r}|{owner}".encode("utf-8")).hexdigest()[:24]
            # FREEZE/F2: record the authority this claim actually won under. Read from the
            # committed device row inside THIS transaction — never from a caller argument —
            # so it is the same state the claim predicate just tested. `execution_id` already
            # pins the core instance, but it is stored explicitly so the recheck can name a
            # mismatch instead of inferring it.
            snap = ({"core_instance_id": auth.core_instance_id, "principal": auth.principal}
                    if dashboard else self._claim_authority_snapshot(approval_id, device_id,
                                                                      core_instance_id))
            self._conn.execute(
                "INSERT INTO execution_attempts (attempt_id, execution_id, approval_id, "
                "device_id, capability, semantics, idempotency_key, status, lease_owner, "
                "lease_expires_at, claimed_at, claim_identity_bound, claim_core_instance_id, "
                "claim_principal, claim_environment, claim_enrollment_id, "
                "claim_approval_key_sha256, claim_app_attest_key_id, claim_authorization_method, "
                "claim_browser_session_id, claim_browser_session_binding, claim_action_digest) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, execution_id, approval_id, device_id, capability, semantics,
                 idempotency_key, _X.CLAIMED, owner, now + lease_seconds, now,
                 1 if snap else 0,
                 (snap or {}).get("core_instance_id"), (snap or {}).get("principal"),
                 (snap or {}).get("environment"), (snap or {}).get("enrollment_id"),
                 (snap or {}).get("approval_key_sha256"),
                 (snap or {}).get("app_attest_key_id"),
                 MEMORY_COMMAND_METHOD if command else DASHBOARD_METHOD if dashboard else "face_id",
                 auth.session_id if dashboard else None,
                 auth.session_binding if dashboard else None,
                 auth.action_digest if dashboard else self._conn.execute(
                     "SELECT action_digest FROM approval_requests WHERE approval_id=?",
                     (approval_id,)).fetchone()["action_digest"]))
            self._audit("execution_attempt_claimed", approval_id=approval_id,
                        device_id=device_id, reason=attempt_id)
            self._conn.execute("COMMIT")
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return attempt_id, "ok"

    async def begin_external_execution(self, *, attempt_id, identities) -> str:
        """P1C/§6: THE durable boundary. Nothing external may be called before this commits.

        After this row says EXTERNAL_PENDING the only honest statement about the world is
        "the effect MAY have happened" — and that stays true even if the adapter was never
        reached, which is exactly why it is written BEFORE the call rather than after it.

        Revocation is re-checked here (P1C/§14 case B): if the authority died while the
        attempt was merely CLAIMED, no external effect has happened yet and none will.
        """
        return await self._run(self._begin_external, attempt_id,
                               identities if type(identities) in (DashboardAuthorization, BrowserCommandAuthorization) else list(identities))

    def _begin_external(self, attempt_id, identities):
        if not identities:
            raise ValueError("begin_external_execution requires at least one identity")
        now = time.time()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute("SELECT * FROM execution_attempts WHERE attempt_id=?",
                                     (attempt_id,)).fetchone()
            if row is None:
                return self._exec_deny(None, None, "unknown_attempt")
            if row["status"] != _X.CLAIMED:
                return self._exec_deny(row["approval_id"], row["device_id"],
                                       "attempt_not_claimed:" + row["status"])
            if row["claim_authorization_method"] == MEMORY_COMMAND_METHOD:
                denied = self._browser_command_attempt_reason(row,
                    getattr(identities, "core_instance_id", ""), identities)
            elif row["claim_authorization_method"] == DASHBOARD_METHOD:
                if not isinstance(identities, DashboardAuthorization):
                    return self._exec_deny(row["approval_id"], None, "dashboard_authorization_required")
                denied = self._dashboard_attempt_reason(row, identities.core_instance_id, identities)
            elif type(identities) in (DashboardAuthorization, BrowserCommandAuthorization):
                return self._exec_deny(row["approval_id"], row["device_id"], "mobile_authorization_required")
            else:
                pred = " OR ".join(["(kind=? AND value=?)"] * len(identities))
                args = [x for pair in identities for x in pair]
                denied = "device_revoked" if self._conn.execute(
                    f"SELECT 1 FROM revocations WHERE {pred} LIMIT 1", args).fetchone() else None
            if denied:
                refusal = ("browser_before_external:" + denied
                           if row["claim_authorization_method"] in (DASHBOARD_METHOD, MEMORY_COMMAND_METHOD)
                           else "revoked_before_external_start")
                # Safe to stop: CLAIMED means the adapter was never called.
                self._conn.execute(
                    "UPDATE execution_attempts SET status=?, finished_at=?, detail=? "
                    "WHERE attempt_id=? AND status=?",
                    (_X.ABANDONED, now, refusal, attempt_id,
                     _X.CLAIMED))
                # HYGIENE/H8: terminalise the REQUEST too, in the same transaction. Refusing
                # to start was already correct, but the row was left saying EXECUTING for
                # ever — fail-safe and actively misleading to an operator, since nothing was
                # executing and nothing ever would. CLAIMED means the adapter was never
                # called, so FAILED here states something true.
                self._cas_transition(row["approval_id"], FAILED, row["device_id"],
                                     refusal)
                self._audit("execution_abandoned_dashboard" if row["claim_authorization_method"] == DASHBOARD_METHOD
                            else "execution_abandoned_revoked", approval_id=row["approval_id"],
                            device_id=row["device_id"], reason=attempt_id)
                self._conn.execute("COMMIT")
                return denied
            upd = self._conn.execute(
                "UPDATE execution_attempts SET status=?, boundary_at=? "
                "WHERE attempt_id=? AND status=?",
                (_X.EXTERNAL_PENDING, now, attempt_id, _X.CLAIMED))
            if upd.rowcount != 1:
                return self._exec_deny(row["approval_id"], row["device_id"], "boundary_lost")
            self._audit("execution_external_boundary", approval_id=row["approval_id"],
                        device_id=row["device_id"], reason=attempt_id)
            self._conn.execute("COMMIT")
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return "ok"

    async def task_authorization_receipt(self, approval_id, core_instance_id):
        """Historical proof of a SUCCEEDED task start, not permission for another action.

        Identity/method come from the claim that crossed the execution boundary.
        Later session expiry/revocation does not rewrite that historical receipt.
        No receipt is manufactured for a pending, unknown or legacy unbound claim.
        """
        return await self._run(self._task_authorization_receipt, approval_id, core_instance_id)

    def _task_authorization_receipt(self, approval_id, core_instance_id):
        req = self._conn.execute("SELECT * FROM approval_requests WHERE approval_id=?",
                                 (approval_id,)).fetchone()
        if req is None:
            return None, "unknown_request"
        if req["state"] != CONSUMED or req["tool"] not in DASHBOARD_TASK_TOOLS:
            return None, "task_not_consumed"
        if not core_instance_id or req["request_core_instance_id"] != core_instance_id:
            return None, "wrong_core_instance"
        execution_id = _X.execution_id_for(core_instance_id, approval_id)
        att = self._conn.execute("SELECT * FROM execution_attempts "
            "WHERE execution_id=? AND approval_id=? AND status=?",
            (execution_id, approval_id, _X.SUCCEEDED)).fetchone()
        if att is None or not att["claim_identity_bound"] or not att["boundary_at"]:
            return None, "execution_receipt_unbound"
        return self._task_receipt_data(req, att, core_instance_id)

    @staticmethod
    def _task_receipt_data(req, att, core_instance_id):
        if not core_instance_id or req["request_core_instance_id"] != core_instance_id:
            return None, "wrong_core_instance"
        if req["tool"] not in DASHBOARD_TASK_TOOLS:
            return None, "dashboard_tool_forbidden"
        if not att["claim_identity_bound"] or not att["boundary_at"]:
            return None, "execution_receipt_unbound"
        method = att["claim_authorization_method"]
        digest = _action_digest(tool_id=req["tool"], mode=req["mode"],
                                task=req["task"], workspace=req["workspace"])
        if (att["claim_principal"] != req["principal"]
                or att["claim_core_instance_id"] != core_instance_id
                or att["capability"] != req["tool"]
                or att["claim_action_digest"] != req["action_digest"]
                or digest != req["action_digest"] or method != req["decision_method"]):
            return None, "execution_receipt_binding_mismatch"
        reference = att["claim_browser_session_id"] if method == DASHBOARD_METHOD else att["device_id"]
        expected = req["decided_session"] if method == DASHBOARD_METHOD else req["decided_device"]
        if method not in (DASHBOARD_METHOD, "face_id") or not reference or reference != expected:
            return None, "execution_receipt_binding_mismatch"
        return {"approval_id": req["approval_id"], "execution_id": att["execution_id"],
                "attempt_id": att["attempt_id"], "method": method,
                "reference": "approval:" + req["approval_id"],
                "session_id": reference if method == DASHBOARD_METHOD else "",
                "device_id": reference if method == "face_id" else "",
                "authorizer": att["claim_principal"], "core_instance_id": core_instance_id,
                "tool": req["tool"], "action_digest": digest,
                "decided_at": req["decided_at"], "executed_at": att["finished_at"],
                "status": att["status"]}, "ok"

    async def task_start_claim_receipt(self, approval_id, execution_id, core_instance_id,
                                        allowed_environments):
        """Core-only authority for the task currently being created, NEVER success.

        Exact action and current session/device authority are checked inside one
        transaction. Only the already claimed EXTERNAL_PENDING attempt can supply
        this receipt; it cannot approve or claim anything, and is not a public API.
        """
        return await self._run(self._task_start_claim_receipt, approval_id, execution_id,
                               core_instance_id, frozenset(allowed_environments or ()))

    def _task_start_claim_receipt(self, approval_id, execution_id, core_instance_id, envs):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            result = self._task_start_claim_receipt_locked(approval_id, execution_id, core_instance_id, envs)
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return result

    def _task_start_claim_receipt_locked(self, approval_id, execution_id, core_instance_id, envs):
        if not core_instance_id or execution_id != _X.execution_id_for(core_instance_id, approval_id):
            return None, "wrong_execution_id"
        req = self._conn.execute("SELECT * FROM approval_requests WHERE approval_id=?",
                                 (approval_id,)).fetchone()
        if req is None or req["state"] != EXECUTING:
            return None, "task_not_executing"
        if req["expires_at"] <= time.time():
            return None, "request_expired"
        att = self._conn.execute("SELECT * FROM execution_attempts WHERE approval_id=? "
            "AND execution_id=? AND status=?", (approval_id, execution_id, _X.EXTERNAL_PENDING)).fetchone()
        if att is None:
            return None, "task_start_not_claimed"
        if not att["lease_expires_at"] or att["lease_expires_at"] <= time.time():
            return None, "execution_lease_expired"
        receipt, reason = self._task_receipt_data(req, att, core_instance_id)
        if receipt is None:
            return None, reason
        if att["claim_authorization_method"] == DASHBOARD_METHOD:
            reason = self._dashboard_attempt_reason(att, core_instance_id)
        else:
            dev = self._conn.execute("SELECT * FROM devices WHERE device_id=?",
                                     (att["device_id"],)).fetchone()
            if dev is None:
                return None, "unknown_device"
            reason, _revoked, fp, aakid = self._execution_authority(req, dev, envs)
            if reason is None:
                reason = self._claim_binding_mismatch(att, req, dev, fp, aakid, core_instance_id)
        if reason:
            return None, reason
        receipt["purpose"] = "task_start_authorization"
        return receipt, "ok"

    def _exec_deny(self, approval_id, device_id, reason: str) -> str:
        self._audit("execution_rejected", approval_id=approval_id, device_id=device_id,
                    reason=reason)
        self._conn.execute("COMMIT")
        return reason

    async def finish_execution_attempt(self, *, attempt_id, status, detail=None,
                                       request_state=None, expected_status=None) -> str:
        """Durably record an outcome — and the request's terminal state — together.

        The success audit is written INSIDE this transaction, after the row actually moves,
        so there can be no SUCCESS in the audit without a committed success. The partial
        unique index on (execution_id) WHERE status='SUCCEEDED' makes a second success a
        database error rather than a policy someone can forget.
        """
        if status not in (_X.SUCCEEDED, _X.FAILED_SAFE, _X.UNKNOWN, _X.ABANDONED):
            raise ValueError(f"not an outcome: {status!r}")
        return await self._run(self._finish_attempt, attempt_id, status, detail,
                               request_state, expected_status)

    def _finish_attempt(self, attempt_id, status, detail, request_state,
                        expected_status=None):
        now = time.time()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute("SELECT * FROM execution_attempts WHERE attempt_id=?",
                                     (attempt_id,)).fetchone()
            if row is None:
                return self._exec_deny(None, None, "unknown_attempt")
            if row["status"] in _X.TERMINAL_STATUSES:
                return self._exec_deny(row["approval_id"], row["device_id"],
                                       "attempt_already_final:" + row["status"])
            # FREEZE/F1B: a caller that decided WHAT to do from a snapshot read outside this
            # transaction must say which status it decided against. The classic loss is a
            # startup scan that saw CLAIMED ("the adapter was never called, safe to close")
            # while the owning process crossed the durable boundary a millisecond later: the
            # attempt is now EXTERNAL_PENDING, the effect MAY have happened, and closing it
            # as ABANDONED would durably assert the opposite. Losing the compare is correct
            # and the caller re-classifies.
            if expected_status is not None and row["status"] != expected_status:
                return self._exec_deny(
                    row["approval_id"], row["device_id"],
                    f"expected_status_mismatch:{expected_status}->{row['status']}")
            upd = self._conn.execute(
                "UPDATE execution_attempts SET status=?, finished_at=?, detail=?, "
                "lease_owner=NULL, lease_expires_at=NULL "
                "WHERE attempt_id=? AND status=?",
                (status, now, (str(detail)[:200] if detail else None), attempt_id,
                 row["status"]))
            if upd.rowcount != 1:
                return self._exec_deny(row["approval_id"], row["device_id"], "outcome_lost")
            if request_state is not None:
                self._cas_transition(row["approval_id"], request_state, row["device_id"],
                                     None if status == _X.SUCCEEDED else str(detail)[:120]
                                     if detail else status)
            self._audit("execution_" + status.lower(), approval_id=row["approval_id"],
                        device_id=row["device_id"], reason=attempt_id)
            self._conn.execute("COMMIT")
        except (IllegalTransition, ConcurrentTransition) as exc:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            self._audit("execution_outcome_conflict", reason=str(exc)[:120])
            return "conflict"
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return "ok"

    async def open_attempt_for(self, execution_id):
        """The one open attempt for a logical action, or None. Read-only."""
        return await self._run(
            lambda: self._conn.execute(
                "SELECT * FROM execution_attempts WHERE execution_id=? AND status IN (?,?,?)",
                (execution_id, _X.CLAIMED, _X.EXTERNAL_PENDING, _X.UNKNOWN)).fetchone())

    async def open_attempt_by_id(self, attempt_id):
        """The attempt row, but only while it is still OPEN. Read-only."""
        return await self._run(
            lambda: self._conn.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id=? AND status IN (?,?,?)",
                (attempt_id, _X.CLAIMED, _X.EXTERNAL_PENDING, _X.UNKNOWN)).fetchone())

    async def open_execution_attempts(self):
        """HYGIENE/H9: every attempt an administrator still has to deal with. Read-only."""
        return await self._run(
            lambda: [dict(r) for r in self._conn.execute(
                "SELECT * FROM execution_attempts WHERE status IN (?,?,?) "
                "ORDER BY claimed_at", (_X.CLAIMED, _X.EXTERNAL_PENDING,
                                        _X.UNKNOWN)).fetchall()])

    async def attempts_for(self, execution_id):
        return await self._run(
            lambda: [dict(r) for r in self._conn.execute(
                "SELECT * FROM execution_attempts WHERE execution_id=? ORDER BY claimed_at",
                (execution_id,)).fetchall()])

    async def acquire_recovery_lease(self, *, attempt_id, owner, lease_seconds=300.0) -> str:
        """P1C/§13: exactly one recovery owner, decided by a compare-and-swap.

        A lease settles OWNERSHIP. It does NOT settle whether an external effect happened —
        an expired lease on an EXTERNAL_PENDING attempt still means "may have happened", and
        the recovery policy, not the lease, decides what may be done about that.
        """
        return await self._run(self._acquire_lease, attempt_id, owner, lease_seconds)

    def _acquire_lease(self, attempt_id, owner, lease_seconds):
        now = time.time()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            upd = self._conn.execute(
                "UPDATE execution_attempts SET lease_owner=?, lease_expires_at=? "
                "WHERE attempt_id=? AND status IN (?,?,?) "
                "  AND (lease_owner IS NULL OR lease_owner=? OR lease_expires_at < ?)",
                (owner, now + lease_seconds, attempt_id, _X.CLAIMED, _X.EXTERNAL_PENDING,
                 _X.UNKNOWN, owner, now))
            out = "ok" if upd.rowcount == 1 else "lease_held"
            if out == "ok":
                self._audit("execution_lease_acquired", reason=f"{attempt_id}:{owner}")
            self._conn.execute("COMMIT")
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return out

    # ---- challenges (single-use nonce) ----
    async def add_challenge(self, *, challenge_nonce, approval_id, device_id, principal,
                           action_digest, expires_at, payload_sha256):
        await self._run(self._add_challenge, challenge_nonce, approval_id, device_id,
                        principal, action_digest, expires_at, payload_sha256)

    def _add_challenge(self, nonce, approval_id, device_id, principal, action_digest,
                       expires_at, payload_sha256):
        """HYGIENE/H4: invalidate, insert and audit in ONE transaction.

        These were three autocommits. The invalidation of older open challenges and the
        creation of the new one are a single logical step — "this approval now has exactly
        one open challenge, and it is this one". Split across commits, an interruption
        between them left the approval with NO open challenge at all (the phone would have
        to ask again), and two concurrent issuances could interleave so that both survived
        the other's sweep. Neither grants authority — a challenge is a nonce, not a decision,
        and `_cas_consume_challenge` still binds every field — but the invariant the code
        claims in its own comment was not actually enforced.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # invalidate prior open challenges for this approval (controlled single-open)
            self._conn.execute(
                "UPDATE challenges SET consumed=1 WHERE approval_id=? AND consumed=0",
                (approval_id,))
            self._conn.execute(
                "INSERT INTO challenges (challenge_nonce, approval_id, device_id, principal, "
                "action_digest, issued_at, expires_at, payload_sha256) VALUES (?,?,?,?,?,?,?,?)",
                (nonce, approval_id, device_id, principal, action_digest, time.time(),
                 expires_at, payload_sha256))
            self._audit("challenge_issued", approval_id=approval_id, device_id=device_id)
            self._conn.execute("COMMIT")
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    async def consume_challenge(self, *, challenge_nonce, approval_id, device_id, principal,
                               action_digest, payload_sha256):
        return await self._run(self._consume_challenge, challenge_nonce, approval_id,
                               device_id, principal, action_digest, payload_sha256)

    def _consume_challenge(self, nonce, approval_id, device_id, principal, action_digest,
                           payload_sha256):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            out = self._cas_consume_challenge(nonce, approval_id, device_id, principal,
                                              action_digest, payload_sha256)
            self._conn.execute("COMMIT")
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return out

    def _cas_consume_challenge(self, nonce, approval_id, device_id, principal, action_digest,
                               payload_sha256):
        """P1B/§5: burn the nonce in ONE conditional UPDATE. Open write transaction required.

        Before this it was SELECT -> `if consumed: return replay` -> UPDATE, in autocommit.
        Reproduced with two connections and a barrier: both returned "ok" for the same nonce,
        so one signed decision could be consumed twice. Every binding the old code checked in
        Python is now a predicate in the UPDATE, so the check and the burn cannot be
        separated. The SELECT afterwards only explains a loss.

        The bindings are the ones the challenge was ISSUED with — approval_id, device,
        principal, action digest and the exact displayed payload hash (V2). A pre-V2 row has
        payload_sha256='' and can never match, which is the intended fail-closed behaviour.
        """
        upd = self._conn.execute(
            "UPDATE challenges SET consumed=1 WHERE challenge_nonce=? AND consumed=0 "
            "AND expires_at >= ? AND approval_id=? AND device_id=? AND principal=? "
            "AND action_digest=? AND payload_sha256<>'' AND payload_sha256=?",
            (nonce, time.time(), approval_id, device_id, principal, action_digest,
             payload_sha256))
        if upd.rowcount == 1:
            return "ok"
        r = self._conn.execute("SELECT * FROM challenges WHERE challenge_nonce=?",
                               (nonce,)).fetchone()
        if r is None:
            return "unknown"
        if r["consumed"]:
            return "replay"
        if time.time() > r["expires_at"]:
            return "expired"
        if (r["approval_id"] != approval_id or r["device_id"] != device_id
                or r["principal"] != principal or r["action_digest"] != action_digest):
            return "mismatch"
        return "display_mismatch"

    # ---- S2A.1: schema migration (idempotent additive columns) ----
    def _migrate(self, conn) -> None:
        request_cols = {r["name"] for r in conn.execute("PRAGMA table_info(approval_requests)")}
        for name, decl in (("request_core_instance_id", "TEXT NOT NULL DEFAULT ''"),
                           ("decision_method", "TEXT NOT NULL DEFAULT ''"),
                           ("decided_session", "TEXT")):
            if name not in request_cols:
                conn.execute(f"ALTER TABLE approval_requests ADD COLUMN {name} {decl}")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(devices)").fetchall()}
        add = {
            "attestation_status": "TEXT NOT NULL DEFAULT 'UNATTESTED'",
            "app_attest_key_id": "TEXT",
            "app_attest_public_key": "TEXT",
            "app_attest_counter": "INTEGER NOT NULL DEFAULT 0",
            "attested_at": "REAL",
            "app_id": "TEXT",
            "environment": "TEXT",
            # P1A.8/C1: the ONE authority-bearing enrollment generation for this device.
            "current_enrollment_id": "TEXT",
        }
        for name, decl in add.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE devices ADD COLUMN {name} {decl}")
        # S2A.1-P0 / Approval Protocol V2: challenges bind the exact signed display bytes.
        # Pre-V2 rows keep '' and can therefore never satisfy a V2 decision -> fail closed.
        # P1A.5/M1: the operator's --reason used to OVERWRITE the revoked identity in the
        # audit row, so using the tool as documented erased what had been cut off. Identity
        # and reason are now separate columns.
        at_cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(attestation_challenges)").fetchall()}
        for name, decl in (("app_attest_key_id", "TEXT"),
                           ("superseded", "INTEGER NOT NULL DEFAULT 0")):
            if name not in at_cols:
                conn.execute(f"ALTER TABLE attestation_challenges ADD COLUMN {name} {decl}")
        au_cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit)").fetchall()}
        if "identity" not in au_cols:
            conn.execute("ALTER TABLE audit ADD COLUMN identity TEXT")
        ch_cols = {r["name"] for r in conn.execute("PRAGMA table_info(challenges)").fetchall()}
        if "payload_sha256" not in ch_cols:
            conn.execute("ALTER TABLE challenges ADD COLUMN payload_sha256 TEXT NOT NULL DEFAULT ''")
        # FREEZE/F2: claim-time authority snapshot. Deliberately NOT backfilled — an attempt
        # claimed before this existed has no trustworthy record of the identity it ran under,
        # and inventing one would be exactly the lie the snapshot exists to prevent. Legacy
        # rows keep claim_identity_bound=0 and are refused a new external write.
        ea_cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(execution_attempts)").fetchall()}
        for name, decl in (("claim_identity_bound", "INTEGER NOT NULL DEFAULT 0"),
                           ("claim_core_instance_id", "TEXT"),
                           ("claim_principal", "TEXT"),
                           ("claim_environment", "TEXT"),
                           ("claim_enrollment_id", "TEXT"),
                           ("claim_approval_key_sha256", "TEXT"),
                           ("claim_app_attest_key_id", "TEXT"),
                           ("claim_authorization_method", "TEXT NOT NULL DEFAULT 'face_id'"),
                           ("claim_browser_session_id", "TEXT"),
                           ("claim_browser_session_binding", "TEXT"),
                           ("claim_action_digest", "TEXT")):
            if name not in ea_cols:
                conn.execute(f"ALTER TABLE execution_attempts ADD COLUMN {name} {decl}")

    # ---- S2A.1: device attestation lifecycle ----
    async def begin_enrollment_atomic(self, *, device_id, key_id, public_key_x963, principal,
                                      app_attest_key_id, identities, enrollment_id, nonce,
                                      approval_pubkey_sha256, binding_raw, expires_at,
                                      transport_cred_hash=None) -> str:
        """P1A.8/C1: read, supersede, enrol and issue the challenge in ONE transaction.

        Before this the supersede sweep + device upsert were one transaction and the
        challenge insert another. Two concurrent begins interleaved as
        write | write | insert | insert, so NEITHER challenge was superseded — the device row
        held the second caller's approval key while the first caller's challenge stayed
        valid, and the victim's own legitimate attestation then marked the attacker's key
        ATTESTED. Reproduced end to end before this fix.

        `current_enrollment_id` makes the winner explicit: whichever begin commits last under
        the write lock owns the device, and every older enrollment_id is terminal.
        """
        return await self._run(self._begin_enrollment_atomic, device_id, key_id,
                               public_key_x963, principal, app_attest_key_id, identities,
                               enrollment_id, nonce, approval_pubkey_sha256, binding_raw,
                               expires_at, transport_cred_hash)

    def _begin_enrollment_atomic(self, device_id, key_id, pk, principal, aakid, identities,
                                 eid, nonce, appr_fp, binding_raw, expires_at, tch):
        if not identities:
            raise ValueError("begin_enrollment requires the bound identities")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            pred = " OR ".join(["(kind=? AND value=?)"] * len(identities))
            args = [x for pair in identities for x in pair]
            if self._conn.execute(
                    f"SELECT 1 FROM revocations WHERE {pred} LIMIT 1", args).fetchone():
                self._audit("enrollment_rejected_revoked_key", device_id=device_id,
                            reason="identity_revoked")
                self._conn.execute("COMMIT")
                return "device_revoked"
            if not self._begin_device_enrollment_write(device_id, key_id, pk, principal,
                                                       aakid, tch, eid):
                self._audit("enrollment_rejected_revoked_key", device_id=device_id,
                            reason="device_record_terminal")
                self._conn.execute("COMMIT")
                return "device_revoked"
            # Older generations lose, in the same transaction that creates the new one.
            self._conn.execute(
                "UPDATE attestation_challenges SET superseded=1 "
                "WHERE device_id=? AND enrollment_id<>? AND consumed=0 AND superseded=0",
                (device_id, eid))
            self._conn.execute(
                "INSERT INTO attestation_challenges (enrollment_id, nonce, device_id, "
                "principal, approval_key_id, approval_pubkey_sha256, binding_raw, issued_at, "
                "expires_at, app_attest_key_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (eid, nonce, device_id, principal, key_id, appr_fp, binding_raw,
                 time.time(), expires_at, aakid))
            self._audit("attestation_challenge", device_id=device_id, reason=eid,
                        identity=f"{REVOKE_APPROVAL_KEY}:{appr_fp}")
            self._conn.execute("COMMIT")
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return "ok"

    def _begin_device_enrollment_write(self, device_id, key_id, pk, principal, aakid, tch,
                                       enrollment_id=None):
        """F5.1: REVOKED is terminal, enforced by SQL — not by the executor.

        A SELECT-then-INSERT guard was a check-then-act race: with autocommit
        (isolation_level=None) a revoke committed by ANOTHER connection between the two
        statements was silently clobbered by an unconditional INSERT OR REPLACE, and the
        device came back as ACTIVE (which then also let the guarded completion succeed).

        This is now ONE statement: the upsert's DO UPDATE carries
        `WHERE devices.status <> 'REVOKED'`, so an existing revoked row can never be
        reactivated no matter how many connections/threads/processes race. rowcount == 0
        means the row exists and is REVOKED -> fail closed. The DO UPDATE mirrors what
        INSERT OR REPLACE used to reset implicitly (stale attestation material cleared),
        so a legitimate re-enrollment still starts from a clean slate."""
        cur = self._conn.execute(
            "INSERT INTO devices (device_id, key_id, public_key_x963, principal, "
            "transport_cred_hash, attested, status, enrolled_at, attestation_status, "
            "app_attest_key_id, app_attest_counter, current_enrollment_id) "
            "VALUES (?,?,?,?,?,0,?,?,?,?,0,?) "
            "ON CONFLICT(device_id) DO UPDATE SET "
            "  key_id=excluded.key_id, public_key_x963=excluded.public_key_x963, "
            "  principal=excluded.principal, transport_cred_hash=excluded.transport_cred_hash, "
            "  attested=0, status=excluded.status, enrolled_at=excluded.enrolled_at, "
            "  attestation_status=excluded.attestation_status, "
            "  app_attest_key_id=excluded.app_attest_key_id, app_attest_counter=0, "
            "  current_enrollment_id=excluded.current_enrollment_id, "
            "  app_attest_public_key=NULL, attested_at=NULL, app_id=NULL, environment=NULL "
            "WHERE devices.status <> ?",
            (device_id, key_id, pk, principal, tch, DEVICE_ACTIVE, time.time(),
             ATT_PENDING, aakid, enrollment_id, DEVICE_REVOKED))
        if cur.rowcount != 1:
            self._audit("device_enroll_begin_rejected", device_id=device_id, reason="revoked")
            return False
        self._audit("device_enroll_begin", device_id=device_id, reason=principal)
        return True

    # P1A.8/§8: `set_device_attested` is GONE. It was a second, weaker way to mark a device
    # trusted — no challenge, no enrolment generation, no history row — and any future caller
    # would have bypassed every check in finalize_attestation. It had zero callers in src,
    # scripts and tests. `finalize_attestation` is the only trusted-transition writer.

    async def set_device_attestation_failed(self, *, enrollment_id, device_id, reason=None):
        """P1A.8/§7: only the enrolment that OWNS the device may mark it failed.

        This was an unconditional UPDATE by device_id. A losing enrolment generation — an
        attacker replaying a superseded challenge with a bad attestation — could therefore
        drive the legitimate device to ATT_FAILED and lock the owner out. The generation is
        part of the UPDATE, so the guard cannot be raced.
        """
        await self._run(self._set_att_failed, enrollment_id, device_id, reason)

    def _set_att_failed(self, eid, device_id, reason):
        detail = str(reason)[:120] if reason else None
        cur = self._conn.execute(
            "UPDATE devices SET attestation_status=? "
            "WHERE device_id=? AND current_enrollment_id=? AND attestation_status=?",
            (ATT_FAILED, device_id, eid, ATT_PENDING))
        if cur.rowcount == 1:
            self._audit("device_attestation_failed", device_id=device_id, reason=detail)
        else:
            # The failure is still recorded — it just carries no authority over the device.
            self._audit("device_attestation_failed_stale", device_id=device_id, reason=detail)

    # HYGIENE/H2: `bump_app_attest_counter` is GONE. It set the replay counter
    # unconditionally, by device_id, with no monotonicity guard and ZERO callers. The counter
    # is advanced inside `commit_decision`, where it is guarded by `app_attest_counter < ?`
    # and commits with the decision it belongs to.

    # ---- S2A.1: attestation challenges (one-time, durable) ----
    async def finalize_attestation(self, *, enrollment_id, app_attest_public_key_x963,
                                   app_attest_counter, environment, app_id,
                                   core_instance_id) -> str:
        """P1A.8/C1: the ENTIRE trusted transition, checked against TWO authoritative sources.

        The caller passes what the attestation actually PROVED (the verified App Attest
        public key, its counter, the environment) plus this core's own identity. It passes no
        security-check values: every field this method compares is read here, under the write
        lock, from the challenge row and the device row — and the two identities are computed
        from key material, not accepted as strings.

        Before P1A.8 the control plane handed back the challenge's own
        `approval_pubkey_sha256` and `app_attest_key_id`, which the store then compared
        against the challenge — tautologies that could not fail. The device row's approval key
        and principal were never compared at all, so a concurrent second enrolment could park
        an attacker's key on the device row and have the victim's attestation promote it.

        `enrollment_id == devices.current_enrollment_id` is the decisive check: exactly one
        enrolment generation owns a device, and finalising any other is refused.
        """
        return await self._run(self._finalize_attestation, enrollment_id,
                               bytes(app_attest_public_key_x963), app_attest_counter,
                               environment, app_id, core_instance_id)

    def _finalize_attestation(self, eid, aapk_raw, counter, env, app_id, core_id):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # ---- source 1: the challenge, authoritative for what was ISSUED --------------
            ch = self._conn.execute(
                "SELECT * FROM attestation_challenges WHERE enrollment_id=?", (eid,)).fetchone()
            if ch is None:
                return self._finalize_deny(None, "challenge_unknown")
            device_id = ch["device_id"]          # never the caller's idea of the device
            if ch["consumed"]:
                return self._finalize_deny(device_id, "challenge_already_consumed")
            if ch["superseded"]:
                return self._finalize_deny(device_id, "challenge_superseded")
            if time.time() > ch["expires_at"]:
                return self._finalize_deny(device_id, "challenge_expired")
            if not ch["app_attest_key_id"]:
                return self._finalize_deny(device_id, "challenge_unbound")

            # The binding the phone signed names the core it was issued by.
            try:
                binding = json.loads(bytes(ch["binding_raw"]).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._finalize_deny(device_id, "challenge_binding_unreadable")
            if not core_id or binding.get("core_instance_id") != core_id:
                return self._finalize_deny(device_id, "wrong_core_instance")
            if binding.get("enrollment_id") != eid or binding.get("device_id") != device_id:
                return self._finalize_deny(device_id, "challenge_binding_mismatch")

            # ---- source 2: the device row, authoritative for what is CURRENT -------------
            dev = self._conn.execute("SELECT * FROM devices WHERE device_id=?",
                                     (device_id,)).fetchone()
            if dev is None:
                return self._finalize_deny(device_id, "unknown_device")

            # The identities are COMPUTED, here, from key material.
            try:
                dev_appr_fp = _crypto.fingerprint(bytes.fromhex(dev["public_key_x963"] or ""))
            except ValueError:
                return self._finalize_deny(device_id, "device_key_unreadable")
            try:
                derived_aakid = _AA.app_attest_identity_from_public_key(aapk_raw)
            except (_AA.AppAttestIdentityError, ValueError):
                return self._finalize_deny(device_id, "app_attest_key_mismatch")

            # Revocation first: every revoke kind also flips devices.status, so folding the
            # two together would report a revoked device as merely "not pending" and lose the
            # revocation audit event.
            pred = "(kind=? AND value=?) OR (kind=? AND value=?) OR (kind=? AND value=?)"
            revoked = self._conn.execute(
                f"SELECT kind FROM revocations WHERE {pred} LIMIT 1",
                (REVOKE_DEVICE, device_id, REVOKE_APPROVAL_KEY, dev_appr_fp,
                 REVOKE_APP_ATTEST_KEY, derived_aakid)).fetchone()
            if revoked or dev["status"] == DEVICE_REVOKED:
                self._audit("enrollment_rejected_revoked_key", device_id=device_id,
                            reason=revoked["kind"] if revoked else dev["status"])
                return self._finalize_deny(device_id, "device_revoked")

            # THE generation check: one enrolment owns the device, and it is not this one.
            if (dev["current_enrollment_id"] or "") != eid:
                return self._finalize_deny(device_id, "enrollment_superseded")
            # Challenge and device row must independently agree on every bound field.
            if ch["approval_pubkey_sha256"] != dev_appr_fp:
                return self._finalize_deny(device_id, "approval_key_mismatch")
            if ch["approval_key_id"] != dev["key_id"]:
                return self._finalize_deny(device_id, "approval_key_mismatch")
            if ch["principal"] != dev["principal"]:
                return self._finalize_deny(device_id, "principal_mismatch")
            if binding.get("approval_public_key_sha256") != dev_appr_fp:
                return self._finalize_deny(device_id, "approval_key_mismatch")
            # ... and the key that was actually verified must BE that app-attest identity.
            if ch["app_attest_key_id"] != derived_aakid:
                return self._finalize_deny(device_id, "app_attest_key_mismatch")
            if (dev["app_attest_key_id"] or "") != derived_aakid:
                return self._finalize_deny(device_id, "device_rebound")
            if dev["status"] != DEVICE_ACTIVE or dev["attestation_status"] != ATT_PENDING:
                return self._finalize_deny(device_id, "not_pending_attestation")
            # An empty environment is never authority-bearing (_environment_blocked treats it
            # as outside every policy), so it is refused rather than recorded as trusted.
            if not env:
                return self._finalize_deny(device_id, "environment_unknown")

            self._conn.execute(
                "UPDATE devices SET attestation_status=?, attested=1, app_attest_public_key=?, "
                "app_attest_counter=?, environment=?, app_id=?, attested_at=? "
                "WHERE device_id=? AND status=? AND attestation_status=? "
                "AND current_enrollment_id=?",
                (ATT_ATTESTED, aapk_raw.hex(), counter, env, app_id, time.time(),
                 device_id, DEVICE_ACTIVE, ATT_PENDING, eid))
            self._conn.execute(
                "UPDATE attestation_challenges SET consumed=1 WHERE enrollment_id=?", (eid,))
            self._conn.execute(
                "INSERT INTO device_identity_history "
                "(device_id, approval_key_sha256, app_attest_key_id, bound_at, provenance) "
                "VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                (device_id, dev_appr_fp, derived_aakid, time.time(), "attestation_completed"))
            self._audit("device_attested", device_id=device_id, reason=env,
                        identity=f"{REVOKE_APP_ATTEST_KEY}:{derived_aakid}")
            self._conn.execute("COMMIT")
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return "ok"

    def _finalize_deny(self, device_id, reason: str) -> str:
        self._audit("device_attest_rejected", device_id=device_id, reason=reason)
        self._conn.execute("COMMIT")     # the audit row is the only write on this path
        return reason

    async def peek_attestation_challenge(self, enrollment_id):
        """Read WITHOUT consuming. The authoritative re-check happens inside
        finalize_attestation, under the write lock."""
        return await self._run(
            lambda: self._conn.execute(
                "SELECT * FROM attestation_challenges WHERE enrollment_id=?",
                (enrollment_id,)).fetchone())

    # HYGIENE/H2: `consume_attestation_challenge` is GONE. It was an unguarded
    # SELECT -> check -> UPDATE on attestation_challenges with ZERO callers — no generation
    # check, no revocation check, no binding comparison. `finalize_attestation` consumes the
    # challenge inside its own transaction; a second, weaker consumer was a footgun waiting
    # for its first caller.
