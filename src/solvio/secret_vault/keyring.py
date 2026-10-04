"""Der Hauptschluessel liegt im Schluesselbund — und nirgends sonst.

Der Portaltresor hat diesen Weg schon einmal gegangen (`solvio/portal/vault.py`),
und die drei Dinge, die man dabei genau einmal falsch macht, stehen dort
aufgeschrieben. Sie stehen hier wieder, weil dieses Modul sie erneut braucht und
weil ein Verweis auf eine andere Datei keine Zusicherung ist:

* **Ein gesperrter Schluesselbund haengt, er scheitert nicht.** Jeder Aufruf
  bekommt eine Frist und `stdin=DEVNULL`. Ohne das legt ein einziger gesperrter
  Schluesselbund den Core still, und zwar lautlos.
* **Ein Geheimnis gehoert nie in `argv`.** Auf diesem Rechner ist die
  Prozessliste ueber Benutzergrenzen hinweg lesbar. Geschrieben wird ueber
  `stdin` — und zwar zweimal, weil `security` sonst klaglos einen LEEREN Eintrag
  anlegt und mit Erfolg zurueckkehrt.
* **Zurueckgelesen wird immer.** Ein leerer Eintrag mit Rueckgabewert 0 ist der
  unangenehmste Fehler, den diese Schnittstelle kennt.

Und eine vierte Regel, die der Portaltresor nicht brauchte und dieser sehr wohl:

* **Ein unlesbarer Schluessel ist kein fehlender Schluessel.** Der Portaltresor
  erzeugt bei `None` einfach einen neuen — bei einem einzigen Alias ist das
  verschmerzbar. Hier waere es eine Katastrophe: ein neuer Hauptschluessel macht
  jeden vorhandenen Geheimtext unentschluesselbar, und zwar endgueltig. Deshalb
  trennt dieses Modul scharf zwischen „es gibt keinen" (`None`) und „ich komme
  gerade nicht heran" (`VaultLocked`) — und legt einen neuen ausschliesslich
  ueber `initialize()` an, das der Aufrufer nur mit leerem Tresor aufrufen darf.

Warum ueberhaupt der Schluesselbund und nicht eine eigene Schluesseldatei: eine
Datei neben dem Geheimtext ist kein Schutz, sondern eine laengere Zeile im
Angriffspfad. Der Schluesselbund ist an Anmeldung und Geraet gebunden, er wird
von macOS verwaltet, und er geht ausdruecklich NICHT mit in die Sicherung
(Klasse B). Genau deshalb ist eine Dateisicherung des Tresors harmlos — und
genau deshalb braucht es den Wiederherstellungsumschlag (`solvio.secret_vault.recovery`).
"""
from __future__ import annotations

import base64
import os
import secrets
import subprocess

from solvio.logging_setup import get_logger

log = get_logger("vault")

SECURITY = "/usr/bin/security"

#: Der Eintrag im Schluesselbund, der den Schluesselverschluesselungsschluessel
#: (KEK) haelt. Ein eigener Dienstname, nicht der des Portaltresors: zwei
#: Tresore, zwei Schluessel, zwei Sperrbereiche.
KEYCHAIN_SERVICE = "de.solvio.vault"
KEYCHAIN_ACCOUNT = "kek-v1"
KEYCHAIN_LABEL = "SOLVIO Tresor — Hauptschluessel"

#: Frist fuer jeden Schluesselbund-Aufruf.
SECURITY_TIMEOUT = 8.0

#: Laenge des Hauptschluessels. AES-256.
KEY_BYTES = 32

#: Testschalter. Ein Test darf den produktiven Schluesselbund niemals anfassen —
#: nicht lesend und schon gar nicht schreibend. Steht diese Variable, liegt der
#: Schluessel in einer Datei im angegebenen Verzeichnis, mit Rechten 0600.
#: Produktiv ist sie nicht gesetzt; `is_test_backend()` sagt das ehrlich, damit
#: die Gesundheit nie einen Dateispeicher als Schluesselbund ausgibt.
TEST_BACKEND_ENV = "SOLVIO_VAULT_TEST_KEYSTORE"


class VaultError(RuntimeError):
    """Der Tresor konnte nicht bedient werden. Traegt nie einen Wert."""


class VaultLocked(VaultError):
    """Der Schluesselbund antwortet nicht oder verweigert — vermutlich gesperrt.

    Ausdruecklich NICHT dasselbe wie „es gibt keinen Schluessel". Diese
    Unterscheidung ist der Grund, warum dieses Modul existiert.
    """


def is_test_backend() -> bool:
    return bool(os.environ.get(TEST_BACKEND_ENV))


def _test_key_path() -> str:
    root = os.path.expanduser(os.environ[TEST_BACKEND_ENV])
    os.makedirs(root, mode=0o700, exist_ok=True)
    return os.path.join(root, "kek.test")


def _security(args: list[str], *, feed: str = "") -> tuple[int, str, str]:
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
    return proc.returncode, proc.stdout, proc.stderr


#: Rueckgabewert von `security`, wenn der gesuchte Eintrag schlicht nicht da ist.
#: Jeder ANDERE Fehlschlag ist eine Stoerung und darf nicht als „gibt es nicht"
#: durchgehen — sonst wuerde ein gesperrter Schluesselbund zur Neuerzeugung
#: fuehren.
_ERR_ITEM_NOT_FOUND = 44


def read_kek() -> bytes | None:
    """Der Hauptschluessel, oder `None`, wenn es nachweislich keinen gibt.

    Wirft `VaultLocked`, wenn der Schluesselbund nicht antwortet oder aus einem
    anderen Grund als „nicht vorhanden" verweigert.
    """
    if is_test_backend():
        path = _test_key_path()
        if not os.path.exists(path):
            return None
        with open(path, "rb") as handle:
            raw = handle.read()
        return raw if len(raw) == KEY_BYTES else None

    code, out, err = _security(["find-generic-password", "-s", KEYCHAIN_SERVICE,
                                "-a", KEYCHAIN_ACCOUNT, "-w"])
    if code == _ERR_ITEM_NOT_FOUND:
        return None
    if code != 0:
        # Die Meldung von `security` kann den Grund nennen, nie den Wert. Sie
        # wird auf eine Kennung reduziert, damit ein Log nicht durch die
        # Hintertuer Text aus einem Sicherheitswerkzeug aufnimmt.
        log.warning("vault.keychain_refused", code=code,
                    hint=err.strip()[:80] or "no detail")
        raise VaultLocked(f"keychain refused with status {code}")
    encoded = out.removesuffix("\n")
    if not encoded:
        # Ein leerer Eintrag ist ein kaputter Eintrag, kein fehlender. Wer ihn
        # als fehlend behandelt, legt einen neuen Schluessel an und verliert
        # alles, was mit dem alten verschluesselt wurde.
        raise VaultError("keychain entry exists but is empty")
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise VaultError("master key is not valid base64") from exc
    if len(key) != KEY_BYTES:
        raise VaultError("master key has the wrong length")
    return key


def _write_kek(key: bytes) -> None:
    if is_test_backend():
        path = _test_key_path()
        previous = os.umask(0o077)
        try:
            with open(path, "wb") as handle:
                handle.write(key)
        finally:
            os.umask(previous)
        os.chmod(path, 0o600)
        return

    encoded = base64.b64encode(key).decode("ascii")
    # Zweimal: `-w` ohne Argument fragt Wert und Wiederholung, beides von stdin.
    # Nur einmal zu fuettern legt einen LEEREN Eintrag an und meldet Erfolg.
    code, _out, _err = _security(
        ["add-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT,
         "-D", KEYCHAIN_LABEL, "-U", "-w"], feed=f"{encoded}\n{encoded}\n")
    if code != 0:
        raise VaultError("could not store the master key")
    if read_kek() != key:
        raise VaultError("master key did not survive the round trip")


def initialize_kek(*, allow_overwrite: bool = False) -> bytes:
    """Legt EINEN Hauptschluessel an. Nur mit leerem Tresor aufzurufen.

    `allow_overwrite` ist nicht Bequemlichkeit, sondern die einzige Tuer fuer die
    Wiederherstellung: dort wird ein aus dem Umschlag entpackter Schluessel
    eingesetzt statt eines neuen. Der normale Weg laesst sie zu.
    """
    existing = read_kek()
    if existing is not None and not allow_overwrite:
        return existing
    key = secrets.token_bytes(KEY_BYTES)
    _write_kek(key)
    log.info("vault.kek_created", backend="file" if is_test_backend() else "keychain")
    return key


def install_kek(key: bytes) -> None:
    """Setzt einen BEKANNTEN Hauptschluessel ein — der Weg der Wiederherstellung."""
    if len(key) != KEY_BYTES:
        raise VaultError("master key has the wrong length")
    _write_kek(key)
    log.info("vault.kek_installed", backend="file" if is_test_backend() else "keychain")


def forget_kek() -> bool:
    """Entfernt den Eintrag. Nur fuer Tests und den ausdruecklichen Zuruecksetzen-Weg."""
    if is_test_backend():
        path = _test_key_path()
        if os.path.exists(path):
            os.remove(path)
            return True
        return False
    code, _out, _err = _security(["delete-generic-password", "-s", KEYCHAIN_SERVICE,
                                  "-a", KEYCHAIN_ACCOUNT])
    return code == 0
