"""Google iPhone login through existing encrypted staging, approval and Vault.

This module never creates authority. Its activation method is called only by
the VERY_CRITICAL google_connect capability after the existing router decides.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from solvio.secret_vault import admin, envelope as E, policy as P
from solvio.secret_vault.google_reconnect import CLIENT_REF, REFRESH_REF, SCOPES, TOKEN_URI
from solvio.secret_vault.store import VaultStore, utcnow_iso

IOS_CLIENT = os.environ.get("SOLVIO_GOOGLE_IOS_CLIENT_ID", "configure-ios-client-id.apps.googleusercontent.com")
SERVER_CLIENT = os.environ.get("SOLVIO_GOOGLE_SERVER_CLIENT_ID", "configure-server-client-id.apps.googleusercontent.com")
EXPORT = Path(os.environ.get("SOLVIO_GOOGLE_MOBILE_CONFIG", str(Path.home() / ".solvio-nexus/credentials/google-mobile/web-client.json")))
RUNTIME = Path(os.environ.get("SOLVIO_GOOGLE_OAUTH_RUNTIME", str(Path.home() / ".solvio-nexus/runtimes/google-oauth/bin/python")))


class GoogleConnectionError(RuntimeError):
    """Fixed reason identifiers only, no raw SDK/HTTP/configuration content."""


def _config(path: Path) -> tuple[dict, str]:
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            parent = path.parent.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
                raise ValueError
            raw = handle.read(16385)
        if not raw or len(raw) > 16384:
            raise ValueError
        source = json.loads(raw)["web"]
        if (source["client_id"] != SERVER_CLIENT or source["token_uri"] != TOKEN_URI
                or source["auth_uri"] != "https://accounts.google.com/o/oauth2/auth"
                or not isinstance(source["client_secret"], str)
                or not 0 < len(source["client_secret"].encode()) <= 8192):
            raise ValueError
        narrowed = {"web": {key: source[key] for key in ("client_id", "client_secret", "token_uri", "auth_uri")}}
        return narrowed, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, KeyError, TypeError):
        raise GoogleConnectionError("google_mobile_setup_unavailable") from None


async def _exchange(config: dict, code: str, account_id: str, account_email: str) -> str:
    if not RUNTIME.is_file():
        raise GoogleConnectionError("google_mobile_setup_unavailable")
    worker = Path(__file__).with_name("google_mobile_exchange.py")
    process = await asyncio.create_subprocess_exec(str(RUNTIME), "-I", "-B", str(worker),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
             "SOLVIO_GOOGLE_SERVER_CLIENT_ID": SERVER_CLIENT}, start_new_session=True)
    try:
        body = json.dumps({"config": config, "code": code, "account_id": account_id,
                           "account_email": account_email}).encode()
        raw, _stderr = await asyncio.wait_for(process.communicate(body), timeout=40)
        if process.returncode != 0 or len(raw) > 16384:
            raise GoogleConnectionError("google_exchange_unconfirmed")
        reply = json.loads(raw)
        refresh = reply.get("refresh_token")
        if reply.get("ok") is not True or not isinstance(refresh, str) or not 0 < len(refresh.encode()) <= 8192:
            raise GoogleConnectionError("google_exchange_unconfirmed")
        return refresh
    except (ValueError, TypeError, asyncio.TimeoutError):
        raise GoogleConnectionError("google_exchange_unconfirmed") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class GoogleMobileConnection:
    def __init__(self, store: VaultStore, *, config_path: Path = EXPORT, exchange=None):
        self.store = store
        self.config_path = config_path
        self.exchange = exchange or _exchange

    def _expected(self):
        result = {}
        for ref, kind in ((CLIENT_REF, P.SecretKind.OAUTH_CLIENT_SECRET),
                          (REFRESH_REF, P.SecretKind.OAUTH_REFRESH_TOKEN)):
            row = self.store.row(ref)
            if row is None:
                raise GoogleConnectionError("google_existing_access_missing")
            policy = P.from_row(row)
            if policy.kind is not kind or row["policy_sha256"] != policy.digest():
                raise GoogleConnectionError("google_existing_access_invalid")
            result[ref] = policy
        return result

    @staticmethod
    def _binding(expected):
        value = {"client_id": SERVER_CLIENT, "credentials": [
            [ref, expected[ref].version, expected[ref].digest()] for ref in sorted(expected)]}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def preview(self):
        _config(self.config_path)
        if not RUNTIME.is_file():
            raise GoogleConnectionError("google_mobile_setup_unavailable")
        expected = self._expected()
        return {"client_id": IOS_CLIENT, "server_client_id": SERVER_CLIENT,
                "scopes": list(SCOPES), "expected_binding": self._binding(expected),
                "capabilities": sorted(set.intersection(*(set(p.allowed_capabilities) for p in expected.values()))),
                "currently_active": all(p.status is P.Status.ACTIVE for p in expected.values())}

    async def activate(self, arguments: dict, raw: bytes):
        if set(arguments) != {"expected_binding", "account_id", "account_email", "staging_id"}:
            raise GoogleConnectionError("google_connection_invalid")
        account_id, account_email = arguments["account_id"], arguments["account_email"]
        if (not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,255}", account_id)
                or not isinstance(account_email, str) or not 0 < len(account_email.encode()) <= 320
                or any(ord(c) < 32 or ord(c) == 127 for c in account_email)):
            raise GoogleConnectionError("google_connection_invalid")
        try:
            code = raw.decode("ascii")
        except UnicodeError:
            raise GoogleConnectionError("google_connection_invalid") from None
        if not re.fullmatch(r"[!-~]{1,8192}", code):
            raise GoogleConnectionError("google_connection_invalid")
        expected = self._expected()
        if arguments["expected_binding"] != self._binding(expected):
            raise GoogleConnectionError("google_connection_changed")
        config, fingerprint = _config(self.config_path)
        # One code exchange, never a retry. A failure leaves the old pair intact.
        refresh = await self.exchange(config, code, account_id, account_email)
        if _config(self.config_path)[1] != fingerprint:
            raise GoogleConnectionError("google_connection_changed")
        values = {}
        kek = admin._kek_or_raise()
        try:
            for ref, text in ((CLIENT_REF, config["web"]["client_secret"]), (REFRESH_REF, refresh)):
                if not isinstance(text, str) or not 0 < len(text.encode()) <= admin.MAX_SECRET_BYTES:
                    raise GoogleConnectionError("google_exchange_unconfirmed")
                before = expected[ref]
                following = replace(before, version=before.version + 1, status=P.Status.ACTIVE, rotated_at=utcnow_iso())
                sealed = E.seal(kek=kek, ref=ref, version=following.version,
                                policy_sha256=following.digest(), plaintext=text.encode())
                values[ref] = (following, sealed)
        finally:
            del kek
        self.store.rotate_google_pair_if_current(values, expected=expected, client_id=SERVER_CLIENT, account_label=account_email)
        return {"verbunden": True, "dienste": ["Gmail", "Google Kalender"],
                "info": "Google ist für SOLVIO verbunden. Gmail und Kalender verwenden den bestätigten Zugang."}
