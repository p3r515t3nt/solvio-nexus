"""M0/2 — authentication for the voice satellite link.

WHY. The satellite listener binds `0.0.0.0:8766` and accepted **any** connection. A client on
the LAN could connect, send `session_start`, and the Core would open an OpenAI Realtime
session and expose the tool dispatcher to it — funded provider resources and Home Assistant
control, reachable by anyone who can route to the port. Reproduced from the Mac against the
running service before this existed.

WHAT THIS IS. The smallest thing that answers "is this our satellite?": a challenge-response
over a shared secret, using HMAC-SHA256 from the standard library.

  Core       -> auth_challenge  { protocol_version, server_nonce }
  Satellite  -> hello           { protocol_version, satellite_id, client_nonce, auth }
  Core       -> constant-time verification, then (and only then) the session protocol

The long-term secret never travels. The challenge is generated per connection and consumed on
the first attempt, so a captured response cannot be replayed — against a new connection it
faces a different nonce, and against the same connection there is no second attempt.

WHAT THIS IS NOT. Not TLS, not a PKI, not device attestation. It authenticates the satellite
to the Core on a trusted LAN segment; it does not encrypt the link or protect against an
attacker who has already read the secret file. Approval authority is unaffected and untouched:
a satellite that authenticates here still cannot approve anything.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
from hashlib import sha256

#: Bumped only when the canonical material below changes. A satellite that speaks a different
#: version is refused rather than silently misinterpreted.
PROTOCOL_VERSION = 1

#: The domain separator makes the digest unusable outside this exact purpose.
_CANONICAL_PREFIX = b"solvio-satellite-auth-v1"

NONCE_BYTES = 32
#: A satellite may take this long to answer the challenge before the connection is dropped.
HANDSHAKE_TIMEOUT = 10.0

#: Deliberately strict: the id goes into a newline-separated canonical string, so it must not
#: be able to contain a separator or anything ambiguous.
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_HEX_RE = re.compile(r"^[0-9a-f]{16,128}$")

DEFAULT_CREDENTIAL_PATH = "~/.solvio/satellite_auth.json"
CREDENTIAL_ENV = "SOLVIO_SATELLITE_AUTH_FILE"


class SatelliteAuthError(Exception):
    """Configuration is unusable. Fail loudly rather than start unauthenticated."""


def new_challenge() -> str:
    """A fresh nonce for exactly one connection."""
    return secrets.token_hex(NONCE_BYTES)


def canonical_material(*, protocol_version: int, server_nonce: str, satellite_id: str,
                       client_nonce: str) -> bytes:
    """The exact bytes both sides sign. Versioned, ordered, unambiguously separated.

    Newline separation is safe because every field is validated to exclude newlines: the id
    against `_ID_RE`, the nonces against `_HEX_RE`, the version as an integer.
    """
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
    material = canonical_material(protocol_version=protocol_version,
                                  server_nonce=server_nonce, satellite_id=satellite_id,
                                  client_nonce=client_nonce)
    return hmac.new(secret, material, sha256).hexdigest()


class SatelliteCredentials:
    """The satellites this Core accepts, loaded from an out-of-repo file."""

    def __init__(self, secrets_by_id: dict[str, bytes], *, source: str = "<memory>") -> None:
        if not secrets_by_id:
            raise SatelliteAuthError("no satellite credentials configured")
        for sat_id, secret in secrets_by_id.items():
            if not _ID_RE.match(sat_id):
                raise SatelliteAuthError(f"unacceptable satellite_id in {source}")
            if len(secret) < 32:
                raise SatelliteAuthError(
                    f"the secret for {sat_id} is shorter than 32 bytes — regenerate it")
        self._secrets = dict(secrets_by_id)
        self.source = source

    @property
    def satellite_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._secrets))

    def verify(self, *, satellite_id: str, server_nonce: str, client_nonce: str,
               protocol_version: int, auth: str) -> tuple[bool, str]:
        """Constant-time check. Returns (ok, reason) — the reason never carries key material."""
        if protocol_version != PROTOCOL_VERSION:
            return False, "protocol_version_mismatch"
        if not _ID_RE.match(satellite_id or ""):
            return False, "malformed_satellite_id"
        if not isinstance(auth, str) or not _HEX_RE.match(auth or ""):
            return False, "malformed_auth"
        secret = self._secrets.get(satellite_id)
        if secret is None:
            # Still do the work so a wrong id and a wrong secret cost the same.
            secret = b"\x00" * 32
            expected = compute_auth(secret, protocol_version=protocol_version,
                                    server_nonce=server_nonce, satellite_id=satellite_id,
                                    client_nonce=client_nonce)
            hmac.compare_digest(expected, auth)
            return False, "unknown_satellite_id"
        try:
            expected = compute_auth(secret, protocol_version=protocol_version,
                                    server_nonce=server_nonce, satellite_id=satellite_id,
                                    client_nonce=client_nonce)
        except SatelliteAuthError:
            return False, "malformed_handshake"
        if not hmac.compare_digest(expected, auth):
            return False, "bad_auth"
        return True, "ok"


def credential_path() -> str:
    return os.path.expanduser(os.environ.get(CREDENTIAL_ENV) or DEFAULT_CREDENTIAL_PATH)


def load_credentials(path: str | None = None) -> SatelliteCredentials:
    """Read the credential file. Refuses a file others can read."""
    resolved = os.path.expanduser(path or credential_path())
    if not os.path.exists(resolved):
        raise SatelliteAuthError(
            f"no satellite credential file at {resolved} — create one with "
            f"scripts/provision_satellite_credential.py")
    mode = os.stat(resolved).st_mode & 0o777
    if mode & 0o077:
        raise SatelliteAuthError(
            f"{resolved} is readable by others (mode {mode:04o}); run chmod 600 on it")
    try:
        with open(resolved, encoding="utf-8") as fh:
            data = json.load(fh)
        entries = data["satellites"]
        secrets_by_id = {str(k): bytes.fromhex(v) for k, v in entries.items()}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SatelliteAuthError(f"{resolved} is not a usable credential file: {exc}") from exc
    return SatelliteCredentials(secrets_by_id, source=resolved)
