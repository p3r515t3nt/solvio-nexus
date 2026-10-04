"""Umfassende Tests der SOLVIO Memory Foundation (STEP 20).

Nur Standardbibliothek (unittest + asyncio); das Projekt hat bewusst kein pytest.
Ausfuehren:  python -m unittest discover -s tests -p 'test_*.py'

Enthaelt die Frozen-Benchmark-Subset-Tests (2/3/8/9) sowie den verpflichtenden
Pre-Purge-Backup -> Purge -> Restore-Test (gepurgte Daten kehren NICHT zurueck).
"""
from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from solvio.contracts.memory import (
    MemoryRecord,
    MemoryStore,
    MemoryType,
    ProvenanceEntry,
    Relation,
    RetentionPolicy,
    Sensitivity,
)
from solvio.contracts.trust import (
    SourceType,
    TrustLevel,
    bears_authority,
    is_untrusted,
    trust_for_source,
)
from solvio.memory import SolvioMemory


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def mk(**over) -> MemoryRecord:
    """Baut einen MemoryRecord mit sinnvollen Defaults; override per kwargs."""
    now = _utc()
    base = dict(
        id="",
        memory_type=MemoryType.USER,
        content="the user drinks flat white coffee in the morning",
        subject="user:beverage",
        source="voice",
        source_type=SourceType.USER_DIRECT,
        created_at=now,
        updated_at=now,
        trust_level=TrustLevel.USER_DIRECT,
    )
    base.update(over)
    return MemoryRecord(**base)


class MemoryTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="solvio-mem-test-")
        self.mem = SolvioMemory(self.dir)

    async def asyncTearDown(self) -> None:
        await self.mem.close()
        shutil.rmtree(self.dir, ignore_errors=True)


# ============================================================ Basis-CRUD
class TestCrud(MemoryTestBase):
    async def test_remember_returns_id_and_get_roundtrips(self):
        rid = await self.mem.remember(mk(content="hello world"))
        self.assertTrue(rid)
        rec = await self.mem.get(rid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.content, "hello world")
        self.assertEqual(rec.id, rid)
        self.assertTrue(rec.is_current)

    async def test_remember_generates_uuid_when_id_empty(self):
        rid = await self.mem.remember(mk())
        self.assertEqual(len(rid), 32)  # uuid4().hex

    async def test_get_unknown_returns_none(self):
        self.assertIsNone(await self.mem.get("does-not-exist"))

    async def test_timestamps_are_timezone_aware_utc(self):
        rid = await self.mem.remember(mk())
        rec = await self.mem.get(rid)
        self.assertIsNotNone(rec.created_at.tzinfo)
        self.assertEqual(rec.created_at.utcoffset(), timedelta(0))

    async def test_tags_metadata_relations_roundtrip(self):
        rid = await self.mem.remember(mk(
            tags=["coffee", "habit"],
            metadata={"confidence_source": "explicit"},
            relations=[Relation(kind="about", target_id="user:profile")],
        ))
        rec = await self.mem.get(rid)
        self.assertEqual(set(rec.tags), {"coffee", "habit"})
        self.assertEqual(rec.metadata["confidence_source"], "explicit")
        self.assertEqual(rec.relations[0].kind, "about")


# ============================================================ Recall / Search
class TestRecallSearch(MemoryTestBase):
    async def test_recall_finds_current_truth(self):
        await self.mem.remember(mk(content="the user drinks flat white coffee"))
        res = await self.mem.recall("coffee")
        self.assertEqual(len(res), 1)

    async def test_recall_no_match_returns_empty(self):
        await self.mem.remember(mk(content="unrelated content about bicycles"))
        self.assertEqual(await self.mem.recall("astrophysics"), [])

    async def test_recall_filters_by_subject(self):
        await self.mem.remember(mk(content="likes coffee", subject="user:beverage"))
        await self.mem.remember(mk(content="likes coffee tables", subject="user:furniture"))
        res = await self.mem.recall("coffee", subject="user:furniture")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].subject, "user:furniture")

    async def test_recall_filters_by_type(self):
        await self.mem.remember(mk(content="coffee preference", memory_type=MemoryType.PREFERENCE))
        await self.mem.remember(mk(content="coffee episodic note", memory_type=MemoryType.EPISODIC))
        res = await self.mem.recall("coffee", memory_types=[MemoryType.PREFERENCE])
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].memory_type, MemoryType.PREFERENCE)

    async def test_recall_respects_limit(self):
        for i in range(5):
            await self.mem.remember(mk(content=f"coffee note number {i}", subject=f"s{i}"))
        res = await self.mem.recall("coffee", limit=3)
        self.assertLessEqual(len(res), 3)

    async def test_search_is_broader_than_recall(self):
        await self.mem.remember(mk(content="the meeting is about the coffee budget"))
        self.assertTrue(await self.mem.search("budget"))

    async def test_valid_until_excludes_expired_from_recall(self):
        past = _utc() - timedelta(days=1)
        await self.mem.remember(mk(content="temporary coffee offer", valid_until=past))
        self.assertEqual(await self.mem.recall("coffee"), [])


# ============================================================ Update
class TestUpdate(MemoryTestBase):
    async def test_update_changes_field_and_bumps_updated_at(self):
        rid = await self.mem.remember(mk(content="old", confidence=0.5))
        before = (await self.mem.get(rid)).updated_at
        rec = await self.mem.update(rid, {"content": "new", "confidence": 0.9})
        self.assertEqual(rec.content, "new")
        self.assertAlmostEqual(rec.confidence, 0.9)
        self.assertGreaterEqual(rec.updated_at, before)

    async def test_update_unknown_raises(self):
        with self.assertRaises(KeyError):
            await self.mem.update("nope", {"content": "x"})

    async def test_update_rejects_non_whitelisted_field(self):
        rid = await self.mem.remember(mk())
        with self.assertRaises(ValueError):
            await self.mem.update(rid, {"id": "hacked"})


# ======================================= Supersession (Frozen Test 2: current truth)
class TestSupersession(MemoryTestBase):
    async def test_supersede_recall_returns_only_new_truth(self):
        old = await self.mem.remember(mk(content="user lives in Berlin", subject="user:city"))
        new_rec = mk(content="user lives in Munich", subject="user:city")
        new = await self.mem.supersede(old, new_rec)
        res = await self.mem.recall("user lives", subject="user:city")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].id, new.id)
        self.assertIn("Munich", res[0].content)

    async def test_supersede_sets_links(self):
        old = await self.mem.remember(mk(content="v1", subject="user:city"))
        new = await self.mem.supersede(old, mk(content="v2", subject="user:city"))
        old_rec = await self.mem.get(old)
        self.assertEqual(old_rec.superseded_by, new.id)
        self.assertFalse(old_rec.is_current)
        self.assertEqual(new.supersedes, old)
        self.assertTrue((await self.mem.get(new.id)).is_current)

    async def test_history_contains_full_chain(self):
        old = await self.mem.remember(mk(content="Berlin", subject="user:city"))
        await self.mem.supersede(old, mk(content="Munich", subject="user:city"))
        hist = await self.mem.history("user:city")
        self.assertEqual(len(hist), 2)
        self.assertEqual([h.content for h in hist], ["Berlin", "Munich"])

    async def test_supersede_unknown_raises(self):
        with self.assertRaises(KeyError):
            await self.mem.supersede("ghost", mk())


# ============================================= Provenance (Frozen Test 3)
class TestProvenance(MemoryTestBase):
    async def test_provenance_roundtrip(self):
        prov = [
            ProvenanceEntry(source_type=SourceType.WEB_PAGE, source="https://example.org",
                            trust_level=TrustLevel.UNTRUSTED_WEB, at=_utc(), note="scraped"),
            ProvenanceEntry(source_type=SourceType.USER_DIRECT, source="voice",
                            trust_level=TrustLevel.USER_DIRECT, at=_utc(), note="confirmed"),
        ]
        rid = await self.mem.remember(mk(content="fact with provenance", provenance=prov))
        got = await self.mem.get_provenance(rid)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0].source_type, SourceType.WEB_PAGE)
        self.assertEqual(got[0].trust_level, TrustLevel.UNTRUSTED_WEB)
        self.assertEqual(got[1].source, "voice")

    async def test_trust_is_separate_from_source(self):
        # UNTRUSTED CONTENT CAN PROVIDE INFORMATION, BUT NEVER AUTHORITY.
        rid = await self.mem.remember(mk(
            content="claim scraped from the web",
            source="https://evil.example",
            source_type=SourceType.WEB_PAGE,
            trust_level=TrustLevel.UNTRUSTED_WEB,
        ))
        rec = await self.mem.get(rid)
        self.assertEqual(rec.trust_level, TrustLevel.UNTRUSTED_WEB)
        self.assertTrue(is_untrusted(rec.trust_level))
        self.assertFalse(bears_authority(rec.trust_level))
        # Der Store speichert Trust faithfully, unabhaengig von source.
        self.assertEqual(trust_for_source(SourceType.WEB_PAGE), TrustLevel.UNTRUSTED_WEB)

    async def test_untrusted_stored_but_not_authority_bearing(self):
        for st, expect_untrusted in [
            (SourceType.GMAIL_MESSAGE, True),
            (SourceType.WEB_PAGE, True),
            (SourceType.USER_DIRECT, False),
        ]:
            lvl = trust_for_source(st)
            self.assertEqual(is_untrusted(lvl), expect_untrusted)
            if expect_untrusted:
                self.assertFalse(bears_authority(lvl))


# ============================================= Relations
class TestRelations(MemoryTestBase):
    async def test_list_related_follows_edges(self):
        target = await self.mem.remember(mk(content="the profile", subject="user:profile"))
        rid = await self.mem.remember(mk(
            content="detail about the user",
            relations=[Relation(kind="about", target_id=target)],
        ))
        rel = await self.mem.list_related(rid)
        self.assertEqual(len(rel), 1)
        self.assertEqual(rel[0].id, target)


# ============================================= Forget / Purge (Frozen Test 9)
class TestForgetPurge(MemoryTestBase):
    async def test_forget_excludes_from_recall_but_keeps_history(self):
        rid = await self.mem.remember(mk(content="forgettable coffee note", subject="user:beverage"))
        self.assertTrue(await self.mem.forget(rid, reason="user asked"))
        self.assertEqual(await self.mem.recall("coffee"), [])
        self.assertEqual(await self.mem.search("coffee"), [])
        hist = await self.mem.history("user:beverage")
        self.assertEqual(len(hist), 1)  # in Historie erhalten

    async def test_forget_unknown_returns_false(self):
        self.assertFalse(await self.mem.forget("ghost", reason="x"))

    async def test_purge_removes_record_and_writes_tombstone(self):
        rid = await self.mem.remember(mk(content="sensitive secret", subject="user:secret"))
        self.assertTrue(await self.mem.purge(rid, reason="gdpr erase"))
        self.assertIsNone(await self.mem.get(rid))
        self.assertEqual(await self.mem.recall("secret"), [])
        self.assertEqual(await self.mem.history("user:secret"), [])  # weg, nur Tombstone
        tombs = await self.mem.list_tombstones()
        self.assertEqual(len(tombs), 1)
        self.assertEqual(tombs[0].reason, "gdpr erase")

    async def test_purge_is_idempotent(self):
        rid = await self.mem.remember(mk(content="secret", subject="user:secret"))
        self.assertTrue(await self.mem.purge(rid, reason="once"))
        self.assertTrue(await self.mem.purge(rid, reason="again"))  # bereits gepurgt -> True
        self.assertEqual(len(await self.mem.list_tombstones()), 1)

    async def test_purge_unknown_never_purged_returns_false(self):
        self.assertFalse(await self.mem.purge("ghost", reason="x"))

    async def test_tombstone_is_contentless(self):
        rid = await self.mem.remember(mk(content="my home address is Musterstr 1",
                                         subject="user:address"))
        await self.mem.purge(rid, reason="privacy")
        t = (await self.mem.list_tombstones())[0]
        # Kein Klartext von id/subject/content im Tombstone.
        self.assertNotIn("Musterstr", t.subject_hash)
        self.assertNotIn("user:address", t.subject_hash)
        self.assertEqual(len(t.subject_hash), 64)  # sha256 hex


# ============================================= Sensitivity / Retention
class TestSensitivityRetention(MemoryTestBase):
    async def test_secret_reference_stored_as_reference_not_value(self):
        # SECRET_REFERENCE: content ist ein Verweis, NICHT der Geheimwert.
        rid = await self.mem.remember(mk(
            content="secret_ref://keychain/openai_api_key",
            subject="secret:openai",
            sensitivity=Sensitivity.SECRET_REFERENCE,
        ))
        rec = await self.mem.get(rid)
        self.assertEqual(rec.sensitivity, Sensitivity.SECRET_REFERENCE)
        self.assertTrue(rec.content.startswith("secret_ref://"))
        self.assertNotIn("sk-", rec.content)

    async def test_ttl_expiry_forgets_by_default(self):
        old = _utc() - timedelta(days=10)
        rid = await self.mem.remember(mk(
            content="ephemeral coffee reminder", subject="user:reminder",
            created_at=old, updated_at=old,
            retention_policy=RetentionPolicy(mode="ttl", ttl_days=7, purge_on_expiry=False),
        ))
        out = await self.mem.apply_retention()
        self.assertIn(rid, out["forgotten"])
        self.assertEqual(await self.mem.recall("coffee"), [])
        self.assertEqual(len(await self.mem.history("user:reminder")), 1)  # noch in Historie

    async def test_ttl_expiry_purges_when_purge_on_expiry(self):
        old = _utc() - timedelta(days=10)
        rid = await self.mem.remember(mk(
            content="ephemeral secret token reference", subject="user:token",
            created_at=old, updated_at=old,
            retention_policy=RetentionPolicy(mode="ttl", ttl_days=7, purge_on_expiry=True),
        ))
        out = await self.mem.apply_retention()
        self.assertIn(rid, out["purged"])
        self.assertIsNone(await self.mem.get(rid))
        self.assertEqual(len(await self.mem.list_tombstones()), 1)


# ============================================= Consolidate (STEP 20 = no-op)
class TestConsolidate(MemoryTestBase):
    async def test_consolidate_is_deterministic_noop(self):
        await self.mem.remember(mk(content="a"))
        out = await self.mem.consolidate()
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["merged"], 0)
        self.assertTrue(out["tombstones_respected"])


# ============================================= Persistenz (Frozen Test 2)
class TestPersistence(MemoryTestBase):
    async def test_data_survives_reopen(self):
        rid = await self.mem.remember(mk(content="durable coffee fact", subject="user:beverage"))
        await self.mem.close()
        self.mem = SolvioMemory(self.dir)  # gleicher Ordner, neue Instanz
        rec = await self.mem.get(rid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.content, "durable coffee fact")
        self.assertEqual(len(await self.mem.recall("coffee")), 1)


# ============================================= Backup / Restore (Frozen Test 8)
class TestBackupRestore(MemoryTestBase):
    async def test_backup_and_verify_ok(self):
        await self.mem.remember(mk(content="fact for backup"))
        bk = await self.mem.backup()
        v = await self.mem.verify_backup(bk["path"])
        self.assertTrue(v["ok"], v.get("errors"))
        self.assertEqual(v["manifest"]["record_count"], 1)

    async def test_verify_detects_checksum_tampering(self):
        await self.mem.remember(mk(content="fact"))
        bk = await self.mem.backup()
        with open(bk["path"], "ab") as fh:
            fh.write(b"corruption")  # Datei veraendern -> Checksumme passt nicht
        v = await self.mem.verify_backup(bk["path"])
        self.assertFalse(v["ok"])

    async def test_restore_roundtrip_recovers_records(self):
        rid = await self.mem.remember(mk(content="recover me", subject="user:x"))
        bk = await self.mem.backup()
        # nach Backup weiteren Record anlegen, dann restaurieren -> point-in-time
        rid2 = await self.mem.remember(mk(content="added after backup", subject="user:y"))
        await self.mem.restore(bk["path"])
        self.assertIsNotNone(await self.mem.get(rid))
        self.assertIsNone(await self.mem.get(rid2))  # nach Backup -> nicht im Snapshot

    async def test_restore_rejects_invalid_backup(self):
        await self.mem.remember(mk())
        bk = await self.mem.backup()
        with open(bk["path"], "r+b") as fh:  # DB-Header zerstoeren
            fh.seek(0)
            fh.write(b"\x00" * 32)
        with self.assertRaises(ValueError):
            await self.mem.restore(bk["path"])

    # ----- DER verpflichtende Pre-Purge-Restore-Test -----
    async def test_pre_purge_backup_then_purge_then_restore_does_not_return(self):
        secret = await self.mem.remember(mk(
            content="the secret diary entry about my therapy", subject="user:diary"))
        keep = await self.mem.remember(mk(
            content="public fact the user likes coffee", subject="user:beverage"))
        bk = await self.mem.backup()                       # PRE-PURGE-Backup (enthaelt Secret)

        self.assertTrue(await self.mem.purge(secret, reason="user requested erase"))
        self.assertIsNone(await self.mem.get(secret))

        stats = await self.mem.restore(bk["path"])         # ALTES Backup einspielen
        self.assertEqual(stats["removed"], 1)              # Ledger entfernte 1 gepurgten Record

        # MUSS gelten: gepurgte Daten kehren NICHT zurueck.
        self.assertIsNone(await self.mem.get(secret))
        self.assertEqual(await self.mem.recall("secret diary"), [])
        self.assertEqual(await self.mem.history("user:diary"), [])
        # Nicht-gepurgte Daten kehren korrekt zurueck.
        self.assertIsNotNone(await self.mem.get(keep))
        self.assertEqual(len(await self.mem.recall("coffee")), 1)
        # Tombstone ueberlebt den Restore des Vor-Purge-Backups.
        self.assertEqual(len(await self.mem.list_tombstones()), 1)


# ============================================= Sicherheit / Integritaet
class TestSafety(MemoryTestBase):
    async def test_integrity_check_ok(self):
        await self.mem.remember(mk())
        self.assertEqual(await self.mem.integrity_check(), "ok")

    async def test_backup_path_traversal_rejected(self):
        outside = os.path.join(os.path.dirname(self.dir), "escape.bak")
        with self.assertRaises(ValueError):
            await self.mem.backup(outside)

    async def test_ledger_and_memory_are_separate_files(self):
        await self.mem.remember(mk())
        self.assertTrue(os.path.exists(os.path.join(self.dir, "memory.sqlite3")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "privacy_ledger.sqlite3")))


# ============================================= Protocol-Konformitaet
class TestProtocolConformance(MemoryTestBase):
    async def test_instance_conforms_to_memorystore_protocol(self):
        # MemoryStore ist @runtime_checkable -> prueft, dass alle 14 Methoden existieren.
        self.assertIsInstance(self.mem, MemoryStore)

    async def test_all_fourteen_operations_present(self):
        # 13 aus STEP 20, plus `reinforce` aus Adaptive Memory V1: append-only
        # Provenienz, damit Wiederholung keinen zweiten Record erzeugt.
        for op in ("remember", "get", "recall", "search", "update", "supersede",
                   "forget", "purge", "history", "consolidate", "list_related",
                   "get_provenance", "list_tombstones", "reinforce"):
            self.assertTrue(callable(getattr(self.mem, op)), op)


# ============================================= Async / Nebenlaeufigkeit
class TestAsync(MemoryTestBase):
    async def test_concurrent_remember_is_serialized(self):
        import asyncio
        ids = await asyncio.gather(*[
            self.mem.remember(mk(content=f"concurrent note {i}", subject=f"s{i}"))
            for i in range(25)
        ])
        self.assertEqual(len(set(ids)), 25)
        self.assertEqual(await self.mem.count(), 25)


if __name__ == "__main__":
    from _harness import run_unittest
    raise SystemExit(run_unittest(globals(), __name__))
