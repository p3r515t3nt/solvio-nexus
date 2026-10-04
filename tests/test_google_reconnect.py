"""Local Owner reconnect: real temporary Vault; synthetic OAuth counterparts."""
from __future__ import annotations
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
import logging
import os
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.secret_vault import admin, google_reconnect as G, keyring as K, envelope as E, policy as P
from solvio.secret_vault.store import VaultStore

CLIENT = 'fixture.apps.googleusercontent.com'
SECRET = 'synthetic-client-only-fixture'
OLD = b'synthetic-old-refresh'
NEW = 'synthetic-new-refresh'


@contextmanager
def world():
    with tempfile.TemporaryDirectory(prefix='solvio-google-reconnect-') as d:
        with patch.dict(os.environ, {'SOLVIO_VAULT_DIR': d + '/vault',
                                    'SOLVIO_VAULT_TEST_KEYSTORE': d + '/keys'}):
            K.forget_kek()
            store = VaultStore()
            admin.initialize(store)
            for ref, kind, value in ((G.CLIENT_REF, P.SecretKind.OAUTH_CLIENT_SECRET, SECRET.encode()),
                                     (G.REFRESH_REF, P.SecretKind.OAUTH_REFRESH_TOKEN, OLD)):
                admin.add(secret_ref=ref, kind=kind, plaintext=value, store=store,
                    allowed_capabilities=('calendar_create_event', 'gmail_create_draft'),
                    allowed_targets=('https://oauth2.googleapis.com',),
                    allowed_executors=(P.ExecutorId.HTTP,), allow_background=True,
                    requires_user_presence=True, display_name='Display', service_label='Google',
                    account_label='Owner label', note='Keep this note')
            yield SimpleNamespace(store=store, root=Path(d))
            K.forget_kek()


def config(kind='installed'):
    return {kind: {'client_id': CLIENT, 'client_secret': SECRET,
                   'auth_uri': 'https://accounts.google.com/o/oauth2/auth', 'token_uri': G.TOKEN_URI,
                   'redirect_uris': ['http://localhost'] if kind == 'installed' else ['http://127.0.0.1:8497/']}}


def plan(w, **kwargs):
    return G.prepare(client_config=config(), client_id=CLIENT, store=w.store, **kwargs)


def value(store, ref):
    row = store.row(ref); p = P.from_row(row)
    return E.unseal(kek=K.read_kek(), ref=ref, version=p.version,
                    policy_sha256=p.digest(), sealed=E.Sealed(row['envelope_version'],
                    bytes(row['wrapped_dek']), bytes(row['ciphertext'])))


class SyntheticFlow:
    def __init__(self, p, check):
        self.plan = p; self.check = check; self.options = None; self.closed = False
        self.oauth2session = SimpleNamespace(token={
            'scope': list(G.SCOPES), 'access_token': 'synthetic-access', 'refresh_token': NEW},
            close=self.close)
        self.credentials = SimpleNamespace(refresh_token=NEW, client_id=CLIENT,
            client_secret=SECRET, token_uri=G.TOKEN_URI, granted_scopes=list(G.SCOPES))
        self.before = lambda: None
    def close(self): self.closed = True
    def run_local_server(self, **kwargs):
        self.options = kwargs
        self.before()
        self.check()
        return self.credentials


def expect_error(fn, reason):
    try: fn()
    except G.ReconnectError as exc: require_equal(str(exc), reason)
    else: raise AssertionError('reconnect unexpectedly accepted')


def t_original_client_export_type_secret_and_fixed_endpoints_required():
    with world() as w:
        for document in ({}, {'client_secret': SECRET}, {'installed': {}, 'web': {}}):
            expect_error(lambda: G.prepare(client_config=document, client_id=CLIENT, store=w.store),
                         'original_google_client_export_required')
        for key, bad in [('client_id', 'different'), ('client_secret', 'different')]:
            c = config(); c['installed'][key] = bad
            expect_error(lambda: G.prepare(client_config=c, client_id=CLIENT, store=w.store),
                         'original_google_client_mismatch')
        for key in ('auth_uri', 'token_uri'):
            c = config(); c['installed'][key] = 'https://elsewhere.invalid/token'
            expect_error(lambda: G.prepare(client_config=c, client_id=CLIENT, store=w.store),
                         'google_oauth_endpoint_invalid')
        require_equal(value(w.store, G.REFRESH_REF), OLD)
        require_equal(w.store.policy(G.REFRESH_REF).version, 1)


def t_web_requires_exact_registered_local_root_redirect():
    with world() as w:
        for uri in ('', 'http://127.0.0.1:8498/', 'http://127.0.0.1:8497',
                    'http://evil.invalid:8497/', 'http://127.0.0.1:8497/callback'):
            expect_error(lambda: G.prepare(client_config=config('web'), client_id=CLIENT,
                store=w.store, redirect_uri=uri), 'registered_web_loopback_redirect_required')
        p = G.prepare(client_config=config('web'), client_id=CLIENT, store=w.store,
                      redirect_uri='http://127.0.0.1:8497/')
        require_equal((p.host, p.port, p.trailing_slash), ('127.0.0.1', 8497, True))
        require_equal(plan(w).port, 0)


def t_complete_consent_replaces_only_refresh_preserving_current_rights_and_labels():
    with world() as w:
        p = plan(w); prior = w.store.row(G.REFRESH_REF)
        flow = SyntheticFlow(p, lambda: None)
        # Non-authority metadata and last_used_at may change during consent.
        with w.store._open() as c:
            c.execute('UPDATE secrets SET account_label=?,last_used_at=? WHERE secret_ref=?',
                      ('New owner label', '2099-01-01T00:00:00+00:00', G.REFRESH_REF))
        result = G.reconnect(p, store=w.store, client_id_reader=lambda: CLIENT,
                             flow_factory=lambda p, check: flow)
        following = w.store.row(G.REFRESH_REF)
        require_equal(result, {'status': 'reconnected', 'secret_ref': G.REFRESH_REF, 'version': 2})
        require_equal(value(w.store, G.REFRESH_REF), NEW.encode())
        for key in ('kind', 'status', 'allowed_capabilities', 'allowed_targets', 'allowed_executors',
                    'allow_background', 'requires_user_presence', 'display_name', 'service_label',
                    'note', 'created_at'):
            require_equal(following[key], prior[key], key)
        require_equal(following['account_label'], 'New owner label')
        require_equal(following['last_used_at'], '2099-01-01T00:00:00+00:00')
        require_equal(w.store.policy(G.CLIENT_REF).version, 1)
        require_equal(w.store.ledger(secret_ref=G.REFRESH_REF)[0]['capability'], 'secret_replace')
        require(flow.closed)
        require_equal(flow.options['include_granted_scopes'], 'false')
        require_equal(flow.options['authorization_prompt_message'], None)
        require_equal(flow.options['timeout_seconds'], 300)
        require(not list(w.root.rglob('*token*.json')))


def t_granted_scope_mismatch_or_missing_refresh_never_replaces_vault():
    for mode in ('missing', 'partial', 'broader', 'missing_refresh', 'different_client'):
        with world() as w:
            p = plan(w); f = SyntheticFlow(p, lambda: None)
            if mode == 'missing': f.oauth2session.token.pop('scope')
            elif mode == 'partial': f.credentials.granted_scopes = list(G.SCOPES[:-1])
            elif mode == 'broader': f.oauth2session.token['scope'].append('https://www.googleapis.com/auth/drive')
            elif mode == 'missing_refresh': f.credentials.refresh_token = ''
            else: f.credentials.client_id = 'other'
            reason = 'google_granted_scopes_mismatch' if mode in ('missing', 'partial', 'broader') else 'google_credentials_incomplete'
            expect_error(lambda: G.reconnect(p, store=w.store, client_id_reader=lambda: CLIENT,
                flow_factory=lambda p, check: f), reason)
            require_equal(value(w.store, G.REFRESH_REF), OLD)
            require_equal(w.store.policy(G.REFRESH_REF).version, 1)


def t_rotation_or_revocation_during_consent_cannot_be_overwritten():
    for ref in (G.CLIENT_REF, G.REFRESH_REF):
        for mode in ('rotate', 'revoke'):
            with world() as w:
                p = plan(w); f = SyntheticFlow(p, lambda: None)
                def intervene():
                    if mode == 'rotate':
                        admin.replace_value(secret_ref=ref, plaintext=b'synthetic-intervening', store=w.store)
                    else: admin.set_status(secret_ref=ref, status=P.Status.REVOKED, store=w.store)
                f.before = intervene
                expect_error(lambda: G.reconnect(p, store=w.store, client_id_reader=lambda: CLIENT,
                    flow_factory=lambda p, check: f),
                    'google_binding_changed' if mode == 'rotate' else 'google_vault_binding_invalid')
                if mode == 'rotate' and ref == G.REFRESH_REF:
                    require_equal(value(w.store, G.REFRESH_REF), b'synthetic-intervening')
                else: require_equal(value(w.store, G.REFRESH_REF), OLD)


def t_atomic_compare_catches_rotation_after_last_preflight():
    for ref in (G.CLIENT_REF, G.REFRESH_REF):
        with world() as w:
            p = plan(w); original = w.store.rotate_if_current
            def race(policy, sealed, *, expected):
                admin.replace_value(secret_ref=ref, plaintext=b'synthetic-race-winner', store=w.store)
                return original(policy, sealed, expected=expected)
            with patch.object(w.store, 'rotate_if_current', race):
                expect_error(lambda: G.reconnect(p, store=w.store, client_id_reader=lambda: CLIENT,
                    flow_factory=SyntheticFlow), 'google_binding_changed')
            require_equal(value(w.store, G.REFRESH_REF),
                b'synthetic-race-winner' if ref == G.REFRESH_REF else OLD)


def t_replay_and_changed_core_client_id_cannot_replace_a_newer_login():
    with world() as w:
        p = plan(w)
        expect_error(lambda: G.reconnect(p, store=w.store, client_id_reader=lambda: 'other',
            flow_factory=SyntheticFlow), 'google_binding_changed')
        G.reconnect(p, store=w.store, client_id_reader=lambda: CLIENT, flow_factory=SyntheticFlow)
        expect_error(lambda: G.reconnect(p, store=w.store, client_id_reader=lambda: CLIENT,
            flow_factory=SyntheticFlow), 'google_binding_changed')
        require_equal(w.store.policy(G.REFRESH_REF).version, 2)


def t_provider_errors_and_callback_logs_do_not_expose_credentials():
    with world() as w:
        p = plan(w); f = SyntheticFlow(p, lambda: None); out = io.StringIO()
        def fail():
            logging.getLogger('google_auth_oauthlib.flow').critical('secret-code=' + NEW)
            raise RuntimeError('token=' + NEW)
        f.before = fail
        with redirect_stdout(out), redirect_stderr(out):
            expect_error(lambda: G.reconnect(p, store=w.store, client_id_reader=lambda: CLIENT,
                flow_factory=lambda p, check: f), 'google_login_not_completed')
        require(NEW not in out.getvalue()); require(SECRET not in repr(p))
        require_equal(value(w.store, G.REFRESH_REF), OLD)


def t_cli_check_reads_only_existing_binding_without_flow_or_mutation():
    with world() as w:
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'google_reconnect.py'
        main = runpy.run_path(str(script), run_name='google_reconnect_cli_test')['main']
        export = w.root / 'original-client.json'; export.write_text(json.dumps(config()))
        env = w.root / 'core.env'
        env.write_text('GOOGLE_CALENDAR_CLIENT_ID=' + CLIENT + '\nSOLVIO_VAULT_DIR=' + str(w.root / 'vault'))
        out = io.StringIO(); before = w.store.row(G.REFRESH_REF)
        with patch.object(G, 'reconnect', side_effect=AssertionError('check cannot log in')), redirect_stdout(out):
            result = main(['--check', '--client-json', str(export), '--core-env', str(env)])
        require_equal(result, 0)
        require_equal(json.loads(out.getvalue())['browser_opened'], False)
        require_equal(w.store.row(G.REFRESH_REF), before)
        require(SECRET not in out.getvalue())
        out = io.StringIO(); export.write_text('{bad JSON ' + SECRET)
        with redirect_stdout(out):
            result = main(['--check', '--client-json', str(export), '--core-env', str(env)])
        require_equal(result, 2)
        require_equal(json.loads(out.getvalue())['reason'], 'google_login_preparation_failed')
        require(SECRET not in out.getvalue())


def t_cli_selected_configuration_refuses_invalid_duplicate_or_interpolated_values():
    with world() as w:
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'google_reconnect.py'
        read = runpy.run_path(str(script), run_name='google_config_test')['read_binding_settings']
        env = w.root / 'configuration.env'
        good = 'export GOOGLE_CALENDAR_CLIENT_ID="' + CLIENT + '"\nSOLVIO_VAULT_DIR="' + str(w.root / 'vault dir') + '"\n'
        env.write_text(good)
        require_equal(read(env, {}), (CLIENT, (w.root / 'vault dir').resolve()))
        for suffix in ('GOOGLE_CALENDAR_CLIENT_ID="unclosed',
                       'GOOGLE_CALENDAR_CLIENT_ID wrong syntax',
                       'GOOGLE_CALENDAR_CLIENT_ID:\n',
                       '"google_calendar_client_id "=wrong',
                       'SOLVIO_VAULT_DIR="${HOME}/vault"'):
            env.write_text(good + suffix)
            try: read(env, {})
            except G.ReconnectError: pass
            else: raise AssertionError('ambiguous or invalid native configuration silently accepted')
        env.write_text(good + 'google_calendar_client_id=' + CLIENT)
        expect_error(lambda: read(env, {}), 'google_core_configuration_ambiguous')
        env.write_text(good)
        expect_error(lambda: read(env, {'GOOGLE_CALENDAR_CLIENT_ID': CLIENT,
                                       'google_calendar_client_id': CLIENT}),
                     'google_core_configuration_ambiguous')


def t_existing_desktop_uses_original_vault_without_export_or_new_secret():
    with world() as w:
        before = w.store.row(G.CLIENT_REF)
        p = G.prepare_existing_desktop(confirmed_client_id=CLIENT, client_id=CLIENT, store=w.store)
        require_equal(p.client_type_source, 'local_operator_desktop_confirmation')
        require_equal((p.client_type, p.host, p.port), ('installed', '127.0.0.1', 0))
        require('redirect_uris' not in p.client_config['installed'], 'no original redirects were measured')
        require_equal(p.client_config['installed']['client_secret'], SECRET)
        result = G.reconnect(p, store=w.store, client_id_reader=lambda: CLIENT, flow_factory=SyntheticFlow)
        require_equal(result['version'], 2)
        require_equal(w.store.row(G.CLIENT_REF), before)
        require_equal(value(w.store, G.REFRESH_REF), NEW.encode())
        require(not list(w.root.rglob('*.json')))
        require(SECRET not in repr(p))


def t_existing_desktop_rejects_stale_id_and_credential_rotation_before_save():
    with world() as w:
        for given in ('', 'different.apps.googleusercontent.com', CLIENT + ' '):
            expect_error(lambda: G.prepare_existing_desktop(confirmed_client_id=given,
                client_id=CLIENT, store=w.store), 'confirmed_desktop_client_mismatch')
        require_equal(w.store.policy(G.REFRESH_REF).version, 1)
    for ref in (G.CLIENT_REF, G.REFRESH_REF):
        for stage in ('before_token', 'before_cas'):
            with world() as w:
                p = G.prepare_existing_desktop(confirmed_client_id=CLIENT, client_id=CLIENT, store=w.store)
                original = w.store.rotate_if_current
                def rotate():
                    admin.replace_value(secret_ref=ref, plaintext=b'synthetic-desktop-intervening', store=w.store)
                if stage == 'before_token':
                    f = SyntheticFlow(p, lambda: None); f.before = rotate
                    expect_error(lambda: G.reconnect(p, store=w.store, client_id_reader=lambda: CLIENT,
                        flow_factory=lambda p, check: f), 'google_binding_changed')
                else:
                    def race(policy, sealed, *, expected):
                        rotate()
                        return original(policy, sealed, expected=expected)
                    with patch.object(w.store, 'rotate_if_current', race):
                        expect_error(lambda: G.reconnect(p, store=w.store, client_id_reader=lambda: CLIENT,
                            flow_factory=SyntheticFlow), 'google_binding_changed')
                require_equal(value(w.store, G.REFRESH_REF),
                    b'synthetic-desktop-intervening' if ref == G.REFRESH_REF else OLD)


def t_cli_existing_desktop_is_explicit_read_only_confirmation_not_google_verification():
    with world() as w:
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'google_reconnect.py'
        main = runpy.run_path(str(script), run_name='google_desktop_cli_test')['main']
        env = w.root / 'core.env'; env.write_text('GOOGLE_CALENDAR_CLIENT_ID=' + CLIENT)
        args = ['--check', '--desktop-client-id', CLIENT, '--core-env', str(env)]
        before = w.store.row(G.REFRESH_REF); out = io.StringIO()
        with patch.object(G, 'reconnect', side_effect=AssertionError('check must not connect')), redirect_stdout(out):
            require_equal(main(args), 0)
        result = json.loads(out.getvalue())
        require_equal(result['client_type_source'], 'local_operator_desktop_confirmation')
        require_equal(result['client_type_verified_by_helper'], False)
        require_equal(result['client_id_matches_core'], True)
        require_equal(result['browser_opened'], False)
        require_equal(w.store.row(G.REFRESH_REF), before)
        out = io.StringIO()
        with redirect_stdout(out):
            require_equal(main(args + ['--redirect', 'http://127.0.0.1:8497/']), 2)
        require_equal(json.loads(out.getvalue())['reason'], 'desktop_redirect_is_sdk_owned')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try: main(args + ['--client-json', '/unused.json'])
            except SystemExit as exc: require_equal(exc.code, 2)
            else: raise AssertionError('ambiguous client source accepted')


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
