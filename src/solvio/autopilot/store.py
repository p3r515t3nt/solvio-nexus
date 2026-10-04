"""Das Autopilot-Ledger — es fuehrt Entscheidungen und Belege, keine Gespraeche.

Bauart uebernommen vom Agent Run Ledger (ADR-0029), samt seiner haerteste
Lehre: **Laengendeckel sind der Grund, warum kein Transkript hineinpasst.** Ein
Deckel, den man erhoehen muesste, um ein Modellgespraech abzulegen, ist eine
sichtbare Entscheidung — genau das ist beabsichtigt.

Es beantwortet: *In welchem Zustand ist der Milestone? Welcher Contract gilt,
und mit welchem Hash? Was ist gemessen worden? Was ist offen? Wer hat was
entschieden, und woraufhin? Was hat es gekostet?* — auch nach `kill -9`.

Es ist ausdruecklich NICHT das Agent Run Ledger, nicht das Freigabejournal und
nicht der Posteingang. Der Posteingang wird geteilt (`ProactiveStore`), das
Auftragsbuch nicht.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("autopilot")

DEFAULT_PATH = "~/.solvio/autopilot.sqlite3"
PATH_ENV = "SOLVIO_AUTOPILOT_DB"
SCHEMA_VERSION = 1

# -- Zustaende (Vertrag §3). Geschlossen. -------------------------------------
PLANNING = "PLANNING"
BUILDING = "BUILDING"
TESTING = "TESTING"
REVIEWING = "REVIEWING"
FIXING = "FIXING"
READY = "READY"
HUMAN_REQUIRED = "HUMAN_REQUIRED"
BLOCKED = "BLOCKED"
STOPPED = "STOPPED"

ALL_STATES = frozenset({PLANNING, BUILDING, TESTING, REVIEWING, FIXING,
                        READY, HUMAN_REQUIRED, BLOCKED, STOPPED})
TERMINAL_STATES = frozenset({READY, STOPPED})
#: Zustaende, in denen der Treiber NICHT von selbst weiterlaeuft.
PARKED_STATES = frozenset({HUMAN_REQUIRED, BLOCKED})

#: Gruende fuer BLOCKED. Geschlossen — ein Grund ohne Namen ist eine Ausrede.
BLOCK_REASONS = frozenset({
    "capacity", "contract_change_required", "builder_unavailable_all",
})

#: Phasenarten in der Chronik.
PHASE_KINDS = frozenset({"plan", "build", "test", "review", "fix", "checkpoint"})

#: Ereignisarten. Geschlossen — die Chronik liest genau diese.
EVENT_KINDS = frozenset({
    "state_changed", "phase_started", "phase_finished", "evidence_recorded",
    "evidence_reused", "finding_opened", "finding_closed", "decision_recorded",
    "quota_failover", "builder_switched", "boundary_opened", "boundary_resolved",
    "loop_detected", "interrupted", "recovered", "notice_sent",
    "contract_pinned", "contract_drift", "security_finding",
    "route_decided", "checkpoint_published",
})

#: Schwere eines Findings. `blocker` und `major` verhindern READY.
SEVERITIES = ("blocker", "major", "minor", "info")
BLOCKING_SEVERITIES = frozenset({"blocker", "major"})

#: Status eines Akzeptanzkriteriums.
CRIT_OPEN = "open"
CRIT_PROVEN = "proven"
CRIT_REFUTED = "refuted"
CRIT_STATES = frozenset({CRIT_OPEN, CRIT_PROVEN, CRIT_REFUTED})

#: Rollen fuer Capacity (Amendment 8). Getrennt, weil ihre Anbieter getrennt
#: sind: ein Claude-Limit darf keinen Codex-Build blockieren.
ROLE_LEAD = "technical_lead"
ROLE_BUILDER = "builder"
ROLE_ADVISOR = "advisor"
ROLES = frozenset({ROLE_LEAD, ROLE_BUILDER, ROLE_ADVISOR})

# -- Laengendeckel ------------------------------------------------------------
MAX_SUMMARY = 1_000
MAX_TITLE = 300
MAX_DETAIL = 2_000
MAX_JSON = 8_000
MAX_REF = 200

RETENTION_SECONDS = 180 * 24 * 3600


class LedgerError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def resolve_path(path: str = "") -> str:
    chosen = path or os.environ.get(PATH_ENV, "") or DEFAULT_PATH
    return os.path.abspath(os.path.expanduser(chosen))


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def utcnow() -> float:
    return time.time()


def _cap(text: Any, limit: int) -> str:
    return str(text or "")[:limit]


@dataclass
class Milestone:
    milestone_id: str
    state: str
    contract_version: str
    contract_hash: str
    contract_json: str
    repository: str = ""
    block_reason: str = ""
    resume_at: float = 0.0
    builder: str = ""
    model: str = ""
    current_task: str = ""
    base_commit: str = ""
    last_commit: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    state_before_park: str = ""


@dataclass
class Finding:
    finding_id: str
    milestone_id: str
    severity: str
    title: str
    detail: str = ""
    origin: str = ""
    state: str = "open"
    opened_at: float = 0.0
    closed_at: float = 0.0


@dataclass
class Evidence:
    evidence_id: str
    milestone_id: str
    kind: str
    commit: str
    env_fingerprint: str
    ok: bool
    summary: str
    payload_json: str = ""
    measured_at: float = 0.0


class AutopilotLedger:
    """Eine Datei, eine Wahrheit. WAL, 0600, geschlossene Kantentabelle."""

    def __init__(self, path: str = "") -> None:
        self.path = resolve_path(path)
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        neu = not os.path.exists(self.path)
        self._db = sqlite3.connect(self.path, isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        if neu:
            os.chmod(self.path, 0o600)
        self._migrate()

    # -- Schema ---------------------------------------------------------------
    def _migrate(self) -> None:
        cur = self._db.execute("PRAGMA user_version")
        fassung = int(cur.fetchone()[0])
        if fassung >= SCHEMA_VERSION:
            return
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS milestones (
            milestone_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            contract_json TEXT NOT NULL,
            repository TEXT NOT NULL DEFAULT '',
            block_reason TEXT NOT NULL DEFAULT '',
            resume_at REAL NOT NULL DEFAULT 0,
            builder TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            current_task TEXT NOT NULL DEFAULT '',
            base_commit TEXT NOT NULL DEFAULT '',
            last_commit TEXT NOT NULL DEFAULT '',
            state_before_park TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL);

        CREATE TABLE IF NOT EXISTS criteria (
            milestone_id TEXT NOT NULL,
            key TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            text TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'open',
            evidence_ref TEXT NOT NULL DEFAULT '',
            decided_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (milestone_id, key),
            FOREIGN KEY (milestone_id) REFERENCES milestones(milestone_id));

        CREATE TABLE IF NOT EXISTS phases (
            phase_id TEXT PRIMARY KEY,
            milestone_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            kind TEXT NOT NULL,
            state TEXT NOT NULL,
            builder TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            base_commit TEXT NOT NULL DEFAULT '',
            result_commit TEXT NOT NULL DEFAULT '',
            attempt_digest TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            child_pgid INTEGER NOT NULL DEFAULT 0,
            child_started_at REAL NOT NULL DEFAULT 0,
            child_executable TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL,
            finished_at REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (milestone_id) REFERENCES milestones(milestone_id));

        CREATE TABLE IF NOT EXISTS evidence (
            evidence_id TEXT PRIMARY KEY,
            milestone_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            commit_sha TEXT NOT NULL DEFAULT '',
            env_fingerprint TEXT NOT NULL DEFAULT '',
            ok INTEGER NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '',
            measured_at REAL NOT NULL,
            FOREIGN KEY (milestone_id) REFERENCES milestones(milestone_id));

        CREATE TABLE IF NOT EXISTS findings (
            finding_id TEXT PRIMARY KEY,
            milestone_id TEXT NOT NULL,
            severity TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'open',
            opened_at REAL NOT NULL,
            closed_at REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (milestone_id) REFERENCES milestones(milestone_id));

        CREATE TABLE IF NOT EXISTS decisions (
            decision_id TEXT PRIMARY KEY,
            milestone_id TEXT NOT NULL,
            role TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            verdict TEXT NOT NULL,
            next_action TEXT NOT NULL DEFAULT '',
            rationale TEXT NOT NULL DEFAULT '',
            digest TEXT NOT NULL DEFAULT '',
            decided_at REAL NOT NULL,
            FOREIGN KEY (milestone_id) REFERENCES milestones(milestone_id));

        CREATE TABLE IF NOT EXISTS handoffs (
            handoff_id TEXT PRIMARY KEY,
            milestone_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (milestone_id) REFERENCES milestones(milestone_id));

        CREATE TABLE IF NOT EXISTS usage (
            usage_id TEXT PRIMARY KEY,
            milestone_id TEXT NOT NULL,
            role TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            calls INTEGER NOT NULL DEFAULT 0,
            wall_seconds REAL NOT NULL DEFAULT 0,
            provider_tokens INTEGER,
            note TEXT NOT NULL DEFAULT '',
            recorded_at REAL NOT NULL,
            FOREIGN KEY (milestone_id) REFERENCES milestones(milestone_id));

        CREATE TABLE IF NOT EXISTS capacity (
            milestone_id TEXT NOT NULL,
            role TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            signals_json TEXT NOT NULL DEFAULT '{}',
            measured_at REAL NOT NULL,
            PRIMARY KEY (milestone_id, role));

        CREATE TABLE IF NOT EXISTS boundaries (
            boundary_id TEXT PRIMARY KEY,
            milestone_id TEXT NOT NULL,
            category TEXT NOT NULL,
            kind TEXT NOT NULL,
            question TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'open',
            answer TEXT NOT NULL DEFAULT '',
            opened_at REAL NOT NULL,
            resolved_at REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (milestone_id) REFERENCES milestones(milestone_id));

        CREATE TABLE IF NOT EXISTS events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            milestone_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            ref TEXT NOT NULL DEFAULT '',
            at REAL NOT NULL);

        CREATE INDEX IF NOT EXISTS idx_events_ms ON events(milestone_id, at DESC);
        CREATE INDEX IF NOT EXISTS idx_phases_ms ON phases(milestone_id, seq);
        CREATE INDEX IF NOT EXISTS idx_findings_ms ON findings(milestone_id, state);
        CREATE INDEX IF NOT EXISTS idx_evidence_ms ON evidence(milestone_id, kind);
        """)
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        log.info("autopilot.ledger_ready", path=self.path,
                 schema=SCHEMA_VERSION)

    def close(self) -> None:
        self._db.close()

    # -- Milestones -----------------------------------------------------------
    def create_milestone(self, contract, *, repository: str = "",
                         now: float = 0.0) -> Milestone:
        """Legt den Milestone an und PINNT den Contract (Amendment 5).

        Ab hier ist der Ledger die Quelle. Eine Datei im Arbeitsbaum ist von
        diesem Moment an bestenfalls ein Vorschlag.
        """
        moment = now or utcnow()
        digest = contract.digest()
        try:
            self._db.execute(
                "INSERT INTO milestones (milestone_id, state, contract_version,"
                " contract_hash, contract_json, repository, base_commit,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (contract.milestone_id, PLANNING, contract.version, digest,
                 contract.canonical_json(), repository or contract.repository,
                 "", moment, moment))
        except sqlite3.IntegrityError:
            raise LedgerError("milestone_exists", contract.milestone_id) from None
        for crit in contract.acceptance_criteria:
            self._db.execute(
                "INSERT INTO criteria (milestone_id, key, evidence_type, text,"
                " state) VALUES (?,?,?,?,?)",
                (contract.milestone_id, crit.key, crit.evidence_type,
                 _cap(crit.text, MAX_TITLE), CRIT_OPEN))
        self.record_event(contract.milestone_id, "contract_pinned",
                          f"{contract.version} gepinnt", ref=digest, now=moment)
        return self.milestone(contract.milestone_id)

    def milestone(self, milestone_id: str) -> Milestone:
        row = self._db.execute(
            "SELECT * FROM milestones WHERE milestone_id=?",
            (milestone_id,)).fetchone()
        if row is None:
            raise LedgerError("unknown_milestone", milestone_id)
        return Milestone(**{k: row[k] for k in row.keys()})

    def milestones(self, *, active_only: bool = False) -> list[Milestone]:
        sql = "SELECT * FROM milestones"
        if active_only:
            marks = ",".join("?" * len(TERMINAL_STATES))
            sql += f" WHERE state NOT IN ({marks})"
            rows = self._db.execute(sql + " ORDER BY created_at DESC",
                                    tuple(sorted(TERMINAL_STATES))).fetchall()
        else:
            rows = self._db.execute(sql + " ORDER BY created_at DESC").fetchall()
        return [Milestone(**{k: r[k] for k in r.keys()}) for r in rows]

    def set_fields(self, milestone_id: str, *, now: float = 0.0, **felder) -> None:
        erlaubt = {"builder", "model", "current_task", "base_commit",
                   "last_commit", "block_reason", "resume_at",
                   "state_before_park", "repository"}
        unbekannt = sorted(set(felder) - erlaubt)
        if unbekannt:
            raise LedgerError("unknown_field", ", ".join(unbekannt))
        if not felder:
            return
        teile = ", ".join(f"{k}=?" for k in felder)
        werte = list(felder.values()) + [now or utcnow(), milestone_id]
        self._db.execute(
            f"UPDATE milestones SET {teile}, updated_at=? WHERE milestone_id=?",
            werte)

    def set_state(self, milestone_id: str, state: str, *,
                  summary: str = "", now: float = 0.0) -> None:
        """Nur `machine.transition` ruft das. Die Kantenpruefung sitzt dort.

        Bewusst getrennt: der Ledger fuehrt Buch, die Maschine entscheidet.
        Ein Ledger, der auch die Regeln kennt, hat zwei Wahrheiten.
        """
        if state not in ALL_STATES:
            raise LedgerError("unknown_state", state)
        moment = now or utcnow()
        self._db.execute(
            "UPDATE milestones SET state=?, updated_at=? WHERE milestone_id=?",
            (state, moment, milestone_id))
        self.record_event(milestone_id, "state_changed",
                          summary or state, ref=state, now=moment)

    # -- Kriterien ------------------------------------------------------------
    def criteria(self, milestone_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM criteria WHERE milestone_id=? ORDER BY key",
            (milestone_id,)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def set_criterion(self, milestone_id: str, key: str, *, state: str,
                      evidence_ref: str, now: float = 0.0) -> None:
        """Setzt einen Kriteriumsstatus. Ohne Evidence-Referenz geht `proven` nicht.

        Diese eine Zeile ist Amendment 9 in Code: ein freies Modellurteil kann
        kein Kriterium beweisen, weil `proven` ohne Referenz gar nicht
        schreibbar ist.
        """
        if state not in CRIT_STATES:
            raise LedgerError("unknown_criterion_state", state)
        if state == CRIT_PROVEN and not evidence_ref.strip():
            raise LedgerError("proven_without_evidence", key)
        cur = self._db.execute(
            "UPDATE criteria SET state=?, evidence_ref=?, decided_at=?"
            " WHERE milestone_id=? AND key=?",
            (state, _cap(evidence_ref, MAX_REF), now or utcnow(),
             milestone_id, key))
        if cur.rowcount == 0:
            raise LedgerError("unknown_criterion", key)

    def acceptance_counts(self, milestone_id: str) -> tuple[int, int]:
        row = self._db.execute(
            "SELECT COUNT(*) AS gesamt,"
            " SUM(CASE WHEN state='proven' THEN 1 ELSE 0 END) AS bewiesen"
            " FROM criteria WHERE milestone_id=?", (milestone_id,)).fetchone()
        return int(row["bewiesen"] or 0), int(row["gesamt"] or 0)

    # -- Phasen ---------------------------------------------------------------
    def start_phase(self, milestone_id: str, *, kind: str, builder: str = "",
                    model: str = "", base_commit: str = "",
                    attempt_digest: str = "", now: float = 0.0) -> str:
        if kind not in PHASE_KINDS:
            raise LedgerError("unknown_phase_kind", kind)
        moment = now or utcnow()
        seq = int(self._db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM phases WHERE milestone_id=?",
            (milestone_id,)).fetchone()[0])
        phase_id = new_id("ph")
        self._db.execute(
            "INSERT INTO phases (phase_id, milestone_id, seq, kind, state,"
            " builder, model, base_commit, attempt_digest, started_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (phase_id, milestone_id, seq, kind, "running", builder, model,
             base_commit, attempt_digest, moment))
        self.record_event(milestone_id, "phase_started", f"{kind} #{seq}",
                          ref=phase_id, now=moment)
        return phase_id

    def finish_phase(self, phase_id: str, *, state: str, summary: str = "",
                     result_commit: str = "", now: float = 0.0) -> None:
        moment = now or utcnow()
        row = self._db.execute(
            "SELECT milestone_id, kind FROM phases WHERE phase_id=?",
            (phase_id,)).fetchone()
        if row is None:
            raise LedgerError("unknown_phase", phase_id)
        self._db.execute(
            "UPDATE phases SET state=?, summary=?, result_commit=?,"
            " finished_at=? WHERE phase_id=?",
            (state, _cap(summary, MAX_SUMMARY), result_commit, moment, phase_id))
        self.record_event(row["milestone_id"], "phase_finished",
                          f"{row['kind']}: {state}", ref=phase_id, now=moment)

    def set_phase_child(self, phase_id: str, *, pgid: int, started_at: float,
                        executable: str) -> None:
        """Der Kindprozess einer Phase — fuer die Abstimmung nach `kill -9`.

        Drei Felder, nicht eines: eine PID ist kein Besitztitel. Erst pgid UND
        Startzeit UND Programmpfad zusammen identifizieren ein Kind sicher
        genug, um es beenden zu duerfen.
        """
        self._db.execute(
            "UPDATE phases SET child_pgid=?, child_started_at=?,"
            " child_executable=? WHERE phase_id=?",
            (int(pgid), float(started_at), _cap(executable, MAX_REF), phase_id))

    def phases(self, milestone_id: str, *, limit: int = 50) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM phases WHERE milestone_id=? ORDER BY seq DESC"
            " LIMIT ?", (milestone_id, limit)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def open_phases(self, milestone_id: str = "") -> list[dict]:
        if milestone_id:
            rows = self._db.execute(
                "SELECT * FROM phases WHERE state='running' AND milestone_id=?"
                " ORDER BY seq", (milestone_id,)).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM phases WHERE state='running' ORDER BY seq"
            ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    # -- Evidence -------------------------------------------------------------
    def record_evidence(self, milestone_id: str, *, kind: str, commit: str,
                        env_fingerprint: str, ok: bool, summary: str,
                        payload: dict | None = None,
                        now: float = 0.0) -> str:
        moment = now or utcnow()
        evidence_id = new_id("ev")
        self._db.execute(
            "INSERT INTO evidence (evidence_id, milestone_id, kind, commit_sha,"
            " env_fingerprint, ok, summary, payload_json, measured_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (evidence_id, milestone_id, kind, commit, env_fingerprint,
             1 if ok else 0, _cap(summary, MAX_SUMMARY),
             _cap(json.dumps(payload or {}, ensure_ascii=False), MAX_JSON),
             moment))
        self.record_event(milestone_id, "evidence_recorded",
                          f"{kind}: {'ok' if ok else 'rot'}",
                          ref=evidence_id, now=moment)
        return evidence_id

    def evidence(self, evidence_id: str) -> Evidence | None:
        row = self._db.execute("SELECT * FROM evidence WHERE evidence_id=?",
                               (evidence_id,)).fetchone()
        if row is None:
            return None
        return Evidence(evidence_id=row["evidence_id"],
                        milestone_id=row["milestone_id"], kind=row["kind"],
                        commit=row["commit_sha"],
                        env_fingerprint=row["env_fingerprint"],
                        ok=bool(row["ok"]), summary=row["summary"],
                        payload_json=row["payload_json"],
                        measured_at=row["measured_at"])

    def fresh_evidence(self, milestone_id: str, *, kind: str, commit: str,
                       env_fingerprint: str) -> Evidence | None:
        """Wiederverwendbare Evidence: gleicher Commit UND gleiche Umgebung.

        Die Umgebung steht hier nicht aus Vorsicht, sondern aus Erfahrung: beim
        Offsite-Release fielen 14 Zusicherungen, weil eine frische Umgebung ohne
        Extras lief. Das sah wie ein Codefehler aus und war keiner.
        """
        row = self._db.execute(
            "SELECT * FROM evidence WHERE milestone_id=? AND kind=?"
            " AND commit_sha=? AND env_fingerprint=?"
            " ORDER BY measured_at DESC, evidence_id DESC LIMIT 1",
            (milestone_id, kind, commit, env_fingerprint)).fetchone()
        if row is None:
            return None
        return self.evidence(row["evidence_id"])

    def note_evidence_reused(self, milestone_id: str, evidence_id: str,
                             *, now: float = 0.0) -> None:
        self.record_event(milestone_id, "evidence_reused",
                          "Messung wiederverwendet", ref=evidence_id, now=now)

    # -- Findings -------------------------------------------------------------
    def open_finding(self, milestone_id: str, *, severity: str, title: str,
                     detail: str = "", origin: str = "",
                     now: float = 0.0) -> str:
        if severity not in SEVERITIES:
            raise LedgerError("unknown_severity", severity)
        moment = now or utcnow()
        finding_id = new_id("f")
        self._db.execute(
            "INSERT INTO findings (finding_id, milestone_id, severity, title,"
            " detail, origin, state, opened_at) VALUES (?,?,?,?,?,?,?,?)",
            (finding_id, milestone_id, severity, _cap(title, MAX_TITLE),
             _cap(detail, MAX_DETAIL), origin, "open", moment))
        kind = "security_finding" if origin == "security" else "finding_opened"
        self.record_event(milestone_id, kind, f"{severity}: {title}",
                          ref=finding_id, now=moment)
        return finding_id

    def close_finding(self, finding_id: str, *, now: float = 0.0) -> None:
        moment = now or utcnow()
        row = self._db.execute(
            "SELECT milestone_id, title FROM findings WHERE finding_id=?",
            (finding_id,)).fetchone()
        if row is None:
            raise LedgerError("unknown_finding", finding_id)
        self._db.execute(
            "UPDATE findings SET state='closed', closed_at=? WHERE finding_id=?",
            (moment, finding_id))
        self.record_event(row["milestone_id"], "finding_closed", row["title"],
                          ref=finding_id, now=moment)

    def findings(self, milestone_id: str, *, open_only: bool = True) -> list[Finding]:
        sql = "SELECT * FROM findings WHERE milestone_id=?"
        if open_only:
            sql += " AND state='open'"
        rows = self._db.execute(sql + " ORDER BY opened_at", (milestone_id,)).fetchall()
        return [Finding(finding_id=r["finding_id"], milestone_id=r["milestone_id"],
                        severity=r["severity"], title=r["title"],
                        detail=r["detail"], origin=r["origin"], state=r["state"],
                        opened_at=r["opened_at"], closed_at=r["closed_at"])
                for r in rows]

    def blocking_findings(self, milestone_id: str) -> list[Finding]:
        return [f for f in self.findings(milestone_id, open_only=True)
                if f.severity in BLOCKING_SEVERITIES]

    # -- Entscheidungen, Handoffs, Verbrauch, Capacity -------------------------
    def record_route(self, milestone_id: str, route, *, now: float = 0.0) -> None:
        """Die Routenwahl — MODEL_FIT, EXECUTION_FIT und ROUTE getrennt.

        Sie wird als Entscheidung gefuehrt, nicht als Notiz: wer nur festhaelt,
        WAS gelaufen ist, kann nach drei Monaten eine dauerhafte
        Notloesung nicht mehr von einer Architekturentscheidung unterscheiden.
        """
        self.record_decision(milestone_id, role="routing",
                             verdict=route.reason, model=route.route,
                             next_action=route.trigger,
                             rationale=route.line() + (f" ({route.detail})"
                                                       if route.detail else ""),
                             now=now)
        self.record_event(milestone_id, "route_decided", route.line(),
                          ref=route.trigger, now=now)

    def record_publication(self, milestone_id: str, publication,
                           *, now: float = 0.0) -> None:
        """Die Checkpoint-Publikation. Die Evidenz liefert git, nicht ein Modell."""
        self.record_evidence(
            milestone_id, kind="checkpoint_ref", commit=publication.commit,
            env_fingerprint="", ok=True,
            summary=f"{publication.canonical_ref} -> {publication.commit[:12]}",
            payload=publication.as_dict(), now=now)
        self.record_event(milestone_id, "checkpoint_published",
                          publication.canonical_ref,
                          ref=publication.checkpoint_id, now=now)

    def record_decision(self, milestone_id: str, *, role: str, verdict: str,
                        model: str = "", next_action: str = "",
                        rationale: str = "", digest: str = "",
                        now: float = 0.0) -> str:
        moment = now or utcnow()
        decision_id = new_id("d")
        self._db.execute(
            "INSERT INTO decisions (decision_id, milestone_id, role, model,"
            " verdict, next_action, rationale, digest, decided_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (decision_id, milestone_id, role, model, verdict,
             _cap(next_action, MAX_TITLE), _cap(rationale, MAX_DETAIL),
             digest, moment))
        self.record_event(milestone_id, "decision_recorded",
                          f"{role}: {verdict}", ref=decision_id, now=moment)
        return decision_id

    def decisions(self, milestone_id: str, *, limit: int = 20) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM decisions WHERE milestone_id=?"
            " ORDER BY decided_at DESC, decision_id DESC LIMIT ?",
            (milestone_id, limit)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def last_decision(self, milestone_id: str) -> dict | None:
        rows = self.decisions(milestone_id, limit=1)
        return rows[0] if rows else None

    def record_handoff(self, milestone_id: str, payload: dict,
                       *, now: float = 0.0) -> str:
        handoff_id = new_id("h")
        self._db.execute(
            "INSERT INTO handoffs (handoff_id, milestone_id, payload_json,"
            " created_at) VALUES (?,?,?,?)",
            (handoff_id, milestone_id,
             _cap(json.dumps(payload, ensure_ascii=False), MAX_JSON),
             now or utcnow()))
        return handoff_id

    def handoffs(self, milestone_id: str, *, limit: int = 10) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM handoffs WHERE milestone_id=? ORDER BY created_at"
            " DESC LIMIT ?", (milestone_id, limit)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def record_usage(self, milestone_id: str, *, role: str, provider: str = "",
                     model: str = "", calls: int = 1, wall_seconds: float = 0.0,
                     provider_tokens: int | None = None, note: str = "",
                     now: float = 0.0) -> None:
        """Verbrauch. `provider_tokens=None` heisst: der Anbieter hat es nicht
        gesagt — und dann wird auch nichts erfunden."""
        self._db.execute(
            "INSERT INTO usage (usage_id, milestone_id, role, provider, model,"
            " calls, wall_seconds, provider_tokens, note, recorded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (new_id("u"), milestone_id, role, provider, model, int(calls),
             float(wall_seconds), provider_tokens, _cap(note, MAX_TITLE),
             now or utcnow()))

    def usage_rows(self, milestone_id: str, *, role: str = "",
                   limit: int = 500) -> list[dict]:
        """Die Rohzeilen des Verbrauchs — der Capacity-Preflight liest sie.

        Das lokale Buch ist die einzige Verbrauchsquelle, die SOLVIO wirklich
        besitzt. Ein Anbieter-Kontostand waere fremde Auskunft; hier steht,
        was bei UNS passiert ist.
        """
        if role:
            rows = self._db.execute(
                "SELECT * FROM usage WHERE milestone_id=? AND role=?"
                " ORDER BY recorded_at DESC LIMIT ?",
                (milestone_id, role, limit)).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM usage WHERE milestone_id=?"
                " ORDER BY recorded_at DESC LIMIT ?",
                (milestone_id, limit)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def usage_summary(self, milestone_id: str) -> dict[str, Any]:
        rows = self._db.execute(
            "SELECT role, COUNT(*) AS eintraege, SUM(calls) AS calls,"
            " SUM(wall_seconds) AS sekunden,"
            " SUM(CASE WHEN provider_tokens IS NULL THEN 0 ELSE provider_tokens END) AS tokens,"
            " SUM(CASE WHEN provider_tokens IS NULL THEN 1 ELSE 0 END) AS ohne_angabe"
            " FROM usage WHERE milestone_id=? GROUP BY role", (milestone_id,)
        ).fetchall()
        out: dict[str, Any] = {}
        for r in rows:
            out[r["role"]] = {
                "calls": int(r["calls"] or 0),
                "wall_seconds": round(float(r["sekunden"] or 0.0), 1),
                "provider_tokens": int(r["tokens"] or 0),
                "eintraege_ohne_tokenangabe": int(r["ohne_angabe"] or 0)}
        return out

    def set_capacity(self, milestone_id: str, *, role: str, provider: str,
                     state: str, signals: dict, now: float = 0.0) -> None:
        if role not in ROLES:
            raise LedgerError("unknown_role", role)
        self._db.execute(
            "INSERT INTO capacity (milestone_id, role, provider, state,"
            " signals_json, measured_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(milestone_id, role) DO UPDATE SET provider=excluded.provider,"
            " state=excluded.state, signals_json=excluded.signals_json,"
            " measured_at=excluded.measured_at",
            (milestone_id, role, provider, state,
             _cap(json.dumps(signals, ensure_ascii=False), MAX_JSON),
             now or utcnow()))

    def capacity(self, milestone_id: str) -> dict[str, dict]:
        rows = self._db.execute(
            "SELECT * FROM capacity WHERE milestone_id=?", (milestone_id,)).fetchall()
        return {r["role"]: {"provider": r["provider"], "state": r["state"],
                            "signals": json.loads(r["signals_json"] or "{}"),
                            "measured_at": r["measured_at"]} for r in rows}

    # -- Grenzen --------------------------------------------------------------
    def open_boundary(self, milestone_id: str, *, category: str, kind: str,
                      question: str, now: float = 0.0) -> str:
        moment = now or utcnow()
        boundary_id = new_id("b")
        self._db.execute(
            "INSERT INTO boundaries (boundary_id, milestone_id, category, kind,"
            " question, state, opened_at) VALUES (?,?,?,?,?,?,?)",
            (boundary_id, milestone_id, category, kind,
             _cap(question, MAX_DETAIL), "open", moment))
        self.record_event(milestone_id, "boundary_opened", f"{category}/{kind}",
                          ref=boundary_id, now=moment)
        return boundary_id

    def resolve_boundary(self, boundary_id: str, answer: str,
                         *, now: float = 0.0) -> str:
        moment = now or utcnow()
        row = self._db.execute(
            "SELECT milestone_id, state FROM boundaries WHERE boundary_id=?",
            (boundary_id,)).fetchone()
        if row is None:
            raise LedgerError("unknown_boundary", boundary_id)
        if row["state"] != "open":
            raise LedgerError("boundary_not_open", boundary_id)
        self._db.execute(
            "UPDATE boundaries SET state='resolved', answer=?, resolved_at=?"
            " WHERE boundary_id=?", (_cap(answer, MAX_DETAIL), moment, boundary_id))
        self.record_event(row["milestone_id"], "boundary_resolved",
                          "beantwortet", ref=boundary_id, now=moment)
        return row["milestone_id"]

    def open_boundaries(self, milestone_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM boundaries WHERE milestone_id=? AND state='open'"
            " ORDER BY opened_at", (milestone_id,)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    # -- Chronik --------------------------------------------------------------
    def record_event(self, milestone_id: str, kind: str, summary: str = "",
                     *, ref: str = "", now: float = 0.0) -> None:
        if kind not in EVENT_KINDS:
            raise LedgerError("unknown_event_kind", kind)
        self._db.execute(
            "INSERT INTO events (milestone_id, kind, summary, ref, at)"
            " VALUES (?,?,?,?,?)",
            (milestone_id, kind, _cap(summary, MAX_SUMMARY),
             _cap(ref, MAX_REF), now or utcnow()))

    def events(self, milestone_id: str, *, limit: int = 50) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM events WHERE milestone_id=? ORDER BY at DESC,"
            " event_id DESC LIMIT ?", (milestone_id, limit)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
