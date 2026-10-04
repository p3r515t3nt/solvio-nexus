"""P-256 crypto for the SOLVIO Mobile Approval Protocol (STEP S2A).

Interop contract with iOS CryptoKit / Secure Enclave:
- Public keys travel as **X9.63** uncompressed points (0x04 || X || Y), which map to
  CryptoKit `P256.Signing.PublicKey(x963Representation:)` and
  `SecureEnclave.P256.Signing.PrivateKey.publicKey.x963Representation`.
- Signatures travel as **DER** (CryptoKit `.derRepresentation`), verified here as DER.
- Signing/verification is ECDSA P-256 over **SHA-256** of the exact wire bytes. The
  RECEIVER verifies over the received bytes and never re-serialises (no cross-language
  canonicalisation dependency).

No private key ever leaves the Secure Enclave (iOS) or the Mac control-plane key file.
"""
from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

_CURVE = ec.SECP256R1()
_SIG = ec.ECDSA(hashes.SHA256())


# ---- Mac control-plane signing key (P-256) --------------------------------
def generate_private_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(_CURVE)


def private_key_to_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def load_private_key_pem(pem: bytes) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1):
        raise ValueError("not a P-256 private key")
    return key


# ---- public keys (X9.63 uncompressed point, CryptoKit-compatible) ---------
def public_key_x963(key) -> bytes:
    pub = key.public_key() if isinstance(key, ec.EllipticCurvePrivateKey) else key
    return pub.public_bytes(serialization.Encoding.X962,
                            serialization.PublicFormat.UncompressedPoint)


def public_key_from_x963(x963: bytes) -> ec.EllipticCurvePublicKey:
    if len(x963) != 65 or x963[0] != 0x04:
        raise ValueError("not an uncompressed P-256 X9.63 point")
    return ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, x963)


def key_id(pubkey_or_x963) -> str:
    """Stable, non-secret key identifier: sha256(x963)[:16] hex."""
    x963 = pubkey_or_x963 if isinstance(pubkey_or_x963, (bytes, bytearray)) \
        else public_key_x963(pubkey_or_x963)
    return hashlib.sha256(bytes(x963)).hexdigest()[:16]


def fingerprint(data: bytes) -> str:
    """Full sha256 hex (e.g. TLS/pubkey pinning fingerprints)."""
    return hashlib.sha256(data).hexdigest()


# ---- sign / verify over EXACT bytes ---------------------------------------
def sign(key: ec.EllipticCurvePrivateKey, data: bytes) -> bytes:
    """ECDSA P-256 / SHA-256 -> DER signature over the exact bytes."""
    return key.sign(data, _SIG)


def verify(pubkey: ec.EllipticCurvePublicKey, signature_der: bytes, data: bytes) -> bool:
    """Verify a DER ECDSA P-256/SHA-256 signature over the exact bytes. Never raises."""
    try:
        pubkey.verify(signature_der, data, _SIG)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
