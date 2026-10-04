"""Schema-Versionierung + Migrationen fuer den nativen SOLVIO-Memory-Store (STEP 20).

Bewusst minimal: eine schema_migrations-Tabelle mit genau einer Zeile (version).
Kuenftige Schema-Aenderungen haengen eine neue MIGRATION an und erhoehen
SCHEMA_VERSION; migrate() ist idempotent.
"""
from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

# --- Version 1: kanonischer aktiver Memory-Store ---------------------------
_V1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id                        TEXT PRIMARY KEY,
    memory_type               TEXT NOT NULL,
    content                   TEXT NOT NULL,
    subject                   TEXT NOT NULL,
    source                    TEXT NOT NULL,
    source_type               TEXT NOT NULL,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    trust_level               TEXT NOT NULL,
    sensitivity               TEXT NOT NULL,
    retention_mode            TEXT NOT NULL DEFAULT 'default',
    retention_ttl_days        INTEGER,
    retention_purge_on_expiry INTEGER NOT NULL DEFAULT 0,
    confidence                REAL NOT NULL DEFAULT 1.0,
    importance                REAL NOT NULL DEFAULT 0.5,
    valid_from                TEXT,
    valid_until               TEXT,
    supersedes                TEXT,
    superseded_by             TEXT,
    forgotten                 INTEGER NOT NULL DEFAULT 0,
    forgotten_at              TEXT,
    forgotten_reason          TEXT,
    tags                      TEXT NOT NULL DEFAULT '[]',
    metadata                  TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_mem_subject       ON memories(subject);
CREATE INDEX IF NOT EXISTS idx_mem_type          ON memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_mem_superseded_by ON memories(superseded_by);
CREATE INDEX IF NOT EXISTS idx_mem_active        ON memories(forgotten, superseded_by);

CREATE TABLE IF NOT EXISTS provenance (
    memory_id   TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    source      TEXT NOT NULL,
    trust_level TEXT NOT NULL,
    at          TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (memory_id, seq),
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS relations (
    memory_id TEXT NOT NULL,
    kind      TEXT NOT NULL,
    target_id TEXT NOT NULL,
    PRIMARY KEY (memory_id, kind, target_id),
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
"""


def _fts_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def migrate(conn: sqlite3.Connection) -> int:
    """Wendet ausstehende Migrationen an. Idempotent. Gibt die Endversion zurueck.

    Legt bei vorhandenem FTS5 die externe Suchtabelle memories_fts an; fehlt FTS5,
    faellt der Store auf eine LIKE-Suche zurueck (kein harter Fehler).
    """
    conn.executescript(_V1)
    if _fts_available(conn):
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
            "USING fts5(id UNINDEXED, content, subject, tags)"
        )
    row = conn.execute("SELECT version FROM schema_migrations LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (SCHEMA_VERSION,))
    else:
        conn.execute("UPDATE schema_migrations SET version = ?", (SCHEMA_VERSION,))
    conn.commit()
    return SCHEMA_VERSION


def current_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT version FROM schema_migrations LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0]) if row else None


def has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone()
    return row is not None
