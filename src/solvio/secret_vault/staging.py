"""Das Aufnahmelager — wo ein Wert wartet, waehrend ein Mensch entscheidet.

Warum es das ueberhaupt gibt, ist eine Messung und keine Vorliebe. Der
Freigabeweg schreibt die ARGUMENTE einer Faehigkeit als Text in die
Freigabe-Buchhaltung (`capabilities/approval_gateway.py:111` bildet je Argument
eine Zeile `Bezeichnung: <json>`), und dieser Text wird gehasht, auf dem iPhone
angezeigt und in `approval_control.sqlite3` abgelegt. Eine Vault-Faehigkeit, die
ihren Wert als Argument mitgibt, legt ihn damit im Klartext in eine dauerhafte
Datenbank — und zwar in die eine, die es am wenigsten verdient.

Also reist der Wert nicht als Argument. Er kommt EINMAL ueber den attestierten
Aufnahmeweg herein, wird sofort unter dem Hauptschluessel versiegelt und
bekommt eine zufaellige Kennung. Was danach durch Router, Freigabe, Anzeige,
Digest und Journal laeuft, ist diese Kennung — ein Wegwerfwort ohne Bedeutung.

Vier Eigenschaften, und jede hat einen Grund:

* **Nur im Arbeitsspeicher.** Ein Neustart verliert die Einlagerung, und das ist
  richtig: eine halb bestaetigte Aufnahme soll einen Neustart nicht ueberleben.
* **Versiegelt.** Der Wert liegt auch hier als Geheimtext, nicht als
  Zeichenkette. Er wird genau zweimal Klartext: bei der Aufnahme und beim
  Einlagern in den Tresor.
* **Genau einmal.** Die Kennung wird beim Abholen verbraucht. Ein zweiter
  Versuch trifft auf nichts.
* **Mit Frist und an das Geraet gebunden.** Fuenf Minuten sind reichlich fuer
  eine Face-ID-Runde und knapp fuer alles andere.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from solvio.logging_setup import get_logger
from solvio.secret_vault import envelope as E
from solvio.secret_vault import keyring as K

log = get_logger("vault")

#: Wie lange ein eingelagerter Wert hoechstens wartet. Zwischen Aufnahme und
#: Freigabe liegt ein Mensch, der liest, was er gleich tut — und danach eine
#: Face-ID-Runde. Fuenf Minuten decken das mit Rand.
DEFAULT_TTL = 300.0

#: Wie viele Werte gleichzeitig warten duerfen. Der Tresor bekommt seine
#: Eintraege einzeln von einem Menschen; alles darueber ist ein Fehler oder ein
#: Versuch, den Speicher zu fuellen.
MAX_PENDING = 8

PURPOSE = "vault-staging"


class StagingError(RuntimeError):
    """Die Einlagerung ging nicht. Traegt nie einen Wert."""


@dataclass
class _Slot:
    blob: bytes
    device_id: str
    payload_sha256: str
    issued_at: float
    ttl: float
    spent: bool = False

    def expired(self, now: float) -> bool:
        return (now - self.issued_at) > self.ttl

    def __repr__(self) -> str:
        return (f"_Slot(device={self.device_id[:8]} "
                f"payload={self.payload_sha256[:8]} spent={self.spent})")


class SecretStaging:
    """Kurzlebige, versiegelte Einlagerung. Prozesslokal, nie auf Platte."""

    def __init__(self, ttl: float = DEFAULT_TTL) -> None:
        self.ttl = ttl
        self._slots: dict[str, _Slot] = {}

    def _sweep(self, now: float) -> None:
        for key in [k for k, s in self._slots.items() if s.expired(now) or s.spent]:
            self._slots.pop(key, None)

    def stage(self, plaintext: bytes, *, device_id: str,
              payload_sha256: str) -> str:
        """Nimmt EINEN Wert auf und gibt seine Kennung zurueck.

        Der Hauptschluessel wird HIER schon gebraucht. Das ist Absicht: ein
        gesperrter Schluesselbund faellt damit auf, BEVOR ein Mensch eine
        Freigabe bestaetigt, die danach ins Leere liefe.
        """
        if not plaintext:
            raise StagingError("nothing to stage")
        now = time.monotonic()
        self._sweep(now)
        if len(self._slots) >= MAX_PENDING:
            raise StagingError("too many pending secrets")
        try:
            kek = K.read_kek()
        except K.VaultLocked as exc:
            raise StagingError("vault_unavailable") from exc
        if kek is None:
            raise StagingError("vault_not_initialised")
        try:
            blob = E.seal_bytes(key=kek, purpose=PURPOSE, plaintext=plaintext)
        finally:
            del kek
        staging_id = "stg-" + secrets.token_hex(16)
        self._slots[staging_id] = _Slot(blob=blob, device_id=device_id,
                                        payload_sha256=payload_sha256,
                                        issued_at=now, ttl=self.ttl)
        log.info("vault.staged", device=device_id[:12], staging=staging_id[:12])
        return staging_id

    def peek(self, staging_id: str, *, device_id: str) -> str:
        """Der Nutzlast-Hash der Einlagerung — ohne sie zu verbrauchen.

        Gebraucht, damit die Bindung ueber genau dasselbe rechnet wie bei der
        ersten Anfrage, wenn das iPhone nach der Face-ID-Runde erneut anklopft.
        """
        slot = self._slots.get(staging_id)
        now = time.monotonic()
        if slot is None or slot.spent or slot.expired(now):
            raise StagingError("unknown_or_expired_staging")
        if slot.device_id != device_id:
            raise StagingError("staging_belongs_to_another_device")
        return slot.payload_sha256

    def take(self, staging_id: str, *, device_id: str) -> bytes:
        """Holt den Wert genau einmal heraus. Danach ist die Kennung tot."""
        now = time.monotonic()
        self._sweep(now)
        slot = self._slots.get(staging_id)
        if slot is None or slot.spent or slot.expired(now):
            raise StagingError("unknown_or_expired_staging")
        if slot.device_id != device_id:
            raise StagingError("staging_belongs_to_another_device")
        slot.spent = True
        self._slots.pop(staging_id, None)
        try:
            kek = K.read_kek()
        except K.VaultLocked as exc:
            raise StagingError("vault_unavailable") from exc
        if kek is None:
            raise StagingError("vault_not_initialised")
        try:
            return E.unseal_bytes(key=kek, purpose=PURPOSE, blob=slot.blob)
        except E.EnvelopeError as exc:
            raise StagingError("staged_value_did_not_open") from exc
        finally:
            del kek
            slot.blob = b""

    def drop(self, staging_id: str) -> None:
        self._slots.pop(staging_id, None)

    def pending(self) -> int:
        self._sweep(time.monotonic())
        return len(self._slots)

    def __repr__(self) -> str:
        return f"<SecretStaging pending={len(self._slots)}>"
