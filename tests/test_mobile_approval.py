"""SOLVIO Mobile Approval Protocol V1 — crypto + protocol tests (STEP S2A).

No network, no Secure Enclave (a software P-256 key stands in for the iPhone SE key in
these Mac-side tests; the on-device SE/Face-ID authority is proven separately on hardware).
Direct: python test_mobile_approval.py."""
import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

from solvio.security.mobile_approval import crypto, identity, protocol as P


def test_sign_verify_roundtrip():
    k = crypto.generate_private_key()
    data = b"exact-bytes-\x00\x1f\xf0\x9f"
    sig = crypto.sign(k, data)
    assert crypto.verify(k.public_key(), sig, data) is True


def test_verify_rejects_tamper():
    k = crypto.generate_private_key()
    sig = crypto.sign(k, b"hello")
    assert crypto.verify(k.public_key(), sig, b"hell0") is False       # data tampered
    assert crypto.verify(k.public_key(), sig[:-1] + bytes([sig[-1] ^ 1]), b"hello") is False
    other = crypto.generate_private_key()
    assert crypto.verify(other.public_key(), sig, b"hello") is False   # wrong key


def test_x963_pubkey_roundtrip_and_key_id():
    k = crypto.generate_private_key()
    x963 = crypto.public_key_x963(k)
    assert len(x963) == 65 and x963[0] == 0x04
    pub2 = crypto.public_key_from_x963(x963)
    sig = crypto.sign(k, b"m")
    assert crypto.verify(pub2, sig, b"m") is True
    assert crypto.key_id(k) == crypto.key_id(x963) == crypto.key_id(pub2)
    assert len(crypto.key_id(k)) == 16


def test_pem_roundtrip():
    k = crypto.generate_private_key()
    k2 = crypto.load_private_key_pem(crypto.private_key_to_pem(k))
    assert crypto.public_key_x963(k) == crypto.public_key_x963(k2)


def test_strict_parse_rejects_dupes_and_nonobject():
    assert P.strict_parse(b'{"a":1,"b":2}') == {"a": 1, "b": 2}
    for bad in (b'{"a":1,"a":2}', b'[1,2,3]', b'123', b'"x"', b'\xff\xfe'):
        try:
            P.strict_parse(bad)
            assert False, f"should reject {bad!r}"
        except P.ProtocolError:
            pass


def test_canonical_bytes_deterministic():
    a = P.canonical_bytes({"b": 1, "a": "x|y:z\n"})
    b = P.canonical_bytes({"a": "x|y:z\n", "b": 1})
    assert a == b and b'"a":"x|y:z\\n"' in a


def _challenge():
    return P.build_challenge_payload(
        core_instance_id="core-1", approval_id="ap-1", action_digest="d" * 64,
        principal_id="local-owner", device_id="dev-1", tool_id="codex_task", mode="modify",
        workspace="/Users/solvio/solvio-core", task="edit README",
        human_summary="Codex will edit README",
        challenge_nonce="n" * 32, issued_at=1000, expires_at=1120)


def test_challenge_validate():
    p = _challenge()
    P.validate_challenge_payload(p)
    # missing field
    bad = dict(p); del bad["action_digest"]
    try:
        P.validate_challenge_payload(bad); assert False
    except P.ProtocolError:
        pass
    # unknown field
    bad = dict(p); bad["evil"] = 1
    try:
        P.validate_challenge_payload(bad); assert False
    except P.ProtocolError:
        pass
    # wrong type/version
    bad = dict(p); bad["protocol_version"] = 1  # V1 must not be accepted by V2
    try:
        P.validate_challenge_payload(bad); assert False
    except P.ProtocolError:
        pass


def test_decision_validate():
    d = P.build_decision_payload(
        core_instance_id="core-1", approval_id="ap-1", action_digest="d" * 64,
        principal_id="local-owner", device_id="dev-1", key_id="k" * 16,
        challenge_nonce="n" * 32, challenge_payload_sha256="c" * 64,
        decision=P.DECISION_APPROVE, issued_at=1010, challenge_expires_at=1120)
    P.validate_decision_payload(d)
    try:
        P.build_decision_payload(core_instance_id="c", approval_id="a", action_digest="x",
                                 principal_id="p", device_id="dv", key_id="k",
                                 challenge_nonce="n", challenge_payload_sha256="s",
                                 decision="MAYBE", issued_at=1, challenge_expires_at=2)
        assert False
    except P.ProtocolError:
        pass


def test_end_to_end_challenge_then_decision():
    # --- Mac signs a challenge ---
    mac = crypto.generate_private_key()
    payload = _challenge()
    raw = P.canonical_bytes(payload)
    wire = {"payload_b64": P.b64e(raw), "signature_b64": P.b64e(crypto.sign(mac, raw)),
            "key_id": crypto.key_id(mac)}
    # --- iPhone side: verify over EXACT received bytes, then parse ---
    rx = P.b64d(wire["payload_b64"])
    assert crypto.verify(mac.public_key(), P.b64d(wire["signature_b64"]), rx) is True
    parsed = P.strict_parse(rx)
    P.validate_challenge_payload(parsed)
    assert parsed["human_summary"] == "Codex will edit README"
    # tampered payload -> Mac signature no longer verifies
    tampered = raw.replace(b"README", b"secrets")
    assert crypto.verify(mac.public_key(), P.b64d(wire["signature_b64"]), tampered) is False

    # --- iPhone signs a decision (software key stands in for the SE key) ---
    dev = crypto.generate_private_key()
    dp = P.build_decision_payload(
        core_instance_id=parsed["core_instance_id"], approval_id=parsed["approval_id"],
        action_digest=parsed["action_digest"], principal_id=parsed["principal_id"],
        device_id="dev-1", key_id=crypto.key_id(dev),
        challenge_nonce=parsed["challenge_nonce"],
        challenge_payload_sha256=hashlib.sha256(raw).hexdigest(),
        decision=P.DECISION_APPROVE, issued_at=1010,
        challenge_expires_at=parsed["expires_at"])
    draw = P.canonical_bytes(dp)
    dwire = {"payload_b64": P.b64e(draw), "signature_b64": P.b64e(crypto.sign(dev, draw)),
             "key_id": crypto.key_id(dev)}
    # --- Mac verifies decision over EXACT bytes with the device public key ---
    drx = P.b64d(dwire["payload_b64"])
    assert crypto.verify(dev.public_key(), P.b64d(dwire["signature_b64"]), drx) is True
    dparsed = P.strict_parse(drx)
    P.validate_decision_payload(dparsed)
    assert dparsed["action_digest"] == payload["action_digest"]  # action bound end-to-end
    # tampered decision -> device signature fails
    assert crypto.verify(dev.public_key(), P.b64d(dwire["signature_b64"]),
                         draw.replace(b"APPROVE", b"DENY_ll")) is False


def test_core_instance_id_stable_and_persisted():
    d = tempfile.mkdtemp()
    try:
        a = identity.load_or_create_core_instance_id(d)
        b = identity.load_or_create_core_instance_id(d)
        assert a == b and a.startswith("core-")
        idfile = os.path.join(d, "core_instance_id")
        assert os.path.isfile(idfile)
        assert oct(os.stat(idfile).st_mode & 0o777) == "0o600"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_mac_signing_key_persist_sign_and_perms():
    d = tempfile.mkdtemp()
    try:
        k1 = identity.MacSigningKey.load_or_create(d)
        k2 = identity.MacSigningKey.load_or_create(d)
        assert k1.public_key_x963() == k2.public_key_x963()  # persisted, not regenerated
        sig = k1.sign(b"challenge-bytes")
        pub = crypto.public_key_from_x963(k1.public_key_x963())
        assert crypto.verify(pub, sig, b"challenge-bytes") is True
        assert len(k1.key_id) == 16 and len(k1.fingerprint()) == 64
        keyfile = os.path.join(d, "core_signing_key.pem")
        assert oct(os.stat(keyfile).st_mode & 0o777) == "0o600"
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
