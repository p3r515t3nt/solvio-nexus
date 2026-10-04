"""N8/C3 §8 — Migrations- und Rollbacktest auf KOPIEN synthetischer Altschema-Stores.

Kein produktiver Speicher wird geoeffnet. Beide Dateien entstehen hier aus den
eingefrorenen Modulen des Rollback-Ziels `aa6d2ae`
(`tests/fixtures/rollback_aa6d2ae/`; die Fixture-Klassen importieren `costs`
aus dem KANDIDATEN-Pfad — fuer „alter Code schreibt" benutzt diese Suite deshalb
nur `ensure_schema` und rohes SQL, der volle Nachweis laeuft gegen den echten
`aa6d2ae`-Quellbaum im Unterprozess; Review Runde 6, C6-H2): das alte `conversation/store.py` legt das
exakte Altschema der Gespraeche an, das alte `cost_subjects.py` das exakte
Kosten-Schema v3. Die Zaehler entsprechen dem Bestandsinventar vom 17.09.2026
(182 Gespraeche / 473 Sitzungen / 2900 Nachrichten; 29 Aktivitaeten, 72
Invocations, 72 Reservierungen, Schema 3), damit derselbe Ablauf spaeter
unveraendert auf echten Kopien laufen kann (`exercise(...)`, siehe unten).

Bewiesen wird in Reihenfolge des Vertrags: additive Migration mit identischen
Zaehlern, Szenario (explizite Chats, Zustellungen, Links, `blocked` mit
gehaltener/abgebrochener Aktivitaet), Purge, der explizite Chats verschont und
ein verlinktes Sprachgespraech samt Link loescht, Rollback-Lesen beider Dateien
(alter Purge steht still, alter CostLedger braucht das Downgrade), Downgrade
nach `blocked` ohne Flag, erneute Migration mit Rueckimport.

Direkt: python tests/test_conversation_migration.py
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from solvio.conversation import ConversationStore  # noqa: E402
from solvio.agent_runtime import costs as C, cost_dispatch as D, cost_subjects as A, store as S  # noqa: E402

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "rollback_aa6d2ae")
#: Die Erwartung aus docs/plan/evidence/n8-state-inventory-20260917.json.
INVENTORY = {"conversations": 182, "sessions": 473, "messages": 2900,
             "activities": 29, "adaptive_extract": 15, "voice_delegate": 14,
             "invocations": 72, "reservations": 72, "cost_schema": 3}
DAY = 86400.0
T0 = 1_700_000_000.0


#: Die Migration laeuft, waehrend der juengste Bestand 2..89 Tage alt ist: der
#: Purge beim Oeffnen darf nichts finden — genau wie produktiv, wo der alte Code
#: bei jedem Start schon aufgeraeumt hat.
MIGRATION_AT = T0 + 91 * DAY


class _Clock:
    def __init__(self, t=MIGRATION_AT):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _load(name):
    spec = importlib.util.spec_from_file_location("solvio_rollback_" + name,
                                                  os.path.join(_FIXTURES, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


OLD_STORE = _load("conversation_store")
OLD_COSTS = _load("cost_subjects")


# ---------------------------------------------------------------- synthetische Altbestaende

def build_old_conversations(path: str) -> None:
    """Das exakte Altschema, gefuellt wie der produktive Bestand — ohne dessen Inhalt."""
    store = OLD_STORE.ConversationStore(path, now_fn=lambda: T0).open()
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        sessions = messages = 0
        for index in range(INVENTORY["conversations"]):
            cid = f"c-{index:016x}"
            at = T0 + 2 * DAY + index * DAY / 2.1        # 2 .. 88 Tage vor MIGRATION_AT
            db.execute("INSERT INTO conversations VALUES (?,?,?,'active')", (cid, at, at))
            per_conversation = 3 if index % 2 == 0 else 2   # 91*3 + 91*2 = 455, Rest unten
            for s in range(per_conversation):
                db.execute("INSERT INTO conversation_sessions VALUES (?,?,?,?,?)",
                           (f"s-{index}-{s}", cid, at + s, at + s + 30, "timeout"))
                sessions += 1
            for m in range(16 if index < 170 else 15):   # 170*16 + 12*15 = 2900
                db.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?)",
                           (f"m-{index:08x}{m:08x}", cid, m + 1,
                            "user" if m % 2 == 0 else "assistant",
                            f"synthetischer Satz {index}/{m}", at + m, f"s-{index}-0",
                            f"t-{index}-{m // 2}"))
                messages += 1
        # Restliche Sitzungen bis 473 an das letzte Gespraech.
        while sessions < INVENTORY["sessions"]:
            db.execute("INSERT INTO conversation_sessions VALUES (?,?,?,?,?)",
                       (f"s-extra-{sessions}", f"c-{181:016x}", T0, T0 + 1, "timeout"))
            sessions += 1
        require_equal(messages, INVENTORY["messages"], "fixture message count")
        require_equal(sessions, INVENTORY["sessions"], "fixture session count")


def build_v3_agent_runs(path: str) -> S.AgentRunLedger:
    """Das exakte Kosten-Schema v3 aus dem eingefrorenen Modul, mit Geldzustaenden."""
    ledger = S.AgentRunLedger(path)
    OLD_COSTS.ensure_schema(ledger, C.SCHEMA)
    with ledger._open() as db:
        require_equal(db.execute("SELECT version FROM agent_cost_schema").fetchone()[0], 3)
        db.execute("INSERT OR IGNORE INTO agent_cost_settings VALUES (1,1000,'owner:initial',?)", (T0,))
        states = ("reserved", "approval_required", "unbounded_cost", "unknown", "settled", "released")
        invocation_states = ("claimed", "finished", "unknown", "not_dispatched")
        activities = invocations = 0
        for index in range(INVENTORY["activities"]):
            purpose = "adaptive_extract" if index < INVENTORY["adaptive_extract"] else "voice_delegate"
            kind = "dashboard" if index % 2 else "app"
            subject = "ci-" + hashlib.sha256(f"subject-{index}".encode()).hexdigest()
            db.execute("INSERT INTO agent_cost_subjects VALUES (?,'interaction',NULL,?,?,?,?,NULL)",
                       (subject, "owner:device", kind, f"conversation:{index}", T0))
            db.execute("INSERT INTO agent_cost_policies(subject_id,ask_threshold_cents,created_at) "
                       "VALUES (?,1000,?)", (subject, T0))
            activity = f"ca-{index:032x}"
            db.execute("INSERT INTO agent_cost_activities(activity_id,subject_id,purpose,operation_key,"
                       "principal,source_kind,source_ref,conversation_id,message_id,content_digest,"
                       "accepted_at,expires_at,state,held_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (activity, subject, purpose, "" if purpose == "adaptive_extract" else f"op-{index}",
                        "owner:device", kind, f"session:{index}", f"conversation:{index}",
                        f"message:{index}", hashlib.sha256(f"digest-{index}".encode()).hexdigest(),
                        T0, T0 + 3600, ("pending", "completed", "cancelled")[index % 3],
                        "provider_hold" if index % 5 == 0 else ""))
            activities += 1
            # 72 Invocations/Reservierungen: 2 oder 3 je Aktivitaet (29*2 = 58, +14).
            for ordinal in range(3 if index < 14 else 2):
                reservation = f"ac-{index:04x}-{ordinal}"
                state = states[(index + ordinal) % len(states)]
                db.execute("INSERT INTO agent_cost_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (reservation, subject, f"pc-{index:04x}-{ordinal}", "ai_tool", "codex",
                            None if state == "unbounded_cost" else 100,
                            C.CostEvidence("enforceable_upper_bound", "old:bound").json(), state,
                            75 if state == "settled" else None,
                            "old-settlement" if state in ("settled", "released") else "", T0, T0))
                inv_state = invocation_states[(index + ordinal) % len(invocation_states)]
                db.execute("INSERT INTO agent_provider_invocations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (reservation, f"pc-{index:04x}-{ordinal}", subject, None, None, activity,
                            purpose, activity, ordinal + 1, "codex", "digest:old", "process:old",
                            inv_state, T0, None if inv_state == "claimed" else T0 + 1))
                invocations += 1
        require_equal(activities, INVENTORY["activities"])
        require_equal(invocations, INVENTORY["invocations"])
        require_equal(db.execute("PRAGMA foreign_key_check").fetchall(), [])
    return ledger


# ---------------------------------------------------------------- Zaehler und Abzuege

def conversation_counts(path: str) -> dict:
    with sqlite3.connect(path) as db:
        out = {}
        for table in ("conversations", "conversation_sessions", "conversation_messages",
                      "conversation_deliveries", "conversation_task_links", "conversation_creations"):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                out[table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out


def cost_counts(ledger) -> dict:
    with ledger._open() as db:
        out = {"version": db.execute("SELECT version FROM agent_cost_schema").fetchone()[0]}
        for table in ("agent_cost_activities", "agent_provider_invocations", "agent_cost_reservations",
                      A.TEXT_CHAT_PARK_ACTIVITIES, A.TEXT_CHAT_PARK_INVOCATIONS):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                out[table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        out["text_chat"] = db.execute(
            "SELECT COUNT(*) FROM agent_cost_activities WHERE purpose='text_chat'").fetchone()[0]
        out["foreign_key_check"] = db.execute("PRAGMA foreign_key_check").fetchall()
        return out


def cost_rows(ledger) -> dict:
    with ledger._open() as db:
        return {table: sorted(tuple(row) for row in db.execute(f"SELECT * FROM {table}"))
                for table in ("agent_cost_activities", "agent_provider_invocations",
                              "agent_cost_reservations", "agent_cost_subjects")}


def _copy(src: str, dst: str) -> str:
    """Eine Kopie inklusive WAL/SHM — die Originale bleiben unberuehrt."""
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(src + suffix):
            shutil.copy2(src + suffix, dst + suffix)
    return dst


async def _finished_run(*_):
    from solvio.specialists.launcher import Outcome
    return Outcome(True, text="synthetic answer", exit_code=0, process_started=True)


# ---------------------------------------------------------------- der Ablauf (§8)

def exercise(conversations_src: str, agent_runs_src: str, *, workdir: str,
             evidence_path: str | None = None) -> dict:
    """§8 Schritte 1–7 auf KOPIEN von `conversations_src`/`agent_runs_src`.

    Reicht man hier Kopien eines echten Sicherungssatzes herein (selbst dann
    kopiert dieser Ablauf noch einmal in `workdir`), laeuft derselbe Beweis auf
    echten Daten. Gibt die Zaehler zurueck und schreibt sie optional als JSON.
    """
    import asyncio
    import cost_schema_downgrade_v4_to_v3 as DG
    evidence: dict = {}
    conversations = _copy(conversations_src, os.path.join(workdir, "conversations.sqlite3"))
    agent_runs = _copy(agent_runs_src, os.path.join(workdir, "agent_runs.sqlite3"))

    # 2. Vorher-Zaehler
    evidence["before"] = {"conversations": conversation_counts(conversations),
                          "costs": cost_counts(S.AgentRunLedger(agent_runs))}
    before_rows = cost_rows(S.AgentRunLedger(agent_runs))
    with sqlite3.connect(conversations) as db:
        old_columns = [r[1] for r in db.execute("PRAGMA table_info(conversations)")]
        old_ids = sorted(r[0] for r in db.execute("SELECT conversation_id FROM conversations"))
    require("owner_principal" not in old_columns, "the fixture is not the old schema")

    # 3. Migration
    clock = _Clock()
    store = ConversationStore(conversations, now_fn=clock, retention_days=90.0).open()
    ledger = S.AgentRunLedger(agent_runs)
    C.CostLedger(ledger)
    after = {"conversations": conversation_counts(conversations), "costs": cost_counts(ledger)}
    evidence["after_migration"] = after
    for table in ("conversations", "conversation_sessions", "conversation_messages"):
        require_equal(after["conversations"][table], evidence["before"]["conversations"][table],
                      f"{table} count changed by the migration")
    for table in ("conversation_deliveries", "conversation_task_links", "conversation_creations"):
        require_equal(after["conversations"][table], 0, f"{table} is not empty after migration")
    require_equal(sorted(store.migrated_columns), ["explicit", "kind", "owner_principal", "room_device_id", "room_session_id", "title"])
    with sqlite3.connect(conversations) as db:
        rows = db.execute("SELECT owner_principal, kind, explicit, title, room_device_id, room_session_id FROM conversations").fetchall()
        require_equal(set(rows), {("", "voice", 0, "", "", "")}, "a historical conversation was reassigned")
        require_equal(sorted(r[0] for r in db.execute("SELECT conversation_id FROM conversations")), old_ids)
        require_equal(db.execute("PRAGMA foreign_key_check").fetchall(), [])
    require_equal(after["costs"]["version"], 4)
    require_equal(after["costs"]["foreign_key_check"], [])
    for table in ("agent_cost_activities", "agent_provider_invocations", "agent_cost_reservations"):
        require_equal(after["costs"][table], evidence["before"]["costs"][table], table)
    migrated_rows = cost_rows(ledger)
    for table, rows in before_rows.items():
        require_equal(migrated_rows[table], rows, f"{table} rows changed by the migration")
    require_equal(store.last_purge_error, None, store.last_purge_error)
    # Idempotent: ein zweites Oeffnen zieht nichts nach.
    again = ConversationStore(conversations, now_fn=clock, retention_days=90.0).open()
    require_equal(again.migrated_columns, [])
    again.close()

    # 4. Szenario
    chat_a, _ = store.create_conversation(owner_principal="owner:device-a", kind="text",
                                          client_request_id="req-chat-a")
    chat_b, _ = store.create_conversation(owner_principal="owner:device-b", kind="text",
                                          client_request_id="req-chat-b")
    chat_a, chat_b = chat_a["conversation_id"], chat_b["conversation_id"]

    def deliver(cid, principal, cm, text):
        row, created = store.accept_delivery(
            conversation_id=cid, principal=principal, client_message_id=cm, text=text,
            digest=hashlib.sha256(text.encode()).hexdigest(), source_kind="dashboard",
            source_ref="browser:s-1", core_id="core-1", source_generation="gen-1")
        require(created)
        return store.claim_next_delivery(cid, "pw-1")

    activities = A.ActivityLedger(ledger)
    d1 = deliver(chat_a, "owner:device-a", "cm-1", "Wie wird das Wetter morgen in Hamburg?")
    src1 = A._verified_source(principal="owner:device-a", source_kind="dashboard",
                              source_ref="browser:s-1", conversation_id=chat_a, message_id=d1["message_id"])
    binding1 = activities.admit(src1, content_digest=hashlib.sha256(b"d1").hexdigest(),
                                purpose="text_chat", operation_key=d1["delivery_id"], lifetime_seconds=900)
    store.record_activity(d1["delivery_id"], "pw-1", binding1.activity_id)

    async def run_claim():
        with D.interaction_cost_scope(ledger, activity_id=binding1.activity_id,
                                      content_digest=binding1.content_digest,
                                      quote_adapter=lambda *_: D.CostQuote(0, C.CostEvidence("free_local", "test"))):
            from solvio.specialists.launcher import Invocation
            return await D.dispatch("codex", Invocation("/synthetic", (), cwd="/", timeout=1),
                                    "answer", _finished_run)
    require(asyncio.run(run_claim()).outcome.ok, "the synthetic claim did not finish")
    store.complete_delivery(d1["delivery_id"], "pw-1", assistant_text="Sonnig, 21 Grad.")
    activities.finish(binding1.activity_id)

    d2 = deliver(chat_a, "owner:device-a", "cm-2", "Und uebermorgen?")
    src2 = A._verified_source(principal="owner:device-a", source_kind="dashboard",
                              source_ref="browser:s-1", conversation_id=chat_a, message_id=d2["message_id"])
    binding2 = activities.admit(src2, content_digest=hashlib.sha256(b"d2").hexdigest(),
                                purpose="text_chat", operation_key=d2["delivery_id"], lifetime_seconds=900)
    store.record_activity(d2["delivery_id"], "pw-1", binding2.activity_id)

    async def lost_claim():
        # Der Transport stirbt nach dem Claim: die Invocation wird `unknown` gebucht.
        async def dying(*_):
            raise RuntimeError("synthetic transport loss")
        with D.interaction_cost_scope(ledger, activity_id=binding2.activity_id,
                                      content_digest=binding2.content_digest,
                                      quote_adapter=lambda *_: D.CostQuote(0, C.CostEvidence("free_local", "test"))):
            from solvio.specialists.launcher import Invocation
            try:
                await D.dispatch("codex", Invocation("/synthetic", (), cwd="/", timeout=1), "answer", dying)
            except RuntimeError:
                pass
    asyncio.run(lost_claim())
    with ledger._open() as db:
        require_equal(db.execute("SELECT state FROM agent_provider_invocations WHERE activity_id=?",
                                 (binding2.activity_id,)).fetchone()[0], "unknown")
    # `blocked`/`cost_recovery_required`: kein `hold` (Muster voice_delegate), aber
    # in jedem Fall `finish(cancelled=True)` — eine terminale Zustellung laesst
    # keine offene Aktivitaet zurueck (§3.4, §1.6).
    store.block_delivery(d2["delivery_id"], "pw-1", error_code="cost_recovery_required")
    activities.finish(binding2.activity_id, cancelled=True)

    d3 = deliver(chat_b, "owner:device-b", "cm-1", "Bau mir ein Skript fuer die Sicherung.")
    store.complete_delivery(d3["delivery_id"], "pw-1", assistant_text="Aufgenommen.",
                            task_id="at-0123456789abcdef", run_id="ar-0123456789abcdef", revision=1)
    old_voice = old_ids[0]                          # historisch, aelter als 90 Tage
    store.add_task_link(old_voice, "at-fedcba9876543210", "ar-fedcba9876543210",
                        source="voice:s-historic")
    evidence["scenario"] = {"conversations": conversation_counts(conversations),
                            "costs": cost_counts(ledger)}
    require_equal(evidence["scenario"]["conversations"]["conversation_deliveries"], 3)
    require_equal(evidence["scenario"]["conversations"]["conversation_task_links"], 2)
    require_equal(evidence["scenario"]["costs"]["text_chat"], 2)
    scenario_copy = _copy(conversations, os.path.join(workdir, "conversations.scenario.sqlite3"))
    store.close()
    store = ConversationStore(conversations, now_fn=clock, retention_days=90.0).open()

    # 5. Purge +400 Tage
    clock.advance(400 * DAY)
    report = store.purge_expired()
    evidence["purge"] = {"report": report, "conversations": conversation_counts(conversations)}
    require_equal(store.last_purge_error, None)
    require_equal(report["conversations"], INVENTORY["conversations"], report)
    require_equal(store.conversation(old_voice), None, "the linked voice conversation survived")
    require_equal(store.task_links(old_voice), [], "the voice link survived")
    require_equal(evidence["purge"]["conversations"]["conversations"], 2)
    require_equal(evidence["purge"]["conversations"]["conversation_deliveries"], 3)
    require_equal(evidence["purge"]["conversations"]["conversation_task_links"], 1)
    require_equal(evidence["purge"]["conversations"]["conversation_messages"], 5)
    require_equal(sorted(c["conversation_id"] for c in store.list_conversations("owner:device-a")), [chat_a])
    require_equal(sorted(c["conversation_id"] for c in store.list_conversations("owner:device-b")), [chat_b])
    with sqlite3.connect(conversations) as db:
        require_equal(db.execute("PRAGMA foreign_key_check").fetchall(), [])
    store.close()

    # 6a. Rollback-Lesen: der alte Store auf einer frischen Kopie des Szenariostands.
    rollback = _copy(scenario_copy, os.path.join(workdir, "conversations.rollback.sqlite3"))
    legacy = OLD_STORE.ConversationStore(rollback, now_fn=clock, retention_days=90.0).open()
    try:
        require(legacy.last_purge_error is not None, "the old purge did not fail on C3 rows")
        require("FOREIGN KEY" in legacy.last_purge_error, legacy.last_purge_error)
        counts = conversation_counts(rollback)
        require_equal(counts["conversations"], INVENTORY["conversations"] + 2, counts)
        require_equal(counts["conversation_messages"], INVENTORY["messages"] + 5, counts)
        cid, resumed = legacy.begin_session("s-rollback")
        require(not resumed and cid not in (chat_a, chat_b))
        legacy.add_message(cid, "user", "im Rollback-Fenster")
        evidence["rollback_read"] = {"last_purge_error": legacy.last_purge_error.split(":")[0],
                                     "conversations": conversation_counts(rollback)}
    finally:
        legacy.close()

    # 6b. Kosten: der alte Code liest v4 NICHT — das Downgrade nach `blocked` laeuft ohne Flag.
    try:
        OLD_COSTS.ensure_schema(ledger, C.SCHEMA)
    except ValueError as exc:
        require_equal(str(exc), "unsupported_cost_schema")
    else:
        raise AssertionError("the rollback code accepted schema version 4")
    downgraded = DG.downgrade(agent_runs, now=clock())
    evidence["downgrade"] = downgraded | {"costs": cost_counts(ledger)}
    require_equal((downgraded["version_after"], downgraded["parked_activities"],
                   downgraded["parked_invocations"]), (3, 2, 2), downgraded)
    counts = cost_counts(ledger)
    require_equal(counts["version"], 3)
    require_equal(counts["agent_cost_activities"], INVENTORY["activities"])
    require_equal(counts["agent_provider_invocations"], INVENTORY["invocations"])
    require_equal(counts[A.TEXT_CHAT_PARK_ACTIVITIES], 2)
    require_equal(counts[A.TEXT_CHAT_PARK_INVOCATIONS], 2)
    require_equal(counts["agent_cost_reservations"], INVENTORY["reservations"] + 2, "reservations must stay")
    require_equal(counts["foreign_key_check"], [])
    OLD_COSTS.ensure_schema(ledger, C.SCHEMA)          # aa6d2ae oeffnet jetzt ohne Fehler
    with ledger._open() as db:
        try:
            db.execute("INSERT INTO agent_cost_activities(activity_id,subject_id,purpose,principal,"
                       "source_kind,source_ref,conversation_id,message_id,content_digest,accepted_at,"
                       "expires_at) VALUES ('ca-probe',?,'text_chat','o','app','r','c','m',?,1,2)",
                       (binding1.subject_id, "a" * 64))
        except sqlite3.IntegrityError:
            pass                                       # der enge CHECK von v3 greift
        else:
            raise AssertionError("the downgraded CHECK still accepts text_chat")
    # Erneute Migration importiert die geparkten Zeilen zurueck.
    C.CostLedger(ledger)
    restored = cost_counts(ledger)
    evidence["remigration"] = restored
    require_equal(restored["version"], 4)
    require_equal(restored["agent_cost_activities"], INVENTORY["activities"] + 2)
    require_equal(restored["agent_provider_invocations"], INVENTORY["invocations"] + 2)
    require_equal(restored["text_chat"], 2)
    require(A.TEXT_CHAT_PARK_ACTIVITIES not in restored and A.TEXT_CHAT_PARK_INVOCATIONS not in restored,
            "park tables survived the re-migration")
    require_equal(restored["foreign_key_check"], [])
    with ledger._open() as db:
        states = dict(db.execute("SELECT activity_id, state FROM agent_cost_activities WHERE purpose='text_chat'").fetchall())
        require_equal(states, {binding1.activity_id: "completed", binding2.activity_id: "cancelled"},
                      "parked states did not come back column-identical")
        claims = {row[0]: row[1] for row in db.execute(
            "SELECT activity_id, state FROM agent_provider_invocations WHERE activity_id IN (?,?)",
            (binding1.activity_id, binding2.activity_id))}
        require_equal(claims, {binding1.activity_id: "finished", binding2.activity_id: "unknown"},
                      "parked claims did not come back with their state")
    # Zusatz: ein kuenstlich `claimed` gesetzter Claim verweigert ohne Flag, laeuft mit Flag.
    with ledger._open() as db:
        db.execute("UPDATE agent_provider_invocations SET state='claimed', finished_at=NULL WHERE activity_id=?",
                   (binding1.activity_id,))
    try:
        DG.downgrade(agent_runs, now=clock())
    except DG.DowngradeRefused as exc:
        require(str(exc).startswith("text_chat_invocations_claimed:1"), str(exc))
    else:
        raise AssertionError("a live claim did not stop the downgrade")
    require_equal(cost_counts(ledger)["version"], 4, "a refused downgrade changed the schema")
    forced = DG.downgrade(agent_runs, core_stopped=True, now=clock())
    require_equal((forced["version_after"], forced["parked_invocations"]), (3, 2))
    evidence["forced_downgrade"] = forced
    C.CostLedger(ledger)
    require_equal(cost_counts(ledger)["text_chat"], 2)

    # 7. Evidence
    if evidence_path:
        with open(evidence_path, "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, indent=2, sort_keys=True, default=str)
    return evidence


# ---------------------------------------------------------------- Tests

def t_synthetic_old_stores_migrate_purge_and_roll_back_both_ways():
    with tempfile.TemporaryDirectory(prefix="solvio-c3-migration-") as tmp:
        with patch.dict(os.environ, {"SOLVIO_STATE_DIR": tmp, "SOLVIO_AGENT_RUNS_DB": ""}):
            originals = os.path.join(tmp, "originals")
            os.makedirs(originals)
            conversations = os.path.join(originals, "conversations.sqlite3")
            agent_runs = os.path.join(originals, "agent_runs.sqlite3")
            build_old_conversations(conversations)
            build_v3_agent_runs(agent_runs)
            fingerprint = {name: hashlib.sha256(open(os.path.join(originals, name), "rb").read()).hexdigest()
                           for name in os.listdir(originals) if name.endswith(".sqlite3")}
            work = os.path.join(tmp, "work")
            os.makedirs(work)
            evidence = exercise(conversations, agent_runs, workdir=work,
                                evidence_path=os.path.join(tmp, "evidence.json"))
            require_equal(evidence["before"]["conversations"]["conversations"], INVENTORY["conversations"])
            require_equal(evidence["before"]["costs"]["version"], INVENTORY["cost_schema"])
            # Die Originale wurden nie beruehrt.
            for name, digest in fingerprint.items():
                require_equal(hashlib.sha256(open(os.path.join(originals, name), "rb").read()).hexdigest(),
                              digest, f"the original {name} was modified")
            blob = open(os.path.join(tmp, "evidence.json"), encoding="utf-8").read()
            require("synthetischer Satz" not in blob and "Wetter" not in blob,
                    "the evidence carries message text")


def t_the_rollback_fixtures_match_the_published_source_digests():
    """Both actual rollback source fixtures are fixed; private Git is not required."""
    from _public_source_fixture import source_bytes
    for name in ("cost_subjects.py", "conversation_store.py"):
        committed = source_bytes("rollback_aa6d2ae/" + name)
        require_equal(open(os.path.join(_FIXTURES, name), "rb").read(), committed,
                      "the actual rollback fixture changed: " + name)
    require(b"text_chat" not in source_bytes("rollback_aa6d2ae/cost_subjects.py"),
            "the rollback cost fixture already knows text_chat")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
