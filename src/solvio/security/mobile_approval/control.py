"""Mobile approval control-plane orchestration (STEP S2A + S2A.1). MAC ONLY, authoritative.

Ties the durable store + P-256 crypto + protocol + Mac identity + Apple App Attest together.
The Mac signs challenges; the iPhone (Secure Enclave, after Face ID) signs decisions; the
Mac verifies EVERYTHING before an APPROVED state, then the executor claims exactly the
stored action.

TWO KEYS (STEP S2A.1), never conflated:
- APPROVAL key: the Face-ID Secure-Enclave P-256 key. Its signature over the exact decision
  bytes is the ONLY approval authority (human authority).
- APP ATTEST key: a DCAppAttestService key attested by Apple. Proves a legitimate SOLVIO app
  instance + app/device integrity. It is bound to the approval key at enrollment (the
  approval-key fingerprint is inside the attested clientDataHash) and must produce a fresh
  assertion for every APPROVE. App Attest is NEVER user authority.

Enrollment is two-step and fail-closed: begin_enrollment (device -> PENDING_ATTESTATION,
issues a one-time attestation nonce + the exact binding) then complete_attestation (Apple
attestation verified -> ATTESTED + ACTIVE). A device can neither read nor approve until
ATTESTED. Legacy devices migrate to UNATTESTED and must re-enroll.

Authority separation: the language model has NO method here. It cannot enroll, attest,
revoke, mint a nonce, set a principal, or approve.

N2 explicitly adds authenticated dashboard decisions for the two agent task starts.
They have their own session authority and never stand in for a biometric assertion.
Every other capability retains the existing mobile two-proof requirement.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time

from solvio.security.approval import action_digest as s1_action_digest
from solvio.security.mobile_approval import app_attest as AA
from solvio.security.mobile_approval import attest_protocol as AP
from solvio.security.mobile_approval import crypto
from solvio.security.mobile_approval import execution as _X
from solvio.security.mobile_approval import protocol as P
from solvio.security.mobile_approval import store as S


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


_DEVICE_ID_MAX = 128
_DEVICE_ID_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")


def canonical_device_id(value) -> str:
    """P1A.5/H4: an ALLOWLIST, because the previous blocklist was incomplete.

    The old check rejected `isspace()`, C0, DEL and U+200B–U+200F — and therefore ACCEPTED
    ten other invisible characters, including the bidi overrides U+202A–U+202E, the isolates
    U+2066+, U+FEFF, U+2060, U+00AD and most C1 controls. Ten different identifiers all
    rendered as `dev-1` in the admin table, so revoking one left the others live: exactly
    the operator ambiguity this check exists to prevent, while the documentation claimed it
    was covered.

    Enumerating invisible Unicode is a losing game, so this states what IS allowed instead.
    Every device_id in this project — fixtures, tests and the real store (`dev-<uuid>`) —
    fits. Nothing is normalised: an identifier is taken exactly as given or refused, because
    silently rewriting one would change what an existing identity means.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("device_id must be a non-empty string")
    if len(value) > _DEVICE_ID_MAX:
        raise ValueError(f"device_id longer than {_DEVICE_ID_MAX} characters")
    bad = {ch for ch in value if ch not in _DEVICE_ID_ALLOWED}
    if bad:
        shown = ", ".join(f"U+{ord(c):04X}" for c in sorted(bad)[:4])
        raise ValueError(f"device_id contains characters outside "
                         f"[A-Za-z0-9._:-]: {shown}")
    return value


def _same_app_attest_key(stored, canonical) -> bool:
    """Compare by IDENTITY, not by spelling. Stored values are canonical after the
    migration; a stale or unparseable one simply does not match."""
    if not stored:
        return False
    try:
        return AA.canonical_app_attest_key_id(stored) == canonical
    except (AA.AppAttestIdentityError, ValueError):
        return False


class MobileApprovalControlPlane:
    def __init__(self, store: S.ApprovalControlStore, mac_key, core_instance_id: str, *,
                 attest_verifier: AA.AppAttestVerifier | None = None, app_id: str | None = None,
                 challenge_ttl: float = 300.0, request_ttl: float = 600.0,
                 enroll_ttl: float = 300.0, attest_ttl: float = 300.0,
                 allowed_environments=None) -> None:
        self.store = store
        self.mac_key = mac_key
        self.core_instance_id = core_instance_id
        self.attest_verifier = attest_verifier
        self.app_id = app_id
        self.challenge_ttl = challenge_ttl
        self.request_ttl = request_ttl
        self.enroll_ttl = enroll_ttl
        self.attest_ttl = attest_ttl
        # P1A/F6: the environment policy is enforced at AUTHORITY time too, not only when
        # the attestation is verified. Defaults to the verifier's set so a mismatch is
        # impossible; pass explicitly to be unambiguous.
        if allowed_environments is None:
            allowed_environments = getattr(attest_verifier, "allowed_environments", None)
        # Falsy (None / () / [] / set()) stays falsy on purpose: _environment_blocked reads
        # that as DENY-ALL. Never widen it to "accept anything".
        self.allowed_environments = (frozenset(allowed_environments)
                                     if allowed_environments else None)

    # ---- enrollment (control-plane / out-of-band pairing) -----------------
    async def create_enrollment_token(self, principal: str):
        """Returns (plaintext_token, expires_at). Only the HASH is stored. The plaintext
        goes into the one-time pairing QR and is never logged."""
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + self.enroll_ttl
        await self.store.add_enrollment_token(_sha256_hex(token), principal, expires_at)
        return token, expires_at

    async def begin_enrollment(self, *, enrollment_token: str, device_id: str,
                               approval_public_key_x963_b64: str, app_attest_key_id: str,
                               transport_cred: str | None = None):
        """Step 1/2. Consume the one-time pairing token, register the device as
        PENDING_ATTESTATION, and return the EXACT enrollment binding the iPhone must attest
        (App Attest) — it embeds the approval-key fingerprint, so a valid attestation can
        never be re-bound to a different approval key."""
        principal, st = await self.store.consume_enrollment_token(_sha256_hex(enrollment_token))
        if principal is None:
            return None, st
        try:
            x963 = P.b64d(approval_public_key_x963_b64)
            crypto.public_key_from_x963(x963)  # validate P-256 point
        except Exception:  # noqa: BLE001
            return None, "bad_public_key"
        try:
            device_id = canonical_device_id(device_id)
        except ValueError:
            return None, "bad_device_id"
        try:
            # P1A.4/C1: identity comes from the DECODED BYTES, never from the spelling.
            app_attest_key_id = AA.canonical_app_attest_key_id(app_attest_key_id)
        except (AA.AppAttestIdentityError, ValueError):
            return None, "bad_app_attest_key_id"
        approval_key_id = crypto.key_id(x963)
        approval_pubkey_sha256 = crypto.fingerprint(x963)
        enrollment_id = "enr-" + secrets.token_hex(16)
        nonce = secrets.token_hex(32)
        issued = time.time()
        expires = issued + self.attest_ttl
        binding = AP.build_enrollment_binding(
            core_instance_id=self.core_instance_id, principal_id=principal, device_id=device_id,
            enrollment_id=enrollment_id, approval_key_id=approval_key_id,
            approval_public_key_sha256=approval_pubkey_sha256, attestation_nonce=nonce,
            issued_at=issued, expires_at=expires)
        binding_raw = P.canonical_bytes(binding)
        tch = _sha256_hex(transport_cred) if transport_cred else None
        # P1A.4/§10: the revocation check and the enrollment write are ONE transaction in
        # the store. It used to be a Python check followed by a separate write, so a revoke
        # committed by the admin CLI in between produced an ACTIVE record bound to an
        # already-revoked key. A revoked identity — device_id, approval key or app-attest
        # key — can never be enrolled again, not even under a brand-new device_id with a
        # freshly minted pairing token. F5 terminality of a REVOKED record still applies.
        # P1A.8/C1: ONE transaction — revocation check, supersede, device binding and the
        # challenge. It used to be two: device write ... commit ... challenge insert. Two
        # concurrent begins interleaved as write|write|insert|insert left BOTH challenges
        # live while the device row held only the second caller's key, and the victim's own
        # attestation then promoted the attacker's binding to ATTESTED. Reproduced.
        st_enroll = await self.store.begin_enrollment_atomic(
            device_id=device_id, key_id=approval_key_id, public_key_x963=x963.hex(),
            principal=principal, app_attest_key_id=app_attest_key_id,
            transport_cred_hash=tch, enrollment_id=enrollment_id, nonce=nonce,
            approval_pubkey_sha256=approval_pubkey_sha256, binding_raw=binding_raw,
            expires_at=expires,
            identities=[(S.REVOKE_DEVICE, device_id),
                        (S.REVOKE_APPROVAL_KEY, approval_pubkey_sha256),
                        (S.REVOKE_APP_ATTEST_KEY, app_attest_key_id)])
        if st_enroll != "ok":
            return None, st_enroll
        return {"enrollment_id": enrollment_id, "binding_b64": P.b64e(binding_raw),
                "attestation_nonce": nonce, "expires_at": expires,
                "device_id": device_id, "principal": principal}, "ok"

    async def complete_attestation(self, *, enrollment_id: str, attestation_b64: str):
        """Step 2/2. Verify the Apple App Attest attestation over the EXACT enrollment
        binding the CHALLENGE was issued for, then commit the trusted transition atomically.

        P1A.7/H1: attestation authority is never reconstructed from the mutable devices row.
        A second begin_enrollment overwrote it, and an older challenge then attested a
        binding its attestation had never proved — leaving app_attest_key_id and
        app_attest_public_key describing different keys, so revoking the key that actually
        signs every assertion matched nothing. The challenge row is authoritative, and the
        App Attest identity is DERIVED from the verified public key rather than taken from
        anyone's input.
        """
        if self.attest_verifier is None:
            return None, "no_attest_verifier"
        ch = await self.store.peek_attestation_challenge(enrollment_id)
        if ch is None:
            return None, "challenge_unknown"
        if ch["consumed"] or ch["superseded"]:
            return None, "challenge_superseded"
        if time.time() > ch["expires_at"]:
            return None, "challenge_expired"
        challenge_aakid = ch["app_attest_key_id"]
        if not challenge_aakid:
            # A pre-P1A.7 challenge carries no app-attest identity and therefore cannot be
            # bound; it must be re-issued rather than trusted.
            return None, "challenge_unbound"
        try:
            att = P.b64d(attestation_b64)
        except P.ProtocolError as exc:
            return None, f"malformed:{exc}"
        client_data_hash = AP.enrollment_client_data_hash(bytes(ch["binding_raw"]))
        try:
            attested = self.attest_verifier.verify_attestation(
                key_id_b64=challenge_aakid, attestation=att,
                client_data_hash=client_data_hash)
        except AA.AppAttestError as exc:
            await self.store.set_device_attestation_failed(
                enrollment_id=enrollment_id, device_id=ch["device_id"], reason=str(exc))
            return None, "attestation_failed"
        # P1A.7/§3: the security identity comes from the key that was actually verified.
        derived = AA.app_attest_identity_from_public_key(attested.public_key_x963)
        if derived != challenge_aakid:
            await self.store.set_device_attestation_failed(
                enrollment_id=enrollment_id, device_id=ch["device_id"],
                reason="verified key identity != challenge identity")
            return None, "app_attest_key_mismatch"

        # P1A.8/C1: only VERIFIED facts cross this boundary. Passing the challenge's own
        # fields back made the store's comparisons tautologies; it now reads the challenge
        # and the device row itself and computes both identities from key material.
        st = await self.store.finalize_attestation(
            enrollment_id=enrollment_id,
            app_attest_public_key_x963=attested.public_key_x963,
            app_attest_counter=attested.counter, environment=attested.environment,
            app_id=self.app_id or "", core_instance_id=self.core_instance_id)
        if st != "ok":
            return None, st
        return {"device_id": ch["device_id"], "environment": attested.environment,
                "approval_key_id": ch["approval_key_id"],
                "attestation_status": S.ATT_ATTESTED}, "ok"

    # ---- P1A: revocation is checked on EVERY authority path ---------------
    @staticmethod
    def _identities(dev) -> list[tuple[str, str]]:
        """Every durable identity a device record carries. Revoking ANY of them is
        terminal — a fresh device_id with the same approval key stays locked out."""
        ids: list[tuple[str, str]] = [(S.REVOKE_DEVICE, dev["device_id"])]
        pk = dev["public_key_x963"]
        if pk:
            try:
                ids.append((S.REVOKE_APPROVAL_KEY, crypto.fingerprint(bytes.fromhex(pk))))
            except ValueError:
                pass
        if dev["app_attest_key_id"]:
            ids.append((S.REVOKE_APP_ATTEST_KEY, dev["app_attest_key_id"]))
        return ids

    def _environment_blocked(self, dev) -> bool:
        """P1A.1/F2 — FAIL CLOSED in both directions.

        * No explicit policy (None / empty) means DENY-ALL, not allow-all. An empty set is
          a hard error at the verifier, so it must mean deny here too — the two must not
          disagree about what "empty" means.
        * A stored environment that is NULL / empty / unknown is never authority-bearing:
          it is simply not in the allowed set. Stored DB values are never rewritten to fake
          a promotion; such a device must RE-ENROLL / RE-ATTEST.
        """
        if not self.allowed_environments:
            return True
        return dev["environment"] not in self.allowed_environments

    async def _revoked(self, dev) -> str | None:
        """Returns the revoked identity kind, or None. Checked at enrollment, attestation,
        transport auth, challenge issue AND decision submit — so a challenge issued before
        a revoke is still refused when it comes back."""
        return await self.store.revoked_identities(self._identities(dev))

    async def execution_preflight(self, approval_id: str, device_id: str):
        """P1C: the pre-transaction checks and the identity list, shared by both entry points.

        Returns (identities, None) or (None, reason). The identities come from the STORED
        device row — never from caller-supplied data — so a stale in-memory approval object
        cannot dodge the revocation predicate that the claiming UPDATE applies.
        """
        async def deny(reason: str):
            # EVERY rejection is audited — a refused execution is exactly the event an
            # operator needs to see after revoking a device.
            await self.store.audit("execution_rejected", approval_id=approval_id,
                                   device_id=device_id or None, reason=reason)
            return None, reason

        if not device_id:
            return await deny("unknown_device")
        dev = await self.store.get_device(device_id)
        if dev is None:
            return await deny("unknown_device")
        if dev["status"] != S.DEVICE_ACTIVE:
            return await deny("device_revoked")
        if self._environment_blocked(dev):
            return await deny("device_environment_not_allowed")
        return self._identities(dev), None

    async def submit_dashboard_decision(self, *, actor, approval_id: str,
                                         action_digest: str, decision: str):
        """Trusted HTTP service passes its verified BrowserActor, never request identity.

        Authentication/CSRF belongs to BrowserSessionService. The store rechecks the
        actual session, current request and exact displayed digest atomically.
        """
        from .browser_sessions import BrowserActor
        if type(actor) is not BrowserActor:
            return None, "browser_actor_required"
        if decision not in ("APPROVE", "DENY"):
            return None, "invalid_decision"
        if not isinstance(action_digest, str) or len(action_digest) != 64:
            return None, "action_digest_mismatch"
        status = await self.store.commit_dashboard_decision(
            approval_id=approval_id, session_id=actor.session_id, principal=actor.principal,
            core_instance_id=self.core_instance_id, action_digest=action_digest,
            decision=decision)
        if status != "ok":
            return None, status
        return {"approval_id": approval_id, "decision": decision,
                "method": S.DASHBOARD_METHOD}, "ok"

    async def prepare_browser_memory_command(self, *, actor, client_request_id,
                                              tool, mode, task):
        """N5: exact authenticated command, using the existing display/digest contract."""
        import hashlib
        import json
        import re
        from .browser_sessions import BrowserActor
        if type(actor) is not BrowserActor:
            return None, "browser_actor_required"
        if not isinstance(client_request_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", client_request_id):
            return None, "invalid_command_id"
        if tool not in S.MEMORY_COMMAND_TOOLS or mode != "controlled-v1":
            return None, "memory_command_binding_invalid"
        P.validate_action_display_fields(tool_id=tool, mode=mode, task=task,
            workspace="", human_summary="Bewusster Gedächtnisbefehl im Dashboard")
        # Arguments deliberately excluded: changed content under the same ID is
        # a conflict, not a new executable command.
        key = json.dumps(["solvio.browser.memory.v1", self.core_instance_id,
                          actor.principal, client_request_id], separators=(",", ":"))
        approval_id = "bm-" + hashlib.sha256(key.encode()).hexdigest()
        digest = s1_action_digest(tool_id=tool, mode=mode, task=task, workspace="")
        status = await self.store.prepare_browser_memory_command(approval_id=approval_id,
            session_id=actor.session_id, principal=actor.principal,
            core_instance_id=self.core_instance_id, tool=tool, mode=mode, task=task,
            action_digest=digest, expires_at=time.time() + self.request_ttl)
        return (approval_id if status in {"ok", "existing"} else None), status

    async def task_authorization_receipt(self, approval_id: str):
        """Core-only historical receipt; no principal/method asserted by a caller."""
        return await self.store.task_authorization_receipt(approval_id, self.core_instance_id)

    async def task_start_claim_receipt(self, approval_id: str, execution_id: str):
        """Core executor only: authorize this task's creation, without claiming success."""
        return await self.store.task_start_claim_receipt(
            approval_id, execution_id, self.core_instance_id, self.allowed_environments)

    async def claim_execution(self, approval_id: str, device_id: str) -> str | None:
        """P1A.1/F3: authoritative, server-side gate immediately before the executor hand-off.

        Identities come from the STORED device row — never from caller-supplied data — so a
        stale in-memory approval object cannot dodge it. The environment policy is static
        config and is checked here; device status and every revocation kind are re-checked
        INSIDE the claiming UPDATE, so a revoke racing in from the admin CLI still wins.
        Returns a reason string when execution must not start, else None.

        P1C/F4: this now goes through the SAME journalled claim the coordinator uses. It was
        the only other way to reach EXECUTING, and a claim without a journal entry is exactly
        the state P1C exists to abolish — a row that says "executing" while nothing records
        what was about to run or whether it started.
        """
        identities, reason = await self.execution_preflight(approval_id, device_id)
        if identities is None:
            return reason
        req = await self.store.get_request(approval_id)
        capability = req["tool"] if req else ""
        execution_id = _X.execution_id_for(self.core_instance_id, approval_id)
        attempt_id, st = await self.store.claim_execution_attempt(
            approval_id=approval_id, device_id=device_id, identities=identities,
            execution_id=execution_id, capability=capability,
            semantics=_X.semantics_for(capability),
            idempotency_key=_X.idempotency_key_for(execution_id, capability),
            owner=f"pid-{os.getpid()}", core_instance_id=self.core_instance_id)
        if attempt_id is None:
            if st == "claim_rejected":
                # lost the race against a revoke committed by another connection
                await self.store.audit("execution_rejected", approval_id=approval_id,
                                       device_id=device_id, reason="device_revoked")
                return "device_revoked"
            return st
        return None

    async def verify_transport_cred(self, device_id: str, cred: str) -> bool:
        dev = await self.store.get_device(device_id)
        if (dev is None or dev["status"] != S.DEVICE_ACTIVE
                or dev["attestation_status"] != S.ATT_ATTESTED
                or not dev["transport_cred_hash"]):
            return False
        if await self._revoked(dev) or self._environment_blocked(dev):
            return False
        return secrets.compare_digest(dev["transport_cred_hash"], _sha256_hex(cred or ""))

    # ---- revocation (trusted control plane only; never LLM-reachable) -----
    async def revoke_device(self, device_id: str, *, reason: str | None = None):
        """P1A.4/H2: revoking a device means the physical trusted approver is no longer
        trusted, so its BOUND KEY MATERIAL goes with it.

        Before this, only `kind=device` was recorded. The same Secure Enclave approval key
        and the same App Attest key then re-paired under a fresh device_id and regained full
        execution authority — while the CLI printed "terminal". Now the device_id, the
        approval-key identity and the app-attest-key identity are revoked in ONE
        transaction, which also kills every other record built on that key material.

        If the record carries no key material (unknown/never-enrolled device_id) only the
        device_id is recorded, and the CLI says so rather than claiming more.

        P1A.6/§11+§12 — EXACT SCOPE. Revoked are the device_id, the CURRENT binding, and
        every binding recorded in `device_identity_history` for this device_id. History is
        written when an attestation succeeds, so it covers key rotations from that point on.
        It does NOT cover bindings that were trusted BEFORE the history table existed:
        those rows are simply not in the database and are deliberately not reconstructed —
        `attestation_challenges` records enrollment ATTEMPTS (its `consumed` flag is set
        before verification, so it cannot distinguish success from failure) and carries no
        app-attest identity at all, so any backfill from it would be invention. For such a
        device the guarantee is the current binding plus whatever history exists, and
        nothing more is claimed.
        """
        def plan(rows, history):
            # Runs INSIDE the write transaction: the record is read, its bound key material
            # determined and every record built on that material selected, all under the
            # same lock. Reading it beforehand would let a re-enrollment slip in between.
            by_id = {r["device_id"]: r for r in rows}
            entries = [(S.REVOKE_DEVICE, device_id)]
            target = by_id.get(device_id)
            if target is not None:
                entries += [(k, v) for k, v in self._identities(target)
                            if k != S.REVOKE_DEVICE and v]
            # P1A.6/§11: every binding this device_id was EVER trusted with, not just the
            # current one. A legitimate key rotation used to orphan the previous approval
            # key — revoke-device could not see it and it stayed enrollable under a fresh
            # device_id. History is read inside this transaction, like everything else here.
            for h in history:
                if h["device_id"] != device_id:
                    continue
                if h["approval_key_sha256"]:
                    entries.append((S.REVOKE_APPROVAL_KEY, h["approval_key_sha256"]))
                if h["app_attest_key_id"]:
                    entries.append((S.REVOKE_APP_ATTEST_KEY, h["app_attest_key_id"]))
            seen = set()
            entries = [e for e in entries if not (e in seen or seen.add(e))]
            values = {v for _, v in entries}
            doomed = [r["device_id"] for r in rows
                      if r["status"] != S.DEVICE_REVOKED
                      and any(v in values for _, v in self._identities(r))]
            return entries, doomed

        return await self.store.revoke_identities_planned(
            plan=plan, reason=reason, device_id=device_id, operation="revoke-device")

    @staticmethod
    def _approval_fingerprint(row) -> str | None:
        pk = row["public_key_x963"]
        if not pk:
            return None
        try:
            return crypto.fingerprint(bytes.fromhex(pk))
        except ValueError:
            return None

    async def revoke_approval_key(self, approval_pubkey_sha256: str, *,
                                  reason: str | None = None):
        """Revoke an approval KEY across every device_id it was ever bound to — this is what
        stops a physical device re-pairing under a fresh device_id. Terminal.

        P1A.3: atomic. The fingerprint is derived in Python from the stored public key rather
        than being a column, so the read, the match and the writes all happen inside ONE
        transaction whose write lock is already held — no schema migration needed, and no
        window in which the key is revoked while a bound device is still ACTIVE."""
        return await self.store.revoke_identities(
            entries=[(S.REVOKE_APPROVAL_KEY, approval_pubkey_sha256)], reason=reason,
            operation="revoke-approval-key",
            match=lambda row: self._approval_fingerprint(row) == approval_pubkey_sha256)

    async def revoke_app_attest_key(self, app_attest_key_id: str, *,
                                    reason: str | None = None):
        """P1A.2 guarantee, now carried by the shared P1A.3 primitive: the durable revocation
        and every bound device record land in one transaction. The `revocations` table stays
        the canonical identity blocklist — authority paths consult it, they do NOT rely on
        devices.status."""
        canonical = AA.canonical_app_attest_key_id(app_attest_key_id)
        return await self.store.revoke_identities(
            entries=[(S.REVOKE_APP_ATTEST_KEY, canonical)], reason=reason,
            operation="revoke-app-attest-key",
            match=lambda row: _same_app_attest_key(row["app_attest_key_id"], canonical))

    async def list_revocations(self):
        return await self.store.list_revocations()

    # ---- request (created by the control plane from a codex request) ------
    async def create_request(self, *, principal: str, tool: str, mode: str, task: str,
                             workspace: str, human_summary: str) -> str:
        # Display-safety is enforced HERE so a poisoned request can never be stored: the
        # requester is the untrusted model. Raises ProtocolError -> fail closed.
        P.validate_action_display_fields(tool_id=tool, mode=mode, task=task,
                                         workspace=workspace, human_summary=human_summary)
        digest = s1_action_digest(tool_id=tool, mode=mode, task=task, workspace=workspace)
        approval_id = "ap-" + secrets.token_hex(16)
        await self.store.create_request(
            approval_id=approval_id, principal=principal, tool=tool, mode=mode, task=task,
            workspace=workspace, action_digest=digest, human_summary=human_summary,
            expires_at=time.time() + self.request_ttl, core_instance_id=self.core_instance_id)
        return approval_id

    # ---- challenge (Mac-signed) -------------------------------------------
    async def issue_challenge(self, *, approval_id: str, device_id: str):
        req = await self.store.get_request(approval_id)
        if req is None or req["state"] != S.PENDING:
            return None, "not_pending"
        if time.time() > req["expires_at"]:
            return None, "expired"
        dev = await self.store.get_device(device_id)
        if (dev is None or dev["status"] != S.DEVICE_ACTIVE
                or dev["attestation_status"] != S.ATT_ATTESTED):
            # P1A.2: a revoked device gets the SAME vague answer as any other inactive one —
            # an untrusted client must not learn why it was cut off — but the server-side
            # audit has to record the real reason. Before this, the generic short-circuit
            # swallowed `challenge_rejected_revoked_key` entirely, because every revoke kind
            # now also flips devices.status.
            if dev is not None:
                rk = await self._revoked(dev)
                if rk:
                    await self.store.audit("challenge_rejected_revoked_key",
                                           device_id=device_id, approval_id=approval_id,
                                           reason=rk)
            return None, "device_inactive"
        rk = await self._revoked(dev)
        if rk:
            await self.store.audit("challenge_rejected_revoked_key", device_id=device_id,
                                   approval_id=approval_id, reason=rk)
            return None, "device_revoked"
        if self._environment_blocked(dev):
            await self.store.audit("challenge_rejected_environment", device_id=device_id,
                                   approval_id=approval_id, reason=dev["environment"])
            return None, "device_environment_not_allowed"
        if dev["principal"] != req["principal"]:
            return None, "principal_mismatch"
        nonce = secrets.token_hex(32)
        issued = time.time()
        expires = issued + self.challenge_ttl
        # V2: the signed challenge carries the EXACT stored task (the thing the human
        # actually authorizes) and the device it was issued to.
        payload = P.build_challenge_payload(
            core_instance_id=self.core_instance_id, approval_id=approval_id,
            action_digest=req["action_digest"], principal_id=req["principal"],
            device_id=device_id, tool_id=req["tool"], mode=req["mode"],
            workspace=req["workspace"], task=req["task"],
            human_summary=req["human_summary"], challenge_nonce=nonce,
            issued_at=issued, expires_at=expires)
        raw = P.canonical_bytes(payload)
        # Bind the decision to THESE exact display bytes (checked in submit_decision).
        payload_sha256 = hashlib.sha256(raw).hexdigest()
        await self.store.add_challenge(
            challenge_nonce=nonce, approval_id=approval_id, device_id=device_id,
            principal=req["principal"], action_digest=req["action_digest"],
            expires_at=expires, payload_sha256=payload_sha256)
        return {"payload_b64": P.b64e(raw),
                "signature_b64": P.b64e(self.mac_key.sign(raw)),
                "key_id": self.mac_key.key_id}, "ok"

    # ---- decision (iPhone-signed; Mac verifies EVERYTHING) ----------------
    async def submit_decision(self, *, payload_b64: str, signature_b64: str, key_id: str,
                              assertion_b64: str | None = None):
        try:
            raw = P.b64d(payload_b64)
            payload = P.strict_parse(raw)
            P.validate_decision_payload(payload)
            sig = P.b64d(signature_b64)
        except P.ProtocolError as exc:
            return None, f"malformed:{exc}"
        if payload["core_instance_id"] != self.core_instance_id:
            return None, "wrong_core_instance"
        dev = await self.store.get_device(payload["device_id"])
        if dev is None:
            return None, "unknown_device"
        if dev["status"] != S.DEVICE_ACTIVE:
            # P1A.2: same reason as in issue_challenge — the audit must not go quiet just
            # because the status column already carries the revoke.
            rk = await self._revoked(dev)
            await self.store.audit("decision_rejected_revoked_key",
                                   device_id=dev["device_id"],
                                   reason=rk or dev["status"])
            return None, "device_revoked"
        # P1A: also refuse a challenge that was issued BEFORE the revoke landed.
        rk = await self._revoked(dev)
        if rk:
            await self.store.audit("decision_rejected_revoked_key",
                                   device_id=dev["device_id"], reason=rk)
            return None, "device_revoked"
        if self._environment_blocked(dev):
            await self.store.audit("decision_rejected_environment",
                                   device_id=dev["device_id"], reason=dev["environment"])
            return None, "device_environment_not_allowed"
        if dev["attestation_status"] != S.ATT_ATTESTED:
            return None, "device_not_attested"
        if key_id != dev["key_id"] or payload["key_id"] != dev["key_id"]:
            return None, "key_mismatch"
        pub = crypto.public_key_from_x963(bytes.fromhex(dev["public_key_x963"]))
        if not crypto.verify(pub, sig, raw):  # PROOF A: approval-key signature over EXACT bytes
            return None, "bad_signature"
        req = await self.store.get_request(payload["approval_id"])
        if req is None:
            return None, "unknown_request"
        if req["state"] != S.PENDING:
            return None, "not_pending"
        if payload["principal_id"] != req["principal"] or dev["principal"] != req["principal"]:
            return None, "principal_mismatch"
        if payload["action_digest"] != req["action_digest"]:
            return None, "action_digest_mismatch"

        # PROOF B (APPROVE only): a fresh App Attest assertion over the EXACT decision binding.
        new_counter = None
        if payload["decision"] == P.DECISION_APPROVE:
            if self.attest_verifier is None:
                return None, "no_attest_verifier"
            if not assertion_b64:
                return None, "assertion_required"
            if not dev["app_attest_public_key"]:
                return None, "device_not_attested"
            try:
                assertion = P.b64d(assertion_b64)
            except P.ProtocolError as exc:
                return None, f"malformed:{exc}"
            decision_binding = AP.build_decision_binding(
                core_instance_id=self.core_instance_id, approval_id=req["approval_id"],
                device_id=payload["device_id"], decision_sha256=AP.sha256_hex(raw),
                challenge_nonce=payload["challenge_nonce"],
                approval_public_key_sha256=crypto.fingerprint(bytes.fromhex(dev["public_key_x963"])))
            cdh = AP.decision_client_data_hash(P.canonical_bytes(decision_binding))
            try:
                new_counter = self.attest_verifier.verify_assertion(
                    assertion=assertion, client_data_hash=cdh,
                    public_key_x963=bytes.fromhex(dev["app_attest_public_key"]),
                    prev_counter=dev["app_attest_counter"])
            except AA.AppAttestError:
                return None, "bad_assertion"

        # single-use nonce (binds approval_id/device/principal/action_digest AND the exact
        # signed challenge bytes the phone displayed); burned last so a failed second proof
        # does not consume the challenge. V2: challenge_payload_sha256 proves the decision
        # belongs to the precise display context the human saw -> closes F1.
        # P1B/F9: ONE atomic authority operation. Everything above is a cheap early
        # rejection and the cryptographic verification; nothing above is the decision. The
        # store re-reads the request and the device under the write lock, re-checks
        # revocation, environment, attestation and the enrolment generation there, burns the
        # nonce and swaps the state — together or not at all.
        #
        # It used to be four separate autocommits. Reproduced: a `revoke-device` committed by
        # a second connection between the revocation check and the transition still produced
        # an APPROVED request bound to a REVOKED device. And a concurrent replay of the same
        # signed payload raised IllegalTransition out of this function — an HTTP 500 whose
        # loser had already burned the nonce, so an honest retry then failed as a replay.
        approve = payload["decision"] == P.DECISION_APPROVE
        st = await self.store.commit_decision(
            approval_id=req["approval_id"], device_id=payload["device_id"],
            new_state=S.APPROVED if approve else S.DENIED,
            challenge_nonce=payload["challenge_nonce"],
            challenge_payload_sha256=payload["challenge_payload_sha256"],
            verified_public_key_x963=bytes.fromhex(dev["public_key_x963"]),
            verified_app_attest_public_key=(
                bytes.fromhex(dev["app_attest_public_key"]) if approve else None),
            app_attest_counter=new_counter if approve else None,
            allowed_environments=self.allowed_environments)
        if st != "ok":
            return None, st
        return {"approval_id": req["approval_id"],
                "decision": "APPROVE" if approve else "DENY"}, "ok"

    # P1A/F3: `claim_approved` was REMOVED. It handed out the stored action on durable
    # state alone, without the S1 broker / MobileApprover trust check — a second execution
    # path contradicting "the S1 broker is the last execution boundary". It had no
    # production caller. The only supported route is
    # MobileApprovalCoordinator.execute_approved(), which is S1-gated and therefore
    # fail-closed after a restart. Invariant: NO TRUSTED MOBILE DECISION -> NO S1 APPROVAL
    # -> NO EXECUTION.

    # HYGIENE/H3: `mark_consumed` and `mark_failed` are GONE. They drove an approval to a
    # TERMINAL state directly, outside the execution journal, and had zero callers since P1C
    # moved the outcome into `finish_execution_attempt` — which records WHAT happened to the
    # external effect in the same transaction as the state change. A terminal setter that
    # knows nothing about the attempt can only guess, and a guess here is the P1C defect.
