#!/usr/bin/env python3
"""Google erneut verbinden, lokal am Mac und ausschliesslich auf Owner-Aufruf.

Benutzt einen separat eingerichteten Python mit google-oauth-requirements.txt.
Standard ist --check: weder Browser/Netzwerk noch Tresorschreibung. --connect
startet den echten Google-Dialog und ersetzt nach dessen Pruefung den Refresh-
Eintrag im bestehenden Core-Tresor. Export/Token werden niemals kopiert.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def read_binding_settings(path, environ=None):
    """Exact dotenv parsing, no silently ignored or ambiguous selected entry."""
    from dotenv.parser import parse_stream
    from solvio.secret_vault.google_reconnect import ReconnectError
    wanted = {'google_calendar_client_id', 'solvio_vault_dir'}
    values = {}
    with Path(path).open() as stream:
        for binding in parse_stream(stream):
            if binding.error:
                raise ReconnectError('google_core_configuration_invalid')
            if binding.key is None:
                continue
            key = binding.key.lower()
            if (key.strip() in wanted and key != key.strip()) or any(
                    key.startswith(name + ':') for name in wanted):
                raise ReconnectError('google_core_configuration_invalid')
            if key in wanted:
                if key in values or binding.value is None:
                    raise ReconnectError('google_core_configuration_ambiguous')
                values[key] = binding.value
    overrides = {}
    for raw_key, value in (os.environ if environ is None else environ).items():
        key = raw_key.lower()
        if key in wanted:
            if key in overrides or not isinstance(value, str):
                raise ReconnectError('google_core_configuration_ambiguous')
            overrides[key] = value
    values.update(overrides)
    client_id = values.get('google_calendar_client_id', '')
    directory = values.get('solvio_vault_dir') or '~/.solvio-vault'
    if (not re.fullmatch(r'[A-Za-z0-9._-]{1,256}\.apps\.googleusercontent\.com', client_id)
            or '\x00' in directory or '\n' in directory or '\r' in directory
            or '$' in directory or not (directory.startswith('/') or directory.startswith('~/'))):
        raise ReconnectError('google_core_configuration_invalid')
    return client_id, Path(directory).expanduser().resolve()


def main(argv=None):
    parser = argparse.ArgumentParser(description='SOLVIO Google-Anmeldung am Mac')
    binding = parser.add_mutually_exclusive_group()
    binding.add_argument('--client-json', type=Path,
                        help='Originaler Google-Client-Export, bleibt an seinem bisherigen Ort')
    binding.add_argument('--desktop-client-id',
                        help='Originale Client-ID; Operator hat genau diesen Client in Google Cloud als Desktop geprueft')
    parser.add_argument('--core-env', type=Path, default=ROOT / '.env')
    parser.add_argument('--redirect', default='', help='Nur Web-Client: exakt registrierter Loopback-Redirect')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Nur lokale Vorbedingungen pruefen (Standard)')
    mode.add_argument('--connect', action='store_true', help='Jetzt am Mac ueber Google anmelden')
    args = parser.parse_args(argv)
    if args.client_json is None and args.desktop_client_id is None:
        print('Originalen Client-Export auswaehlen oder die in der Google-Konsole bestaetigte '
              'Desktop-Client-ID ausdruecklich angeben. Die ID muss mit dem bestehenden Core '
              'uebereinstimmen. Kein neues Secret anlegen; keine Geheimnisse oder Codes in den Chat kopieren.')
        return 2
    try:
        from solvio.secret_vault import google_reconnect as G
        from solvio.secret_vault.store import VaultStore

        def settings():
            return read_binding_settings(args.core_env)

        client_id, vault_dir = settings()

        class ExistingVault(VaultStore):
            def __init__(self):
                # Do not initialize a vault, run schema migrations or change
                # permissions merely because the Owner checks a client export.
                self.path = str(vault_dir / 'vault.sqlite3')
                if not Path(self.path).is_file() or not self.permissions_ok():
                    raise G.ReconnectError('existing_google_vault_required')
            @contextmanager
            def _open(self):
                uri = Path(self.path).as_uri() + ('?mode=rw' if args.connect else '?mode=ro')
                connection = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
                connection.row_factory = sqlite3.Row
                try: yield connection
                finally: connection.close()

        def current_client_id():
            current_id, current_vault = settings()
            if current_vault != vault_dir:
                raise G.ReconnectError('google_binding_changed')
            return current_id

        store = ExistingVault()
        if args.desktop_client_id is not None:
            if args.redirect:
                raise G.ReconnectError('desktop_redirect_is_sdk_owned')
            plan = G.prepare_existing_desktop(confirmed_client_id=args.desktop_client_id,
                                             client_id=client_id, store=store)
        else:
            with args.client_json.open('rb') as source:
                data = source.read(16 * 1024 + 1)
            if len(data) > 16 * 1024:
                raise G.ReconnectError('original_google_client_export_required')
            document = json.loads(data)
            del data
            plan = G.prepare(client_config=document, client_id=client_id, store=store, redirect_uri=args.redirect)
            del document
        G.ensure_current(plan, store, current_client_id())
        if not args.connect:
            print(json.dumps({'status': 'ready_for_owner_login', 'client_type': plan.client_type,
                'client_type_source': plan.client_type_source, 'client_type_verified_by_helper': False,
                'client_id_matches_core': True,
                'redirect': 'sdk_loopback' if plan.client_type == 'installed' else 'registered_loopback',
                'requested_scopes': list(G.SCOPES), 'browser_opened': False, 'vault_changed': False}))
            return 0
        print('Google-Anmeldung startet im Browser dieses Macs. Nach der Zustimmung hier auf den Abschluss warten.', flush=True)
        result = G.reconnect(plan, store=store, client_id_reader=current_client_id)
        print(json.dumps(result))
        print('Anmeldung im bestehenden Tresor ersetzt. Bei einem wartenden Auftrag im Dashboard '
              'das erneuerte Konto ausdruecklich auswaehlen; keine neue Aufgabe anlegen.')
        return 0
    except Exception as exc:
        # Never print an SDK exception, JSON document, path content or traceback.
        reason = (str(exc) if 'G' in locals() and isinstance(exc, G.ReconnectError)
                  else 'google_login_preparation_failed')
        print(json.dumps({'status': 'not_completed', 'reason': reason}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
