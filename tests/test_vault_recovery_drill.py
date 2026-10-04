"""Exercise the recovery CLI with disposable secrets and file-only keys."""
import atexit
import contextlib
import hashlib
import io
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'scripts'), str(ROOT / 'tests')]
from _guard import enforce_assertions, require, require_equal, require_raises

enforce_assertions()
SANDBOX = Path(tempfile.mkdtemp(prefix='solvio-recovery-cli-'))
atexit.register(shutil.rmtree, SANDBOX, True)
os.environ['SOLVIO_VAULT_DIR'] = str(SANDBOX / 'initial-vault')
os.environ['SOLVIO_VAULT_TEST_KEYSTORE'] = str(SANDBOX / 'initial-keys')

from solvio.capabilities import policy as AP
from solvio.secret_vault import admin, broker, context, keyring, policy, recovery
from solvio.secret_vault.store import VaultStore
import vault_admin
import vault_drill

PASSPHRASE = 'synthetic-recovery-phrase-only'
VALUE = b'synthetic-recovery-material-only'


def _forbid_keychain(*args, **kwargs):
    raise AssertionError('The recovery tests must never reach macOS Keychain')


@contextlib.contextmanager
def _case():
    root = Path(tempfile.mkdtemp(dir=SANDBOX))
    with patch.dict(os.environ, {
        'SOLVIO_VAULT_DIR': str(root / 'prepared-vault'),
        'SOLVIO_VAULT_TEST_KEYSTORE': str(root / 'prepared-keys'),
    }), patch.object(keyring, '_security', _forbid_keychain):
        store = VaultStore()
        admin.initialize(store)
        entry = admin.add(
            secret_ref='secret://example/recovery', kind=policy.SecretKind.PASSWORD,
            plaintext=VALUE, allowed_capabilities=('portal_login',),
            allowed_targets=('https://example.invalid',),
            allowed_executors=(policy.ExecutorId.BROWSER,),
            requires_user_presence=True, store=store)
        recovery.write(recovery.build(PASSPHRASE, keyring.read_kek()))
        snapshot = root / 'snapshot' / 'Vault'
        snapshot.mkdir(parents=True, mode=0o700)
        original = root / 'prepared-vault'
        with sqlite3.connect((original / 'vault.sqlite3').as_uri() + '?mode=ro', uri=True) as source:
            with sqlite3.connect(snapshot / 'vault.sqlite3') as destination:
                source.backup(destination)
        shutil.copyfile(original / 'recovery.json', snapshot / 'recovery.json')
        yield root, snapshot, store, entry


def _run(root, response):
    output = io.StringIO()
    with patch.object(vault_admin, 'ask_secret', return_value=response) as ask, \
            patch.object(vault_drill, '_aufzeichnen'), \
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        code = vault_drill.main(['--from', str(root / 'snapshot'),
                                 '--into', str(root / 'restore')])
    return code, output.getvalue(), ask.call_count


def t_recovery_restores_an_entry_requiring_owner_presence():
    with _case() as (root, snapshot, _store, _entry):
        before = {p.name: hashlib.sha256(p.read_bytes()).digest() for p in snapshot.iterdir()}
        code, output, asked = _run(root, PASSPHRASE)
        require_equal(code, 0)
        require_equal(asked, 1)
        require('[1] Platte allein oeffnet den Tresor: NEIN' in output)
        require('[3] 1 von 1 Zugaengen wieder benutzbar.' in output)
        require(VALUE.decode() not in output)
        require(PASSPHRASE not in output)
        require(not (root / 'restore').exists())
        require_equal(before, {p.name: hashlib.sha256(p.read_bytes()).digest() for p in snapshot.iterdir()})


def t_wrong_recovery_passphrase_does_not_install_a_key_or_recover():
    with _case() as (root, _snapshot, _store, _entry):
        with patch.object(keyring, 'install_kek') as install, \
                patch.object(vault_drill, '_disposable_executor') as use:
            code, output, asked = _run(root, 'synthetic-incorrect-recovery')
        require_equal(code, 1)
        require_equal(asked, 1)
        require_equal(install.call_count, 0)
        require_equal(use.call_count, 0)
        require('Umschlag oeffnet nicht' in output)
        require(not (root / 'restore').exists())


def t_cancelled_recovery_does_not_install_a_key_or_recover():
    with _case() as (root, _snapshot, _store, _entry):
        with patch.object(keyring, 'install_kek') as install, \
                patch.object(vault_drill, '_disposable_executor') as use:
            code, _output, asked = _run(root, '')
        require_equal(code, 2)
        require_equal(asked, 1)
        require_equal(install.call_count, 0)
        require_equal(use.call_count, 0)
        require(not (root / 'restore').exists())


def t_the_same_entry_remains_denied_without_presence_outside_the_drill():
    with _case() as (_root, _snapshot, store, entry):
        with context.bound(context.UseContext(
                origin=AP.OriginClass.LOCAL_OWNER, capability='portal_login')):
            require_raises(broker.SecretDenied, vault_drill._disposable_executor,
                           broker.SecretBroker(store), entry)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
