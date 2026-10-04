"""M0/2 — proving to the Core that this is the authorised satellite.

Mirrors `solvio.realtime.satellite_auth` on the Core. The two sides MUST agree byte for byte
on the canonical material below; `PROTOCOL_VERSION` is what makes a disagreement a clean
refusal instead of a silent misinterpretation.

The shared secret never leaves this machine — only the HMAC over a challenge the Core just
generated does.
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
from hashlib import sha256

PROTOCOL_VERSION = 1
_CANONICAL_PREFIX = b"solvio-satellite-auth-v1"
NONCE_BYTES = 32

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_HEX_RE = re.compile(r"^[0-9a-f]{16,128}$")

DEFAULT_SECRET_PATH = "~/.solvio/satellite_secret"
DEFAULT_ID_PATH = "~/.solvio/satellite_id"


class SatelliteAuthError(Exception):
    pass


def canonical_material(*, protocol_version: int, server_nonce: str, satellite_id: str,
                       client_nonce: str) -> bytes:
    if not isinstance(protocol_version, int):
        raise SatelliteAuthError("protocol_version must be an integer")
    if not _ID_RE.match(satellite_id or ""):
        raise SatelliteAuthError("satellite_id has an unacceptable shape")
    for nonce in (server_nonce, client_nonce):
        if not _HEX_RE.match(nonce or ""):
            raise SatelliteAuthError("nonce has an unacceptable shape")
    return b"\n".join((
        _CANONICAL_PREFIX,
        str(protocol_version).encode("ascii"),
        server_nonce.encode("ascii"),
        satellite_id.encode("ascii"),
        client_nonce.encode("ascii"),
    ))


def compute_auth(secret: bytes, *, protocol_version: int, server_nonce: str,
                 satellite_id: str, client_nonce: str) -> str:
    return hmac.new(secret, canonical_material(
        protocol_version=protocol_version, server_nonce=server_nonce,
        satellite_id=satellite_id, client_nonce=client_nonce), sha256).hexdigest()


def _read_private(path: str) -> str:
    resolved = os.path.expanduser(path)
    if not os.path.exists(resolved):
        raise SatelliteAuthError(f"missing {resolved}")
    mode = os.stat(resolved).st_mode & 0o777
    if mode & 0o077:
        raise SatelliteAuthError(f"{resolved} is readable by others (mode {mode:04o})")
    with open(resolved, encoding="utf-8") as fh:
        return fh.read().strip()


def load_identity() -> tuple[str, bytes]:
    """(satellite_id, secret). Both live outside the repository, owner-readable only."""
    satellite_id = os.environ.get("SOLVIO_SATELLITE_ID") or _read_private(DEFAULT_ID_PATH)
    secret_hex = _read_private(os.environ.get("SOLVIO_SATELLITE_SECRET_FILE")
                               or DEFAULT_SECRET_PATH)
    if not _ID_RE.match(satellite_id):
        raise SatelliteAuthError("configured satellite_id has an unacceptable shape")
    try:
        secret = bytes.fromhex(secret_hex)
    except ValueError as exc:
        raise SatelliteAuthError("the secret file is not hexadecimal") from exc
    if len(secret) < 32:
        raise SatelliteAuthError("the secret is shorter than 32 bytes")
    return satellite_id, secret


def build_hello(challenge: dict, *, satellite_id: str, secret: bytes) -> dict:
    """Answer the Core's auth_challenge. Raises if the Core speaks another version."""
    version = challenge.get("protocol_version")
    if version != PROTOCOL_VERSION:
        raise SatelliteAuthError(
            f"core speaks protocol_version {version}, this satellite speaks {PROTOCOL_VERSION}")
    server_nonce = str(challenge.get("server_nonce", ""))
    client_nonce = secrets.token_hex(NONCE_BYTES)
    return {"type": "hello", "protocol_version": PROTOCOL_VERSION,
            "satellite_id": satellite_id, "client_nonce": client_nonce,
            "info": "pi-satellit",
            "auth": compute_auth(secret, protocol_version=PROTOCOL_VERSION,
                                 server_nonce=server_nonce, satellite_id=satellite_id,
                                 client_nonce=client_nonce)}
