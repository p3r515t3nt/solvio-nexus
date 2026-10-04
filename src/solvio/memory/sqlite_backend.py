"""Synchrone SQLite-Kernschicht des nativen SOLVIO-Memory-Stores (STEP 20).

Kapselt: Verbindung/Pragmas, Serialisierung MemoryRecord <-> Zeilen, die
kanonischen CRUD-/Query-Operationen und die Synchronisation des FTS-Index.
Alle Funktionen sind SYNCHRON; die async-Fassade (store.py) ruft sie in einem
dedizierten Single-Thread-Executor auf, sodass der Event-Loop nicht blockiert
und der Zugriff serialisiert bleibt.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from solvio.contracts.memory import (
    MemoryRecord,
    MemoryType,
    ProvenanceEntry,
    Relation,
    RetentionPolicy,
    Sensitivity,
)
from solvio.contracts.trust import SourceType, TrustLevel
from solvio.memory import migrations

_TOKEN_RE = re.compile(r"[0-9A-Za-zÀ-ÿ]+")
# Felder, die update() aendern darf (kein neuer Record).
_UPDATABLE = {
    "content", "subject", "confidence", "importance", "sensitivity",
    "valid_from", "valid_until", "tags", "metadata", "trust_level",
}


# --------------------------------------------------------------------------- time
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dt_to_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def str_to_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------------- connect
def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def open_store(path: str) -> tuple[sqlite3.Connection, bool]:
    """Verbindung + Migration; gibt (conn, has_fts) zurueck."""
    conn = connect(path)
    migrations.migrate(conn)
    return conn, migrations.has_fts(conn)


# ------------------------------------------------------------------ serialisation
def _fts_match(query: str) -> str | None:
    toks = _TOKEN_RE.findall(query or "")
    if not toks:
        return None
    return " OR ".join('"' + t.replace('"', "") + '"' for t in toks)


def _record_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> MemoryRecord:
    prov = [
        ProvenanceEntry(
            source_type=SourceType(p["source_type"]),
            source=p["source"],
            trust_level=TrustLevel(p["trust_level"]),
            at=str_to_dt(p["at"]),  # type: ignore[arg-type]
            note=p["note"],
        )
        for p in conn.execute(
            "SELECT source_type, source, trust_level, at, note FROM provenance "
            "WHERE memory_id=? ORDER BY seq", (row["id"],)
        )
    ]
    rels = [
        Relation(kind=r["kind"], target_id=r["target_id"])
        for r in conn.execute(
            "SELECT kind, target_id FROM relations WHERE memory_id=? ORDER BY kind, target_id",
            (row["id"],),
        )
    ]
    return MemoryRecord(
        id=row["id"],
        memory_type=MemoryType(row["memory_type"]),
        content=row["content"],
        subject=row["subject"],
        source=row["source"],
        source_type=SourceType(row["source_type"]),
        created_at=str_to_dt(row["created_at"]),  # type: ignore[arg-type]
        updated_at=str_to_dt(row["updated_at"]),  # type: ignore[arg-type]
        trust_level=TrustLevel(row["trust_level"]),
        sensitivity=Sensitivity(row["sensitivity"]),
        retention_policy=RetentionPolicy(
            mode=row["retention_mode"],
            ttl_days=row["retention_ttl_days"],
            purge_on_expiry=bool(row["retention_purge_on_expiry"]),
        ),
        confidence=row["confidence"],
        importance=row["importance"],
        valid_from=str_to_dt(row["valid_from"]),
        valid_until=str_to_dt(row["valid_until"]),
        provenance=prov,
        supersedes=row["supersedes"],
        superseded_by=row["superseded_by"],
        tags=json.loads(row["tags"]),
        relations=rels,
        metadata=json.loads(row["metadata"]),
    )


def _fts_upsert(conn: sqlite3.Connection, has_fts: bool, rec_id: str,
                content: str, subject: str, tags: list[str]) -> None:
    if not has_fts:
        return
    conn.execute("DELETE FROM memories_fts WHERE id=?", (rec_id,))
    conn.execute(
        "INSERT INTO memories_fts(id, content, subject, tags) VALUES (?,?,?,?)",
        (rec_id, content, subject, " ".join(tags)),
    )


def fts_delete(conn: sqlite3.Connection, has_fts: bool, rec_id: str) -> None:
    if has_fts:
        conn.execute("DELETE FROM memories_fts WHERE id=?", (rec_id,))


# ------------------------------------------------------------------------- writes
def _refuse_credentials(text: str, where: str) -> None:
    """Der Zaun des Tresors, an der engsten Stelle des Gedaechtnisses.

    Er steht HIER und nicht (nur) eine Schicht hoeher, weil dies die einzige
    Funktion im Baum ist, die `INSERT INTO memories` ausfuehrt. Die
    Semantikschicht hatte den Zaun bisher an zwei von vier Eingaengen —
    `memory_correct` und ein bestaetigter Kandidat kamen ungeprueft durch. Ein
    Zaun, den ein kuenftiger Autor vergessen kann, ist keiner.

    Der Import liegt in der Funktion: `solvio.secret_vault.firewall` fragt
    `solvio.memory.intent`, und ein Import auf Modulebene waere ein Zyklus.
    """
    from solvio.secret_vault.firewall import refuse_if_credential
    refuse_if_credential(text, where=where)


def insert_record(conn: sqlite3.Connection, has_fts: bool, rec: MemoryRecord) -> None:
    _refuse_credentials(rec.content, "memory.insert_record")
    conn.execute(
        """INSERT INTO memories(
            id, memory_type, content, subject, source, source_type, created_at,
            updated_at, trust_level, sensitivity, retention_mode, retention_ttl_days,
            retention_purge_on_expiry, confidence, importance, valid_from, valid_until,
            supersedes, superseded_by, forgotten, forgotten_at, forgotten_reason,
            tags, metadata
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL,?,?)""",
        (
            rec.id, rec.memory_type.value, rec.content, rec.subject, rec.source,
            rec.source_type.value, dt_to_str(rec.created_at), dt_to_str(rec.updated_at),
            rec.trust_level.value, rec.sensitivity.value, rec.retention_policy.mode,
            rec.retention_policy.ttl_days, int(rec.retention_policy.purge_on_expiry),
            rec.confidence, rec.importance, dt_to_str(rec.valid_from),
            dt_to_str(rec.valid_until), rec.supersedes, rec.superseded_by,
            json.dumps(rec.tags), json.dumps(rec.metadata),
        ),
    )
    for i, p in enumerate(rec.provenance):
        conn.execute(
            "INSERT INTO provenance(memory_id, seq, source_type, source, trust_level, at, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (rec.id, i, p.source_type.value, p.source, p.trust_level.value,
             dt_to_str(p.at), p.note),
        )
    for r in rec.relations:
        conn.execute(
            "INSERT OR IGNORE INTO relations(memory_id, kind, target_id) VALUES (?,?,?)",
            (rec.id, r.kind, r.target_id),
        )
    _fts_upsert(conn, has_fts, rec.id, rec.content, rec.subject, rec.tags)


def get_record(conn: sqlite3.Connection, rec_id: str) -> MemoryRecord | None:
    row = conn.execute("SELECT * FROM memories WHERE id=?", (rec_id,)).fetchone()
    return _record_from_row(conn, row) if row else None


def update_fields(conn: sqlite3.Connection, has_fts: bool, rec_id: str,
                  changes: dict[str, Any], now: datetime) -> bool:
    row = conn.execute("SELECT * FROM memories WHERE id=?", (rec_id,)).fetchone()
    if row is None:
        return False
    if "content" in changes:
        _refuse_credentials(str(changes["content"]), "memory.update_fields")
    sets, params = [], []
    for k, v in changes.items():
        if k not in _UPDATABLE:
            raise ValueError(f"field not updatable via update(): {k!r}")
        if k in ("valid_from", "valid_until"):
            sets.append(f"{k}=?"); params.append(dt_to_str(v))
        elif k == "tags":
            sets.append("tags=?"); params.append(json.dumps(list(v)))
        elif k == "metadata":
            sets.append("metadata=?"); params.append(json.dumps(dict(v)))
        elif k in ("sensitivity", "trust_level"):
            sets.append(f"{k}=?"); params.append(v.value if hasattr(v, "value") else v)
        else:
            sets.append(f"{k}=?"); params.append(v)
    sets.append("updated_at=?"); params.append(dt_to_str(now))
    params.append(rec_id)
    conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", params)
    nrow = conn.execute("SELECT content, subject, tags, forgotten FROM memories WHERE id=?",
                        (rec_id,)).fetchone()
    if not nrow["forgotten"]:
        _fts_upsert(conn, has_fts, rec_id, nrow["content"], nrow["subject"],
                    json.loads(nrow["tags"]))
    return True


def set_superseded(conn: sqlite3.Connection, old_id: str, new_id: str, now: datetime) -> None:
    conn.execute("UPDATE memories SET superseded_by=?, updated_at=? WHERE id=?",
                 (new_id, dt_to_str(now), old_id))


def set_forgotten(conn: sqlite3.Connection, has_fts: bool, rec_id: str,
                  reason: str, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE memories SET forgotten=1, forgotten_at=?, forgotten_reason=? "
        "WHERE id=? AND forgotten=0",
        (dt_to_str(now), reason, rec_id),
    )
    if cur.rowcount == 0:
        return conn.execute("SELECT 1 FROM memories WHERE id=?", (rec_id,)).fetchone() is not None
    fts_delete(conn, has_fts, rec_id)  # aus aktivem Recall/Search entfernen
    return True


def hard_delete(conn: sqlite3.Connection, has_fts: bool, rec_id: str) -> bool:
    exists = conn.execute("SELECT 1 FROM memories WHERE id=?", (rec_id,)).fetchone() is not None
    conn.execute("DELETE FROM memories WHERE id=?", (rec_id,))  # cascade prov/rel
    fts_delete(conn, has_fts, rec_id)
    return exists


# ------------------------------------------------------------------------ queries
def _candidate_ids(conn: sqlite3.Connection, has_fts: bool, query: str) -> list[str]:
    if has_fts:
        m = _fts_match(query)
        if m is None:
            return []
        rows = conn.execute(
            "SELECT id FROM memories_fts WHERE memories_fts MATCH ? "
            "ORDER BY bm25(memories_fts) LIMIT 500", (m,)
        ).fetchall()
        return [r["id"] for r in rows]
    # Fallback ohne FTS: LIKE ueber Tokens.
    toks = _TOKEN_RE.findall(query or "")
    if not toks:
        return []
    where = " OR ".join(["content LIKE ? OR subject LIKE ?"] * len(toks))
    params: list[str] = []
    for t in toks:
        params += [f"%{t}%", f"%{t}%"]
    rows = conn.execute(f"SELECT id FROM memories WHERE {where} LIMIT 500", params).fetchall()
    return [r["id"] for r in rows]


def _valid_now(row: sqlite3.Row, now_s: str) -> bool:
    vf, vu = row["valid_from"], row["valid_until"]
    if vf is not None and vf > now_s:
        return False
    if vu is not None and now_s >= vu:
        return False
    return True


def recall(conn: sqlite3.Connection, has_fts: bool, query: str, *,
           memory_types: list[MemoryType] | None, subject: str | None,
           limit: int, now: datetime) -> list[MemoryRecord]:
    """Antwort-Pfad: NUR aktuelle Wahrheit (nicht supersediert, nicht vergessen,
    zeitlich gueltig)."""
    now_s = dt_to_str(now)
    types = {t.value for t in memory_types} if memory_types else None
    out: list[MemoryRecord] = []
    for cid in _candidate_ids(conn, has_fts, query):
        row = conn.execute("SELECT * FROM memories WHERE id=?", (cid,)).fetchone()
        if row is None or row["forgotten"] or row["superseded_by"] is not None:
            continue
        if subject is not None and row["subject"] != subject:
            continue
        if types is not None and row["memory_type"] not in types:
            continue
        if not _valid_now(row, now_s):
            continue
        out.append(_record_from_row(conn, row))
        if len(out) >= limit:
            break
    return out


def search(conn: sqlite3.Connection, has_fts: bool, query: str, *,
           include_superseded: bool, limit: int) -> list[MemoryRecord]:
    """Breite Suche. Vergessene werden ausgeschlossen; gepurgte existieren nicht mehr.
    Supersedierte nur bei include_superseded."""
    out: list[MemoryRecord] = []
    for cid in _candidate_ids(conn, has_fts, query):
        row = conn.execute("SELECT * FROM memories WHERE id=?", (cid,)).fetchone()
        if row is None or row["forgotten"]:
            continue
        if not include_superseded and row["superseded_by"] is not None:
            continue
        out.append(_record_from_row(conn, row))
        if len(out) >= limit:
            break
    return out


def history(conn: sqlite3.Connection, subject: str) -> list[MemoryRecord]:
    """Vollstaendige Zeitreihe eines Subjekts inkl. supersediert/vergessen.
    Gepurgte fehlen (nur als Tombstone im Privacy-Ledger)."""
    rows = conn.execute(
        "SELECT * FROM memories WHERE subject=? ORDER BY created_at, id", (subject,)
    ).fetchall()
    return [_record_from_row(conn, r) for r in rows]


def related(conn: sqlite3.Connection, rec_id: str) -> list[MemoryRecord]:
    rows = conn.execute(
        "SELECT m.* FROM relations r JOIN memories m ON m.id = r.target_id "
        "WHERE r.memory_id=? ORDER BY m.created_at", (rec_id,)
    ).fetchall()
    return [_record_from_row(conn, r) for r in rows]


def provenance(conn: sqlite3.Connection, rec_id: str) -> list[ProvenanceEntry]:
    return [
        ProvenanceEntry(
            source_type=SourceType(p["source_type"]), source=p["source"],
            trust_level=TrustLevel(p["trust_level"]), at=str_to_dt(p["at"]),  # type: ignore[arg-type]
            note=p["note"],
        )
        for p in conn.execute(
            "SELECT source_type, source, trust_level, at, note FROM provenance "
            "WHERE memory_id=? ORDER BY seq", (rec_id,)
        )
    ]


def expired_active(conn: sqlite3.Connection, now: datetime) -> list[tuple[str, bool]]:
    """Aktive TTL-Records, deren created_at + ttl_days <= now. -> (id, purge_on_expiry)."""
    out: list[tuple[str, bool]] = []
    rows = conn.execute(
        "SELECT id, created_at, retention_ttl_days, retention_purge_on_expiry "
        "FROM memories WHERE retention_mode='ttl' AND forgotten=0 AND superseded_by IS NULL"
    ).fetchall()
    for r in rows:
        if r["retention_ttl_days"] is None:
            continue
        created = str_to_dt(r["created_at"])
        expiry = created.timestamp() + r["retention_ttl_days"] * 86400
        if now.timestamp() >= expiry:
            out.append((r["id"], bool(r["retention_purge_on_expiry"])))
    return out


def count_records(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])


def id_exists(conn: sqlite3.Connection, rec_id: str) -> bool:
    return conn.execute("SELECT 1 FROM memories WHERE id=?", (rec_id,)).fetchone() is not None


def get_visible_record(conn: sqlite3.Connection, rec_id: str, now: datetime) -> MemoryRecord | None:
    """Record NUR, wenn aktuelle Wahrheit: nicht vergessen, nicht supersediert,
    zeitlich gueltig. (Purge-Deny prueft der Store zusaetzlich vorab.)"""
    row = conn.execute("SELECT * FROM memories WHERE id=?", (rec_id,)).fetchone()
    if row is None or row["forgotten"] or row["superseded_by"] is not None:
        return None
    if not _valid_now(row, dt_to_str(now)):
        return None
    return _record_from_row(conn, row)


def iter_active_records(conn: sqlite3.Connection,
                       now: datetime | None = None) -> list[MemoryRecord]:
    """Alle indexierbaren Records — AKTUELLE WAHRHEIT im Sinne von Contract §6.

    DEBT-0093: frueher stand hier „zeitliche Gueltigkeit ist ein
    Query-Zeit-Filter, daher hier nicht gefiltert". Das war falsch, und zwar
    sichtbar: `recall()` verwarf einen Record mit abgelaufenem `valid_until`,
    waehrend derselbe Record im Semantik-Index und im Obsidian-Buendel weiter
    als aktuell erschien. Der Contract kennt genau EINE Definition aktueller
    Wahrheit; zwei Sichten darauf sind eine zu viel.

    Mit Adaptive Memory ist das keine Randnotiz mehr: befristete Eintraege
    entstehen dort regelmaessig.
    """
    now_s = dt_to_str(now or utcnow())
    rows = conn.execute(
        "SELECT * FROM memories WHERE forgotten=0 AND superseded_by IS NULL "
        "ORDER BY created_at, id").fetchall()
    return [_record_from_row(conn, r) for r in rows if _valid_now(r, now_s)]


def active_ids(conn: sqlite3.Connection, now: datetime | None = None) -> set[str]:
    """Dieselbe Menge wie `iter_active_records`, nur die Kennungen.

    Muss dieselbe Definition benutzen — sonst raeumte die Index-Pflege genau
    die Records weg, die die aktive Sicht noch liefert (oder umgekehrt).
    """
    now_s = dt_to_str(now or utcnow())
    return {r["id"] for r in conn.execute(
        "SELECT id, valid_from, valid_until FROM memories "
        "WHERE forgotten=0 AND superseded_by IS NULL") if _valid_now(r, now_s)}


def append_provenance(conn: sqlite3.Connection, rec_id: str,
                      entry: ProvenanceEntry, now: datetime) -> bool:
    """Einen Provenienzeintrag ANHAENGEN. Append-only, nie ueberschreibend.

    Der schmale Weg fuer `reinforce()`: `update()` laesst die Provenienz per
    Feld-Whitelist bewusst nicht zu, und `supersede()` fuer blosse Verstaerkung
    wuerde die Historie fluten. Eine dritte, enge Operation ist ehrlicher, als
    eine der beiden zu verbiegen.
    """
    _refuse_credentials(getattr(entry, "note", "") or "", "memory.append_provenance")
    row = conn.execute("SELECT source_type FROM memories WHERE id=?",
                       (rec_id,)).fetchone()
    if row is None:
        return False
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) + 1 FROM provenance WHERE memory_id=?",
        (rec_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO provenance(memory_id, seq, source_type, source, trust_level,"
        " at, note) VALUES (?,?,?,?,?,?,?)",
        (rec_id, seq, entry.source_type.value, entry.source,
         entry.trust_level.value, dt_to_str(entry.at), entry.note))
    conn.execute("UPDATE memories SET updated_at=? WHERE id=?",
                 (dt_to_str(now), rec_id))
    return True


def append_provenance_once(conn: sqlite3.Connection, rec_id: str,
                           entry: ProvenanceEntry, now: datetime) -> str:
    """Aufrufer haelt BEGIN IMMEDIATE; dieselbe Beobachtung zaehlt einmal."""
    existing = conn.execute(
        "SELECT 1 FROM provenance WHERE memory_id=? AND source=? AND source_type=? AND trust_level=?",
        (rec_id, entry.source, entry.source_type.value, entry.trust_level.value)).fetchone()
    if existing is not None:
        return "present"
    return "added" if append_provenance(conn, rec_id, entry, now) else "unavailable"


def removal_reason(conn: sqlite3.Connection, rec_id: str) -> str:
    """Warum ein Record nicht mehr aktive Wahrheit ist — soweit der Store es weiss.

    `forgotten` / `superseded` / `expired` / `""` (unbekannt). Nur der GRUND,
    nie der Inhalt: diese Antwort geht in eine menschenlesbare Ansicht, und die
    soll von einem vergessenen Eintrag genau nichts erfahren.
    """
    row = conn.execute(
        "SELECT forgotten, superseded_by, valid_until FROM memories WHERE id=?",
        (rec_id,)).fetchone()
    if row is None:
        return ""
    if row["forgotten"]:
        return "forgotten"
    if row["superseded_by"] is not None:
        return "superseded"
    if row["valid_until"] is not None and dt_to_str(utcnow()) >= row["valid_until"]:
        return "expired"
    return ""


def prune_orphan_fts(conn: sqlite3.Connection) -> int:
    """Entfernt FTS-Eintraege ohne zugehoerige memories-Zeile (Konsistenz/fail-closed)."""
    cur = conn.execute(
        "DELETE FROM memories_fts WHERE id NOT IN (SELECT id FROM memories)"
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def reconcile_purged(conn: sqlite3.Connection, has_fts: bool, is_purged) -> int:
    """Entfernt physisch alle aktiven Records, die laut Ledger gepurgt sind.

    `is_purged(id) -> bool` entscheidet pro Record. Cascade loescht provenance/
    relations; FTS wird separat bereinigt. Idempotent: ein zweiter Lauf findet
    nichts mehr. Gibt die Zahl entfernter Records zurueck.
    """
    victims = [
        r["id"] for r in conn.execute("SELECT id FROM memories").fetchall()
        if is_purged(r["id"])
    ]
    for rid in victims:
        conn.execute("DELETE FROM memories WHERE id=?", (rid,))  # cascade prov/rel
        fts_delete(conn, has_fts, rid)
    if has_fts:
        prune_orphan_fts(conn)  # verwaiste FTS-Eintraege fail-closed mitraeumen
    return len(victims)
