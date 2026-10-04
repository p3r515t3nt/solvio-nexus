"""Managed Backup & Restore fuer den nativen SOLVIO-Memory-Store (STEP 20).

Kernidee (Pre-Purge-Restore-Sicherheit):
  Backup  = konsistenter Einzeldatei-Snapshot NUR von memory.sqlite3 (+ Manifest).
  Restore = Backup -> Staging -> integrity_check -> **aktuellen Privacy-Ledger
            anwenden** (gepurgte IDs aus dem Staging entfernen) -> erst dann atomar
            aktiv schalten.
Der Privacy-Ledger ist NICHT Teil des Backups und wird daher durch das
Einspielen eines alten Backups nicht zurueckgerollt. Ergebnis: Ein Restore eines
Vor-Purge-Backups bringt gepurgte Daten NICHT zurueck.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass

from solvio.memory import migrations
from solvio.memory.privacy_ledger import hash_id
from solvio.memory.sqlite_backend import utcnow, dt_to_str

_MANIFEST_SUFFIX = ".manifest.json"
_CHUNK = 1 << 20


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: str, data: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass
class BackupManifest:
    schema_version: int
    created_at: str
    record_count: int
    sha256: str
    format_version: int = 1
    tool: str = "solvio-memory"

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=True)


# --------------------------------------------------------------------------- create
def create_backup(src_conn: sqlite3.Connection, dest_path: str) -> BackupManifest:
    """Konsistenter Snapshot der aktiven Memory-DB als Einzeldatei + Manifest."""
    parent = os.path.dirname(os.path.abspath(dest_path))
    os.makedirs(parent, exist_ok=True)
    if os.path.exists(dest_path):
        os.remove(dest_path)
    dst = sqlite3.connect(dest_path)  # Ziel bewusst OHNE WAL (Einzeldatei)
    try:
        src_conn.backup(dst)
        rec = int(dst.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        ver = migrations.current_version(dst) or migrations.SCHEMA_VERSION
    finally:
        dst.close()
    manifest = BackupManifest(
        schema_version=ver,
        created_at=dt_to_str(utcnow()),  # type: ignore[arg-type]
        record_count=rec,
        sha256=_sha256_file(dest_path),
    )
    _atomic_write(dest_path + _MANIFEST_SUFFIX, manifest.to_json())
    return manifest


# --------------------------------------------------------------------------- verify
def verify_backup(backup_path: str) -> dict:
    """Prueft Manifest-Existenz, Checksumme, SQLite-Integritaet und Schema-Version."""
    errors: list[str] = []
    mpath = backup_path + _MANIFEST_SUFFIX
    if not os.path.exists(backup_path):
        return {"ok": False, "errors": [f"backup file missing: {backup_path}"]}
    if not os.path.exists(mpath):
        return {"ok": False, "errors": [f"manifest missing: {mpath}"]}
    with open(mpath, encoding="utf-8") as fh:
        manifest = json.loads(fh.read())

    actual = _sha256_file(backup_path)
    if actual != manifest.get("sha256"):
        errors.append(f"checksum mismatch: expected {manifest.get('sha256')}, got {actual}")

    # Das Schliessen gehoert in ein `finally`, nicht ans Ende des `try`.
    # `sqlite3.connect()` liest den Dateikopf nicht — eine beschaedigte Sicherung
    # faellt erst beim ersten `PRAGMA` auf, und genau dieser Pfad sprang frueher
    # am `close()` vorbei. Gefunden beim Verbindungs-Audit nach dem Leck im
    # Hintergrundspeicher; hier ist es dieselbe Form, nur seltener.
    conn = None
    try:
        conn = sqlite3.connect(backup_path)
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ic != "ok":
            errors.append(f"integrity_check failed: {ic}")
        ver = migrations.current_version(conn)
        if ver != migrations.SCHEMA_VERSION:
            errors.append(f"schema_version {ver} != supported {migrations.SCHEMA_VERSION}")
    except sqlite3.DatabaseError as exc:
        errors.append(f"cannot open backup: {exc}")
    finally:
        if conn is not None:
            conn.close()

    return {"ok": not errors, "errors": errors, "manifest": manifest}


# -------------------------------------------------------------------------- restore
def build_restored_db(backup_path: str, staging_path: str, purged_id_hashes: set[str]) -> dict:
    """Erzeugt aus dem Backup eine Staging-DB und entfernt darin alle gepurgten IDs.

    Gibt Statistik zurueck. Wirft bei Integritaets-/Schemafehlern (Fail-closed).
    """
    v = verify_backup(backup_path)
    if not v["ok"]:
        raise ValueError(f"refusing restore, backup invalid: {v['errors']}")

    if os.path.exists(staging_path):
        os.remove(staging_path)
    for sfx in ("-wal", "-shm"):
        if os.path.exists(staging_path + sfx):
            os.remove(staging_path + sfx)
    shutil.copyfile(backup_path, staging_path)

    # Ein `finally` um den ganzen Block: hier liegt eine offene Verbindung auf
    # der Staging-Datei, die anschliessend per `os.replace` an ihren Platz
    # geschoben wird. Ein Fehler in der Mitte hinterliess frueher genau dort ein
    # offenes Handle — die unangenehmste Stelle im ganzen Modul.
    conn = sqlite3.connect(staging_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ic != "ok":
            raise ValueError(f"staging integrity_check failed: {ic}")

        has_fts = migrations.has_fts(conn)
        removed = 0
        if purged_id_hashes:
            rows = conn.execute("SELECT id FROM memories").fetchall()
            to_delete = [r[0] for r in rows if hash_id(r[0]) in purged_id_hashes]
            for rid in to_delete:
                conn.execute("DELETE FROM memories WHERE id=?", (rid,))  # cascade prov/rel
                if has_fts:
                    conn.execute("DELETE FROM memories_fts WHERE id=?", (rid,))
            removed = len(to_delete)
        conn.commit()
        remaining = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return {"removed": removed, "remaining": remaining, "staging_path": staging_path}
