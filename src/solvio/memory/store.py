"""SolvioMemory — die async MemoryStore-Implementierung (STEP 20, gehaertet 20.1).

Erfuellt das eingefrorene MemoryStore-Protokoll (contracts.memory) mit stdlib
sqlite3. Zwei physisch getrennte Datenbanken:
    <base>/memory.sqlite3          kanonischer aktiver Store (+ FTS5)
    <base>/privacy_ledger.sqlite3  Purge-/Tombstone-Ledger (ueberlebt Restore)

Nebenlaeufigkeit: Alle DB-Zugriffe laufen in EINEM dedizierten Worker-Thread
(ThreadPoolExecutor max_workers=1). Der Event-Loop wird nie blockiert, der
Zugriff ist serialisiert, es gibt keine unsicher geteilte Connection.

Purge-Sicherheit (20.1): Weil memory.sqlite3 und der Ledger KEINE gemeinsame
Transaktion teilen, gilt strikt:
  1) Tombstone DURABLE im Ledger committen  (synchronous=FULL)
  2) danach aktive Daten (memory/FTS/prov/rel) loeschen
Bricht der Prozess zwischen 1) und 2) ab, wirkt der Tombstone trotzdem sofort
als fail-closed Deny (Read-Time-Pruefung) und wird beim naechsten Start physisch
reconciled. Ein Managed-Restore verlangt einen vorhandenen, integren Ledger.

STEP-20-Grenzen: kein LLM/Netz/Embeddings. consolidate() ist ein deterministischer
No-op (echte Konsolidierung folgt in STEP 22).
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from solvio.contracts.memory import (
    MemoryRecord,
    MemoryType,
    ProvenanceEntry,
    Tombstone,
)
from solvio.contracts.trust import SourceType, TrustLevel
from solvio.memory import backup as backup_mod
from solvio.memory import sqlite_backend as be
from solvio.memory.privacy_ledger import (
    PrivacyLedger,
    hash_id,
    validate_ledger_file,
)

_T = TypeVar("_T")

MEMORY_DB = "memory.sqlite3"
LEDGER_DB = "privacy_ledger.sqlite3"
BACKUP_DIR = "backups"

_FILE_MODE = 0o600
_DIR_MODE = 0o700


class MemoryTargetChanged(KeyError):
    """The target is no longer current; raised before any superseding write."""


class MemoryWriteNotApplied(RuntimeError):
    """A canonical write transaction failed and its rollback was confirmed."""


class SolvioMemory:
    """Native, lokale, provenance-/purge-bewusste Memory-Implementierung."""

    def __init__(self, base_dir: str) -> None:
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)
        os.makedirs(os.path.join(self.base_dir, BACKUP_DIR), exist_ok=True)
        self.memory_path = os.path.join(self.base_dir, MEMORY_DB)
        self.ledger_path = os.path.join(self.base_dir, LEDGER_DB)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="solvio-mem")
        self._conn, self._has_fts = be.open_store(self.memory_path)
        self.ledger = PrivacyLedger(self.ledger_path)  # fail-closed bei Korruption
        # Read-Time-Deny-Cache: In-Memory-Menge der gepurgten id_hashes (ein Prozess).
        self._purged_hashes: set[str] = self.ledger.purged_id_hashes()
        # Deterministischer Test-Failpoint (nur Tests setzen ihn): wird nach dem
        # durablen Tombstone-Commit, aber VOR dem aktiven Delete aufgerufen.
        self._crash_after_tombstone: Callable[[], None] | None = None
        self._closed = False
        # Startup-Reconciliation: physisch aufraeumen, was ein frueherer Crash
        # nach dem Tombstone offen liess.
        self._reconcile_sync()
        self._harden_perms()

    # ------------------------------------------------------------------ plumbing
    async def _run(self, fn: Callable[..., _T], *args: Any) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, lambda: fn(*args))

    def _commit(self) -> None:
        self._conn.commit()

    def _rollback(self) -> None:
        try:
            self._conn.rollback()
        except Exception:  # noqa: BLE001 - Rollback darf nie neu werfen
            pass

    # ------------------------------------------------- Read-Time fail-closed deny
    def _is_purged(self, rec_id: str) -> bool:
        """True, wenn rec_id einem Tombstone entspricht. Fast-Path: leere Menge."""
        if not self._purged_hashes:
            return False
        return hash_id(rec_id) in self._purged_hashes

    def _deny_filter(self, recs: list[MemoryRecord]) -> list[MemoryRecord]:
        if not self._purged_hashes:
            return recs
        ph = self._purged_hashes
        return [r for r in recs if hash_id(r.id) not in ph]

    # -------------------------------------------------------------- 1. remember
    def _remember_sync(self, record: MemoryRecord) -> str:
        if not record.id:
            record.id = uuid.uuid4().hex
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            be.insert_record(self._conn, self._has_fts, record)
            self._commit()
        except Exception as exc:
            if self._conn.in_transaction:
                self._conn.rollback()  # Failure here is NOT a non-effect receipt.
                raise MemoryWriteNotApplied("canonical_write_rolled_back") from exc
            raise
        return record.id

    async def remember(self, record: MemoryRecord) -> str:
        return await self._run(self._remember_sync, record)

    async def adoption_receipt(self, memory_id: str, candidate_id: str, digest: str) -> bool:
        """Exact canonical commit evidence, never search/reinforce or retry.

        A forgotten/superseded record can still prove a historical commit.
        Purged content is never loaded and an absent row proves no success.
        """
        def read():
            if not memory_id or not digest or self.ledger.contains_id(memory_id):
                return False
            record = be.get_record(self._conn, memory_id)
            return bool(record is not None and record.source_type == SourceType.SOLVIO_INFERENCE
                        and record.metadata.get("candidate_id") == candidate_id
                        and record.metadata.get("adoption_digest") == digest)
        return await self._run(read)

    # ------------------------------------------------------------------- 2. get
    async def get(self, id: str) -> MemoryRecord | None:
        if self._is_purged(id):  # fail-closed, auch wenn Zeile physisch noch da ist
            return None
        return await self._run(be.get_record, self._conn, id)

    # ---------------------------------------------------------------- 3. recall
    def _recall_sync(self, query, memory_types, subject, limit) -> list[MemoryRecord]:
        return be.recall(self._conn, self._has_fts, query,
                         memory_types=memory_types, subject=subject,
                         limit=limit, now=be.utcnow())

    async def recall(self, query: str, *, memory_types: list[MemoryType] | None = None,
                     subject: str | None = None, limit: int = 10) -> list[MemoryRecord]:
        res = await self._run(self._recall_sync, query, memory_types, subject, limit)
        return self._deny_filter(res)

    # ---------------------------------------------------------------- 4. search
    def _search_sync(self, query, include_superseded, limit) -> list[MemoryRecord]:
        return be.search(self._conn, self._has_fts, query,
                        include_superseded=include_superseded, limit=limit)

    async def search(self, query: str, *, include_superseded: bool = False,
                     limit: int = 20) -> list[MemoryRecord]:
        res = await self._run(self._search_sync, query, include_superseded, limit)
        return self._deny_filter(res)

    # ---------------------------------------------------------------- 5. update
    def _update_sync(self, id, changes) -> MemoryRecord:
        try:
            ok = be.update_fields(self._conn, self._has_fts, id, changes, be.utcnow())
            if not ok:
                raise KeyError(f"unknown memory id: {id}")
            self._commit()
        except Exception:
            self._rollback()
            raise
        return be.get_record(self._conn, id)  # type: ignore[return-value]

    async def update(self, id: str, changes: dict[str, Any]) -> MemoryRecord:
        if self._is_purged(id):
            raise KeyError(f"unknown memory id: {id}")  # gepurgt -> existiert nicht mehr
        return await self._run(self._update_sync, id, changes)

    # ------------------------------------------------------------- 6. supersede
    def _supersede_sync(self, old_id, new_record) -> MemoryRecord:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Two tabs/processes may correct the same old view. Exactly one may
            # replace a current target. Never branch active truth or revive a
            # forgotten/purged/expired record from a stale browser card.
            if self.ledger.contains_id(old_id) or be.get_visible_record(self._conn, old_id, be.utcnow()) is None:
                raise MemoryTargetChanged("memory_target_changed")
            if not new_record.id:
                new_record.id = uuid.uuid4().hex
            new_record.supersedes = old_id
            be.insert_record(self._conn, self._has_fts, new_record)
            be.set_superseded(self._conn, old_id, new_record.id, be.utcnow())
            self._commit()
        except BaseException as exc:
            if self._conn.in_transaction:
                self._conn.rollback()
                if isinstance(exc, Exception) and not isinstance(exc, MemoryTargetChanged):
                    raise MemoryWriteNotApplied("canonical_write_rolled_back") from exc
            raise
        return be.get_record(self._conn, new_record.id)  # type: ignore[return-value]

    async def supersede(self, old_id: str, new_record: MemoryRecord) -> MemoryRecord:
        if self._is_purged(old_id):
            raise MemoryTargetChanged("memory_target_changed")
        return await self._run(self._supersede_sync, old_id, new_record)

    # ---------------------------------------------------------------- 7. forget
    def _forget_sync(self, id, reason) -> bool:
        try:
            ok = be.set_forgotten(self._conn, self._has_fts, id, reason, be.utcnow())
            self._commit()
        except Exception:
            self._rollback()
            raise
        return ok

    async def forget(self, id: str, *, reason: str) -> bool:
        if self._is_purged(id):
            return False  # bereits gepurgt: nichts mehr zu vergessen
        return await self._run(self._forget_sync, id, reason)

    # ----------------------------------------------------------------- 8. purge
    def _purge_sync(self, id, reason) -> bool:
        rec = be.get_record(self._conn, id)
        if rec is None:
            # Nicht (mehr) aktiv vorhanden -> idempotent True, falls schon getombstoned.
            return self.ledger.contains_id(id)
        # 1) TOMBSTONE FIRST — durabler Commit im Ledger (synchronous=FULL).
        self.ledger.add(id, rec.subject, reason)
        self._purged_hashes.add(hash_id(id))  # Read-Time-Deny ab sofort wirksam
        # Deterministischer Crash-Failpoint: Tombstone committed, Delete noch NICHT.
        if self._crash_after_tombstone is not None:
            self._crash_after_tombstone()
        # 2) ACTIVE DELETE (memory + FTS + prov/rel Cascade).
        try:
            be.hard_delete(self._conn, self._has_fts, id)
            self._commit()
        except Exception:
            self._rollback()  # Tombstone bleibt -> Reads denyen, Restart reconciled
            raise
        return True

    async def purge(self, id: str, *, reason: str) -> bool:
        return await self._run(self._purge_sync, id, reason)

    # --------------------------------------------------------------- 9. history
    def _history_sync(self, subject) -> list[MemoryRecord]:
        return be.history(self._conn, subject)

    async def history(self, subject: str) -> list[MemoryRecord]:
        res = await self._run(self._history_sync, subject)
        return self._deny_filter(res)

    # ---------------------------------------------------------- 10. consolidate
    def _consolidate_sync(self, scope) -> dict:
        # STEP 20: KEIN AI. Deterministischer, sicherer No-op. Respektiert Tombstones
        # trivial (keine Aenderung). Echte Konsolidierung/Dreaming folgt in STEP 22.
        return {
            "status": "noop",
            "scope": scope,
            "merged": 0,
            "superseded": 0,
            "tombstones_respected": True,
            "note": "consolidation deferred to STEP 22 (no AI in STEP 20)",
        }

    async def consolidate(self, scope: str | None = None) -> dict:
        return await self._run(self._consolidate_sync, scope)

    # -------------------------------------------------------- 11. list_related
    async def list_related(self, id: str) -> list[MemoryRecord]:
        res = await self._run(be.related, self._conn, id)
        return self._deny_filter(res)

    # ------------------------------------------------------- 12. get_provenance
    async def get_provenance(self, id: str) -> list[ProvenanceEntry]:
        if self._is_purged(id):  # keine personenbezogene Herkunft eines gepurgten Records
            return []
        return await self._run(be.provenance, self._conn, id)

    # ------------------------------------------------------ 13. list_tombstones
    async def list_tombstones(self) -> list[Tombstone]:
        return await self._run(self.ledger.list_tombstones)

    # ------------------------------- active-truth helpers (fuer Semantic Layer)
    async def get_visible(self, id: str, now=None) -> MemoryRecord | None:
        """Record nur, wenn aktuelle Wahrheit (nicht purged/forgotten/superseded,
        zeitlich gueltig). Basis fuer den fail-closed Active-Truth-Filter (STEP 21)."""
        if self._is_purged(id):
            return None
        return await self._run(be.get_visible_record, self._conn, id, now or be.utcnow())

    async def active_records(self, now=None) -> list[MemoryRecord]:
        """Aktuelle Wahrheit: nicht forgotten/superseded/purged UND zeitlich gueltig.

        Das Zeitfenster kam mit DEBT-0093 dazu. Ohne es lieferte diese Sicht
        Records, die `recall()` bereits verwarf — und Semantik-Index wie
        Obsidian-Buendel zeigten Abgelaufenes als aktuell.
        """
        recs = await self._run(be.iter_active_records, self._conn, now)
        return self._deny_filter(recs)

    async def active_ids(self, now=None) -> set[str]:
        ids = await self._run(be.active_ids, self._conn, now)
        if not self._purged_hashes:
            return ids
        return {i for i in ids if not self._is_purged(i)}

    # ------------------------------------------------------------ reinforce
    def _reinforce_sync(self, rec_id: str, entry: ProvenanceEntry) -> bool:
        record = be.get_record(self._conn, rec_id)
        if record is None:
            return False
        # Verstaerken darf man nur, was die Maschine abgeleitet hat. Einen
        # `user_direct`-Record um eine Beobachtung zu ergaenzen hiesse, seine
        # Herkunft zu verwaessern — und genau daran haengt die Autoritaetsachse.
        if record.source_type is not SourceType.SOLVIO_INFERENCE:
            raise ValueError(
                f"reinforce refused: {rec_id} is {record.source_type.value}, "
                f"not solvio_inference")
        try:
            ok = be.append_provenance(self._conn, rec_id, entry, be.utcnow())
            self._commit()
        except Exception:
            self._rollback()
            raise
        return ok

    async def reinforce(self, id: str, entry: ProvenanceEntry) -> bool:
        """Eine weitere Beobachtung anhaengen — append-only, ohne neuen Record.

        Wiederholung erzeugt keinen zweiten Eintrag, sondern verlaengert die
        Kette des bestehenden. Nur gegen `solvio_inference`; sonst wirft es.
        """
        if self._is_purged(id):
            return False
        return await self._run(self._reinforce_sync, id, entry)

    def _reinforce_observation_sync(self, rec_id: str, entry: ProvenanceEntry) -> str:
        # Pruefung und Anhaengen teilen den Schreibanspruch auch zwischen
        # frischen Store-Instanzen. Kein zweiter Index und keine Schemaaenderung.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Ein anderer offener Store kann den Tombstone bereits dauerhaft
            # geschrieben haben, ohne den aktiven Record loeschen zu koennen.
            # Der Cache vor dem Workerauftrag reicht dafuer nicht aus.
            if self.ledger.contains_id(rec_id):
                self._purged_hashes.add(hash_id(rec_id))
                self._commit()
                return "unavailable"
            record = be.get_visible_record(self._conn, rec_id, be.utcnow())
            if record is None:
                result = "unavailable"
            elif record.source_type is not SourceType.SOLVIO_INFERENCE:
                raise ValueError("reinforce_observation_requires_machine_memory")
            else:
                result = be.append_provenance_once(self._conn, rec_id, entry, be.utcnow())
            self._commit()
            return result
        except BaseException:
            self._rollback()
            raise

    async def reinforce_observation(self, id: str, entry: ProvenanceEntry) -> str:
        """Eine belegte Nachricht genau einmal: added/present/unavailable.

        source bindet die vom Core gebildete Nachrichtenidentitaet. Auch die
        Erst-Adoption legt genau diesen Provenienzeintrag an; ein Wiederanlauf
        nach deren Commit kann deshalb keine zweite Beobachtung behaupten.
        """
        if (entry.source_type is not SourceType.SOLVIO_INFERENCE or
                entry.trust_level is not TrustLevel.AGENT_GENERATED or
                not re.fullmatch(r"conversation:.+#observation:[0-9a-f]{64}", entry.source)):
            raise ValueError("invalid_observation_provenance")
        if self._is_purged(id):
            return "unavailable"
        return await self._run(self._reinforce_observation_sync, id, entry)

    async def removal_reasons(self, ids) -> dict[str, str]:
        """Warum diese Kennungen nicht mehr aktive Wahrheit sind.

        Antwortet mit Gruenden, nie mit Inhalt: `forgotten` / `purged` /
        `superseded` / `expired` / `""`. Der Wissens-Compiler braucht genau
        das, um ein VERGESSEN anders zu behandeln als eine Abloesung — er sieht
        sonst nur, dass eine Kennung fehlt.
        """
        def _read() -> dict[str, str]:
            out: dict[str, str] = {}
            for ident in ids:
                if self._is_purged(ident):
                    out[ident] = "purged"
                    continue
                reason = be.removal_reason(self._conn, ident)
                if reason:
                    out[ident] = reason
            return out
        return await self._run(_read)

    # ============================================================= reconciliation
    def _reconcile_sync(self, force_prune: bool = False) -> int:
        """Getombstonete Records physisch entfernen (+ verwaiste FTS-Eintraege).

        Startup nutzt den Fast-Path (kein Scan, wenn nichts gepurgt ist); ein
        expliziter reconcile()-Aufruf raeumt zusaetzlich verwaiste FTS-Eintraege.
        """
        if self._purged_hashes:
            removed = be.reconcile_purged(self._conn, self._has_fts, self._is_purged)
            self._commit()
            return removed
        if force_prune and self._has_fts:
            be.prune_orphan_fts(self._conn)
            self._commit()
        return 0

    async def reconcile(self) -> int:
        """Ledger gegen aktiven Store abgleichen; getombstonete Records physisch entfernen."""
        return await self._run(self._reconcile_sync, True)

    async def ledger_integrity(self) -> str:
        return await self._run(self.ledger.integrity_check)

    # ============================================================= permissions
    def _secure_file(self, path: str) -> None:
        try:
            if os.path.exists(path):
                os.chmod(path, _FILE_MODE)
        except (OSError, NotImplementedError):
            pass  # z. B. Windows-Dateisysteme ohne POSIX-Modi

    def _secure_dir(self, path: str) -> None:
        try:
            if os.path.isdir(path):
                os.chmod(path, _DIR_MODE)
        except (OSError, NotImplementedError):
            pass

    def _harden_perms(self) -> None:
        self._secure_dir(self.base_dir)
        self._secure_dir(os.path.join(self.base_dir, BACKUP_DIR))
        for p in (self.memory_path, self.ledger_path):
            self._secure_file(p)
            self._secure_file(p + "-wal")
            self._secure_file(p + "-shm")

    # ============================================================= management
    def _safe_path(self, path: str, *, must_exist: bool) -> str:
        """Path-Traversal-/Symlink-Schutz: erlaubt nur Pfade unterhalb von base_dir."""
        ap = os.path.abspath(path)
        base = self.base_dir + os.sep
        if not (ap == self.base_dir or ap.startswith(base)):
            raise ValueError(f"path escapes memory base_dir: {path}")
        check = ap if must_exist else os.path.dirname(ap)
        probe = check
        while probe and probe.startswith(self.base_dir):
            if os.path.islink(probe):
                raise ValueError(f"symlink in path is not allowed: {probe}")
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        if must_exist and not os.path.exists(ap):
            raise FileNotFoundError(path)
        return ap

    def _backup_sync(self, dest_path) -> dict:
        if dest_path is None:
            name = "memory-" + uuid.uuid4().hex[:12] + ".sqlite3.bak"
            dest_path = os.path.join(self.base_dir, BACKUP_DIR, name)
        dest = self._safe_path(dest_path, must_exist=False)
        self._conn.commit()
        manifest = backup_mod.create_backup(self._conn, dest)
        self._secure_file(dest)                       # 20.1: restriktive Rechte
        self._secure_file(dest + backup_mod._MANIFEST_SUFFIX)
        return {"path": dest, "manifest": manifest.__dict__}

    async def backup(self, dest_path: str | None = None) -> dict:
        return await self._run(self._backup_sync, dest_path)

    async def verify_backup(self, backup_path: str) -> dict:
        safe = self._safe_path(backup_path, must_exist=True)
        return await self._run(backup_mod.verify_backup, safe)

    def _restore_sync(self, backup_path) -> dict:
        safe = self._safe_path(backup_path, must_exist=True)
        # FAIL-CLOSED: ohne vorhandenen, integren Privacy-Ledger KEIN Restore —
        # sonst koennte ein altes Backup gepurgte Daten reaktivieren. Die Pruefung
        # liest den Ledger ueber eine frische Verbindung (main+WAL) direkt von Platte.
        ok, err = validate_ledger_file(self.ledger_path)
        if not ok:
            raise ValueError(f"managed restore refused (fail-closed): {err}")
        purged = self.ledger.purged_id_hashes()          # AKTUELLER Ledger-Stand
        staging = self.memory_path + ".restore"
        stats = backup_mod.build_restored_db(safe, staging, purged)  # Ledger anwenden
        # aktive DB atomar ersetzen
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._conn.close()
        for sfx in ("-wal", "-shm"):
            p = self.memory_path + sfx
            if os.path.exists(p):
                os.remove(p)
        os.replace(staging, self.memory_path)
        self._conn, self._has_fts = be.open_store(self.memory_path)
        # Deny-Cache auffrischen, idempotent reconciliieren, Rechte haerten.
        self._purged_hashes = self.ledger.purged_id_hashes()
        stats["reconciled"] = self._reconcile_sync()
        self._harden_perms()
        stats["ledger_tombstones"] = self.ledger.count()
        return stats

    async def restore(self, backup_path: str) -> dict:
        """Managed Restore: Ledger validieren -> Backup -> Staging -> Ledger anwenden
        -> atomar aktiv. Gepurgte Daten kehren NICHT zurueck; fehlt/korrupt der
        Ledger, wird fail-closed abgebrochen."""
        return await self._run(self._restore_sync, backup_path)

    def _apply_retention_sync(self, now) -> dict:
        now = now or be.utcnow()
        forgotten, purged = [], []
        for rid, purge_on_expiry in be.expired_active(self._conn, now):
            if purge_on_expiry:
                rec = be.get_record(self._conn, rid)
                if rec is not None:
                    self.ledger.add(rid, rec.subject, "ttl_expiry_purge")  # Tombstone first
                    self._purged_hashes.add(hash_id(rid))
                    be.hard_delete(self._conn, self._has_fts, rid)
                    purged.append(rid)
            else:
                be.set_forgotten(self._conn, self._has_fts, rid, "ttl_expiry_forget", now)
                forgotten.append(rid)
        self._commit()
        return {"forgotten": forgotten, "purged": purged}

    async def apply_retention(self, now=None) -> dict:
        return await self._run(self._apply_retention_sync, now)

    async def integrity_check(self) -> str:
        return await self._run(
            lambda: self._conn.execute("PRAGMA integrity_check").fetchone()[0]
        )

    async def count(self) -> int:
        return await self._run(be.count_records, self._conn)

    def _close_sync(self) -> None:
        if self._closed:
            return
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001
            pass
        self._conn.close()
        self.ledger.close()
        self._closed = True

    async def close(self) -> None:
        await self._run(self._close_sync)
        self._pool.shutdown(wait=True)
