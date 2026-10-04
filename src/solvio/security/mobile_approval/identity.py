"""Mac control-plane identity + dedicated signing key (STEP S2A).

MAC ONLY, never settable by the model. The dedicated challenge-signing key exists for a
single purpose — *SOLVIO Mobile Approval Core Signing* — and is separate from the Node
CA, the OpenAI key, and any GitHub key. Private key material never enters git; it lives
0600 in a 0700 state directory. Only the public key / its fingerprint is shared (via the
pairing QR), so a later relay can never alter an approval challenge unnoticed.

Key rotation: replace `core_signing_key.pem` (control-plane/admin action). Enrolled
devices pin the old fingerprint, so rotation requires re-pairing — that is intentional.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from solvio.security.mobile_approval import crypto

DEFAULT_STATE_DIR = os.environ.get(
    "SOLVIO_APPROVAL_STATE_DIR", os.path.expanduser("~/.solvio-approvals"))
_ID_FILE = "core_instance_id"
_KEY_FILE = "core_signing_key.pem"


def _ensure_dir(state_dir: str) -> str:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass
    return state_dir


def load_or_create_core_instance_id(state_dir: str = DEFAULT_STATE_DIR) -> str:
    """Stable per-Mac control-plane id. Created once, then persisted. Not model-settable."""
    _ensure_dir(state_dir)
    path = os.path.join(state_dir, _ID_FILE)
    if os.path.isfile(path):
        val = open(path, encoding="utf-8").read().strip()
        if val:
            return val
    val = "core-" + uuid.uuid4().hex
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(val)
    return val


class MissingCoreIdentity(FileNotFoundError):
    """P1A.6/§7: the core identity files are absent where they were required to exist."""


def load_core_instance_id(state_dir: str = DEFAULT_STATE_DIR) -> str:
    """Load WITHOUT creating. Admin commands must never mint a new core identity:
    a fresh core_instance_id silently de-authorises every enrolled device (they then fail
    `wrong_core_instance`) while the operator's own listing still shows them healthy."""
    path = os.path.join(state_dir, _ID_FILE)
    if not os.path.isfile(path):
        raise MissingCoreIdentity(f"missing {_ID_FILE} in {state_dir}")
    val = open(path, encoding="utf-8").read().strip()
    if not val:
        raise MissingCoreIdentity(f"empty {_ID_FILE} in {state_dir}")
    return val


class MacSigningKey:
    """Dedicated P-256 key used ONLY to sign approval challenges."""

    def __init__(self, key) -> None:
        self._key = key

    @classmethod
    def load_or_create(cls, state_dir: str = DEFAULT_STATE_DIR) -> "MacSigningKey":
        _ensure_dir(state_dir)
        path = os.path.join(state_dir, _KEY_FILE)
        if os.path.isfile(path):
            return cls(crypto.load_private_key_pem(open(path, "rb").read()))
        key = crypto.generate_private_key()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(crypto.private_key_to_pem(key))
        return cls(key)

    @classmethod
    def load(cls, state_dir: str = DEFAULT_STATE_DIR) -> "MacSigningKey":
        """Load WITHOUT creating — see load_core_instance_id."""
        path = os.path.join(state_dir, _KEY_FILE)
        if not os.path.isfile(path):
            raise MissingCoreIdentity(f"missing {_KEY_FILE} in {state_dir}")
        return cls(crypto.load_private_key_pem(open(path, "rb").read()))

    def sign(self, data: bytes) -> bytes:
        return crypto.sign(self._key, data)

    def public_key_x963(self) -> bytes:
        return crypto.public_key_x963(self._key)

    @property
    def key_id(self) -> str:
        return crypto.key_id(self._key)

    def fingerprint(self) -> str:
        """sha256 hex of the X9.63 public key — pinned via the pairing QR."""
        return crypto.fingerprint(self.public_key_x963())
