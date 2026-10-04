"""STEP 20.1 — Privacy/Crash-Hardening-Tests der SOLVIO Memory Foundation.

Nur Standardbibliothek (unittest + asyncio). Deckt ab:
- Purge-Crash-Window (Tombstone committed, Delete nicht) -> Read-Time-Deny
- Restart-Reconciliation (idempotent, physisch clean)
- Missing/Corrupt Privacy-Ledger -> Managed Restore FAIL-CLOSED
- Ledger-Integritaet + Durability-Pragmas (WAL + synchronous=FULL)
- Restriktive Dateirechte (0600 / 0700)
- Re-Learning nach Purge (gleiches Subject, altes bleibt gepurgt)
- Idempotenz / partielle Fehlerzustaende (kein PII-Leak, fail-closed)

Ausfuehren:  python tests/memory/test_hardening.py
"""
from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import os
import shutil
import sqlite3
import stat
import tempfile
import unittest
from datetime import datetime, timezone

from solvio.contracts.memory import MemoryRecord, MemoryType, Relation
from solvio.contracts.trust import SourceType, TrustLevel
from solvio.memory import SolvioMemory

WIN = os.name == "nt"


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def mk(**over) -> MemoryRecord:
    now = _utc()
    base = dict(
        id="", memory_type=MemoryType.USER,
        content="the user favorite color is blue",
        subject="user:color", source="voice", source_type=SourceType.USER_DIRECT,
        created_at=now, updated_at=now, trust_level=TrustLevel.USER_DIRECT,
    )
    base.update(over)
    return MemoryRecord(**base)


def _boom() -> None:
    raise RuntimeError("simulated crash between tombstone commit and active delete")


def raw_count(db_path: str, rec_id: str) -> int:
    c = sqlite3.connect(db_path)
    try:
        return int(c.execute("SELECT COUNT(*) FROM memories WHERE id=?", (rec_id,)).fetchone()[0])
    finally:
        c.close()


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="solvio-hard-")
        self.mem = SolvioMemory(self.dir)

    async def asyncTearDown(self) -> None:
        try:
            await self.mem.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)

    async def _exec_raw_on_store(self, sql, *params):
        """Fuehrt SQL auf der store-eigenen Connection aus (Pool-Thread)."""
        def run():
            self.mem._conn.execute(sql, params)
            self.mem._conn.commit()
        await self.mem._run(run)


# ============================================= B/C. Crash Window
class TestCrashWindow(Base):
    async def test_live_read_deny_after_tombstone_before_delete(self):
        rid = await self.mem.remember(mk(content="secret diary blue", subject="user:diary"))
        self.mem._crash_after_tombstone = _boom
        with self.assertRaises(RuntimeError):
            await self.mem.purge(rid, reason="user erase")
        # Physisch ist die Zeile noch da (Delete kam nicht dran) ...
        self.assertEqual(raw_count(self.mem.memory_path, rid), 1)
        # ... aber ALLE Read-Paths denyen sofort (fail-closed, ohne Restart):
        self.assertIsNone(await self.mem.get(rid))
        self.assertEqual(await self.mem.recall("secret diary"), [])
        self.assertEqual(await self.mem.search("secret diary"), [])
        self.assertEqual(await self.mem.history("user:diary"), [])
        self.assertEqual(await self.mem.get_provenance(rid), [])

    async def test_restart_reconciles_physical_state(self):
        rid = await self.mem.remember(mk(content="secret token green", subject="user:token"))
        self.mem._crash_after_tombstone = _boom
        with self.assertRaises(RuntimeError):
            await self.mem.purge(rid, reason="erase")
        self.assertEqual(raw_count(self.mem.memory_path, rid), 1)  # noch da
        await self.mem.close()
        self.mem = SolvioMemory(self.dir)  # Restart -> Startup-Reconciliation
        self.assertEqual(raw_count(self.mem.memory_path, rid), 0)  # physisch entfernt
        self.assertIsNone(await self.mem.get(rid))
        self.assertEqual(await self.mem.recall("secret token"), [])
        self.assertEqual(len(await self.mem.list_tombstones()), 1)

    async def test_delete_failure_after_ledger_commit_stays_denied(self):
        rid = await self.mem.remember(mk(content="alpha bravo charlie"))
        self.mem._crash_after_tombstone = _boom  # simuliert scheiternden Delete
        with self.assertRaises(RuntimeError):
            await self.mem.purge(rid, reason="erase")
        self.assertIsNone(await self.mem.get(rid))
        # Store bleibt benutzbar:
        rid2 = await self.mem.remember(mk(content="delta echo foxtrot", subject="user:x2"))
        self.assertIsNotNone(await self.mem.get(rid2))


# ============================================= Startup Reconciliation
class TestReconciliation(Base):
    async def test_reconcile_idempotent(self):
        rid = await self.mem.remember(mk(content="idem coffee", subject="user:i"))
        self.mem._crash_after_tombstone = _boom
        with self.assertRaises(RuntimeError):
            await self.mem.purge(rid, reason="erase")
        first = await self.mem.reconcile()
        second = await self.mem.reconcile()
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)  # idempotent
        self.assertEqual(raw_count(self.mem.memory_path, rid), 0)

    async def test_reconcile_noop_when_nothing_purged(self):
        await self.mem.remember(mk(content="nothing purged here"))
        self.assertEqual(await self.mem.reconcile(), 0)


# ============================================= Ledger Durability / Integrity
class TestLedgerDurability(Base):
    async def test_ledger_pragmas_wal_and_full(self):
        jm = self.mem.ledger._conn.execute("PRAGMA journal_mode").fetchone()[0]
        sync = self.mem.ledger._conn.execute("PRAGMA synchronous").fetchone()[0]
        self.assertEqual(str(jm).lower(), "wal")
        self.assertEqual(sync, 2)  # 2 == FULL

    async def test_memory_store_stays_normal(self):
        sync = self.mem._conn.execute("PRAGMA synchronous").fetchone()[0]
        self.assertEqual(sync, 1)  # 1 == NORMAL (Memory-Store bleibt optimiert)

    async def test_ledger_integrity_ok(self):
        self.assertEqual(await self.mem.ledger_integrity(), "ok")


# ============================================= Missing / Corrupt Ledger Restore
class TestLedgerRestoreGuards(Base):
    async def _seed_and_backup(self):
        rid = await self.mem.remember(mk(content="kept fact", subject="user:k"))
        secret = await self.mem.remember(mk(content="secret fact", subject="user:s"))
        await self.mem.purge(secret, reason="erase")
        bk = await self.mem.backup()
        return rid, secret, bk

    async def test_restore_normal_with_valid_ledger(self):
        rid, secret, bk = await self._seed_and_backup()
        stats = await self.mem.restore(bk["path"])
        self.assertIsNotNone(await self.mem.get(rid))
        self.assertIsNone(await self.mem.get(secret))
        self.assertGreaterEqual(stats["ledger_tombstones"], 1)

    async def test_restore_fails_closed_when_ledger_missing(self):
        rid, secret, bk = await self._seed_and_backup()
        # Ledger schliessen (Windows verbietet Loeschen offener Dateien), dann
        # physisch entfernen -> Restore muss fail-closed abbrechen.
        self.mem.ledger.close()
        for p in (self.mem.ledger_path, self.mem.ledger_path + "-wal",
                  self.mem.ledger_path + "-shm"):
            if os.path.exists(p):
                os.remove(p)
        with self.assertRaises(ValueError):
            await self.mem.restore(bk["path"])
        # aktive Memory-DB unveraendert (kein stiller Restore)
        self.assertIsNotNone(await self.mem.get(rid))

    async def test_restore_fails_closed_when_ledger_corrupt(self):
        rid, secret, bk = await self._seed_and_backup()
        self.mem.ledger.checkpoint()  # WAL in Hauptdatei ziehen
        with open(self.mem.ledger_path, "r+b") as f:
            f.seek(0)
            f.write(b"\x00" * 100)  # SQLite-Header zerstoeren
        with self.assertRaises(ValueError):
            await self.mem.restore(bk["path"])


# ============================================= File Permissions
@unittest.skipIf(WIN, "POSIX-Dateirechte werden unter Windows nicht erzwungen")
class TestFilePermissions(Base):
    async def test_db_and_dir_permissions(self):
        await self.mem.remember(mk())
        self.assertEqual(stat.S_IMODE(os.stat(self.mem.memory_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.mem.ledger_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.mem.base_dir).st_mode), 0o700)
        backups = os.path.join(self.mem.base_dir, "backups")
        self.assertEqual(stat.S_IMODE(os.stat(backups).st_mode), 0o700)

    async def test_backup_and_manifest_permissions(self):
        await self.mem.remember(mk())
        bk = await self.mem.backup()
        self.assertEqual(stat.S_IMODE(os.stat(bk["path"]).st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(os.stat(bk["path"] + ".manifest.json").st_mode), 0o600)


# ============================================= Re-Learning after Purge
class TestReLearning(Base):
    async def test_A_relearn_same_subject_after_purge(self):
        old = await self.mem.remember(mk(content="favorite color is blue",
                                         subject="user:color"))
        self.assertTrue(await self.mem.purge(old, reason="user erase"))
        new = await self.mem.remember(mk(content="favorite color is now green",
                                         subject="user:color"))
        self.assertIsNone(await self.mem.get(old))
        self.assertIsNotNone(await self.mem.get(new))
        res = await self.mem.recall("favorite color", subject="user:color")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].id, new)
        self.assertNotIn("blue", res[0].content)

    async def test_B_relearn_across_restart(self):
        old = await self.mem.remember(mk(content="color blue", subject="user:color"))
        await self.mem.purge(old, reason="erase")
        await self.mem.close()
        self.mem = SolvioMemory(self.dir)  # Restart
        new = await self.mem.remember(mk(content="color green", subject="user:color"))
        self.assertIsNone(await self.mem.get(old))
        self.assertIsNotNone(await self.mem.get(new))
        self.assertEqual([r.content for r in await self.mem.search("color")], ["color green"])

    async def test_C_backup_after_new_then_restore_keeps_new_drops_old(self):
        old = await self.mem.remember(mk(content="color blue old", subject="user:color"))
        await self.mem.purge(old, reason="erase")
        new = await self.mem.remember(mk(content="color green new", subject="user:color"))
        bk = await self.mem.backup()  # Backup NACH dem neuen Fakt
        await self.mem.restore(bk["path"])
        self.assertIsNone(await self.mem.get(old))       # alt nie zurueck
        self.assertIsNotNone(await self.mem.get(new))    # neu bleibt

    async def test_D_pre_purge_backup_restore_old_never_returns(self):
        old = await self.mem.remember(mk(content="color blue secret", subject="user:color"))
        bk = await self.mem.backup()                     # PRE-PURGE (enthaelt old)
        await self.mem.purge(old, reason="erase")
        stats = await self.mem.restore(bk["path"])
        self.assertEqual(stats["removed"], 1)
        self.assertIsNone(await self.mem.get(old))       # bleibt weg
        self.assertEqual(await self.mem.recall("color blue"), [])
        self.assertEqual(len(await self.mem.list_tombstones()), 1)


# ============================================= Idempotence / Partial Failures
class TestIdempotencePartial(Base):
    async def test_purge_twice_stable(self):
        rid = await self.mem.remember(mk(content="x", subject="user:x"))
        self.assertTrue(await self.mem.purge(rid, reason="a"))
        self.assertTrue(await self.mem.purge(rid, reason="b"))
        self.assertEqual(len(await self.mem.list_tombstones()), 1)

    async def test_purge_unknown_never_purged_false(self):
        self.assertFalse(await self.mem.purge("ghost-id", reason="x"))

    async def test_orphan_fts_does_not_surface_pii(self):
        rid = await self.mem.remember(mk(content="sensitive orphan zulu", subject="user:o"))
        # Memory-Zeile hart entfernen, FTS-Eintrag absichtlich stehen lassen.
        await self._exec_raw_on_store("DELETE FROM memories WHERE id=?", rid)
        # Suche darf den verwaisten FTS-Treffer NICHT als Inhalt liefern.
        self.assertEqual(await self.mem.search("sensitive orphan"), [])
        self.assertEqual(await self.mem.recall("sensitive orphan"), [])
        # Reconciliation raeumt den verwaisten FTS-Eintrag mit.
        await self.mem.reconcile()
        self.assertEqual(await self.mem.search("sensitive orphan"), [])

    async def test_restore_after_reconcile(self):
        rid = await self.mem.remember(mk(content="keep me", subject="user:k"))
        secret = await self.mem.remember(mk(content="wipe me", subject="user:s"))
        await self.mem.purge(secret, reason="erase")
        await self.mem.reconcile()
        bk = await self.mem.backup()
        await self.mem.restore(bk["path"])
        self.assertIsNotNone(await self.mem.get(rid))
        self.assertIsNone(await self.mem.get(secret))


if __name__ == "__main__":
    from _harness import run_unittest
    raise SystemExit(run_unittest(globals(), __name__))
