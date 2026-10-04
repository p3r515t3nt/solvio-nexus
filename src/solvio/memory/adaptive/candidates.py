"""Der Kandidatenspeicher — Vermutungen, die noch kein Gedaechtnis sind.

EIN KANDIDAT IST KEIN GEDAECHTNIS. Er liegt in einer eigenen Datei
(`candidates.sqlite3`), und das ist keine Ordnungsliebe, sondern die tragende
Entscheidung des ganzen Entwurfs:

* Das kanonische Schema bleibt byte-identisch — die Migration fasst
  `memory.sqlite3` nicht an.
* Kandidaten waren nie Wahrheit und duerfen hart geloescht werden. Die
  Tombstone-Zeremonie des Contracts gilt dem Gedaechtnis, nicht den
  Vermutungen.
* Ein Werkzeug, das den kanonischen Store liest, KANN Kandidaten nicht
  versehentlich mitlesen. Ein Statusfeld im selben Store haette verlangt, dass
  jeder Leser daran denkt — und einer vergisst es. Die erste Ausbaustufe der
  Obsidian-Projektion hat genau dieses Muster schon einmal vorgefuehrt, mit
  `is_current`.

DIE UNTERDRUECKUNGSLISTE ist die zweite Haelfte. Lehnt der Mensch einen
Vorschlag ab oder laesst er Gelerntes vergessen, wird der Dedup-Schluessel hier
vermerkt — sonst formte die naechste Wiederholung denselben Kandidaten neu, und
SOLVIO fragte erneut, was laengst beantwortet war. **Eine Ablehnung, die nicht
haelt, ist keine.**

Bewusst in Kauf genommen: diese Datei liegt ausserhalb der Ledger-Garantien.
Geht sie verloren, ist der schlimmste Ausgang eine ERNEUTE FRAGE — nie eine
stille Adoption. Das ist die richtige Richtung zu scheitern.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")

CANDIDATE_DB = "candidates.sqlite3"

# --------------------------------------------------------------- Zustaende
GATHERING = "gathering"
ASK_PENDING = "ask_pending"
CONTESTED = "contested"
ADOPTING = "adopting"
ADOPTED = "adopted"
DECLINED = "declined"
EXPIRED = "expired"

STATES = (GATHERING, ASK_PENDING, CONTESTED, ADOPTING, ADOPTED, DECLINED, EXPIRED)

#: Offen heisst: der Kandidat lebt und kann sich noch bewegen.
OPEN_STATES = (GATHERING, ASK_PENDING, CONTESTED, ADOPTING)

#: Terminal heisst terminal. Ein neuer Anlauf ist ein NEUER Kandidat — und der
#: laeuft gegen die Unterdrueckungsliste. Es gibt keinen Wiedereintritt.
TERMINAL_STATES = (ADOPTED, DECLINED, EXPIRED)

#: Die Uebergangstabelle aus ADAPTIVE_MEMORY_STATE_MACHINE.md §1.1, woertlich.
#: Was hier nicht steht, ist verboten — und `transition()` wirft dann.
ALLOWED: dict[str, frozenset[str]] = {
    GATHERING: frozenset({GATHERING, ADOPTING, ADOPTED, ASK_PENDING, CONTESTED, EXPIRED}),
    ASK_PENDING: frozenset({ADOPTING, ADOPTED, DECLINED, EXPIRED}),
    CONTESTED: frozenset({ADOPTING, ADOPTED, DECLINED, EXPIRED}),
    ADOPTING: frozenset({ADOPTED}),
    ADOPTED: frozenset(),
    DECLINED: frozenset(),
    EXPIRED: frozenset(),
}

#: Warum gefragt wird. Steht in der WISSEN-Ansicht und im Freigabetext.
ASK_SENSITIVE = "sensitive"
ASK_RULE = "rule"
ASK_CONTRADICTION = "contradiction"
ASK_CATEGORY = "category"

#: Warum unterdrueckt wurde.
SUPPRESS_DECLINED = "declined"
SUPPRESS_FORGOTTEN = "forgotten"

#: Fristen aus der Zustandsmaschine. Ein Kandidat, der verhungert, ist ein
#: vorgesehener Ausgang — kein Fehler.
GATHER_DAYS = 90
ASK_DAYS = 30

#: Obergrenzen. Ein Speicher fuer Vermutungen darf nicht unbegrenzt wachsen:
#: sonst waere er eine Chronik des Menschen, die niemand bestellt hat.
MAX_EVIDENCE = 20
MAX_OPEN_CANDIDATES = 500
MAX_NOTE = 160

_FILE_MODE = 0o600
_DIR_MODE = 0o700


class CandidateError(RuntimeError):
    """Etwas soll passieren, das die Zustandsmaschine nicht vorsieht."""


class IllegalTransition(CandidateError):
    """Ein verbotener Zustandsuebergang. Terminal bleibt terminal."""


@dataclass
class Evidence:
    """Eine Beobachtung. Bezeichner und Kurzform — nie ein Transkript.

    `note` ist auf `MAX_NOTE` Zeichen begrenzt und traegt einen Praefix
    (`stated:` / `observed:`), damit spaeter vorlesbar ist, WIE SOLVIO zu einer
    Annahme kam, ohne dass das Gesagte irgendwo doppelt liegt.
    """

    at: str
    kind: str                      # stated | observed
    conversation_id: str = ""
    message_id: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"at": self.at, "kind": self.kind,
                "conversation_id": self.conversation_id,
                "message_id": self.message_id, "note": self.note[:MAX_NOTE]}

    @property
    def day(self) -> str:
        return (self.at or "")[:10]


@dataclass
class Candidate:
    """Ein Vorschlag. Nicht mehr, und mit Absicht nicht weniger."""

    id: str
    statement: str
    dedup_key: str
    kind: str                      # stated | inferred
    memory_type: str
    subject: str
    sensitivity: str
    state: str = GATHERING
    ask_reason: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""
    contested_memory_id: str = ""
    adopted_memory_id: str = ""
    asked_at: str = ""
    decided_at: str = ""
    valid_until: str = ""
    adoption_binding: dict[str, str] = field(default_factory=dict)

    def independent_conversations(self) -> int:
        """Wie viele UNABHAENGIGE Gespraeche diese Annahme stuetzen.

        Unabhaengig heisst: andere `conversation_id` UND anderer Tag. Beides
        zusammen, weil beides einzeln umgehbar waere — zehn Wiederholungen in
        derselben Sitzung sind eine Beobachtung, und zwei Gespraeche am selben
        Nachmittag sind eine Stimmung. Ohne diese Regel koennte jemand (oder
        etwas) Gewissheit durch blosse Wiederholung herstellen.
        """
        seen: set[tuple[str, str]] = set()
        for entry in self.evidence:
            seen.add((entry.conversation_id or "", entry.day))
        conversations = {c for c, _ in seen if c}
        days = {d for _, d in seen if d}
        return min(len(conversations), len(days)) if conversations else 0

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "statement": self.statement,
                "dedup_key": self.dedup_key, "kind": self.kind,
                "memory_type": self.memory_type, "subject": self.subject,
                "sensitivity": self.sensitivity, "state": self.state,
                "ask_reason": self.ask_reason,
                "evidence": [e.as_dict() for e in self.evidence],
                "first_seen": self.first_seen, "last_seen": self.last_seen,
                "contested_memory_id": self.contested_memory_id,
                "adopted_memory_id": self.adopted_memory_id,
                "asked_at": self.asked_at, "decided_at": self.decided_at,
                "valid_until": self.valid_until}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id                  TEXT PRIMARY KEY,
    statement           TEXT NOT NULL,
    dedup_key           TEXT NOT NULL,
    kind                TEXT NOT NULL,
    memory_type         TEXT NOT NULL,
    subject             TEXT NOT NULL,
    sensitivity         TEXT NOT NULL,
    state               TEXT NOT NULL,
    ask_reason          TEXT NOT NULL DEFAULT '',
    evidence            TEXT NOT NULL DEFAULT '[]',
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    contested_memory_id TEXT NOT NULL DEFAULT '',
    adopted_memory_id   TEXT NOT NULL DEFAULT '',
    asked_at            TEXT NOT NULL DEFAULT '',
    decided_at          TEXT NOT NULL DEFAULT '',
    valid_until         TEXT NOT NULL DEFAULT ''
);
-- Hoechstens EIN offener Kandidat je Schluessel. Ohne diesen Index koennten
-- zwei Turns denselben Gedanken doppelt anlegen und beide adoptiert werden.
CREATE UNIQUE INDEX IF NOT EXISTS candidates_one_open
    ON candidates(dedup_key)
    WHERE state IN ('gathering', 'ask_pending', 'contested', 'adopting');
CREATE INDEX IF NOT EXISTS candidates_state ON candidates(state);
CREATE TABLE IF NOT EXISTS suppressions (
    dedup_key  TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    reason     TEXT NOT NULL
);
-- Bounded work journal, never canonical knowledge or a conversation archive.
-- Freeze a validated batch before its first effect; resume its exact cursor.
CREATE TABLE IF NOT EXISTS extraction_batches (
    activity_id TEXT PRIMARY KEY, content_digest TEXT NOT NULL,
    payload TEXT NOT NULL, next_index INTEGER NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


class CandidateStore:
    """Durabel, begrenzt, lokal — und nie Teil des Gedaechtnisses.

    Nebenlaeufigkeit wie im kanonischen Store: alle Zugriffe laufen in EINEM
    Worker-Thread, der Eventloop blockiert nie, und die Uebergaenge sind
    serialisiert.
    """

    def __init__(self, base_dir: str) -> None:
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)
        self.path = os.path.join(self.base_dir, CANDIDATE_DB)
        self._pool = ThreadPoolExecutor(max_workers=1,
                                        thread_name_prefix="solvio-cand")
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        # N5: an in-flight adoption still occupies its dedup key. Existing
        # databases have the old partial index, so replace it atomically.
        self._conn.execute("BEGIN IMMEDIATE")
        if 'adoption_binding' not in {r[1] for r in self._conn.execute('PRAGMA table_info(candidates)')}:
            self._conn.execute("ALTER TABLE candidates ADD COLUMN adoption_binding TEXT NOT NULL DEFAULT '{}'")
        index = self._conn.execute("SELECT sql FROM sqlite_master WHERE name='candidates_one_open'").fetchone()
        if index is None or "'adopting'" not in index[0]:
            self._conn.execute("DROP INDEX IF EXISTS candidates_one_open")
            self._conn.execute("CREATE UNIQUE INDEX candidates_one_open ON candidates(dedup_key) "
                               "WHERE state IN ('gathering','ask_pending','contested','adopting')")
        self._conn.commit()
        self._closed = False
        self._harden()

    def _harden(self) -> None:
        for path in (self.base_dir,):
            try:
                os.chmod(path, _DIR_MODE)
            except (OSError, NotImplementedError):
                pass
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            try:
                if os.path.exists(path):
                    os.chmod(path, _FILE_MODE)
            except (OSError, NotImplementedError):
                pass

    async def _run(self, fn: Callable[..., _T], *args: Any) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, lambda: fn(*args))

    # ------------------------------------------------------- Unterdrueckung
    def _is_suppressed_sync(self, dedup_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM suppressions WHERE dedup_key=?", (dedup_key,)).fetchone()
        return row is not None

    async def is_suppressed(self, dedup_key: str) -> bool:
        return await self._run(self._is_suppressed_sync, dedup_key)

    def _suppress_sync(self, dedup_key: str, reason: str, now: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO suppressions(dedup_key, created_at, reason) "
            "VALUES(?,?,?)", (dedup_key, now, reason))
        self._conn.commit()

    async def suppress(self, dedup_key: str, *, reason: str,
                       now: datetime | None = None) -> None:
        """Diesen Gedanken nicht wieder vorschlagen.

        Wird BEVOR ein Kandidat entsteht geprueft — nicht danach. Eine
        Unterdrueckung, die erst beim Adoptieren greift, haette den Menschen
        schon wieder gefragt.
        """
        await self._run(self._suppress_sync, dedup_key, reason,
                        _iso(now or utcnow()))

    def _unsuppress_sync(self, dedup_key: str) -> bool:
        cur = self._conn.execute("DELETE FROM suppressions WHERE dedup_key=?",
                                 (dedup_key,))
        self._conn.commit()
        return cur.rowcount > 0

    async def unsuppress(self, dedup_key: str) -> bool:
        """Nur fuer den ausdruecklichen Nutzerweg (er sagt es doch wieder)."""
        return await self._run(self._unsuppress_sync, dedup_key)

    async def suppressions(self) -> list[dict[str, str]]:
        def _read() -> list[dict[str, str]]:
            return [dict(r) for r in self._conn.execute(
                "SELECT dedup_key, created_at, reason FROM suppressions "
                "ORDER BY created_at DESC").fetchall()]
        return await self._run(_read)

    # ------------------------------------------------------------ Lesen
    @staticmethod
    def _from_row(row: sqlite3.Row) -> Candidate:
        raw = json.loads(row["evidence"] or "[]")
        return Candidate(
            id=row["id"], statement=row["statement"], dedup_key=row["dedup_key"],
            kind=row["kind"], memory_type=row["memory_type"],
            subject=row["subject"], sensitivity=row["sensitivity"],
            state=row["state"], ask_reason=row["ask_reason"],
            evidence=[Evidence(**e) for e in raw],
            first_seen=row["first_seen"], last_seen=row["last_seen"],
            contested_memory_id=row["contested_memory_id"],
            adopted_memory_id=row["adopted_memory_id"],
            asked_at=row["asked_at"], decided_at=row["decided_at"],
            valid_until=row["valid_until"], adoption_binding=json.loads(row["adoption_binding"]))

    def _get_sync(self, cand_id: str) -> Candidate | None:
        row = self._conn.execute("SELECT * FROM candidates WHERE id=?",
                                 (cand_id,)).fetchone()
        return self._from_row(row) if row else None

    async def get(self, cand_id: str) -> Candidate | None:
        return await self._run(self._get_sync, cand_id)

    def _open_by_key_sync(self, dedup_key: str) -> Candidate | None:
        row = self._conn.execute(
            f"SELECT * FROM candidates WHERE dedup_key=? AND state IN "
            f"({','.join('?' * len(OPEN_STATES))})",
            (dedup_key, *OPEN_STATES)).fetchone()
        return self._from_row(row) if row else None

    async def open_by_key(self, dedup_key: str) -> Candidate | None:
        return await self._run(self._open_by_key_sync, dedup_key)

    def _list_sync(self, states: tuple[str, ...]) -> list[Candidate]:
        rows = self._conn.execute(
            f"SELECT * FROM candidates WHERE state IN "
            f"({','.join('?' * len(states))}) ORDER BY last_seen DESC",
            states).fetchall()
        return [self._from_row(r) for r in rows]

    async def list_states(self, *states: str) -> list[Candidate]:
        return await self._run(self._list_sync, tuple(states) or OPEN_STATES)

    async def pending_decisions(self) -> list[Candidate]:
        """Was auf eine Entscheidung des Menschen wartet — und nur das."""
        return await self.list_states(ASK_PENDING, CONTESTED)

    async def count_open(self) -> int:
        def _count() -> int:
            return self._conn.execute(
                f"SELECT COUNT(*) FROM candidates WHERE state IN "
                f"({','.join('?' * len(OPEN_STATES))})", OPEN_STATES).fetchone()[0]
        return await self._run(_count)

    async def extraction_batch(self, activity_id, content_digest):
        def read():
            row = self._conn.execute("SELECT * FROM extraction_batches WHERE activity_id=?",
                                     (activity_id,)).fetchone()
            if row is None:
                return None
            if row['content_digest'] != content_digest or row['expires_at'] <= time.time():
                raise CandidateError('extraction_batch_binding_expired_or_changed')
            return {'payload': json.loads(row['payload']), 'next_index': row['next_index']}
        return await self._run(read)

    async def save_extraction_batch(self, activity_id, content_digest, payload):
        from solvio.memory.intent import looks_like_secret
        def secret(value):
            if isinstance(value, str): return looks_like_secret(value)
            if isinstance(value, dict): return any(secret(v) for v in value.values())
            if isinstance(value, (list, tuple)): return any(secret(v) for v in value)
            return False
        if secret(payload):
            raise CandidateError('extraction_batch_secret_shaped')
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        if len(encoded) > 16000:
            raise CandidateError('extraction_batch_too_large')
        def save():
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                now = time.time()
                self._conn.execute('DELETE FROM extraction_batches WHERE expires_at<=?', (now,))
                if self._conn.execute('SELECT 1 FROM extraction_batches WHERE activity_id=?', (activity_id,)).fetchone() is None:
                    if self._conn.execute('SELECT COUNT(*) FROM extraction_batches').fetchone()[0] >= MAX_OPEN_CANDIDATES:
                        raise CandidateError('extraction_batch_budget')
                    self._conn.execute('INSERT INTO extraction_batches(activity_id,content_digest,payload,expires_at) VALUES(?,?,?,?)',
                        (activity_id, content_digest, encoded, now + 3600))
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        await self._run(save)
        return await self.extraction_batch(activity_id, content_digest)

    async def advance_extraction_batch(self, activity_id, content_digest, index):
        def advance():
            self._conn.execute('UPDATE extraction_batches SET next_index=? WHERE activity_id=? '
                'AND content_digest=? AND next_index=?', (index+1, activity_id, content_digest, index))
            self._conn.commit()
        await self._run(advance)

    # --------------------------------------------------------- Schreiben
    def _upsert_sync(self, cand: Candidate, evidence: Evidence,
                     now: str) -> Candidate:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            result = self._upsert_locked(cand, evidence, now)
            self._conn.commit()
            return result
        except BaseException:
            self._conn.rollback()
            raise

    def _upsert_locked(self, cand: Candidate, evidence: Evidence, now: str) -> Candidate:
        existing = self._open_by_key_sync(cand.dedup_key)
        if existing is not None:
            if existing.state == ADOPTING:
                return existing
            # Wiederholung verlaengert die Evidenz — sie legt keinen zweiten
            # Kandidaten an. Innerhalb DESSELBEN Turns zaehlt sie gar nicht:
            # sonst koennte ein Turn sich selbst zur Gewissheit wiederholen.
            same_turn = any(e.message_id and e.message_id == evidence.message_id
                            for e in existing.evidence)
            if not same_turn:
                existing.evidence.append(evidence)
                existing.evidence = existing.evidence[-MAX_EVIDENCE:]
            existing.last_seen = now
            self._write_sync(existing)
            return existing

        open_count = self._conn.execute(
            f"SELECT COUNT(*) FROM candidates WHERE state IN "
            f"({','.join('?' * len(OPEN_STATES))})", OPEN_STATES).fetchone()[0]
        if open_count >= MAX_OPEN_CANDIDATES:
            raise CandidateError("candidate_budget_exhausted")

        cand.id = cand.id or uuid.uuid4().hex
        cand.evidence = [evidence]
        cand.first_seen = cand.first_seen or now
        cand.last_seen = now
        cand.state = GATHERING
        self._insert_sync(cand)
        return cand

    async def observe(self, cand: Candidate, evidence: Evidence, *,
                      now: datetime | None = None) -> Candidate:
        """Eine Beobachtung hinterlegen — neuer Kandidat oder mehr Evidenz."""
        return await self._run(self._upsert_sync, cand, evidence,
                               _iso(now or utcnow()))

    def _insert_sync(self, cand: Candidate) -> None:
        self._conn.execute(
            "INSERT INTO candidates(id, statement, dedup_key, kind, memory_type,"
            " subject, sensitivity, state, ask_reason, evidence, first_seen,"
            " last_seen, contested_memory_id, adopted_memory_id, asked_at,"
            " decided_at, valid_until)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cand.id, cand.statement, cand.dedup_key, cand.kind,
             cand.memory_type, cand.subject, cand.sensitivity, cand.state,
             cand.ask_reason,
             json.dumps([e.as_dict() for e in cand.evidence], ensure_ascii=False),
             cand.first_seen, cand.last_seen, cand.contested_memory_id,
             cand.adopted_memory_id, cand.asked_at, cand.decided_at,
             cand.valid_until))
        self._conn.commit()

    def _write_sync(self, cand: Candidate) -> None:
        self._conn.execute(
            "UPDATE candidates SET statement=?, kind=?, memory_type=?, subject=?,"
            " sensitivity=?, state=?, ask_reason=?, evidence=?, last_seen=?,"
            " contested_memory_id=?, adopted_memory_id=?, asked_at=?,"
            " decided_at=?, valid_until=?, adoption_binding=? WHERE id=?",
            (cand.statement, cand.kind, cand.memory_type, cand.subject,
             cand.sensitivity, cand.state, cand.ask_reason,
             json.dumps([e.as_dict() for e in cand.evidence], ensure_ascii=False),
             cand.last_seen, cand.contested_memory_id, cand.adopted_memory_id,
             cand.asked_at, cand.decided_at, cand.valid_until,
             json.dumps(cand.adoption_binding, sort_keys=True), cand.id))
        self._conn.commit()

    def _transition_sync(self, cand_id: str, target: str, now: str,
                         memory_id: str, contested: str, reason: str) -> Candidate:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            result = self._transition_locked(cand_id, target, now, memory_id, contested, reason)
            self._conn.commit()
            return result
        except BaseException:
            self._conn.rollback()
            raise

    def _transition_locked(self, cand_id, target, now, memory_id, contested, reason):
        cand = self._get_sync(cand_id)
        if cand is None:
            raise CandidateError(f"unknown candidate: {cand_id}")
        if target not in ALLOWED.get(cand.state, frozenset()):
            raise IllegalTransition(f"{cand.state} -> {target}")
        cand.state = target
        cand.last_seen = now
        if target == ADOPTED:
            cand.adopted_memory_id = memory_id or cand.adopted_memory_id
            cand.decided_at = now
        elif target in (DECLINED, EXPIRED):
            cand.decided_at = now
        elif target in (ASK_PENDING, CONTESTED):
            cand.asked_at = now
            cand.ask_reason = reason or cand.ask_reason
            if contested:
                cand.contested_memory_id = contested
        self._write_sync(cand)
        return cand

    async def claim_adoption(self, candidate_id: str, *, now=None, expected=None,
                             binding=None, memory_id="") -> Candidate | None:
        """Durable single winner before canonical memory is written.

        ADOPTING is neither personal truth nor a completed decision. A crash
        leaves an explicit unresolved adoption. Only exact canonical evidence
        or a proven automatic-write rollback can resolve that claim.
        """
        def claim():
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cand = self._get_sync(candidate_id)
                if (cand is None or ADOPTING not in ALLOWED.get(cand.state, ())
                        or (expected is not None and cand != expected)):
                    self._conn.rollback()
                    return None
                previous = cand.state
                cand.state = ADOPTING
                cand.last_seen = _iso(now or utcnow())
                cand.adopted_memory_id = memory_id or uuid.uuid4().hex
                cand.adoption_binding = dict(binding or {"mode": "confirmation"}, previous_state=previous)
                self._write_sync(cand)
                return cand
            except BaseException:
                self._conn.rollback()
                raise
        return await self._run(claim)

    async def release_rolled_back_adoption(self, claimed: Candidate) -> bool:
        """Internal automatic path, called only after a canonical rollback receipt."""
        def release():
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cand = self._get_sync(claimed.id)
                if (cand != claimed or cand is None or cand.state != ADOPTING
                        or cand.adoption_binding.get("mode") != "automatic"):
                    self._conn.rollback()
                    return False
                cand.state = cand.adoption_binding["previous_state"]
                cand.adopted_memory_id = ""
                cand.adoption_binding = {}
                self._write_sync(cand)
                return True
            except BaseException:
                self._conn.rollback()
                raise
        return await self._run(release)

    async def transition(self, cand_id: str, target: str, *,
                         memory_id: str = "", contested_memory_id: str = "",
                         reason: str = "", now: datetime | None = None) -> Candidate:
        """Einen Zustandsuebergang vollziehen — oder werfen.

        Der einzige Weg, den Zustand eines Kandidaten zu aendern. Ein Modell hat
        hier keinen Aufrufer: die Pipeline ruft, nachdem die Policy entschieden
        hat, und die iPhone-Wege rufen nach einer Freigabe.
        """
        if target not in STATES:
            raise CandidateError(f"unknown state: {target!r}")
        return await self._run(self._transition_sync, cand_id, target,
                               _iso(now or utcnow()), memory_id,
                               contested_memory_id, reason)

    # -------------------------------------------------------------- Fristen
    def _expire_sync(self, now: datetime) -> dict[str, int]:
        gather_cut = _iso(now - timedelta(days=GATHER_DAYS))
        ask_cut = _iso(now - timedelta(days=ASK_DAYS))
        stamp = _iso(now)
        gathered = self._conn.execute(
            "UPDATE candidates SET state=?, decided_at=? "
            "WHERE state=? AND last_seen < ?",
            (EXPIRED, stamp, GATHERING, gather_cut)).rowcount
        asked = self._conn.execute(
            f"UPDATE candidates SET state=?, decided_at=? "
            f"WHERE state IN (?,?) AND asked_at != '' AND asked_at < ?",
            (EXPIRED, stamp, ASK_PENDING, CONTESTED, ask_cut)).rowcount
        self._conn.execute('DELETE FROM extraction_batches WHERE expires_at<=?', (now.timestamp(),))
        self._conn.commit()
        return {"gathering_expired": max(gathered, 0), "asked_expired": max(asked, 0)}

    async def expire_due(self, *, now: datetime | None = None) -> dict[str, int]:
        """Was verhungert ist, wird geschlossen. Ein Uhrensprung zurueck
        verlaengert schlimmstenfalls das Warten — adoptiert wird dabei nie."""
        return await self._run(self._expire_sync, now or utcnow())

    # -------------------------------------------------------------- Betrieb
    async def stats(self) -> dict[str, int]:
        def _read() -> dict[str, int]:
            out = {s: 0 for s in STATES}
            for row in self._conn.execute(
                    "SELECT state, COUNT(*) c FROM candidates GROUP BY state"):
                out[row["state"]] = row["c"]
            out["suppressions"] = self._conn.execute(
                "SELECT COUNT(*) FROM suppressions").fetchone()[0]
            return out
        return await self._run(_read)

    def _close_sync(self) -> None:
        if self._closed:
            return
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001
            pass
        self._conn.close()
        self._closed = True

    async def close(self) -> None:
        await self._run(self._close_sync)
        self._pool.shutdown(wait=True)
