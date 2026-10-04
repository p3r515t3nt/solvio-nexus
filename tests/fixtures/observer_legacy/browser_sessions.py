"""Private dashboard sessions in the existing approval control plane.

The privileged local caller issues enrollment tokens for an already established
principal. There is deliberately no HTTP issuance endpoint. Browser credentials
authenticate that principal; they are neither device transport credentials nor a
Face-ID assertion. Task-specific authorization belongs to the task/approval layer.

All SQL runs on ApprovalControlStore's existing executor/connection. Its writable
startup owns schema creation; read-only inspection never creates session tables.
The caller supplies the persisted control-plane core_instance_id, not a process ID.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import urlsplit

from aiohttp import web

from .store import ApprovalControlStore, ApprovalStoreError

COOKIE_NAME = "__Host-solvio-session"
CSRF_HEADER = "X-CSRF-Token"
SESSION_PATH = "/v1/browser/session"
ENROLLMENT_LIFETIME_S = 600
DEFAULT_SESSION_LIFETIME_S = 30 * 86400
_SECRET = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9._:-]{1,200}\Z")


@dataclass(frozen=True)
class BrowserActor:
    principal: str
    session_id: str
    expires_at: float = 0.0


@dataclass(frozen=True)
class BrowserEnrollment:
    token: str = field(repr=False)
    expires_at: float


@dataclass(frozen=True)
class BrowserSession:
    actor: BrowserActor
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)


def _digest(kind: str, value: str) -> str:
    return hashlib.sha256(("solvio-browser-v1:" + kind + ":" + value).encode("ascii")).hexdigest()


def _valid_secret(value) -> bool:
    return isinstance(value, str) and _SECRET.fullmatch(value) is not None


def _csrf_for(token: str) -> str:
    # Recoverable from the HttpOnly secret on authenticated GET, so reloads and
    # multiple tabs need no rotation or plaintext CSRF storage. CSRF never reveals
    # the cookie. Distinct secrets yield distinct CSRF values.
    return hmac.new(token.encode("ascii"), b"solvio-browser-csrf-v1", hashlib.sha256).hexdigest()


class BrowserSessionService:
    def __init__(self, store: ApprovalControlStore, *, core_instance_id: str,
                 session_lifetime_s: float = DEFAULT_SESSION_LIFETIME_S,
                 clock: Callable[[], float] = time.time):
        if not isinstance(core_instance_id, str) or not _IDENTIFIER.fullmatch(core_instance_id):
            raise ValueError("persisted core_instance_id required")
        if (isinstance(session_lifetime_s, bool)
                or not isinstance(session_lifetime_s, (float, int))
                or not math.isfinite(session_lifetime_s)
                or not 0 < session_lifetime_s <= 365 * 86400):
            raise ValueError("session lifetime must be positive and at most 365 days")
        self.store = store
        self.core_instance_id = core_instance_id
        self.session_lifetime_s = float(session_lifetime_s)
        self._clock = clock

    def _connection(self, *, writable=False):
        if self.store._conn is None or self.store._closed:
            raise ApprovalStoreError("approval store unavailable")
        if writable and self.store.read_only:
            raise ApprovalStoreError("read-only approval store")
        return self.store._conn

    async def issue_enrollment(self, *, principal: str) -> BrowserEnrollment:
        """LOCAL PRIVILEGED SERVICE ONLY: principal must come from the trusted caller.

        Deliver the returned secret directly to the owner, never in a URL, argv,
        model context, or audit log. No browser/agent-supplied principal is accepted
        by any HTTP route in this module.
        """
        if not isinstance(principal, str) or not _IDENTIFIER.fullmatch(principal):
            raise ValueError("trusted principal required")
        token = secrets.token_urlsafe(32)
        return await self.store._run(self._issue, token, principal)

    def _issue(self, token, principal):
        conn = self._connection(writable=True)
        now = self._clock()
        expires_at = now + ENROLLMENT_LIFETIME_S
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("INSERT INTO browser_enrollment_tokens "
                         "(token_hash,principal,core_instance_id,created_at,expires_at) "
                         "VALUES (?,?,?,?,?)", (_digest("enrollment", token), principal,
                             self.core_instance_id, now, expires_at))
            self.store._audit("browser_enrollment_issued", reason=principal)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return BrowserEnrollment(token, expires_at)

    async def redeem(self, token: str) -> BrowserSession | None:
        if not _valid_secret(token):
            return None
        return await self.store._run(self._redeem, token)

    def _redeem(self, token):
        conn = self._connection(writable=True)
        now = self._clock()
        conn.execute("BEGIN IMMEDIATE")
        try:
            taken = conn.execute("UPDATE browser_enrollment_tokens SET consumed_at=? "
                "WHERE token_hash=? AND core_instance_id=? AND consumed_at IS NULL "
                "AND expires_at>?", (now, _digest("enrollment", token), self.core_instance_id, now))
            if taken.rowcount != 1:
                conn.execute("COMMIT")
                return None
            row = conn.execute("SELECT principal FROM browser_enrollment_tokens WHERE token_hash=?",
                               (_digest("enrollment", token),)).fetchone()
            secret = secrets.token_urlsafe(32)
            csrf = _csrf_for(secret)
            sid = "browser-" + secrets.token_hex(16)
            expires_at = now + self.session_lifetime_s
            conn.execute("INSERT INTO browser_sessions "
                "(session_id,token_hash,csrf_hash,principal,core_instance_id,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?)", (sid, _digest("session", secret), _digest("csrf", csrf),
                     row["principal"], self.core_instance_id, now, expires_at))
            self.store._audit("browser_session_created", identity=sid)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return BrowserSession(BrowserActor(row["principal"], sid, expires_at), secret, csrf)

    async def authenticate(self, token: str, *, csrf_token: str | None = None) -> BrowserActor | None:
        if not _valid_secret(token):
            return None
        if csrf_token is not None and (not isinstance(csrf_token, str)
                or re.fullmatch(r"[a-f0-9]{64}", csrf_token) is None):
            return None
        return await self.store._run(self._authenticate, token, csrf_token)

    def _authenticate(self, token, csrf_token):
        conn = self._connection()
        row = conn.execute("SELECT session_id,principal,expires_at,csrf_hash FROM browser_sessions "
            "WHERE token_hash=? AND core_instance_id=? AND revoked_at IS NULL AND expires_at>?",
            (_digest("session", token), self.core_instance_id, self._clock())).fetchone()
        if row is None:
            return None
        if csrf_token is not None and not hmac.compare_digest(row["csrf_hash"], _digest("csrf", csrf_token)):
            return None
        return BrowserActor(row["principal"], row["session_id"], row["expires_at"])

    async def revoke(self, session_id: str, *, principal: str) -> bool:
        """Revoke a session belonging to the authenticated/local trusted principal."""
        return await self.store._run(self._revoke, session_id, principal)

    def _revoke(self, session_id, principal):
        conn = self._connection(writable=True)
        conn.execute("BEGIN IMMEDIATE")
        try:
            updated = conn.execute("UPDATE browser_sessions SET revoked_at=? WHERE session_id=? "
                "AND principal=? AND core_instance_id=? AND revoked_at IS NULL",
                (self._clock(), session_id, principal, self.core_instance_id))
            if updated.rowcount == 1:
                self.store._audit("browser_session_revoked", identity=session_id)
            conn.execute("COMMIT")
            return updated.rowcount == 1
        except BaseException:
            conn.execute("ROLLBACK")
            raise


_SERVICE = web.AppKey("solvio_browser_session_service", BrowserSessionService)
_ORIGINS = web.AppKey("solvio_browser_session_origins", frozenset)


def _trusted_origins(origins: Iterable[str]) -> frozenset[str]:
    if isinstance(origins, str):
        raise ValueError("allowed_origins must contain exact HTTPS origins")
    checked = set()
    for origin in origins:
        if not isinstance(origin, str) or any(ord(c) <= 32 or ord(c) >= 127 for c in origin):
            raise ValueError("invalid trusted HTTPS origin")
        parts = urlsplit(origin)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.path or parts.query or parts.fragment
                or "*" in origin or parts.port == 0 or "\\" in origin):
            raise ValueError("allowed_origins must contain exact HTTPS origins")
        checked.add(origin)
    if not checked:
        raise ValueError("at least one trusted HTTPS origin required")
    return frozenset(checked)


def _origin_allowed(request: web.Request) -> bool:
    values = request.headers.getall("Origin", [])
    return len(values) == 1 and values[0] in request.app.get(_ORIGINS, ())


async def actor(request: web.Request, mutating: bool = False) -> BrowserActor | None:
    """Verify the durable browser session. Mutations require exact Origin + bound CSRF.

    Ignores X-Device-Id, X-Transport-Cred, bearer tokens, query and body identity fields.
    Forwarded scheme headers do not replace a TLS connection. No authentication cache
    can outlive a committed revocation or a session expiry.
    """
    service = request.app.get(_SERVICE)
    if service is None or not request.secure:
        return None
    if mutating or "Origin" in request.headers:
        if not _origin_allowed(request):
            return None
    csrf = None
    if mutating:
        values = request.headers.getall(CSRF_HEADER, [])
        if len(values) != 1 or not values[0]:
            return None
        csrf = values[0]
    try:
        return await service.authenticate(request.cookies.get(COOKIE_NAME, ""), csrf_token=csrf)
    except (ApprovalStoreError, sqlite3.Error):
        return None


def _response(data, *, status=200):
    return web.json_response(data, status=status, headers={
        "Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff"})


def _session_view(verified: BrowserActor, secret: str):
    return {"principal": verified.principal, "session_id": verified.session_id,
            "expires_at": verified.expires_at, "csrf_token": _csrf_for(secret)}


async def _login(request):
    if not request.secure or not _origin_allowed(request):
        return _response({"error": "untrusted_browser_origin"}, status=403)
    if request.content_type != "application/json":
        return _response({"error": "bad_request"}, status=400)
    # Bound reads even for chunked bodies; tokens never belong in query strings.
    data = bytearray()
    async for chunk in request.content.iter_chunked(2048):
        data.extend(chunk)
        if len(data) > 2048:
            return _response({"error": "bad_request"}, status=400)
    try:
        body = json.loads(data)
    except (ValueError, UnicodeError):
        return _response({"error": "bad_request"}, status=400)
    if not isinstance(body, dict) or set(body) != {"token"}:
        return _response({"error": "bad_request"}, status=400)
    service = request.app[_SERVICE]
    try:
        session = await service.redeem(body["token"])
    except (ApprovalStoreError, sqlite3.Error):
        return _response({"error": "session_unavailable"}, status=503)
    if session is None:
        return _response({"error": "invalid_enrollment"}, status=401)
    response = _response(_session_view(session.actor, session.token))
    response.set_cookie(COOKIE_NAME, session.token, path="/", secure=True, httponly=True,
                        samesite="Strict", max_age=math.ceil(service.session_lifetime_s))
    return response


async def _current(request):
    verified = await actor(request)
    if verified is None:
        return _response({"error": "unauthorized"}, status=401)
    return _response(_session_view(verified, request.cookies[COOKIE_NAME]))


async def _logout(request):
    verified = await actor(request, mutating=True)
    if verified is None:
        return _response({"error": "unauthorized"}, status=401)
    try:
        await request.app[_SERVICE].revoke(verified.session_id, principal=verified.principal)
    except (ApprovalStoreError, sqlite3.Error):
        return _response({"error": "session_unavailable"}, status=503)
    response = _response({"ok": True})
    response.set_cookie(COOKIE_NAME, "", path="/", secure=True, httponly=True,
                        samesite="Strict", max_age=0, expires="Thu, 01 Jan 1970 00:00:00 GMT")
    return response


def attach(app: web.Application, store_service: BrowserSessionService,
           allowed_origins: Iterable[str]) -> None:
    """Attach only redemption, current-session, and logout; never token issuance.

    The hosting gateway must serve HTTPS. Trusted origins are explicit deployment
    configuration. This module installs no CORS allowances or public HTTP fallback.
    """
    origins = _trusted_origins(allowed_origins)
    app[_SERVICE] = store_service
    app[_ORIGINS] = origins
    app.router.add_post(SESSION_PATH + "/login", _login)
    app.router.add_get(SESSION_PATH, _current)
    app.router.add_post(SESSION_PATH + "/logout", _logout)
