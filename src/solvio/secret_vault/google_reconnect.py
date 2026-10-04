"""Lokaler Owner-Helfer um Googles offizielles OAuth-SDK, kein Agentenwerkzeug.

Clienttyp/Redirect kommen aus dem Originalexport oder der ausdruecklichen
lokalen Operatorangabe fuer einen zuvor in der Konsole geprueften Desktopclient.
Letztere ist kein kryptografischer Google-Nachweis. Nur die bestehende
Core-Client-ID und das Tresorgeheimnis werden benutzt. Keine Token-Datei,
keine Scope-Wahl, keine neue Clientregistrierung und keine automatische Nutzung.

SDK-Vertrag: google-auth-oauthlib==1.3.1, InstalledAppFlow.run_local_server.
https://googleapis.dev/python/google-auth-oauthlib/latest/_modules/google_auth_oauthlib/flow.html
https://developers.google.com/identity/protocols/oauth2/native-app
"""
from __future__ import annotations

import hmac
import json
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from solvio.secret_vault import admin, envelope as E, policy as VP
from solvio.secret_vault.store import VaultStore, VaultStoreError

CLIENT_REF = "secret://google/oauth-client"
REFRESH_REF = "secret://google/refresh"
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URIS = frozenset({"https://accounts.google.com/o/oauth2/auth",
                       "https://accounts.google.com/o/oauth2/v2/auth"})
SCOPES = ("https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.compose")


class ReconnectError(RuntimeError):
    """Nur feste Fehlerkennungen; niemals SDK-Fehlertexte oder Credentialwerte."""


@dataclass(frozen=True)
class LoginPlan:
    client_type: str
    host: str
    port: int
    trailing_slash: bool
    _config: str = field(repr=False)
    _expected: tuple[VP.SecretPolicy, ...] = field(repr=False)
    _client_id: str = field(repr=False)
    client_type_source: str = "original_client_export"

    @property
    def client_config(self) -> dict:
        return json.loads(self._config)

    @property
    def expected(self) -> dict[str, VP.SecretPolicy]:
        return {p.secret_ref: p for p in self._expected}


def _rows(store: VaultStore) -> dict[str, dict]:
    rows = {}
    for ref, kind in ((CLIENT_REF, VP.SecretKind.OAUTH_CLIENT_SECRET),
                      (REFRESH_REF, VP.SecretKind.OAUTH_REFRESH_TOKEN)):
        row = store.row(ref)
        if row is None:
            raise ReconnectError("google_vault_entry_missing")
        p = VP.from_row(row)
        if (p.status is not VP.Status.ACTIVE or p.kind is not kind
                or row["policy_sha256"] != p.digest()):
            raise ReconnectError("google_vault_binding_invalid")
        rows[ref] = row
    return rows


def prepare(*, client_config: dict, client_id: str, store: VaultStore,
            redirect_uri: str = "") -> LoginPlan:
    """Prueft lokalen Export/Tresor, ohne Browser, Netzwerk oder Schreibvorgang."""
    if (not isinstance(client_config, dict) or len(client_config) != 1
            or next(iter(client_config), "") not in {"installed", "web"}):
        raise ReconnectError("original_google_client_export_required")
    kind = next(iter(client_config))
    config = client_config[kind]
    if not isinstance(config, dict):
        raise ReconnectError("original_google_client_export_required")
    if (not client_id or not isinstance(config.get("client_id"), str)
            or config["client_id"] != client_id
            or not isinstance(config.get("client_secret"), str)
            or not config["client_secret"]):
        raise ReconnectError("original_google_client_mismatch")
    if config.get("auth_uri") not in AUTH_URIS or config.get("token_uri") != TOKEN_URI:
        raise ReconnectError("google_oauth_endpoint_invalid")
    redirects = config.get("redirect_uris")
    if (not isinstance(redirects, list) or not redirects
            or any(not isinstance(v, str) for v in redirects)):
        raise ReconnectError("original_google_redirect_configuration_required")
    if kind == "installed":
        if redirect_uri:
            raise ReconnectError("desktop_redirect_is_sdk_owned")
        # Desktop-Loopback-Ports sind laut Google dynamisch. Die OS-Zuteilung
        # wird vom offiziellen SDK in genau dessen Redirect eingesetzt.
        host, port, trailing = "127.0.0.1", 0, True
    else:
        if not redirect_uri or redirect_uri not in redirects:
            raise ReconnectError("registered_web_loopback_redirect_required")
        try:
            url = urlsplit(redirect_uri)
            port = url.port
            if (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1"}
                    or not port or not 1024 <= port <= 65535
                    or url.path not in {"", "/"} or url.query or url.fragment
                    or url.username or url.password
                    or redirect_uri != f"http://{url.hostname}:{port}{url.path}"):
                raise ValueError
            host, trailing = url.hostname, url.path == "/"
        except (ValueError, TypeError):
            raise ReconnectError("registered_web_loopback_redirect_required") from None
    rows = _rows(store)
    p = VP.from_row(rows[CLIENT_REF])
    row = rows[CLIENT_REF]
    secret = b""
    kek = admin._kek_or_raise()
    try:
        secret = E.unseal(kek=kek, ref=CLIENT_REF, version=p.version,
                          policy_sha256=p.digest(), sealed=E.Sealed(
                              row["envelope_version"], bytes(row["wrapped_dek"]),
                              bytes(row["ciphertext"])))
        if not hmac.compare_digest(secret, config["client_secret"].encode("utf-8")):
            raise ReconnectError("original_google_client_mismatch")
    finally:
        del secret, kek
    # Reconstruct a narrow SDK config: unknown export fields cannot supply
    # scope, callback handlers, alternate URLs or another credential store.
    narrow = {kind: {key: config[key] for key in
                    ("client_id", "client_secret", "auth_uri", "token_uri", "redirect_uris")}}
    return LoginPlan(kind, host, port, trailing, json.dumps(narrow),
                     tuple(VP.from_row(rows[r]) for r in (CLIENT_REF, REFRESH_REF)), client_id)


def prepare_existing_desktop(*, confirmed_client_id: str, client_id: str,
                             store: VaultStore) -> LoginPlan:
    """Vorhandener Desktopclient; explizite Operatorangabe, kein Typ-Autodetektor.

    Google erlaubt den erneuten Secret-Download bestehender Clients nicht mehr.
    Die lokale ID muss nach Konsolevergleich ausdruecklich uebergeben werden.
    Das vorhandene Vaultsecret gelangt nur in die SDK-Konfiguration im Speicher.
    Fuer Webclients gibt es hier weder einen Fallback noch geratene Redirects.
    """
    if (not isinstance(confirmed_client_id, str) or confirmed_client_id != client_id
            or not re.fullmatch(r'[A-Za-z0-9._-]{1,256}\.apps\.googleusercontent\.com', client_id)):
        raise ReconnectError("confirmed_desktop_client_mismatch")
    bound_client = store.meta_get("google_oauth_client_id")
    if bound_client and bound_client != client_id:
        raise ReconnectError("confirmed_desktop_client_mismatch")
    rows = _rows(store)
    row = rows[CLIENT_REF]
    p = VP.from_row(row)
    secret = b""
    kek = admin._kek_or_raise()
    try:
        secret = E.unseal(kek=kek, ref=CLIENT_REF, version=p.version,
                          policy_sha256=p.digest(), sealed=E.Sealed(
                              row["envelope_version"], bytes(row["wrapped_dek"]),
                              bytes(row["ciphertext"])))
        decoded = secret.decode("utf-8")
        if not decoded or len(secret) > admin.MAX_SECRET_BYTES:
            raise ReconnectError("google_vault_binding_invalid")
        # No original redirect list is invented. run_local_server owns the
        # ephemeral loopback redirect for this explicitly declared Desktop type.
        narrow = {"installed": {"client_id": client_id, "client_secret": decoded,
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth", "token_uri": TOKEN_URI}}
        encoded = json.dumps(narrow)
    finally:
        del secret, kek
    return LoginPlan("installed", "127.0.0.1", 0, True, encoded,
        tuple(VP.from_row(rows[r]) for r in (CLIENT_REF, REFRESH_REF)), client_id,
        "local_operator_desktop_confirmation")


def ensure_current(plan: LoginPlan, store: VaultStore, client_id: str) -> None:
    if client_id != plan._client_id:
        raise ReconnectError("google_binding_changed")
    rows = _rows(store)
    for before in plan._expected:
        actual = VP.from_row(rows[before.secret_ref])
        if actual.version != before.version or actual.digest() != before.digest():
            raise ReconnectError("google_binding_changed")


@contextmanager
def _quiet_sdk():
    # Das offizielle SDK protokolliert andernfalls die Callback-Query. Dieser
    # Helfer laeuft als eigener lokaler Prozess, nicht im produktiven Core.
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def _official_flow(plan: LoginPlan, check: Callable[[], None]):
    try:
        from importlib.metadata import version
        if version('google-auth-oauthlib') != '1.3.1':
            raise ReconnectError("isolated_google_oauth_runtime_version_mismatch")
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise ReconnectError("isolated_google_oauth_runtime_required") from None

    class BoundFlow(InstalledAppFlow):
        def fetch_token(self, **kwargs):
            check()
            kwargs["timeout"] = (10, 30)
            return super().fetch_token(**kwargs)

    flow = BoundFlow.from_client_config(plan.client_config, scopes=list(SCOPES),
                                        autogenerate_code_verifier=True)
    # requests-oauthlib 2.0 forwards unknown fetch_token kwargs into the form
    # body. Therefore allow_redirects=False there would NOT stop redirects.
    # The actual Requests session refuses any redirect before a second request.
    flow.oauth2session.max_redirects = 0
    return flow


def reconnect(plan: LoginPlan, *, store: VaultStore,
              client_id_reader: Callable[[], str],
              flow_factory: Callable | None = None) -> dict:
    """Explizit gestartete lokale Owner-Anmeldung; speichert nur Refresh im Vault.

    flow_factory ist ein lokaler Testport. Es gibt keine HTTP-/Modellroute,
    keinen automatischen Aufruf und keinen zweiten Token-Dauerspeicher.
    """
    check = lambda: ensure_current(plan, store, client_id_reader())
    check()
    flow = None
    try:
        with _quiet_sdk():
            flow = (flow_factory or _official_flow)(plan, check)
            credentials = flow.run_local_server(
                host=plan.host, bind_addr="127.0.0.1", port=plan.port,
                redirect_uri_trailing_slash=plan.trailing_slash,
                authorization_prompt_message=None,
                success_message="Google-Antwort angekommen. Den Abschluss zeigt das SOLVIO-Fenster am Mac.",
                open_browser=True, timeout_seconds=300,
                access_type="offline", prompt="consent", include_granted_scopes="false")
        check()
        token = flow.oauth2session.token
        scopes = token.get("scope")
        if isinstance(scopes, str):
            scopes = scopes.split()
        granted = getattr(credentials, "granted_scopes", None)
        if isinstance(granted, str):
            granted = granted.split()
        if (not isinstance(scopes, (list, tuple)) or set(scopes) != set(SCOPES)
                or not isinstance(granted, (list, tuple)) or set(granted) != set(SCOPES)):
            raise ReconnectError("google_granted_scopes_mismatch")
        refresh = getattr(credentials, "refresh_token", None)
        if (not isinstance(refresh, str) or not refresh or len(refresh.encode()) > admin.MAX_SECRET_BYTES
                or credentials.client_id != plan._client_id
                or credentials.token_uri != TOKEN_URI
                or credentials.client_secret != plan.client_config[plan.client_type]["client_secret"]
                or not isinstance(token.get("access_token"), str) or not token["access_token"]
                or token.get("refresh_token") != refresh):
            raise ReconnectError("google_credentials_incomplete")
        check()
        result = admin.replace_value_if_current(
            secret_ref=REFRESH_REF, plaintext=refresh.encode(), expected=plan.expected, store=store)
        return {"status": "reconnected", "secret_ref": REFRESH_REF, "version": result.version}
    except ReconnectError:
        raise
    except VaultStoreError:
        raise ReconnectError("google_binding_changed") from None
    except Exception:
        # OAuth errors may contain code, URL, request body or token. None of
        # that belongs in the terminal, a traceback or a support message.
        raise ReconnectError("google_login_not_completed") from None
    finally:
        if flow is not None:
            session = getattr(flow, "oauth2session", None)
            if session is not None:
                session.close()
