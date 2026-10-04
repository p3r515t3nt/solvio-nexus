"""Die Offsite-Identitaet: operativ im Schluesselbund, Recovery im Umschlag.

Ein age-Schluesselpaar, zwei Aufbewahrungswege, eine Passphrase (Vertrag §5):

* **OPERATIV** liegt die X25519-Identitaet im macOS-Schluesselbund
  (`de.solvio.offsite` / `identity-v1`), Klasse B — sie geht in KEINE
  Sicherung und wird nur fuer Verify/Restore gebraucht, NIE fuer den Upload.
  Der Upload verschluesselt mit dem OEFFENTLICHEN Recipient aus der
  Konfiguration und haelt nie ein Geheimnis.
* **RECOVERY** ist `offsite-identity-v1.age`: dieselbe Identitaet unter ages
  eigenem scrypt-Passphrasen-Rezipienten (Standardformat — jede stock-age-
  Installation oeffnet ihn). Die Passphrase kennt nur der Mensch.

Die Schluesselbund-Mechanik folgt dem Tresor-KEK (`secret_vault/keyring.py`)
— mit denselben Regeln, die man genau einmal falsch macht: Frist gegen den
haengenden Schluesselbund, nie ein Wert in `argv`, immer zurueckelesen, und
die scharfe Trennung zwischen „es gibt keinen" (`None`) und „ich komme
gerade nicht heran" (`OffsiteKeychainLocked`). Die letzte traegt hier
besonders: ein Verify-Lauf, der einen GESPERRTEN Schluesselbund als „keine
Identitaet" liest, wuerde `auth_required` faelschlich zu einem Schluessel-
verlust erklaeren.

**Eine Regel ist hier ANDERS als beim KEK, und sie ist gemessen (B2,
2026-08-31, Owner-Lauf + Reproduktion):** `add-generic-password -w` OHNE
Wert liest gegen die LOGIN-Keychain nicht etwa still von stdin — sobald der
Prozess ein controlling TTY hat (jedes Owner-Terminal!), oeffnet `security`
`/dev/tty`, schreibt „password data for new item:" dorthin und wartet auf
die Tastatur; der stdin-Feed bleibt ungelesen liegen, bis die Frist ihn
toetet. Der stdin-Fallback existiert nur OHNE controlling TTY — genau
deshalb war die erste Messung blind: sie lief TTY-los. Geschrieben wird
deshalb ueber den Batch-Modus `security -i`: das Kommando samt
base64-Wert reist als EINE Zeile ueber die stdin-Pipe (nie `argv`, nie
Prozessliste, nie History), und einen Prompt-Pfad gibt es nicht, weil
`-w` seinen Wert traegt. Zweite Messung dazu: `-i` VERSCHLUCKT
Subkommando-Fehler (Exit 0 trotz Fehlschlag) — der Rueckweg
(`read_identity`-Vergleich) ist darum nicht Vorsicht, sondern der einzige
Beweis, dass geschrieben wurde.

Rotation (§5): je Fassung ein eigener Account (`identity-v2`, ...). Der
Schluesselbund fuehrt ALLE Identitaeten, deren Generationen noch leben —
der Restore-Beweis einer v1-Generation braucht v1 ohne Passphrase.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess

from solvio.logging_setup import get_logger

log = get_logger("offsite")

SECURITY = "/usr/bin/security"

KEYCHAIN_SERVICE = "de.solvio.offsite"
#: Leerzeichenfrei MIT ABSICHT: jedes Token des `security -i`-Batchkommandos
#: kommt aus einem festen, quoting-freien Zeichensatz. Wer hier ein
#: Leerzeichen einfuehrt, muss den Batch-Parser von `security` verstehen —
#: das will niemand muessen.
KEYCHAIN_LABEL = "solvio-offsite-age-identitaet"

SECURITY_TIMEOUT = 8.0
AGE_TIMEOUT = 30.0

#: Testschalter, eigener Name: Offsite-Tests duerfen weder den produktiven
#: Schluesselbund noch den Test-Schluesselspeicher des Tresors anfassen.
TEST_BACKEND_ENV = "SOLVIO_OFFSITE_TEST_KEYSTORE"

#: Zweiter Testschalter: eine WEGWERF-Keychain-DATEI. Damit laeuft der ECHTE
#: `security`-Pfad (Batch-Write, find, delete) in Tests gegen eine eigene
#: Datei statt gegen die Login-Keychain — die Luecke, durch die der
#: TTY-Prompt-Fehler an der ersten Messung vorbeikam, war genau, dass der
#: echte Pfad nur TTY-los und nur gegen die Login-Keychain des Baurechners
#: pruefbar schien.
TEST_KEYCHAIN_FILE_ENV = "SOLVIO_OFFSITE_TEST_KEYCHAIN_FILE"

#: Wo age/age-keygen liegen koennen, wenn PATH nichts sagt — der launchd-
#: Kontext des woechentlichen Beweises hat ein karges PATH.
_AGE_FALLBACKS = ("/opt/homebrew/bin", "/usr/local/bin")


class OffsiteIdentityError(RuntimeError):
    """Die Identitaet konnte nicht bedient werden. Traegt nie einen Wert."""


class OffsiteKeychainLocked(OffsiteIdentityError):
    """Der Schluesselbund antwortet nicht oder verweigert — vermutlich gesperrt.

    Ausdruecklich NICHT dasselbe wie „es gibt keine Identitaet".
    """


def is_test_backend() -> bool:
    return bool(os.environ.get(TEST_BACKEND_ENV))


def _account(version: int) -> str:
    return f"identity-v{int(version)}"


def _test_item_path(version: int) -> str:
    root = os.path.expanduser(os.environ[TEST_BACKEND_ENV])
    os.makedirs(root, mode=0o700, exist_ok=True)
    return os.path.join(root, f"{_account(version)}.test")


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for prefix in _AGE_FALLBACKS:
        candidate = os.path.join(prefix, name)
        if os.path.exists(candidate):
            return candidate
    raise OffsiteIdentityError(f"{name} not installed")


def _test_keychain_file() -> str | None:
    """Die Wegwerf-Keychain der Tests — oder None (dann: Login-Keychain)."""
    path = os.environ.get(TEST_KEYCHAIN_FILE_ENV) or ""
    if not path:
        return None
    if " " in path:
        # Ehrliche Grenze statt Quoting-Roulette im Batch-Parser.
        raise OffsiteIdentityError("test keychain path must not contain spaces")
    return path


def _security(args: list[str], *, feed: str = "") -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            [SECURITY, *args],
            input=feed if feed else None,
            stdin=None if feed else subprocess.DEVNULL,
            capture_output=True, text=True, timeout=SECURITY_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise OffsiteKeychainLocked("keychain did not answer in time") from exc
    except FileNotFoundError as exc:
        raise OffsiteIdentityError("security tool missing") from exc
    return proc.returncode, proc.stdout, proc.stderr


#: Die EINZIGE argv des Schreibwegs — statisch, wertfrei, einfrierbar. Der
#: Wert reist ausschliesslich im Batch-Kommando ueber die stdin-Pipe.
_WRITE_ARGV: tuple[str, ...] = (SECURITY, "-i")

#: Tokens, die in ein Batch-Kommando duerfen. base64 plus die Zeichen der
#: festen Bezeichner — alles andere waere ein Quoting-Experiment.
_BATCH_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/=._/-]+$")


def _write_batch_command(service: str, account: str, label: str,
                         encoded: str, keychain: str | None) -> str:
    """Das `security -i`-Kommando fuer den Write. Jedes Token geprueft."""
    tokens = [service, account, label, encoded] + ([keychain] if keychain else [])
    for token in tokens:
        if not token or not _BATCH_TOKEN_RE.match(token):
            raise OffsiteIdentityError("batch token is not batch-safe")
    tail = f" {keychain}" if keychain else ""
    return (f"add-generic-password -s {service} -a {account} "
            f"-D {label} -U -w {encoded}{tail}\n")


_ERR_ITEM_NOT_FOUND = 44

#: Eine age-X25519-Identitaet ist genau EINE Bech32-Zeile. Kommentarzeilen der
#: Schluesseldatei gehoeren nicht in den Schluesselbund — gespeichert wird der
#: Schluessel, nicht die Datei.
_IDENTITY_RE = re.compile(r"^AGE-SECRET-KEY-1[A-Z0-9]{50,80}$")


def _require_identity_line(value: str) -> str:
    line = (value or "").strip()
    if not _IDENTITY_RE.match(line):
        raise OffsiteIdentityError("value is not an age identity line")
    return line


def read_identity(version: int = 1) -> str | None:
    """Die Identitaetszeile, oder `None`, wenn es nachweislich keine gibt.

    Wirft `OffsiteKeychainLocked` bei jedem anderen Hindernis.
    """
    if is_test_backend():
        path = _test_item_path(version)
        if not os.path.exists(path):
            return None
        with open(path, encoding="ascii") as handle:
            return _require_identity_line(handle.read())

    keychain = _test_keychain_file()
    code, out, err = _security(["find-generic-password", "-s", KEYCHAIN_SERVICE,
                                "-a", _account(version), "-w"]
                               + ([keychain] if keychain else []))
    if code == _ERR_ITEM_NOT_FOUND:
        return None
    if code != 0:
        log.warning("offsite.keychain_refused", code=code,
                    hint=err.strip()[:80] or "no detail")
        raise OffsiteKeychainLocked(f"keychain refused with status {code}")
    encoded = out.removesuffix("\n")
    if not encoded:
        raise OffsiteIdentityError("keychain entry exists but is empty")
    try:
        raw = base64.b64decode(encoded, validate=True).decode("ascii")
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise OffsiteIdentityError("keychain entry is not a stored identity") from exc
    return _require_identity_line(raw)


def store_identity(value: str, version: int = 1) -> None:
    """Legt die Identitaet ab — und glaubt es erst nach dem Rueckweg."""
    line = _require_identity_line(value)
    if is_test_backend():
        path = _test_item_path(version)
        previous = os.umask(0o077)
        try:
            with open(path, "w", encoding="ascii") as handle:
                handle.write(line)
        finally:
            os.umask(previous)
        os.chmod(path, 0o600)
    else:
        encoded = base64.b64encode(line.encode("ascii")).decode("ascii")
        # Batch-Modus `security -i`: das Kommando traegt seinen Wert, also
        # existiert kein Prompt-Pfad — `-w` OHNE Wert wuerde mit controlling
        # TTY auf /dev/tty fragen und den stdin-Feed liegen lassen (gemessen
        # am 2026-08-31, siehe Modulkopf). argv bleibt statisch und wertfrei.
        command = _write_batch_command(KEYCHAIN_SERVICE, _account(version),
                                       KEYCHAIN_LABEL, encoded,
                                       _test_keychain_file())
        code, _out, err = _security(list(_WRITE_ARGV[1:]), feed=command)
        if code != 0:
            # `-i` meldet 0 sogar fuer gescheiterte Subkommandos (gemessen);
            # ein Nicht-Null hier ist also etwas Grundsaetzliches.
            log.warning("offsite.keychain_write_refused", code=code,
                        hint=err.strip()[:80] or "no detail")
            raise OffsiteIdentityError("could not store the identity")
    if read_identity(version) != line:
        # Nicht Guertel-und-Hosentraeger: `security -i` verschluckt Fehler
        # seiner Subkommandos — DIESE Zeile ist der Beweis des Schreibens.
        raise OffsiteIdentityError("identity did not survive the round trip")
    log.info("offsite.identity_stored", version=int(version),
             backend="file" if is_test_backend() else "keychain")


def present(version: int = 1) -> bool:
    """Gibt es den Eintrag — OHNE den Wert zu holen? Fuer Statusanzeigen."""
    if is_test_backend():
        return os.path.exists(_test_item_path(version))
    keychain = _test_keychain_file()
    code, _out, _err = _security(["find-generic-password", "-s",
                                  KEYCHAIN_SERVICE, "-a", _account(version)]
                                 + ([keychain] if keychain else []))
    if code == _ERR_ITEM_NOT_FOUND:
        return False
    if code != 0:
        raise OffsiteKeychainLocked(f"keychain refused with status {code}")
    return True


def forget_identity(version: int = 1) -> bool:
    """Entfernt den Eintrag. Fuer Tests und den ausdruecklichen Rueckbau (§21)."""
    if is_test_backend():
        path = _test_item_path(version)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False
    keychain = _test_keychain_file()
    code, _out, _err = _security(["delete-generic-password", "-s",
                                  KEYCHAIN_SERVICE, "-a", _account(version)]
                                 + ([keychain] if keychain else []))
    return code == 0


# ------------------------------------------------------------------ age-Werkzeug
def recipient_of(identity_line: str) -> str:
    """Der oeffentliche Recipient zur Identitaet — via `age-keygen -y`.

    Die Identitaet reist ueber die stdin-Pipe in den Unterprozess und
    nirgendwohin sonst; zurueck kommt der oeffentliche Schluessel.
    """
    line = _require_identity_line(identity_line)
    try:
        proc = subprocess.run([_tool("age-keygen"), "-y"],
                              input=line + "\n", capture_output=True,
                              text=True, timeout=AGE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise OffsiteIdentityError("age-keygen did not answer in time") from exc
    if proc.returncode != 0:
        raise OffsiteIdentityError("age-keygen rejected the identity")
    recipient = proc.stdout.strip()
    if not recipient.startswith("age1"):
        raise OffsiteIdentityError("age-keygen returned no recipient")
    return recipient


def create_identity() -> tuple[str, str]:
    """Erzeugt ein NEUES Paar (Identitaetszeile, Recipient) — fuer Rotation.

    `age-keygen` schreibt auf stdout; es entsteht keine Datei. Der Aufrufer
    (der Rotationsweg in `offsite_admin`) traegt die Verantwortung, die Zeile
    sofort in den Schluesselbund und den Umschlag zu bringen und dann zu
    vergessen.
    """
    try:
        proc = subprocess.run([_tool("age-keygen")], capture_output=True,
                              text=True, timeout=AGE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise OffsiteIdentityError("age-keygen did not answer in time") from exc
    if proc.returncode != 0:
        raise OffsiteIdentityError("age-keygen failed")
    identity_line = ""
    for raw in proc.stdout.splitlines():
        candidate = raw.strip()
        if candidate.startswith("AGE-SECRET-KEY-1"):
            identity_line = candidate
            break
    if not identity_line:
        raise OffsiteIdentityError("age-keygen produced no identity")
    return _require_identity_line(identity_line), recipient_of(identity_line)


# ---------------------------------------------------------------- der Umschlag
def envelope_scrypt_log_n(path: str) -> int:
    """Der scrypt-Arbeitsfaktor (log2 N) aus dem Umschlag-Kopf.

    Der Kopf eines age-Umschlags ist unverschluesselte Struktur — derselbe
    Kopf liegt beim Provider. Gelesen wird er fuer §4 („die gemessenen
    scrypt-Arbeitsfaktoren werden im Vertrag nachgetragen") und fuer die
    Statusanzeige; der Geheimtext selbst bleibt zu.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError as exc:
        raise OffsiteIdentityError(f"envelope unreadable: {exc}") from exc
    try:
        text = head.decode("ascii", errors="replace")
    except Exception as exc:  # pragma: no cover - decode mit replace wirft nicht
        raise OffsiteIdentityError("envelope header undecodable") from exc
    if not text.startswith("age-encryption.org/v1"):
        raise OffsiteIdentityError("not an age envelope")
    for line in text.splitlines():
        if line.startswith("-> scrypt "):
            parts = line.split()
            try:
                return int(parts[3])
            except (IndexError, ValueError) as exc:
                raise OffsiteIdentityError("scrypt stanza malformed") from exc
    raise OffsiteIdentityError("envelope has no scrypt stanza")
