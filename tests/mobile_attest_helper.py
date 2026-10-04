"""Shared helpers for the attested (S2A.1) mobile-approval test flow.

Software P-256 keys stand in for the iPhone Secure-Enclave approval key and the Apple App
Attest key; the FakeAppAttestVerifier skips only the Apple certificate chain (covered by
test_app_attest.py) while still binding real ECDSA signatures to the clientDataHash + a
strictly-increasing counter. Used by the wiring/gateway/control regression tests so they run
against the real two-proof control-plane.
"""
import base64
import _guard  # noqa: F401  (P1A.4/C2: refuses to load under python -O)
import hashlib
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from solvio.security.mobile_approval import app_attest as AA
from solvio.security.mobile_approval import attest_protocol as AP
from solvio.security.mobile_approval import crypto
from solvio.security.mobile_approval import protocol as P


def fake_verifier():
    return AA.FakeAppAttestVerifier(environment=AA.ENV_DEVELOPMENT)


def aa_key():
    k = ec.generate_private_key(ec.SECP256R1())
    x963 = k.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    kid = base64.b64encode(hashlib.sha256(x963).digest()).decode()
    return k, x963, kid


class DeviceCtx:
    def __init__(self, appr, appr_x963, aakey, aakey_x963, aakid, device_id):
        self.appr = appr
        self.appr_x963 = appr_x963
        self.aakey = aakey
        self.aakey_x963 = aakey_x963
        self.aakid = aakid
        self.device_id = device_id
        self.key_id = crypto.key_id(appr_x963)


def new_device(device_id="dev-1"):
    appr = crypto.generate_private_key()
    appr_x963 = crypto.public_key_x963(appr)
    aakey, aakey_x963, aakid = aa_key()
    return DeviceCtx(appr, appr_x963, aakey, aakey_x963, aakid, device_id)


async def store_begin(st, ctx, *, device_id=None, principal="local-owner",
                      identities=None, transport_cred_hash=None, enrollment_id=None):
    """Drive the store's ONE enrollment writer (P1A.8: begin_enrollment_atomic).

    The split `begin_device_enrollment` + `add_attestation_challenge` pair is gone — it was
    the C1 authority-bypass window — so tests that need a store-level enrolment supply the
    challenge fields the single transaction now writes.
    """
    import secrets
    from solvio.security.mobile_approval import store as S
    device_id = device_id or ctx.device_id
    fp = crypto.fingerprint(ctx.appr_x963)
    if identities is None:
        identities = [(S.REVOKE_DEVICE, device_id), (S.REVOKE_APPROVAL_KEY, fp),
                      (S.REVOKE_APP_ATTEST_KEY, ctx.aakid)]
    return await st.begin_enrollment_atomic(
        device_id=device_id, key_id=ctx.key_id, public_key_x963=ctx.appr_x963.hex(),
        principal=principal, app_attest_key_id=ctx.aakid,
        transport_cred_hash=transport_cred_hash, identities=identities,
        enrollment_id=enrollment_id or ("enr-" + secrets.token_hex(16)),
        nonce=secrets.token_hex(32), approval_pubkey_sha256=fp,
        binding_raw=b"{}", expires_at=time.time() + 300)


async def enroll_attested(cp, *, device_id="dev-1", principal="local-owner", transport_cred=None):
    """Two-step attested enrollment (begin + Fake App Attest complete). Returns a DeviceCtx."""
    ctx = new_device(device_id)
    token, _ = await cp.create_enrollment_token(principal)
    res, st = await cp.begin_enrollment(
        enrollment_token=token, device_id=device_id,
        approval_public_key_x963_b64=P.b64e(ctx.appr_x963), app_attest_key_id=ctx.aakid,
        transport_cred=transport_cred)
    assert st == "ok", st
    r2, s2 = await cp.complete_attestation(
        enrollment_id=res["enrollment_id"],
        attestation_b64=P.b64e(AA.fake_attestation(ctx.aakey_x963)))
    assert s2 == "ok", s2
    return ctx


def sign_decision(ctx, ch_raw, *, decision=P.DECISION_APPROVE, counter=1,
                  with_assertion=True, payload_sha256=None):
    """Build {payload_b64, signature_b64, key_id[, assertion_b64]} from the EXACT signed
    challenge bytes `ch_raw` (V2 binds their SHA-256). APPROVE includes a Fake App Attest
    assertion over the exact decision binding unless with_assertion=False.
    `payload_sha256` overrides the display binding for negative tests."""
    ch = P.strict_parse(ch_raw)
    payload = P.build_decision_payload(
        core_instance_id=ch["core_instance_id"], approval_id=ch["approval_id"],
        action_digest=ch["action_digest"], principal_id=ch["principal_id"],
        device_id=ctx.device_id, key_id=ctx.key_id, challenge_nonce=ch["challenge_nonce"],
        challenge_payload_sha256=(payload_sha256 or hashlib.sha256(ch_raw).hexdigest()),
        decision=decision, issued_at=int(time.time()), challenge_expires_at=ch["expires_at"])
    raw = P.canonical_bytes(payload)
    wire = {"payload_b64": P.b64e(raw), "signature_b64": P.b64e(crypto.sign(ctx.appr, raw)),
            "key_id": ctx.key_id}
    if decision == P.DECISION_APPROVE and with_assertion:
        db = AP.build_decision_binding(
            core_instance_id=ch["core_instance_id"], approval_id=ch["approval_id"],
            device_id=ctx.device_id, decision_sha256=AP.sha256_hex(raw),
            challenge_nonce=ch["challenge_nonce"],
            approval_public_key_sha256=crypto.fingerprint(ctx.appr_x963))
        cdh = AP.decision_client_data_hash(P.canonical_bytes(db))
        wire["assertion_b64"] = P.b64e(AA.fake_assertion(ctx.aakey, cdh, counter))
    return wire


async def decide(cp, ctx, approval_id, *, decision=P.DECISION_APPROVE, counter=1,
                 with_assertion=True, payload_sha256=None):
    """issue_challenge + sign_decision + submit_decision. Returns (res, status)."""
    wire, s = await cp.issue_challenge(approval_id=approval_id, device_id=ctx.device_id)
    if wire is None:
        return None, "challenge_" + s
    ch_raw = P.b64d(wire["payload_b64"])
    return await cp.submit_decision(**sign_decision(
        ctx, ch_raw, decision=decision, counter=counter,
        with_assertion=with_assertion, payload_sha256=payload_sha256))
