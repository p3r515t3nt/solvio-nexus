"""Apple App Attest verifier tests (STEP S2A.1). Direct: python test_app_attest.py.

We cannot mint real Apple signatures, so we build a SYNTHETIC but structurally-valid
attestation (test CA hierarchy + nonce extension + authData + CBOR) and an assertion, then
run the REAL AppleAppAttestVerifier against a TEST root anchor. This exercises every check
(chain, nonce binding, key_id/pubkey hash, rpIdHash, counter, AAGUID, assertion signature +
strictly-increasing counter) end-to-end. Production pins the real Apple root; no accept-any.
"""
import datetime as dt
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import cbor2
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from solvio.security.mobile_approval import app_attest as AA

TEAM = "TESTTEAM99"
BUNDLE = "de.solvio.approvals"
APP_ID = f"{TEAM}.{BUNDLE}"


def _sha256(*p):
    h = hashlib.sha256()
    for x in p:
        h.update(x)
    return h.digest()


def _mk_cert(subject, issuer_name, pub, issuer_key, *, ca, extra_ext=None, sign_hash=hashes.SHA256()):
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    b = (x509.CertificateBuilder()
         .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
         .issuer_name(issuer_name)
         .public_key(pub)
         .serial_number(x509.random_serial_number())
         .not_valid_before(now - dt.timedelta(days=1))
         .not_valid_after(now + dt.timedelta(days=3650))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if extra_ext is not None:
        b = b.add_extension(extra_ext, critical=False)
    return b.sign(issuer_key, sign_hash)


def _nonce_ext(nonce: bytes):
    der = bytes([0x30, 0x24, 0xA1, 0x22, 0x04, 0x20]) + nonce   # SEQ{[1]{OCTET STRING(32)}}
    return x509.UnrecognizedExtension(x509.ObjectIdentifier(AA.NONCE_OID), der)


def _auth_data(aaguid, counter, cred_id, *, at=True, app_id=APP_ID):
    ad = _sha256(app_id.encode()) + bytes([0x40 if at else 0x00]) + counter.to_bytes(4, "big")
    if at:
        ad += aaguid + len(cred_id).to_bytes(2, "big") + cred_id + b"\xa0"
    return ad


def _mint(*, client_data_hash, aaguid=AA.AAGUID_DEVELOPMENT, counter=0,
          break_chain=False, tamper_credid=False, tamper_rpid=False):
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TEST App Attest Root")])
    root = _mk_cert("TEST App Attest Root", root_name, root_key.public_key(), root_key, ca=True)

    inter_key = ec.generate_private_key(ec.SECP256R1())
    inter_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TEST App Attest CA")])
    signer = ec.generate_private_key(ec.SECP256R1()) if break_chain else root_key
    inter = _mk_cert("TEST App Attest CA", root_name, inter_key.public_key(), signer, ca=True)

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_x963 = leaf_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    key_id = _sha256(leaf_x963)
    auth_data = _auth_data(aaguid, counter,
                           (b"\x00" * 32) if tamper_credid else key_id,
                           app_id="OTHER.bundle" if tamper_rpid else APP_ID)
    nonce = _sha256(auth_data, client_data_hash)
    leaf = _mk_cert("leaf", inter_name, leaf_key.public_key(), inter_key,
                    ca=False, extra_ext=_nonce_ext(nonce))
    att = cbor2.dumps({"fmt": "apple-appattest",
                       "attStmt": {"x5c": [leaf.public_bytes(serialization.Encoding.DER),
                                           inter.public_bytes(serialization.Encoding.DER)],
                                   "receipt": b"test-receipt"},
                       "authData": auth_data})
    root_pem = root.public_bytes(serialization.Encoding.PEM)
    import base64
    return dict(attestation=att, key_id_b64=base64.b64encode(key_id).decode(),
                root_pem=root_pem, leaf_key=leaf_key, leaf_x963=leaf_x963, auth_data=auth_data)


def _verifier(root_pem, allowed=(AA.ENV_DEVELOPMENT, AA.ENV_PRODUCTION)):
    return AA.AppleAppAttestVerifier(team_id=TEAM, bundle_id=BUNDLE,
                                     allowed_environments=allowed, root_ca_pem=root_pem)


def _reject(fn, *, contains=None):
    try:
        fn()
    except AA.AppAttestError as exc:
        if contains:
            assert contains in str(exc), f"expected {contains!r} in {exc!r}"
        return
    raise AssertionError("expected AppAttestError, got success")


def test_attestation_ok():
    cdh = os.urandom(32)
    m = _mint(client_data_hash=cdh)
    ak = _verifier(m["root_pem"]).verify_attestation(
        key_id_b64=m["key_id_b64"], attestation=m["attestation"], client_data_hash=cdh)
    assert ak.public_key_x963 == m["leaf_x963"]
    assert ak.environment == AA.ENV_DEVELOPMENT
    assert ak.counter == 0
    assert ak.receipt == b"test-receipt"


def test_attestation_wrong_client_data_hash_reject():
    m = _mint(client_data_hash=os.urandom(32))
    _reject(lambda: _verifier(m["root_pem"]).verify_attestation(
        key_id_b64=m["key_id_b64"], attestation=m["attestation"],
        client_data_hash=os.urandom(32)), contains="nonce")


def test_attestation_broken_chain_reject():
    cdh = os.urandom(32)
    m = _mint(client_data_hash=cdh, break_chain=True)
    _reject(lambda: _verifier(m["root_pem"]).verify_attestation(
        key_id_b64=m["key_id_b64"], attestation=m["attestation"], client_data_hash=cdh),
        contains="chain")


def test_attestation_wrong_keyid_reject():
    cdh = os.urandom(32)
    m = _mint(client_data_hash=cdh)
    import base64
    bad = base64.b64encode(os.urandom(32)).decode()
    _reject(lambda: _verifier(m["root_pem"]).verify_attestation(
        key_id_b64=bad, attestation=m["attestation"], client_data_hash=cdh))


def test_attestation_credid_mismatch_reject():
    cdh = os.urandom(32)
    m = _mint(client_data_hash=cdh, tamper_credid=True)
    _reject(lambda: _verifier(m["root_pem"]).verify_attestation(
        key_id_b64=m["key_id_b64"], attestation=m["attestation"], client_data_hash=cdh))


def test_attestation_rpid_mismatch_reject():
    cdh = os.urandom(32)
    m = _mint(client_data_hash=cdh, tamper_rpid=True)
    _reject(lambda: _verifier(m["root_pem"]).verify_attestation(
        key_id_b64=m["key_id_b64"], attestation=m["attestation"], client_data_hash=cdh),
        contains="rpIdHash")


def test_attestation_counter_nonzero_reject():
    cdh = os.urandom(32)
    m = _mint(client_data_hash=cdh, counter=1)
    _reject(lambda: _verifier(m["root_pem"]).verify_attestation(
        key_id_b64=m["key_id_b64"], attestation=m["attestation"], client_data_hash=cdh),
        contains="counter")


def test_attestation_production_aaguid_when_only_dev_allowed_reject():
    cdh = os.urandom(32)
    m = _mint(client_data_hash=cdh, aaguid=AA.AAGUID_PRODUCTION)
    _reject(lambda: _verifier(m["root_pem"], allowed=(AA.ENV_DEVELOPMENT,)).verify_attestation(
        key_id_b64=m["key_id_b64"], attestation=m["attestation"], client_data_hash=cdh),
        contains="not allowed")


def _assertion(leaf_key, counter, cdh, *, app_id=APP_ID, tamper_sig=False, webauthn_style=False):
    """Apple signs the NONCE = SHA256(authenticatorData || clientDataHash). `webauthn_style`
    signs the raw concatenation instead — the convention a real device does NOT use."""
    ad = _sha256(app_id.encode()) + bytes([0x00]) + counter.to_bytes(4, "big")
    signed = (ad + cdh) if webauthn_style else _sha256(ad, cdh)
    sig = leaf_key.sign(signed, ec.ECDSA(hashes.SHA256()))
    if tamper_sig:
        sig = sig[:-1] + bytes([sig[-1] ^ 1])
    return cbor2.dumps({"signature": sig, "authenticatorData": ad})


def test_assertion_uses_apple_nonce_not_webauthn_concat():
    """REGRESSION (found by the S2A.1 live on-device E2E): a real DCAppAttestService
    assertion signs SHA256(authData || clientDataHash). Verifying over the raw
    concatenation (WebAuthn style) makes every real assertion fail closed, and a
    self-consistent stub cannot catch it — so pin the convention explicitly."""
    cdh = os.urandom(32)
    m = _mint(client_data_hash=os.urandom(32))
    v = _verifier(m["root_pem"])
    apple = _assertion(m["leaf_key"], 1, cdh)
    assert v.verify_assertion(assertion=apple, client_data_hash=cdh,
                              public_key_x963=m["leaf_x963"], prev_counter=0) == 1
    wrong = _assertion(m["leaf_key"], 1, cdh, webauthn_style=True)
    _reject(lambda: v.verify_assertion(assertion=wrong, client_data_hash=cdh,
            public_key_x963=m["leaf_x963"], prev_counter=0), contains="signature")


def test_assertion_ok_and_counter_increments():
    cdh = os.urandom(32)
    m = _mint(client_data_hash=os.urandom(32))
    v = _verifier(m["root_pem"])
    a1 = _assertion(m["leaf_key"], 1, cdh)
    c1 = v.verify_assertion(assertion=a1, client_data_hash=cdh,
                            public_key_x963=m["leaf_x963"], prev_counter=0)
    assert c1 == 1
    a2 = _assertion(m["leaf_key"], 5, cdh)
    c2 = v.verify_assertion(assertion=a2, client_data_hash=cdh,
                            public_key_x963=m["leaf_x963"], prev_counter=c1)
    assert c2 == 5


def test_assertion_replay_counter_reject():
    cdh = os.urandom(32)
    m = _mint(client_data_hash=os.urandom(32))
    v = _verifier(m["root_pem"])
    a1 = _assertion(m["leaf_key"], 3, cdh)
    v.verify_assertion(assertion=a1, client_data_hash=cdh, public_key_x963=m["leaf_x963"],
                       prev_counter=0)
    _reject(lambda: v.verify_assertion(assertion=a1, client_data_hash=cdh,
            public_key_x963=m["leaf_x963"], prev_counter=3), contains="counter")


def test_assertion_bad_signature_reject():
    cdh = os.urandom(32)
    m = _mint(client_data_hash=os.urandom(32))
    v = _verifier(m["root_pem"])
    a = _assertion(m["leaf_key"], 1, cdh, tamper_sig=True)
    _reject(lambda: v.verify_assertion(assertion=a, client_data_hash=cdh,
            public_key_x963=m["leaf_x963"], prev_counter=0), contains="signature")


def test_assertion_wrong_client_data_hash_reject():
    cdh = os.urandom(32)
    m = _mint(client_data_hash=os.urandom(32))
    v = _verifier(m["root_pem"])
    a = _assertion(m["leaf_key"], 1, cdh)
    _reject(lambda: v.verify_assertion(assertion=a, client_data_hash=os.urandom(32),
            public_key_x963=m["leaf_x963"], prev_counter=0), contains="signature")


def test_real_apple_root_loads():
    # P1A/F6: the environment policy is now a REQUIRED argument (no permissive default).
    v = AA.AppleAppAttestVerifier(
        team_id="WQ8CG7R53R", bundle_id="de.solvio.approvals",
        allowed_environments=AA.allowed_environments_for_mode(AA.RUNTIME_PRODUCTION))
    assert v.root.subject.rfc4514_string().count("Apple App Attestation Root CA") == 1


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
