"""Privacy-Ledger: der Purge-/Tombstone-Speicher (STEP 20, gehaertet 20.1).

BEWUSST eine EIGENE Datenbank (privacy_ledger.sqlite3), physisch getrennt vom
kanonischen Memory-Store. Grund: Ein Restore eines alten Memory-Backups darf den
Ledger NIEMALS zuruecksetzen. Der Ledger ist die dauerhafte Wahrheit darueber,
was geloescht wurde — und wird beim Managed-Restore auf den wiederhergestellten
Stand angewandt, sodass gepurgte Daten nicht zurueckkehren.

Ein Tombstone ist INHALTSLOS: er enthaelt nur Einweg-Hashes von id/subject sowie
Zeitpunkt + Grund. Keine personenbezogenen Klartextdaten.

Durability (20.1): Der Ledger ist sicherheitskritischer als normale Memory-Writes.
Er laeuft mit journal_mode=WAL UND synchronous=FULL, damit ein bestaetigter
Purge-Tombstone einen normalen Power-Loss moeglichst uebersteht. Ausserdem wird
ein bereits vorhandener Ledger beim Oeffnen auf Integritaet geprueft (fail-closed).
"""
from __future__ import annotations

import hashlib
import os
import sqlite3

from solvio.contracts.memory import Tombstone
from solvio.memory.sqlite_backend import dt_to_str, str_to_dt, utcnow

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tombstones (
    id_hash      TEXT PRIMARY KEY,
    subject_hash TEXT NOT NULL,
    purged_at    TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tomb_subject ON tombstones(subject_hash);
"""
LEDGER_VERSION = 1
_REQUIRED_TABLES = ("ledger_version", "tombstones")


class LedgerError(Exception):
    """Der Privacy-Ledger fehlt oder ist beschaedigt. Wird fail-closed behandelt."""


def hash_id(rec_id: str) -> str:
    return hashlib.sha256(("solvio-id:" + rec_id).encode("utf-8")).hexdigest()


def hash_subject(subject: str) -> str:
    return hashlib.sha256(("solvio-subject:" + subject).encode("utf-8")).hexdigest()


def validate_ledger_file(path: str) -> tuple[bool, str]:
    """Fail-closed-Pruefung des Ledgers ueber eine FRISCHE Verbindung (fuer Restore).

    Prueft: Datei existiert, SQLite-integrity_check == ok, erwartete Tabellen da.
    Gibt (ok, fehlermeldung) zurueck. Die Meldung enthaelt keine personenbezogenen Daten.
    """
    if not os.path.exists(path):
        return (False, "privacy ledger file is missing")
    conn = None
    try:
        conn = sqlite3.connect(path)
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ic != "ok":
            return (False, f"privacy ledger integrity_check failed: {ic}")
        for tbl in _REQUIRED_TABLES:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
            ).fetchone()
            if row is None:
                return (False, f"privacy ledger missing table: {tbl}")
    except sqlite3.DatabaseError as exc:
        return (False, f"privacy ledger unreadable: {exc}")
    finally:
        if conn is not None:
            conn.close()
    return (True, "")


class PrivacyLedger:
    """Duenner, synchroner Wrapper um die Tombstone-Datenbank."""

    def __init__(self, path: str) -> None:
        self.path = path
        pre_existing = os.path.exists(path) and os.path.getsize(path) > 0
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if pre_existing:
            # Fail-closed: mit einem beschaedigten Privacy-Ledger nicht weiterlaufen.
            try:
                ic = self._conn.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                self._conn.close()
                raise LedgerError(f"privacy ledger unreadable on open: {exc}") from exc
            if ic != "ok":
                self._conn.close()
                raise LedgerError(f"privacy ledger corrupt on open: {ic}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=FULL")  # 20.1: max Durability fuer Purges
        self._conn.executescript(_LEDGER_SCHEMA)
        if self._conn.execute("SELECT 1 FROM ledger_version").fetchone() is None:
            self._conn.execute("INSERT INTO ledger_version(version) VALUES (?)", (LEDGER_VERSION,))
        self._conn.commit()
        self._closed = False

    # -- writes -------------------------------------------------------------
    def add(self, rec_id: str, subject: str, reason: str, *, purged_at=None) -> Tombstone:
        pa = purged_at or utcnow()
        idh, sbh = hash_id(rec_id), hash_subject(subject)
        self._conn.execute(
            "INSERT OR IGNORE INTO tombstones(id_hash, subject_hash, purged_at, reason) "
            "VALUES (?,?,?,?)",
            (idh, sbh, dt_to_str(pa), reason),
        )
        self._conn.commit()  # durabler Commit (synchronous=FULL) VOR dem aktiven Delete
        return Tombstone(subject_hash=sbh, purged_at=pa, reason=reason)

    # -- reads --------------------------------------------------------------
    def contains_id(self, rec_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM tombstones WHERE id_hash=?", (hash_id(rec_id),)
        ).fetchone() is not None

    def purged_id_hashes(self) -> set[str]:
        return {r["id_hash"] for r in self._conn.execute("SELECT id_hash FROM tombstones")}

    def list_tombstones(self) -> list[Tombstone]:
        return [
            Tombstone(
                subject_hash=r["subject_hash"],
                purged_at=str_to_dt(r["purged_at"]),  # type: ignore[arg-type]
                reason=r["reason"],
            )
            for r in self._conn.execute(
                "SELECT subject_hash, purged_at, reason FROM tombstones ORDER BY purged_at, id_hash"
            )
        ]

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0])

    def integrity_check(self) -> str:
        return self._conn.execute("PRAGMA integrity_check").fetchone()[0]

    def checkpoint(self) -> None:
        if self._closed:
            return
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self.checkpoint()
        self._conn.close()
        self._closed = True
