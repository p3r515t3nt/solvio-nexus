"""Atomic mobile Google credential switch, synthetic encrypted stores only."""
from dataclasses import replace
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_google_reconnect import world, value
from solvio.secret_vault import envelope as E, keyring as K, policy as P, google_reconnect as G
from solvio.secret_vault.store import VaultStoreError, utcnow_iso

CLIENT = "synthetic-mobile.apps.googleusercontent.com"
ACCOUNT = "synthetic@example.invalid"


def prepare(w):
    before = {ref: w.store.policy(ref) for ref in (G.CLIENT_REF, G.REFRESH_REF)}
    values = {}
    for ref, old in before.items():
        following = replace(old, version=old.version + 1, rotated_at=utcnow_iso())
        sealed = E.seal(kek=K.read_kek(), ref=ref, version=following.version,
                        policy_sha256=following.digest(), plaintext=b"synthetic-new-" + ref.encode())
        values[ref] = (following, sealed)
    return before, values


def apply(w, before, values):
    w.store.rotate_google_pair_if_current(values, expected=before, client_id=CLIENT, account_label=ACCOUNT)


def refused(fn):
    try:
        fn()
    except VaultStoreError as exc:
        require(str(exc).startswith("google_rotation_binding_"))
    else:
        raise AssertionError("changed Google binding accepted")


def snapshot(w):
    return ({r: value(w.store, r) for r in (G.CLIENT_REF, G.REFRESH_REF)},
            {r: w.store.policy(r) for r in (G.CLIENT_REF, G.REFRESH_REF)},
            w.store.meta_get("google_oauth_client_id"), w.store.ledger())


def t_pair_changes_together_retains_rights_and_replay_is_refused():
    with world() as w:
        before, values = prepare(w)
        apply(w, before, values)
        require_equal(w.store.meta_get("google_oauth_client_id"), CLIENT)
        for ref, old in before.items():
            current = w.store.policy(ref)
            require_equal(current.digest(), replace(old, version=old.version + 1).digest())
            require_equal(value(w.store, ref), b"synthetic-new-" + ref.encode())
            require_equal(current.account_label, ACCOUNT)
        after = snapshot(w)
        refused(lambda: apply(w, before, values))
        require_equal(snapshot(w), after)


def t_database_failure_after_first_write_rolls_back_pair_metadata_and_ledger():
    with world() as w:
        before, values = prepare(w)
        previous = snapshot(w)
        with w.store._open() as connection:
            connection.execute("""CREATE TRIGGER fixture_fail_second BEFORE UPDATE ON secrets
                WHEN NEW.secret_ref = 'secret://google/refresh'
                BEGIN SELECT RAISE(ABORT, 'synthetic write fault'); END""")
        try:
            apply(w, before, values)
        except sqlite3.Error:
            pass
        else:
            raise AssertionError("synthetic database fault did not fire")
        require_equal(snapshot(w), previous)


def t_missing_changed_or_revoked_entry_never_half_switches():
    from solvio.secret_vault import admin
    for action in ("delete", "rotate", "disable"):
        with world() as w:
            before, values = prepare(w)
            if action == "delete":
                w.store.delete(G.REFRESH_REF)
            elif action == "rotate":
                admin.replace_value(secret_ref=G.REFRESH_REF, plaintext=b"synthetic-concurrent", store=w.store)
            else:
                admin.set_status(secret_ref=G.REFRESH_REF, status=P.Status.DISABLED, store=w.store)
            old_client = value(w.store, G.CLIENT_REF)
            ledger = w.store.ledger()
            refused(lambda: apply(w, before, values))
            require_equal(value(w.store, G.CLIENT_REF), old_client)
            require_equal(w.store.meta_get("google_oauth_client_id"), "")
            require_equal(w.store.ledger(), ledger)


def t_pair_cannot_expand_scopes_or_change_kind():
    for field in ("allowed_capabilities", "kind", "version", "secret_ref"):
        with world() as w:
            before, values = prepare(w)
            changes = {"allowed_capabilities": ("gmail_send",), "kind": P.SecretKind.API_TOKEN,
                       "version": 99, "secret_ref": "secret://different/ref"}
            policy, sealed = values[G.CLIENT_REF]
            values[G.CLIENT_REF] = (replace(policy, **{field: changes[field]}), sealed)
            previous = snapshot(w)
            refused(lambda: apply(w, before, values))
            require_equal(snapshot(w), previous)


async def t_existing_clients_drop_warm_tokens_and_use_the_new_client_pair():
    from unittest.mock import patch
    import time
    from solvio.secret_vault.broker import SecretBroker, SecretDenied
    from solvio.secret_vault import context as SC, admin
    from solvio.capabilities.policy import OriginClass
    from solvio.integrations.gmail import Gmail
    from solvio.integrations.google_calendar import GoogleCalendar
    for cls, capability in ((Gmail, "gmail_create_draft"), (GoogleCalendar, "calendar_create_event")):
        with world() as w, SC.bound(SC.UseContext(origin=OriginClass.TRUSTED_INTERACTIVE_APP,
                                                  capability=capability, user_present=True)):
            client = cls(client_id="old.apps.googleusercontent.com", broker=SecretBroker(w.store))
            client._access_token = "synthetic-old-access"
            client._expires_at = time.monotonic() + 3600
            client._token_credential_versions = client._credential_versions()
            require_equal(await client._token(), "synthetic-old-access")
            before, values = prepare(w)
            apply(w, before, values)
            calls = []

            class Reply:
                status = 200
                async def __aenter__(self): return self
                async def __aexit__(self, *_): pass
                async def json(self, **_): return {"access_token": "synthetic-new-access", "expires_in": 3600}

            class Session:
                def __init__(self, **_): pass
                async def __aenter__(self): return self
                async def __aexit__(self, *_): pass
                def post(self, url, *, data):
                    require_equal(url, G.TOKEN_URI)
                    calls.append(dict(data))
                    return Reply()

            with patch("aiohttp.ClientSession", Session):
                require_equal(await client._token(), "synthetic-new-access")
                require_equal(calls, [{"client_id": CLIENT,
                    "client_secret": "synthetic-new-" + G.CLIENT_REF,
                    "refresh_token": "synthetic-new-" + G.REFRESH_REF, "grant_type": "refresh_token"}])
                require_equal(await client._token(), "synthetic-new-access")
                require_equal(len(calls), 1)
                admin.set_status(secret_ref=G.REFRESH_REF, status=P.Status.DISABLED, store=w.store)
                try:
                    await client._token()
                except SecretDenied:
                    pass
                else:
                    raise AssertionError("disabled Google entry accepted a warm access token")
                require_equal(len(calls), 1)


def t_action_account_identity_follows_public_client_binding_without_changing_legacy_shape():
    from solvio.capabilities import task_action as TA
    from solvio.integrations.gmail import Gmail
    from solvio.secret_vault.broker import SecretBroker
    with world() as w:
        client = Gmail(client_id="legacy.apps.googleusercontent.com", broker=SecretBroker(w.store))
        original = TA._identity("gmail", client)
        require_equal(original["client_id"], "legacy.apps.googleusercontent.com")
        require(all("oauth_client_id" not in item for item in original["credentials"]))
        before, values = prepare(w);apply(w, before, values)
        current = TA._identity("gmail", client)
        require_equal(current["client_id"], CLIENT)
        require_equal(current["credentials"][0]["oauth_client_id"], CLIENT)
        require(current != original)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
