"""Closed Hermes observation contract; authentication stays in browser_sessions.

Prefix, cookie and digest separation also hold when an older Core ignores new
columns. This module contains no socket, HTTP, provider or credential storage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import ipaddress
import math
import re
import time
from urllib.parse import urlsplit

PURPOSE = "hermes_observer_v1"
COOKIE_NAME = "__Host-solvio-hermes-observer"
ENROLLMENT_PREFIX = "hoe1."
SESSION_PREFIX = "hos1."
ENROLLMENT_LIFETIME_S = 600
SESSION_LIFETIME_S = 8 * 3600
_SECRET = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9._:-]{1,200}\Z")
_RUN = r"ar-[a-f0-9]{16}"


def valid_secret(token, kind):
    prefix = {"enrollment": ENROLLMENT_PREFIX, "session": SESSION_PREFIX}.get(kind)
    return bool(prefix and isinstance(token, str) and token.startswith(prefix)
                and _SECRET.fullmatch(token[len(prefix):]))


def digest(kind, value):
    return hashlib.sha256(("solvio-browser-observer-v1:" + kind + ":" + value).encode("ascii")).hexdigest()


def csrf_for(token):
    return hmac.new(token.encode("ascii"), b"solvio-browser-observer-csrf-v1", hashlib.sha256).hexdigest()


def local_origin(value):
    """A fixed numeric local gateway origin, never a wildcard or remote URL."""
    if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise ValueError("invalid_observer_origin")
    try:
        parts = urlsplit(value)
        address = ipaddress.ip_address(parts.hostname or "")
        host = f"[{address}]" if address.version == 6 else str(address)
        if (not (address.is_private or address.is_loopback) or address.is_unspecified
                or address.is_multicast or "%" in value or not parts.port
                or value != f"https://{host}:{parts.port}"):
            raise ValueError
    except ValueError:
        raise ValueError("invalid_observer_origin") from None
    return value


def fingerprint(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("invalid_observer_fingerprint")
    return value


@dataclass(frozen=True)
class Binding:
    core_instance_id: str
    owner_principal: str
    origin: str
    tls_fingerprint: str

    def __post_init__(self):
        for value in (self.core_instance_id, self.owner_principal):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError("invalid_observer_identity")
        local_origin(self.origin)
        fingerprint(self.tls_fingerprint)


@dataclass(frozen=True)
class Enrollment:
    token: str = field(repr=False)
    expires_at: float
    binding: Binding

    def as_response(self):
        return {"ok": True, "purpose": PURPOSE, "token": self.token,
                "expires_at": self.expires_at, **self.binding.__dict__}


def validate_enrollment(value, *, expected: Binding | None = None, now=None):
    """A client MUST validate before redeeming; old full-owner replies fail closed."""
    fields = {"ok", "purpose", "token", "expires_at", "core_instance_id",
              "owner_principal", "origin", "tls_fingerprint"}
    if (type(value) is not dict or set(value) != fields or value["ok"] is not True
            or value["purpose"] != PURPOSE or not valid_secret(value["token"], "enrollment")):
        raise ValueError("invalid_observer_enrollment")
    expires = value["expires_at"]
    current = time.time() if now is None else now
    if (type(expires) not in (int, float) or not math.isfinite(expires)
            or not 0 < expires - current <= ENROLLMENT_LIFETIME_S):
        raise ValueError("invalid_observer_expiry")
    binding = Binding(**{key: value[key] for key in Binding.__dataclass_fields__})
    if expected is not None and (type(expected) is not Binding or binding != expected):
        raise ValueError("observer_binding_changed")
    return Enrollment(value["token"], expires, binding)


def read_route(method, path):
    # No HEAD/mutation alias, general proxy, downloads or alternate run actions.
    if method != "GET":
        return False
    if path in {"/v1/browser/session", "/v1/agent/runs"}:
        return True
    return bool(re.fullmatch(r"/v1/agent/runs/" + _RUN + r"(?:/events)?", path))


def static_route(method, path):
    if method != "GET":
        return False
    if path == "/dashboard/assets/window-share.js":
        return True  # Existing renderer import only; ticket routes stay denied.
    return bool(re.fullmatch(r"/dashboard/hermes/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)?\.(?:html|js|css|woff2?|ttf|txt)", path))
