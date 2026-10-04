"""Der Umschlag — versioniert, authentifiziert, an seine Metadaten gebunden.

Hier wird nichts erfunden. AES-256-GCM aus `cryptography`, zufaellige Nonce je
Vorgang, und die zusaetzlichen Daten (AAD) tragen die Bindung. Was hier
tatsaechlich entschieden wurde, sind drei Dinge:

**Zwei Ebenen statt einer.** Der Hauptschluessel (KEK) liegt im Schluesselbund
und verschluesselt nur je einen Datenschluessel (DEK); der DEK verschluesselt
genau ein Geheimnis. Der Gewinn ist nicht Geschwindigkeit — bei zwanzig
Zugaengen misst das niemand — sondern Aufraeumbarkeit: einen einzelnen Zugang
kryptografisch zu erledigen heisst, seinen DEK zu vergessen, und das beruehrt
keinen anderen Eintrag. Ausserdem laesst sich der KEK tauschen, ohne jedes
Geheimnis im Klartext sehen zu muessen? Nein — genau das laesst sich NICHT, und
das steht hier, damit es niemand annimmt: eine KEK-Rotation entpackt jeden DEK
und packt ihn neu ein. Der Klartext der Geheimnisse bleibt dabei unberuehrt, und
das ist der eigentliche Gewinn.

**Die Policy steht im AAD.** Der zulaessige Zweck eines Geheimnisses — welche
Faehigkeit, welche Domaene, welcher Executor, welcher Zustand — geht als
Pruefsumme in die zusaetzlichen Daten ein. Wer die Policyzeile in der Datenbank
aendert, ohne den Umschlag neu zu bilden, macht den Geheimtext unentschluesselbar.
Damit ist „Metadaten manipulieren und trotzdem benutzen" kein Angriff mehr,
sondern ein Fehlschlag. Der Preis ist ausgesprochen: jede Policyaenderung ist
eine Neuversiegelung, und die braucht den KEK — also einen entsperrten
Schluesselbund. Das ist der richtige Preis: eine Policy zu aendern IST ein
Eingriff.

**Die Fassung steht im Klartext davor.** Ein Umschlag ohne Fassungsnummer ist
ein Umschlag, den man nur einmal bauen kann. Die Nummer liegt ausserhalb des
Geheimtextes, geht aber ins AAD ein — sie ist damit lesbar und trotzdem nicht
faelschbar.

Was NICHT hier steht: der Schluessel. Er kommt aus `solvio.secret_vault.keyring` und
wird durchgereicht, nie gehalten.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any, Mapping

#: Fassung des Umschlagformats. Aendert sich das Verfahren, aendert sich diese
#: Zahl — und alter Geheimtext bleibt lesbar, weil die Nummer mitgeschrieben ist.
ENVELOPE_VERSION = 1

#: Laenge der Nonce fuer AES-GCM. 96 Bit ist die Groesse, fuer die GCM entworfen
#: wurde; alles andere laeuft durch eine zusaetzliche Ableitung und ist damit
#: schwerer nachzuvollziehen, ohne sicherer zu sein.
NONCE_BYTES = 12
DEK_BYTES = 32

#: Domaenentrenner. Ein Umschlag aus dem einen Zusammenhang entschluesselt nie im
#: anderen — auch dann nicht, wenn beide denselben Schluessel benutzen.
_DOMAIN_WRAP = b"solvio-vault-kek-v1"
_DOMAIN_SECRET = b"solvio-vault-secret-v1"
_DOMAIN_FILE = b"solvio-vault-file-v1"


class EnvelopeError(ValueError):
    """Der Umschlag traegt nicht. Traegt selbst nie einen Wert."""


def canonical_json(payload: Mapping[str, Any]) -> bytes:
    """Eine Abbildung, die auf allen Wegen dieselben Bytes ergibt."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def policy_digest(fields: Mapping[str, Any]) -> str:
    """Die Pruefsumme ueber genau die Felder, die Befugnis bedeuten.

    WELCHE Felder das sind, entscheidet `solvio.secret_vault.policy` — nicht dieses
    Modul. Hier steht nur, wie gerechnet wird.
    """
    return hashlib.sha256(canonical_json(fields)).hexdigest()


def _aad(domain: bytes, *parts: str) -> bytes:
    # `\x00` als Trenner: er kann in keinem der Teile vorkommen (Verweise sind
    # auf ein Zeichenalphabet begrenzt, Pruefsummen sind Hex, Zahlen sind Zahlen),
    # also lassen sich zwei verschiedene Tupel nie auf dieselben Bytes abbilden.
    joined = b"\x00".join(p.encode("utf-8") for p in parts)
    return domain + b"\x00" + joined


def _aesgcm(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key)


def _encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce + _aesgcm(key).encrypt(nonce, plaintext, aad)


def _decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(blob) <= NONCE_BYTES + 16:
        raise EnvelopeError("ciphertext is truncated")
    nonce, body = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        return _aesgcm(key).decrypt(nonce, body, aad)
    except Exception as exc:  # noqa: BLE001 - jede Ursache ist dieselbe Aussage
        # Absichtlich EINE Meldung fuer alle Faelle: falscher Schluessel,
        # veraenderte Metadaten, beschaedigter Geheimtext. Wer hier
        # unterscheidet, baut ein Orakel.
        raise EnvelopeError("envelope did not open") from exc


@dataclass(frozen=True)
class Sealed:
    """Ein versiegeltes Geheimnis. Enthaelt keinen Klartext und keine Laenge."""

    envelope_version: int
    wrapped_dek: bytes
    ciphertext: bytes

    def __repr__(self) -> str:
        return (f"Sealed(v={self.envelope_version}, wrapped={len(self.wrapped_dek)}B, "
                f"cipher=<redacted>)")


def seal(*, kek: bytes, ref: str, version: int, policy_sha256: str,
         plaintext: bytes) -> Sealed:
    """Versiegelt EIN Geheimnis unter EINEM frischen Datenschluessel."""
    if not plaintext:
        raise EnvelopeError("refusing to seal an empty secret")
    dek = secrets.token_bytes(DEK_BYTES)
    wrapped = _encrypt(kek, dek,
                       _aad(_DOMAIN_WRAP, ref, str(version), str(ENVELOPE_VERSION)))
    cipher = _encrypt(dek, plaintext,
                      _aad(_DOMAIN_SECRET, ref, str(version), str(ENVELOPE_VERSION),
                           policy_sha256))
    return Sealed(ENVELOPE_VERSION, wrapped, cipher)


def unseal(*, kek: bytes, ref: str, version: int, policy_sha256: str,
           sealed: Sealed) -> bytes:
    """Oeffnet EIN Geheimnis. Fehler sind immer derselbe Fehler."""
    if sealed.envelope_version != ENVELOPE_VERSION:
        raise EnvelopeError(f"unknown envelope version {sealed.envelope_version}")
    dek = _decrypt(kek, sealed.wrapped_dek,
                   _aad(_DOMAIN_WRAP, ref, str(version), str(sealed.envelope_version)))
    try:
        return _decrypt(dek, sealed.ciphertext,
                        _aad(_DOMAIN_SECRET, ref, str(version),
                             str(sealed.envelope_version), policy_sha256))
    finally:
        # Ehrlich zur Grenze: `del` gibt eine Referenz frei, es loescht kein RAM.
        # CPython hat keine Zusage darueber, wann und ob der Speicher
        # ueberschrieben wird. Die Zeile steht trotzdem, weil sie die Lebensdauer
        # der Referenz verkuerzt — nicht, weil sie eine Garantie waere.
        del dek


def seal_bytes(*, key: bytes, purpose: str, plaintext: bytes) -> bytes:
    """Ein einstufiger Umschlag fuer Dateien — Sicherung, Umschlag, Ablage.

    Bewusst OHNE zweite Ebene: hier gibt es nichts einzeln zu vergessen, und ein
    Format weniger ist ein Fehler weniger.
    """
    return _encrypt(key, plaintext, _aad(_DOMAIN_FILE, purpose, str(ENVELOPE_VERSION)))


def unseal_bytes(*, key: bytes, purpose: str, blob: bytes) -> bytes:
    return _decrypt(key, blob, _aad(_DOMAIN_FILE, purpose, str(ENVELOPE_VERSION)))
