"""Actual pinned old auth/store source against new rows; no emulated old validator."""
import hashlib
import asyncio
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from aiohttp import CookieJar, TCPConnector, web
from aiohttp.test_utils import TestClient, TestServer, unused_port
from solvio.security.mobile_approval import browser_sessions as B, observer_contract as O, store as S
from test_browser_sessions import _tls

from _public_source_fixture import load_module


def old_module(name):
    return load_module('observer_legacy/' + name + '.py',
                       'solvio.security.mobile_approval._observer_old_' + name)


def snapshot(path):
    with sqlite3.connect(path) as c:
        schema = c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        rows = {table: sorted(c.execute('SELECT * FROM ' + table).fetchall(), key=repr) for table in tables}
    return schema, rows


async def t_unconsumed_code_and_cookie_rejected_by_actual_old_https_login_and_actor():
    old = old_module('browser_sessions')
    with tempfile.TemporaryDirectory(prefix='solvio-observer-legacy-') as directory:
        path = str(Path(directory) / 'approval.sqlite3')
        store = S.ApprovalControlStore(path); await store.open()
        port = unused_port(); origin = f'https://127.0.0.1:{port}'
        new = B.BrowserSessionService(store, core_instance_id='core-compat',
            observer_origin=origin, observer_tls_fingerprint='a' * 64)
        unopened = await new.issue_observer_enrollment(principal='local-owner')
        other = await new.issue_observer_enrollment(principal='local-owner')
        observer = await new.redeem_observer(other.token, origin=origin)
        legacy = old.BrowserSessionService(store, core_instance_id='core-compat')
        app = web.Application(); old.attach(app, legacy, {origin})
        server_tls, client_tls = _tls(directory)
        server = TestServer(app, port=port, scheme='https'); await server.start_server(ssl=server_tls)
        client = TestClient(server, cookie_jar=CookieJar(unsafe=True), connector=TCPConnector(ssl=client_tls))
        await client.start_server()
        before = snapshot(path)
        try:
            response = await client.post(old.SESSION_PATH + '/login', json={'token': unopened.token},
                                         headers={'Origin': origin})
            require_equal(response.status, 401); require_equal(await response.json(), {'error': 'invalid_enrollment'})
            require(old.COOKIE_NAME not in response.cookies)
            for name, token in [(O.COOKIE_NAME, observer.token), (old.COOKIE_NAME, observer.token),
                                (old.COOKIE_NAME, observer.token[len(O.SESSION_PREFIX):])]:
                response = await client.get(old.SESSION_PATH, headers={'Cookie': f'{name}={token}'})
                require_equal(response.status, 401)
            # Stripping the visible prefix cannot convert the hash domain either.
            response = await client.post(old.SESSION_PATH + '/login',
                json={'token': unopened.token[len(O.ENROLLMENT_PREFIX):]}, headers={'Origin': origin})
            require_equal(response.status, 401)
            require_equal(snapshot(path), before)
            require(await new.redeem_observer(unopened.token, origin=origin) is not None)
        finally:
            await client.close(); await store.close()


async def t_old_new_old_new_preserves_observer_rows_old_owner_and_locked_files():
    old_s, old_b = old_module('store'), old_module('browser_sessions')
    with tempfile.TemporaryDirectory(prefix='solvio-observer-roundtrip-') as directory:
        path = str(Path(directory) / 'approval.sqlite3')
        document = Path(directory) / 'owner-document.txt'
        document.write_bytes(b'Existing bound document\x00exact bytes'); document.chmod(0o400)
        old_store = old_s.ApprovalControlStore(path); await old_store.open()
        old_service = old_b.BrowserSessionService(old_store, core_instance_id='core-roundtrip')
        owner_code = await old_service.issue_enrollment(principal='local-owner')
        owner = await old_service.redeem(owner_code.token)
        await old_store.close()
        unmigrated_bytes = Path(path).read_bytes()
        readonly = S.ApprovalControlStore(path, read_only=True)
        try:
            try: await readonly.open()
            except S.StateMigrationRequired: pass
            else: raise AssertionError('new read-only opener silently migrated')
        finally: await readonly.close()
        require_equal(Path(path).read_bytes(), unmigrated_bytes)
        new_store = S.ApprovalControlStore(path); await new_store.open()
        origin = 'https://127.0.0.1:8770'
        new = B.BrowserSessionService(new_store, core_instance_id='core-roundtrip',
            observer_origin=origin, observer_tls_fingerprint='b' * 64)
        require((await new.authenticate(owner.token)) is not None)
        unopened = await new.issue_observer_enrollment(principal='local-owner')
        used = await new.issue_observer_enrollment(principal='local-owner')
        observer = await new.redeem_observer(used.token, origin=origin)
        gone = await new.issue_observer_enrollment(principal='local-owner')
        revoked = await new.redeem_observer(gone.token, origin=origin)
        await new.revoke(revoked.actor.session_id, principal='local-owner')
        await new_store.close()
        before = snapshot(path)
        bytes_before = Path(path).read_bytes()
        modes_before = [stat.S_IMODE(Path(p).stat().st_mode) for p in (path, document)]
        # Actual old store in restricted read-only mode accepts additive schema,
        # preserves new data and still cannot authenticate the new credentials.
        rollback = old_s.ApprovalControlStore(path, read_only=True); await rollback.open()
        try:
            old = old_b.BrowserSessionService(rollback, core_instance_id='core-roundtrip')
            require(await old.authenticate(owner.token) is not None)
            require(await old.authenticate(observer.token) is None)
            require(await old.redeem(unopened.token) is None)
        finally: await rollback.close()
        require_equal(snapshot(path), before); require_equal(Path(path).read_bytes(), bytes_before)
        require_equal([stat.S_IMODE(Path(p).stat().st_mode) for p in (path, document)], modes_before)
        require_equal(document.read_bytes(), b'Existing bound document\x00exact bytes')
        again = S.ApprovalControlStore(path); await again.open()
        try:
            restored = B.BrowserSessionService(again, core_instance_id='core-roundtrip',
                observer_origin=origin, observer_tls_fingerprint='b' * 64)
            require(await restored.authenticate(owner.token) is not None)
            require(await restored.authenticate_observer(observer.token, origin=origin) is not None)
            require(await restored.authenticate_observer(revoked.token, origin=origin) is None)
            require(await restored.redeem_observer(used.token, origin=origin) is None)
            require_equal(snapshot(path), before, 'idempotent reopen rewrote canonical rows')
        finally: await again.close()


async def t_purpose_tamper_and_digest_domain_swaps_do_not_create_owner_authority():
    with tempfile.TemporaryDirectory(prefix='solvio-observer-domain-') as directory:
        store = S.ApprovalControlStore(str(Path(directory) / 'approval.sqlite3')); await store.open()
        service = B.BrowserSessionService(store, core_instance_id='core-domain',
            observer_origin='https://127.0.0.1:8770', observer_tls_fingerprint='a' * 64)
        try:
            code = await service.issue_observer_enrollment(principal='local-owner')
            session = await service.redeem_observer(code.token, origin=service.observer_origin)
            await store._run(lambda: store._conn.execute(
                "UPDATE browser_sessions SET purpose='owner',audience_origin='',tls_fingerprint='' WHERE session_id=?",
                (session.actor.session_id,)))
            require(await service.authenticate(session.token) is None)
            require(await service.authenticate_observer(session.token, origin=service.observer_origin) is None)
            require(await service.authenticate(session.token[len(O.SESSION_PREFIX):]) is None)
        finally: await store.close()


async def t_two_writable_openers_migrate_old_schema_once_without_reclassifying_owner():
    old_s, old_b = old_module('store'), old_module('browser_sessions')
    with tempfile.TemporaryDirectory(prefix='solvio-observer-migrate-') as directory:
        path = str(Path(directory) / 'approval.sqlite3')
        old = old_s.ApprovalControlStore(path); await old.open()
        service = old_b.BrowserSessionService(old, core_instance_id='core-migrate')
        code = await service.issue_enrollment(principal='local-owner')
        owner = await service.redeem(code.token)
        await old.close()
        first, second = S.ApprovalControlStore(path), S.ApprovalControlStore(path)
        try:
            await asyncio.gather(first.open(), second.open())
            for store in (first, second):
                current = B.BrowserSessionService(store, core_instance_id='core-migrate')
                require(await current.authenticate(owner.token) is not None)
                rows = await store._run(lambda: [tuple(r) for r in store._conn.execute(
                    'SELECT purpose,audience_origin,tls_fingerprint FROM browser_sessions')])
                require_equal(rows, [('owner', '', '')])
        finally:
            await first.close(); await second.close()


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
