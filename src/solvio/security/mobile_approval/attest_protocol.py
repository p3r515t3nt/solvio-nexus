"""SOLVIO App Attest binding protocol V1 (STEP S2A.1). Shared Mac<->iOS contract.

Two `clientDataHash` constructions, each DOMAIN-SEPARATED and computed over EXACT canonical
bytes (the sender hashes the exact bytes it will attest/assert; the receiver rebuilds the
same bytes and hashes them — no cross-language canonicalisation dependency at verify time):

  ENROLLMENT binding — ties the Apple App Attest *attestation* to the SOLVIO enrollment
  context: core instance, principal, device, enrollment id, the server attestation nonce,
  AND the Face-ID approval public key (key_id + full sha256). Because the approval-key
  fingerprint is inside the attested clientDataHash, a valid attestation can NEVER be
  re-bound to a different approval key (STEP S2A.1 Phase 4).

  DECISION binding — ties an App Attest *assertion* to the EXACT approval decision: the
  sha256 of the signed decision bytes, the challenge nonce, and the approval public key
  sha256. Two proofs (Face-ID approval signature + App Attest assertion) must agree on the
  same decision, or the Mac rejects (Phase 11).

    clientDataHash = SHA256( domain_separator || 0x00 || canonical_bytes )

App Attest is APP/DEVICE INTEGRITY, never user authority. The approval key stays the only
approval authority; this binding just proves the assertion came from the same attested app
instance that enrolled the approval key.
"""
from __future__ import annotations

import hashlib

from solvio.security.mobile_approval import protocol as P

BINDING_PROTOCOL_VERSION = 1
DOMAIN_ENROLLMENT = b"SOLVIO_APP_ATTEST_ENROLLMENT_V1"
DOMAIN_DECISION = b"SOLVIO_APP_ATTEST_DECISION_V1"

TYPE_ENROLLMENT_BINDING = "app_attest_enrollment_binding"
TYPE_DECISION_BINDING = "app_attest_decision_binding"


def _require(payload: dict, fields: dict) -> None:
    extra = set(payload) - set(fields)
    if extra:
        raise P.ProtocolError(f"unknown fields: {sorted(extra)}")
    for name, typ in fields.items():
        if name not in payload:
            raise P.ProtocolError(f"missing field: {name}")
        if not isinstance(payload[name], typ):
            raise P.ProtocolError(f"field {name} wrong type")


def _domain_hash(domain: bytes, raw: bytes) -> bytes:
    return hashlib.sha256(domain + b"\x00" + raw).digest()


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ---- enrollment binding (attestKey clientDataHash) ------------------------
_ENROLL_FIELDS = {
    "protocol_version": int, "type": str, "core_instance_id": str, "principal_id": str,
    "device_id": str, "enrollment_id": str, "approval_key_id": str,
    "approval_public_key_sha256": str, "attestation_nonce": str,
    "issued_at": (int, float), "expires_at": (int, float),
}


def build_enrollment_binding(*, core_instance_id, principal_id, device_id, enrollment_id,
                             approval_key_id, approval_public_key_sha256, attestation_nonce,
                             issued_at, expires_at) -> dict:
    return {
        "protocol_version": BINDING_PROTOCOL_VERSION, "type": TYPE_ENROLLMENT_BINDING,
        "core_instance_id": core_instance_id, "principal_id": principal_id,
        "device_id": device_id, "enrollment_id": enrollment_id,
        "approval_key_id": approval_key_id,
        "approval_public_key_sha256": approval_public_key_sha256,
        "attestation_nonce": attestation_nonce, "issued_at": issued_at,
        "expires_at": expires_at,
    }


def validate_enrollment_binding(payload: dict) -> None:
    _require(payload, _ENROLL_FIELDS)
    if payload["protocol_version"] != BINDING_PROTOCOL_VERSION:
        raise P.ProtocolError("unsupported binding protocol_version")
    if payload["type"] != TYPE_ENROLLMENT_BINDING:
        raise P.ProtocolError("not an enrollment binding")


def enrollment_client_data_hash(binding_raw: bytes) -> bytes:
    """clientDataHash for `attestKey()`, over the EXACT canonical binding bytes."""
    return _domain_hash(DOMAIN_ENROLLMENT, binding_raw)


# ---- decision binding (generateAssertion clientDataHash) ------------------
_DECISION_FIELDS = {
    "protocol_version": int, "type": str, "core_instance_id": str, "approval_id": str,
    "device_id": str, "decision_sha256": str, "challenge_nonce": str,
    "approval_public_key_sha256": str,
}


def build_decision_binding(*, core_instance_id, approval_id, device_id, decision_sha256,
                           challenge_nonce, approval_public_key_sha256) -> dict:
    return {
        "protocol_version": BINDING_PROTOCOL_VERSION, "type": TYPE_DECISION_BINDING,
        "core_instance_id": core_instance_id, "approval_id": approval_id,
        "device_id": device_id, "decision_sha256": decision_sha256,
        "challenge_nonce": challenge_nonce,
        "approval_public_key_sha256": approval_public_key_sha256,
    }


def validate_decision_binding(payload: dict) -> None:
    _require(payload, _DECISION_FIELDS)
    if payload["protocol_version"] != BINDING_PROTOCOL_VERSION:
        raise P.ProtocolError("unsupported binding protocol_version")
    if payload["type"] != TYPE_DECISION_BINDING:
        raise P.ProtocolError("not a decision binding")


def decision_client_data_hash(binding_raw: bytes) -> bytes:
    """clientDataHash for `generateAssertion()`, over the EXACT canonical binding bytes."""
    return _domain_hash(DOMAIN_DECISION, binding_raw)
