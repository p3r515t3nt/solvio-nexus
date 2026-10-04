"""Local pairing: one-time enrollment QR + local TLS cert (STEP S2A, Phase 16/17).

The Mac shows a one-time pairing QR; the iPhone scans it and enrolls. The QR carries the
protocol version, core_instance_id, the local approval endpoint, the server TLS
fingerprint, the Mac challenge-signing public-key fingerprint, a high-entropy one-time
enrollment secret (short TTL), and an expiry. The iPhone PINS the TLS fingerprint from the
QR (it does not disable global TLS validation) and later verifies challenges against the
pinned Mac signing key. The one-time secret is never logged and never committed.

TLS: a self-signed certificate for the local host, stored 0600 in the 0700 state dir. It
is device-pinned via the QR fingerprint, so a self-signed cert is safe here.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from solvio.security.mobile_approval import crypto
from solvio.security.mobile_approval import protocol as P

_CERT = "gateway_cert.pem"
_CERTKEY = "gateway_key.pem"


def build_pairing_payload(*, core_instance_id, endpoint, tls_fingerprint,
                          mac_pubkey_fingerprint, mac_pubkey_x963_b64,
                          enrollment_token, expires_at) -> dict:
    return {
        "v": P.PROTOCOL_VERSION, "type": "pairing", "core_instance_id": core_instance_id,
        "endpoint": endpoint, "tls_fingerprint": tls_fingerprint,
        "mac_pubkey_fingerprint": mac_pubkey_fingerprint,
        "mac_pubkey_x963_b64": mac_pubkey_x963_b64,
        "enrollment_token": enrollment_token, "expires_at": expires_at,
    }


def pairing_qr_text(payload: dict) -> str:
    """Compact JSON the iPhone decodes from the scanned QR."""
    return P.canonical_bytes(payload).decode("utf-8")


def cert_fingerprint(cert_pem: bytes) -> str:
    cert = x509.load_pem_x509_certificate(cert_pem)
    return crypto.fingerprint(cert.public_bytes(serialization.Encoding.DER))


def load_or_create_gateway_cert(state_dir: str, *, host: str = "127.0.0.1"):
    """Returns (cert_path, key_path, fingerprint_hex). Self-signed P-256, 0600."""
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass
    cpath = os.path.join(state_dir, _CERT)
    kpath = os.path.join(state_dir, _CERTKEY)
    if os.path.isfile(cpath) and os.path.isfile(kpath):
        return cpath, kpath, cert_fingerprint(open(cpath, "rb").read())

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SOLVIO Approvals Gateway")])
    epoch = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    san = [x509.IPAddress(__import__("ipaddress").ip_address(host))] if host[0].isdigit() \
        else [x509.DNSName(host)]
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(epoch)
            .not_valid_after(epoch + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    for path, data in ((cpath, cert_pem), (kpath, key_pem)):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    return cpath, kpath, cert_fingerprint(cert_pem)
