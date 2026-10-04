"""Private stdin/stdout SDK worker, run only after the Core's Face-ID decision.

No HTTP server, agent tool, credentials file or raw error output. The parent
captures the result pipe directly into the existing encrypted Vault workflow.
Uses the separately installed official SDK; never changes production .venv.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys

SCOPES = ("https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.compose")
TOKEN_URI = "https://oauth2.googleapis.com/token"
SERVER_CLIENT = os.environ.get("SOLVIO_GOOGLE_SERVER_CLIENT_ID", "configure-server-client-id.apps.googleusercontent.com")


def exchange(payload):
    from importlib.metadata import version
    if any(version(name) != expected for name, expected in (
            ("google-auth-oauthlib", "1.3.1"), ("google-auth", "2.58.0"), ("requests-oauthlib", "2.0.0"))):
        raise ValueError("sdk_version")
    from google_auth_oauthlib.flow import Flow
    from google.oauth2 import id_token
    from google.auth.transport.requests import Request
    import requests

    config = payload["config"]
    web = config["web"]
    if (web["client_id"] != SERVER_CLIENT or web["token_uri"] != TOKEN_URI
            or web["auth_uri"] != "https://accounts.google.com/o/oauth2/auth"):
        raise ValueError("configuration")
    flow = Flow.from_client_config(config, scopes=list(SCOPES), redirect_uri="")
    flow.oauth2session.max_redirects = 0
    try:
        os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
        flow.fetch_token(code=payload["code"], timeout=(10, 15))
        token = flow.oauth2session.token
        scopes = token.get("scope", [])
        if isinstance(scopes, str):
            scopes = scopes.split()
        identity = {"openid", "email", "profile", "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/userinfo.profile"}
        if (not isinstance(scopes, list) or not set(SCOPES).issubset(scopes)
                or not set(scopes).issubset(set(SCOPES) | identity)):
            raise ValueError("scope")
        refresh = token.get("refresh_token")
        raw_id = token.get("id_token")
        if (not isinstance(refresh, str) or not 0 < len(refresh.encode()) <= 8192
                or not isinstance(raw_id, str) or not raw_id):
            raise ValueError("incomplete")
        # Verify signature, issuer, audience and expiry with Google's official
        # implementation. Matching the SDK's account is checked again here.
        with requests.Session() as certs:
            certs.max_redirects = 0
            verified = id_token.verify_oauth2_token(raw_id, Request(session=certs), SERVER_CLIENT)
        if (verified.get("sub") != payload["account_id"]
                or verified.get("email") != payload["account_email"]
                or verified.get("email_verified") is not True):
            raise ValueError("account")
        return {"refresh_token": refresh}
    finally:
        flow.oauth2session.close()


def main():
    logging.disable(logging.CRITICAL)
    try:
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise ValueError("size")
        payload = json.loads(raw)
        # Third-party diagnostic prints must not mix with the secret result pipe.
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            result = exchange(payload)
        sys.stdout.write(json.dumps({"ok": True, **result}))
        return 0
    except Exception:
        # No SDK exception, URL, code, provider body or token reaches diagnostics.
        sys.stdout.write('{"ok":false,"reason":"google_exchange_unconfirmed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
