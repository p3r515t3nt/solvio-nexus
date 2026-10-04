"""Der Tresor. Alias hinein, Geheimnis heraus — und sonst nirgendwohin.

Zwei Ablagen waren moeglich, und die Wahl ist begruendet.

Der **Schluesselbund allein** haette funktioniert, aber er ist fuer die
Isolationsmechanik dieses Projekts unsichtbar: alles andere Geheime ist ueber
einen Pfad versiegelt, den ein Test nachpruefen kann. Ein Schluesselbund-Eintrag
ist kein Pfad. Er waere Verschluesselung im Ruhezustand ohne jede Trennung
gegenueber einem Arbeiterprozess.

Eine **Datei allein** haette den Schluessel neben den Geheimtext gelegt.

Also beides: der Geheimtext liegt in `~/.solvio-portal/vault.bin` mit Rechten
`0600`, AES-256-GCM; der 32-Byte-Hauptschluessel liegt im Schluesselbund. Wer
die Datei liest, hat nichts. Wer den Schluessel hat, aber die Datei nicht,
ebenso. Und weil es ein Pfad ist, kann ein Test behaupten und beweisen, dass ein
fremder Prozess nicht herankommt.

Drei Dinge, die man genau einmal falsch macht und die deshalb hier stehen:

* **Ein gesperrter Schluesselbund haengt, er scheitert nicht.** Jeder Aufruf
  bekommt eine Frist und `stdin=DEVNULL`. Ohne das legt ein einziger gesperrter
  Schluesselbund den Core still.
* **Ein Geheimnis gehoert nie in die Kommandozeile.** `argv` ist auf diesem
  Rechner ueber Benutzergrenzen hinweg lesbar. Geschrieben wird ueber `stdin` —
  und zwar **zweimal**, weil `security` sonst klaglos einen leeren Eintrag
  anlegt und mit Erfolg zurueckkehrt.
* **Zurueckgelesen wird immer.** Ein leerer Eintrag mit Rueckgabewert 0 ist der
  unangenehmste Fehler, den diese Schnittstelle kennt.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import subprocess
from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("portal")

SECURITY = "/usr/bin/security"

#: Wo der Geheimtext liegt. Ausserhalb des Repos, ausserhalb von `.env`.
DEFAULT_DIR = os.path.expanduser("~/.solvio-portal")

#: Umlenkung des Ablageorts fuer Tests (DEBT-0223). In Produktion ungesetzt:
#: dann gilt `DEFAULT_DIR` wie bisher. Den Schluesselbund lenkt sie NICHT um —
#: das bleibt `TEST_BACKEND_ENV`.
DIR_ENV = "SOLVIO_PORTAL_VAULT_DIR"


def default_dir() -> str:
    return os.path.expanduser(os.environ.get(DIR_ENV) or DEFAULT_DIR)
VAULT_FILE = "vault.bin"

#: Der Eintrag im Schluesselbund, der den Hauptschluessel haelt.
KEYCHAIN_SERVICE = "de.solvio.portal-vault"
KEYCHAIN_ACCOUNT = "master-key"

#: Frist fuer jeden Schluesselbund-Aufruf. Ein gesperrter Schluesselbund wartet
#: sonst auf eine Eingabe, die im Hintergrund niemand macht.
SECURITY_TIMEOUT = 8.0

#: Testschalter — dieselbe Naht wie `secret_vault/keyring.py:66`, wortgleich
#: uebernommen statt neu erfunden.
#:
#: Gemessen am 2026-09-05: unter einem umgeleiteten `HOME` gibt es keinen
#: Standard-Schluesselbund (`security default-keychain` meldet „A default
#: keychain could not be found"). Das Lesen scheitert dann sofort, und der
#: anschliessende SCHREIBAUFRUF blockiert, weil `security` einen
#: Anmelde-Schluesselbund anlegen will und auf eine Eingabe wartet, die
#: `stdin=DEVNULL` nie liefert — daher „keychain did not answer in time".
#:
#: Der Schalter ist aber nicht bloss Bequemlichkeit fuer das Gate. Ohne ihn
#: muessten diese Tests im ECHTEN `HOME` laufen, und dort ist der Weg
#: `_read_master() is None` → neuer Zufallsschluessel → `_write_master(-U)`
#: **nicht nebenwirkungsfrei**: er ueberschreibt den produktiven Eintrag.
#: `~/.solvio-portal/vault.bin` waere danach dauerhaft unlesbar, und der
#: Eintrag ist `CLASS_OS_BOUND` — von der Sicherung ausdruecklich ausgenommen
#: (`storage/inventory.py`). Ein Test darf diesen Weg nie betreten.
#:
#: Produktiv ist die Variable nicht gesetzt; `is_test_backend()` sagt das
#: ehrlich, damit keine Gesundheitsanzeige einen Dateispeicher als
#: Schluesselbund ausgibt.
TEST_BACKEND_ENV = "SOLVIO_PORTAL_TEST_KEYSTORE"

#: Laenge des Hauptschluessels. AES-256.
KEY_BYTES = 32

#: Der EINE Rueckgabewert von `security`, der „gibt es nicht" heisst. Jeder
#: andere heisst „konnte nicht gelesen werden" — und die beiden zu verwechseln
#: kostete beinahe den produktiven Portalschluessel (DEBT-0229). Derselbe Wert
#: steht in `secret_vault/keyring.py`; er ist hier absichtlich noch einmal
#: benannt, statt quer importiert: die beiden Tresore sollen sich nicht
#: gegenseitig laden.
_ERR_ITEM_NOT_FOUND = 44

#: Zusatzdaten der Verschluesselung. Bindet den Geheimtext an Zweck und Fassung —
#: ein Blob aus einem anderen Zusammenhang entschluesselt nicht.
AAD = b"solvio-portal-vault-v1"

#: Die Felder, die eine Zugangsbindung kennt.
USERNAME = "username"
PASSWORD = "password"
TOTP = "totp"
FIELDS = (USERNAME, PASSWORD, TOTP)


class VaultError(RuntimeError):
    """Der Tresor konnte nicht bedient werden. Traegt nie einen Wert."""


class VaultLocked(VaultError):
    """Der Schluesselbund antwortet nicht — vermutlich gesperrt."""


def _security(args: list[str], *, feed: str = "") -> tuple[int, str]:
    """Ruft `security` auf. Mit Frist, ohne Terminal, ohne Geheimnis in `argv`."""
    try:
        proc = subprocess.run(
            [SECURITY, *args],
            input=feed if feed else None,
            stdin=None if feed else subprocess.DEVNULL,
            capture_output=True, text=True, timeout=SECURITY_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise VaultLocked("keychain did not answer in time") from exc
    except FileNotFoundError as exc:
        raise VaultError("security tool missing") from exc
    return proc.returncode, proc.stdout


def is_test_backend() -> bool:
    return bool(os.environ.get(TEST_BACKEND_ENV))


def _test_key_path() -> str:
    root = os.path.expanduser(os.environ[TEST_BACKEND_ENV])
    os.makedirs(root, mode=0o700, exist_ok=True)
    return os.path.join(root, "portal-master.test")


def _read_master() -> bytes | None:
    """`None` heisst „es gibt keinen" — und NUR das (DEBT-0229).

    Hier stand frueher `if code != 0: return None`, also: jeder Fehlschlag
    heisst „kein Schluessel". Das war der gefaehrlichste Satz dieser Datei.
    `PortalVault._master()` legt bei `None` einen neuen an, und `_write_master`
    schreibt mit `-U`, also ueberschreibend. Ein gesperrter Schluesselbund, eine
    verweigernde ACL oder ein „Deny" im Dialog haetten damit den produktiven
    Hauptschluessel ersetzt — und `~/.solvio-portal/vault.bin` waere dauerhaft
    unlesbar gewesen. Der Eintrag ist `CLASS_OS_BOUND` und von der Sicherung
    ausgenommen; es gaebe keinen Weg zurueck.

    Die Unterscheidung ist dieselbe wie in `secret_vault/keyring.py:129-137`:
    Rueckgabewert 44 heisst „Eintrag nicht vorhanden", alles andere heisst
    „konnte nicht gelesen werden". Ein beschaedigter Eintrag ist ebenfalls
    KEIN fehlender — wer ihn als fehlend behandelt, verliert alles, was mit dem
    alten Schluessel verschluesselt wurde.

    Der Dateizweig des Testschalters gilt hier mit: eine Datei falscher Laenge
    ist beschaedigt, nicht abwesend. Sonst haette der Schalter genau den
    Fehlerpfad umgangen, den er sichtbar machen soll.
    """
    if is_test_backend():
        pfad = _test_key_path()
        if not os.path.exists(pfad):
            return None
        with open(pfad, "rb") as handle:
            roh = handle.read()
        if len(roh) != KEY_BYTES:
            raise VaultError("master key has the wrong length")
        return roh

    code, out = _security(["find-generic-password", "-s", KEYCHAIN_SERVICE,
                           "-a", KEYCHAIN_ACCOUNT, "-w"])
    if code == _ERR_ITEM_NOT_FOUND:
        return None
    if code != 0:
        # Der Rueckgabewert, nie die Meldung: ein Log nimmt keinen Text aus
        # einem Sicherheitswerkzeug auf.
        log.warning("portal.keychain_refused", code=code)
        raise VaultLocked(f"keychain refused with status {code}")
    # `security` haengt ein Zeilenende an. Ohne das Abschneiden waere der
    # Schluessel ein anderer als der gespeicherte.
    encoded = out.removesuffix("\n")
    if not encoded:
        raise VaultError("keychain entry exists but is empty")
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise VaultError("master key is not valid base64") from exc
    if len(key) != KEY_BYTES:
        raise VaultError("master key has the wrong length")
    return key


def _write_master(key: bytes) -> None:
    if is_test_backend():
        pfad = _test_key_path()
        vorher = os.umask(0o077)
        try:
            with open(pfad, "wb") as handle:
                handle.write(key)
        finally:
            os.umask(vorher)
        os.chmod(pfad, 0o600)
        return

    encoded = base64.b64encode(key).decode("ascii")
    # Zweimal: `-w` ohne Argument fragt Wert und Wiederholung, beides von stdin.
    # Nur einmal zu fuettern legt einen LEEREN Eintrag an und meldet Erfolg.
    code, _ = _security(["add-generic-password", "-s", KEYCHAIN_SERVICE,
                         "-a", KEYCHAIN_ACCOUNT, "-D", "SOLVIO portal vault key",
                         "-U", "-w"], feed=f"{encoded}\n{encoded}\n")
    if code != 0:
        raise VaultError("could not store the master key")
    readback = _read_master()
    if readback != key:
        raise VaultError("master key did not survive the round trip")


class PortalVault:
    """Geheimnisse hinter undurchsichtigen Aliassen."""

    def __init__(self, base_dir: str | None = None) -> None:
        base_dir = default_dir() if base_dir is None else base_dir
        self.base_dir = base_dir
        self.path = os.path.join(base_dir, VAULT_FILE)
        self._key: bytes | None = None

    # -- Schluessel ----------------------------------------------------------
    def _master(self) -> bytes:
        if self._key is None:
            key = _read_master()
            if key is None:
                key = secrets.token_bytes(32)
                _write_master(key)
                log.info("portal.vault_key_created")
            self._key = key
        return self._key

    # -- Ablage --------------------------------------------------------------
    def _load(self) -> dict[str, dict[str, str]]:
        if not os.path.exists(self.path):
            return {}
        mode = os.stat(self.path).st_mode & 0o777
        if mode & 0o077:
            # Nicht reparieren, sondern verweigern: Rechte, die einmal offen
            # standen, koennten bereits gelesen worden sein.
            raise VaultError(f"vault file is group/other readable ({oct(mode)})")
        with open(self.path, "rb") as handle:
            blob = handle.read()
        if len(blob) < 13:
            raise VaultError("vault file is truncated")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce, cipher = blob[:12], blob[12:]
        try:
            plain = AESGCM(self._master()).decrypt(nonce, cipher, AAD)
        except Exception as exc:  # noqa: BLE001 - jede Ursache ist dieselbe Aussage
            raise VaultError("vault could not be opened") from exc
        data = json.loads(plain.decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        os.makedirs(self.base_dir, mode=0o700, exist_ok=True)
        os.chmod(self.base_dir, 0o700)
        nonce = secrets.token_bytes(12)
        plain = json.dumps(data, ensure_ascii=False).encode("utf-8")
        blob = nonce + AESGCM(self._master()).encrypt(nonce, plain, AAD)
        temporary = self.path + ".new"
        previous = os.umask(0o077)
        try:
            with open(temporary, "wb") as handle:
                handle.write(blob)
        finally:
            os.umask(previous)
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    # -- Bedienung -----------------------------------------------------------
    def ciphertext_identity(self) -> dict:
        """Opaque encrypted-file binding. Never decrypts or creates a key."""
        return self._ciphertext()[1]

    def _ciphertext(self) -> tuple[bytes, dict]:
        """One bounded, private native file snapshot without following a symlink."""
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_mode & 0o077 or not 13 <= before.st_size <= 4 * 1024 * 1024):
                raise VaultError("portal ciphertext binding unavailable")
            chunks, remaining = [], before.st_size
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    raise VaultError("portal ciphertext changed")
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise VaultError("portal ciphertext changed")
            blob = b''.join(chunks)
            return blob, {"path": os.path.realpath(self.path), "device": before.st_dev,
                    "inode": before.st_ino, "sha256": hashlib.sha256(blob).hexdigest()}
        finally:
            os.close(descriptor)

    def has_existing(self, alias: str) -> bool:
        """Check a complete configured login, with no key/file creation.

        The older has()/get()/store() contract is deliberately unchanged.
        Catalogue reads must never enter _master's missing-key creation path.
        Decrypted material remains entirely within this native vault method.
        """
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        try:
            blob, _ = self._ciphertext()
            key = self._key if self._key is not None else _read_master()
            if key is None:
                return False
            plain = AESGCM(key).decrypt(blob[:12], blob[12:], AAD)
            data = json.loads(plain.decode('utf-8'))
            entry = data.get(alias) if isinstance(data, dict) else None
            return bool(isinstance(entry, dict) and all(
                isinstance(entry.get(field), str) and entry[field] for field in (USERNAME, PASSWORD)))
        except (OSError, VaultError, ValueError, TypeError, InvalidTag):
            return False

    def store(self, alias: str, field: str, value: str) -> None:
        """Legt einen Wert unter einem Alias ab. Der Wert erscheint nirgends."""
        if field not in FIELDS:
            raise VaultError(f"unknown credential field: {field!r}")
        if not alias or not value:
            raise VaultError("alias and value are both required")
        data = self._load()
        data.setdefault(alias, {})[field] = value
        self._save(data)
        # Nur die Tatsache, nie der Wert, nie die Laenge des Wertes.
        log.info("portal.credential_stored", alias=alias, field=field)

    def get(self, alias: str, field: str) -> str:
        """Holt einen Wert. Der einzige Weg — und er fuehrt nie durch ein Modell."""
        data = self._load()
        entry = data.get(alias) or {}
        value = entry.get(field, "")
        if not value:
            raise VaultError(f"no {field} stored for {alias!r}")
        return value

    def has(self, alias: str, field: str = PASSWORD) -> bool:
        try:
            data = self._load()
        except VaultError:
            return False
        return bool((data.get(alias) or {}).get(field))

    def aliases(self) -> list[str]:
        """Die Aliasse. Das Modell darf sie sehen; Werte sieht es nie."""
        try:
            return sorted(self._load())
        except VaultError:
            return []

    def forget(self, alias: str) -> bool:
        data = self._load()
        if alias not in data:
            return False
        data.pop(alias)
        self._save(data)
        log.info("portal.credential_forgotten", alias=alias)
        return True

    def __repr__(self) -> str:
        return f"<PortalVault at {self.base_dir}>"
