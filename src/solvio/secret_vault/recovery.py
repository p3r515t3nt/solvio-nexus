"""Der Wiederherstellungsumschlag — damit der Tresor den Mac ueberlebt.

Das Problem, das dieses Modul loest, ist genau das, was ADR-0024 als Preis
ausgesprochen hat: Der Hauptschluessel liegt im macOS-Schluesselbund. Das ist
richtig — er ist damit an Anmeldung und Geraet gebunden und wandert nicht mit in
die Sicherung. Es hat aber eine Folge, die man einmal zu Ende denken muss:

    Stirbt die interne Platte, ist der verschluesselte Tresor auf der externen
    Platte **wertlos**. Nicht beschaedigt — wertlos.

Eine Sicherung, die nach dem Ernstfall nicht aufgeht, ist keine Sicherung. Also
gibt es einen zweiten Weg zum Schluessel, und der haengt an einem Menschen statt
an einem Geraet: eine **Wiederherstellungs-Passphrase**, die der Besitzer setzt
und die SOLVIO nie speichert.

Die Rechnung dahinter, ausgeschrieben:

    Passphrase --Argon2id--> Umschlagschluessel --AES-256-GCM--> Hauptschluessel

Der Umschlag enthaelt den Hauptschluessel verschluesselt, das Salz, die
KDF-Parameter und einen Fingerabdruck des Schluessels. Er enthaelt NICHT die
Passphrase und nichts, woraus sie sich herleiten liesse.

Damit gilt beides gleichzeitig, und das ist der Punkt:

* Wer die externe Platte mitnimmt, hat den Geheimtext des Tresors UND den
  Umschlag — und kann nichts damit anfangen, weil ihm die Passphrase fehlt.
* Wer die Passphrase hat und die Platte, bekommt den Tresor zurueck.

**Argon2id** und nicht PBKDF2: eine Passphrase, die ein Mensch sich merkt, hat
wenig Entropie. Was sie schuetzt, ist der Preis eines Rateversuchs, und der
haengt bei PBKDF2 nur an Rechenzeit — die ein Angreifer auf Grafikkarten billig
kauft. Argon2id kostet zusaetzlich Speicher, und Speicher parallelisiert sich
nicht weg. Es kommt aus derselben `cryptography`-Bibliothek, die der Tresor
ohnehin benutzt; es ist keine neue Abhaengigkeit und kein eigener Krypto-Code.

**Die Passphrase kommt nie durch einen Chat.** Sie wird in einem nativen
macOS-Fenster mit verdeckter Eingabe erfragt (`scripts/vault_recovery.py`,
dasselbe Muster wie `scripts/portal_credential.py`). Sie steht in keinem `argv`,
keiner Umgebungsvariablen, keiner Historie und keinem Protokoll.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from solvio.logging_setup import get_logger
from solvio.secret_vault import envelope as E
from solvio.secret_vault import keyring as K
from solvio.secret_vault.store import vault_dir

log = get_logger("vault")

RECOVERY_FILE = "recovery.json"
FORMAT_VERSION = 1

#: Argon2id-Parameter. Gemessen auf diesem Mac: rund 0,3 s je Ableitung.
#: Fuer einen Menschen, der einmal eine Passphrase eingibt, ist das nichts. Fuer
#: jemanden, der raet, ist es der ganze Unterschied — und die 128 MiB kann er
#: nicht auf einer Grafikkarte wegparallelisieren.
KDF_NAME = "argon2id"
KDF_ITERATIONS = 3
KDF_LANES = 4
KDF_MEMORY_KIB = 131072      # 128 MiB
SALT_BYTES = 16

#: Der Zweck im Umschlag-AAD. Ein Wiederherstellungsumschlag kann nie als
#: Geheimnisumschlag durchgehen.
PURPOSE = "vault-recovery-kek"

#: Die Untergrenze fuer eine Passphrase. Keine Zeichenklassenregel — die
#: erzeugt `Passwort1!` und sonst nichts. Laenge ist das Einzige, was messbar
#: hilft, und der Rest ist eine Bitte an den Menschen, kein Filter.
MIN_PASSPHRASE = 12


class RecoveryError(RuntimeError):
    """Die Wiederherstellung geht nicht. Traegt nie eine Passphrase, nie einen Wert."""


def recovery_path(directory: str | None = None) -> str:
    return os.path.join(directory or vault_dir(), RECOVERY_FILE)


def key_fingerprint(key: bytes) -> str:
    """Ein nicht umkehrbarer Wiedererkennungswert fuer einen Schluessel.

    Gebraucht fuer genau eine Frage: passt der Umschlag noch zum Schluessel, der
    heute im Schluesselbund liegt? Ohne das koennte eine Gesundheitsanzeige nur
    sagen „es gibt einen Umschlag", nicht „er ist aktuell" — und ein veralteter
    Umschlag ist genau die Art von Sicherung, die man erst im Ernstfall bemerkt.
    """
    return hashlib.sha256(b"solvio-vault-kek-fingerprint-v1\x00" + key).hexdigest()[:32]


def _derive(passphrase: str, salt: bytes, *, iterations: int, lanes: int,
            memory_kib: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
    return Argon2id(salt=salt, length=32, iterations=iterations, lanes=lanes,
                    memory_cost=memory_kib).derive(passphrase.encode("utf-8"))


@dataclass(frozen=True)
class RecoveryEnvelope:
    format_version: int
    kdf: str
    salt_b64: str
    iterations: int
    lanes: int
    memory_kib: int
    wrapped_kek_b64: str
    kek_fingerprint: str
    created_at: str

    def to_json(self) -> str:
        return json.dumps({
            "format_version": self.format_version,
            "kdf": self.kdf,
            "salt": self.salt_b64,
            "iterations": self.iterations,
            "lanes": self.lanes,
            "memory_kib": self.memory_kib,
            "wrapped_kek": self.wrapped_kek_b64,
            "kek_fingerprint": self.kek_fingerprint,
            "created_at": self.created_at,
        }, indent=2, sort_keys=True) + "\n"

    def __repr__(self) -> str:
        return (f"RecoveryEnvelope(v{self.format_version} {self.kdf} "
                f"fp={self.kek_fingerprint[:8]} at={self.created_at})")

    @classmethod
    def from_json(cls, text: str) -> "RecoveryEnvelope":
        try:
            data: dict[str, Any] = json.loads(text)
        except ValueError as exc:
            raise RecoveryError("recovery envelope is not readable") from exc
        try:
            return cls(
                format_version=int(data["format_version"]),
                kdf=str(data["kdf"]),
                salt_b64=str(data["salt"]),
                iterations=int(data["iterations"]),
                lanes=int(data["lanes"]),
                memory_kib=int(data["memory_kib"]),
                wrapped_kek_b64=str(data["wrapped_kek"]),
                kek_fingerprint=str(data["kek_fingerprint"]),
                created_at=str(data.get("created_at") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RecoveryError("recovery envelope is incomplete") from exc


def build(passphrase: str, kek: bytes) -> RecoveryEnvelope:
    """Packt den Hauptschluessel in einen passphrasengeschuetzten Umschlag."""
    if len(passphrase) < MIN_PASSPHRASE:
        raise RecoveryError(f"passphrase must have at least {MIN_PASSPHRASE} characters")
    if len(kek) != K.KEY_BYTES:
        raise RecoveryError("master key has the wrong length")
    salt = secrets.token_bytes(SALT_BYTES)
    wrapping = _derive(passphrase, salt, iterations=KDF_ITERATIONS,
                       lanes=KDF_LANES, memory_kib=KDF_MEMORY_KIB)
    try:
        blob = E.seal_bytes(key=wrapping, purpose=PURPOSE, plaintext=kek)
    finally:
        del wrapping
    return RecoveryEnvelope(
        format_version=FORMAT_VERSION, kdf=KDF_NAME,
        salt_b64=base64.b64encode(salt).decode("ascii"),
        iterations=KDF_ITERATIONS, lanes=KDF_LANES, memory_kib=KDF_MEMORY_KIB,
        wrapped_kek_b64=base64.b64encode(blob).decode("ascii"),
        kek_fingerprint=key_fingerprint(kek),
        created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat())


def unwrap(envelope: RecoveryEnvelope, passphrase: str) -> bytes:
    """Holt den Hauptschluessel zurueck. Eine falsche Passphrase ist EIN Fehler.

    Kein Unterschied in Meldung oder Laufzeit zwischen „falsche Passphrase" und
    „beschaedigter Umschlag": beides ist dasselbe Nein, und wer hier
    unterscheidet, baut ein Orakel.
    """
    if envelope.format_version != FORMAT_VERSION:
        raise RecoveryError(f"unknown recovery format {envelope.format_version}")
    if envelope.kdf != KDF_NAME:
        raise RecoveryError(f"unknown key derivation {envelope.kdf}")
    try:
        salt = base64.b64decode(envelope.salt_b64, validate=True)
        blob = base64.b64decode(envelope.wrapped_kek_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise RecoveryError("recovery envelope is malformed") from exc
    wrapping = _derive(passphrase, salt, iterations=envelope.iterations,
                       lanes=envelope.lanes, memory_kib=envelope.memory_kib)
    try:
        kek = E.unseal_bytes(key=wrapping, purpose=PURPOSE, blob=blob)
    except E.EnvelopeError as exc:
        raise RecoveryError("recovery envelope did not open") from exc
    finally:
        del wrapping
    if len(kek) != K.KEY_BYTES:
        raise RecoveryError("recovered key has the wrong length")
    if key_fingerprint(kek) != envelope.kek_fingerprint:
        raise RecoveryError("recovered key does not match the envelope")
    return kek


def write(envelope: RecoveryEnvelope, directory: str | None = None) -> str:
    """Legt den Umschlag ab — 0600, atomar, ohne Zwischenzustand."""
    root = directory or vault_dir()
    os.makedirs(root, mode=0o700, exist_ok=True)
    path = os.path.join(root, RECOVERY_FILE)
    temporary = path + ".new"
    previous = os.umask(0o077)
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(envelope.to_json())
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.umask(previous)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    log.info("vault.recovery_envelope_written", fingerprint=envelope.kek_fingerprint[:8])
    return path


def read(directory: str | None = None) -> RecoveryEnvelope | None:
    path = recovery_path(directory)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return RecoveryEnvelope.from_json(handle.read())


def is_current(directory: str | None = None) -> bool | None:
    """Passt der abgelegte Umschlag zum Schluessel, der jetzt gilt?

    `None` heisst „nicht feststellbar" — kein Umschlag oder kein lesbarer
    Schluessel. Das ist ausdruecklich NICHT dasselbe wie `False`, und die
    Gesundheit sagt beides verschieden.
    """
    envelope = read(directory)
    if envelope is None:
        return None
    try:
        kek = K.read_kek()
    except K.VaultError:
        return None
    if kek is None:
        return None
    try:
        return envelope.kek_fingerprint == key_fingerprint(kek)
    finally:
        del kek
