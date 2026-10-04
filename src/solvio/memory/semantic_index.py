"""Semantic Index — die DRITTE, vollstaendig ABGELEITETE Datenbank (STEP 21).

    memory.sqlite3          kanonische Wahrheit          (STEP 20)
    privacy_ledger.sqlite3  Purge-/Tombstone-Autoritaet  (STEP 20)
    semantic_index.sqlite3  DERIVED / REBUILDABLE         (STEP 21)  <-- hier

Grundsatz: Embeddings sind NIEMALS kanonische Wahrheit. Dieser Index darf jederzeit
vollstaendig geloescht und aus memory.sqlite3 rekonstruiert werden. Er ist nie
Authority — die Sichtbarkeit entscheidet immer die kanonische Schicht.

Vektoren werden als L2-normalisierte float32-BLOBs gespeichert; die Suche ist ein
Kosinus-/Dot-Product (NumPy, mit Pure-Python-Fallback). Jeder Vektor traegt seinen
profile_key (PHASE 4) — Vektoren verschiedener Profile werden nie vermischt.
"""
from __future__ import annotations

import array
import sqlite3
from datetime import datetime, timezone

from solvio.memory.embedding import EmbeddingProfile

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS embedding_profiles (
    profile_key     TEXT PRIMARY KEY,
    provider_id     TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    dimension       INTEGER NOT NULL,
    profile_version TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id    TEXT NOT NULL,
    profile_key  TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    vector       BLOB NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (memory_id, profile_key)
);
CREATE INDEX IF NOT EXISTS idx_emb_profile ON memory_embeddings(profile_key);

CREATE TABLE IF NOT EXISTS index_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode_vector(vec: list[float]) -> bytes:
    return array.array("f", vec).tobytes()


def decode_vector(blob: bytes) -> list[float]:
    a = array.array("f")
    a.frombytes(blob)
    return list(a)


def _search(ids: list[str], vectors: list[list[float]], query: list[float],
            top_k: int) -> list[tuple[str, float]]:
    """Kosinus/Dot auf normalisierten Vektoren. NumPy bevorzugt, sonst Pure-Python."""
    if not ids:
        return []
    try:
        import numpy as np  # optional, nur fuer Speed
        M = np.asarray(vectors, dtype=np.float32)
        q = np.asarray(query, dtype=np.float32)
        scores = M @ q
        k = min(top_k, len(ids))
        if k <= 0:
            return []
        idx = np.argpartition(-scores, k - 1)[:k]
        idx = idx[np.argsort(-scores[idx])]
        return [(ids[i], float(scores[i])) for i in idx]
    except ImportError:
        scored = [(ids[i], sum(a * b for a, b in zip(vectors[i], query)))
                  for i in range(len(ids))]
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]


class SemanticIndex:
    """Synchroner Wrapper um semantic_index.sqlite3. Alles hier ist derived."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")  # derived -> NORMAL genuegt
        self._conn.executescript(_SCHEMA)
        if self._conn.execute("SELECT version FROM schema_migrations").fetchone() is None:
            self._conn.execute("INSERT INTO schema_migrations(version) VALUES (?)",
                               (SCHEMA_VERSION,))
        self._conn.commit()
        self._closed = False
        # In-Memory-Matrix-Cache pro profile_key (NumPy). Wird bei jedem Write
        # invalidiert -> nie stale. Ohne NumPy: transparenter Per-Query-Fallback.
        self._cache: dict[str, tuple] = {}

    def _invalidate(self) -> None:
        self._cache.clear()

    # -- profiles -----------------------------------------------------------
    def register_profile(self, profile: EmbeddingProfile) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO embedding_profiles"
            "(profile_key, provider_id, model_id, dimension, profile_version, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (profile.key, profile.provider_id, profile.model_id, profile.dimension,
             profile.profile_version, _utcnow_str()),
        )
        self._conn.commit()

    def set_active_profile(self, profile_key: str) -> None:
        self.set_meta("active_profile", profile_key)

    def active_profile(self) -> str | None:
        return self.get_meta("active_profile")

    def list_profiles(self) -> list[str]:
        return [r["profile_key"] for r in
                self._conn.execute("SELECT profile_key FROM embedding_profiles ORDER BY created_at")]

    # -- metadata -----------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO index_metadata(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM index_metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # -- vectors ------------------------------------------------------------
    def upsert(self, memory_id: str, profile_key: str, content_hash: str,
               vector: list[float]) -> None:
        self._conn.execute(
            "INSERT INTO memory_embeddings"
            "(memory_id, profile_key, content_hash, dim, vector, created_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(memory_id, profile_key) DO UPDATE SET "
            "content_hash=excluded.content_hash, dim=excluded.dim, "
            "vector=excluded.vector, created_at=excluded.created_at",
            (memory_id, profile_key, content_hash, len(vector),
             encode_vector(vector), _utcnow_str()),
        )
        self._conn.commit()
        self._invalidate()

    def get_entry(self, memory_id: str, profile_key: str) -> tuple[str, list[float]] | None:
        row = self._conn.execute(
            "SELECT content_hash, vector FROM memory_embeddings WHERE memory_id=? AND profile_key=?",
            (memory_id, profile_key)).fetchone()
        if row is None:
            return None
        return row["content_hash"], decode_vector(row["vector"])

    def content_hash_of(self, memory_id: str, profile_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT content_hash FROM memory_embeddings WHERE memory_id=? AND profile_key=?",
            (memory_id, profile_key)).fetchone()
        return row["content_hash"] if row else None

    def delete(self, memory_id: str, profile_key: str | None = None) -> None:
        if profile_key is None:
            self._conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))
        else:
            self._conn.execute(
                "DELETE FROM memory_embeddings WHERE memory_id=? AND profile_key=?",
                (memory_id, profile_key))
        self._conn.commit()
        self._invalidate()

    def delete_ids(self, memory_ids, profile_key: str | None = None) -> int:
        ids = list(memory_ids)
        if not ids:
            return 0
        n = 0
        for rid in ids:
            if profile_key is None:
                cur = self._conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (rid,))
            else:
                cur = self._conn.execute(
                    "DELETE FROM memory_embeddings WHERE memory_id=? AND profile_key=?",
                    (rid, profile_key))
            n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        self._conn.commit()
        self._invalidate()
        return n

    def ids_for_profile(self, profile_key: str) -> set[str]:
        return {r["memory_id"] for r in self._conn.execute(
            "SELECT memory_id FROM memory_embeddings WHERE profile_key=?", (profile_key,))}

    def load_matrix(self, profile_key: str, dim: int) -> tuple[list[str], list[list[float]]]:
        ids, vecs = [], []
        for r in self._conn.execute(
            "SELECT memory_id, dim, vector FROM memory_embeddings WHERE profile_key=?",
            (profile_key,)):
            if r["dim"] != dim:      # Dimension-Mismatch -> stale, ueberspringen
                continue
            ids.append(r["memory_id"])
            vecs.append(decode_vector(r["vector"]))
        return ids, vecs

    def search(self, query_vec: list[float], profile_key: str, top_k: int) -> list[tuple[str, float]]:
        """Kosinus-Suche. Mit NumPy: die Matrix wird pro Profil EINMAL geladen und
        im RAM gecacht (Invalidierung bei jedem Write) -> pro Query nur ein Dot-Product.
        Ohne NumPy: transparenter Per-Query-Fallback ueber load_matrix."""
        dim = len(query_vec)
        try:
            import numpy as np
        except ImportError:
            ids, vecs = self.load_matrix(profile_key, dim)
            return _search(ids, vecs, query_vec, top_k)
        entry = self._cache.get(profile_key)
        if entry is None or entry[2] != dim:
            ids, vecs = self.load_matrix(profile_key, dim)
            mat = np.asarray(vecs, dtype=np.float32) if vecs else np.zeros((0, dim), dtype=np.float32)
            entry = (ids, mat, dim)
            self._cache[profile_key] = entry
        ids, mat, _dim = entry
        if not ids:
            return []
        scores = mat @ np.asarray(query_vec, dtype=np.float32)
        k = min(top_k, len(ids))
        idx = np.argpartition(-scores, k - 1)[:k]
        idx = idx[np.argsort(-scores[idx])]
        return [(ids[i], float(scores[i])) for i in idx]

    # -- housekeeping -------------------------------------------------------
    def count(self, profile_key: str | None = None) -> int:
        if profile_key is None:
            return int(self._conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0])
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE profile_key=?", (profile_key,)).fetchone()[0])

    def integrity_check(self) -> str:
        return self._conn.execute("PRAGMA integrity_check").fetchone()[0]

    def drop_all(self) -> None:
        self._conn.execute("DELETE FROM memory_embeddings")
        self._conn.commit()
        self._invalidate()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self._conn.close()
        self._closed = True
