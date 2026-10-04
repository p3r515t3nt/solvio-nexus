"""A fresh App Attest proof for one exact app task start, separate from voice.

Transport credentials may fetch a challenge. Only the registered App Attest key
can prove that the task came from that app instance. This is not Face ID or proof
of a person touching the screen. The owner's explicit app-task policy consumes
this authenticated entrance; this module grants no arbitrary effect authority.

The existing approval database owns the nonce and the current device identity.
No second identity store, in-memory replay cache, pseudo-device, approval request,
or biometric decision is created. The nonce namespace and assertion domain are
specific to task starts, so other approval/voice proofs cannot be substituted.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass

from solvio.agent_runtime.store import SCOPES
from solvio.agent_runtime.task_start_service import conversation_reference, request_identifier
from solvio.capabilities.agent import MIN_OBJECTIVE, MAX_OBJECTIVE
from solvio.security.mobile_approval import app_attest as AA
from solvio.security.mobile_approval import protocol as P
from solvio.security.mobile_approval.store import ApprovalStoreError

DOMAIN_TASK_START = b"SOLVIO_APP_TASK_START_V1"
TYPE_TASK_START_BINDING = "app_task_start_binding"
BINDING_PROTOCOL_VERSION = 1
NONCE_TTL = 30.0
_CHALLENGE_PREFIX = "app-task-start:"
_REQUEST_FIELDS = frozenset({"scope", "objective", "target_repo", "client_request_id"})
_NONCE = re.compile(r"[a-f0-9]{64}\Z")


def canonical_task_body(task_body: dict) -> dict:
    """Validate the complete, exact body; never strip, truncate, or infer authority.

    Callers use this same returned body for task creation. Optional wire defaults
    must be made explicit BEFORE challenge issuance, not changed after a proof.
    """
    # N8/C3: `conversation_ref` ist ein optionales Zusatzfeld, kombinierbar mit
    # genau einem der bisherigen Zusaetze. Fehlt es, ist das Ergebnis byteidentisch
    # zu vorher — `canonical_bytes` ist sortiertes JSON, ein abwesender Schluessel
    # aendert keine Bytes.
    fields = set(task_body) if isinstance(task_body, dict) else set()
    chat_bound = "conversation_ref" in fields
    if (not isinstance(task_body, dict)
            or fields - {"conversation_ref"} not in (
                _REQUEST_FIELDS, _REQUEST_FIELDS | {"document_request"},
                _REQUEST_FIELDS | {"file_request"},
                _REQUEST_FIELDS | {"action_request"}, _REQUEST_FIELDS | {"action_intent"})):
        raise ValueError("task body requires scope, objective, target_repo, client_request_id")
    if chat_bound:
        if not isinstance(task_body["conversation_ref"], str) or not task_body["conversation_ref"]:
            raise ValueError("invalid_conversation_ref")
        conversation_reference(task_body["conversation_ref"])
    if any(not isinstance(task_body[key], str) for key in _REQUEST_FIELDS):
        raise ValueError("task body fields must be strings")
    if task_body["scope"] not in SCOPES:
        raise ValueError("unknown task scope")
    objective = task_body["objective"]
    if (objective != objective.strip() or not MIN_OBJECTIVE <= len(objective) <= MAX_OBJECTIVE
            or "\x00" in objective):
        raise ValueError("task objective must be trimmed and within capability limits")
    repository = task_body["target_repo"]
    if len(repository) > 4096 or "\x00" in repository or repository != repository.strip():
        raise ValueError("target repository is invalid")
    if task_body["scope"] != "build" and repository:
        raise ValueError("only build tasks can specify a repository")
    request_identifier(task_body["client_request_id"])
    result = dict(task_body)
    if "document_request" in task_body:
        from solvio.agent_runtime.document_contract import canonical_request
        if task_body["scope"] != "research":
            raise ValueError("document_requires_research")
        result["document_request"] = canonical_request(task_body["document_request"])
    if "file_request" in task_body:
        from solvio.agent_runtime.file_inputs import canonical_request
        if task_body["scope"] != "research":
            raise ValueError("files_require_research")
        result["file_request"] = canonical_request(task_body["file_request"])
    if (task_body["scope"] == "action") != ("action_request" in task_body or "action_intent" in task_body):
        raise ValueError("action_contract_required")
    if "action_request" in task_body:
        from solvio.agent_runtime.action_contract import canonical_request
        result["action_request"] = canonical_request(task_body["action_request"])
    if "action_intent" in task_body:
        from solvio.agent_runtime.action_intent import validate_request
        result["action_intent"] = validate_request(task_body["action_intent"]).descriptor
    # Reject unpaired surrogates now, rather than raising inside an authority transaction.
    P.canonical_bytes(result).decode("utf-8")
    return result


def request_digest(task_body: dict) -> str:
    return hashlib.sha256(P.canonical_bytes(canonical_task_body(task_body))).hexdigest()


def build_binding(*, core_instance_id: str, principal: str, device_id: str,
                  nonce: str, request_digest: str, enrollment_id: str,
                  app_attest_key_id: str, approval_key_sha256: str) -> dict:
    return {"protocol_version": BINDING_PROTOCOL_VERSION, "type": TYPE_TASK_START_BINDING,
            "core_instance_id": core_instance_id, "principal_id": principal,
            "device_id": device_id, "nonce": nonce, "request_digest": request_digest,
            "enrollment_id": enrollment_id, "app_attest_key_id": app_attest_key_id,
            "approval_key_sha256": approval_key_sha256}


def client_data_hash(binding_raw: bytes) -> bytes:
    return hashlib.sha256(DOMAIN_TASK_START + b"\x00" + binding_raw).digest()


@dataclass(frozen=True)
class TaskStartChallenge:
    nonce: str
    request_digest: str
    expires_at: float
    binding_raw: bytes

    def as_dict(self) -> dict:
        return {"nonce": self.nonce, "request_digest": self.request_digest,
                "expires_at": self.expires_at, "binding_b64": P.b64e(self.binding_raw)}


@dataclass(frozen=True)
class TaskStartActor:
    """A verified entrance receipt, not a reusable task grant or Face-ID assertion."""
    principal: str
    device_id: str
    nonce: str
    request_digest: str
    core_instance_id: str


class TaskStartProofService:
    body_digest = staticmethod(request_digest)
    assertion_hash = staticmethod(client_data_hash)
    challenge_prefix = _CHALLENGE_PREFIX
    binding_type = TYPE_TASK_START_BINDING
    audit_prefix = "app_task_start"

    def __init__(self, control_plane, *, nonce_ttl: float = NONCE_TTL):
        if (isinstance(nonce_ttl, bool) or not isinstance(nonce_ttl, (int, float))
                or not math.isfinite(nonce_ttl) or not 0 < nonce_ttl <= 300):
            raise ValueError("task-start nonce lifetime must be within 300 seconds")
        if not isinstance(control_plane.core_instance_id, str) or not control_plane.core_instance_id:
            raise ValueError("persisted control-plane identity required")
        self.control_plane = control_plane
        self.store = control_plane.store
        self.core_instance_id = control_plane.core_instance_id
        self.nonce_ttl = float(nonce_ttl)

    def _connection(self):
        if self.store._conn is None or self.store._closed or self.store.read_only:
            raise ApprovalStoreError("writable approval store unavailable")
        return self.store._conn

    def _device(self, device_id, principal=None):
        """Existing authority predicate under the store's transaction, plus App Attest key."""
        device = self.store._conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if device is None:
            return None
        reason, _, approval_fp, aakid = self.store._execution_authority(
            {"principal": principal if principal is not None else device["principal"]}, device,
            self.control_plane.allowed_environments)
        if reason or not device["principal"] or device["app_id"] != self.control_plane.app_id:
            return None
        try:
            public_key = bytes.fromhex(device["app_attest_public_key"] or "")
            if AA.app_attest_identity_from_public_key(public_key) != aakid:
                return None
        except (ValueError, AA.AppAttestIdentityError):
            return None
        return device, approval_fp, aakid, public_key

    def _binding(self, verified_device, nonce, digest):
        device, approval_fp, aakid, _ = verified_device
        binding = build_binding(core_instance_id=self.core_instance_id,
            principal=device["principal"], device_id=device["device_id"], nonce=nonce,
            request_digest=digest, enrollment_id=device["current_enrollment_id"],
            app_attest_key_id=aakid, approval_key_sha256=approval_fp)
        binding["type"] = self.binding_type
        return P.canonical_bytes(binding)

    async def issue(self, *, device_id: str, transport_cred: str,
                    task_body: dict) -> TaskStartChallenge | None:
        """Static transport may request a nonce; it cannot start a task."""
        try:
            digest = self.body_digest(task_body)
            if (not isinstance(device_id, str) or not isinstance(transport_cred, str)
                    or not await self.control_plane.verify_transport_cred(device_id, transport_cred)):
                return None
            return await self.store._run(self._issue, device_id, transport_cred, digest)
        except (ApprovalStoreError, sqlite3.Error, ValueError, UnicodeError):
            return None

    def _issue(self, device_id, transport_cred, digest):
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            verified = self._device(device_id)
            if (verified is None or not hmac.compare_digest(verified[0]["transport_cred_hash"] or "",
                    hashlib.sha256(transport_cred.encode("utf-8")).hexdigest())
                    or self.control_plane.core_instance_id != self.core_instance_id
                    or self.control_plane.attest_verifier is None):
                conn.execute("COMMIT")
                return None
            nonce = secrets.token_hex(32)
            raw = self._binding(verified, nonce, digest)
            now = time.time()
            expires = now + self.nonce_ttl
            conn.execute("INSERT INTO challenges (challenge_nonce,approval_id,device_id,principal,"
                "action_digest,issued_at,expires_at,payload_sha256) VALUES (?,?,?,?,?,?,?,?)",
                (nonce, self.challenge_prefix + nonce, device_id, verified[0]["principal"], digest,
                 now, expires, hashlib.sha256(raw).hexdigest()))
            self.store._audit(self.audit_prefix + "_challenge_issued", device_id=device_id)
            conn.execute("COMMIT")
            return TaskStartChallenge(nonce, digest, expires, raw)
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    async def verify(self, *, device_id: str, nonce: str, task_body: dict,
                     assertion: bytes) -> TaskStartActor | None:
        """Verify and consume exactly once. No await separates device check and nonce claim."""
        try:
            digest = self.body_digest(task_body)
            if (not isinstance(device_id, str) or not isinstance(nonce, str) or not _NONCE.fullmatch(nonce)
                    or not isinstance(assertion, bytes) or not 0 < len(assertion) <= 16384):
                return None
            return await self.store._run(self._verify, device_id, nonce, digest, assertion)
        except (ApprovalStoreError, sqlite3.Error, ValueError, UnicodeError):
            return None

    def _verify(self, device_id, nonce, digest, assertion):
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = self._verify_and_consume(device_id, nonce, digest, assertion)
            conn.execute("COMMIT")
            return result
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    async def still_current(self, actor):
        """Recheck the same consumed proof after native admission awaits."""
        if type(actor) is not TaskStartActor:
            return False
        def check():
            row = self._connection().execute('SELECT * FROM challenges WHERE challenge_nonce=?', (actor.nonce,)).fetchone()
            verified = self._device(actor.device_id, actor.principal)
            return bool(row is not None and row['consumed'] and row['expires_at'] > time.time()
                and row['approval_id'] == self.challenge_prefix + actor.nonce
                and row['device_id'] == actor.device_id and row['principal'] == actor.principal
                and row['action_digest'] == actor.request_digest and verified is not None
                and self.control_plane.core_instance_id == actor.core_instance_id == self.core_instance_id
                and hmac.compare_digest(row['payload_sha256'], hashlib.sha256(
                    self._binding(verified, actor.nonce, actor.request_digest)).hexdigest()))
        return await self.store._run(check)

    def _verify_and_consume(self, device_id, nonce, digest, assertion):
        cp = self.control_plane
        row = self.store._conn.execute("SELECT * FROM challenges WHERE challenge_nonce=?", (nonce,)).fetchone()
        if (row is None or row["consumed"] or row["expires_at"] <= time.time()
                or row["approval_id"] != self.challenge_prefix + nonce or row["device_id"] != device_id
                or row["action_digest"] != digest or cp.core_instance_id != self.core_instance_id
                or cp.attest_verifier is None):
            return None
        verified = self._device(device_id, row["principal"])
        if verified is None:
            return None
        raw = self._binding(verified, nonce, digest)
        raw_hash = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(raw_hash, row["payload_sha256"]):
            return None
        try:
            cp.attest_verifier.verify_assertion(assertion=assertion,
                client_data_hash=self.assertion_hash(raw), public_key_x963=verified[3],
                # The existing decision path owns this counter. A fresh task assertion
                # must exceed that floor; the durable single-use nonce owns task replay.
                prev_counter=int(verified[0]["app_attest_counter"] or 0))
        except Exception:  # verifier failures reveal no credential detail to the client
            return None
        if row["expires_at"] <= time.time():
            return None
        if self.store._cas_consume_challenge(nonce, self.challenge_prefix + nonce, device_id,
                row["principal"], digest, raw_hash) != "ok":
            return None
        self.store._audit(self.audit_prefix + "_proved", device_id=device_id)
        return TaskStartActor(row["principal"], device_id, nonce, digest, self.core_instance_id)
