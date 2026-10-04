"""Official SDK, real loopback HTTP, synthetic browser and token transport.

The Core interpreter delegates each fixed case to the isolated admin runtime
when the optional SDK is absent. Missing runtime is a failure, never a skip.
"""
from __future__ import annotations
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_google_reconnect import G, world, plan, CLIENT, SECRET, OLD, NEW, value


def probe(*, wrong_state=False, redirect_token=False, existing_desktop=False):
    if importlib.util.find_spec('google_auth_oauthlib') is None:
        if os.environ.get('SOLVIO_GOOGLE_OAUTH_SDK_CHILD') == '1':
            raise AssertionError('official SDK absent from configured isolated runtime')
        executable = os.environ.get('SOLVIO_GOOGLE_OAUTH_TEST_PYTHON') or str(
            Path(__file__).resolve().parents[2] / 'google-oauth-runtime' / 'bin' / 'python')
        require(Path(executable).is_file(), 'separate Google OAuth test runtime is required')
        case = 'wrong_state' if wrong_state else 'redirect_token' if redirect_token else 'success'
        if existing_desktop:
            case = 'existing_' + case
        env = dict(os.environ)
        env.pop('PYTHONPATH', None); env.pop('PYTHONHOME', None)
        env['SOLVIO_GOOGLE_OAUTH_SDK_CHILD'] = '1'
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        result = subprocess.run([executable, str(Path(__file__).resolve()), '--sdk-case', case],
                                capture_output=True, text=True, env=env, timeout=30)
        require_equal(result.returncode, 0, 'isolated official SDK case failed: ' + result.stdout[-1600:] + result.stderr[-1600:])
        require_equal(result.stdout.splitlines()[-1], 'SOLVIO-GOOGLE-SDK-PASSED:' + case)
        return
    from requests import Response
    from requests.adapters import BaseAdapter
    with world() as w:
        p = (G.prepare_existing_desktop(confirmed_client_id=CLIENT, client_id=CLIENT, store=w.store)
             if existing_desktop else plan(w))
        posts = []; callbacks = []; errors = []; threads = []; opened = []; destinations = []

        class TokenPort(BaseAdapter):
            def send(self, request, **kwargs):
                destinations.append(request.url)
                require_equal(request.url, G.TOKEN_URI)
                require_equal(request.method, 'POST')
                body = parse_qs(request.body)
                # requests-oauthlib 2.0 sends the original client pair with
                # HTTP Basic authentication, not again in the form body.
                require_equal(request.headers['Authorization'], 'Basic ' +
                    base64.b64encode((CLIENT + ':' + SECRET).encode()).decode())
                require_equal(body['code'], ['synthetic-callback-code'])
                verifier = body['code_verifier'][0]
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
                require_equal(challenge, opened[0]['code_challenge'][0])
                require_equal(body['redirect_uri'], opened[0]['redirect_uri'])
                require_equal(kwargs['timeout'], (10, 30))
                posts.append('fixed_google_token_endpoint')
                response = Response(); response.status_code = 200
                response.headers['Content-Type'] = 'application/json'; response.request = request
                response._content = json.dumps({'access_token': 'synthetic-sdk-access',
                    'refresh_token': NEW, 'scope': ' '.join(G.SCOPES),
                    'token_type': 'Bearer', 'expires_in': 3600}).encode()
                if redirect_token:
                    response.status_code = 307
                    response.headers['Location'] = 'https://foreign-token.invalid/steal'
                    response._content = b'{}'
                return response
            def close(self): pass

        class Browser:
            def open(self, url, **kwargs):
                parts = urlsplit(url)
                require(parts.scheme + '://' + parts.netloc + parts.path in G.AUTH_URIS)
                query = parse_qs(parts.query); opened.append(query)
                require_equal(set(query['scope'][0].split()), set(G.SCOPES))
                require_equal(query['access_type'], ['offline'])
                require_equal(query['prompt'], ['consent'])
                require_equal(query['code_challenge_method'], ['S256'])
                uri = query['redirect_uri'][0]; redirect = urlsplit(uri)
                require_equal(redirect.hostname, '127.0.0.1')
                require(redirect.port > 0)
                state = 'wrong-fixture-state' if wrong_state else query['state'][0]
                def respond():
                    try:
                        with urlopen(uri + '?' + urlencode({'code': 'synthetic-callback-code',
                                                            'state': state}), timeout=10) as response:
                            callbacks.append(response.status); response.read()
                    except Exception as exc: errors.append(type(exc).__name__)
                thread = threading.Thread(target=respond); threads.append(thread); thread.start()
                return True

        def factory(p, check):
            flow = G._official_flow(p, check)
            flow.oauth2session.mount('https://', TokenPort())
            return flow

        with patch('webbrowser.get', return_value=Browser()):
            try:
                result = G.reconnect(p, store=w.store, client_id_reader=lambda: CLIENT, flow_factory=factory)
            except G.ReconnectError as exc:
                require(wrong_state or redirect_token, 'valid local SDK flow unexpectedly failed')
                require_equal(str(exc), 'google_login_not_completed')
            else:
                require(not wrong_state and not redirect_token, 'invalid OAuth exchange unexpectedly accepted')
                require_equal(result['version'], 2)
            finally:
                for thread in threads: thread.join(timeout=10)
        require_equal(errors, [])
        require_equal(callbacks, [200])
        require_equal(posts, [] if wrong_state else ['fixed_google_token_endpoint'])
        require_equal(destinations, [] if wrong_state else [G.TOKEN_URI])
        require_equal(value(w.store, G.REFRESH_REF), OLD if wrong_state or redirect_token else NEW.encode())
        require(not list(w.root.rglob('*token*.json')))


def t_official_sdk_loopback_pkce_and_exact_token_exchange():
    probe()


def t_official_sdk_wrong_state_never_exchanges_or_replaces():
    probe(wrong_state=True)


def t_official_sdk_token_redirect_never_forwards_code_or_verifier():
    probe(redirect_token=True)


def t_existing_desktop_official_sdk_loopback_pkce_and_exact_token_exchange():
    probe(existing_desktop=True)


def t_existing_desktop_official_sdk_wrong_state_never_exchanges_or_replaces():
    probe(wrong_state=True, existing_desktop=True)


def t_existing_desktop_official_sdk_token_redirect_never_forwards_code_or_verifier():
    probe(redirect_token=True, existing_desktop=True)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--sdk-case':
        case = sys.argv[2]
        require(os.environ.get('SOLVIO_GOOGLE_OAUTH_SDK_CHILD') == '1')
        require(case in {'success', 'wrong_state', 'redirect_token',
                        'existing_success', 'existing_wrong_state', 'existing_redirect_token'})
        probe(wrong_state=case.endswith('wrong_state'), redirect_token=case.endswith('redirect_token'),
              existing_desktop=case.startswith('existing_'))
        print('SOLVIO-GOOGLE-SDK-PASSED:' + case)
        raise SystemExit(0)
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
