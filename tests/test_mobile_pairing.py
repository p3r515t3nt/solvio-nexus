"""Pairing QR + local TLS cert tests (STEP S2A). Direct: python test_mobile_pairing.py."""
import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

from solvio.security.mobile_approval import control as C
from solvio.security.mobile_approval import crypto
from solvio.security.mobile_approval import identity
from solvio.security.mobile_approval import pairing
from solvio.security.mobile_approval import protocol as P
from solvio.security.mobile_approval import store as S


def test_pairing_payload_roundtrip():
    payload = pairing.build_pairing_payload(
        core_instance_id="core-x", endpoint="https://192.168.178.123:8770",
        tls_fingerprint="a" * 64, mac_pubkey_fingerprint="b" * 64,
        mac_pubkey_x963_b64="Zm9vYmFy", enrollment_token="secret-token", expires_at=1234.0)
    text = pairing.pairing_qr_text(payload)
    parsed = P.strict_parse(text.encode("utf-8"))
    assert parsed["core_instance_id"] == "core-x"
    assert parsed["tls_fingerprint"] == "a" * 64
    assert parsed["enrollment_token"] == "secret-token"
    assert parsed["mac_pubkey_x963_b64"] == "Zm9vYmFy"
    assert parsed["v"] == P.PROTOCOL_VERSION and parsed["type"] == "pairing"


def test_pairing_payload_carries_mac_signing_key():
    """The QR's mac_pubkey_x963_b64 MUST be the Mac challenge-signing key: a challenge
    signed by the Mac verifies under the key decoded from the pairing payload — exactly
    what the iPhone does before trusting a challenge. Guards the Mac<->iOS contract so the
    Mac can never advertise a fingerprint without the matching verifiable key."""
    d = tempfile.mkdtemp()
    try:
        mac = identity.MacSigningKey.load_or_create(d)
        payload = pairing.build_pairing_payload(
            core_instance_id="core-x", endpoint="https://x:8770",
            tls_fingerprint="a" * 64, mac_pubkey_fingerprint=mac.fingerprint(),
            mac_pubkey_x963_b64=P.b64e(mac.public_key_x963()),
            enrollment_token="tok", expires_at=1.0)
        parsed = P.strict_parse(pairing.pairing_qr_text(payload).encode("utf-8"))
        x963 = P.b64d(parsed["mac_pubkey_x963_b64"])
        assert len(x963) == 65 and x963[0] == 0x04
        pub = crypto.public_key_from_x963(x963)
        challenge = b"exact-challenge-bytes-\x00\xf0\x9f"
        sig = mac.sign(challenge)
        assert crypto.verify(pub, sig, challenge) is True           # right key verifies
        assert crypto.verify(pub, sig, challenge + b"x") is False    # tamper rejected
        assert crypto.fingerprint(x963) == parsed["mac_pubkey_fingerprint"]  # fp matches key
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_gateway_cert_persist_and_fingerprint():
    d = tempfile.mkdtemp()
    try:
        c1, k1, fp1 = pairing.load_or_create_gateway_cert(d, host="127.0.0.1")
        c2, k2, fp2 = pairing.load_or_create_gateway_cert(d, host="127.0.0.1")
        assert fp1 == fp2 and len(fp1) == 64        # persisted, deterministic fingerprint
        assert oct(os.stat(k1).st_mode & 0o777) == "0o600"
        assert oct(os.stat(c1).st_mode & 0o777) == "0o600"
        assert pairing.cert_fingerprint(open(c1, "rb").read()) == fp1
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_enrollment_token_is_one_time_via_control_plane():
    async def go():
        d = tempfile.mkdtemp()
        try:
            st = S.ApprovalControlStore(os.path.join(d, "approval_control.sqlite3"))
            await st.open()
            cp = C.MobileApprovalControlPlane(st, identity.MacSigningKey.load_or_create(d),
        identity.load_or_create_core_instance_id(d))
            token, exp = await cp.create_enrollment_token("local-owner")
            payload = pairing.build_pairing_payload(
                core_instance_id="core-x", endpoint="https://x:8770",
                tls_fingerprint="a" * 64, mac_pubkey_fingerprint="b" * 64,
                mac_pubkey_x963_b64="Zm9vYmFy", enrollment_token=token, expires_at=exp)
            # the token embedded in the QR consumes exactly once
            p1, s1 = await st.consume_enrollment_token(
                __import__("hashlib").sha256(payload["enrollment_token"].encode()).hexdigest())
            assert p1 == "local-owner" and s1 == "ok"
            p2, s2 = await st.consume_enrollment_token(
                __import__("hashlib").sha256(payload["enrollment_token"].encode()).hexdigest())
            assert p2 is None and s2 == "already_consumed"
            await st.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)
    asyncio.run(go())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
