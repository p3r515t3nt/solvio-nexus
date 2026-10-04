"""Browser authority at real local HTTPS handlers + temporary canonical approval DB.

Only the clock and the local TLS certificate are supplied by the test. No production
account, device, service, certificate, token, or provider is used.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import os
from pathlib import Path
import secrets
import sqlite3
import ssl
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal, require_raises
enforce_assertions()

from aiohttp import CookieJar, TCPConnector, web
from aiohttp.test_utils import TestClient, TestServer, unused_port
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from solvio.security.mobile_approval import browser_sessions as B, identity, store as S


class Clock:
    def __init__(self):
        self.now = 10000.0

    def __call__(self):
        return self.now


def _tls(tmp):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256()))
    certfile, keyfile = Path(tmp) / "test-cert.pem", Path(tmp) / "test-key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    keyfile.chmod(0o600)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certfile, keyfile)
    # Validate the generated local certificate: the HTTPS test uses real TLS.
    client_context = ssl.create_default_context(cafile=str(certfile))
    return server_context, client_context


class Harness:
    async def init(self, tmp, *, lifetime=B.DEFAULT_SESSION_LIFETIME_S, clock=None):
        self.path = os.path.join(tmp, "approval_control.sqlite3")
        self.store = S.ApprovalControlStore(self.path)
        await self.store.open()
        self.core_id = identity.load_or_create_core_instance_id(tmp)
        self.clock = clock or Clock()
        self.service = B.BrowserSessionService(self.store, core_instance_id=self.core_id,
                                              session_lifetime_s=lifetime, clock=self.clock)
        port = unused_port()
        self.origin = f"https://127.0.0.1:{port}"
        self.app = web.Application()
        B.attach(self.app, self.service, {self.origin})
        self.effects = []

        async def mutate(request):
            verified = await B.actor(request, mutating=True)
            if verified is None:
                return web.json_response({"error": "unauthorized"}, status=401)
            self.effects.append(verified)
            return web.json_response({"principal": verified.principal, "session_id": verified.session_id})

        self.app.router.add_post("/mutate", mutate)
        server_tls, self.client_tls = _tls(tmp)
        self.server = TestServer(self.app, port=port, scheme="https")
        await self.server.start_server(ssl=server_tls)
        self.clients = []
        self.client = await self.new_client()
        return self

    async def new_client(self):
        client = TestClient(self.server, cookie_jar=CookieJar(unsafe=True),
                            connector=TCPConnector(ssl=self.client_tls))
        await client.start_server()
        self.clients.append(client)
        return client

    async def login(self, *, client=None, principal="local-owner"):
        enrollment = await self.service.issue_enrollment(principal=principal)
        response = await (client or self.client).post(B.SESSION_PATH + "/login",
            json={"token": enrollment.token}, headers={"Origin": self.origin})
        require_equal(response.status, 200)
        return enrollment, response, await response.json()

    async def close(self):
        for client in self.clients:
            await client.close()
        await self.store.close()


@asynccontextmanager
async def _harness(*, lifetime=B.DEFAULT_SESSION_LIFETIME_S):
    with tempfile.TemporaryDirectory(prefix="solvio-browser-test-") as tmp:
        h = await Harness().init(tmp, lifetime=lifetime)
        try:
            yield h
        finally:
            await h.close()


async def t_https_login_uses_hardened_cookie_and_only_hashes_at_rest():
    async with _harness() as h:
        enrollment, response, data = await h.login()
        require_equal(enrollment.expires_at - h.clock.now, 600)
        require_equal(data["expires_at"] - h.clock.now, 30 * 86400)
        cookie = response.cookies[B.COOKIE_NAME]
        require(cookie["secure"] and cookie["httponly"])
        require_equal(cookie["samesite"], "Strict")
        require_equal(cookie["path"], "/")
        require_equal(cookie["domain"], "")
        require_equal(int(cookie["max-age"]), 30 * 86400)
        require_equal(response.headers["Cache-Control"], "no-store")
        require("Access-Control-Allow-Origin" not in response.headers)
        require("token" not in data and "face_id" not in data)
        require_equal(data["principal"], "local-owner")
        dump = await h.store._run(lambda: "\n".join(h.store._conn.iterdump()))
        for secret in (enrollment.token, cookie.value, data["csrf_token"]):
            require(secret not in dump, "plaintext secret persisted in approval database/audit")
        require(enrollment.token not in repr(enrollment))
        counts = await h.store._run(lambda: [h.store._conn.execute(
            "SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in ("devices", "enrollment_tokens", "approval_requests", "browser_sessions")])
        require_equal(counts, [0, 0, 0, 1], "browser login manufactured a device or an approval")
        current = await h.client.get(B.SESSION_PATH)
        require_equal(current.status, 200)
        require_equal(await current.json(), data, "reload changed the bound CSRF value")


async def t_http_replay_cannot_mint_a_second_session_or_replace_the_cookie():
    async with _harness() as h:
        enrollment, first, data = await h.login()
        replay = await h.client.post(B.SESSION_PATH + "/login", json={"token": enrollment.token},
                                     headers={"Origin": h.origin})
        require_equal(replay.status, 401)
        require(B.COOKIE_NAME not in replay.cookies)
        require_equal((await (await h.client.get(B.SESSION_PATH)).json())["session_id"], data["session_id"])
        require_equal(await h.store._run(lambda: h.store._conn.execute(
            "SELECT COUNT(*) FROM browser_sessions").fetchone()[0]), 1)


async def t_redemption_race_across_two_real_connections_has_exactly_one_winner():
    async with _harness() as h:
        second = S.ApprovalControlStore(h.path)
        await second.open()
        try:
            other = B.BrowserSessionService(second, core_instance_id=h.core_id, clock=h.clock)
            token = await h.service.issue_enrollment(principal="local-owner")
            outcomes = await asyncio.gather(h.service.redeem(token.token), other.redeem(token.token))
            require_equal(sum(item is not None for item in outcomes), 1)
            require_equal(await second._run(lambda: second._conn.execute(
                "SELECT COUNT(*) FROM browser_sessions").fetchone()[0]), 1)
        finally:
            await second.close()


async def t_login_requires_exact_configured_https_origin_and_never_accepts_claimed_principal():
    async with _harness() as h:
        token = await h.service.issue_enrollment(principal="local-owner")
        for headers in ({}, {"Origin": "null"}, {"Origin": h.origin + ".evil.test"},
                        {"Origin": h.origin.replace("https:", "http:")},
                        {"Origin": h.origin + "/"}, {"Origin": "https://evil.test"},
                        [("Origin", h.origin), ("Origin", "https://evil.test")]):
            response = await h.client.post(B.SESSION_PATH + "/login", json={"token": token.token},
                                           headers=headers)
            require_equal(response.status, 403)
        forged = await h.client.post(B.SESSION_PATH + "/login",
            json={"token": token.token, "principal": "attacker"}, headers={"Origin": h.origin})
        require_equal(forged.status, 400)
        form = await h.client.post(B.SESSION_PATH + "/login", data={"token": token.token},
                                  headers={"Origin": h.origin})
        require_equal(form.status, 400)
        query = await h.client.post(B.SESSION_PATH + "/login", params={"token": token.token},
            json={}, headers={"Origin": h.origin})
        require_equal(query.status, 400)
        missing = await h.client.post(B.SESSION_PATH + "/issue", json={"principal": "attacker"})
        require_equal(missing.status, 404, "a public issuance endpoint appeared")
        valid = await h.client.post(B.SESSION_PATH + "/login", json={"token": token.token},
                                    headers={"Origin": h.origin})
        require_equal(valid.status, 200, "rejected requests consumed the token")


async def t_plain_http_and_forwarded_proto_never_authenticate_a_browser():
    async with _harness() as h:
        token, response, data = await h.login()
        enrollment = await h.service.issue_enrollment(principal="local-owner")
        app = web.Application()
        B.attach(app, h.service, {h.origin})
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            headers = {"Origin": h.origin, "X-Forwarded-Proto": "https",
                       "Forwarded": "proto=https", "Cookie": B.COOKIE_NAME + "=" + response.cookies[B.COOKIE_NAME].value}
            login = await client.post(B.SESSION_PATH + "/login", json={"token": enrollment.token}, headers=headers)
            require_equal(login.status, 403)
            require_equal((await client.get(B.SESSION_PATH, headers=headers)).status, 401)
            require(await h.service.redeem(enrollment.token) is not None)
        finally:
            await client.close()


async def t_mutation_requires_origin_and_csrf_bound_to_this_live_session():
    async with _harness() as h:
        _, _, first = await h.login()
        client2 = await h.new_client()
        _, _, second = await h.login(client=client2)
        for headers in ({}, {"Origin": h.origin}, {B.CSRF_HEADER: first["csrf_token"]},
                        {"Origin": "https://evil.test", B.CSRF_HEADER: first["csrf_token"]},
                        {"Origin": h.origin, B.CSRF_HEADER: second["csrf_token"]},
                        {"Origin": h.origin, B.CSRF_HEADER: "wrong"},
                        [("Origin", h.origin), (B.CSRF_HEADER, first["csrf_token"]),
                         (B.CSRF_HEADER, second["csrf_token"])]):
            denied = await h.client.post("/mutate", json={"principal": "attacker"}, headers=headers)
            require_equal(denied.status, 401)
        require_equal(h.effects, [])
        valid = await h.client.post("/mutate", json={"principal": "attacker"},
            headers={"Origin": h.origin, B.CSRF_HEADER: first["csrf_token"]})
        require_equal(valid.status, 200)
        require_equal((await valid.json())["principal"], "local-owner")
        require_equal(len(h.effects), 1)


async def t_transport_bearer_enrollment_and_public_session_id_are_not_browser_credentials():
    async with _harness() as h:
        token, response, data = await h.login()
        raw = response.cookies[B.COOKIE_NAME].value
        stranger = await h.new_client()
        iphone = secrets.token_urlsafe(32)
        await h.store.add_enrollment_token(hashlib.sha256(iphone.encode()).hexdigest(), "local-owner", h.clock.now + 600)
        attempts = [{"X-Device-Id": "iphone-owner", "X-Transport-Cred": raw},
                    {"Authorization": "Bearer " + raw}]
        attempts += [{"Cookie": B.COOKIE_NAME + "=" + value}
                     for value in (token.token, iphone, data["session_id"], secrets.token_urlsafe(32))]
        for headers in attempts:
            require_equal((await stranger.get(B.SESSION_PATH, headers=headers)).status, 401)
        require_equal((await stranger.get(B.SESSION_PATH, params={"token": raw})).status, 401)
        require(await h.service.redeem(iphone) is None, "iPhone enrollment became browser enrollment")


async def t_session_persists_with_existing_core_identity_and_fixed_expiry_after_restart():
    with tempfile.TemporaryDirectory(prefix="solvio-browser-restart-") as tmp:
        clock = Clock()
        first = await Harness().init(tmp, lifetime=3600, clock=clock)
        try:
            _, response, data = await first.login()
            raw = response.cookies[B.COOKIE_NAME].value
            original_core = first.core_id
        finally:
            await first.close()
        clock.now += 900
        second = await Harness().init(tmp, lifetime=30 * 86400, clock=clock)
        try:
            require_equal(second.core_id, original_core, "process restart created a new core identity")
            headers = {"Cookie": B.COOKIE_NAME + "=" + raw}
            current = await second.client.get(B.SESSION_PATH, headers=headers)
            require_equal(current.status, 200)
            require_equal(await current.json(), data, "restart renewed expiry or lost session binding")
            clock.now = data["expires_at"]
            require_equal((await second.client.get(B.SESSION_PATH, headers=headers)).status, 401)
        finally:
            await second.close()


async def t_enrollment_expiry_and_core_identity_are_checked_before_consumption():
    async with _harness() as h:
        token = await h.service.issue_enrollment(principal="local-owner")
        other = B.BrowserSessionService(h.store, core_instance_id="core-other", clock=h.clock)
        require(await other.redeem(token.token) is None)
        grant = await h.service.redeem(token.token)
        require(grant is not None)
        require(await other.authenticate(grant.token) is None)
        require(not await other.revoke(grant.actor.session_id, principal="local-owner"))
        require(not await h.service.revoke(grant.actor.session_id, principal="different-owner"))
        require(await h.service.authenticate(grant.token) is not None)
        expired = await h.service.issue_enrollment(principal="local-owner")
        h.clock.now = expired.expires_at
        response = await h.client.post(B.SESSION_PATH + "/login", json={"token": expired.token},
                                       headers={"Origin": h.origin})
        require_equal(response.status, 401)


async def t_logout_requires_csrf_and_revocation_survives_reopen():
    async with _harness() as h:
        _, response, data = await h.login()
        raw = response.cookies[B.COOKIE_NAME].value
        denied = await h.client.post(B.SESSION_PATH + "/logout", headers={"Origin": h.origin})
        require_equal(denied.status, 401)
        require(await h.service.authenticate(raw) is not None)
        logout = await h.client.post(B.SESSION_PATH + "/logout",
            headers={"Origin": h.origin, B.CSRF_HEADER: data["csrf_token"]})
        require_equal(logout.status, 200)
        cookie = logout.cookies[B.COOKIE_NAME]
        require_equal(cookie["max-age"], "0")
        require(cookie["secure"] and cookie["httponly"])
        require_equal(cookie["samesite"], "Strict")
        require_equal((await h.client.get(B.SESSION_PATH)).status, 401)
        reopened = S.ApprovalControlStore(h.path)
        await reopened.open()
        try:
            service = B.BrowserSessionService(reopened, core_instance_id=h.core_id, clock=h.clock)
            require(await service.authenticate(raw) is None)
            require(not await service.revoke(data["session_id"], principal="local-owner"))
        finally:
            await reopened.close()


async def t_session_insert_failure_rolls_back_token_consumption_and_audit():
    async with _harness() as h:
        token = await h.service.issue_enrollment(principal="local-owner")
        await h.store._run(lambda: h.store._conn.execute("CREATE TRIGGER test_fail_session "
            "BEFORE INSERT ON browser_sessions BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"))
        failed = await h.client.post(B.SESSION_PATH + "/login", json={"token": token.token},
                                     headers={"Origin": h.origin})
        require_equal(failed.status, 503)
        consumed = await h.store._run(lambda: h.store._conn.execute(
            "SELECT consumed_at FROM browser_enrollment_tokens").fetchone()[0])
        require_equal(consumed, None, "failed session creation consumed the enrollment token")
        require_equal(sum(e["event"] == "browser_session_created" for e in await h.store.audit_events()), 0)
        await h.store._run(lambda: h.store._conn.execute("DROP TRIGGER test_fail_session"))
        retried = await h.client.post(B.SESSION_PATH + "/login", json={"token": token.token},
                                      headers={"Origin": h.origin})
        require_equal(retried.status, 200)


async def t_read_only_open_never_migrates_and_migrated_read_only_sessions_never_write():
    with tempfile.TemporaryDirectory(prefix="solvio-browser-migration-") as tmp:
        path = os.path.join(tmp, "approval_control.sqlite3")
        st = S.ApprovalControlStore(path)
        await st.open()
        await st._run(lambda: st._conn.executescript("DROP TABLE browser_sessions; DROP TABLE browser_enrollment_tokens;"))
        await st.close()
        before = Path(path).read_bytes()
        ro = S.ApprovalControlStore(path, read_only=True)
        try:
            try:
                await ro.open()
            except S.StateMigrationRequired:
                pass
            else:
                require(False, "read-only admin accepted an unmigrated session schema")
        finally:
            await ro.close()
        require_equal(Path(path).read_bytes(), before)
        writable = S.ApprovalControlStore(path)
        await writable.open()
        service = B.BrowserSessionService(writable, core_instance_id="core-stable")
        token = await service.issue_enrollment(principal="local-owner")
        grant = await service.redeem(token.token)
        require(grant is not None)
        await writable.close()
        ro = S.ApprovalControlStore(path, read_only=True)
        await ro.open()
        try:
            service = B.BrowserSessionService(ro, core_instance_id="core-stable")
            require_equal(await service.authenticate(grant.token), grant.actor)
            try:
                await service.issue_enrollment(principal="local-owner")
            except S.ApprovalStoreError:
                pass
            else:
                require(False, "read-only session service wrote an enrollment token")
        finally:
            await ro.close()


def t_origins_and_session_lifetime_are_explicit_finite_configuration():
    for origins in ([], "https://localhost", {"*"}, {"http://localhost"},
                    {"https://*.example.test"}, {"https://owner@example.test"},
                    {"https://example.test/"}, {"https://example.test?a=1"},
                    {"https://example.test#fragment"}, {"https://example.test\n"}):
        require_raises(ValueError, B._trusted_origins, origins)
    for lifetime in (0, -1, float("inf"), float("nan"), "300", True, 366 * 86400):
        require_raises(ValueError, B.BrowserSessionService, None,
                       core_instance_id="core-stable", session_lifetime_s=lifetime)
    require_equal(B._trusted_origins({"https://localhost:8770"}), frozenset({"https://localhost:8770"}))


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
